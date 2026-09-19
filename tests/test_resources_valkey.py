import json
from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from gimme.control import AWSElastiCacheValkeyResource, ControlState, StateStore
from gimme.resources_postgres import ResourceError
import gimme.server as server_module

EXAMPLE = Path(__file__).resolve().parents[1] / "config/state.example.json"
NAME = "example-elasticache-valkey"


def valkey(**updates) -> AWSElastiCacheValkeyResource:
    values = json.loads(EXAMPLE.read_text())["resources"][NAME] | updates
    return AWSElastiCacheValkeyResource.model_validate(values)


def use_state(tmp_path: Path, monkeypatch, *, registered: bool = True) -> ControlState:
    state = ControlState.model_validate(json.loads(EXAMPLE.read_text()))
    if not registered:
        state = state.model_copy(update={"resources": {
            key: value for key, value in state.resources.items() if key != NAME
        }})
    selected = StateStore(tmp_path / "state")
    selected.save(state)
    monkeypatch.setattr(server_module, "store", selected)
    return state


def test_the_documented_example_registers_with_its_defaults() -> None:
    resource = valkey()

    assert resource.provider == "aws_elasticache_valkey" and resource.kind == "valkey"
    assert valkey(snapshot_retention_days=7) == resource
    assert resource.retain_on_removal is True


@pytest.mark.parametrize(
    "updates",
    [
        {"engine_version": "8.1"}, {"engine_version": "7.2"}, {"engine_version": "9"},
        {"engine_version": "9.x"}, {"engine_version": "9.0-rc1"}, {"engine_version": "9.0.0.1"},
        {"node_type": "db.t3.medium"}, {"node_type": "cache.m7g"},
        {"node_type": "cache.m7g.large; x"},
        {"security_group_id": "sg-xyz"},
        {"snapshot_retention_days": 0}, {"snapshot_retention_days": 36},
        {"snapshot_window": "3:00-4:00"}, {"snapshot_window": "03:00-03:30"},
        {"snapshot_window": "03:00-03:00"}, {"snapshot_window": "24:00-25:00"},
        {"maintenance_window": "sun:05:00-sun:07:00"},
        {"maintenance_window": "sun:05:00-sun:05:30"},
        {"maintenance_window": "Sun:05:00-sun:06:00"}, {"maintenance_window": "05:00-06:00"},
        {"maintenance_window": "sun:05:00-mon:06:00"},
        {"workload_secret_store": "Bad Name"}, {"unknown_field": 1},
    ],
)
def test_invalid_definitions_are_refused(updates) -> None:
    with pytest.raises(ValidationError):
        valkey(**updates)


@pytest.mark.parametrize(
    ("snapshot", "maintenance"),
    [
        ("05:00-06:00", "sun:05:00-sun:06:00"),
        ("04:30-05:30", "sun:05:00-sun:06:00"),
        ("23:30-00:30", "mon:00:00-mon:01:00"),  # snapshot wraps midnight
        ("00:00-01:00", "sun:23:30-mon:00:30"),  # maintenance wraps the week
        ("22:00-23:30", "wed:23:00-thu:00:00"),
    ],
)
def test_overlapping_windows_are_refused(snapshot, maintenance) -> None:
    with pytest.raises(ValidationError, match="must not overlap"):
        valkey(snapshot_window=snapshot, maintenance_window=maintenance)


@pytest.mark.parametrize(
    ("snapshot", "maintenance"),
    [
        ("03:00-04:00", "sun:04:00-sun:05:00"),  # adjacent is not overlapping
        ("23:00-00:00", "sun:00:00-sun:01:00"),
        ("22:00-23:00", "sun:23:00-mon:00:00"),
    ],
)
def test_adjacent_windows_are_accepted(snapshot, maintenance) -> None:
    valkey(snapshot_window=snapshot, maintenance_window=maintenance)


