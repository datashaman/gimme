import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gimme.control import ControlState, StateStore

EXAMPLE = Path(__file__).resolve().parents[1] / "config/state.example.json"
NAME = "example-elasticache-valkey"


def document() -> dict:
    return json.loads(EXAMPLE.read_text())


def bound(uses: list[str], resource: str = NAME, target: str = "devbox") -> dict:
    """The example state with the Deployment bound to a Valkey Resource."""
    value = document()
    deployment = value["deployments"]["example-local"]
    deployment["resources"]["valkey"] = {"resource": resource, "uses": uses}
    if resource == NAME:
        value["resources"][NAME]["deployment_security_group_ids"] = (
            {"devbox": "sg-0123456789abcdef1"} if target == "devbox" else {}
        )
    return value


def v4() -> dict:
    """The example as schema-v4 state: the Deployment binds `resources.cache`."""
    value = document()
    value["schema_version"] = 4
    value["resources"].pop(NAME)
    resources = value["deployments"]["example-local"]["resources"]
    resources["cache"] = resources.pop("valkey")["resource"]
    return value


def migrate(tmp_path: Path, value: dict) -> ControlState:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps(value))
    return StateStore(tmp_path).state_migration({})


# --- the typed binding -----------------------------------------------------------------


def test_the_example_binds_a_target_local_valkey_by_use() -> None:
    state = ControlState.model_validate(document())

    binding = state.deployments["example-local"].resources.valkey
    assert binding is not None and binding.uses == ["cache"]


@pytest.mark.parametrize(
    "uses", [[], ["cache", "cache"], ["cache", "session", "queue", "cache"], ["horizon"], ["Cache"]]
)
def test_invalid_uses_are_refused(uses) -> None:
    with pytest.raises(ValidationError):
        ControlState.model_validate(bound(uses, "devbox-valkey"))


def test_all_three_uses_are_accepted_and_the_binding_takes_no_other_field() -> None:
    ControlState.model_validate(bound(["cache", "session", "queue"], "devbox-valkey"))
    value = bound(["cache"], "devbox-valkey")
    value["deployments"]["example-local"]["resources"]["valkey"]["namespace"] = "x:"
    with pytest.raises(ValidationError):
        ControlState.model_validate(value)


def test_the_old_cache_string_is_not_read() -> None:
    value = document()
    value["deployments"]["example-local"]["resources"]["cache"] = "devbox-valkey"

    with pytest.raises(ValidationError, match="cache"):
        ControlState.model_validate(value)


@pytest.mark.parametrize(
    ("workers", "uses", "message"),
    [
        ({"driver": "horizon"}, ["cache"], "runs Horizon and requires the queue use"),
        ({"driver": "horizon", "enabled": True}, ["cache", "session"], "requires the queue use"),
        ({"driver": "horizon"}, ["cache", "queue"], None),
        ({"driver": "horizon", "enabled": False}, ["cache"], None),
        ({"driver": "queue"}, ["cache"], None),
    ],
)
def test_horizon_requires_the_queue_use(workers, uses, message) -> None:
    value = bound(uses, "devbox-valkey")
    value["deployments"]["example-local"]["workers"] = workers
    if message is None:
        ControlState.model_validate(value)
    else:
        with pytest.raises(ValidationError, match=message):
            ControlState.model_validate(value)


def test_a_deployment_may_bind_a_managed_valkey_on_an_eligible_target() -> None:
    state = ControlState.model_validate(bound(["cache", "queue"]))

    binding = state.deployments["example-local"].resources.valkey
    assert binding is not None and binding.resource == NAME


def test_a_deployment_on_an_ineligible_target_cannot_bind_a_managed_valkey() -> None:
    with pytest.raises(ValidationError, match="not an eligible Deployment Target"):
        ControlState.model_validate(bound(["cache"], target="none"))


def test_a_valkey_binding_must_name_a_valkey_resource() -> None:
    with pytest.raises(ValidationError, match="incompatible valkey binding"):
        ControlState.model_validate(bound(["cache"], "devbox-postgres"))
    with pytest.raises(ValidationError, match="unknown resource"):
        ControlState.model_validate(bound(["cache"], "missing"))


