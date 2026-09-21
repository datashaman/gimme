import dataclasses
import datetime
import json
import re
import stat
from pathlib import Path
from typing import cast

import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY
from pydantic import ValidationError

from gimme.control import (
    AWSElastiCacheValkeyResource, AWSProviderAccount, AWSSecretsManagerStore, ControlState,
    DeploymentRegistration, MANAGED_VALKEY_ENV_KEYS, ResourceBindings, StateStore, ValkeyBinding,
)
from gimme.resources_postgres import ResourceError
from gimme.resources_valkey import (
    ADMIN_ACCESS_STRING, BotoElastiCacheAdapter, GroupObservation, ISSUES, LARAVEL_COMMANDS,
    MODIFIABLE_FIELDS, SnapshotInfo, ValkeyOptions, apply_binding, apply_destroy, apply_provision,
    binding_username, derive_binding_user_id, derive_group_id, derive_user_group_id, group_drift,
    laravel_access_string, load_observed, metric_warnings, modification_for,
    namespace_prefixes, structural_issues,
)
from gimme.config import HorizonWorkerConfig
from gimme.deployer import CommandResult
from gimme.resource_orchestration import ManagedResourceOrchestrator
from gimme.secrets import SecretMetadata
from gimme.valkey_contract import (
    contract_variables, credential_references, probe_names,
)
import gimme.resources_valkey as resources_valkey_module
import gimme.valkey_recovery as valkey_recovery_module
import gimme.server as server_module

EXAMPLE = Path(__file__).resolve().parents[1] / "config/state.example.json"
NAME = "example-elasticache-valkey"


def valkey(**updates) -> AWSElastiCacheValkeyResource:
    values = json.loads(EXAMPLE.read_text())["resources"][NAME] | updates
    return AWSElastiCacheValkeyResource.model_validate(values)


GROUP_ID = derive_group_id(NAME)
ARN = f"arn:aws:elasticache:eu-central-1:123456789012:replicationgroup:{GROUP_ID}"
PASSWORD = "p" * 48
GROUP_SG = "sg-0123456789abcdef2"
ADMIN_SG = "sg-0123456789abcdef0"
DEVBOX_SG = "sg-0123456789abcdef1"
ANY_UPDATE_ACTIONS = {
    "ReplicationGroupIds": [GROUP_ID], "ServiceUpdateStatus": ["available"], "MaxRecords": 50,
}


def observation(**updates) -> GroupObservation:
    values: dict[str, object] = dict(
        identity=ARN, status="available", engine_version="9.0", node_type="cache.m7g.large",
        cluster_enabled=True, shards=1, members=2,
        member_zones=("eu-central-1a", "eu-central-1b"), multi_az=True, automatic_failover=True,
        transit_encryption=True, at_rest_encryption=True, effective_durability="sync",
        user_group_ids=(derive_user_group_id(GROUP_ID),), snapshot_retention_days=7,
        snapshot_window="03:00-04:00", maintenance_window="sun:05:00-sun:06:00",
        automatic_minor_upgrade=False, endpoint="cfg.example.cache.amazonaws.com", port=6379,
        security_group_ids=(GROUP_SG,), ingress_sources=(ADMIN_SG, DEVBOX_SG),
        member_ids=(f"{GROUP_ID}-0001-001", f"{GROUP_ID}-0001-002"),
    )
    values.update(updates)
    return GroupObservation(**values)  # type: ignore[arg-type]


FIELDS = {
    "EngineVersion": "engine_version", "CacheNodeType": "node_type",
    "SnapshotRetentionLimit": "snapshot_retention_days", "SnapshotWindow": "snapshot_window",
    "PreferredMaintenanceWindow": "maintenance_window",
}
METRIC_KEYS = tuple(key for key, *_ in resources_valkey_module.METRICS)
OPTIONS = ValkeyOptions(("9.0", "9.1"), ("cache.m7g.large", "cache.m7g.xlarge"))


class FakeValkey:
    """An existing or absent group. A created or modified group reports 'creating' or
    'modifying' for `settle_polls` describes and then 'available'."""

    def __init__(
        self, live: GroupObservation | None = None, *, settle_polls: int = 0,
        allowed: frozenset[str] = frozenset({"cache.m7g.large", "cache.m7g.xlarge"}),
    ) -> None:
        self.live = live
        self.settle_polls = settle_polls
        self.allowed = allowed
        self.create_calls = 0
        self.describe_calls = 0
        self.modify_calls: list[dict[str, object]] = []
        self.options = OPTIONS
        self.binding_calls: list[tuple[str, bool]] = []
        self.users: set[str] = set()
        self.delete_calls: list[str] = []
        self.dependents_calls: list[tuple[str, list[str]]] = []
        self.dependents_error: ResourceError | None = None
        self.dependents_pending = False
        self.snapshots: list[SnapshotInfo] = []
        self.deleted_snapshots: list[str] = []
        self.deleted_secrets: list[str] = []
        self.create_args: list[dict[str, object]] = []
        # deployment -> credential versions, oldest first; the last one is current
        self.credentials: dict[str, list[str]] = {}
        self.removed_users: list[str] = []
        self.events: list[str] = []
        self.remove_error: ResourceError | None = None
        self.begin_error: ResourceError | None = None
        self.metrics: dict[str, float | None] = dict.fromkeys(METRIC_KEYS)
        self.metrics_error: ResourceError | None = None
        self.metrics_calls: list[tuple[str, ...]] = []
        self._polls = 0

    def describe_group(self, account, network, group_id):
        self.describe_calls += 1
        if self.live is not None and self.live.status in ("creating", "modifying"):
            self._polls += 1
            if self._polls > self.settle_polls:
                self.live = dataclasses.replace(self.live, status="available")
        elif self.live is not None and self.live.status == "deleting":
            self._polls += 1
            if self._polls > self.settle_polls:
                self.live = None
        return self.live

    def allowed_node_types(self, account, network, group_id):
        return self.allowed

    def modify_group(self, account, network, group_id, changes):
        assert self.live is not None and set(changes) <= MODIFIABLE_FIELDS
        self.modify_calls.append(dict(changes))
        self._polls = 0
        self.live = dataclasses.replace(
            self.live, status="modifying" if self.settle_polls else "available",
            **{FIELDS[key]: value for key, value in changes.items()},
        )
        return self.live

    def live_options(self, account, network):
        return self.options

    def recent_metrics(self, account, network, member_ids):
        self.metrics_calls.append(member_ids)
        if self.metrics_error is not None:
            raise self.metrics_error
        return dict(self.metrics)

    def ensure_binding(
        self, account, network, store, store_name, resource_name, group_id, deployment_name,
        keep_credential, generation=1,
    ):
        self.binding_calls.append((deployment_name, keep_credential))
        user_id = derive_binding_user_id(group_id, deployment_name, generation)
        if user_id in self.users and keep_credential:
            return None
        self.users.add(user_id)
        self.credentials.setdefault(deployment_name, []).append(
            binding_username(deployment_name, generation)
        )
        arn = f"arn:aws:secretsmanager:eu-central-1:123456789012:secret:{user_id}"
        return user_id, arn, "v" * 32

    def list_snapshots(self, account, network, group_id):
        return list(self.snapshots)

    def delete_final_snapshot(self, account, network, snapshot_name):
        self.deleted_snapshots.append(snapshot_name)
        for index, snapshot in enumerate(self.snapshots):
            if snapshot.name == snapshot_name:
                self.snapshots.pop(index)
                return True
        return False

    def delete_retained_secrets(self, account, store, store_name, resource_name, secret_names):
        self.deleted_secrets.extend(secret_names)
        return len(secret_names)

    def _version(self, deployment_name):
        return f"{len(self.credentials[deployment_name]):032d}"

    def begin_rotation(
        self, account, network, store, store_name, resource_name, group_id, deployment_name,
        generation,
    ):
        if self.begin_error is not None:
            raise self.begin_error
        user_id = derive_binding_user_id(group_id, deployment_name, generation)
        self.users.add(user_id)
        self.credentials[deployment_name].append(binding_username(deployment_name, generation))
        arn = f"arn:aws:secretsmanager:eu-central-1:123456789012:secret:{deployment_name}"
        return user_id, arn, self._version(deployment_name)

    def restore_credential(self, account, store, resource_name, deployment_name, expected_username):
        versions = self.credentials[deployment_name]
        if versions[-1] != expected_username:
            if len(versions) < 2 or versions[-2] != expected_username:
                raise ResourceError("aws_elasticache_rotate_previous_credential_missing")
            versions.append(expected_username)
        arn = f"arn:aws:secretsmanager:eu-central-1:123456789012:secret:{deployment_name}"
        return arn, self._version(deployment_name)

    def disable_binding(self, account, network, group_id, user_id):
        self.events.append(f"disable:{user_id}")

    def remove_user(self, account, network, resource_name, user_id):
        assert account.destructive_role_arn is not None
        self.removed_users.append(user_id)
        if self.remove_error is not None:
            raise self.remove_error
        self.users.discard(user_id)

    def delete_group(self, account, network, group_id, final_snapshot):
        assert self.live is not None and account.destructive_role_arn is not None
        self.delete_calls.append(final_snapshot)
        self._polls = 0
        self.live = dataclasses.replace(self.live, status="deleting")

    def delete_dependents(self, account, network, resource_name, group_id, user_ids):
        assert self.live is None, "dependents go only after the group is gone"
        self.dependents_calls.append((resource_name, list(user_ids)))
        if self.dependents_error is not None:
            raise self.dependents_error
        return not self.dependents_pending

    def create_group(
        self, account, network, resource, name, group_id, store, store_name,
        snapshot_name=None, restore_users=None,
    ):
        self.create_args.append({"snapshot_name": snapshot_name, "restore_users": restore_users})
        self.create_calls += 1
        self._polls = 0
        self.live = observation(status="creating" if self.settle_polls else "available")
        return self.live


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


def provision(adapter, tmp_path: Path, **updates):
    state = ControlState.model_validate(json.loads(EXAMPLE.read_text()))
    network = state.aws_networks["primary"]
    clock = FakeClock()
    return apply_provision(
        adapter, tmp_path, state.provider_accounts[network.provider_account], network,
        valkey(**updates), NAME,
        cast(AWSSecretsManagerStore, state.secret_stores["workload-secrets"]), "workload-secrets",
        sleep=clock.sleep, now=clock.monotonic,
    )


def use_state(
    tmp_path: Path, monkeypatch, *, registered: bool = True, adapter: FakeValkey | None = None,
) -> ControlState:
    monkeypatch.setattr(server_module, "elasticache_valkey", adapter or FakeValkey())
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
        {"engine_version": "09.0"}, {"engine_version": "9.00"}, {"engine_version": "9.0.00"},
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
    for field, value, message in (
        ("aws_network", "missing", "unknown AWS Network"),
        ("administration_target", "devbox", "administration Target"),
        ("administration_target", "missing", "administration Target"),
        ("workload_secret_store", "local-sops", "AWS Secrets Manager store"),
    ):
        broken = json.loads(json.dumps(document))
        broken["resources"][NAME] = resource | {field: value}
        with pytest.raises(ValidationError, match=message):
            ControlState.model_validate(broken)


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


# --- identifiers -----------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["a", "devbox-cache", "x" * 34, "x" * 35, "a" * 64, "a--b", "trailing-", "a" + "-" * 30 + "b",
     "shared-valkey-with-a-rather-long-name-indeed"],
)
def test_derived_identifiers_fit_elasticache_rules(name) -> None:
    group = derive_group_id(name)
    for identifier in (group, derive_user_group_id(group)):
        assert len(identifier) <= 40
        assert re.fullmatch(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*", identifier), identifier
    assert group.startswith("gimme-")


def test_derived_identifiers_stay_distinct_for_long_or_irregular_names() -> None:
    names = ["a" * 40, "a" * 41, "a" * 42, "a--b", "a-b", "a---b", "ab-", "ab"]
    groups = {derive_group_id(name) for name in names}
    users = {derive_user_group_id(group) for group in groups}

    assert len(groups) == len(names) and len(users) == len(names)
    assert derive_group_id("devbox-cache") == "gimme-devbox-cache"


# --- plan, apply, resume ---------------------------------------------------------------


def test_the_provision_plan_is_local_and_secret_free(tmp_path, monkeypatch) -> None:
    adapter = FakeValkey()
    use_state(tmp_path, monkeypatch, adapter=adapter)

    plan = server_module.plan_apply_resource(NAME)

    assert adapter.describe_calls == 0 and adapter.create_calls == 0
    assert plan["current_phase"] == "absent" and plan["kind"] == "resource_provision"
    effects = " ".join(cast(list[str], plan["effects"]))
    assert "synchronous durability" in effects
    assert "converge an existing replication group" in effects


def test_apply_creates_once_and_a_second_apply_only_describes(tmp_path, monkeypatch) -> None:
    adapter = FakeValkey()
    use_state(tmp_path, monkeypatch, adapter=adapter)
    first_plan = server_module.plan_apply_resource(NAME)

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.apply_resource(NAME, "plan_" + "0" * 20)
    assert adapter.create_calls == 0
    first = server_module.apply_resource(NAME, str(first_plan["plan_id"]))
    second_plan = server_module.plan_apply_resource(NAME)
    second = server_module.apply_resource(NAME, str(second_plan["plan_id"]))

    assert adapter.create_calls == 1
    assert first["phase"] == second["phase"] == "ready" and first["issues"] == []
    assert second_plan["plan_id"] != first_plan["plan_id"], "the plan follows the phase"
    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.apply_resource(NAME, str(first_plan["plan_id"]))


def test_a_slow_group_is_pending_and_a_later_apply_resumes_without_recreating(
    tmp_path,
) -> None:
    adapter = FakeValkey(settle_polls=1000)

    first = provision(adapter, tmp_path)
    resumed = provision(adapter, tmp_path)

    assert first["phase"] == resumed["phase"] == "pending"
    assert adapter.create_calls == 1
    observed = load_observed(tmp_path, NAME)
    assert observed is not None and observed["phase"] == "pending"


def test_a_create_acknowledgement_survives_a_transient_first_observation_failure(
    tmp_path, monkeypatch
) -> None:
    adapter = FakeValkey(settle_polls=1000)
    use_state(tmp_path, monkeypatch, adapter=adapter)
    original_describe = adapter.describe_group

    def fail_after_create(*args, **kwargs):
        if adapter.create_calls:
            raise ResourceError("aws_elasticache_describe_unavailable")
        return original_describe(*args, **kwargs)

    adapter.describe_group = fail_after_create  # type: ignore[method-assign]
    plan = server_module.plan_apply_resource(NAME)
    with pytest.raises(ResourceError, match="^aws_elasticache_describe_unavailable$"):
        server_module.apply_resource(NAME, str(plan["plan_id"]))

    marker = server_module.store.root / "provisioning-resources" / f"{NAME}.json"
    assert adapter.create_calls == 1 and json.loads(marker.read_text())["phase"] == "creating"
    assert load_observed(server_module.store.root, NAME) is None
    inspected = server_module.inspect_resource(NAME)
    assert inspected["operation"] == "provisioning"
    assert inspected["phase"] == "pending"
    assert inspected["progress"] == {"phase": "creating"}

    adapter.describe_group = original_describe  # type: ignore[method-assign]
    adapter.settle_polls = 0
    resumed = server_module.apply_resource(
        NAME, str(server_module.plan_apply_resource(NAME)["plan_id"])
    )

    assert resumed["phase"] == "ready" and adapter.create_calls == 1
    assert not marker.exists()
    observed = load_observed(server_module.store.root, NAME)
    assert observed is not None and observed["phase"] == "ready"


def test_a_group_that_settles_within_the_poll_budget_is_ready(tmp_path) -> None:
    adapter = FakeValkey(settle_polls=3)

    assert provision(adapter, tmp_path)["phase"] == "ready"


def test_a_failed_creation_is_recorded_as_failed(tmp_path) -> None:
    result = provision(FakeValkey(observation(status="create-failed")), tmp_path)

    assert result["phase"] == "failed"


def test_a_group_that_matches_is_described_and_never_created_or_modified(tmp_path) -> None:
    adapter = FakeValkey(observation(engine_version="9.0.3"))  # a patch of the desired 9.0

    result = provision(adapter, tmp_path)

    assert adapter.create_calls == 0 and adapter.modify_calls == []
    assert result["phase"] == "ready" and result["modified_fields"] == []
    assert result["engine_version"] == "9.0.3"


# --- readiness contract ----------------------------------------------------------------

BROKEN = {
    "cluster_mode": {"cluster_enabled": False},
    "topology": {"shards": 2},
    "availability_zones": {"member_zones": ("eu-central-1a", "eu-central-1a")},
    "multi_az": {"multi_az": False},
    "automatic_failover": {"automatic_failover": False},
    "tls": {"transit_encryption": False},
    "encryption_at_rest": {"at_rest_encryption": False},
    "durability": {"effective_durability": "async"},
    "authentication": {"user_group_ids": ()},
    "snapshot_policy": {"snapshot_retention_days": 1},
    "maintenance_policy": {"maintenance_window": "mon:01:00-mon:02:00"},
    "automatic_minor_upgrade": {"automatic_minor_upgrade": True},
    "service_update_overdue": {"service_update_overdue": True},
    "security_group": {"ingress_sources": (ADMIN_SG, DEVBOX_SG, "0.0.0.0/0")},
}


def test_every_readiness_code_has_a_case() -> None:
    assert set(BROKEN) == set(ISSUES)


REPAIRED = {"snapshot_policy", "maintenance_policy"}  # the policy fields apply converges


@pytest.mark.parametrize("code", sorted(BROKEN))
def test_every_readiness_code_is_reported_by_structural_issues(code) -> None:
    assert structural_issues(valkey(), observation(**BROKEN[code]), GROUP_ID) == [code]


@pytest.mark.parametrize("code", sorted(set(BROKEN) - REPAIRED))
def test_an_available_group_missing_one_part_of_the_contract_is_degraded(
    tmp_path, code
) -> None:
    adapter = FakeValkey(observation(**BROKEN[code]))

    result = provision(adapter, tmp_path)

    assert result["phase"] == "degraded" and result["issues"] == [code]
    assert adapter.modify_calls == []


def test_a_mismatched_snapshot_window_is_a_snapshot_policy_issue() -> None:
    observed = observation(snapshot_window="05:00-06:00")

    assert structural_issues(valkey(), observed, GROUP_ID) == ["snapshot_policy"]


def test_a_group_that_is_not_available_is_pending_not_degraded(tmp_path) -> None:
    result = provision(
        FakeValkey(
            observation(status="modifying", effective_durability="async"), settle_polls=1000
        ),
        tmp_path,
    )

    assert result["phase"] == "pending"


# --- inspection ------------------------------------------------------------------------


def test_inspect_before_provisioning_is_absent(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch)

    assert server_module.inspect_resource(NAME) == {
        "resource": NAME, "provider": "aws_elasticache_valkey", "kind": "valkey",
        "phase": "absent", "source": "cache",
    }


def test_inspect_reports_live_state_without_endpoints_or_identifiers(
    tmp_path, monkeypatch
) -> None:
    use_state(tmp_path, monkeypatch, adapter=FakeValkey(observation(effective_durability="async")))

    result = server_module.inspect_resource(NAME)

    assert result["phase"] == "degraded" and result["issues"] == ["durability"]
    assert result["source"] == "live" and result["effective_durability"] == "async"
    text = json.dumps(result)
    for secret in ("cfg.example.cache.amazonaws.com", ARN, GROUP_ID, "6379"):
        assert secret not in text


def test_inspect_reports_topology_policy_pending_updates_and_metric_warnings(
    tmp_path, monkeypatch
) -> None:
    adapter = FakeValkey(observation(pending_service_updates=2))
    adapter.metrics = {**dict.fromkeys(METRIC_KEYS), "memory_usage_percent": 91.5,
                       "connections": 12, "evictions": 0, "durability_lag_ms": 3}
    use_state(tmp_path, monkeypatch, adapter=adapter)

    result = server_module.inspect_resource(NAME)

    assert result["phase"] == "ready" and result["issues"] == []
    assert result["topology"] == {
        "shards": 1, "members": 2, "multi_az": True, "automatic_failover": True,
        "transit_encryption": True, "at_rest_encryption": True,
    }
    assert result["snapshot_policy"] == {"retention_days": 7, "window": "03:00-04:00"}
    assert result["maintenance_window"] == "sun:05:00-sun:06:00"
    assert result["pending_service_updates"] == 2
    assert result["metrics"] == adapter.metrics
    assert result["warnings"] == ["metric_memory_high", "metric_durability_lag"]
    assert adapter.metrics_calls == [observation().member_ids]
    text = json.dumps(result)
    for hidden in (observation().member_ids[0], GROUP_ID, ARN, "cfg.example", "6379"):
        assert hidden not in text


def test_a_metrics_failure_neither_fails_nor_degrades_inspection(tmp_path, monkeypatch) -> None:
    adapter = FakeValkey(observation())
    adapter.metrics_error = ResourceError("aws_elasticache_metrics_access_denied")
    use_state(tmp_path, monkeypatch, adapter=adapter)

    result = server_module.inspect_resource(NAME)

    assert result["phase"] == "ready" and result["issues"] == []
    assert result["metrics_error"] == "aws_elasticache_metrics_access_denied"
    assert "metrics" not in result and "warnings" not in result


def test_inspection_reads_no_metrics_without_a_live_available_group(tmp_path, monkeypatch) -> None:
    adapter = FakeValkey(observation(status="creating", member_ids=()))
    use_state(tmp_path, monkeypatch, adapter=adapter)
    server_module.inspect_resource(NAME)

    cached = FakeValkey(observation())
    use_state(tmp_path, monkeypatch, adapter=cached)
    plan = server_module.plan_apply_resource(NAME)
    server_module.apply_resource(NAME, str(plan["plan_id"]))
    cached.metrics_calls.clear()

    def denied(*args, **kwargs):
        raise ResourceError("aws_elasticache_describe_access_denied")

    cached.describe_group = denied  # type: ignore[method-assign]
    result = server_module.inspect_resource(NAME)

    assert result["source"] == "cache"
    assert adapter.metrics_calls == [] and cached.metrics_calls == []
    assert not {"metrics", "warnings", "topology"} & set(result)


@pytest.mark.parametrize(
    ("metrics", "expected"),
    [
        ({}, []),
        ({"memory_usage_percent": 80}, []),
        ({"memory_usage_percent": 80.1}, ["metric_memory_high"]),
        ({"evictions": 1}, ["metric_evictions"]),
        ({"replica_lag_seconds": 5}, []),
        ({"replica_lag_seconds": 5.5}, ["metric_replica_lag"]),
        ({"durability_lag_ms": 1, "durability_rejections": 1},
         ["metric_durability_lag", "metric_durability_rejections"]),
        ({"traffic_management_active": 1}, ["metric_traffic_management"]),
        ({"connections": 100000, "evictions": None}, []),
    ],
)
def test_metric_warnings_use_fixed_limits(metrics, expected) -> None:
    assert metric_warnings(metrics) == expected


def test_inspect_falls_back_to_the_cache_with_a_bounded_refresh_error(
    tmp_path, monkeypatch
) -> None:
    adapter = FakeValkey(observation())
    use_state(tmp_path, monkeypatch, adapter=adapter)
    plan = server_module.plan_apply_resource(NAME)
    server_module.apply_resource(NAME, str(plan["plan_id"]))

    def denied(*args, **kwargs):
        raise ResourceError("aws_elasticache_describe_access_denied")

    adapter.describe_group = denied  # type: ignore[method-assign]
    result = server_module.inspect_resource(NAME)

    assert result["source"] == "cache" and result["phase"] == "ready"
    assert result["refresh_error"] == "aws_elasticache_describe_access_denied"


def test_the_observation_cache_is_private_and_secret_free(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch, adapter=FakeValkey(observation()))
    plan = server_module.plan_apply_resource(NAME)
    server_module.apply_resource(NAME, str(plan["plan_id"]))

    path = server_module.store.root / "observed-resources" / f"{NAME}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert set(json.loads(path.read_text())) == {
        "schema_version", "resource", "replication_group_id", "identity", "status", "phase",
        "engine_version", "effective_durability", "issues", "endpoint", "port", "allocations",
    }
    path.write_text(json.dumps({"schema_version": 1}))
    with pytest.raises(ResourceError, match="^observed_resource_invalid$"):
        load_observed(server_module.store.root, NAME)


