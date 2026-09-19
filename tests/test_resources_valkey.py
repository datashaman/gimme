import dataclasses
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
    AWSElastiCacheValkeyResource, AWSSecretsManagerStore, ControlState, StateStore,
)
from gimme.resources_postgres import ResourceError
from gimme.resources_valkey import (
    ADMIN_ACCESS_STRING, BotoElastiCacheAdapter, GroupObservation, ISSUES, MODIFIABLE_FIELDS,
    ValkeyOptions, apply_provision, derive_group_id, derive_user_group_id, group_drift,
    load_observed, modification_for, structural_issues,
)
import gimme.server as server_module

EXAMPLE = Path(__file__).resolve().parents[1] / "config/state.example.json"
NAME = "example-elasticache-valkey"


def valkey(**updates) -> AWSElastiCacheValkeyResource:
    values = json.loads(EXAMPLE.read_text())["resources"][NAME] | updates
    return AWSElastiCacheValkeyResource.model_validate(values)


GROUP_ID = derive_group_id(NAME)
ARN = f"arn:aws:elasticache:eu-central-1:123456789012:replicationgroup:{GROUP_ID}"
PASSWORD = "p" * 48
ANY_UPDATE_ACTIONS = {
    "ReplicationGroupIds": [GROUP_ID], "ServiceUpdateStatus": ["available"], "MaxRecords": 100,
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
    )
    values.update(updates)
    return GroupObservation(**values)  # type: ignore[arg-type]


FIELDS = {
    "EngineVersion": "engine_version", "CacheNodeType": "node_type",
    "SnapshotRetentionLimit": "snapshot_retention_days", "SnapshotWindow": "snapshot_window",
    "PreferredMaintenanceWindow": "maintenance_window",
}
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
        self._polls = 0

    def describe_group(self, account, network, group_id):
        self.describe_calls += 1
        if self.live is not None and self.live.status in ("creating", "modifying"):
            self._polls += 1
            if self._polls > self.settle_polls:
                self.live = dataclasses.replace(self.live, status="available")
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

    def create_group(self, account, network, resource, name, group_id, store, store_name):
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
        "engine_version", "effective_durability", "issues", "endpoint", "port",
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


def expect_describe(
    stub, cache_client_stub, *, actions=None, pending=None, **updates
) -> None:
    stub.add_response(
        "describe_replication_groups", {"ReplicationGroups": [group_response(**updates)]},
        {"ReplicationGroupId": GROUP_ID},
    )
    stub.add_response(
        "list_tags_for_resource", {"TagList": [{"Key": "gimme:resource", "Value": NAME}]},
        {"ResourceName": ARN},
    )
    if updates.get("Status", "available") == "available":
        stub.add_response(
            "describe_update_actions", {"UpdateActions": actions or []}, ANY_UPDATE_ACTIONS
        )
    stub.add_response(
        "describe_cache_clusters",
        {"CacheClusters": [{
            "EngineVersion": "9.0", "PreferredMaintenanceWindow": "sun:05:00-sun:06:00",
            "AutoMinorVersionUpgrade": False, "PendingModifiedValues": pending or {},
        }]},
        {"CacheClusterId": f"{GROUP_ID}-0001-001"},
    )


TAG = [{"Key": "gimme:resource", "Value": NAME}]


def test_describe_parses_a_live_group(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    expect_describe(stub, stub)
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
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
    stub.add_response(
        "list_tags_for_resource", {"TagList": TAG}, {"ResourceName": ARN},
    )
    stub.add_client_error("describe_cache_clusters", "CacheClusterNotFound")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        observed = adapter.describe_group(account, network, GROUP_ID)

    assert observed is not None and observed.engine_version is None
    assert observed.maintenance_window is None


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
         "AccessString": "off ~* -@all", "NoPasswordRequired": True, "Tags": TAG},
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
    expect_describe(stub, stub, Status="creating")


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
        observed = adapter.create_group(
            account, network, valkey(), NAME, GROUP_ID, store, "workload-secrets"
        )

    assert observed.status == "creating"
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
    expect_describe(
        stub, stub,
        actions=[
            {"SlaMet": "yes", "UpdateActionStatus": "not-applied"},
            {"SlaMet": "no", "UpdateActionStatus": "complete"},
            {"SlaMet": "no", "UpdateActionStatus": "not-applied"},
        ],
        pending={"EngineVersion": "9.1", "CacheNodeType": "cache.m7g.xlarge"},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        observed = adapter.describe_group(account, network, GROUP_ID)

    assert observed == observation(
        pending_engine_version="9.1", pending_node_type="cache.m7g.xlarge",
        service_update_overdue=True,
    )


@pytest.mark.parametrize(
    "action", [{"SlaMet": "n/a", "UpdateActionStatus": "not-applied"},
               {"SlaMet": "no", "UpdateActionStatus": "not-applicable"}],
)
def test_only_an_unfinished_action_past_its_apply_by_date_is_overdue(monkeypatch, action) -> None:
    client, stub = stubbed("elasticache")
    expect_describe(stub, stub, actions=[action])
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        assert adapter.describe_group(account, network, GROUP_ID).service_update_overdue is False


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
    expect_describe(stub, stub, Status="modifying")
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
        ]},
        {"Marker": "more"},
    )
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub:
        options = adapter.live_options(account, network)

    assert options == ValkeyOptions(
        ("9.0", "9.1", "10.0"), ("cache.m7g.large", "cache.m7g.xlarge")
    )
    stub.assert_no_pending_responses()


def test_live_options_failures_are_bounded(monkeypatch) -> None:
    client, stub = stubbed("elasticache")
    stub.add_client_error("describe_cache_engine_versions", "AccessDenied")
    adapter = adapter_with(monkeypatch, ("elasticache", (client, stub)))
    account, network, _ = context()

    with stub, pytest.raises(ResourceError, match="^aws_elasticache_options_access_denied$"):
        adapter.live_options(account, network)