def test_a_target_local_binding_must_be_on_the_deployments_own_target() -> None:
    value = bound(["cache"], "devbox-valkey")
    value["resources"]["devbox-valkey"]["target"] = "adminbox"

    with pytest.raises(ValidationError):
        ControlState.model_validate(value)


def test_a_managed_valkey_lists_only_deployment_targets() -> None:
    value = bound(["cache"])
    value["resources"][NAME]["deployment_security_group_ids"] = {"adminbox": "sg-0123456789abcdef1"}

    with pytest.raises(ValidationError, match="invalid target"):
        ControlState.model_validate(value)
    value["resources"][NAME]["deployment_security_group_ids"] = {"devbox": "sg-x"}
    with pytest.raises(ValidationError, match="exact security group ids"):
        ControlState.model_validate(value)


# --- the one-way migration -------------------------------------------------------------


def test_a_v4_cache_binding_becomes_a_cache_use(tmp_path) -> None:
    migrated = migrate(tmp_path, v4())

    binding = migrated.deployments["example-local"].resources.valkey
    assert migrated.schema_version == 5
    assert binding is not None and (binding.resource, binding.uses) == ("devbox-valkey", ["cache"])


@pytest.mark.parametrize(
    ("workers", "uses"),
    [
        ({"driver": "horizon"}, ["cache", "queue"]),
        ({"driver": "horizon", "enabled": True}, ["cache", "queue"]),
        ({"driver": "horizon", "enabled": False}, ["cache"]),
        ({"driver": "queue"}, ["cache"]),
        (None, ["cache"]),
    ],
)
def test_migration_adds_queue_only_for_a_running_horizon_and_never_session(
    tmp_path, workers, uses
) -> None:
    value = v4()
    value["deployments"]["example-local"]["workers"] = workers

    migrated = migrate(tmp_path, value)

    binding = migrated.deployments["example-local"].resources.valkey
    assert binding is not None and binding.uses == uses and "session" not in binding.uses


def test_a_v4_deployment_without_a_cache_binding_gets_none() -> None:
    value = {"deployments": {"site": {"resources": {"database": None, "cache": None}}}}

    StateStore._migrate_valkey_bindings(value)

    assert value == {
        "schema_version": 5,
        "deployments": {"site": {"resources": {"database": None, "valkey": None}}},
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["resources"].update(cache=["devbox-valkey"]),
        lambda d: d["resources"].update(cache={"resource": "devbox-valkey"}),
        lambda d: d["resources"].update(valkey={"resource": "devbox-valkey", "uses": ["cache"]}),
        lambda d: d["resources"].update(extra="x"),
        lambda d: d.update(resources="devbox-valkey"),
        lambda d: d.update(workers={"driver": "unknown"}),
        lambda d: d.update(workers="horizon"),
        lambda d: d["resources"].update(cache="missing-valkey"),
        lambda d: d["resources"].update(cache="devbox-postgres"),
    ],
)
def test_ambiguous_v4_state_fails_migration_and_writes_nothing(tmp_path, mutate) -> None:
    value = v4()
    mutate(value["deployments"]["example-local"])
    tmp_path.mkdir(exist_ok=True)
    original = json.dumps(value)
    (tmp_path / "state.json").write_text(original)

    with pytest.raises((ValueError, ValidationError)):
        StateStore(tmp_path).state_migration({})

    assert (tmp_path / "state.json").read_text() == original


def test_v4_state_is_not_loadable_until_migrated(tmp_path) -> None:
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps(v4()))

    with pytest.raises(RuntimeError, match="state migration required"):
        StateStore(tmp_path).load()


def test_v5_state_cannot_be_migrated_again(tmp_path) -> None:
    with pytest.raises(ValueError, match="schema-v5 state already exists"):
        migrate(tmp_path, document())


def test_migration_does_not_mutate_its_input_document(tmp_path) -> None:
    value = v4()
    snapshot = copy.deepcopy(value)
    migrate(tmp_path, value)

    assert value == snapshot