# --- removal ---------------------------------------------------------------------------


def test_removing_a_provisioned_resource_leaves_a_tombstone_and_touches_nothing(
    tmp_path, monkeypatch
) -> None:
    adapter = FakeValkey(observation())
    use_state(tmp_path, monkeypatch, adapter=adapter)
    plan = server_module.plan_apply_resource(NAME)
    server_module.apply_resource(NAME, str(plan["plan_id"]))
    calls = (adapter.create_calls, adapter.describe_calls)

    cleanup = server_module.plan_cleanup_resource(NAME)
    result = server_module.apply_cleanup_resource(
        NAME, str(cleanup["plan_id"]), str(cleanup["confirmation"])
    )

    assert "ElastiCache replication group and its data intact" in " ".join(
        cast(list[str], cleanup["effects"])
    )
    assert result["retained"] is True and NAME not in server_module.store.load().resources
    assert (adapter.create_calls, adapter.describe_calls) == calls
    tombstone = json.loads(
        (server_module.store.root / "retained-resources" / f"{NAME}.json").read_text()
    )
    assert tombstone["replication_group_id"] == GROUP_ID and tombstone["aws_network"] == "primary"


# --- the boto adapter ------------------------------------------------------------------


class StubbedSession:
    def __init__(self, clients: dict[str, object]) -> None:
        self._clients = clients

    def client(self, service: str, region_name: str | None = None) -> object:
        return self._clients[service]