def test_state_requires_the_network_administration_target_and_workload_store() -> None:
    document = json.loads(EXAMPLE.read_text())
    resource = document["resources"][NAME]
    for field, value in (
        ("aws_network", "missing"), ("administration_target", "devbox"),
        ("administration_target", "missing"), ("workload_secret_store", "local-sops"),
    ):
        broken = json.loads(json.dumps(document))
        broken["resources"][NAME] = resource | {field: value}
        with pytest.raises(ValidationError):
            ControlState.model_validate(broken)


def test_a_deployment_cannot_bind_a_managed_valkey_resource_yet() -> None:
    document = json.loads(EXAMPLE.read_text())
    document["deployments"]["example-local"]["resources"]["cache"] = NAME

    with pytest.raises(ValidationError, match="managed Valkey bindings are not implemented"):
        ControlState.model_validate(document)


def test_registration_makes_no_aws_call_and_lists_the_resource(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch, registered=False)

    server_module.register_resource(NAME, valkey())

    listed = cast(dict[str, dict[str, object]], server_module.list_resources()["resources"])
    assert listed[NAME]["provider"] == "aws_elasticache_valkey"
    assert listed[NAME]["node_type"] == "cache.m7g.large"
    assert NAME not in cast(
        dict[str, object], server_module.list_resources(target="devbox")["resources"]
    )
    with pytest.raises(ValueError, match="already exists"):
        server_module.register_resource(NAME, valkey())


def test_inspect_reports_only_registration_until_provisioning_exists(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch)

    assert server_module.inspect_resource(NAME) == {
        "resource": NAME, "provider": "aws_elasticache_valkey", "kind": "valkey",
        "engine_version": "9.0", "phase": "registered",
    }


def test_updates_inside_the_allowlist_are_applied(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch)
    larger = valkey(
        engine_version="9.1", node_type="cache.m7g.xlarge", snapshot_retention_days=14,
        snapshot_window="02:00-03:00", maintenance_window="mon:05:00-mon:06:00",
        administration_target="adminbox", retain_on_removal=False,
    )

    plan = server_module.plan_update_resource(NAME, larger)
    server_module.update_resource(NAME, larger, str(plan["plan_id"]))

    assert server_module.store.load().resources[NAME] == larger


def test_updates_that_need_a_new_resource_are_refused_and_change_nothing(
    tmp_path, monkeypatch
) -> None:
    use_state(tmp_path, monkeypatch)
    before = server_module.store.load()
    for code, updates in (
        ("engine_major", {"engine_version": "10.0"}),
        ("security_group_id", {"security_group_id": "sg-0123456789abcdef9"}),
    ):
        with pytest.raises(ResourceError, match=f"^aws_elasticache_update_forbidden_{code}$"):
            server_module.plan_update_resource(NAME, valkey(**updates))
        with pytest.raises(ResourceError, match=f"^aws_elasticache_update_forbidden_{code}$"):
            server_module.update_resource(NAME, valkey(**updates), "plan_" + "0" * 20)
    assert server_module.store.load() == before


def test_a_network_change_and_a_provider_change_are_refused(tmp_path, monkeypatch) -> None:
    state = use_state(tmp_path, monkeypatch)
    server_module.store.save(state.model_copy(update={"aws_networks": {
        **state.aws_networks, "secondary": state.aws_networks["primary"],
    }}))

    with pytest.raises(ResourceError, match="^aws_elasticache_update_forbidden_aws_network$"):
        server_module.plan_update_resource(NAME, valkey(aws_network="secondary"))
    with pytest.raises(ResourceError, match="^aws_elasticache_update_forbidden_provider$"):
        server_module.plan_update_resource(NAME, state.resources["devbox-valkey"])
    with pytest.raises(ResourceError, match="^aws_elasticache_update_forbidden_provider$"):
        server_module.plan_update_resource("devbox-valkey", valkey())


def test_removal_only_deletes_the_registration(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch)

    plan = server_module.plan_cleanup_resource(NAME)
    server_module.apply_cleanup_resource(NAME, str(plan["plan_id"]), str(plan["confirmation"]))

    assert NAME not in server_module.store.load().resources