def stubbed(service: str):
    import boto3
    from botocore.stub import Stubber

    client = boto3.client(
        service, region_name="eu-central-1", aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    return client, Stubber(client)


def fault(code: str, operation: str, message: str = "boom") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": message}}, operation)


def adapter_with(monkeypatch, *pairs):
    adapter = BotoElastiCacheAdapter()
    clients = {service: client for service, (client, _stub) in pairs}
    monkeypatch.setattr(adapter, "_session", lambda *a, **k: StubbedSession(clients))
    monkeypatch.setattr(
        "gimme.resources_valkey.secrets_module.token_urlsafe", lambda size: PASSWORD
    )
    return adapter


def context():
    state = ControlState.model_validate(json.loads(EXAMPLE.read_text()))
    network = state.aws_networks["primary"]
    return (
        state.provider_accounts[network.provider_account], network,
        cast(AWSSecretsManagerStore, state.secret_stores["workload-secrets"]),
    )


def test_capture_converges_the_existing_admin_user_to_the_fixed_acl(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_response(
        "modify_user", {},
        {
            "UserId": resources_valkey_module._derived(GROUP_ID, "admin"),
            "AccessString": ADMIN_ACCESS_STRING,
        },
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _store = context()

    with stub:
        adapter.ensure_admin_capture_access(account, network, NAME)

    stub.assert_no_pending_responses()


def group_response(**updates) -> dict[str, object]:
    values: dict[str, object] = {
        "ReplicationGroupId": GROUP_ID, "Status": "available", "ARN": ARN,
        "ClusterEnabled": True, "MultiAZ": "enabled", "AutomaticFailover": "enabled",
        "CacheNodeType": "cache.m7g.large", "TransitEncryptionEnabled": True,
        "AtRestEncryptionEnabled": True, "Durability": "sync", "EffectiveDurability": "sync",
        "SnapshotRetentionLimit": 7, "SnapshotWindow": "03:00-04:00",
        "UserGroupIds": [derive_user_group_id(GROUP_ID)],
        "MemberClusters": [f"{GROUP_ID}-0001-001", f"{GROUP_ID}-0001-002"],
        "ConfigurationEndpoint": {"Address": "cfg.example.cache.amazonaws.com", "Port": 6379},
        "NodeGroups": [{"NodeGroupId": "0001", "Status": "available", "NodeGroupMembers": [
            {"CacheClusterId": f"{GROUP_ID}-0001-001",
             "PreferredAvailabilityZone": "eu-central-1a"},
            {"CacheClusterId": f"{GROUP_ID}-0001-002",
             "PreferredAvailabilityZone": "eu-central-1b"},
        ]}],
    }
    values.update(updates)
    return values


def rule(source, *, egress=False, protocol="tcp", low=6379, high=6379) -> dict[str, object]:
    key = "ReferencedGroupInfo" if source.startswith("sg-") else "CidrIpv4"
    return {
        "SecurityGroupRuleId": "sgr-0123456789abcdef0", "GroupId": GROUP_SG, "IsEgress": egress,
        "IpProtocol": protocol, "FromPort": low, "ToPort": high,
        key: {"GroupId": source} if key == "ReferencedGroupInfo" else source,
    }


COMPLIANT_RULES = [
    rule(ADMIN_SG), rule(DEVBOX_SG, low=6000, high=7000), rule(ADMIN_SG, protocol="-1", low=-1,
                                                             high=-1),
    rule("0.0.0.0/0", low=22, high=22), rule("0.0.0.0/0", egress=True, protocol="-1", low=-1,
                                             high=-1),
]


def expect_describe(
    stub, ec2_stub, *, actions=None, pending=None, rules=None, groups=None, **updates
) -> None:
    stub.add_response(
        "describe_replication_groups", {"ReplicationGroups": [group_response(**updates)]},
        {"ReplicationGroupId": GROUP_ID},
    )
    if updates.get("Status", "available") == "available":
        stub.add_response(
            "list_tags_for_resource", {"TagList": [{"Key": "gimme:resource", "Value": NAME}]},
            {"ResourceName": ARN},
        )
        stub.add_response(
            "describe_update_actions", {"UpdateActions": actions or []}, ANY_UPDATE_ACTIONS
        )
    attached = [{"SecurityGroupId": sg, "Status": "active"} for sg in groups or [GROUP_SG]]
    stub.add_response(
        "describe_cache_clusters",
        {"CacheClusters": [{
            "EngineVersion": "9.0", "PreferredMaintenanceWindow": "sun:05:00-sun:06:00",
            "AutoMinorVersionUpgrade": False, "PendingModifiedValues": pending or {},
            "SecurityGroups": attached,
        }]},
        {"CacheClusterId": f"{GROUP_ID}-0001-001"},
    )
    if updates.get("Status", "available") == "available":
        ec2_stub.add_response(
            "describe_security_group_rules",
            {"SecurityGroupRules": COMPLIANT_RULES if rules is None else rules},
            {"Filters": [{"Name": "group-id", "Values": sorted(groups or [GROUP_SG])}]},
        )


TAG = [{"Key": "gimme:resource", "Value": NAME}]


def test_describe_parses_a_live_group(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    ec2, ec2_stub = stubbed("ec2")
    expect_describe(stub, ec2_stub)
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)), ("ec2", (ec2, ec2_stub)))
    account, network, _ = context()

    with stub, ec2_stub:
        observed = adapter.describe_group(account, network, GROUP_ID)

    assert observed == observation()


def test_describe_of_an_absent_group_is_none(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_client_error("describe_replication_groups", "ReplicationGroupNotFoundFault")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        assert adapter.describe_group(account, network, GROUP_ID) is None


def test_describe_refuses_a_group_this_resource_does_not_own(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_response(
        "describe_replication_groups", {"ReplicationGroups": [group_response()]},
        {"ReplicationGroupId": GROUP_ID},
    )
    stub.add_response(
        "list_tags_for_resource", {"TagList": [{"Key": "gimme:resource", "Value": "other"}]},
        {"ResourceName": ARN},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub, pytest.raises(ResourceError, match="^aws_elasticache_group_ownership_mismatch$"):
        adapter.describe_group(account, network, GROUP_ID)


def test_describe_tolerates_a_member_cluster_that_does_not_exist_yet(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_response(
        "describe_replication_groups",
        {"ReplicationGroups": [group_response(Status="creating")]},
        {"ReplicationGroupId": GROUP_ID},
    )
    stub.add_client_error("describe_cache_clusters", "CacheClusterNotFound")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        observed = adapter.describe_group(account, network, GROUP_ID)

    assert observed is not None and observed.engine_version is None
    assert observed.maintenance_window is None


def test_describe_tolerates_missing_node_groups_while_creating(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    response = group_response(Status="creating")
    response.pop("NodeGroups")
    stub.add_response(
        "describe_replication_groups", {"ReplicationGroups": [response]},
        {"ReplicationGroupId": GROUP_ID},
    )
    stub.add_response(
        "describe_cache_clusters", {"CacheClusters": [{
            "EngineVersion": "9.0", "PreferredMaintenanceWindow": "sun:05:00-sun:06:00",
            "AutoMinorVersionUpgrade": False, "PendingModifiedValues": {},
            "SecurityGroups": [{"SecurityGroupId": GROUP_SG, "Status": "active"}],
        }]}, {"CacheClusterId": f"{GROUP_ID}-0001-001"},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        observed = adapter.describe_group(account, network, GROUP_ID)

    assert observed.status == "creating" and observed.shards == 0 and observed.members == 2


def test_describe_tolerates_missing_members_and_node_groups_while_deleting(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    response = group_response(Status="deleting")
    response.pop("MemberClusters")
    response.pop("NodeGroups")
    stub.add_response(
        "describe_replication_groups", {"ReplicationGroups": [response]},
        {"ReplicationGroupId": GROUP_ID},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        observed = adapter.describe_group(account, network, GROUP_ID)

    assert observed.status == "deleting" and observed.shards == observed.members == 0


def test_describe_failures_are_bounded_and_never_carry_the_provider_message(
    monkeypatch,
) -> None:
    client, stub = stubbed("elasticache")
    stub.add_client_error(
        "describe_replication_groups", "AccessDenied",
        service_message="arn:aws:iam::123456789012:role/secret-role is not authorized",
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub, pytest.raises(ResourceError) as raised:
        adapter.describe_group(account, network, GROUP_ID)

    assert str(raised.value) == "aws_elasticache_describe_access_denied"


def expect_create(
    elasticache_stub, secrets_stub, *, admin_exists: bool = False, admin_fault: bool = False
) -> None:
    subnet = f"{GROUP_ID}-subnets"
    params = f"{GROUP_ID}-params"
    users = derive_user_group_id(GROUP_ID)
    default_id, admin_id = f"{GROUP_ID}-default", f"{GROUP_ID}-admin"
    stub = elasticache_stub
    stub.add_response(
        "create_cache_subnet_group", {},
        {"CacheSubnetGroupName": subnet,
         "CacheSubnetGroupDescription": f"Gimme-managed subnet group for {NAME}",
         "SubnetIds": ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"], "Tags": TAG},
    )
    stub.add_response(
        "create_cache_parameter_group", {},
        {"CacheParameterGroupName": params, "CacheParameterGroupFamily": "valkey9",
         "Description": f"Gimme-managed parameter group for {NAME}", "Tags": TAG},
    )
    stub.add_response(
        "modify_cache_parameter_group", {"CacheParameterGroupName": params},
        {"CacheParameterGroupName": params, "ParameterNameValues": [
            {"ParameterName": "cluster-enabled", "ParameterValue": "yes"},
            {"ParameterName": "maxmemory-policy", "ParameterValue": "noeviction"},
        ]},
    )
    stub.add_response(
        "create_user", {},
        {"UserId": default_id, "UserName": "default", "Engine": "valkey",
         "AccessString": "off ~* -@all", "Passwords": [ANY], "Tags": TAG},
    )
    if admin_exists:
        stub.add_response("describe_users", {"Users": [{"UserId": admin_id}]},
                          {"UserId": admin_id})
    else:
        stub.add_client_error(
            "describe_users", "UserNotFound", expected_params={"UserId": admin_id}
        )
        secrets_stub.add_response(
            "create_secret", {"ARN": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:x"},
            {"Name": f"gimme/workload/{NAME}/_admin", "SecretString": ANY, "Tags": [
                {"Key": "gimme:resource", "Value": NAME},
                {"Key": "gimme:secret-store", "Value": "workload-secrets"}]},
        )
        secrets_stub.add_response(
            "put_secret_value",
            {"ARN": "arn:aws:secretsmanager:eu-central-1:123456789012:secret:x",
             "VersionId": "v" * 32},
            {"SecretId": f"gimme/workload/{NAME}/_admin", "SecretString": ANY},
        )
        admin_request = {
            "UserId": admin_id, "UserName": "gimme-admin", "Engine": "valkey",
            "AccessString": ADMIN_ACCESS_STRING, "Passwords": [PASSWORD], "Tags": TAG,
        }
        if admin_fault:
            stub.add_client_error(
                "create_user", "AccessDenied", service_message=f"denied for {PASSWORD}",
                expected_params=admin_request,
            )
            return
        stub.add_response("create_user", {}, admin_request)
    stub.add_response(
        "create_user_group", {},
        {"UserGroupId": users, "Engine": "valkey", "UserIds": [default_id, admin_id],
         "Tags": TAG},
    )
    stub.add_response(
        "create_replication_group", {},
        {
            "ReplicationGroupId": GROUP_ID,
            "ReplicationGroupDescription": f"Gimme-managed Valkey for {NAME}",
            "Engine": "valkey", "EngineVersion": "9.0", "CacheNodeType": "cache.m7g.large",
            "CacheParameterGroupName": params, "CacheSubnetGroupName": subnet,
            "SecurityGroupIds": ["sg-0123456789abcdef2"], "ClusterMode": "enabled",
            "NumNodeGroups": 1, "ReplicasPerNodeGroup": 1, "AutomaticFailoverEnabled": True,
            "MultiAZEnabled": True, "TransitEncryptionEnabled": True,
            "TransitEncryptionMode": "required", "AtRestEncryptionEnabled": True,
            "UserGroupIds": [users], "Durability": "sync", "AutoMinorVersionUpgrade": False,
            "SnapshotRetentionLimit": 7, "SnapshotWindow": "03:00-04:00",
            "PreferredMaintenanceWindow": "sun:05:00-sun:06:00", "Port": 6379, "Tags": TAG,
        },
    )


def test_create_sends_exactly_the_fixed_contract_and_writes_the_admin_secret(
    monkeypatch,
) -> None:
    client, stub = stubbed("elasticache")
    secrets_client, secrets_stub = stubbed("secretsmanager")
    expect_create(stub, secrets_stub)
    adapter = adapter_with(
        monkeypatch, ("elasticache", (client, stub)),
        ("secretsmanager", (secrets_client, secrets_stub)),
    )
    account, network, store = context()

    with stub, secrets_stub:
        acknowledged = adapter.create_group(
            account, network, valkey(), NAME, GROUP_ID, store, "workload-secrets"
        )

    assert acknowledged is None
    stub.assert_no_pending_responses()
    secrets_stub.assert_no_pending_responses()


def test_an_existing_admin_user_keeps_its_stored_credential(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    secrets_client, secrets_stub = stubbed("secretsmanager")
    expect_create(stub, secrets_stub, admin_exists=True)
    adapter = adapter_with(
        monkeypatch, ("elasticache", (client, stub)),
        ("secretsmanager", (secrets_client, secrets_stub)),
    )
    account, network, store = context()

    with stub, secrets_stub:
        adapter.create_group(account, network, valkey(), NAME, GROUP_ID, store, "workload-secrets")

    secrets_stub.assert_no_pending_responses()  # no secret write was expected or made


def test_the_admin_password_never_leaks_into_errors(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    secrets_client, secrets_stub = stubbed("secretsmanager")
    expect_create(stub, secrets_stub, admin_fault=True)
    adapter = adapter_with(
        monkeypatch, ("elasticache", (client, stub)),
        ("secretsmanager", (secrets_client, secrets_stub)),
    )
    account, network, store = context()

    with stub, secrets_stub, pytest.raises(ResourceError) as raised:
        adapter.create_group(account, network, valkey(), NAME, GROUP_ID, store, "workload-secrets")

    assert str(raised.value) == "aws_elasticache_user_create_access_denied"
    assert PASSWORD not in repr(raised.value)


def test_a_foreign_parameter_group_of_the_same_name_is_refused(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    params = f"{GROUP_ID}-params"
    stub.add_response(
        "create_cache_subnet_group", {}, {
            "CacheSubnetGroupName": f"{GROUP_ID}-subnets",
            "CacheSubnetGroupDescription": f"Gimme-managed subnet group for {NAME}",
            "SubnetIds": ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"], "Tags": TAG},
    )
    stub.add_client_error("create_cache_parameter_group", "CacheParameterGroupAlreadyExists")
    pg_arn = "arn:aws:elasticache:eu-central-1:123456789012:parametergroup:" + params
    stub.add_response(
        "describe_cache_parameter_groups",
        {"CacheParameterGroups": [{"CacheParameterGroupName": params,
                                   "CacheParameterGroupFamily": "valkey9", "ARN": pg_arn}]},
        {"CacheParameterGroupName": params},
    )
    stub.add_response(
        "list_tags_for_resource", {"TagList": [{"Key": "gimme:resource", "Value": "someone-else"}]},
        {"ResourceName": pg_arn},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, store = context()

    with stub, pytest.raises(
        ResourceError, match="^aws_elasticache_parameter_group_ownership_mismatch$"
    ):
        adapter.create_group(account, network, valkey(), NAME, GROUP_ID, store, "workload-secrets")
    stub.assert_no_pending_responses()


# --- reviewed updates ------------------------------------------------------------------

ALLOWED_CHANGES = [
    ({"engine_version": "9.1"}, {"EngineVersion": "9.1"}),
    ({"node_type": "cache.m7g.xlarge"}, {"CacheNodeType": "cache.m7g.xlarge"}),
    ({"snapshot_retention_days": 14}, {"SnapshotRetentionLimit": 14}),
    ({"snapshot_window": "02:00-03:00"}, {"SnapshotWindow": "02:00-03:00"}),
    ({"maintenance_window": "mon:05:00-mon:06:00"},
     {"PreferredMaintenanceWindow": "mon:05:00-mon:06:00"}),
    (
        {"engine_version": "9.1", "node_type": "cache.m7g.xlarge", "snapshot_retention_days": 14,
         "snapshot_window": "02:00-03:00", "maintenance_window": "mon:05:00-mon:06:00"},
        {"EngineVersion": "9.1", "CacheNodeType": "cache.m7g.xlarge",
         "SnapshotRetentionLimit": 14, "SnapshotWindow": "02:00-03:00",
         "PreferredMaintenanceWindow": "mon:05:00-mon:06:00"},
    ),
]


@pytest.mark.parametrize(("desired", "changes"), ALLOWED_CHANGES)
def test_each_allowed_change_is_one_modification_of_only_the_differing_fields(
    tmp_path, desired, changes
) -> None:
    adapter = FakeValkey(observation())

    result = provision(adapter, tmp_path, **desired)

    assert adapter.modify_calls == [changes] and adapter.create_calls == 0
    assert result["modified_fields"] == sorted(changes) and result["phase"] == "ready"
    assert modification_for(valkey(**desired), adapter.live, GROUP_ID) == {}
    again = provision(adapter, tmp_path, **desired)
    assert len(adapter.modify_calls) == 1 and again["modified_fields"] == []


@pytest.mark.parametrize(
    ("live", "desired", "code"),
    [
        ({"engine_version": "10.0"}, {}, "engine_major"),
        ({"engine_version": "9.1"}, {"engine_version": "9.0"}, "engine_downgrade"),
        ({"engine_version": "9.0.3"}, {"engine_version": "9.0.1"}, "engine_downgrade"),
        ({}, {"node_type": "cache.m7g.4xlarge"}, "node_type"),
        ({"transit_encryption": False}, {"snapshot_retention_days": 14}, "tls"),
        ({"shards": 2}, {"node_type": "cache.m7g.xlarge"}, "topology"),
        ({"user_group_ids": ()}, {"snapshot_window": "02:00-03:00"}, "authentication"),
        ({"effective_durability": "async"}, {"engine_version": "9.1"}, "durability"),
        ({"automatic_minor_upgrade": True}, {"snapshot_retention_days": 3},
         "automatic_minor_upgrade"),
    ],
)
def test_a_live_difference_outside_the_allowlist_fails_apply_with_no_modify_call(
    tmp_path, live, desired, code
) -> None:
    adapter = FakeValkey(observation(**live))

    with pytest.raises(ResourceError, match=f"^aws_elasticache_modify_forbidden_{code}$"):
        provision(adapter, tmp_path, **desired)

    assert adapter.modify_calls == [] and load_observed(tmp_path, NAME) is None


def test_a_group_outside_the_contract_with_nothing_to_change_is_reported_not_refused(
    tmp_path,
) -> None:
    adapter = FakeValkey(observation(transit_encryption=False))

    result = provision(adapter, tmp_path)

    assert result["phase"] == "degraded" and result["issues"] == ["tls"]
    assert adapter.modify_calls == []


def test_a_group_that_is_already_modifying_is_left_alone_and_a_resume_sends_nothing(
    tmp_path,
) -> None:
    adapter = FakeValkey(observation(), settle_polls=1000)

    first = provision(adapter, tmp_path, engine_version="9.1")
    second = provision(adapter, tmp_path, engine_version="9.1")

    assert first["phase"] == second["phase"] == "pending"
    assert first["modified_fields"] == ["EngineVersion"] and second["modified_fields"] == []
    assert len(adapter.modify_calls) == 1
    adapter.settle_polls = 0
    third = provision(adapter, tmp_path, engine_version="9.1")
    assert third["phase"] == "ready" and len(adapter.modify_calls) == 1


def test_a_group_that_settles_within_the_poll_is_diffed_once_it_is_available(tmp_path) -> None:
    adapter = FakeValkey(observation(status="modifying"), settle_polls=2)

    result = provision(adapter, tmp_path, snapshot_retention_days=14)

    assert adapter.modify_calls == [{"SnapshotRetentionLimit": 14}]
    assert result["modified_fields"] == ["SnapshotRetentionLimit"]


def test_a_reviewed_update_reaches_the_group_through_the_mcp_apply(tmp_path, monkeypatch) -> None:
    adapter = FakeValkey(observation())
    use_state(tmp_path, monkeypatch, adapter=adapter)
    larger = valkey(node_type="cache.m7g.xlarge", snapshot_retention_days=14)
    update = server_module.plan_update_resource(NAME, larger)
    server_module.update_resource(NAME, larger, str(update["plan_id"]))

    plan = server_module.plan_apply_resource(NAME)
    result = server_module.apply_resource(NAME, str(plan["plan_id"]))

    assert result["changed"] is True and result["phase"] == "ready"
    assert result["modified_fields"] == ["CacheNodeType", "SnapshotRetentionLimit"]
    assert adapter.modify_calls == [
        {"CacheNodeType": "cache.m7g.xlarge", "SnapshotRetentionLimit": 14}
    ]


def test_a_modification_that_settles_during_the_poll_is_ready_and_recorded(tmp_path) -> None:
    adapter = FakeValkey(observation(), settle_polls=3)

    result = provision(adapter, tmp_path, node_type="cache.m7g.xlarge")

    assert result["phase"] == "ready"
    observed = load_observed(tmp_path, NAME)
    assert observed is not None and observed["phase"] == "ready"


def test_values_already_pending_count_as_applied(tmp_path) -> None:
    adapter = FakeValkey(
        observation(pending_engine_version="9.1", pending_node_type="cache.m7g.xlarge")
    )

    result = provision(adapter, tmp_path, engine_version="9.1", node_type="cache.m7g.xlarge")

    assert adapter.modify_calls == [] and result["phase"] == "pending"


def test_a_pending_downgrade_is_judged_against_the_pending_version(tmp_path) -> None:
    adapter = FakeValkey(observation(pending_engine_version="9.2"))

    with pytest.raises(ResourceError, match="engine_downgrade$"):
        provision(adapter, tmp_path, engine_version="9.1")

    assert adapter.modify_calls == []


def test_an_overdue_service_update_degrades_and_does_not_block_a_change(tmp_path) -> None:
    overdue = FakeValkey(observation(service_update_overdue=True))

    result = provision(overdue, tmp_path)

    assert result["phase"] == "degraded" and result["issues"] == ["service_update_overdue"]
    assert overdue.modify_calls == []
    fixing = FakeValkey(observation(service_update_overdue=True))
    result = provision(fixing, tmp_path, engine_version="9.1")
    assert fixing.modify_calls == [{"EngineVersion": "9.1"}]
    assert result["issues"] == ["service_update_overdue"]


def test_unobserved_fields_are_never_sent() -> None:
    live = observation(
        engine_version=None, snapshot_retention_days=None, snapshot_window=None,
        maintenance_window=None,
    )

    assert modification_for(valkey(), live, GROUP_ID) == {}
    assert group_drift(valkey(), live)["fields"] == {}


def test_inspect_reports_secret_free_drift(tmp_path, monkeypatch) -> None:
    live = observation(
        node_type="cache.m7g.xlarge", engine_version="9.1", status="modifying",
    )
    use_state(tmp_path, monkeypatch, adapter=FakeValkey(live, settle_polls=1000))

    result = server_module.inspect_resource(NAME)

    assert result["drift"] == {
        "fields": {
            "engine_version": {"desired": "9.0", "live": "9.1"},
            "node_type": {"desired": "cache.m7g.large", "live": "cache.m7g.xlarge"},
        },
        "modification_pending": True,
    }
    assert "cfg.example.cache.amazonaws.com" not in json.dumps(result)


def test_a_matching_group_has_no_drift() -> None:
    assert group_drift(valkey(), observation(engine_version="9.0.4")) == {
        "fields": {}, "modification_pending": False,
    }


def test_the_apply_plan_names_the_disruption_and_the_refusals(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch)

    effects = " ".join(cast(list[str], server_module.plan_apply_resource(NAME)["effects"]))

    assert "may fail over the primary" in effects
    assert "outside the modifications AWS allows" in effects
    assert "overdue required service update" in effects


# --- live options and registration ------------------------------------------------------


def test_registration_rejects_a_node_type_the_account_cannot_use(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch, registered=False)

    with pytest.raises(ResourceError, match="^aws_elasticache_node_type_unavailable$"):
        server_module.register_resource(NAME, valkey(node_type="cache.m7g.4xlarge"))
    assert NAME not in server_module.store.load().resources
    server_module.register_resource(NAME, valkey(node_type="cache.m7g.xlarge"))


@pytest.mark.parametrize("node_type", ["cache.t4g.small", "cache.m5.large", "cache.r5.large"])
def test_a_node_family_without_documented_durability_is_refused_before_any_aws_read(
    tmp_path, monkeypatch, node_type
) -> None:
    adapter = FakeValkey()
    adapter.options = ValkeyOptions(("9.0",), (node_type,))  # AWS lists it; still not durable
    use_state(tmp_path, monkeypatch, adapter=adapter, registered=False)

    def unexpected(*args, **kwargs):
        raise AssertionError("the family gate needs no provider read")

    adapter.live_options = unexpected  # type: ignore[method-assign]
    with pytest.raises(ResourceError, match="^aws_elasticache_node_type_not_durable$"):
        server_module.register_resource(NAME, valkey(node_type=node_type))
    assert NAME not in server_module.store.load().resources


def test_an_update_to_a_non_durable_family_is_refused(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch)

    with pytest.raises(ResourceError, match="^aws_elasticache_node_type_not_durable$"):
        server_module.plan_update_resource(NAME, valkey(node_type="cache.t4g.small"))


def test_an_update_checks_the_node_type_only_when_it_changes(tmp_path, monkeypatch) -> None:
    adapter = FakeValkey()
    use_state(tmp_path, monkeypatch, adapter=adapter)
    adapter.options = ValkeyOptions((), ())

    server_module.plan_update_resource(NAME, valkey(snapshot_retention_days=10))
    with pytest.raises(ResourceError, match="^aws_elasticache_node_type_unavailable$"):
        server_module.plan_update_resource(NAME, valkey(node_type="cache.m7g.xlarge"))


def test_the_live_options_resource_lists_versions_and_node_types(
    tmp_path, monkeypatch
) -> None:
    use_state(tmp_path, monkeypatch)

    assert server_module.valkey_options("primary") == {
        "aws_network": "primary", "engine_versions": ["9.0", "9.1"],
        "node_types": ["cache.m7g.large", "cache.m7g.xlarge"],
    }
    with pytest.raises(KeyError):
        server_module.valkey_options("missing")


# --- the boto adapter: updates and options ----------------------------------------------


def test_describe_reads_pending_values_and_overdue_service_updates(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    ec2, ec2_stub = stubbed("ec2")
    expect_describe(
        stub, ec2_stub,
        actions=[
            {"SlaMet": "yes", "UpdateActionStatus": "not-applied"},
            {"SlaMet": "no", "UpdateActionStatus": "complete"},
            {"SlaMet": "no", "UpdateActionStatus": "not-applied"},
        ],
        pending={"EngineVersion": "9.1", "CacheNodeType": "cache.m7g.xlarge"},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)), ("ec2", (ec2, ec2_stub)))
    account, network, _ = context()

    with stub, ec2_stub:
        observed = adapter.describe_group(account, network, GROUP_ID)

    assert observed == observation(
        pending_engine_version="9.1", pending_node_type="cache.m7g.xlarge",
        service_update_overdue=True, pending_service_updates=2,
    )


@pytest.mark.parametrize(
    ("action", "pending"), [({"SlaMet": "n/a", "UpdateActionStatus": "not-applied"}, 1),
                            ({"SlaMet": "no", "UpdateActionStatus": "not-applicable"}, 0)],
)
def test_only_an_unfinished_action_past_its_apply_by_date_is_overdue(
    monkeypatch, action, pending
) -> None:
    client, stub = stubbed("elasticache")
    ec2, ec2_stub = stubbed("ec2")
    expect_describe(stub, ec2_stub, actions=[action])
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)), ("ec2", (ec2, ec2_stub)))
    account, network, _ = context()

    with stub, ec2_stub:
        observed = adapter.describe_group(account, network, GROUP_ID)

    assert observed.service_update_overdue is False
    assert observed.pending_service_updates == pending


def test_a_service_update_read_failure_is_bounded_and_fails_the_describe(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_response(
        "describe_replication_groups", {"ReplicationGroups": [group_response()]},
        {"ReplicationGroupId": GROUP_ID},
    )
    stub.add_response("list_tags_for_resource", {"TagList": TAG}, {"ResourceName": ARN})
    stub.add_client_error("describe_update_actions", "AccessDenied", service_message="arn:secret")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub, pytest.raises(ResourceError) as raised:
        adapter.describe_group(account, network, GROUP_ID)

    assert str(raised.value) == "aws_elasticache_update_actions_access_denied"


def test_modify_sends_only_the_given_fields_immediately_then_describes(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_response(
        "modify_replication_group", {"ReplicationGroup": group_response(Status="modifying")},
        {"ReplicationGroupId": GROUP_ID, "ApplyImmediately": True, "EngineVersion": "9.1",
         "SnapshotRetentionLimit": 14},
    )
    expect_describe(stub, None, Status="modifying")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        observed = adapter.modify_group(
            account, network, GROUP_ID, {"EngineVersion": "9.1", "SnapshotRetentionLimit": 14}
        )

    assert observed.status == "modifying"
    stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "changes",
    [{}, {"AutomaticFailoverEnabled": False}, {"EngineVersion": "9.1", "Engine": "redis"},
     {"UserGroupIdsToRemove": ["x"]}, {"TransitEncryptionEnabled": False}],
)
def test_modify_refuses_any_field_outside_the_allowlist_before_calling_aws(
    monkeypatch, changes
) -> None:
    client, stub = stubbed("elasticache")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub, pytest.raises(ResourceError, match="^aws_elasticache_modify_field_forbidden$"):
        adapter.modify_group(account, network, GROUP_ID, changes)


def test_modify_failures_are_bounded(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_client_error(
        "modify_replication_group", "InvalidReplicationGroupState",
        service_message=f"{ARN} is busy",
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub, pytest.raises(ResourceError) as raised:
        adapter.modify_group(account, network, GROUP_ID, {"EngineVersion": "9.1"})

    assert str(raised.value) == "aws_elasticache_modify_invalid_state"


def test_allowed_node_types_join_scale_up_and_scale_down(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_response(
        "list_allowed_node_type_modifications",
        {"ScaleUpModifications": ["cache.m7g.xlarge"],
         "ScaleDownModifications": ["cache.t4g.small"]},
        {"ReplicationGroupId": GROUP_ID},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        allowed = adapter.allowed_node_types(account, network, GROUP_ID)

    assert allowed == {"cache.m7g.xlarge", "cache.t4g.small"}


def test_live_options_page_through_versions_and_offerings(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_response(
        "describe_cache_engine_versions",
        {"Marker": "next", "CacheEngineVersions": [
            {"Engine": "valkey", "EngineVersion": "8.0"},
            {"Engine": "valkey", "EngineVersion": "9.1"},
        ]},
        {"Engine": "valkey"},
    )
    stub.add_response(
        "describe_cache_engine_versions",
        {"CacheEngineVersions": [
            {"Engine": "valkey", "EngineVersion": "9.0"},
            {"Engine": "valkey", "EngineVersion": "10.0"},
            {"Engine": "valkey", "EngineVersion": "9.0"},
        ]},
        {"Engine": "valkey", "Marker": "next"},
    )
    stub.add_response(
        "describe_reserved_cache_nodes_offerings",
        {"Marker": "more", "ReservedCacheNodesOfferings": [
            {"CacheNodeType": "cache.m7g.xlarge"}, {"CacheNodeType": "cache.m7g.large"},
        ]},
        {},
    )
    stub.add_response(
        "describe_reserved_cache_nodes_offerings",
        {"ReservedCacheNodesOfferings": [
            {"CacheNodeType": "cache.m7g.large"}, {"CacheNodeType": "not-a-cache-type"},
            {"CacheNodeType": "cache.t4g.small"}, {"CacheNodeType": "cache.m5.large"},
            {"CacheNodeType": "cache.c7gn.large"}, {"CacheNodeType": "cache.m7gx.large"},
        ]},
        {"Marker": "more"},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        options = adapter.live_options(account, network)

    assert options == ValkeyOptions(
        ("9.0", "9.1", "10.0"), ("cache.c7gn.large", "cache.m7g.large", "cache.m7g.xlarge")
    )
    stub.assert_no_pending_responses()


MEMBERS = observation().member_ids


def metric_queries(members=MEMBERS) -> list[dict[str, object]]:
    return [
        {
            "Id": f"m{index}n{node}",
            "MetricStat": {
                "Metric": {
                    "Namespace": "AWS/ElastiCache", "MetricName": name,
                    "Dimensions": [{"Name": "CacheClusterId", "Value": member}],
                },
                "Period": 300, "Stat": "Maximum",
            },
        }
        for index, (_key, name, _limit, _code) in enumerate(resources_valkey_module.METRICS)
        for node, member in enumerate(members)
    ]


def test_recent_metrics_asks_for_the_fixed_queries_and_takes_the_maximum(monkeypatch) -> None:
    from botocore.stub import ANY

    client, stub = stubbed("cloudwatch")
    stub.add_response(
        "get_metric_data",
        {"NextToken": "more", "MetricDataResults": [
            {"Id": "m0n0", "Values": [41.0, 55.5]}, {"Id": "m0n1", "Values": [12.0]},
            {"Id": "m2n0", "Values": [0.0]},
        ]},
        {"MetricDataQueries": metric_queries(), "StartTime": ANY, "EndTime": ANY},
    )
    stub.add_response(
        "get_metric_data",
        {"MetricDataResults": [
            {"Id": "m1n1", "Values": [3]}, {"Id": "m3n1", "Values": [0.25]},
            {"Id": "m6n0", "Values": []},
        ]},
        {"MetricDataQueries": metric_queries(), "StartTime": ANY, "EndTime": ANY,
         "NextToken": "more"},
    )
    adapter = adapter_with(monkeypatch, ("cloudwatch", (client, stub)))
    account, network, _ = context()

    with stub:
        metrics = adapter.recent_metrics(account, network, MEMBERS)

    stub.assert_no_pending_responses()
    assert metrics == {
        "memory_usage_percent": 55.5, "connections": 3.0, "evictions": 0.0,
        "replica_lag_seconds": 0.25, "durability_lag_ms": None,
        "durability_rejections": None, "traffic_management_active": None,
    }


def test_recent_metrics_is_bounded_in_members_and_failures(monkeypatch) -> None:
    from botocore.stub import ANY

    client, stub = stubbed("cloudwatch")
    many = tuple(f"{GROUP_ID}-0001-{index:03d}" for index in range(1, 30))
    stub.add_response(
        "get_metric_data", {"MetricDataResults": []},
        {"MetricDataQueries": metric_queries(many[:8]), "StartTime": ANY, "EndTime": ANY},
    )
    stub.add_client_error(
        "get_metric_data", "AccessDenied", service_message="arn:aws:iam::123:role/secret"
    )
    adapter = adapter_with(monkeypatch, ("cloudwatch", (client, stub)))
    account, network, _ = context()

    with stub:
        assert set(adapter.recent_metrics(account, network, many).values()) == {None}
        with pytest.raises(ResourceError) as raised:
            adapter.recent_metrics(account, network, MEMBERS)

    assert str(raised.value) == "aws_elasticache_metrics_access_denied"
    assert "secret" not in str(raised.value)


def test_live_options_failures_are_bounded(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_client_error("describe_cache_engine_versions", "AccessDenied")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub, pytest.raises(ResourceError, match="^aws_elasticache_options_access_denied$"):
        adapter.live_options(account, network)


# --- bindings: ACL users, namespaces, credentials ---------------------------------------

DEPLOYMENT = "example-local"


def use_bound(tmp_path, monkeypatch, adapter: FakeValkey | None = None) -> FakeValkey:
    adapter = adapter or FakeValkey(observation())
    state = use_state(tmp_path, monkeypatch, adapter=adapter)
    bound = state.deployments[DEPLOYMENT].model_copy(update={"resources": ResourceBindings(
        database="devbox-postgres",
        valkey=ValkeyBinding(resource=NAME, uses=["cache", "queue"]),
    )})
    server_module.store.save(state.model_copy(update={
        "deployments": {**state.deployments, DEPLOYMENT: bound}
    }))
    return adapter


def provisioned(tmp_path, monkeypatch, adapter: FakeValkey | None = None) -> FakeValkey:
    adapter = use_bound(tmp_path, monkeypatch, adapter)
    plan = server_module.plan_apply_resource(NAME)
    server_module.apply_resource(NAME, str(plan["plan_id"]))
    return adapter


def bind(name: str = DEPLOYMENT) -> dict[str, object]:
    plan = server_module.plan_bind_resource(name)
    return server_module.bind_resource(name, str(plan["plan_id"]))


def test_the_binding_plan_names_namespaces_and_profile_and_is_secret_free(
    tmp_path, monkeypatch
) -> None:
    use_bound(tmp_path, monkeypatch)

    plan = server_module.plan_bind_resource(DEPLOYMENT)

    valkey = cast(dict[str, object], plan["valkey"])
    assert plan["kind"] == "resource_binding" and plan["database"] is None
    assert valkey["acl_profile"] == "laravel-v1" and valkey["resource_ready"] is False
    assert valkey["namespaces"] == {
        "cache": "{gimme:example-local}:cache:", "queue": "{gimme:example-local}:queue:",
        "horizon": "{gimme:example-local}:horizon:",
    }
    text = json.dumps(plan).lower()
    assert "password" not in text.replace("generated 48-character password", "")


def test_a_binding_needs_a_ready_resource_and_a_current_plan(tmp_path, monkeypatch) -> None:
    adapter = use_bound(tmp_path, monkeypatch)
    plan = server_module.plan_bind_resource(DEPLOYMENT)

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.bind_resource(DEPLOYMENT, "plan_" + "0" * 20)
    with pytest.raises(ValueError, match="not ready"):
        server_module.bind_resource(DEPLOYMENT, str(plan["plan_id"]))

    assert adapter.binding_calls == []
    server_module.apply_resource(NAME, str(server_module.plan_apply_resource(NAME)["plan_id"]))
    stale = server_module.plan_bind_resource(DEPLOYMENT)
    assert stale["plan_id"] != plan["plan_id"], "readiness is part of the plan"
    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.bind_resource(DEPLOYMENT, str(plan["plan_id"]))


def test_binding_records_a_secret_free_allocation_and_returns_no_credential(
    tmp_path, monkeypatch
) -> None:
    adapter = provisioned(tmp_path, monkeypatch)

    result = bind()

    user_id = derive_binding_user_id(GROUP_ID, DEPLOYMENT)
    valkey = cast(dict[str, object], result["valkey"])
    assert valkey["secret_reference"] == {
        "store": "workload-secrets", "secret": f"{NAME}/{DEPLOYMENT}",
    }
    assert adapter.binding_calls == [(DEPLOYMENT, False)]
    observed = load_observed(server_module.store.root, NAME)
    assert observed is not None
    allocation = cast(dict[str, dict[str, object]], observed["allocations"])[DEPLOYMENT]
    assert allocation["user_id"] == user_id and allocation["status"] == "active"
    assert set(allocation) == {"user_id", "secret_arn", "secret_version_id", "status"}
    inspected = server_module.inspect_resource(NAME)
    assert inspected["allocations"] == {DEPLOYMENT: {"status": "active"}}
    for output in (result, inspected):
        text = json.dumps(output)
        assert user_id not in text and "password" not in text and "username" not in text


def test_a_second_bind_keeps_the_credential_and_the_allocation(tmp_path, monkeypatch) -> None:
    adapter = provisioned(tmp_path, monkeypatch)
    bind()
    first = load_observed(server_module.store.root, NAME)

    bind()

    assert adapter.binding_calls == [(DEPLOYMENT, False), (DEPLOYMENT, True)]
    assert load_observed(server_module.store.root, NAME) == first


def test_a_rerun_of_provisioning_keeps_allocations(tmp_path, monkeypatch) -> None:
    provisioned(tmp_path, monkeypatch)
    bind()

    server_module.apply_resource(NAME, str(server_module.plan_apply_resource(NAME)["plan_id"]))

    observed = load_observed(server_module.store.root, NAME)
    assert observed is not None and list(cast(dict, observed["allocations"])) == [DEPLOYMENT]


@pytest.mark.parametrize(
    "live",
    [
        {"effective_durability": "async"},
        {"ingress_sources": (ADMIN_SG, "0.0.0.0/0")},
        {"security_group_ids": ("sg-0123456789abcdef9",)},
        {"status": "modifying"},
        {"service_update_overdue": True},
    ],
)
def test_a_group_that_a_live_read_does_not_call_ready_takes_no_new_binding(
    tmp_path, monkeypatch, live
) -> None:
    adapter = provisioned(tmp_path, monkeypatch)
    plan = server_module.plan_bind_resource(DEPLOYMENT)
    adapter.settle_polls = 1000
    adapter.live = dataclasses.replace(adapter.live, **live)

    with pytest.raises(ResourceError, match="^aws_elasticache_binding_resource_not_ready$"):
        server_module.bind_resource(DEPLOYMENT, str(plan["plan_id"]))

    assert adapter.binding_calls == []


def test_a_vanished_group_takes_no_binding(tmp_path, monkeypatch) -> None:
    adapter = provisioned(tmp_path, monkeypatch)
    plan = server_module.plan_bind_resource(DEPLOYMENT)
    adapter.live = None

    with pytest.raises(ResourceError, match="^aws_elasticache_binding_resource_not_ready$"):
        server_module.bind_resource(DEPLOYMENT, str(plan["plan_id"]))


def test_each_deployment_gets_a_distinct_user_namespace_and_secret() -> None:
    names = ["a", "a-b", "b", "orders", "orders-2", "x" * 64]
    users = {derive_binding_user_id(GROUP_ID, name) for name in names}
    tags = {namespace_prefixes(name, ["cache"])["cache"] for name in names}

    assert len(users) == len(tags) == len(names)
    for user in users:
        assert re.fullmatch(r"[a-z][a-z0-9-]{0,39}", user)
        assert user not in (f"{GROUP_ID}-default", f"{GROUP_ID}-admin")
    assert derive_binding_user_id(GROUP_ID, "a") != derive_binding_user_id("gimme-other", "a")


def test_a_namespace_pattern_never_matches_another_deployments_keys() -> None:
    import fnmatch

    for own, other in (("a", "a-b"), ("a-b", "a"), ("orders", "orders-2"), ("a", "ab")):
        tag = re.search(r"~(\S+)", laravel_access_string(own))
        assert tag is not None
        for use, prefix in namespace_prefixes(other, ["cache", "session", "queue"]).items():
            assert not fnmatch.fnmatchcase(prefix + "key", tag.group(1)), (own, other, use)
    own = re.search(r"~(\S+)", laravel_access_string("a"))
    assert own is not None
    for prefix in namespace_prefixes("a", ["cache", "queue"]).values():
        assert fnmatch.fnmatchcase(prefix + "key", own.group(1))


def test_the_laravel_profile_allows_only_the_deployment_namespace_and_listed_commands() -> None:
    tokens = laravel_access_string("orders").split()

    assert tokens[:4] == ["on", "~{gimme:orders}:*", "&{gimme:orders}:*", "-@all"]
    assert tokens[4:] == list(LARAVEL_COMMANDS)
    assert not any(token.startswith(("+@", "allkeys", "allchannels", "~*", "&*", ">", "nopass"))
                   for token in tokens), "no category, wildcard, or password grants"
    granted = {token.removeprefix("+") for token in tokens[4:]}
    for forbidden in (
        "flushall", "flushdb", "config", "acl", "keys", "scan", "shutdown", "debug", "save",
        "bgsave", "replicaof", "slaveof", "monitor", "client|kill", "client|list", "script|flush",
        "cluster|reset", "cluster|failover", "info", "role", "migrate", "move", "swapdb",
        "object", "dbsize", "randomkey", "pubsub", "sync", "psync", "module", "select",
    ):
        assert forbidden not in granted, forbidden
    assert len(granted) == len(LARAVEL_COMMANDS), "no duplicate grants"


def test_namespaces_share_one_hash_tag_and_horizon_accompanies_queue_only() -> None:
    prefixes = namespace_prefixes("orders", ["cache", "session"])
    assert prefixes == {
        "cache": "{gimme:orders}:cache:", "session": "{gimme:orders}:session:",
    }
    both = namespace_prefixes("orders", ["queue"])
    assert both == {
        "queue": "{gimme:orders}:queue:", "horizon": "{gimme:orders}:horizon:",
    }
    assert {value.split("}")[0] for value in both.values()} == {"{gimme:orders"}


def test_updates_cannot_drop_a_security_group_of_a_bound_target(tmp_path, monkeypatch) -> None:
    provisioned(tmp_path, monkeypatch)
    dropped = valkey(deployment_security_group_ids={})

    with pytest.raises(
        ResourceError, match="^aws_elasticache_update_forbidden_deployment_security_group_ids$"
    ):
        server_module.plan_update_resource(NAME, dropped)


def test_updates_cannot_move_the_secret_store_once_credentials_exist(
    tmp_path, monkeypatch
) -> None:
    provisioned(tmp_path, monkeypatch)
    bind()

    with pytest.raises(
        ResourceError, match="^aws_elasticache_update_forbidden_workload_secret_store$"
    ):
        server_module.plan_update_resource(NAME, valkey(workload_secret_store="other-store"))


def test_a_bound_resource_cannot_be_removed(tmp_path, monkeypatch) -> None:
    provisioned(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="still referenced by a deployment"):
        server_module.plan_cleanup_resource(NAME)


def test_apply_binding_rejects_an_unsafe_deployment_name(tmp_path) -> None:
    state = ControlState.model_validate(json.loads(EXAMPLE.read_text()))
    network = state.aws_networks["primary"]
    with pytest.raises(ResourceError, match="^deployment_name_invalid$"):
        apply_binding(
            FakeValkey(observation()), tmp_path,
            state.provider_accounts[network.provider_account], network, valkey(), NAME,
            cast(AWSSecretsManagerStore, state.secret_stores["workload-secrets"]),
            "workload-secrets", "Bad Name", ["cache"],
        )


# --- the boto adapter: security group ingress and binding users -------------------------


def sources_of(monkeypatch, rules, groups=None) -> tuple[str, ...] | None:
    client, stub = stubbed("elasticache")
    ec2, ec2_stub = stubbed("ec2")
    expect_describe(stub, ec2_stub, rules=rules, groups=groups)
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)), ("ec2", (ec2, ec2_stub)))
    account, network, _ = context()
    with stub, ec2_stub:
        observed = adapter.describe_group(account, network, GROUP_ID)
    assert observed is not None
    return observed.ingress_sources


def test_only_inbound_rules_that_reach_the_valkey_port_are_sources(monkeypatch) -> None:
    rules = [
        rule(ADMIN_SG), rule(ADMIN_SG), rule(DEVBOX_SG, low=6000, high=7000),
        rule("sg-0123456789abcdef7", protocol="-1", low=-1, high=-1),
        rule("0.0.0.0/0", low=22, high=22), rule("0.0.0.0/0", low=6380, high=6380),
        rule("0.0.0.0/0", egress=True, protocol="-1", low=-1, high=-1),
        rule("10.0.0.0/8", protocol="udp", low=6379, high=6379),
    ]

    assert sources_of(monkeypatch, rules) == (ADMIN_SG, DEVBOX_SG, "sg-0123456789abcdef7")


def bare(**fields) -> dict[str, object]:
    """A rule with no referenced group, then the given source fields."""
    base = {k: v for k, v in rule(ADMIN_SG).items() if k != "ReferencedGroupInfo"}
    return {**base, **fields}


def test_cidr_prefix_list_and_unrecognised_sources_are_reported(monkeypatch) -> None:
    rules = [
        rule("10.0.0.0/8"), bare(CidrIpv6="::/0"), bare(PrefixListId="pl-0123456789abcdef0"),
    ]

    assert sources_of(monkeypatch, rules) == ("10.0.0.0/8", "::/0", "pl:pl-0123456789abcdef0")


def test_a_rule_with_no_recognisable_source_is_unknown_not_safe(monkeypatch) -> None:
    assert sources_of(monkeypatch, [bare()]) == ("unknown",)


def test_a_group_with_no_inbound_rule_has_no_sources(monkeypatch) -> None:
    assert sources_of(monkeypatch, []) == ()


def test_every_attached_security_group_is_read(monkeypatch) -> None:
    other = "sg-0123456789abcdef9"

    assert sources_of(monkeypatch, [rule(ADMIN_SG)], groups=[other, GROUP_SG]) == (ADMIN_SG,)


def test_a_security_group_read_failure_is_bounded(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    ec2, ec2_stub = stubbed("ec2")
    expect_describe(stub, ec2_stub)
    ec2_stub._queue.clear()  # noqa: SLF001 - replace the queued rules response with a fault
    ec2_stub.add_client_error(
        "describe_security_group_rules", "UnauthorizedOperation", service_message="arn:secret"
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)), ("ec2", (ec2, ec2_stub)))
    account, network, _ = context()

    with stub, ec2_stub, pytest.raises(ResourceError) as raised:
        adapter.describe_group(account, network, GROUP_ID)

    assert str(raised.value) == "aws_elasticache_security_group_unavailable"


def test_the_adapter_has_no_way_to_edit_a_security_group() -> None:
    names = {name for name in dir(BotoElastiCacheAdapter) if not name.startswith("__")}

    assert not {name for name in names if "security" in name and "ingress" not in name}
    assert not {name for name in names if name.startswith(("authorize", "revoke"))}


class Capture:
    """Matches anything and keeps what it was compared with."""

    value: object = None

    def __eq__(self, other: object) -> bool:
        self.value = other
        return True


USER = derive_binding_user_id(GROUP_ID, DEPLOYMENT)
USER_TAGS = [
    {"Key": "gimme:resource", "Value": NAME}, {"Key": "gimme:deployment", "Value": DEPLOYMENT},
]
SECRET_ARN = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:x"


def expect_membership(stub, *, member: bool = False) -> None:
    users = [f"{GROUP_ID}-default", f"{GROUP_ID}-admin", *([USER] if member else [])]
    stub.add_response(
        "describe_user_groups", {"UserGroups": [{"UserGroupId": derive_user_group_id(GROUP_ID),
                                                 "UserIds": users}]},
        {"UserGroupId": derive_user_group_id(GROUP_ID)},
    )
    if not member:
        stub.add_response(
            "modify_user_group", {},
            {"UserGroupId": derive_user_group_id(GROUP_ID), "UserIdsToAdd": [USER]},
        )


def expect_secret(secrets_stub) -> Capture:
    payload = Capture()
    name = f"gimme/workload/{NAME}/{DEPLOYMENT}"
    secrets_stub.add_response(
        "create_secret", {"ARN": SECRET_ARN},
        {"Name": name, "SecretString": payload, "Tags": [
            {"Key": "gimme:deployment", "Value": DEPLOYMENT},
            {"Key": "gimme:resource", "Value": NAME},
            {"Key": "gimme:secret-store", "Value": "workload-secrets"}]},
    )
    secrets_stub.add_response(
        "put_secret_value", {"ARN": SECRET_ARN, "VersionId": "v" * 32},
        {"SecretId": name, "SecretString": ANY},
    )
    return payload


def binder(monkeypatch, sizes: list[int] | None = None):
    client, stub = stubbed("elasticache")
    secrets_client, secrets_stub = stubbed("secretsmanager")
    adapter = adapter_with(
        monkeypatch, ("elasticache", (client, stub)),
        ("secretsmanager", (secrets_client, secrets_stub)),
    )
    if sizes is not None:
        monkeypatch.setattr(
            "gimme.resources_valkey.secrets_module.token_urlsafe",
            lambda size: sizes.append(size) or PASSWORD,
        )
    return adapter, stub, secrets_stub


def ensure(adapter, *, keep: bool):
    account, network, store = context()
    return adapter.ensure_binding(
        account, network, store, "workload-secrets", NAME, GROUP_ID, DEPLOYMENT, keep
    )


def test_a_new_binding_creates_the_secret_before_the_user_and_joins_the_user_group(
    monkeypatch,
) -> None:
    sizes: list[int] = []
    adapter, stub, secrets_stub = binder(monkeypatch, sizes)
    stub.add_client_error("describe_users", "UserNotFound", expected_params={"UserId": USER})
    payload = expect_secret(secrets_stub)
    stub.add_response(
        "create_user", {},
        {"UserId": USER, "UserName": f"gimme-{DEPLOYMENT}", "Engine": "valkey",
         "AccessString": laravel_access_string(DEPLOYMENT), "Passwords": [PASSWORD],
         "Tags": USER_TAGS},
    )
    expect_membership(stub)

    with stub, secrets_stub:
        written = ensure(adapter, keep=False)

    assert written == (USER, SECRET_ARN, "v" * 32)
    assert sizes == [36], "36 random bytes make a 48-character URL-safe password"
    assert json.loads(cast(str, payload.value)) == {
        "password": PASSWORD, "username": f"gimme-{DEPLOYMENT}",
    }
    stub.assert_no_pending_responses()
    secrets_stub.assert_no_pending_responses()


def test_an_existing_user_with_a_recorded_credential_only_converges_profile_and_membership(
    monkeypatch,
) -> None:
    adapter, stub, secrets_stub = binder(monkeypatch)
    stub.add_response("describe_users", {"Users": [{"UserId": USER}]}, {"UserId": USER})
    stub.add_response(
        "modify_user", {}, {"UserId": USER, "AccessString": laravel_access_string(DEPLOYMENT)}
    )
    expect_membership(stub, member=True)

    with stub, secrets_stub:
        assert ensure(adapter, keep=True) is None

    stub.assert_no_pending_responses()
    secrets_stub.assert_no_pending_responses()


def test_an_existing_user_with_no_recorded_credential_gets_a_new_one(monkeypatch) -> None:
    adapter, stub, secrets_stub = binder(monkeypatch)
    stub.add_response("describe_users", {"Users": [{"UserId": USER}]}, {"UserId": USER})
    expect_secret(secrets_stub)
    stub.add_response(
        "modify_user", {},
        {"UserId": USER, "AccessString": laravel_access_string(DEPLOYMENT),
         "Passwords": [PASSWORD]},
    )
    expect_membership(stub, member=True)

    with stub, secrets_stub:
        written = ensure(adapter, keep=False)

    assert written is not None and written[0] == USER
    stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("failing", "code"),
    [("create_user", "user_bind"), ("modify_user_group", "user_group_bind")],
)
def test_binding_failures_are_bounded_and_never_carry_the_password(
    monkeypatch, failing, code
) -> None:
    adapter, stub, secrets_stub = binder(monkeypatch)
    stub.add_client_error("describe_users", "UserNotFound", expected_params={"UserId": USER})
    expect_secret(secrets_stub)
    if failing == "create_user":
        stub.add_client_error("create_user", "AccessDenied", service_message=f"bad {PASSWORD}")
    else:
        stub.add_response("create_user", {}, None)
        stub.add_response(
            "describe_user_groups", {"UserGroups": []},
            {"UserGroupId": derive_user_group_id(GROUP_ID)},
        )
        stub.add_client_error(
            "modify_user_group", "InvalidUserGroupState", service_message=f"bad {PASSWORD}"
        )

    with stub, secrets_stub, pytest.raises(ResourceError) as raised:
        ensure(adapter, keep=False)

    assert PASSWORD not in repr(raised.value)
    assert str(raised.value).startswith(f"aws_elasticache_{code}_")


def test_a_user_read_failure_stops_before_any_secret_is_written(monkeypatch) -> None:
    adapter, stub, secrets_stub = binder(monkeypatch)
    stub.add_client_error("describe_users", "Throttling", expected_params={"UserId": USER})

    with stub, secrets_stub, pytest.raises(ResourceError, match="user_describe_throttled"):
        ensure(adapter, keep=False)

    secrets_stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "name", ["x} ~* +@all", "a b", "A", "a\n~*", "", "-a", "a:b", "a}", "{a", "a" * 65, "a*"]
)
def test_the_acl_helpers_refuse_a_name_that_could_alter_the_access_string(name) -> None:
    for helper in (
        laravel_access_string, lambda n: namespace_prefixes(n, ["cache"]),
        lambda n: derive_binding_user_id(GROUP_ID, n),
    ):
        with pytest.raises(ResourceError, match="^deployment_name_invalid$"):
            helper(name)


def test_an_observation_cache_from_before_bindings_is_upgraded_in_place(tmp_path) -> None:
    provision(FakeValkey(observation()), tmp_path)
    path = tmp_path / "observed-resources" / f"{NAME}.json"
    old = json.loads(path.read_text())
    del old["allocations"]
    path.write_text(json.dumps(old))

    loaded = load_observed(tmp_path, NAME)

    assert loaded is not None and loaded["allocations"] == {}


@pytest.mark.parametrize(
    "allocations",
    [{"a": {}}, {"a": {"user_id": "u"}}, {"A": {"user_id": "u", "secret_arn": "x",
     "secret_version_id": "v", "status": "active"}}, {"a": {"user_id": "u", "secret_arn": "x",
     "secret_version_id": "v", "status": "gone"}}, [], "x"],
)
def test_a_malformed_allocation_makes_the_cache_invalid(tmp_path, allocations) -> None:
    provision(FakeValkey(observation()), tmp_path)
    path = tmp_path / "observed-resources" / f"{NAME}.json"
    document = json.loads(path.read_text())
    document["allocations"] = allocations
    path.write_text(json.dumps(document))

    with pytest.raises(ResourceError, match="^observed_resource_invalid$"):
        load_observed(tmp_path, NAME)


def test_a_deployment_with_a_managed_database_and_valkey_binds_the_database_first(
    tmp_path, monkeypatch
) -> None:
    adapter = provisioned(tmp_path, monkeypatch)
    state = server_module.store.load()
    dual = state.deployments[DEPLOYMENT].model_copy(update={"resources": ResourceBindings(
        database="example-rds-postgres",
        valkey=ValkeyBinding(resource=NAME, uses=["cache"]),
    )})
    server_module.store.save(state.model_copy(update={
        "deployments": {**state.deployments, DEPLOYMENT: dual}
    }))
    order: list[str] = []
    monkeypatch.setattr(
        ManagedResourceOrchestrator, "_database_binding_plan",
        lambda self, name: {"resource": "example-rds-postgres", "resource_ready": True},
    )
    monkeypatch.setattr(
        ManagedResourceOrchestrator, "_bind_database",
        lambda self, name, expected: order.append("database") or {
            "database": "gimme_example_local"},
    )
    original = adapter.ensure_binding
    adapter.ensure_binding = lambda *a, **k: order.append("valkey") or original(*a, **k)  # type: ignore[method-assign]

    result = bind()

    assert order == ["database", "valkey"]
    assert result["database"] == "gimme_example_local" and "valkey" in result
    assert "password" not in json.dumps(result)


def test_an_unready_managed_database_stops_a_dual_binding_before_any_change(
    tmp_path, monkeypatch
) -> None:
    adapter = provisioned(tmp_path, monkeypatch)
    state = server_module.store.load()
    dual = state.deployments[DEPLOYMENT].model_copy(update={"resources": ResourceBindings(
        database="example-rds-postgres",
        valkey=ValkeyBinding(resource=NAME, uses=["cache"]),
    )})
    server_module.store.save(state.model_copy(update={
        "deployments": {**state.deployments, DEPLOYMENT: dual}
    }))
    plan = server_module.plan_bind_resource(DEPLOYMENT)

    with pytest.raises(ValueError, match="not ready"):
        server_module.bind_resource(DEPLOYMENT, str(plan["plan_id"]))

    assert adapter.binding_calls == []


# --- the laravel-cluster-v1 contract ----------------------------------------------------

class FakeSecrets:
    """Metadata only, like planning: the values are resolved at apply time."""

    def describe(self, account, store_name, store, secret) -> SecretMetadata:
        return SecretMetadata(version_id="v1", identity=f"arn:{secret}")

    def resolve(self, account, store_name, store, secret, version_id) -> str:
        return json.dumps({"username": "gimme-u", "password": PASSWORD})


def without_local_secrets(monkeypatch) -> None:
    """The example's own sops secret is not on disk; only the contract's are under test."""
    state = server_module.store.load()
    stripped = state.deployments[DEPLOYMENT].model_copy(update={"secrets": {}})
    server_module.store.save(state.model_copy(update={
        "deployments": {**state.deployments, DEPLOYMENT: stripped}
    }))
    monkeypatch.setattr(server_module, "aws_secrets", FakeSecrets())


def bound_and_ready(tmp_path, monkeypatch) -> FakeValkey:
    adapter = provisioned(tmp_path, monkeypatch)
    without_local_secrets(monkeypatch)
    bind()
    return adapter


def capture_runs(monkeypatch) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def fake_run(task, *args, **kwargs):
        calls.append({"task": task, **kwargs})
        return CommandResult(["dep"], 0, "GIMME_REVISION|" + "a" * 40)

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    return calls


def test_the_contract_injects_fixed_values_and_pins_undeclared_uses_local() -> None:
    values = contract_variables(DEPLOYMENT, ["cache", "queue"], "cfg.example.internal", 6379)

    assert values["GIMME_VALKEY_CONTRACT"] == "laravel-cluster-v1"
    assert values["GIMME_VALKEY_HOST"] == "cfg.example.internal"
    assert values["GIMME_VALKEY_USES"] == "cache,queue"
    assert values["GIMME_VALKEY_CACHE_PREFIX"] == "{gimme:example-local}:cache:"
    assert values["GIMME_VALKEY_HORIZON_PREFIX"] == values["HORIZON_PREFIX"] == (
        "{gimme:example-local}:horizon:"
    )
    assert (values["CACHE_STORE"], values["QUEUE_CONNECTION"]) == ("redis", "redis")
    assert values["SESSION_DRIVER"] == "file", "an undeclared use never reaches Valkey"
    assert values["GIMME_VALKEY_READ_REPLICAS"] == "false"
    assert values["GIMME_VALKEY_VERIFY_PEER"] == "true"
    assert values["GIMME_VALKEY_CLUSTER"] == "true" and values["GIMME_VALKEY_SCHEME"] == "tls"
    assert "GIMME_VALKEY_PASSWORD" not in values and "GIMME_VALKEY_USERNAME" not in values


def test_every_key_the_contract_sets_is_protected_from_deployment_environment() -> None:
    values = contract_variables(DEPLOYMENT, ["cache", "session", "queue"], "h", 6379)

    protected = {
        key for key in values if key.startswith("GIMME_VALKEY_") or key in MANAGED_VALKEY_ENV_KEYS
    }
    assert protected == set(values)
    assert MANAGED_VALKEY_ENV_KEYS <= set(values)


def test_no_queue_means_no_horizon_prefix_and_a_local_queue() -> None:
    values = contract_variables(DEPLOYMENT, ["cache", "session"], "h", 6379)

    assert "HORIZON_PREFIX" not in values and "GIMME_VALKEY_HORIZON_PREFIX" not in values
    assert values["QUEUE_CONNECTION"] == "sync" and values["SESSION_DRIVER"] == "redis"


def test_the_credential_is_referenced_by_field_never_carried() -> None:
    references = credential_references("workload-secrets", NAME, DEPLOYMENT)

    assert {key: (ref.store, ref.secret, ref.field) for key, ref in references.items()} == {
        "GIMME_VALKEY_USERNAME": ("workload-secrets", f"{NAME}/{DEPLOYMENT}", "username"),
        "GIMME_VALKEY_PASSWORD": ("workload-secrets", f"{NAME}/{DEPLOYMENT}", "password"),
    }


@pytest.mark.parametrize("field", ["variables", "secrets"])
@pytest.mark.parametrize("key", [
    "GIMME_VALKEY_HOST", "GIMME_VALKEY_PASSWORD", "CACHE_STORE", "SESSION_DRIVER",
    "QUEUE_CONNECTION", "HORIZON_PREFIX",
])
def test_deployment_environment_cannot_override_a_managed_contract(
    tmp_path, monkeypatch, field, key
) -> None:
    use_bound(tmp_path, monkeypatch)
    document = server_module.store.load().model_dump(mode="json")
    value = {"store": "local-sops", "secret": "x", "field": "F"} if field == "secrets" else "x"
    document["deployments"][DEPLOYMENT][field][key] = value

    with pytest.raises(ValueError, match="managed by the Valkey contract|reserved or unsafe"):
        ControlState.model_validate(document)


def test_a_target_local_binding_keeps_its_own_adapter_values(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch)
    document = server_module.store.load().model_dump(mode="json")
    document["deployments"][DEPLOYMENT]["variables"]["SESSION_DRIVER"] = "array"
    ControlState.model_validate(document)
    document["deployments"][DEPLOYMENT]["variables"]["GIMME_VALKEY_HOST"] = "x"

    with pytest.raises(ValueError, match="managed by the Valkey contract"):
        ControlState.model_validate(document)


def test_deployment_resources_name_the_contract_and_carry_no_endpoint_or_credential(
    tmp_path, monkeypatch
) -> None:
    bound_and_ready(tmp_path, monkeypatch)

    plan = server_module.plan_deployment_resources(DEPLOYMENT)

    contract = cast(dict[str, object], plan["valkey_contract"])
    assert contract["contract"] == "laravel-cluster-v1" and contract["uses"] == ["cache", "queue"]
    assert contract["adapters"] == {
        "CACHE_STORE": "redis", "SESSION_DRIVER": "file", "QUEUE_CONNECTION": "redis"
    }
    assert contract["probes"] == probe_names(["cache", "queue"], False)
    assert contract["credential_keys"] == ["GIMME_VALKEY_USERNAME", "GIMME_VALKEY_PASSWORD"]
    assert {item["environment_key"] for item in plan["secret_versions"]} >= {
        "GIMME_VALKEY_USERNAME", "GIMME_VALKEY_PASSWORD"
    }
    assert "readiness_issues" not in plan
    text = json.dumps(plan)
    assert "cfg." not in text and ".cache.amazonaws.com" not in text and "password\"" not in text


def test_the_plan_changes_when_the_endpoint_the_contract_injects_changes(
    tmp_path, monkeypatch
) -> None:
    adapter = bound_and_ready(tmp_path, monkeypatch)
    before = server_module.plan_deployment_resources(DEPLOYMENT)

    observed = load_observed(server_module.store.root, NAME)
    assert observed is not None
    observed["endpoint"] = "moved." + str(observed["endpoint"])
    (server_module.store.root / "observed-resources" / f"{NAME}.json").write_text(
        json.dumps(observed)
    )

    assert server_module.plan_deployment_resources(DEPLOYMENT)["plan_id"] != before["plan_id"]
    assert adapter.users


def test_deployment_resources_are_not_ready_until_the_resource_is_ready_and_bound(
    tmp_path, monkeypatch
) -> None:
    use_bound(tmp_path, monkeypatch)
    without_local_secrets(monkeypatch)

    assert "valkey_resource_not_ready" in server_module.plan_deployment_resources(
        DEPLOYMENT
    )["readiness_issues"]

    server_module.apply_resource(NAME, str(server_module.plan_apply_resource(NAME)["plan_id"]))
    assert "valkey_binding_missing" in server_module.plan_deployment_resources(
        DEPLOYMENT
    )["readiness_issues"]

    bind()
    assert "readiness_issues" not in server_module.plan_deployment_resources(DEPLOYMENT)


def test_a_release_is_blocked_until_the_binding_exists(tmp_path, monkeypatch) -> None:
    provisioned(tmp_path, monkeypatch)
    monkeypatch.setattr(server_module, "aws_secrets", FakeSecrets())
    capture_runs(monkeypatch)

    with pytest.raises(ValueError, match="valkey_binding_missing"):
        server_module.plan_deployment(DEPLOYMENT)


def test_a_deploy_carries_the_contract_and_probe_but_never_a_credential(
    tmp_path, monkeypatch
) -> None:
    bound_and_ready(tmp_path, monkeypatch)
    calls = capture_runs(monkeypatch)

    server_module.plan_deployment(DEPLOYMENT)

    render = next(call for call in calls if call.get("arguments") == ("--plan",))
    variables = cast(dict[str, str], render["variables"])
    assert variables["LOG_CHANNEL"] == "stack", "the Deployment's own values still flow"
    assert variables["GIMME_VALKEY_CONTRACT"] == "laravel-cluster-v1"
    assert variables["HORIZON_PREFIX"] == "{gimme:example-local}:horizon:"
    probe = cast(dict[str, object], render["valkey_probe"])
    assert probe["deployment"] == DEPLOYMENT and probe["uses"] == ["cache", "queue"]
    assert probe["horizon"] is False, "queue alone does not run Horizon"
    assert probe["prefixes"] == namespace_prefixes(DEPLOYMENT, ["cache", "queue"])
    for call in calls:
        assert "PASSWORD" not in json.dumps(call, default=str).upper().replace(
            "GIMME_VALKEY_PASSWORD", ""
        )


def test_a_target_local_binding_gets_neither_contract_nor_probe(tmp_path, monkeypatch) -> None:
    use_state(tmp_path, monkeypatch)
    without_local_secrets(monkeypatch)
    calls = capture_runs(monkeypatch)

    server_module.plan_deployment(DEPLOYMENT)

    render = next(call for call in calls if call.get("arguments") == ("--plan",))
    assert render["valkey_probe"] is None
    assert not any(key.startswith("GIMME_VALKEY_") for key in cast(dict, render["variables"]))


def test_the_probe_checks_horizon_only_for_a_deployment_that_runs_it(
    tmp_path, monkeypatch
) -> None:
    bound_and_ready(tmp_path, monkeypatch)
    state = server_module.store.load()
    running = state.deployments[DEPLOYMENT].model_copy(update={"workers": HorizonWorkerConfig()})
    server_module.store.save(state.model_copy(update={
        "deployments": {**state.deployments, DEPLOYMENT: running}
    }))
    calls = capture_runs(monkeypatch)

    plan = server_module.plan_deployment_resources(DEPLOYMENT)
    server_module.plan_deployment(DEPLOYMENT)

    contract = cast(dict[str, object], plan["valkey_contract"])
    assert contract["probes"] == probe_names(["cache", "queue"], True)
    render = next(call for call in calls if call.get("arguments") == ("--plan",))
    assert cast(dict[str, object], render["valkey_probe"])["horizon"] is True


def test_a_corrupt_observation_makes_the_resource_unready_instead_of_breaking_tasks(
    tmp_path, monkeypatch
) -> None:
    bound_and_ready(tmp_path, monkeypatch)
    (server_module.store.root / "observed-resources" / f"{NAME}.json").write_text("{not json")
    calls = capture_runs(monkeypatch)

    plan = server_module.plan_deployment_resources(DEPLOYMENT)
    server_module._run_deployment("gimme:inspect", DEPLOYMENT)

    assert "valkey_resource_not_ready" in plan["readiness_issues"]
    assert calls[-1]["valkey_probe"] is None


# --- retention, destruction, and forgetting -----------------------------------------------

DESTROYER = "arn:aws:iam::123456789012:role/gimme-destroy"
CONFIRM = f"DESTROY RESOURCE {NAME}"


def unreferenced(tmp_path, monkeypatch, adapter: FakeValkey | None = None) -> FakeValkey:
    """A provisioned Resource that no Deployment references."""
    adapter = provisioned(tmp_path, monkeypatch, adapter)
    document = server_module.store.load().model_dump(mode="json")
    for deployment in document["deployments"].values():
        if (deployment["resources"].get("valkey") or {}).get("resource") == NAME:
            deployment["resources"]["valkey"] = {"resource": "devbox-valkey", "uses": ["cache"]}
    server_module.store.save(ControlState.model_validate(document))
    return adapter


def destroyable(tmp_path, monkeypatch, adapter: FakeValkey | None = None) -> FakeValkey:
    """The same, on an account with a destructive role."""
    adapter = unreferenced(tmp_path, monkeypatch, adapter)
    document = server_module.store.load().model_dump(mode="json")
    document["provider_accounts"]["main"]["destructive_role_arn"] = DESTROYER
    server_module.store.save(ControlState.model_validate(document))
    return adapter


def destroy(confirmation: str = CONFIRM) -> dict[str, object]:
    plan = server_module.plan_destroy_resource(NAME)
    return server_module.apply_destroy_resource(NAME, str(plan["plan_id"]), confirmation)


@pytest.fixture
def instant(monkeypatch):
    monkeypatch.setattr(resources_valkey_module, "POLL_INTERVAL_SECONDS", 0)


def test_the_destructive_role_is_optional_and_must_be_a_third_distinct_role() -> None:
    document = json.loads(EXAMPLE.read_text())["provider_accounts"]["main"]
    assert AWSProviderAccount.model_validate(document).destructive_role_arn is None

    valid = {**document, "destructive_role_arn": DESTROYER}
    assert AWSProviderAccount.model_validate(valid).destructive_role_arn == DESTROYER
    for role in (
        document["inspection_role_arn"], document["resolver_role_arn"],
        "arn:aws:iam::999999999999:role/gimme-destroy", "arn:aws:iam::123456789012:user/x",
    ):
        with pytest.raises(ValidationError):
            AWSProviderAccount.model_validate({**document, "destructive_role_arn": role})


def test_a_destruction_plan_is_local_secret_free_and_names_what_it_keeps(
    tmp_path, monkeypatch
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    describes = adapter.describe_calls

    plan = server_module.plan_destroy_resource(NAME)

    assert adapter.describe_calls == describes, "planning makes no AWS call"
    assert plan["confirmation"] == CONFIRM and plan["irreversible"] is True
    assert str(plan["final_snapshot"]).startswith(f"{GROUP_ID}-final-")
    assert any("final snapshot" in item for item in cast(list[str], plan["retains"]))
    assert any("secrets" in item for item in cast(list[str], plan["retains"]))
    text = json.dumps(plan)
    assert ARN not in text and "gimme-u-" not in text and "password" not in text.lower()


@pytest.mark.parametrize("problem", ["role", "reference", "allocation", "unobserved", "kind"])
def test_a_destruction_plan_fails_closed_until_it_is_safe(
    tmp_path, monkeypatch, problem
) -> None:
    destroyable(tmp_path, monkeypatch)
    document = server_module.store.load().model_dump(mode="json")
    name = NAME
    match problem:
        case "role":
            document["provider_accounts"]["main"]["destructive_role_arn"] = None
            expected = "aws_elasticache_destroy_role_missing"
        case "reference":
            document["deployments"][DEPLOYMENT]["resources"]["valkey"] = {
                "resource": NAME, "uses": ["cache"]}
            expected = "still referenced"
        case "allocation":
            expected = "aws_elasticache_destroy_bindings_remain"
        case "unobserved":
            (server_module.store.root / "observed-resources" / f"{NAME}.json").unlink()
            expected = "aws_elasticache_destroy_not_observed"
        case _:
            name = "devbox-postgres"
            expected = "not a managed ElastiCache Valkey resource"
    if problem == "allocation":
        observed_path = server_module.store.root / "observed-resources" / f"{NAME}.json"
        observed = json.loads(observed_path.read_text())
        observed["allocations"] = {DEPLOYMENT: {
            "user_id": "gimme-u-x", "secret_arn": "arn", "secret_version_id": "v",
            "status": "active"}}
        observed_path.write_text(json.dumps(observed))
    server_module.store.save(ControlState.model_validate(document))

    with pytest.raises((ValueError, ResourceError), match=expected):
        server_module.plan_destroy_resource(name)


def test_destruction_needs_the_exact_confirmation_and_a_current_plan(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    plan = server_module.plan_destroy_resource(NAME)

    with pytest.raises(ValueError, match="confirmation must exactly equal"):
        server_module.apply_destroy_resource(NAME, str(plan["plan_id"]), "DESTROY")
    with pytest.raises(ValueError, match="confirmation must exactly equal"):
        server_module.apply_destroy_resource(NAME, str(plan["plan_id"]), CONFIRM.lower())
    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.apply_destroy_resource(NAME, "plan_" + "0" * 20, CONFIRM)

    assert adapter.delete_calls == [] and adapter.dependents_calls == []


def test_destruction_deletes_the_group_then_what_gimme_created_and_forgets_the_resource(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    bind_state = load_observed(server_module.store.root, NAME)
    assert bind_state is not None

    result = destroy()

    assert result["destroyed"] is True and result["phase"] == "destroyed"
    assert adapter.delete_calls == [result["final_snapshot"]]
    assert adapter.live is None
    assert [call[0] for call in adapter.dependents_calls] == [NAME]
    assert adapter.dependents_calls[0][1][:2] == [
        f"{GROUP_ID}-default", f"{GROUP_ID}-admin",
    ]
    assert NAME not in server_module.store.load().resources
    assert load_observed(server_module.store.root, NAME) is None
    assert not (server_module.store.root / "destroying-resources" / f"{NAME}.json").exists()
    assert not (server_module.store.root / "retained-resources" / f"{NAME}.json").exists()
    receipt = resources_valkey_module.load_destroyed_receipt(server_module.store.root, NAME)
    assert receipt is not None and receipt["final_snapshot"] == result["final_snapshot"]
    assert bind_state["identity"] == ARN


def test_a_destroyed_resources_final_snapshot_can_be_purged_with_an_exact_plan(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    destroyed = destroy()
    adapter.snapshots = [SnapshotInfo(
        name=str(destroyed["final_snapshot"]), source="system", status="available",
        created=None, engine_version="9.0", shards=1,
    )]

    plan = server_module.plan_purge_final_snapshot(NAME)

    assert plan["confirmation"] == f"PURGE FINAL SNAPSHOT {NAME}"
    assert plan["snapshot"] == destroyed["final_snapshot"]
    with pytest.raises(ValueError, match="confirmation must exactly equal"):
        server_module.apply_purge_final_snapshot(NAME, str(plan["plan_id"]), "PURGE")
    result = server_module.apply_purge_final_snapshot(
        NAME, str(plan["plan_id"]), str(plan["confirmation"])
    )

    assert result["changed"] is True and result["resource"] == NAME and result["purged"] is True
    assert adapter.deleted_snapshots == [destroyed["final_snapshot"]]
    assert resources_valkey_module.load_destroyed_receipt(server_module.store.root, NAME) is None


def test_a_destroyed_resources_credentials_can_be_purged_with_an_exact_plan(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    destroy()

    plan = server_module.plan_purge_retained_secrets(NAME)

    assert plan["credentials"] == 1
    result = server_module.apply_purge_retained_secrets(
        NAME, str(plan["plan_id"]), str(plan["confirmation"])
    )

    assert result["changed"] is True and result["purged"] == 1
    assert adapter.deleted_secrets == ["_admin"]
    assert resources_valkey_module.load_destroyed_secrets(server_module.store.root, NAME) is None


def test_a_group_still_deleting_returns_pending_and_a_repeat_continues(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    adapter.settle_polls = 1000
    monkeypatch.setattr(resources_valkey_module, "POLL_BUDGET_SECONDS", 0)
    plan = server_module.plan_destroy_resource(NAME)

    first = server_module.apply_destroy_resource(NAME, str(plan["plan_id"]), CONFIRM)

    assert first["phase"] == "deleting" and first["destroyed"] is False
    assert adapter.dependents_calls == [] and NAME in server_module.store.load().resources
    marker = server_module.store.root / "destroying-resources" / f"{NAME}.json"
    assert marker.is_file()
    inspected = server_module.inspect_resource(NAME)
    assert inspected["operation"] == "destroying"
    assert inspected["phase"] == "deleting"
    assert inspected["progress"] == {"phase": "deleting"}
    assert server_module.plan_destroy_resource(NAME)["plan_id"] == plan["plan_id"], (
        "the same plan resumes the destruction"
    )

    adapter.settle_polls = 1  # the repeat first sees the group still 'deleting', then gone
    monkeypatch.setattr(resources_valkey_module, "POLL_BUDGET_SECONDS", 30)
    second = server_module.apply_destroy_resource(NAME, str(plan["plan_id"]), CONFIRM)

    assert second["destroyed"] is True
    assert adapter.delete_calls == [first["final_snapshot"]], "the group is deleted only once"
    assert not marker.exists()


def test_a_partly_destroyed_resource_cannot_be_provisioned_or_bound_again(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    adapter.dependents_error = ResourceError("aws_elasticache_destroy_delete_invalid_state")

    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_delete_invalid_state$"):
        destroy()

    assert NAME in server_module.store.load().resources
    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_in_progress$"):
        server_module.apply_resource(NAME, str(server_module.plan_apply_resource(NAME)["plan_id"]))
    account, network, secret_store = context()
    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_in_progress$"):
        apply_binding(
            adapter, server_module.store.root, account, network, valkey(), NAME, secret_store,
            "workload-secrets", DEPLOYMENT, ["cache"],
        )

    adapter.dependents_error = None
    assert destroy()["destroyed"] is True, "a repeat resumes and finishes"


def test_destroy_waits_for_an_asynchronously_deleting_user_group(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    adapter.dependents_pending = True

    first = destroy()

    assert first["phase"] == "waiting_for_user_group" and first["destroyed"] is False
    marker = server_module.store.root / "destroying-resources" / f"{NAME}.json"
    assert json.loads(marker.read_text())["phase"] == "waiting_for_user_group"
    inspected = server_module.inspect_resource(NAME)
    assert inspected["operation"] == "destroying"
    assert inspected["phase"] == "waiting_for_user_group"
    assert inspected["progress"] == {"phase": "waiting_for_user_group"}

    adapter.dependents_pending = False
    second = destroy()

    assert second["phase"] == "destroyed" and second["destroyed"] is True
    assert len(adapter.dependents_calls) == 2 and not marker.exists()


def test_a_fingerprint_that_is_not_the_planned_one_deletes_nothing(
    tmp_path, monkeypatch
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    account, network, _store = context()

    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_identity_changed$"):
        apply_destroy(
            adapter, server_module.store.root,
            account.model_copy(update={"destructive_role_arn": DESTROYER}), network, NAME,
            "0" * 16,
        )

    assert adapter.delete_calls == [] and adapter.dependents_calls == []


@pytest.mark.parametrize(
    ("live", "code"),
    [
        ({"identity": ARN + "-other"}, "aws_elasticache_destroy_identity_changed"),
        ({"status": "modifying"}, "aws_elasticache_destroy_invalid_state"),
        ({"status": "creating"}, "aws_elasticache_destroy_invalid_state"),
    ],
)
def test_a_group_that_is_not_the_planned_one_is_never_deleted(
    tmp_path, monkeypatch, instant, live, code
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    plan = server_module.plan_destroy_resource(NAME)
    adapter.settle_polls = 1000
    adapter.live = dataclasses.replace(adapter.live, **live)

    with pytest.raises(ResourceError, match=f"^{code}$"):
        server_module.apply_destroy_resource(NAME, str(plan["plan_id"]), CONFIRM)

    assert adapter.delete_calls == [] and adapter.dependents_calls == []
    assert not (server_module.store.root / "destroying-resources" / f"{NAME}.json").exists()


def test_a_replanned_group_identity_makes_the_old_plan_stale(tmp_path, monkeypatch) -> None:
    destroyable(tmp_path, monkeypatch)
    plan = server_module.plan_destroy_resource(NAME)
    observed_path = server_module.store.root / "observed-resources" / f"{NAME}.json"
    observed = json.loads(observed_path.read_text())
    observed["identity"] = ARN + "-recreated"
    observed_path.write_text(json.dumps(observed))

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.apply_destroy_resource(NAME, str(plan["plan_id"]), CONFIRM)


def test_forgetting_deletes_only_a_local_tombstone(tmp_path, monkeypatch) -> None:
    adapter = unreferenced(tmp_path, monkeypatch)
    cleanup = server_module.plan_cleanup_resource(NAME)
    server_module.apply_cleanup_resource(
        NAME, str(cleanup["plan_id"]), str(cleanup["confirmation"])
    )
    tombstone = server_module.store.root / "retained-resources" / f"{NAME}.json"
    assert tombstone.is_file()
    calls = (adapter.describe_calls, adapter.delete_calls)

    plan = server_module.plan_forget_resource(NAME)
    with pytest.raises(ValueError, match="confirmation must exactly equal"):
        server_module.apply_forget_resource(NAME, str(plan["plan_id"]), "forget")
    assert tombstone.is_file()
    result = server_module.apply_forget_resource(
        NAME, str(plan["plan_id"]), f"FORGET RETAINED RESOURCE {NAME}"
    )

    assert result["changed"] is True and result["resource"] == NAME
    assert not tombstone.exists()
    assert (adapter.describe_calls, adapter.delete_calls) == calls
    with pytest.raises(KeyError):
        server_module.plan_forget_resource(NAME)


def test_only_retained_infrastructure_can_be_forgotten(tmp_path, monkeypatch) -> None:
    provisioned(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="still registered"):
        server_module.plan_forget_resource(NAME)
    with pytest.raises(KeyError):
        server_module.plan_forget_resource("never-existed")


INSPECTOR = "arn:aws:iam::123456789012:role/gimme-inspect"
ARN_PREFIX = "arn:aws:elasticache:us-east-1:123456789012"
USER_GROUP = derive_user_group_id(GROUP_ID)


def destroyer(monkeypatch, *, with_role: bool = True):
    """Two stubbed ElastiCache clients: the inspection role reads, the destructive one deletes.
    `calls` is the order in which the two were used."""
    reader, reader_stub = stubbed("elasticache")
    killer, killer_stub = stubbed("elasticache")
    calls: list[str] = []
    for label, client in (("read", reader), ("delete", killer)):
        client.meta.events.register(
            "before-call.*.*",
            lambda model, label=label, **kwargs: calls.append(f"{label}:{model.name}"),
        )
    assumed: list[tuple[str, str]] = []
    adapter = BotoElastiCacheAdapter()

    def session(account, role_arn, purpose):
        assumed.append((role_arn, purpose))
        return StubbedSession({"elasticache": {INSPECTOR: reader, DESTROYER: killer}[role_arn]})

    monkeypatch.setattr(adapter, "_session", session)
    account, network, _store = context()
    if with_role:
        account = account.model_copy(update={"destructive_role_arn": DESTROYER})
    return adapter, account, network, (reader_stub, killer_stub), calls, assumed


def owned(resource: str = NAME) -> dict[str, object]:
    return {"TagList": [{"Key": "gimme:resource", "Value": resource}]}


def test_the_group_is_deleted_with_a_final_snapshot_by_the_destructive_role_only(
    monkeypatch,
) -> None:
    adapter, account, network, (reader_stub, killer_stub), _calls, assumed = destroyer(monkeypatch)
    killer_stub.add_response(
        "delete_replication_group", {},
        {"ReplicationGroupId": GROUP_ID, "FinalSnapshotIdentifier": f"{GROUP_ID}-final-abcd1234"},
    )

    with reader_stub, killer_stub:
        adapter.delete_group(account, network, GROUP_ID, f"{GROUP_ID}-final-abcd1234")

    assert assumed == [(DESTROYER, "elasticache-destroy")]
    killer_stub.assert_no_pending_responses()


def test_without_a_destructive_role_nothing_is_assumed_or_deleted(monkeypatch) -> None:
    adapter, account, network, _stubs, _calls, assumed = destroyer(monkeypatch, with_role=False)

    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_role_missing$"):
        adapter.delete_group(account, network, GROUP_ID, "snapshot")
    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_role_missing$"):
        adapter.delete_dependents(account, network, NAME, GROUP_ID, [])

    assert assumed == []


def test_the_final_snapshot_is_deleted_by_name_with_the_destructive_role(monkeypatch) -> None:
    adapter, account, network, (_reader_stub, killer_stub), _calls, assumed = destroyer(monkeypatch)
    snapshot = resources_valkey_module.final_snapshot_id(GROUP_ID, "a" * 16)
    killer_stub.add_response("delete_snapshot", {}, {"SnapshotName": snapshot})

    with killer_stub:
        assert adapter.delete_final_snapshot(account, network, snapshot) is True

    assert assumed == [(DESTROYER, "elasticache-destroy")]
    killer_stub.assert_no_pending_responses()


def test_an_already_absent_final_snapshot_is_a_completed_purge(monkeypatch) -> None:
    adapter, account, network, (_reader_stub, killer_stub), _calls, _assumed = destroyer(
        monkeypatch
    )
    snapshot = resources_valkey_module.final_snapshot_id(GROUP_ID, "a" * 16)
    killer_stub.add_client_error("delete_snapshot", "SnapshotNotFoundFault")

    with killer_stub:
        assert adapter.delete_final_snapshot(account, network, snapshot) is False


@pytest.mark.parametrize(
    ("wire", "code"),
    [
        ("SnapshotAlreadyExistsFault", "snapshot_exists"),
        ("InvalidReplicationGroupState", "invalid_state"),
        ("AccessDenied", "access_denied"),
        ("SomethingElse", "unavailable"),
    ],
)
def test_group_deletion_failures_are_bounded_codes(monkeypatch, wire, code) -> None:
    adapter, account, network, (reader_stub, killer_stub), _calls, _assumed = destroyer(monkeypatch)
    killer_stub.add_client_error("delete_replication_group", wire, "secret detail")

    with killer_stub, pytest.raises(ResourceError) as caught:
        adapter.delete_group(account, network, GROUP_ID, "snapshot")

    assert str(caught.value) == f"aws_elasticache_destroy_group_{code}"
    assert "secret" not in str(caught.value)


def dependents(monkeypatch, users: list[str]):
    adapter, account, network, stubs, calls, assumed = destroyer(monkeypatch)
    adapter_call = lambda: adapter.delete_dependents(  # noqa: E731
        account, network, NAME, GROUP_ID, users
    )
    return adapter_call, stubs, calls, assumed


def test_dependents_are_verified_then_deleted_in_dependency_order(monkeypatch) -> None:
    users = [f"{GROUP_ID}-default", f"{GROUP_ID}-admin", "gimme-u-abc"]
    run, (reader_stub, killer_stub), calls, assumed = dependents(monkeypatch, users)
    for kind, object_id in (
        ("usergroup", USER_GROUP), *(("user", user) for user in users),
        ("parametergroup", f"{GROUP_ID}-params"), ("subnetgroup", f"{GROUP_ID}-subnets"),
    ):
        reader_stub.add_response(
            "list_tags_for_resource", owned(),
            {"ResourceName": f"{ARN_PREFIX}:{kind}:{object_id}"},
        )
    killer_stub.add_response("delete_user_group", {}, {"UserGroupId": USER_GROUP})
    for user in users:
        killer_stub.add_response("delete_user", {}, {"UserId": user})
    killer_stub.add_response(
        "delete_cache_parameter_group", {}, {"CacheParameterGroupName": f"{GROUP_ID}-params"}
    )
    killer_stub.add_response(
        "delete_cache_subnet_group", {}, {"CacheSubnetGroupName": f"{GROUP_ID}-subnets"}
    )

    with reader_stub, killer_stub:
        run()

    reader_stub.assert_no_pending_responses()
    killer_stub.assert_no_pending_responses()
    assert {role for role, _ in assumed} == {INSPECTOR, DESTROYER}
    # every object is read before it is deleted, and nothing is deleted out of order
    assert calls[:2] == ["read:ListTagsForResource", "delete:DeleteUserGroup"]
    assert calls.index("delete:DeleteCacheSubnetGroup") == len(calls) - 1
    assert calls.count("delete:DeleteUser") == 3


def test_dependents_wait_until_the_deleted_user_group_releases_its_users(monkeypatch) -> None:
    user_id = f"{GROUP_ID}-default"
    run, (reader_stub, killer_stub), calls, _assumed = dependents(monkeypatch, [user_id])
    reader_stub.add_response("list_tags_for_resource", owned(), {
        "ResourceName": f"{ARN_PREFIX}:usergroup:{USER_GROUP}"})
    killer_stub.add_response("delete_user_group", {}, {"UserGroupId": USER_GROUP})
    reader_stub.add_response("list_tags_for_resource", owned(), {
        "ResourceName": f"{ARN_PREFIX}:user:{user_id}"})
    killer_stub.add_client_error("delete_user", "InvalidUserGroupState", "secret detail")

    with reader_stub, killer_stub:
        assert run() is False

    reader_stub.assert_no_pending_responses()
    killer_stub.assert_no_pending_responses()
    assert calls == [
        "read:ListTagsForResource", "delete:DeleteUserGroup",
        "read:ListTagsForResource", "delete:DeleteUser",
    ]


def test_an_object_owned_by_someone_else_stops_the_sequence_before_it_is_deleted(
    monkeypatch,
) -> None:
    run, (reader_stub, killer_stub), calls, _assumed = dependents(
        monkeypatch, [f"{GROUP_ID}-default"]
    )
    reader_stub.add_response("list_tags_for_resource", owned(), {
        "ResourceName": f"{ARN_PREFIX}:usergroup:{USER_GROUP}"})
    killer_stub.add_response("delete_user_group", {}, {"UserGroupId": USER_GROUP})
    reader_stub.add_response("list_tags_for_resource", owned("another-resource"), {
        "ResourceName": f"{ARN_PREFIX}:user:{GROUP_ID}-default"})

    with reader_stub, killer_stub, pytest.raises(
        ResourceError, match="^aws_elasticache_destroy_ownership_mismatch$"
    ):
        run()

    assert "delete:DeleteUser" not in calls and "delete:DeleteCacheSubnetGroup" not in calls


def test_an_untagged_object_is_not_ours_to_delete(monkeypatch) -> None:
    run, (reader_stub, killer_stub), calls, _assumed = dependents(monkeypatch, [])
    reader_stub.add_response("list_tags_for_resource", {"TagList": []}, {
        "ResourceName": f"{ARN_PREFIX}:usergroup:{USER_GROUP}"})

    with reader_stub, killer_stub, pytest.raises(
        ResourceError, match="^aws_elasticache_destroy_ownership_mismatch$"
    ):
        run()

    assert calls == ["read:ListTagsForResource"]


def test_objects_that_are_already_gone_count_as_done(monkeypatch) -> None:
    run, (reader_stub, killer_stub), calls, _assumed = dependents(monkeypatch, ["gimme-u-abc"])
    reader_stub.add_client_error("list_tags_for_resource", "UserGroupNotFound")
    reader_stub.add_client_error("list_tags_for_resource", "UserNotFound")
    reader_stub.add_response("list_tags_for_resource", owned(), {
        "ResourceName": f"{ARN_PREFIX}:parametergroup:{GROUP_ID}-params"})
    killer_stub.add_client_error("delete_cache_parameter_group", "CacheParameterGroupNotFound")
    reader_stub.add_client_error("list_tags_for_resource", "CacheSubnetGroupNotFoundFault")

    with reader_stub, killer_stub:
        run()

    assert not any(call.startswith("delete:DeleteUser") for call in calls)
    reader_stub.assert_no_pending_responses()
    killer_stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    ("wire", "code"),
    [
        ("InvalidUserGroupState", "aws_elasticache_destroy_delete_invalid_state"),
        ("AccessDenied", "aws_elasticache_destroy_delete_access_denied"),
    ],
)
def test_a_deletion_that_cannot_proceed_fails_with_a_bounded_code(
    monkeypatch, wire, code
) -> None:
    run, (reader_stub, killer_stub), _calls, _assumed = dependents(monkeypatch, [])
    reader_stub.add_response("list_tags_for_resource", owned(), {
        "ResourceName": f"{ARN_PREFIX}:usergroup:{USER_GROUP}"})
    killer_stub.add_client_error("delete_user_group", wire, "secret detail")

    with reader_stub, killer_stub, pytest.raises(ResourceError) as caught:
        run()

    assert str(caught.value) == code


def test_a_verification_that_cannot_be_read_stops_before_any_deletion(monkeypatch) -> None:
    run, (reader_stub, killer_stub), calls, _assumed = dependents(monkeypatch, [])
    reader_stub.add_client_error("list_tags_for_resource", "AccessDenied")

    with reader_stub, killer_stub, pytest.raises(
        ResourceError, match="^aws_elasticache_destroy_verify_access_denied$"
    ):
        run()

    assert not any(call.startswith("delete:") for call in calls)


def test_retaining_a_half_destroyed_resource_abandons_the_destruction(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    adapter.dependents_error = ResourceError("aws_elasticache_destroy_ownership_mismatch")
    with pytest.raises(ResourceError):
        destroy()
    marker = server_module.store.root / "destroying-resources" / f"{NAME}.json"
    assert marker.is_file()

    cleanup = server_module.plan_cleanup_resource(NAME)
    server_module.apply_cleanup_resource(
        NAME, str(cleanup["plan_id"]), str(cleanup["confirmation"])
    )

    assert not marker.exists() and NAME not in server_module.store.load().resources


def test_a_destruction_reloads_desired_state_before_forgetting_the_resource(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    original = adapter.delete_dependents

    def edit_meanwhile(*args, **kwargs):
        original(*args, **kwargs)
        document = server_module.store.load().model_dump(mode="json")
        document["deployments"][DEPLOYMENT]["variables"]["EDITED_MEANWHILE"] = "yes"
        server_module.store.save(ControlState.model_validate(document))

    adapter.delete_dependents = edit_meanwhile  # type: ignore[method-assign]

    destroy()

    state = server_module.store.load()
    assert NAME not in state.resources
    assert state.deployments[DEPLOYMENT].variables["EDITED_MEANWHILE"] == "yes"


def test_a_corrupt_tombstone_can_still_be_forgotten(tmp_path, monkeypatch) -> None:
    unreferenced(tmp_path, monkeypatch)
    cleanup = server_module.plan_cleanup_resource(NAME)
    server_module.apply_cleanup_resource(
        NAME, str(cleanup["plan_id"]), str(cleanup["confirmation"])
    )
    tombstone = server_module.store.root / "retained-resources" / f"{NAME}.json"
    tombstone.write_text("{not json")

    plan = server_module.plan_forget_resource(NAME)
    server_module.apply_forget_resource(
        NAME, str(plan["plan_id"]), f"FORGET RETAINED RESOURCE {NAME}"
    )

    assert not tombstone.exists()


# --- recovery: restore, recreate empty, rotate ------------------------------------------

SNAPSHOT = "gimme-final-0001"


def marker_file(kind: str) -> Path:
    return server_module.store.root / f"{kind}-resources" / f"{NAME}.json"


def a_snapshot(**updates) -> SnapshotInfo:
    values: dict[str, object] = dict(
        name=SNAPSHOT, source="manual", status="available",
        created="2026-09-01T00:00:00+00:00", engine_version="9.0", shards=1,
    )
    values.update(updates)
    return SnapshotInfo(**values)  # type: ignore[arg-type]


def lost(tmp_path, monkeypatch, instant) -> tuple[FakeValkey, list[dict[str, object]]]:
    """A bound, ready Resource whose replication group has since vanished from AWS."""
    adapter = bound_and_ready(tmp_path, monkeypatch)
    adapter.live = None
    adapter.snapshots = [a_snapshot()]
    monkeypatch.setattr(valkey_recovery_module, "POLL_INTERVAL_SECONDS", 0)
    return adapter, capture_runs(monkeypatch)


def restore(snapshot: str = SNAPSHOT) -> dict[str, object]:
    plan = server_module.plan_restore_resource(NAME, snapshot)
    return server_module.apply_restore_resource(NAME, snapshot, str(plan["plan_id"]))


def recreate_empty(confirmation: str = f"RECREATE EMPTY RESOURCE {NAME}") -> dict[str, object]:
    plan = server_module.plan_recreate_empty_resource(NAME)
    return server_module.apply_recreate_empty_resource(NAME, str(plan["plan_id"]), confirmation)


def tasks(calls: list[dict[str, object]]) -> list[object]:
    return [call["task"] for call in calls]


def test_a_vanished_group_is_never_silently_recreated_empty(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, _calls = lost(tmp_path, monkeypatch, instant)
    plan = server_module.plan_apply_resource(NAME)

    with pytest.raises(ResourceError, match="^aws_elasticache_group_missing_replace_explicitly$"):
        server_module.apply_resource(NAME, str(plan["plan_id"]))

    assert adapter.create_calls == 0, "nothing recreated the vanished group"


def test_a_restore_plan_is_local_and_the_same_before_during_and_after(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, _calls = lost(tmp_path, monkeypatch, instant)
    adapter.settle_polls = 1000
    monkeypatch.setattr(valkey_recovery_module, "POLL_BUDGET_SECONDS", 0)
    describes = adapter.describe_calls
    plan = server_module.plan_restore_resource(NAME, SNAPSHOT)

    assert adapter.describe_calls == describes, "planning reads nothing from AWS"
    assert plan["kind"] == "resource_restore" and plan["snapshot"] == SNAPSHOT
    assert plan["deployments"] == [DEPLOYMENT] and "confirmation" not in plan
    assert "password" not in json.dumps(plan).lower()

    assert server_module.apply_restore_resource(NAME, SNAPSHOT, str(plan["plan_id"]))["phase"] == (
        "restoring"
    )
    assert server_module.plan_restore_resource(NAME, SNAPSHOT)["plan_id"] == plan["plan_id"]


def test_a_restore_recreates_from_the_snapshot_keeping_every_credential_then_verifies(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, calls = lost(tmp_path, monkeypatch, instant)
    before = load_observed(server_module.store.root, NAME)
    assert before is not None
    allocation = cast(dict[str, dict[str, object]], before["allocations"])[DEPLOYMENT]

    result = restore()

    assert adapter.create_args[-1] == {
        "snapshot_name": SNAPSHOT,
        "restore_users": {DEPLOYMENT: (allocation["user_id"], 1)},
    }
    assert result["restored"] is True and result["phase"] == "ready"
    assert result["verified"] == [DEPLOYMENT] and result["snapshot"] == SNAPSHOT
    # the Deployment is pointed at the new group, proven against the live release, and restarted
    assert tasks(calls)[-4:] == [
        "gimme:provision:app", "gimme:recovery:schedule-reconcile",
        "gimme:probe:valkey:current", "gimme:restart:workers",
    ]
    assert not marker_file("restoring").exists()
    after = load_observed(server_module.store.root, NAME)
    assert after is not None and after["phase"] == "ready"
    assert after["allocations"] == before["allocations"], "no credential was rotated"
    assert adapter.binding_calls == [(DEPLOYMENT, False)], "restore never re-binds"
    assert "password" not in json.dumps(result).lower()


@pytest.mark.parametrize(
    ("snapshot", "code"),
    [
        (a_snapshot(name="other"), "aws_elasticache_restore_snapshot_missing"),
        (a_snapshot(status="creating"), "aws_elasticache_restore_snapshot_unavailable"),
        (a_snapshot(engine_version="9.1"), "aws_elasticache_restore_engine_older"),
    ],
)
def test_a_restore_refuses_an_unusable_snapshot_before_creating_anything(
    tmp_path, monkeypatch, instant, snapshot, code
) -> None:
    adapter, _calls = lost(tmp_path, monkeypatch, instant)
    adapter.snapshots = [snapshot]

    with pytest.raises(ResourceError, match=f"^{code}$"):
        restore()

    assert adapter.create_calls == 0 and not marker_file("restoring").exists()


def test_a_restore_never_replaces_a_group_that_exists(tmp_path, monkeypatch, instant) -> None:
    adapter = bound_and_ready(tmp_path, monkeypatch)
    adapter.snapshots = [a_snapshot()]

    with pytest.raises(ResourceError, match="^aws_elasticache_restore_group_exists$"):
        restore()
    with pytest.raises(ResourceError, match="^aws_elasticache_restore_group_exists$"):
        recreate_empty()

    assert adapter.create_calls == 0


def test_a_restoring_resource_refuses_every_other_operation_and_says_so(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, calls = lost(tmp_path, monkeypatch, instant)
    adapter.settle_polls = 1000
    monkeypatch.setattr(valkey_recovery_module, "POLL_BUDGET_SECONDS", 0)

    first = restore()

    assert first["restored"] is False and first["phase"] == "restoring"
    assert first["pending"] == [DEPLOYMENT] and tasks(calls) == [], "nothing is verified early"
    inspected = server_module.inspect_resource(NAME)
    assert inspected["phase"] == "restoring" and inspected["operation"] == "restoring"
    with pytest.raises(ResourceError, match="^aws_elasticache_restore_in_progress$"):
        server_module.apply_resource(
            NAME, str(server_module.plan_apply_resource(NAME)["plan_id"])
        )
    with pytest.raises(ValueError, match="managed resource is not ready"):
        bind()
    with pytest.raises(ResourceError, match="^aws_elasticache_restore_in_progress$"):
        resources_valkey_module.apply_binding(
            adapter, server_module.store.root, *destroy_context(), DEPLOYMENT, ["cache"]
        )
    assert server_module.plan_deployment_resources(DEPLOYMENT)["readiness_issues"] == [
        "valkey_resource_not_ready"
    ], "a Deployment is not handed a half-restored Resource"


def test_a_pending_restore_resumes_without_creating_again(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, calls = lost(tmp_path, monkeypatch, instant)
    adapter.settle_polls = 1000
    monkeypatch.setattr(valkey_recovery_module, "POLL_BUDGET_SECONDS", 0)
    restore()

    adapter.settle_polls = 1
    monkeypatch.setattr(valkey_recovery_module, "POLL_BUDGET_SECONDS", 30)
    second = restore()

    assert second["restored"] is True and adapter.create_calls == 1
    assert tasks(calls)[-1] == "gimme:restart:workers"


def test_a_failed_verification_keeps_the_resource_restoring_and_a_repeat_finishes(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, calls = lost(tmp_path, monkeypatch, instant)
    real = server_module.runner.run

    def failing(task, *args, **kwargs):
        if task == "gimme:probe:valkey:current":
            raise RuntimeError("Valkey activation probe failed: authentication")
        return real(task, *args, **kwargs)

    monkeypatch.setattr(server_module.runner, "run", failing)

    with pytest.raises(ResourceError, match="^aws_elasticache_restore_verification_failed$"):
        restore()

    observed = load_observed(server_module.store.root, NAME)
    assert observed is not None and observed["phase"] == "restoring"
    assert marker_file("restoring").is_file()

    monkeypatch.setattr(server_module.runner, "run", real)
    assert restore()["restored"] is True
    assert adapter.create_calls == 1, "the repeat verified; it did not create again"
    assert not marker_file("restoring").exists()


def test_deployments_already_verified_are_not_verified_again_on_resume(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, _calls = lost(tmp_path, monkeypatch, instant)
    root = server_module.store.root
    observed = load_observed(root, NAME)
    assert observed is not None
    allocations = cast(dict[str, dict[str, object]], observed["allocations"])
    allocations["aaa-first"] = {**allocations[DEPLOYMENT], "user_id": "gimme-u-other"}
    (root / "observed-resources" / f"{NAME}.json").write_text(json.dumps(observed))
    state = server_module.store.load()
    seen: list[str] = []

    def verify(deployment: str) -> None:
        seen.append(deployment)
        if deployment == DEPLOYMENT and seen.count(DEPLOYMENT) == 1:
            raise RuntimeError("first attempt fails")

    def call() -> dict[str, object]:
        network = state.aws_networks["primary"]
        return valkey_recovery_module.apply_restore(
            adapter, root, state.provider_accounts[network.provider_account], network,
            cast(AWSElastiCacheValkeyResource, state.resources[NAME]),
            NAME, cast(AWSSecretsManagerStore, state.secret_stores["workload-secrets"]),
            "workload-secrets", SNAPSHOT, verify,
        )

    with pytest.raises(ResourceError, match="verification_failed"):
        call()
    assert call()["restored"] is True

    assert seen == ["aaa-first", DEPLOYMENT, DEPLOYMENT]


def test_a_resumed_restore_must_name_the_snapshot_it_started_with(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, _calls = lost(tmp_path, monkeypatch, instant)
    adapter.settle_polls = 1000
    monkeypatch.setattr(valkey_recovery_module, "POLL_BUDGET_SECONDS", 0)
    restore()
    adapter.snapshots.append(a_snapshot(name="another"))

    with pytest.raises(ResourceError, match="^aws_elasticache_restore_snapshot_mismatch$"):
        restore("another")


def test_a_group_that_fails_to_create_ends_the_restore_and_frees_the_resource(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, _calls = lost(tmp_path, monkeypatch, instant)
    real_create = adapter.create_group

    def failing_group(*args, **kwargs):
        real_create(*args, **kwargs)
        adapter.live = dataclasses.replace(cast(GroupObservation, adapter.live),
                                           status="create-failed")
        return adapter.live

    monkeypatch.setattr(adapter, "create_group", failing_group)

    with pytest.raises(ResourceError, match="^aws_elasticache_restore_create_failed$"):
        restore()

    assert not marker_file("restoring").exists()
    observed = load_observed(server_module.store.root, NAME)
    assert observed is not None and observed["phase"] == "failed"


def test_recreating_empty_needs_the_exact_confirmation_and_creates_no_snapshot_group(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, calls = lost(tmp_path, monkeypatch, instant)
    plan = server_module.plan_recreate_empty_resource(NAME)
    assert plan["kind"] == "resource_recreate_empty" and plan["irreversible"] is True

    with pytest.raises(ValueError, match="confirmation must exactly equal"):
        recreate_empty("yes")
    assert adapter.create_calls == 0

    result = recreate_empty()

    assert adapter.create_args[-1]["snapshot_name"] is None
    assert adapter.create_args[-1]["restore_users"] and result["restored"] is True
    assert tasks(calls)[-1] == "gimme:restart:workers"


def test_recreating_empty_needs_a_resource_that_was_once_observed(
    tmp_path, monkeypatch
) -> None:
    use_bound(tmp_path, monkeypatch)

    with pytest.raises(ResourceError, match="^aws_elasticache_recreate_not_needed$"):
        server_module.plan_recreate_empty_resource(NAME)


def test_a_restore_after_destroy_recreates_from_the_final_snapshot_with_fresh_bindings(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    adapter.snapshots = [a_snapshot()]
    monkeypatch.setattr(valkey_recovery_module, "POLL_INTERVAL_SECONDS", 0)
    destroy()
    adapter.live = None
    document = json.loads(EXAMPLE.read_text())
    state = server_module.store.load()
    server_module.store.save(state.model_copy(update={"resources": {
        **state.resources,
        NAME: AWSElastiCacheValkeyResource.model_validate(document["resources"][NAME]),
    }}))

    result = restore()

    assert result["restored"] is True and result["verified"] == []
    assert adapter.create_args[-1] == {"snapshot_name": SNAPSHOT, "restore_users": {}}


def test_snapshots_are_listed_by_name_and_status_only(tmp_path, monkeypatch, instant) -> None:
    adapter, _calls = lost(tmp_path, monkeypatch, instant)

    result = server_module.list_resource_snapshots(NAME)

    assert result == {"resource": NAME, "snapshots": [{
        "name": SNAPSHOT, "source": "manual", "status": "available",
        "created": "2026-09-01T00:00:00+00:00", "engine_version": "9.0", "shards": 1,
    }]}


def test_a_retained_resource_clears_a_half_finished_restore(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, _calls = lost(tmp_path, monkeypatch, instant)
    adapter.settle_polls = 1000
    monkeypatch.setattr(valkey_recovery_module, "POLL_BUDGET_SECONDS", 0)
    restore()
    assert marker_file("restoring").is_file()

    resources_valkey_module.retain_group(server_module.store.root, NAME, "primary")

    assert not marker_file("restoring").exists()


# rotation


def destroy_context():
    state = server_module.store.load()
    resource = cast(AWSElastiCacheValkeyResource, state.resources[NAME])
    network = state.aws_networks[resource.aws_network]
    return (
        state.provider_accounts[network.provider_account], network, resource, NAME,
        cast(AWSSecretsManagerStore, state.secret_stores[resource.workload_secret_store]),
        resource.workload_secret_store,
    )


def rotatable(tmp_path, monkeypatch) -> tuple[FakeValkey, list[dict[str, object]]]:
    adapter = bound_and_ready(tmp_path, monkeypatch)
    document = server_module.store.load().model_dump(mode="json")
    document["provider_accounts"]["main"]["destructive_role_arn"] = DESTROYER
    server_module.store.save(ControlState.model_validate(document))
    return adapter, capture_runs(monkeypatch)


def rotate() -> dict[str, object]:
    plan = server_module.plan_rotate_resource_credential(NAME, DEPLOYMENT)
    return server_module.apply_rotate_resource_credential(NAME, DEPLOYMENT, str(plan["plan_id"]))


def allocation() -> dict[str, object]:
    observed = load_observed(server_module.store.root, NAME)
    assert observed is not None
    return cast(dict[str, dict[str, object]], observed["allocations"])[DEPLOYMENT]


def test_rotation_needs_the_destructive_role_and_a_bound_deployment(
    tmp_path, monkeypatch
) -> None:
    bound_and_ready(tmp_path, monkeypatch)

    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_role_missing$"):
        server_module.plan_rotate_resource_credential(NAME, DEPLOYMENT)

    rotatable(tmp_path, monkeypatch)
    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_binding_missing$"):
        server_module.plan_rotate_resource_credential(NAME, "nobody")
    plan = server_module.plan_rotate_resource_credential(NAME, DEPLOYMENT)
    assert plan["kind"] == "resource_credential_rotate" and plan["deployment"] == DEPLOYMENT
    assert "password" not in json.dumps(plan).lower()


def test_a_rotation_swaps_users_after_a_probed_switch_and_deletes_the_old_one_last(
    tmp_path, monkeypatch
) -> None:
    adapter, calls = rotatable(tmp_path, monkeypatch)
    old = allocation()
    old_user = str(old["user_id"])

    result = rotate()

    new = allocation()
    assert result["rotated"] is True and result["generation"] == 2
    assert new["generation"] == 2 and new["user_id"] == derive_binding_user_id(
        GROUP_ID, DEPLOYMENT, 2
    ) and new["user_id"] != old_user
    assert adapter.credentials[DEPLOYMENT] == [
        binding_username(DEPLOYMENT), binding_username(DEPLOYMENT, 2)
    ]
    assert adapter.removed_users == [old_user] and old_user not in adapter.users
    assert tasks(calls)[-4:] == [
        "gimme:provision:app", "gimme:recovery:schedule-reconcile",
        "gimme:probe:valkey:current", "gimme:restart:workers",
    ]
    assert not marker_file("rotating").exists()
    text = json.dumps(result).lower()
    assert "password" not in text and old_user not in text


def test_a_failed_switch_restores_the_previous_credential_and_deletes_the_candidate(
    tmp_path, monkeypatch
) -> None:
    adapter, _calls = rotatable(tmp_path, monkeypatch)
    old = allocation()
    real = server_module.runner.run
    probes: list[str] = []

    def failing(task, *args, **kwargs):
        if task == "gimme:probe:valkey:current":
            probes.append(task)
            if len(probes) == 1:
                raise RuntimeError("authentication failed")
        return real(task, *args, **kwargs)

    monkeypatch.setattr(server_module.runner, "run", failing)

    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_switch_failed$"):
        rotate()

    candidate = derive_binding_user_id(GROUP_ID, DEPLOYMENT, 2)
    assert adapter.removed_users == [candidate]
    assert adapter.credentials[DEPLOYMENT][-1] == binding_username(DEPLOYMENT)
    restored = allocation()
    assert restored["user_id"] == old["user_id"] and "generation" not in restored
    assert restored["secret_version_id"] != old["secret_version_id"], (
        "the restored credential is a new secret version"
    )
    assert len(probes) == 2, "the restored credential is proven against the release again"
    assert not marker_file("rotating").exists()


def test_a_rollback_that_cannot_finish_keeps_the_marker_and_a_repeat_completes_it(
    tmp_path, monkeypatch
) -> None:
    adapter, _calls = rotatable(tmp_path, monkeypatch)
    real = server_module.runner.run
    monkeypatch.setattr(server_module.runner, "run", lambda task, *a, **k: (
        (_ for _ in ()).throw(RuntimeError("down")) if task == "gimme:probe:valkey:current"
        else real(task, *a, **k)
    ))
    adapter.remove_error = ResourceError("aws_elasticache_rotate_delete_access_denied")

    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_rollback_failed$"):
        rotate()

    assert marker_file("rotating").is_file()
    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_in_progress$"):
        server_module.apply_resource(
            NAME, str(server_module.plan_apply_resource(NAME)["plan_id"])
        )

    adapter.remove_error = None
    monkeypatch.setattr(server_module.runner, "run", real)
    repeat = rotate()

    assert repeat["rotated"] is False and repeat["resumed"] is True
    assert not marker_file("rotating").exists()
    assert derive_binding_user_id(GROUP_ID, DEPLOYMENT, 2) not in adapter.users
    assert adapter.credentials[DEPLOYMENT][-1] == binding_username(DEPLOYMENT)


def test_a_failed_cleanup_keeps_the_new_credential_and_a_repeat_deletes_the_old_user(
    tmp_path, monkeypatch
) -> None:
    adapter, _calls = rotatable(tmp_path, monkeypatch)
    old_user = str(allocation()["user_id"])
    adapter.remove_error = ResourceError("aws_elasticache_rotate_delete_unavailable")

    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_delete_unavailable$"):
        rotate()

    assert allocation()["generation"] == 2, "the switch had succeeded, so it is recorded"
    assert json.loads(marker_file("rotating").read_text())["phase"] == "cleanup"
    adapter.remove_error = None
    adapter.removed_users.clear()

    repeat = rotate()

    assert repeat["rotated"] is True and repeat["resumed"] is True
    assert adapter.removed_users == [old_user] and not marker_file("rotating").exists()


def test_a_leftover_candidate_user_is_neither_adopted_nor_deleted(
    tmp_path, monkeypatch
) -> None:
    adapter, _calls = rotatable(tmp_path, monkeypatch)
    adapter.begin_error = ResourceError("aws_elasticache_rotate_candidate_exists")

    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_candidate_exists$"):
        rotate()

    assert adapter.removed_users == [] and not marker_file("rotating").exists()


def test_another_deployments_unfinished_rotation_blocks_this_one(
    tmp_path, monkeypatch
) -> None:
    _adapter, _calls = rotatable(tmp_path, monkeypatch)
    resources_valkey_module.write_marker(
        server_module.store.root, "rotating", NAME,
        {"schema_version": 1, "resource": NAME, "deployment": "someone-else",
         "phase": "switching"},
    )

    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_in_progress$"):
        rotate()


def test_rotation_needs_a_ready_group_and_starts_nothing_otherwise(
    tmp_path, monkeypatch
) -> None:
    adapter, _calls = rotatable(tmp_path, monkeypatch)
    adapter.settle_polls = 1000
    adapter.live = dataclasses.replace(cast(GroupObservation, adapter.live), status="modifying")

    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_resource_not_ready$"):
        rotate()

    assert not marker_file("rotating").exists() and adapter.removed_users == []


def test_a_rotated_credential_survives_a_rebind_and_is_what_a_restore_recreates(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, _calls = rotatable(tmp_path, monkeypatch)
    rotate()
    monkeypatch.setattr(valkey_recovery_module, "POLL_INTERVAL_SECONDS", 0)

    bind()
    assert adapter.binding_calls[-1] == (DEPLOYMENT, True), "a rebind keeps the credential"
    assert allocation()["generation"] == 2

    adapter.live = None
    adapter.snapshots = [a_snapshot()]
    restore()

    assert adapter.create_args[-1]["restore_users"] == {
        DEPLOYMENT: (derive_binding_user_id(GROUP_ID, DEPLOYMENT, 2), 2)
    }


def test_the_recovery_tools_are_journaled_and_never_run_on_a_stale_plan(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, _calls = lost(tmp_path, monkeypatch, instant)

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.apply_restore_resource(NAME, SNAPSHOT, "plan_" + "0" * 20)
    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.apply_restore_resource(
            NAME, "another-snapshot",
            str(server_module.plan_restore_resource(NAME, SNAPSHOT)["plan_id"]),
        )

    assert adapter.create_calls == 0


# --- recovery adapter calls -------------------------------------------------------------

USER_ID = derive_binding_user_id(GROUP_ID, "example-local", 1)
SECRET_ARN = "arn:aws:secretsmanager:eu-central-1:123456789012:secret:example"
SECRET_ID = f"gimme/workload/{NAME}/example-local"


def credential(username: str = "gimme-example-local", password: str = "old-" + "q" * 40):
    return {"SecretString": json.dumps({"username": username, "password": password})}


def test_a_snapshot_restore_sends_the_snapshot_omits_the_shard_count_and_never_writes_a_secret(
    monkeypatch,
) -> None:
    client, stub = stubbed("elasticache")
    secrets_client, secrets_stub = stubbed("secretsmanager")
    subnet, params = f"{GROUP_ID}-subnets", f"{GROUP_ID}-params"
    users = derive_user_group_id(GROUP_ID)
    default_id, admin_id = f"{GROUP_ID}-default", f"{GROUP_ID}-admin"
    stub.add_response(
        "create_cache_subnet_group", {},
        {"CacheSubnetGroupName": subnet,
         "CacheSubnetGroupDescription": f"Gimme-managed subnet group for {NAME}",
         "SubnetIds": ["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"], "Tags": TAG},
    )
    stub.add_response(
        "create_cache_parameter_group", {},
        {"CacheParameterGroupName": params, "CacheParameterGroupFamily": "valkey9",
         "Description": f"Gimme-managed parameter group for {NAME}", "Tags": TAG},
    )
    stub.add_response(
        "modify_cache_parameter_group", {"CacheParameterGroupName": params},
        {"CacheParameterGroupName": params, "ParameterNameValues": [
            {"ParameterName": "cluster-enabled", "ParameterValue": "yes"},
            {"ParameterName": "maxmemory-policy", "ParameterValue": "noeviction"},
        ]},
    )
    stub.add_response(
        "create_user", {},
        {"UserId": default_id, "UserName": "default", "Engine": "valkey",
         "AccessString": "off ~* -@all", "Passwords": [ANY], "Tags": TAG},
    )
    stub.add_response("describe_users", {"Users": [{"UserId": admin_id}]}, {"UserId": admin_id})
    stub.add_client_error("describe_users", "UserNotFound", expected_params={"UserId": USER_ID})
    secrets_stub.add_response(
        "get_secret_value", credential(),
        {"SecretId": SECRET_ID, "VersionStage": "AWSCURRENT"},
    )
    stub.add_response(
        "create_user", {},
        {"UserId": USER_ID, "UserName": "gimme-example-local", "Engine": "valkey",
         "AccessString": laravel_access_string("example-local"),
         "Passwords": ["old-" + "q" * 40],
         "Tags": [*TAG, {"Key": "gimme:deployment", "Value": "example-local"}]},
    )
    stub.add_response(
        "create_user_group", {},
        {"UserGroupId": users, "Engine": "valkey", "UserIds": [default_id, admin_id],
         "Tags": TAG},
    )
    stub.add_response(
        "describe_user_groups", {"UserGroups": [{"UserIds": [default_id, admin_id]}]},
        {"UserGroupId": users},
    )
    stub.add_response(
        "modify_user_group", {}, {"UserGroupId": users, "UserIdsToAdd": [USER_ID]},
    )
    stub.add_response(
        "create_replication_group", {},
        {
            "ReplicationGroupId": GROUP_ID,
            "ReplicationGroupDescription": f"Gimme-managed Valkey for {NAME}",
            "Engine": "valkey", "EngineVersion": "9.0", "CacheNodeType": "cache.m7g.large",
            "CacheParameterGroupName": params, "CacheSubnetGroupName": subnet,
            "SecurityGroupIds": ["sg-0123456789abcdef2"], "ClusterMode": "enabled",
            "ReplicasPerNodeGroup": 1, "SnapshotName": SNAPSHOT,
            "AutomaticFailoverEnabled": True,
            "MultiAZEnabled": True, "TransitEncryptionEnabled": True,
            "TransitEncryptionMode": "required", "AtRestEncryptionEnabled": True,
            "UserGroupIds": [users], "Durability": "sync", "AutoMinorVersionUpgrade": False,
            "SnapshotRetentionLimit": 7, "SnapshotWindow": "03:00-04:00",
            "PreferredMaintenanceWindow": "sun:05:00-sun:06:00", "Port": 6379, "Tags": TAG,
        },
    )
    adapter = adapter_with(
        monkeypatch, ("elasticache", (client, stub)),
        ("secretsmanager", (secrets_client, secrets_stub)),
    )
    account, network, store = context()

    with stub, secrets_stub:
        acknowledged = adapter.create_group(
            account, network, valkey(), NAME, GROUP_ID, store, "workload-secrets",
            snapshot_name=SNAPSHOT, restore_users={"example-local": (USER_ID, 1)},
        )

    assert acknowledged is None
    stub.assert_no_pending_responses()
    secrets_stub.assert_no_pending_responses()  # only the one read; a restore writes nothing


def test_a_restored_user_whose_stored_username_is_another_generation_is_refused(
    monkeypatch,
) -> None:
    client, stub = stubbed("elasticache")
    secrets_client, secrets_stub = stubbed("secretsmanager")
    stub.add_response(
        "create_user", {}, {"UserId": f"{GROUP_ID}-default", "UserName": "default",
                            "Engine": "valkey", "AccessString": "off ~* -@all",
                            "Passwords": [ANY], "Tags": TAG},
    )
    stub.add_response(
        "describe_users", {"Users": [{"UserId": f"{GROUP_ID}-admin"}]},
        {"UserId": f"{GROUP_ID}-admin"},
    )
    stub.add_client_error("describe_users", "UserNotFound", expected_params={"UserId": USER_ID})
    secrets_stub.add_response(
        "get_secret_value", credential("gimme-example-local-g2"),
        {"SecretId": SECRET_ID, "VersionStage": "AWSCURRENT"},
    )
    adapter = adapter_with(
        monkeypatch, ("elasticache", (client, stub)),
        ("secretsmanager", (secrets_client, secrets_stub)),
    )
    account, _network, store = context()

    with stub, secrets_stub, pytest.raises(ResourceError) as raised:
        adapter.restore_authentication(
            account, client, store, NAME, GROUP_ID, {"example-local": (USER_ID, 1)}
        )

    assert str(raised.value) == "aws_elasticache_restore_credential_mismatch"
    assert "q" * 10 not in repr(raised.value)


def snapshot_page(names: list[str], marker: str | None = None, **updates):
    return {
        "Snapshots": [{
            "SnapshotName": name, "ReplicationGroupId": GROUP_ID, "SnapshotStatus": "available",
            "SnapshotSource": "manual", "EngineVersion": "9.0", "NumNodeGroups": 1,
            "NodeSnapshots": [{"SnapshotCreateTime": datetime.datetime(
                2026, 9, int(name[-2:]), tzinfo=datetime.timezone.utc)}],
            **updates,
        } for name in names],
        **({"Marker": marker} if marker else {}),
    }


def test_snapshots_are_paged_filtered_to_this_group_and_listed_newest_first(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_response(
        "describe_snapshots", snapshot_page(["snap-01", "snap-03"], marker="m2"),
        {"ReplicationGroupId": GROUP_ID, "MaxRecords": 50},
    )
    other = snapshot_page(["snap-09"], ReplicationGroupId="somebody-else")
    other["Snapshots"] += snapshot_page(["snap-02"])["Snapshots"]
    stub.add_response(
        "describe_snapshots", other,
        {"ReplicationGroupId": GROUP_ID, "MaxRecords": 50, "Marker": "m2"},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        found = adapter.list_snapshots(account, network, GROUP_ID)

    assert [item.name for item in found] == ["snap-03", "snap-02", "snap-01"]
    assert found[0].created == "2026-09-03T00:00:00+00:00" and found[0].shards == 1


def test_snapshot_paging_is_bounded(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    for index in range(4):
        stub.add_response(
            "describe_snapshots", snapshot_page([f"snap-0{index + 1}"], marker="more"),
            {"ReplicationGroupId": GROUP_ID, "MaxRecords": 50,
             **({"Marker": "more"} if index else {})},
        )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        found = adapter.list_snapshots(account, network, GROUP_ID)

    assert len(found) == 4
    stub.assert_no_pending_responses()


def test_snapshot_listing_failures_are_bounded_codes(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_client_error("describe_snapshots", "AccessDenied", "arn:aws:iam::1:role/secret")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub, pytest.raises(ResourceError) as raised:
        adapter.list_snapshots(account, network, GROUP_ID)

    assert str(raised.value) == "aws_elasticache_snapshots_access_denied"


CANDIDATE = derive_binding_user_id(GROUP_ID, "example-local", 2)


def test_a_rotation_creates_the_user_and_joins_the_group_before_the_secret_points_at_it(
    monkeypatch,
) -> None:
    client, stub = stubbed("elasticache")
    secrets_client, secrets_stub = stubbed("secretsmanager")
    calls: list[str] = []
    for label, service in (("elasticache", client), ("secrets", secrets_client)):
        service.meta.events.register(
            "before-call.*.*", lambda model, label=label, **kw: calls.append(model.name)
        )
    stub.add_client_error("describe_users", "UserNotFound", expected_params={"UserId": CANDIDATE})
    stub.add_response(
        "create_user", {},
        {"UserId": CANDIDATE, "UserName": "gimme-example-local-g2", "Engine": "valkey",
         "AccessString": laravel_access_string("example-local"), "Passwords": [PASSWORD],
         "Tags": [*TAG, {"Key": "gimme:deployment", "Value": "example-local"}]},
    )
    stub.add_response(
        "modify_user_group", {},
        {"UserGroupId": derive_user_group_id(GROUP_ID), "UserIdsToAdd": [CANDIDATE]},
    )
    secrets_stub.add_client_error(
        "create_secret", "ResourceExistsException",
        expected_params={"Name": SECRET_ID, "SecretString": ANY, "Tags": [
            {"Key": "gimme:deployment", "Value": "example-local"},
            {"Key": "gimme:resource", "Value": NAME},
            {"Key": "gimme:secret-store", "Value": "workload-secrets"}]},
    )
    secrets_stub.add_response(
        "put_secret_value", {"ARN": SECRET_ARN, "VersionId": "n" * 32},
        {"SecretId": SECRET_ID, "SecretString": ANY},
    )
    adapter = adapter_with(
        monkeypatch, ("elasticache", (client, stub)),
        ("secretsmanager", (secrets_client, secrets_stub)),
    )
    account, network, store = context()

    with stub, secrets_stub:
        result = adapter.begin_rotation(
            account, network, store, "workload-secrets", NAME, GROUP_ID, "example-local", 2
        )

    assert result == (CANDIDATE, SECRET_ARN, "n" * 32)
    assert calls == ["DescribeUsers", "CreateUser", "ModifyUserGroup", "CreateSecret",
                     "PutSecretValue"]


def test_a_rotation_never_adopts_a_candidate_user_that_already_exists(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    secrets_client, secrets_stub = stubbed("secretsmanager")
    stub.add_response("describe_users", {"Users": [{"UserId": CANDIDATE}]}, {"UserId": CANDIDATE})
    adapter = adapter_with(
        monkeypatch, ("elasticache", (client, stub)),
        ("secretsmanager", (secrets_client, secrets_stub)),
    )
    account, network, store = context()

    with stub, secrets_stub, pytest.raises(ResourceError, match="rotate_candidate_exists$"):
        adapter.begin_rotation(
            account, network, store, "workload-secrets", NAME, GROUP_ID, "example-local", 2
        )

    secrets_stub.assert_no_pending_responses()


def test_a_rotation_failure_is_a_bounded_code_without_the_password(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    secrets_client, secrets_stub = stubbed("secretsmanager")
    stub.add_client_error("describe_users", "UserNotFound", expected_params={"UserId": CANDIDATE})
    stub.add_client_error("create_user", "AccessDenied", f"denied for {PASSWORD}")
    adapter = adapter_with(
        monkeypatch, ("elasticache", (client, stub)),
        ("secretsmanager", (secrets_client, secrets_stub)),
    )
    account, network, store = context()

    with stub, pytest.raises(ResourceError) as raised:
        adapter.begin_rotation(
            account, network, store, "workload-secrets", NAME, GROUP_ID, "example-local", 2
        )

    assert str(raised.value) == "aws_elasticache_rotate_user_access_denied"
    assert PASSWORD not in repr(raised.value)


def test_restoring_a_credential_writes_nothing_when_it_is_already_current(monkeypatch) -> None:
    secrets_client, secrets_stub = stubbed("secretsmanager")
    secrets_stub.add_response(
        "get_secret_value", credential(), {"SecretId": SECRET_ID, "VersionStage": "AWSCURRENT"}
    )
    secrets_stub.add_response(
        "describe_secret",
        {"ARN": SECRET_ARN, "VersionIdsToStages": {"a" * 32: ["AWSPREVIOUS"],
                                                   "b" * 32: ["AWSCURRENT"]}},
        {"SecretId": SECRET_ID},
    )
    adapter = adapter_with(monkeypatch, ("secretsmanager", (secrets_client, secrets_stub)))
    account, _network, store = context()

    with secrets_stub:
        result = adapter.restore_credential(account, store, NAME, "example-local",
                                            "gimme-example-local")

    assert result == (SECRET_ARN, "b" * 32)


def test_restoring_a_credential_puts_the_previous_version_back_as_current(monkeypatch) -> None:
    secrets_client, secrets_stub = stubbed("secretsmanager")
    secrets_stub.add_response(
        "get_secret_value", credential("gimme-example-local-g2"),
        {"SecretId": SECRET_ID, "VersionStage": "AWSCURRENT"},
    )
    secrets_stub.add_response(
        "get_secret_value", credential(),
        {"SecretId": SECRET_ID, "VersionStage": "AWSPREVIOUS"},
    )
    secrets_stub.add_client_error(
        "create_secret", "ResourceExistsException",
        expected_params={"Name": SECRET_ID, "SecretString": ANY, "Tags": [
            {"Key": "gimme:deployment", "Value": "example-local"},
            {"Key": "gimme:resource", "Value": NAME}]},
    )
    secrets_stub.add_response(
        "put_secret_value", {"ARN": SECRET_ARN, "VersionId": "c" * 32},
        {"SecretId": SECRET_ID, "SecretString": json.dumps(
            {"password": "old-" + "q" * 40, "username": "gimme-example-local"},
            sort_keys=True, separators=(",", ":"))},
    )
    adapter = adapter_with(monkeypatch, ("secretsmanager", (secrets_client, secrets_stub)))
    account, _network, store = context()

    with secrets_stub:
        result = adapter.restore_credential(account, store, NAME, "example-local",
                                            "gimme-example-local")

    assert result == (SECRET_ARN, "c" * 32)


def test_restoring_a_credential_that_is_in_neither_version_fails_closed(monkeypatch) -> None:
    secrets_client, secrets_stub = stubbed("secretsmanager")
    for stage in ("AWSCURRENT", "AWSPREVIOUS"):
        secrets_stub.add_response(
            "get_secret_value", credential("gimme-example-local-g9"),
            {"SecretId": SECRET_ID, "VersionStage": stage},
        )
    adapter = adapter_with(monkeypatch, ("secretsmanager", (secrets_client, secrets_stub)))
    account, _network, store = context()

    with secrets_stub, pytest.raises(ResourceError) as raised:
        adapter.restore_credential(account, store, NAME, "example-local", "gimme-example-local")

    assert str(raised.value) == "aws_elasticache_rotate_previous_credential_missing"


def test_a_credential_read_failure_is_a_bounded_code(monkeypatch) -> None:
    secrets_client, secrets_stub = stubbed("secretsmanager")
    secrets_stub.add_client_error("get_secret_value", "AccessDeniedException",
                                  f"denied {PASSWORD}")
    adapter = adapter_with(monkeypatch, ("secretsmanager", (secrets_client, secrets_stub)))
    account, _network, store = context()

    with secrets_stub, pytest.raises(ResourceError) as raised:
        adapter.restore_credential(account, store, NAME, "example-local", "gimme-example-local")

    assert str(raised.value) == "aws_elasticache_credential_read_access_denied"
    assert PASSWORD not in repr(raised.value)


def test_one_user_is_deleted_only_by_the_destructive_role_after_its_tag_is_read(
    monkeypatch,
) -> None:
    adapter, account, network, (reader_stub, killer_stub), calls, assumed = destroyer(monkeypatch)
    reader_stub.add_response(
        "list_tags_for_resource", owned(), {"ResourceName": f"{ARN_PREFIX}:user:{USER_ID}"}
    )
    killer_stub.add_response("delete_user", {}, {"UserId": USER_ID})

    with reader_stub, killer_stub:
        adapter.remove_user(account, network, NAME, USER_ID)

    assert calls == ["read:ListTagsForResource", "delete:DeleteUser"]
    assert {role for role, _ in assumed} == {INSPECTOR, DESTROYER}


def test_a_user_another_resource_owns_is_never_deleted(monkeypatch) -> None:
    adapter, account, network, (reader_stub, killer_stub), calls, _assumed = destroyer(monkeypatch)
    reader_stub.add_response(
        "list_tags_for_resource", owned("someone-else"),
        {"ResourceName": f"{ARN_PREFIX}:user:{USER_ID}"},
    )

    with reader_stub, killer_stub, pytest.raises(ResourceError) as raised:
        adapter.remove_user(account, network, NAME, USER_ID)

    assert str(raised.value) == "aws_elasticache_rotate_ownership_mismatch"
    assert calls == ["read:ListTagsForResource"]


def test_removing_a_user_needs_the_destructive_role(monkeypatch) -> None:
    adapter, account, network, _stubs, _calls, assumed = destroyer(monkeypatch, with_role=False)

    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_role_missing$"):
        adapter.remove_user(account, network, NAME, USER_ID)

    assert assumed == []


@pytest.mark.parametrize("kind", ["restoring", "rotating"])
def test_a_resource_mid_recovery_cannot_be_destroyed(tmp_path, monkeypatch, kind) -> None:
    adapter = destroyable(tmp_path, monkeypatch)
    plan = server_module.plan_destroy_resource(NAME)
    resources_valkey_module.write_marker(
        server_module.store.root, kind, NAME, {"schema_version": 1, "resource": NAME}
    )

    with pytest.raises(ResourceError, match="_in_progress$"):
        server_module.apply_destroy_resource(NAME, str(plan["plan_id"]), CONFIRM)

    assert adapter.delete_calls == []


def test_a_rotation_without_the_destructive_role_starts_nothing(tmp_path, monkeypatch) -> None:
    adapter = bound_and_ready(tmp_path, monkeypatch)
    account, network, resource, name, workload_store, store_name = destroy_context()
    assert account.destructive_role_arn is None

    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_role_missing$"):
        valkey_recovery_module.apply_rotation(
            adapter, server_module.store.root, account, network, resource, name,
            workload_store, store_name, DEPLOYMENT, lambda _deployment: None,
        )

    assert not marker_file("rotating").exists() and adapter.credentials[DEPLOYMENT] == [
        binding_username(DEPLOYMENT)
    ]


def test_an_allocation_whose_deployment_is_gone_is_restored_but_not_verified(
    tmp_path, monkeypatch, instant
) -> None:
    adapter, calls = lost(tmp_path, monkeypatch, instant)
    root = server_module.store.root
    observed = load_observed(root, NAME)
    assert observed is not None
    allocations = cast(dict[str, dict[str, object]], observed["allocations"])
    allocations["gone-deployment"] = {**allocations[DEPLOYMENT], "user_id": "gimme-u-orphan"}
    (root / "observed-resources" / f"{NAME}.json").write_text(json.dumps(observed))

    plan = server_module.plan_restore_resource(NAME, SNAPSHOT)
    result = server_module.apply_restore_resource(NAME, SNAPSHOT, str(plan["plan_id"]))

    assert plan["deployments"] == [DEPLOYMENT]
    assert result["restored"] is True
    assert cast(dict[str, object], adapter.create_args[-1]["restore_users"]).keys() == {
        DEPLOYMENT, "gone-deployment"
    }, "the orphan's user is still restored"
    assert tasks(calls).count("gimme:probe:valkey:current") == 1


def test_a_failed_verification_names_the_deployment_in_the_inspection(
    tmp_path, monkeypatch, instant
) -> None:
    _adapter, _calls = lost(tmp_path, monkeypatch, instant)
    real = server_module.runner.run
    monkeypatch.setattr(server_module.runner, "run", lambda task, *a, **k: (
        (_ for _ in ()).throw(RuntimeError("secret detail")) if task == "gimme:probe:valkey:current"
        else real(task, *a, **k)
    ))

    with pytest.raises(ResourceError, match="verification_failed"):
        restore()

    inspected = server_module.inspect_resource(NAME)
    assert inspected["progress"] == {"verified": [], "failed": DEPLOYMENT}
    assert "secret detail" not in json.dumps(inspected)


def test_a_rotation_of_a_deployment_that_no_longer_binds_the_resource_is_refused(
    tmp_path, monkeypatch
) -> None:
    rotatable(tmp_path, monkeypatch)
    document = server_module.store.load().model_dump(mode="json")
    document["deployments"][DEPLOYMENT]["resources"]["valkey"] = {
        "resource": "devbox-valkey", "uses": ["cache"]
    }
    server_module.store.save(ControlState.model_validate(document))

    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_binding_missing$"):
        server_module.plan_rotate_resource_credential(NAME, DEPLOYMENT)


# --- detachment and same-Resource rebinding -----------------------------------------------

USER_G1 = derive_binding_user_id(GROUP_ID, DEPLOYMENT, 1)
USER_G2 = derive_binding_user_id(GROUP_ID, DEPLOYMENT, 2)


def detachable(tmp_path, monkeypatch) -> tuple[FakeValkey, list[dict[str, object]]]:
    adapter = bound_and_ready(tmp_path, monkeypatch)
    calls = capture_runs(monkeypatch)
    real = server_module.runner.run
    monkeypatch.setattr(server_module.runner, "run", lambda task, *a, **k: (
        adapter.events.append(task), real(task, *a, **k))[1])
    return adapter, calls


def moved(**updates) -> DeploymentRegistration:
    """The Deployment now uses another Resource: a non-static Deployment cannot bind none."""
    current = server_module.store.deployment(DEPLOYMENT)
    return DeploymentRegistration.from_deployment(current).model_copy(update={
        "resources": ResourceBindings(
            database="devbox-postgres",
            valkey=ValkeyBinding(resource="devbox-valkey", uses=["cache", "queue"]),
        ), **updates,
    })


def unbind(**updates) -> dict[str, object]:
    definition = moved(**updates)
    plan = server_module.plan_update_deployment(DEPLOYMENT, definition)
    return server_module.update_deployment(DEPLOYMENT, definition, str(plan["plan_id"]))


def recovering_valkey(monkeypatch, evidence: bool) -> list[datetime.datetime]:
    """The Deployment's Recovery Policy includes Valkey; `evidence` says whether a verified
    Component Backup is found. Returns the cutoffs it was asked about."""
    document = server_module.store.load().model_dump(mode="json")
    document["deployments"][DEPLOYMENT]["recovery"]["valkey"] = True
    server_module.store.save(ControlState.model_validate(document))
    cutoffs: list[datetime.datetime] = []

    def find(kind, deployment, allocation, *, cutoff=None):
        assert kind == "valkey" and deployment == DEPLOYMENT
        recorded = allocation.get("recovery_evidence")
        if recorded is not None:
            return recorded if evidence else None
        cutoffs.append(cutoff)
        return None if not evidence else {
            "recovery_point_id": "rp_" + "a" * 20, "destination": "primary",
            "captured_at": (cutoff - datetime.timedelta(hours=1)).isoformat(),
            "generation": allocation.get("generation", 1),
        }

    monkeypatch.setattr(server_module, "_recovery_evidence", find)
    return cutoffs


def test_moving_the_binding_disables_the_user_and_keeps_keys_and_credential(
    tmp_path, monkeypatch
) -> None:
    adapter, calls = detachable(tmp_path, monkeypatch)
    plan = server_module.plan_update_deployment(DEPLOYMENT, moved())
    assert any("Detached Allocation" in effect for effect in plan["effects"])

    result = unbind()

    detached = allocation()
    assert result["changed"] is True and result["deployment"] == DEPLOYMENT
    assert detached["status"] == "detached" and detached["user_id"] == USER_G1
    assert detached["recovery_expected"] is False and detached["recovery_evidence"] is None
    assert adapter.events == [f"disable:{USER_G1}"]
    assert adapter.removed_users == [] and adapter.deleted_secrets == []
    assert adapter.delete_calls == []
    assert server_module.store.deployment(DEPLOYMENT).resources.valkey.resource == "devbox-valkey"
    assert "gimme:stop:processes" not in [call["task"] for call in calls]


def test_a_deployment_with_managed_processes_is_stopped_before_its_user_is_disabled(
    tmp_path, monkeypatch
) -> None:
    adapter, _calls = detachable(tmp_path, monkeypatch)
    running = server_module.store.deployment(DEPLOYMENT).model_copy(
        update={"workers": HorizonWorkerConfig()}
    )
    server_module.store.save(server_module.store.load().model_copy(update={
        "deployments": {**server_module.store.load().deployments, DEPLOYMENT: running}
    }))

    unbind(workers=HorizonWorkerConfig())

    assert adapter.events == ["gimme:stop:processes", f"disable:{USER_G1}"]


def test_removing_the_deployment_detaches_its_allocation(tmp_path, monkeypatch) -> None:
    adapter, _calls = detachable(tmp_path, monkeypatch)
    plan = server_module.plan_remove_deployment(DEPLOYMENT)
    assert any("Detached Allocation" in effect for effect in plan["effects"])

    server_module.remove_deployment(DEPLOYMENT, str(plan["plan_id"]), str(plan["confirmation"]))

    assert allocation()["status"] == "detached"
    assert adapter.events.index("gimme:remove:deployment") < adapter.events.index(
        f"disable:{USER_G1}"
    )
    assert "gimme:stop:processes" not in adapter.events
    assert DEPLOYMENT not in server_module.store.load().deployments


def test_a_detach_records_fresh_evidence_when_the_policy_includes_valkey(
    tmp_path, monkeypatch
) -> None:
    detachable(tmp_path, monkeypatch)
    cutoffs = recovering_valkey(monkeypatch, evidence=True)

    unbind(recovery=server_module.store.deployment(DEPLOYMENT).recovery.model_copy(
        update={"valkey": False}))

    detached = allocation()
    assert detached["recovery_expected"] is True and len(cutoffs) == 1
    assert cast(dict, detached["recovery_evidence"])["recovery_point_id"] == "rp_" + "a" * 20
    assert resources_valkey_module.recovery_evidence_is_fresh(detached)
    assert datetime.datetime.fromisoformat(str(detached["detached_at"])) == cutoffs[0]


def test_missing_evidence_never_blocks_a_detach_but_is_recorded(tmp_path, monkeypatch) -> None:
    adapter, _calls = detachable(tmp_path, monkeypatch)
    recovering_valkey(monkeypatch, evidence=False)

    unbind(recovery=server_module.store.deployment(DEPLOYMENT).recovery.model_copy(
        update={"valkey": False}))

    detached = allocation()
    assert detached["status"] == "detached" and detached["recovery_expected"] is True
    assert detached["recovery_evidence"] is None
    assert not resources_valkey_module.recovery_evidence_is_fresh(detached)
    assert adapter.events == [f"disable:{USER_G1}"]


def test_a_repeated_detach_is_a_no_op(tmp_path, monkeypatch) -> None:
    adapter, _calls = detachable(tmp_path, monkeypatch)
    unbind()
    first = allocation()

    result = server_module._resource_retirement_orchestrator().detach_valkey_allocation(
        DEPLOYMENT, NAME, server_module.store.deployment(DEPLOYMENT), stop_processes=True,
    )

    assert result == {"detached": True, "already_detached": True}
    assert allocation() == first and adapter.events == [f"disable:{USER_G1}"]


def test_an_unfinished_detach_is_finished_by_repeating_the_update(tmp_path, monkeypatch) -> None:
    adapter, _calls = detachable(tmp_path, monkeypatch)
    real = adapter.disable_binding
    monkeypatch.setattr(adapter, "disable_binding", lambda *a: (_ for _ in ()).throw(
        ResourceError("aws_elasticache_user_disable_throttled")))
    definition = moved()
    plan = server_module.plan_update_deployment(DEPLOYMENT, definition)
    with pytest.raises(ResourceError, match="user_disable_throttled"):
        server_module.update_deployment(DEPLOYMENT, definition, str(plan["plan_id"]))
    assert allocation()["status"] == "active"
    assert server_module.store.deployment(DEPLOYMENT).resources.valkey is not None

    monkeypatch.setattr(adapter, "disable_binding", real)
    server_module.update_deployment(DEPLOYMENT, definition, str(plan["plan_id"]))

    assert allocation()["status"] == "detached"


def test_a_detach_is_refused_while_the_resource_is_busy(tmp_path, monkeypatch) -> None:
    adapter, _calls = detachable(tmp_path, monkeypatch)
    resources_valkey_module.write_marker(
        server_module.store.root, "rotating", NAME, {"schema_version": 1, "resource": NAME}
    )
    definition = moved()
    plan = server_module.plan_update_deployment(DEPLOYMENT, definition)

    with pytest.raises(ResourceError, match="_in_progress$"):
        server_module.update_deployment(DEPLOYMENT, definition, str(plan["plan_id"]))

    assert adapter.events == [] and allocation()["status"] == "active"


def test_a_rebind_restores_the_namespace_with_a_new_user_generation(
    tmp_path, monkeypatch
) -> None:
    adapter, _calls = detachable(tmp_path, monkeypatch)
    unbind()
    document = server_module.store.load().model_dump(mode="json")
    document["deployments"][DEPLOYMENT]["resources"]["valkey"] = {
        "resource": NAME, "uses": ["cache", "queue"]}
    server_module.store.save(ControlState.model_validate(document))
    plan = server_module.plan_bind_resource(DEPLOYMENT)
    valkey_plan = cast(dict[str, object], plan["valkey"])
    assert valkey_plan["already_bound"] is False
    assert valkey_plan["reactivates_detached_allocation"] is True

    server_module.bind_resource(DEPLOYMENT, str(plan["plan_id"]))

    rebound = allocation()
    assert rebound["status"] == "active" and rebound["generation"] == 2
    assert rebound["user_id"] == USER_G2 and rebound["retired_user_ids"] == [USER_G1]
    assert not resources_valkey_module.DETACHED_FIELDS & set(rebound)
    assert adapter.binding_calls[-1] == (DEPLOYMENT, False)
    assert adapter.credentials[DEPLOYMENT][-1] == binding_username(DEPLOYMENT, 2)


def test_destruction_targets_include_retired_users(tmp_path, monkeypatch) -> None:
    detachable(tmp_path, monkeypatch)
    unbind()
    document = server_module.store.load().model_dump(mode="json")
    document["deployments"][DEPLOYMENT]["resources"]["valkey"] = {
        "resource": NAME, "uses": ["cache"]}
    server_module.store.save(ControlState.model_validate(document))
    bind()

    _fingerprint, users = resources_valkey_module.destruction_targets(
        server_module.store.root, NAME
    )

    assert USER_G1 in users and USER_G2 in users


def test_a_rebind_is_refused_when_eight_users_are_already_retired(tmp_path, monkeypatch) -> None:
    detachable(tmp_path, monkeypatch)
    unbind()
    path = server_module.store.root / "observed-resources" / f"{NAME}.json"
    document = json.loads(path.read_text())
    document["allocations"][DEPLOYMENT]["retired_user_ids"] = [
        f"gimme-u-{index:024x}" for index in range(8)
    ]
    path.write_text(json.dumps(document))
    document = server_module.store.load().model_dump(mode="json")
    document["deployments"][DEPLOYMENT]["resources"]["valkey"] = {
        "resource": NAME, "uses": ["cache"]}
    server_module.store.save(ControlState.model_validate(document))
    plan = server_module.plan_bind_resource(DEPLOYMENT)

    with pytest.raises(ResourceError, match="^aws_elasticache_binding_retired_users_full$"):
        server_module.bind_resource(DEPLOYMENT, str(plan["plan_id"]))


def test_a_detached_allocation_takes_no_rotation(tmp_path, monkeypatch) -> None:
    detachable(tmp_path, monkeypatch)
    document = server_module.store.load().model_dump(mode="json")
    document["provider_accounts"]["main"]["destructive_role_arn"] = DESTROYER
    server_module.store.save(ControlState.model_validate(document))
    unbind()

    with pytest.raises(ResourceError, match="^aws_elasticache_rotate_binding_missing$"):
        server_module.plan_rotate_resource_credential(NAME, DEPLOYMENT)


def test_inspection_counts_active_and_detached_allocations_separately(
    tmp_path, monkeypatch
) -> None:
    detachable(tmp_path, monkeypatch)
    assert server_module.inspect_resource(NAME)["binding_count"] == 1
    unbind()

    inspected = server_module.inspect_resource(NAME)

    assert inspected["binding_count"] == 0 and inspected["detached_count"] == 1
    assert inspected["allocations"] == {DEPLOYMENT: {"status": "detached"}}
    assert USER_G1 not in json.dumps(inspected)


def written_allocation(tmp_path, monkeypatch, change) -> None:
    detachable(tmp_path, monkeypatch)
    path = server_module.store.root / "observed-resources" / f"{NAME}.json"
    document = json.loads(path.read_text())
    document["allocations"][DEPLOYMENT] = change(document["allocations"][DEPLOYMENT])
    path.write_text(json.dumps(document))


def detached_record(record: dict, **updates) -> dict:
    return {
        **record, "status": "detached", "detached_at": "2026-01-01T00:00:00+00:00",
        "recovery_expected": False, "recovery_evidence": None, **updates,
    }


def evidence(**updates) -> dict:
    return {
        "recovery_point_id": "rp_" + "a" * 20, "destination": "primary",
        "captured_at": "2025-12-31T23:00:00+00:00", "generation": 1, **updates,
    }


@pytest.mark.parametrize(
    "change",
    [
        lambda record: record,
        lambda record: detached_record(record),
        lambda record: detached_record(
            record, recovery_expected=True, recovery_evidence=evidence()
        ),
        lambda record: {**record, "generation": 2, "retired_user_ids": [USER_G1]},
    ],
)
def test_allocation_records_old_and_new_validate(tmp_path, monkeypatch, change) -> None:
    written_allocation(tmp_path, monkeypatch, change)

    assert load_observed(server_module.store.root, NAME) is not None


@pytest.mark.parametrize(
    "change",
    [
        lambda record: {**record, "detached_at": "2026-01-01T00:00:00+00:00"},
        lambda record: {key: value for key, value in detached_record(record).items()
                        if key != "recovery_evidence"},
        lambda record: detached_record(record, detached_at="2026-01-01T00:00:00"),
        lambda record: detached_record(record, detached_at="yesterday"),
        lambda record: detached_record(record, recovery_expected="yes"),
        lambda record: detached_record(record, recovery_evidence=evidence(generation=2)),
        lambda record: detached_record(record, recovery_evidence=evidence(recovery_point_id="x")),
        lambda record: detached_record(record, recovery_evidence={**evidence(), "extra": 1}),
        lambda record: {
            **record, "retired_user_ids": [f"gimme-u-{index:024x}" for index in range(9)]
        },
        lambda record: {**record, "retired_user_ids": ["not-a-user"]},
        lambda record: {**record, "generation": 0},
    ],
)
def test_a_malformed_detached_or_retired_record_makes_the_cache_invalid(
    tmp_path, monkeypatch, change
) -> None:
    written_allocation(tmp_path, monkeypatch, change)

    with pytest.raises(ResourceError, match="^observed_resource_invalid$"):
        load_observed(server_module.store.root, NAME)


def test_disabling_a_binding_uses_only_the_inspection_role_and_is_idempotent(
    monkeypatch,
) -> None:
    client, stub = stubbed("elasticache")
    stub.add_response("modify_user", {}, {"UserId": USER_ID, "AccessString": "off ~* -@all"})
    stub.add_response(
        "describe_user_groups",
        {"UserGroups": [{"UserGroupId": derive_user_group_id(GROUP_ID), "UserIds": [USER_ID]}]},
        {"UserGroupId": derive_user_group_id(GROUP_ID)},
    )
    stub.add_response(
        "modify_user_group", {},
        {"UserGroupId": derive_user_group_id(GROUP_ID), "UserIdsToRemove": [USER_ID]},
    )
    stub.add_response("modify_user", {}, {"UserId": USER_ID, "AccessString": "off ~* -@all"})
    stub.add_response(
        "describe_user_groups",
        {"UserGroups": [{"UserGroupId": derive_user_group_id(GROUP_ID), "UserIds": []}]},
        {"UserGroupId": derive_user_group_id(GROUP_ID)},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _store = context()

    with stub:
        adapter.disable_binding(account, network, GROUP_ID, USER_ID)
        adapter.disable_binding(account, network, GROUP_ID, USER_ID)


def test_a_user_that_is_already_gone_counts_as_disabled(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_client_error("modify_user", "UserNotFound")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _store = context()

    with stub:
        adapter.disable_binding(account, network, GROUP_ID, USER_ID)


@pytest.mark.parametrize("stage", ["disable", "group"])
def test_a_disable_failure_is_a_bounded_code(monkeypatch, stage) -> None:
    client, stub = stubbed("elasticache")
    if stage == "disable":
        stub.add_client_error("modify_user", "AccessDenied", f"denied {PASSWORD}")
    else:
        stub.add_response("modify_user", {}, {"UserId": USER_ID, "AccessString": "off ~* -@all"})
        stub.add_client_error("describe_user_groups", "Throttling", f"slow {PASSWORD}")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _store = context()

    with stub, pytest.raises(ResourceError) as raised:
        adapter.disable_binding(account, network, GROUP_ID, USER_ID)

    assert re.fullmatch(
        r"aws_elasticache_user_(disable|group_unbind)_[a-z_]+", str(raised.value)
    )
    assert PASSWORD not in repr(raised.value)


# --- destruction with Detached Allocations --------------------------------------------------


def detached_and_destroyable(tmp_path, monkeypatch, *, expected: bool | None = None):
    """Bound, then moved off the Resource so its allocation is detached, on an account with a
    destructive role. `expected` None keeps the Recovery Policy without Valkey."""
    adapter, _calls = detachable(tmp_path, monkeypatch)
    document = server_module.store.load().model_dump(mode="json")
    document["provider_accounts"]["main"]["destructive_role_arn"] = DESTROYER
    server_module.store.save(ControlState.model_validate(document))
    if expected is None:
        unbind()
    else:
        recovering_valkey(monkeypatch, evidence=expected)
        unbind(recovery=server_module.store.deployment(DEPLOYMENT).recovery.model_copy(
            update={"valkey": False}))
    return adapter


def test_a_detached_allocation_without_a_valkey_policy_is_resolved_with_a_warning(
    tmp_path, monkeypatch, instant
) -> None:
    adapter = detached_and_destroyable(tmp_path, monkeypatch)

    plan = server_module.plan_destroy_resource(NAME)

    assert plan["detached_allocations"] == [{
        "deployment": DEPLOYMENT, "generation": 1, "recovery_point_id": None, "captured_at": None,
    }]
    assert any("not guaranteed" in warning for warning in plan["warnings"])
    assert USER_G1 not in json.dumps(plan)

    destroy()

    assert USER_G1 in adapter.removed_users or USER_G1 in [
        user for _name, users in adapter.dependents_calls for user in users
    ]


def test_a_detached_allocation_with_fresh_evidence_is_resolved_and_names_it(
    tmp_path, monkeypatch
) -> None:
    detached_and_destroyable(tmp_path, monkeypatch, expected=True)

    plan = server_module.plan_destroy_resource(NAME)

    [item] = plan["detached_allocations"]
    assert item["recovery_point_id"] == "rp_" + "a" * 20 and item["generation"] == 1
    assert plan["warnings"] == []


def test_a_destruction_is_refused_when_recovery_was_expected_but_no_evidence_was_taken(
    tmp_path, monkeypatch
) -> None:
    detached_and_destroyable(tmp_path, monkeypatch, expected=False)

    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_recovery_evidence_missing$"):
        server_module.plan_destroy_resource(NAME)


def test_a_destruction_is_refused_when_the_recorded_evidence_no_longer_exists(
    tmp_path, monkeypatch
) -> None:
    detached_and_destroyable(tmp_path, monkeypatch, expected=True)
    monkeypatch.setattr(server_module, "_recovery_evidence", lambda *a, **k: None)

    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_recovery_evidence_stale$"):
        server_module.plan_destroy_resource(NAME)


def test_an_unfinished_unbind_still_blocks_destruction(tmp_path, monkeypatch) -> None:
    detached_and_destroyable(tmp_path, monkeypatch)
    path = server_module.store.root / "observed-resources" / f"{NAME}.json"
    document = json.loads(path.read_text())
    for key in ("detached_at", "recovery_expected", "recovery_evidence"):
        del document["allocations"][DEPLOYMENT][key]
    document["allocations"][DEPLOYMENT]["status"] = "active"
    path.write_text(json.dumps(document))

    with pytest.raises(ResourceError, match="^aws_elasticache_destroy_bindings_remain$"):
        server_module.plan_destroy_resource(NAME)
