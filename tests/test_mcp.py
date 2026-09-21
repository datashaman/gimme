import base64
import dataclasses
import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastmcp import Client
import pytest

from gimme.config import (
    ArtisanConfig, HealthCheckConfig, HorizonWorkerConfig, SchedulerConfig, StackConfig,
)
from gimme.control import (
    AWSNetwork,
    AWSElastiCacheValkeyResource,
    AWSProviderAccount,
    AWSRDSPostgresResource,
    AWSSecretsManagerStore,
    ApplicationConfig,
    ControlState,
    CredentialReferenceBackupAuth,
    DeploymentConfig,
    DeploymentRegistration,
    DeploymentSource,
    Placement,
    RecoveryPolicy,
    ResourceBindings,
    Rollout,
    RolloutArtifact,
    ValkeyBinding,
    ResourceConfig,
    RuntimePin,
    S3BackupDestination,
    SecretReference,
    SSEAES256,
    SopsSecretStore,
    StateStore,
    TargetConfig,
    TargetNetwork,
    explicit_placement_decision,
)
from gimme.deployer import CommandResult
from gimme.deployment_release_orchestration import DeploymentReleaseOrchestrator
from gimme.recovery import ObjectMetadata, RecoveryError
from gimme.recovery import append_restore_event, recovery_point_id, restore_event_key
from gimme.resources_postgres import (
    RDS_TRUST_BUNDLE_SHA256, InstanceObservation, ResourceError, SnapshotObservation,
)
from gimme.secrets import SecretError, SecretMetadata
import gimme.server as server_module
import gimme.resources_postgres as resources_postgres_module
import gimme.control_plans as control_plans_module
import gimme.deployment_resource_orchestration as deployment_resource_module
import gimme.recovery as recovery_module
import gimme.recovery_schedule as recovery_schedule_module
from gimme.recovery_orchestration import RecoveryOrchestrator
from gimme.resource_orchestration import ManagedResourceOrchestrator
from gimme.server import mcp


LOCAL_VALKEY = ValkeyBinding(resource="devbox-valkey", uses=["cache"])


def sample_state() -> ControlState:
    target = TargetConfig(
        host_alias="devbox",
        bootstrap_hostname="192.0.2.10",
        hostname="devbox.local",
        system_hostname="devbox",
        remote_user="deployer",
        apps_root="/srv/gimme/apps",
        deployment_slots=2,
        network=TargetNetwork(mode="local_mdns", mdns_name="devbox"),
        stack=StackConfig(package_manager="apt", packages=["git"], services=[]),
    )
    application = ApplicationConfig(
        repository="git@github.com:example/example-app.git",
        framework="laravel",
        artisan=ArtisanConfig(allowed_commands=["about", "migrate"]),
        default_health=HealthCheckConfig(path="/up"),
    )
    deployment = DeploymentConfig(
        application="example-app",
        target="devbox",
        stage="local",
        release_mode="source",
        source=DeploymentSource(kind="branch", ref="main"),
        app_env="local",
        runtimes={
            "php": RuntimePin(provider="system", version="8.4.1"),
            "composer": RuntimePin(provider="system", version="2.8.4"),
        },
        resources=ResourceBindings(database="devbox-postgres", valkey=LOCAL_VALKEY),
        placement=Placement(
            instance="example-app",
            relative_path="deployments/example-app",
            database_identifier="gimme_example_app",
            cache_prefix="gimme:example-app:",
            site_host="example-app.devbox.local",
        ),
        placement_decision=explicit_placement_decision("devbox", target),
    )
    return ControlState(
        targets={"devbox": target},
        applications={"example-app": application},
        resources={
            "devbox-postgres": ResourceConfig(target="devbox", kind="postgres", version="17.2"),
            "devbox-valkey": ResourceConfig(target="devbox", kind="valkey", version="8.0.1"),
        },
        deployments={"example-app": deployment},
    )


def active_rollout_record() -> Rollout:
    return Rollout(
        deployment="example-app",
        target="devbox",
        generation=17,
        phase="active",
        stable=RolloutArtifact(
            application="example-app", build_id="build_v1_" + "1" * 64,
            commit="a" * 40, artifact_digest="2" * 64, tree_digest="3" * 64,
        ),
        candidate=RolloutArtifact(
            application="example-app", build_id="build_v1_" + "4" * 64,
            commit="b" * 40, artifact_digest="5" * 64, tree_digest="6" * 64,
        ),
        backend_ready=True,
        outcome="ready",
        candidate_health="ready",
        policy_fingerprint="rollout_" + "7" * 64,
        contract_fingerprint="rollout_" + "8" * 64,
        evidence_fingerprint="rollout_" + "9" * 64,
        route_fingerprint="rollout_" + "a" * 64,
    )


def use_store(tmp_path: Path, monkeypatch) -> StateStore:
    selected = StateStore(tmp_path / "state")
    selected.save(sample_state())
    monkeypatch.setattr(server_module, "store", selected)
    return selected


def use_promotion_store(tmp_path: Path, monkeypatch, *, managed: bool = False) -> StateStore:
    state = sample_state()
    original = state.deployments["example-app"]
    source = original.model_copy(update={
        "source": DeploymentSource(kind="commit", ref="a" * 40),
        "placement": Placement(
            instance="source-app",
            relative_path="deployments/source-app",
            database_identifier="gimme_source_app",
            cache_prefix="gimme:source-app:",
            site_host="source-app.devbox.local",
        ),
    })
    destination_update: dict[str, object] = {
        "placement": Placement(
            instance="destination-app",
            relative_path="deployments/destination-app",
            database_identifier="gimme_destination_app",
            cache_prefix="gimme:destination-app:",
            site_host="destination-app.devbox.local",
        ),
    }
    if managed:
        destination_update["workers"] = HorizonWorkerConfig()
        destination_update["resources"] = ResourceBindings(
            database="devbox-postgres",
            valkey=ValkeyBinding(resource="devbox-valkey", uses=["cache", "queue"]),
        )
    destination = original.model_copy(update=destination_update)
    selected = StateStore(tmp_path / "state")
    selected.save(state.model_copy(update={
        "deployments": {"source-app": source, "destination-app": destination}
    }))
    monkeypatch.setattr(server_module, "store", selected)
    return selected


def recovery_state() -> ControlState:
    state = sample_state()
    destination = S3BackupDestination(
        bucket="gimme-backups", region="us-east-1", encryption=SSEAES256()
    )
    deployment = state.deployments["example-app"].model_copy(
        update={"recovery": RecoveryPolicy(destination="primary")}
    )
    return state.model_copy(
        update={
            "backup_destinations": {"primary": destination},
            "deployments": {"example-app": deployment},
        }
    )


def use_recovery_store(tmp_path: Path, monkeypatch) -> StateStore:
    selected = StateStore(tmp_path / "state")
    selected.save(recovery_state())
    monkeypatch.setattr(server_module, "store", selected)
    return selected


def test_deployment_operation_lock_is_reentrant_and_excludes_other_threads(
    tmp_path, monkeypatch
) -> None:
    use_store(tmp_path, monkeypatch)
    started = threading.Event()
    acquired = threading.Event()

    def contender() -> None:
        started.set()
        with server_module._deployment_resource_lock("example-app"):
            acquired.set()

    with server_module._deployment_resource_lock("example-app"):
        with server_module._deployment_resource_lock("example-app"):
            thread = threading.Thread(target=contender)
            thread.start()
            assert started.wait(1)
            assert not acquired.wait(0.05)

    assert acquired.wait(1)
    thread.join(timeout=1)
    assert not thread.is_alive()


class FakeS3:
    def __init__(self, *, versioning: str = "Enabled") -> None:
        self.versioning = versioning
        self.objects: dict[str, bytes] = {}
        self.versions: dict[str, str] = {}
        self.puts = 0
        self.deletes: list[tuple[str, str | None]] = []

    def bucket_versioning(self, destination, credentials) -> str:
        return self.versioning

    def put_object(self, destination, credentials, key, body, sha256) -> ObjectMetadata:
        self.puts += 1
        self.objects[key] = body
        version_id = f"v{self.puts}"
        self.versions[key] = version_id
        return ObjectMetadata(
            bytes=len(body), sha256=sha256, server_side_encryption="AES256",
            version_id=version_id,
        )

    def head_object(self, destination, credentials, key, version_id=None) -> ObjectMetadata | None:
        body = self.objects.get(key)
        if body is None or (version_id is not None and self.versions.get(key) != version_id):
            return None
        return ObjectMetadata(
            bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
            server_side_encryption="AES256", version_id=self.versions[key],
        )

    def get_object(self, destination, credentials, key, version_id=None) -> bytes:
        if version_id is not None and self.versions.get(key) != version_id:
            raise KeyError(key)
        return self.objects[key]

    def delete_object(self, destination, credentials, key, version_id=None) -> None:
        self.deletes.append((key, version_id))
        if version_id is not None and self.versions.get(key) != version_id:
            return
        self.objects.pop(key, None)
        self.versions.pop(key, None)

    def list_keys(self, destination, credentials, prefix) -> list[str]:
        return [key for key in self.objects if key.startswith(prefix)]


def install_fake_target_recovery(
    tmp_path: Path, monkeypatch, adapter: FakeS3, *, calls: list[dict] | None = None,
) -> list[str]:
    """Simulate the fixed Target runner boundary, not its already-unit-tested internals."""
    captures: list[str] = []

    def fake_run(task, name, **kwargs):
        assert task == "gimme:recovery:on-demand"
        assert name == "example-app"
        if calls is not None:
            calls.append(kwargs)
        request_id = kwargs["recovery_on_demand_request_id"]
        state = server_module.store.load()
        deployment = state.deployments[name]
        destination_name = deployment.recovery.destination
        destination = state.backup_destinations[destination_name]
        point_id = recovery_module.recovery_point_id(name, destination_name, request_id)
        existing = recovery_module.find_recovery_point(
            destination_name, destination, None, adapter, name, point_id
        )
        changed = existing is None
        if changed:
            captures.append(request_id)
            dumps = []
            for kind in kwargs["recovery_schedule_authority"]["components"]:
                body = f"{kind}-dump-{len(captures)}".encode()
                path = tmp_path / f"{kind}-{point_id}.dump"
                path.write_bytes(body)
                dumps.append(recovery_module.ComponentDump(
                    kind=kind, local_path=path,
                    sha256=hashlib.sha256(body).hexdigest(), bytes=len(body),
                    resource_version=(
                        kwargs["recovery_schedule_authority"]["resources"][kind]["version"]
                    ),
                    format="pg-custom-v1" if kind == "postgres" else "gimme-valkey-v1",
                    records=None if kind == "postgres" else 0,
                ))
            recovery_module.create_recovery_point(
                destination_name, destination, None, adapter, name, point_id, dumps
            )
            for dump in dumps:
                dump.local_path.unlink()
        retention = recovery_module.enforce_recovery_retention(
            destination_name, destination, None, adapter, name,
            deployment.recovery.retain_last, point_id,
        )
        result = {
            "changed": changed, "recovery_point_id": point_id, "retention": retention,
        }
        encoded = base64.b64encode(json.dumps(
            result, sort_keys=True, separators=(",", ":")
        ).encode()).decode()
        return CommandResult(["dep"], 0, f"GIMME_RECOVERY_RESULT|{encoded}")

    monkeypatch.setattr(server_module, "_run_deployment", fake_run)
    return captures


class FakeAWSIdentity:
    def __init__(self) -> None:
        self.roles: list[str] = []

    def known_regions(self) -> set[str]:
        return {"us-east-1"}

    def verify_role(self, account, role_arn) -> None:
        self.roles.append(role_arn)


def test_deployment_diagnostics_return_only_allowlisted_structured_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    use_store(tmp_path, monkeypatch)
    output = """task gimme:diagnose:deployment
GIMME_DIAGNOSTIC|release|ready|aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
GIMME_DIAGNOSTIC|database|failed|none
GIMME_DIAGNOSTIC|laravel-log|ready|bytes=120,age_seconds=3,errors=2
GIMME_DIAGNOSTIC|health.primary|ready|status=200
GIMME_DIAGNOSTIC|bad/check|ready|secret-value
password=must-not-escape
"""
    monkeypatch.setattr(
        server_module,
        "_run_deployment",
        lambda *args, **kwargs: CommandResult(["dep"], 0, output),
    )

    result = server_module.diagnose_deployment("example-app")

    assert result["healthy"] is False
    assert [check["check"] for check in result["checks"]] == [
        "release", "database", "laravel-log", "health.primary"
    ]
    assert "password" not in str(result)
    assert "secret-value" not in str(result)


def test_provider_account_and_secret_store_registration_are_planned_and_secret_safe(
    tmp_path: Path, monkeypatch
) -> None:
    selected = use_store(tmp_path, monkeypatch)
    aws = FakeAWSIdentity()
    monkeypatch.setattr(server_module, "aws_secrets", aws)
    account = AWSProviderAccount(
        account_id="123456789012",
        inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
        resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
    )

    account_plan = server_module.plan_register_provider_account("production", account)
    server_module.register_provider_account(
        "production", account, str(account_plan["plan_id"])
    )
    definition = AWSSecretsManagerStore(
        provider_account="production", region="us-east-1", prefix="gimme/apps"
    )
    store_plan = server_module.plan_register_secret_store("applications", definition)
    server_module.register_secret_store(
        "applications", definition, str(store_plan["plan_id"])
    )

    state = selected.load()
    assert state.provider_accounts["production"] == account
    assert state.secret_stores["applications"] == definition
    assert len(aws.roles) == 6  # both roles on each account plan/apply; inspection for store
    journal = (selected.root / "operations.jsonl").read_text()
    assert "123456789012" not in journal
    assert "arn:aws" not in journal


def test_registering_an_account_never_assumes_its_destructive_role(
    tmp_path: Path, monkeypatch
) -> None:
    selected = use_store(tmp_path, monkeypatch)
    aws = FakeAWSIdentity()
    monkeypatch.setattr(server_module, "aws_secrets", aws)
    destructive = "arn:aws:iam::123456789012:role/gimme-destroy"
    account = AWSProviderAccount(
        account_id="123456789012",
        inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
        resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
        destructive_role_arn=destructive,
    )

    plan = server_module.plan_register_provider_account("production", account)
    server_module.register_provider_account("production", account, str(plan["plan_id"]))

    assert selected.load().provider_accounts["production"].destructive_role_arn == destructive
    assert destructive not in aws.roles and len(aws.roles) == 4


def test_control_plane_registration_mcp_adapter_uses_current_orchestrator(
    monkeypatch,
) -> None:
    seen: list[tuple[str, object]] = []

    class FakeRegistrationOrchestrator:
        def plan_register_provider_account(self, name, definition):
            seen.append((name, definition))
            return {"kind": "provider_account_registration", "plan_id": "plan_" + "0" * 20}

    account = AWSProviderAccount(
        account_id="123456789012",
        inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
        resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
    )
    monkeypatch.setattr(
        server_module,
        "_control_plane_registration_orchestrator",
        FakeRegistrationOrchestrator,
    )

    result = server_module.plan_register_provider_account("production", account)

    assert result["kind"] == "provider_account_registration"
    assert seen == [("production", account)]


def test_target_runtime_orchestration_preserves_fixed_task_order(
    tmp_path: Path, monkeypatch
) -> None:
    selected = use_store(tmp_path, monkeypatch)
    packages = selected.target("devbox").stack.packages
    preflight = "\n".join(
        [*(f"GIMME_PACKAGE|{name}|installed|installed" for name in packages),
         "GIMME_APT_BUSY|no", "GIMME_HELPER|ready"]
    )
    calls: list[str] = []

    def fake_run(task, *args, **kwargs):
        calls.append(task)
        if task == "gimme:preflight:stack":
            return CommandResult(["dep"], 0, preflight)
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    stack_plan = server_module.plan_target_stack("devbox")
    server_module.apply_target_stack("devbox", str(stack_plan["plan_id"]))
    runtime_plan = server_module.plan_deployment_runtimes("example-app")
    server_module.apply_deployment_runtimes(
        "example-app", str(runtime_plan["plan_id"])
    )

    assert calls == [
        "gimme:preflight:stack",
        "gimme:preflight:stack",
        "gimme:provision:stack",
        "gimme:provision:runtimes",
        "gimme:preflight:runtimes",
    ]


def test_managed_valkey_recovery_mcp_adapter_uses_current_orchestrator(
    monkeypatch,
) -> None:
    seen: list[tuple[str, str]] = []

    class FakeManagedValkeyRecoveryOrchestrator:
        def plan_restore_resource(self, name, snapshot):
            seen.append((name, snapshot))
            return {"kind": "valkey_restore", "plan_id": "plan_" + "0" * 20}

    monkeypatch.setattr(
        server_module,
        "_managed_valkey_recovery_orchestrator",
        FakeManagedValkeyRecoveryOrchestrator,
    )

    result = server_module.plan_restore_resource("shared-cache", "snapshot-1")

    assert result["kind"] == "valkey_restore"
    assert seen == [("shared-cache", "snapshot-1")]


def test_resource_retirement_mcp_adapter_uses_current_orchestrator(
    monkeypatch,
) -> None:
    seen: list[str] = []

    class FakeResourceRetirementOrchestrator:
        def plan_cleanup_resource(self, name):
            seen.append(name)
            return {"kind": "resource_cleanup", "plan_id": "plan_" + "0" * 20}

    monkeypatch.setattr(
        server_module,
        "_resource_retirement_orchestrator",
        FakeResourceRetirementOrchestrator,
    )

    result = server_module.plan_cleanup_resource("shared-cache")

    assert result["kind"] == "resource_cleanup"
    assert seen == ["shared-cache"]


async def test_hard_v7_tool_surface() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()
        resources = await client.list_resources()
        templates = await client.list_resource_templates()

    names = {tool.name for tool in tools}
    assert "register_environment" not in names
    assert "register_app" not in names
    assert names >= {
        "plan_state_migration",
        "apply_state_migration",
        "register_target",
        "register_application",
        "register_resource",
        "register_deployment",
        "plan_register_deployment",
        "inspect_fleet",
        "inspect_rollout",
        "plan_start_rollout",
        "start_rollout",
        "plan_rollout_weights",
        "apply_rollout_weights",
        "plan_complete_rollout",
        "complete_rollout",
        "plan_reverse_rollout",
        "reverse_rollout",
        "plan_target_stack",
        "apply_target_stack",
        "plan_deployment_runtimes",
        "apply_deployment_runtimes",
        "plan_deployment_resources",
        "apply_deployment_resources",
        "plan_deployment",
        "apply_deployment",
        "plan_rollback_deployment",
        "plan_promotion",
        "promote_deployment",
        "plan_artisan",
        "run_artisan",
        "diagnose_deployment",
        "list_operations",
        "plan_register_provider_account",
        "register_provider_account",
        "plan_register_secret_store",
        "register_secret_store",
        "plan_register_backup_destination",
        "register_backup_destination",
        "plan_register_artifact_store",
        "register_artifact_store",
        "plan_update_artifact_store",
        "update_artifact_store",
        "plan_remove_artifact_store",
        "remove_artifact_store",
        "plan_verify_artifact_store_publisher",
        "verify_artifact_store_publisher",
        "plan_verify_artifact_store_reader",
        "verify_artifact_store_reader",
        "list_artifact_stores",
        "plan_build_artifact",
        "build_artifact",
        "list_artifacts",
        "plan_create_recovery_point",
        "create_recovery_point",
        "list_recovery_points",
        "get_recovery_schedule_status",
        "list_restores",
        "plan_restore_deployment",
        "apply_restore_deployment",
        "plan_verify_restore",
        "apply_verify_restore",
    }
    assert {str(resource.uri) for resource in resources} == {
        "gimme://state", "gimme://operations", "gimme://fleet"
    }
    assert {template.uriTemplate for template in templates} == {
        "gimme://targets/{name}",
        "gimme://applications/{name}",
        "gimme://applications/{name}/artifacts/{build_id}",
        "gimme://resources/{name}",
        "gimme://deployments/{name}",
        "gimme://deployments/{name}/rollout",
        "gimme://operations/{correlation_id}",
        "gimme://provider-accounts/{name}",
        "gimme://secret-stores/{name}",
            "gimme://backup-destinations/{name}",
            "gimme://artifact-stores/{name}",
        "gimme://aws-networks/{name}/valkey-options",
        "gimme://deployments/{name}/restores/{request_id}",
        "gimme://deployments/{name}/recovery-schedule",
    }
    assert all(tool.annotations is not None for tool in tools)
    reference = (Path(__file__).parents[1] / "docs" / "reference" / "mcp.md").read_text()
    assert all(f"`{name}`" in reference for name in names)
    assert all(f"`{template.uriTemplate}`" in reference for template in templates)


async def test_recovery_tool_schemas_remain_stable_across_module_extraction() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    expected = {
        "get_recovery_schedule_status": ({"name"}, {"name"}, True),
        "plan_create_recovery_point": ({"name", "request_id"}, {"name", "request_id"}, True),
        "create_recovery_point": (
            {"name", "request_id", "plan_id"},
            {"name", "request_id", "plan_id"},
            False,
        ),
        "list_recovery_points": ({"name"}, {"name"}, True),
        "list_restores": ({"name"}, {"name"}, True),
        "plan_restore_deployment": (
            {"name", "recovery_point_id", "request_id", "components"},
            {"name", "recovery_point_id", "request_id"},
            True,
        ),
        "apply_restore_deployment": (
            {
                "name", "recovery_point_id", "request_id", "plan_id",
                "confirmation", "components",
            },
            {"name", "recovery_point_id", "request_id", "plan_id", "confirmation"},
            False,
        ),
        "plan_verify_restore": ({"name", "request_id"}, {"name", "request_id"}, True),
        "apply_verify_restore": (
            {"name", "request_id", "plan_id"},
            {"name", "request_id", "plan_id"},
            False,
        ),
        "plan_delete_recovery_point": (
            {"name", "recovery_point_id"},
            {"name", "recovery_point_id"},
            True,
        ),
        "delete_recovery_point": (
            {
                "name", "recovery_point_id", "plan_id", "confirmation",
                "last_recovery_point_confirmation",
            },
            {"name", "recovery_point_id", "plan_id", "confirmation"},
            False,
        ),
    }
    for name, (properties, required, read_only) in expected.items():
        tool = tools[name]
        assert set(tool.inputSchema["properties"]) == properties
        assert set(tool.inputSchema["required"]) == required
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is read_only


def test_recovery_orchestrator_owns_component_selection() -> None:
    def unused(*args, **kwargs):
        return None

    orchestrator = RecoveryOrchestrator(
        backup_s3=None,
        elasticache_valkey=None,
        context=unused,
        managed_database_issues=unused,
        backup_destination_credentials=unused,
        deployment_resource_lock=unused,
        assert_plan=unused,
        run_deployment=unused,
        valkey_runtime=unused,
        postgres_capture_credential=unused,
        bounded_marker_values=unused,
        journal=unused,
    )

    manifest = [{"kind": "postgres"}, {"kind": "valkey"}]
    assert orchestrator._normalize_restore_components(manifest, None) == [
        "postgres", "valkey",
    ]
    assert orchestrator._normalize_restore_components(manifest, ["valkey"]) == ["valkey"]
    with pytest.raises(RecoveryError, match="restore_component_missing"):
        orchestrator._normalize_restore_components([{"kind": "postgres"}], ["valkey"])


def test_recovery_mcp_adapter_delegates_to_current_orchestrator(monkeypatch) -> None:
    seen = []

    class FakeRecoveryOrchestrator:
        def list_restores(self, name):
            seen.append(name)
            return {"deployment": name, "restores": []}

    monkeypatch.setattr(server_module, "_recovery_orchestrator", FakeRecoveryOrchestrator)

    assert server_module.list_restores("example-app") == {
        "deployment": "example-app",
        "restores": [],
    }
    assert seen == ["example-app"]


async def test_managed_resource_tool_schemas_remain_stable_across_module_extraction() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    expected = {
        "plan_apply_resource": ({"name"}, {"name"}, True),
        "apply_resource": ({"name", "plan_id"}, {"name", "plan_id"}, False),
        "inspect_resource": ({"name"}, {"name"}, True),
        "plan_bind_resource": ({"name"}, {"name"}, True),
        "bind_resource": ({"name", "plan_id"}, {"name", "plan_id"}, False),
    }
    for name, (properties, required, read_only) in expected.items():
        tool = tools[name]
        assert set(tool.inputSchema["properties"]) == properties
        assert set(tool.inputSchema["required"]) == required
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is read_only


async def test_deployment_release_tool_schemas_remain_stable_across_module_extraction() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    expected = {
        "plan_deployment": ({"name"}, {"name"}, True),
        "apply_deployment": ({"name", "plan_id"}, {"name", "plan_id"}, False),
        "list_releases": ({"name"}, {"name"}, True),
        "plan_rollback_deployment": ({"name"}, {"name"}, True),
        "rollback_deployment": (
            {"name", "plan_id", "confirmation"},
            {"name", "plan_id", "confirmation"},
            False,
        ),
        "plan_promotion": ({"source", "destination"}, {"source", "destination"}, True),
        "promote_deployment": (
            {"source", "destination", "plan_id"},
            {"source", "destination", "plan_id"},
            False,
        ),
    }
    for name, (properties, required, read_only) in expected.items():
        tool = tools[name]
        assert set(tool.inputSchema["properties"]) == properties
        assert set(tool.inputSchema["required"]) == required
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is read_only


async def test_deployment_resource_tool_schemas_remain_stable_across_extraction() -> None:
    async with Client(mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}

    expected = {
        "plan_deployment_resources": ({"name"}, {"name"}, True),
        "apply_deployment_resources": (
            {"name", "plan_id"}, {"name", "plan_id"}, False,
        ),
    }
    for name, (properties, required, read_only) in expected.items():
        tool = tools[name]
        assert set(tool.inputSchema["properties"]) == properties
        assert set(tool.inputSchema["required"]) == required
        assert tool.inputSchema["additionalProperties"] is False
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is read_only


def test_deployment_resource_mcp_adapter_uses_current_orchestrator(monkeypatch) -> None:
    seen = []

    class FakeDeploymentResourceOrchestrator:
        def plan_deployment_resources(self, name):
            seen.append(name)
            return {"kind": "deployment_resources", "plan_id": "plan_" + "0" * 20}

    monkeypatch.setattr(
        server_module,
        "_deployment_resource_orchestrator",
        FakeDeploymentResourceOrchestrator,
    )
    monkeypatch.setattr(server_module, "_require_no_rollout", lambda *_names: None)

    assert server_module.plan_deployment_resources("example-app")["kind"] == (
        "deployment_resources"
    )
    assert seen == ["example-app"]


def test_deployment_release_orchestrator_owns_listing() -> None:
    calls: list[str] = []

    def run(task, name, **_kwargs):
        calls.append(f"{task}:{name}")
        return CommandResult([name], 0, task)

    orchestrator = DeploymentReleaseOrchestrator(
        store=None,
        context=None,
        run_deployment=run,
        secret_plan=None,
        dns_issues=None,
        managed_database_issues=None,
        valkey_runtime=None,
        deployment_resource_lock=None,
        deployment_resource_locks=None,
        assert_plan=None,
        replace=None,
        result=lambda value: value.as_dict(),
    )

    assert orchestrator.list_releases("example-app")["output"] == "releases"
    assert calls == ["releases:example-app"]


def test_deployment_release_mcp_adapter_uses_current_orchestrator(monkeypatch) -> None:
    seen = []

    class FakeDeploymentReleaseOrchestrator:
        def list_releases(self, name):
            seen.append(name)
            return {"deployment": name, "releases": []}

    monkeypatch.setattr(
        server_module,
        "_deployment_release_orchestrator",
        FakeDeploymentReleaseOrchestrator,
    )

    assert server_module.list_releases("example-app") == {
        "deployment": "example-app",
        "releases": [],
    }
    assert seen == ["example-app"]


def test_managed_resource_orchestrator_owns_local_inspection() -> None:
    state = sample_state()
    orchestrator = ManagedResourceOrchestrator(
        store=SimpleNamespace(load=lambda: state),
        rds_postgres=None,
        elasticache_valkey=None,
        deployment_resource_locks=None,
        assert_plan=None,
        runner=None,
        context=None,
    )

    assert orchestrator.inspect_resource("devbox-postgres") == {
        "resource": "devbox-postgres",
        "provider": "target_local",
        "target": "devbox",
        "kind": "postgres",
        "version": "17.2",
    }


def test_managed_resource_mcp_adapter_delegates_to_current_orchestrator(
    monkeypatch,
) -> None:
    seen = []

    class FakeManagedResourceOrchestrator:
        def inspect_resource(self, name):
            seen.append(name)
            return {"resource": name, "provider": "target_local"}

    monkeypatch.setattr(
        server_module,
        "_managed_resource_orchestrator",
        FakeManagedResourceOrchestrator,
    )

    assert server_module.inspect_resource("devbox-postgres") == {
        "resource": "devbox-postgres",
        "provider": "target_local",
    }
    assert seen == ["devbox-postgres"]


def test_resource_binding_mcp_adapter_delegates_to_current_orchestrator(
    monkeypatch,
) -> None:
    seen = []

    class FakeManagedResourceOrchestrator:
        def plan_bind_resource(self, name):
            seen.append(name)
            return {"kind": "resource_binding", "plan_id": "plan_" + "0" * 20}

    monkeypatch.setattr(
        server_module,
        "_managed_resource_orchestrator",
        FakeManagedResourceOrchestrator,
    )

    assert server_module.plan_bind_resource("example-app")["kind"] == "resource_binding"
    assert seen == ["example-app"]


def test_register_deployment_allocates_immutable_placement(tmp_path, monkeypatch) -> None:
    selected = use_store(tmp_path, monkeypatch)
    definition = DeploymentRegistration(
        application="example-app",
        target="devbox",
        stage="preview",
        release_mode="source",
        source=DeploymentSource(kind="branch", ref="feature/demo"),
        app_env="local",
        runtimes=sample_state().deployments["example-app"].runtimes,
        resources=sample_state().deployments["example-app"].resources,
    )
    registration = server_module.plan_register_deployment("example-preview", definition)
    result = server_module.register_deployment(
        "example-preview", definition, str(registration["plan_id"])
    )
    placement = selected.deployment("example-preview").placement

    assert result["placement"] == placement.model_dump(mode="json")
    assert placement.relative_path == "deployments/example-preview"
    events = server_module.list_operations(
        operation="register_deployment", subject="example-preview"
    )["events"]
    assert [event["phase"] for event in events[:3]] == ["outcome", "apply", "plan"]
    assert events[0]["status"] == "succeeded"
    assert events[1]["plan_correlation_id"] == events[2]["correlation_id"]
    update = definition.model_copy(update={"source": DeploymentSource(kind="branch", ref="next")})
    plan = server_module.plan_update_deployment("example-preview", update)
    server_module.update_deployment("example-preview", update, str(plan["plan_id"]))
    assert selected.deployment("example-preview").placement == placement


def test_deployment_update_rejects_stale_plan(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    current = server_module.store.deployment("example-app")
    definition = DeploymentRegistration.from_deployment(current).model_copy(
        update={"source": DeploymentSource(kind="branch", ref="next")}
    )
    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.update_deployment("example-app", definition, "plan_" + "0" * 20)

    events = server_module.list_operations(operation="update_deployment")["events"]
    assert events[0]["status"] == "stale"
    assert events[0]["error_code"] == "stale_plan"
    assert events[1]["phase"] == "apply"


def test_deployment_update_rejects_plan_after_execution_code_changes(
    tmp_path, monkeypatch
) -> None:
    use_store(tmp_path, monkeypatch)
    current = server_module.store.deployment("example-app")
    definition = DeploymentRegistration.from_deployment(current).model_copy(
        update={"source": DeploymentSource(kind="branch", ref="next")}
    )
    monkeypatch.setattr(
        control_plans_module, "execution_fingerprint", lambda: "exec_" + "a" * 64
    )
    plan = server_module.plan_update_deployment("example-app", definition)

    monkeypatch.setattr(
        control_plans_module, "execution_fingerprint", lambda: "exec_" + "b" * 64
    )

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.update_deployment("example-app", definition, str(plan["plan_id"]))


def test_plan_and_apply_have_linked_secret_safe_journal_events(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    current = server_module.store.deployment("example-app")
    definition = DeploymentRegistration.from_deployment(current).model_copy(
        update={
            "source": DeploymentSource(kind="branch", ref="secret-client-branch"),
            "variables": {"PRIVATE_MARKER": "do-not-journal-this"},
        }
    )

    plan = server_module.plan_update_deployment("example-app", definition)
    result = server_module.update_deployment("example-app", definition, str(plan["plan_id"]))
    events = server_module.list_operations(operation="update_deployment")["events"]

    assert result["correlation_id"] == events[0]["correlation_id"]
    assert events[0]["phase"] == "outcome"
    assert events[0]["plan_correlation_id"] == plan["correlation_id"]
    assert events[1]["phase"] == "apply"
    assert events[2]["phase"] == "plan"
    journal = (tmp_path / "state" / "operations.jsonl").read_text()
    assert "secret-client-branch" not in journal
    assert "do-not-journal-this" not in journal


def test_external_secret_canary_never_crosses_mcp_or_failure_surfaces(
    tmp_path, monkeypatch
) -> None:
    selected = use_store(tmp_path, monkeypatch)
    canary = "gimme-secret-canary-must-not-escape"
    account = AWSProviderAccount(
        account_id="123456789012",
        inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
        resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
    )
    external = AWSSecretsManagerStore(
        provider_account="production", region="us-east-1", prefix="gimme/apps"
    )
    state = selected.load()
    deployment = state.deployments["example-app"].model_copy(update={
        "secrets": {
            "PRIVATE_TOKEN": SecretReference(
                store="external", secret="private/identity", field="TOKEN"
            )
        }
    })
    selected.save(state.model_copy(update={
        "provider_accounts": {"production": account},
        "secret_stores": {**state.secret_stores, "external": external},
        "deployments": {"example-app": deployment},
    }))

    class CanaryAWS:
        def known_regions(self):
            return {"us-east-1"}

        def verify_role(self, account, role_arn):
            return None

        def describe(self, account, store_name, store, secret):
            return SecretMetadata("version-private", "private-arn")

        def resolve(self, account, store_name, store, secret, version_id):
            return json.dumps({"TOKEN": canary})

    monkeypatch.setattr(server_module, "aws_secrets", CanaryAWS())
    temporary_paths: list[Path] = []
    fail_activation = False

    def fake_run(task, *args, **kwargs):
        nonlocal fail_activation
        if task == "gimme:provision:app":
            secret_file = kwargs["secret_file"]
            assert json.loads(secret_file.read_text()) == {"PRIVATE_TOKEN": canary}
            temporary_paths.append(secret_file)
            if fail_activation:
                raise RuntimeError(f"private rollback output: {canary}")
        if task == "gimme:diagnose:deployment":
            return CommandResult(
                ["dep", canary], 0,
                f"{canary}\nGIMME_DIAGNOSTIC|release|ready|{'a' * 40}",
            )
        return CommandResult(["dep", canary], 0, f"private output: {canary}")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_deployment_resources("example-app")

    applied = server_module.apply_deployment_resources(
        "example-app", str(plan["plan_id"])
    )
    diagnostics = server_module.diagnose_deployment("example-app")

    assert applied["changed"] is True
    assert all(not path.exists() for path in temporary_paths)
    public = [
        plan, applied, diagnostics,
        server_module.secret_store_resource("external"),
        server_module.list_operations(subject="example-app"),
    ]
    assert canary not in json.dumps(public, sort_keys=True, default=str)
    assert "private/identity" not in json.dumps(public, sort_keys=True, default=str)

    fail_activation = True
    retry = server_module.plan_deployment_resources("example-app")
    with pytest.raises(
        server_module.SecretError, match="^deployment_secret_activation_failed$"
    ) as failure:
        server_module.apply_deployment_resources(
            "example-app", str(retry["plan_id"])
        )
    assert canary not in str(failure.value)
    assert all(not path.exists() for path in temporary_paths)

    assert canary not in selected.state_path.read_text()
    durable = "\n".join(path.read_text() for path in (
        selected.root / "operations.jsonl",
        selected.root / "applied-secrets" / "example-app.json",
    ))
    assert canary not in durable
    assert "private/identity" not in durable


def test_failed_deployment_resource_activation_does_not_save_manifest(
    tmp_path, monkeypatch
) -> None:
    use_store(tmp_path, monkeypatch)
    canary = "activation-secret-must-not-escape"
    temporary_paths: list[Path] = []
    saved: list[list[dict[str, str]]] = []
    monkeypatch.setattr(
        deployment_resource_module,
        "resolve_planned_secret_references",
        lambda *args, **kwargs: {"PRIVATE_TOKEN": canary},
    )
    monkeypatch.setattr(
        deployment_resource_module,
        "save_applied_secret_manifest",
        lambda root, name, manifest: saved.append(manifest),
    )

    def fail_run(task, *args, **kwargs):
        if task == "gimme:provision:app":
            secret_file = kwargs["secret_file"]
            temporary_paths.append(secret_file)
            assert json.loads(secret_file.read_text()) == {"PRIVATE_TOKEN": canary}
            raise RuntimeError(f"private activation output: {canary}")
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fail_run)
    plan = server_module.plan_deployment_resources("example-app")

    with pytest.raises(
        server_module.SecretError, match="^deployment_secret_activation_failed$"
    ) as failure:
        server_module.apply_deployment_resources(
            "example-app", str(plan["plan_id"])
        )

    assert saved == []
    assert all(not path.exists() for path in temporary_paths)
    assert canary not in str(failure.value)


def test_deploy_rechecks_revision_and_rendered_plan(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run(task, *args, **kwargs):
        arguments = tuple(kwargs.get("arguments", ()))
        calls.append((task, arguments))
        if task == "gimme:resolve-revision":
            return CommandResult(["dep"], 0, "GIMME_REVISION|" + "a" * 40)
        if task == "gimme:preflight:processes":
            return CommandResult(
                ["dep"], 0,
                "GIMME_PROCESS_HELPER|ready\nGIMME_PCNTL|ready\nGIMME_POSIX|ready",
            )
        if arguments == ("--plan",):
            return CommandResult(["dep"], 0, "candidate -> health -> symlink -> live")
        return CommandResult(["dep"], 0, "deployed")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_deployment("example-app")
    result = server_module.apply_deployment("example-app", str(plan["plan_id"]))

    assert result["output"] == "deployed"
    assert calls[-1] == ("deploy", ())
    assert calls.count(("deploy", ("--plan",))) == 2


def test_deploy_blocks_before_activation_when_process_helper_is_stale(
    tmp_path, monkeypatch
) -> None:
    selected = use_store(tmp_path, monkeypatch)
    state = selected.load()
    managed = state.deployments["example-app"].model_copy(update={
        "workers": HorizonWorkerConfig(),
        "scheduler": SchedulerConfig(),
        "resources": ResourceBindings(
            database="devbox-postgres",
            valkey=ValkeyBinding(resource="devbox-valkey", uses=["cache", "queue"]),
        ),
    })
    selected.save(state.model_copy(update={
        "deployments": {**state.deployments, "example-app": managed}
    }))
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run(task, *args, **kwargs):
        arguments = tuple(kwargs.get("arguments", ()))
        calls.append((task, arguments))
        if task == "gimme:resolve-revision":
            return CommandResult(["dep"], 0, "GIMME_REVISION|" + "b" * 40)
        if task == "gimme:preflight:processes":
            return CommandResult(
                ["dep"], 0,
                "GIMME_PROCESS_HELPER|bootstrap_required\n"
                "GIMME_PCNTL|ready\nGIMME_POSIX|ready",
            )
        if arguments == ("--plan",):
            return CommandResult(["dep"], 0, "candidate -> health -> symlink -> live")
        return CommandResult(["dep"], 0, "unexpected mutation")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_deployment("example-app")

    assert plan["ready"] is False
    assert plan["readiness_issues"] == ["privileged process helper requires target bootstrap"]
    with pytest.raises(ValueError, match="deployment is not ready"):
        server_module.apply_deployment("example-app", str(plan["plan_id"]))
    assert ("deploy", ()) not in calls


def test_promotion_pins_exact_live_revision_only_after_success(tmp_path, monkeypatch) -> None:
    selected = use_promotion_store(tmp_path, monkeypatch)
    revision = "b" * 40
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    def fake_run(task, *args, **kwargs):
        name = kwargs["deployment_name"]
        arguments = tuple(kwargs.get("arguments", ()))
        calls.append((task, name, arguments))
        if task == "gimme:current-revision":
            return CommandResult([name], 0, f"GIMME_CURRENT_REVISION|{revision}")
        if task == "gimme:preflight:runtimes":
            return CommandResult([name], 0, "runtime-ready")
        if task == "deploy" and arguments == ("--plan",):
            return CommandResult([name], 0, "candidate -> health -> symlink -> live")
        return CommandResult([name], 0, "deployed")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_promotion("source-app", "destination-app")
    result = server_module.promote_deployment(
        "source-app", "destination-app", str(plan["plan_id"])
    )

    assert result["output"] == "deployed"
    assert selected.deployment("destination-app").source == DeploymentSource(
        kind="commit", ref=revision
    )
    assert calls.count(("deploy", "destination-app", ("--plan",))) == 2
    assert calls[-1] == ("deploy", "destination-app", ())


def test_promotion_rejects_an_unbounded_live_revision_marker(tmp_path, monkeypatch) -> None:
    use_promotion_store(tmp_path, monkeypatch)

    def fake_run(task, *args, **kwargs):
        assert task == "gimme:current-revision"
        return CommandResult([kwargs["deployment_name"]], 0, "GIMME_CURRENT_REVISION|main")

    monkeypatch.setattr(server_module.runner, "run", fake_run)

    with pytest.raises(RuntimeError, match="no exact current revision"):
        server_module.plan_promotion("source-app", "destination-app")


@pytest.mark.parametrize("failure", ["deploy", "processes"])
def test_failed_promotion_does_not_pin_destination_source(
    tmp_path, monkeypatch, failure
) -> None:
    selected = use_promotion_store(
        tmp_path, monkeypatch, managed=failure == "processes"
    )
    before = selected.deployment("destination-app").source
    revision = "c" * 40

    def fake_run(task, *args, **kwargs):
        name = kwargs["deployment_name"]
        arguments = tuple(kwargs.get("arguments", ()))
        if task == "gimme:current-revision":
            return CommandResult([name], 0, f"GIMME_CURRENT_REVISION|{revision}")
        if task == "gimme:preflight:runtimes":
            return CommandResult([name], 0, "runtime-ready")
        if task == "gimme:preflight:processes":
            return CommandResult(
                [name],
                0,
                "GIMME_PROCESS_HELPER|ready\nGIMME_PCNTL|ready\nGIMME_POSIX|ready",
            )
        if task == "deploy" and arguments == ("--plan",):
            return CommandResult([name], 0, "candidate -> health -> symlink -> live")
        if task == failure or (failure == "processes" and task == "gimme:provision:processes"):
            raise RuntimeError(f"{failure} failed")
        return CommandResult([name], 0, "deployed")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_promotion("source-app", "destination-app")

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        server_module.promote_deployment(
            "source-app", "destination-app", str(plan["plan_id"])
        )
    assert selected.deployment("destination-app").source == before


def test_artisan_is_deployment_scoped_and_plan_gated(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    calls: list[dict] = []

    def fake_run(task, *args, **kwargs):
        calls.append({"task": task, **kwargs})
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_artisan("example-app", "migrate", ["--force"])
    server_module.run_artisan("example-app", "migrate", str(plan["plan_id"]), ["--force"])
    assert calls[-1]["artisan_command"] == "migrate"
    assert calls[-1]["artisan_arguments"] == ["--force"]


def test_rollout_blocks_schema_artisan_but_keeps_ordinary_commands_on_stable(
    tmp_path, monkeypatch
) -> None:
    selected = use_store(tmp_path, monkeypatch)
    rollout = active_rollout_record()
    selected.update(lambda state: state.model_copy(update={
        "rollouts": {"example-app": rollout}
    }))

    with pytest.raises(ValueError, match="schema-changing Artisan"):
        server_module.plan_artisan("example-app", "migrate", ["--force"])
    assert server_module.plan_artisan("example-app", "about")["kind"] == "artisan"


def test_recoverable_rollout_blocks_ordinary_mutation_and_pruning_entrypoints(
    tmp_path, monkeypatch
) -> None:
    selected = use_store(tmp_path, monkeypatch)
    selected.update(lambda state: state.model_copy(update={
        "rollouts": {"example-app": active_rollout_record()}
    }))
    guarded = [
        lambda: server_module.plan_deployment("example-app"),
        lambda: server_module.plan_rollback_deployment("example-app"),
        lambda: server_module.plan_promotion("example-app", "missing"),
        lambda: server_module.plan_remove_deployment("example-app"),
        lambda: server_module.plan_update_deployment("example-app", None),
        lambda: server_module.plan_deployment_resources("example-app"),
        lambda: server_module.plan_deployment_runtimes("example-app"),
    ]

    for operation in guarded:
        with pytest.raises(ValueError, match="operation blocked by rollout"):
            operation()


def test_target_service_status_passes_the_service_as_a_config_override(
    tmp_path, monkeypatch
) -> None:
    use_store(tmp_path, monkeypatch)
    calls: list[tuple[str, tuple[str, ...]]] = []

    def fake_run(task, *args, **kwargs):
        calls.append((task, tuple(kwargs.get("arguments", ()))))
        return CommandResult(["dep"], 0, "active (running)")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    server_module.target_service_status("devbox", "postgresql")

    # A raw "service=postgresql" positional token is parsed by Deployer as a host
    # selector filter, not a config override, and get('gimme_service') never sees it.
    assert calls[-1] == ("gimme:service:status", ("-o", "gimme_service=postgresql"))


def test_non_artisan_deployment_does_not_receive_partial_artisan_context(
    tmp_path, monkeypatch
) -> None:
    use_store(tmp_path, monkeypatch)
    captured: dict = {}

    def fake_run(task, *args, **kwargs):
        captured.update(kwargs)
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    server_module._run_deployment("gimme:preflight:frontend", "example-app")
    assert captured["artisan_command"] is None
    assert captured["artisan_arguments"] is None
    assert captured["artisan_allowed_commands"] is None


def test_state_resource_does_not_decrypt_secrets(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    value = server_module.desired_state()
    assert value["schema_version"] == 8
    assert "deployments" in value


def test_backup_destination_registration_verifies_and_persists(tmp_path, monkeypatch) -> None:
    selected = use_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    definition = S3BackupDestination(
        bucket="gimme-backups", region="us-east-1", encryption=SSEAES256()
    )

    plan = server_module.plan_register_backup_destination("primary", definition)
    assert plan["preflight_verified"] is False
    assert adapter.objects == {}, "plan must never make a live destination call"
    server_module.register_backup_destination("primary", definition, str(plan["plan_id"]))

    state = selected.load()
    assert state.backup_destinations["primary"] == definition
    assert adapter.objects == {}, "the preflight probe object must not be left behind"


def test_backup_destination_registration_rejects_unversioned_bucket(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)
    monkeypatch.setattr(server_module, "backup_s3", FakeS3(versioning="Suspended"))
    definition = S3BackupDestination(
        bucket="gimme-backups", region="us-east-1", encryption=SSEAES256()
    )

    plan = server_module.plan_register_backup_destination("primary", definition)
    with pytest.raises(RecoveryError, match="versioning_disabled"):
        server_module.register_backup_destination("primary", definition, str(plan["plan_id"]))
    assert "primary" not in server_module.store.load().backup_destinations


def test_credential_reference_backup_destination_defers_preflight_to_apply(
    tmp_path, monkeypatch
) -> None:
    use_store(tmp_path, monkeypatch)
    monkeypatch.setattr(server_module, "backup_s3", FakeS3(versioning="Suspended"))
    monkeypatch.setattr(
        server_module, "_backup_destination_credentials",
        lambda state, definition: (None, ("fake-access-key", "fake-secret-key")),
    )
    definition = S3BackupDestination(
        bucket="gimme-backups", region="us-east-1", encryption=SSEAES256(),
        auth=CredentialReferenceBackupAuth(
            access_key_id=SecretReference(
                store="local-sops", secret="minio", field="access_key_id"
            ),
            secret_access_key=SecretReference(
                store="local-sops", secret="minio", field="secret_access_key"
            ),
        ),
    )

    plan = server_module.plan_register_backup_destination("primary", definition)
    assert plan["preflight_verified"] is False

    with pytest.raises(RecoveryError, match="versioning_disabled"):
        server_module.register_backup_destination("primary", definition, str(plan["plan_id"]))
    assert "primary" not in server_module.store.load().backup_destinations


def test_backup_destination_removal_is_blocked_while_referenced(tmp_path, monkeypatch) -> None:
    use_recovery_store(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="still referenced"):
        server_module.plan_remove_backup_destination("primary")


def test_create_recovery_point_end_to_end_and_duplicate_apply_is_a_no_op(
    tmp_path, monkeypatch
) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    calls: list[dict] = []
    captures = install_fake_target_recovery(tmp_path, monkeypatch, adapter, calls=calls)

    plan = server_module.plan_create_recovery_point("example-app", "req-1")
    result = server_module.create_recovery_point(
        "example-app", "req-1", str(plan["plan_id"])
    )
    assert result["changed"] is True
    assert result["retention"] == {
        "outcome": "succeeded", "error_code": None, "deleted": 0, "remaining": 1,
    }
    assert len(calls) == 1
    assert captures == ["req-1"]

    duplicate = server_module.create_recovery_point(
        "example-app", "req-1", str(plan["plan_id"])
    )
    assert duplicate["changed"] is False
    assert duplicate["retention"] == result["retention"]
    assert len(calls) == 2
    assert captures == ["req-1"], "duplicate apply must not re-run pg_dump"

    inventory = server_module.list_recovery_points("example-app")
    assert len(inventory["recovery_points"]) == 1
    assert inventory["recovery_points"][0]["recovery_point_id"] == (
        result["recovery_point"]["recovery_point_id"]
    )
    encoded = str(inventory)
    assert "pg-dump-bytes" not in encoded
    assert "gimme/recovery-points" not in encoded
    assert "version_id" not in encoded


def test_on_demand_recovery_enforces_verified_retention_after_publication(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    deployment = selected.deployment("example-app")
    selected.save(selected.load().model_copy(update={
        "deployments": {
            "example-app": deployment.model_copy(update={
                "recovery": deployment.recovery.model_copy(update={"retain_last": 1})
            })
        }
    }))
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    install_fake_target_recovery(tmp_path, monkeypatch, adapter)
    first_plan = server_module.plan_create_recovery_point("example-app", "req-1")
    first = server_module.create_recovery_point(
        "example-app", "req-1", str(first_plan["plan_id"])
    )
    second_plan = server_module.plan_create_recovery_point("example-app", "req-2")
    second = server_module.create_recovery_point(
        "example-app", "req-2", str(second_plan["plan_id"])
    )

    assert first["retention"]["deleted"] == 0
    assert second["retention"] == {
        "outcome": "succeeded", "error_code": None, "deleted": 1, "remaining": 1,
    }
    inventory = server_module.list_recovery_points("example-app")["recovery_points"]
    assert [point["recovery_point_id"] for point in inventory] == [
        second["recovery_point"]["recovery_point_id"]
    ]


def test_on_demand_recovery_plan_preserves_normalized_scheduled_policy(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    deployment = selected.deployment("example-app")
    selected.save(selected.load().model_copy(update={
        "deployments": {
            "example-app": deployment.model_copy(update={
                "recovery": RecoveryPolicy(
                    destination="primary", cadence={"kind": "weekly", "weekday": "mon"},
                    retain_last=30,
                )
            })
        }
    }))

    plan = server_module.plan_create_recovery_point("example-app", "manual-request")

    assert plan["cadence"] == {
        "kind": "weekly", "weekday": "mon", "hour": 2, "minute": 0,
    }
    assert plan["retain_last"] == 30
    assert plan["ready"] is True


def test_deployment_resource_plan_includes_secret_safe_schedule_authority(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    state = selected.load()
    deployment = state.deployments["example-app"]
    destination = S3BackupDestination(
        bucket="gimme-backups", region="us-east-1", encryption=SSEAES256(),
        auth=CredentialReferenceBackupAuth(
            access_key_id=SecretReference(
                store="local-sops", secret="minio", field="access_key_id"
            ),
            secret_access_key=SecretReference(
                store="local-sops", secret="minio", field="secret_access_key"
            ),
        ),
    )
    scheduled = deployment.model_copy(update={
        "recovery": RecoveryPolicy(
            destination="primary", cadence={"kind": "hourly", "minute": 15}
        )
    })
    selected.save(state.model_copy(update={
        "backup_destinations": {"primary": destination},
        "deployments": {"example-app": scheduled},
    }))

    plan = server_module.plan_deployment_resources("example-app")

    assert "recovery_schedule_runtime_missing" in plan["readiness_issues"]
    assert plan["recovery_schedule"] == {
        "enabled": True,
        "cadence": {"kind": "hourly", "minute": 15},
        "calendar": "*-*-* *:15:00 UTC",
        "logical_timezone": "UTC",
        "stable_delay_seconds": recovery_schedule_module.stable_delay_seconds(
            "example-app"
        ),
        "policy_fingerprint": recovery_schedule_module.policy_fingerprint(
            scheduled.recovery
        ),
        "authority_fingerprint": plan["recovery_schedule"]["authority_fingerprint"],
        "auth_mode": "stored",
        "service": "gimme-recovery-example-app.service",
        "timer": "gimme-recovery-example-app.timer",
    }
    encoded = json.dumps(plan, sort_keys=True)
    assert "access_key_id" not in encoded
    assert "secret_access_key" not in encoded
    assert "minio" not in encoded
    authority = recovery_schedule_module.runner_authority(
        "example-app", scheduled, "primary", destination,
        {"postgres": {
            "name": "devbox-postgres", "provider": "target_local",
            "kind": "postgres", "version": "17.2",
        }},
    )
    assert "minio" not in json.dumps(authority, sort_keys=True)
    assert authority["destination"]["auth_mode"] == "stored"

    target = state.targets[scheduled.target]
    selected.save(selected.load().model_copy(update={
        "targets": {
            scheduled.target: target.model_copy(update={
                "stack": target.stack.model_copy(update={
                    "packages": [*target.stack.packages, "python3-boto3"]
                })
            })
        }
    }))
    assert "recovery_schedule_runtime_missing" not in server_module.plan_deployment_resources(
        "example-app"
    ).get("readiness_issues", [])


def test_private_runner_authority_changes_with_bound_execution_policy() -> None:
    state = recovery_state()
    deployment = state.deployments["example-app"]
    destination = state.backup_destinations["primary"]
    first = recovery_schedule_module.runner_authority(
        "example-app", deployment, "primary", destination,
        {"postgres": {
            "name": "devbox-postgres", "provider": "target_local",
            "kind": "postgres", "version": "17.2",
        }},
    )
    changed = recovery_schedule_module.runner_authority(
        "example-app",
        deployment.model_copy(update={
            "recovery": deployment.recovery.model_copy(update={"retain_last": 8})
        }),
        "primary", destination,
        {"postgres": {
            "name": "devbox-postgres", "provider": "target_local",
            "kind": "postgres", "version": "17.2",
        }},
    )

    assert first["placement"] == deployment.placement.model_dump(mode="json")
    assert first["resources"] == {"postgres": {
        "name": "devbox-postgres", "provider": "target_local",
        "kind": "postgres", "version": "17.2",
    }}
    assert first["status_identity"] == "example-app"
    assert recovery_schedule_module.schedule_plan(first)["authority_fingerprint"] != (
        recovery_schedule_module.schedule_plan(changed)["authority_fingerprint"]
    )
    with pytest.raises(ValueError, match="resource authority mismatch"):
        recovery_schedule_module.runner_authority(
            "example-app", deployment, "primary", destination,
            {"postgres": {
                "name": "other-postgres", "provider": "target_local",
                "kind": "postgres", "version": "17.2",
            }},
        )


def test_resource_apply_reconciles_recovery_schedule_after_application_secrets(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    state = selected.load()
    deployment = state.deployments["example-app"].model_copy(update={
        "recovery": RecoveryPolicy(
            destination="primary", cadence={"kind": "hourly", "minute": 15}
        )
    })
    target = state.targets["devbox"]
    selected.save(state.model_copy(update={
        "deployments": {"example-app": deployment},
        "targets": {"devbox": target.model_copy(update={
            "stack": target.stack.model_copy(update={
                "packages": [*target.stack.packages, "python3-boto3"]
            })
        })},
    }))
    calls = []

    def fake_run(task, *args, **kwargs):
        calls.append((task, kwargs))
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_deployment_resources("example-app")
    result = server_module.apply_deployment_resources(
        "example-app", str(plan["plan_id"])
    )

    assert result["changed"] is True
    schedule = next(item for item in calls if item[0] == "gimme:recovery:schedule-reconcile")
    assert schedule[1]["recovery_schedule_authority"]["deployment"] == "example-app"
    assert schedule[1]["secret_file"] is None
    assert schedule[1]["recovery_schedule_valkey_file"] is None
    assert [item[0] for item in calls].index("gimme:provision:app") < calls.index(schedule)


def test_recovery_schedule_activation_failure_is_bounded_after_manifest_commit(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    state = selected.load()
    deployment = state.deployments["example-app"].model_copy(update={
        "recovery": RecoveryPolicy(
            destination="primary", cadence={"kind": "hourly", "minute": 15}
        )
    })
    target = state.targets["devbox"]
    selected.save(state.model_copy(update={
        "deployments": {"example-app": deployment},
        "targets": {"devbox": target.model_copy(update={
            "stack": target.stack.model_copy(update={
                "packages": [*target.stack.packages, "python3-boto3"]
            })
        })},
    }))
    canary = "schedule-secret-must-not-escape"
    calls: list[str] = []

    def fail_schedule(task, *args, **kwargs):
        calls.append(task)
        if task == "gimme:recovery:schedule-reconcile":
            raise RuntimeError(f"private schedule output: {canary}")
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fail_schedule)
    plan = server_module.plan_deployment_resources("example-app")

    with pytest.raises(
        server_module.SecretError, match="^recovery_schedule_activation_failed$"
    ) as failure:
        server_module.apply_deployment_resources(
            "example-app", str(plan["plan_id"])
        )

    assert calls.index("gimme:provision:app") < calls.index(
        "gimme:recovery:schedule-reconcile"
    )
    assert (selected.root / "applied-secrets" / "example-app.json").is_file()
    assert canary not in str(failure.value)


def test_deployment_removal_disables_schedule_before_deleting_placement(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    calls = []

    def fake_run(task, *args, **kwargs):
        calls.append((task, kwargs))
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_remove_deployment("example-app")
    server_module.remove_deployment(
        "example-app", str(plan["plan_id"]), "REMOVE example-app"
    )

    tasks = [item[0] for item in calls]
    assert tasks.index("gimme:recovery:schedule-reconcile") < tasks.index(
        "gimme:remove:deployment"
    )
    authority = next(
        item[1]["recovery_schedule_authority"] for item in calls
        if item[0] == "gimme:recovery:schedule-reconcile"
    )
    assert authority["calendar"] is None
    assert authority["valkey_execution"] is None
    assert "example-app" not in selected.load().deployments


def test_deployment_removal_schedule_failure_preserves_state_and_manifest(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    manifest = selected.root / "applied-secrets" / "example-app.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("[]")

    def fail_schedule(task, *args, **kwargs):
        if task == "gimme:recovery:schedule-reconcile":
            raise RuntimeError("private cleanup output")
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fail_schedule)
    plan = server_module.plan_remove_deployment("example-app")

    with pytest.raises(RuntimeError, match="^recovery_schedule_cleanup_failed$"):
        server_module.remove_deployment(
            "example-app", str(plan["plan_id"]), "REMOVE example-app"
        )

    assert "example-app" in selected.load().deployments
    assert manifest.is_file()


def test_manual_valkey_uses_cleanup_authority_but_on_demand_keeps_execution(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    state = selected.load()
    deployment = state.deployments["example-app"].model_copy(update={
        "recovery": RecoveryPolicy(destination="primary", valkey=True)
    })
    target = state.targets["devbox"]
    selected.save(state.model_copy(update={
        "deployments": {"example-app": deployment},
        "targets": {"devbox": target.model_copy(update={
            "stack": target.stack.model_copy(update={
                "packages": [*target.stack.packages, "python3-boto3"]
            })
        })},
    }))
    calls = []

    def fake_run(task, *args, **kwargs):
        calls.append((task, kwargs))
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_deployment_resources("example-app")
    server_module.apply_deployment_resources("example-app", str(plan["plan_id"]))

    cleanup = next(
        kwargs["recovery_schedule_authority"] for task, kwargs in calls
        if task == "gimme:recovery:schedule-reconcile"
    )
    capture = server_module._recovery_schedule_authority(
        "example-app", selected.load(), deployment
    )
    assert cleanup["calendar"] is None
    assert cleanup["components"] == ["postgres"]
    assert cleanup["valkey_execution"] is None
    assert capture["components"] == ["postgres", "valkey"]
    assert capture["valkey_execution"] == {
        "prefix": "gimme:example-app:", "host": "127.0.0.1", "port": 6379,
        "tls": False, "auth_mode": "none",
    }


def test_recovery_valkey_execution_projects_only_bounded_runtime_metadata(
    monkeypatch,
) -> None:
    state = recovery_state()
    deployment = state.deployments["example-app"].model_copy(update={
        "recovery": RecoveryPolicy(destination="primary", valkey=True)
    })
    local = server_module._recovery_valkey_execution("example-app", state, deployment)
    assert local == {
        "prefix": "gimme:example-app:", "host": "127.0.0.1", "port": 6379,
        "tls": False, "auth_mode": "none",
    }

    managed = AWSElastiCacheValkeyResource(
        aws_network="production", administration_target="devbox",
        engine_version="9.0", node_type="cache.t4g.small",
        security_group_id="sg-0123456789abcdef0",
        administration_security_group_id="sg-0123456789abcdef1",
        workload_secret_store="production", snapshot_window="02:00-03:00",
        maintenance_window="sun:03:00-sun:04:00",
    )
    managed_state = state.model_copy(update={
        "resources": {**state.resources, "devbox-valkey": managed}
    })
    monkeypatch.setattr(
        server_module, "_valkey_runtime",
        lambda *args: ({}, {}, None, ["valkey_resource_not_ready"]),
    )
    assert server_module._recovery_valkey_execution(
        "example-app", managed_state, deployment
    ) is None

    monkeypatch.setattr(
        server_module, "_valkey_runtime",
        lambda *args: (
            {"GIMME_VALKEY_HOST": "cache.example.test", "GIMME_VALKEY_PORT": "6380"},
            {"username": object(), "password": object()}, None, [],
        ),
    )
    assert server_module._recovery_valkey_execution(
        "example-app", managed_state, deployment
    ) == {
        "prefix": "{gimme:example-app}:", "host": "cache.example.test", "port": 6380,
        "tls": True, "auth_mode": "stored",
    }


def test_manual_recovery_schedule_status_is_local_and_disabled(
    tmp_path, monkeypatch
) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    monkeypatch.setattr(
        server_module.runner, "run",
        lambda *args, **kwargs: pytest.fail("manual cadence must not contact the Target"),
    )

    status = server_module._recovery_schedule_status(
        "example-app", datetime(2026, 9, 20, 10, tzinfo=UTC)
    )

    assert status["cadence"] == {"kind": "manual"}
    assert status["timer_state"] == "disabled"
    assert status["timer_enabled"] is False
    assert status["logical_next_utc"] is None
    assert status["outcome"] is None


def test_scheduled_recovery_status_exposes_only_bounded_observation(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    deployment = selected.deployment("example-app")
    selected.save(selected.load().model_copy(update={
        "deployments": {
            "example-app": deployment.model_copy(update={
                "recovery": RecoveryPolicy.model_validate({
                    **deployment.recovery.model_dump(mode="json"),
                    "cadence": {"kind": "daily", "hour": 2, "minute": 0},
                })
            })
        }
    }))
    attempt = {
        "schema_version": 1, "deployment": "example-app",
        "last_logical_slot": "2026-09-19T02:00:00+00:00",
        "started_at": "2026-09-19T02:01:00+00:00",
        "finished_at": "2026-09-19T02:02:00+00:00", "outcome": "succeeded",
        "error_code": None, "recovery_point_id": "rp_" + "a" * 20,
        "last_verified_recovery_point_id": "rp_" + "a" * 20,
        "retention_outcome": "succeeded", "retention_deleted": 1,
        "retention_remaining": 7,
    }
    marker = base64.b64encode(json.dumps(attempt).encode()).decode()
    monkeypatch.setattr(
        server_module.runner, "run",
        lambda *args, **kwargs: CommandResult(
            ["dep", "private-command"], 0,
            "private output\nGIMME_RECOVERY_TIMER|enabled|active\n"
            f"GIMME_RECOVERY_STATUS|{marker}\n",
        ),
    )

    status = server_module._recovery_schedule_status(
        "example-app", datetime(2026, 9, 20, 1, tzinfo=UTC)
    )

    assert status["logical_next_utc"] == "2026-09-20T02:00:00+00:00"
    assert status["timer_state"] == "active"
    assert status["timer_enabled"] is True
    assert status["timer_active"] is True
    assert status["outcome"] == "succeeded"
    assert status["recovery_point_id"] == "rp_" + "a" * 20
    assert status["retention_deleted"] == 1
    assert "private" not in json.dumps(status)


def test_scheduled_recovery_status_fails_closed_without_target_output(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    deployment = selected.deployment("example-app")
    selected.save(selected.load().model_copy(update={
        "deployments": {
            "example-app": deployment.model_copy(update={
                "recovery": RecoveryPolicy.model_validate({
                    **deployment.recovery.model_dump(mode="json"),
                    "cadence": {"kind": "hourly"},
                })
            })
        }
    }))
    monkeypatch.setattr(
        server_module.runner, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("private target failure")
        ),
    )

    status = server_module.get_recovery_schedule_status("example-app")

    assert status["timer_state"] == "unavailable"
    assert status["outcome"] == "status_unavailable"
    assert status["error_code"] == "status_unavailable"
    assert "private target failure" not in json.dumps(status)


def test_restore_record_tool_and_resource_are_destination_authoritative(
    tmp_path, monkeypatch
) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    point = recovery_point_id("example-app", "primary", "source-1")
    append_restore_event(
        server_module.store.load().backup_destinations["primary"], None, adapter,
        "example-app", "restore-1", "started", source_recovery_point_id=point,
        destination_resource="devbox-postgres",
        destination_provider="target_local", destination_kind="postgres",
        destination_version="17.2",
    )

    listed = server_module.list_restores("example-app")
    resource = server_module.restore_record_resource("example-app", "restore-1")

    assert listed["restores"] == [resource]
    assert resource["state"] == "started"
    assert "gimme/restores" not in str(listed)


def test_restore_plan_is_read_only_content_addressed_and_exactly_confirmed(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    content = b"postgres-dump"
    path = tmp_path / "postgres.dump"
    path.write_bytes(content)
    point = recovery_point_id("example-app", "primary", "source-1")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, recovery_module.ComponentDump(
            kind="postgres", local_path=path,
            sha256=hashlib.sha256(content).hexdigest(), bytes=len(content),
            resource_version="17.2",
        ),
    )
    calls = []
    capacity = "ready"

    def fake_run(task, *args, **kwargs):
        calls.append(task)
        assert kwargs["restore_source_bytes"] == len(content)
        return CommandResult(
            ["dep"], 0,
            "GIMME_POSTGRES_RESTORE_PREFLIGHT|nonempty\n"
            f"GIMME_POSTGRES_RESTORE_CAPACITY|{capacity}",
        )

    monkeypatch.setattr(server_module.runner, "run", fake_run)

    plan = server_module.plan_restore_deployment("example-app", point, "restore-1")

    assert calls == ["gimme:recovery:inspect-postgres"]
    assert plan["ready"] is True
    assert plan["source"] == {
        "recovery_point_id": point, "provider": "target_local",
        "kind": "postgres", "version": "17.2",
    }
    assert plan["destination"] == {
        "resource": "devbox-postgres", "provider": "target_local",
        "kind": "postgres", "version": "17.2", "empty": False,
    }
    assert plan["selected_components"] == ["postgres"]
    assert plan["untouched_components"] == []
    assert plan["partial"] is False
    assert plan["confirmation"] == (
        f"RESTORE DEPLOYMENT example-app FROM {point} COMPONENTS postgres"
    )
    assert "database_identifier" not in str(plan)
    assert "gimme/recovery-points" not in str(plan)

    capacity = "insufficient"
    insufficient = server_module.plan_restore_deployment(
        "example-app", point, "restore-remote-capacity"
    )
    assert insufficient["ready"] is False
    assert insufficient["readiness_issues"] == ["restore_capacity_insufficient"]

    capacity = "ready"
    monkeypatch.setattr(
        server_module.shutil, "disk_usage", lambda path: SimpleNamespace(free=0)
    )
    local_insufficient = server_module.plan_restore_deployment(
        "example-app", point, "restore-local-capacity"
    )
    assert local_insufficient["ready"] is False
    assert local_insufficient["readiness_issues"] == [
        "restore_capacity_insufficient"
    ]


def test_restore_plan_defaults_to_full_and_explicit_postgres_is_partial(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    postgres = tmp_path / "postgres.dump"
    valkey = tmp_path / "valkey.dump"
    postgres.write_bytes(b"pg")
    valkey.write_bytes(b'{}\n')
    point = recovery_point_id("example-app", "primary", "source-1")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, [
            recovery_module.ComponentDump(
                kind="postgres", local_path=postgres,
                sha256=hashlib.sha256(b"pg").hexdigest(), bytes=2,
                resource_version="17.2",
            ),
            recovery_module.ComponentDump(
                kind="valkey", local_path=valkey,
                sha256=hashlib.sha256(b'{}\n').hexdigest(), bytes=3,
                resource_version="8.0.1", format="gimme-valkey-v1", records=0,
            ),
        ],
    )
    inspected: list[str] = []

    def inspect(task, *args, **kwargs):
        inspected.append(task)
        return CommandResult(
            ["dep"], 0,
            "GIMME_POSTGRES_RESTORE_PREFLIGHT|empty\n"
            "GIMME_POSTGRES_RESTORE_CAPACITY|ready",
        )

    monkeypatch.setattr(server_module.runner, "run", inspect)

    plan = server_module.plan_restore_deployment("example-app", point, "restore-1")

    assert plan["ready"] is True
    assert plan["readiness_issues"] == []
    assert plan["selected_components"] == ["postgres", "valkey"]
    assert plan["untouched_components"] == []
    assert plan["partial"] is False
    assert plan["destinations"] == [
        {
            "resource": "devbox-postgres", "provider": "target_local",
            "kind": "postgres", "version": "17.2", "empty": True,
        },
        {
            "resource": "devbox-valkey", "provider": "target_local",
            "kind": "valkey", "version": "8.0.1",
        },
    ]
    assert plan["safety_components"] == ["valkey"]

    partial = server_module.plan_restore_deployment(
        "example-app", point, "restore-2", ["postgres"]
    )

    assert partial["ready"] is True
    assert partial["selected_components"] == ["postgres"]
    assert partial["untouched_components"] == ["valkey"]
    assert partial["partial"] is True
    assert partial["request_fingerprint"] != plan["request_fingerprint"]
    assert partial["destinations"] == [{
        "resource": "devbox-postgres", "provider": "target_local",
        "kind": "postgres", "version": "17.2", "empty": True,
    }]
    assert partial["safety_components"] == []
    assert partial["confirmation"] == (
        f"PARTIAL RESTORE DEPLOYMENT example-app FROM {point} COMPONENTS postgres "
        "BREAK CONSISTENCY WITH valkey"
    )

    changed = selected.load()
    changed.resources["devbox-valkey"] = ResourceConfig(
        target="devbox", kind="valkey", version="8.0.2"
    )
    selected.save(changed)
    incompatible = server_module.plan_restore_deployment(
        "example-app", point, "restore-3", ["valkey"]
    )

    assert incompatible["selected_components"] == ["valkey"]
    assert incompatible["untouched_components"] == ["postgres"]
    assert incompatible["destinations"] == [{
        "resource": "devbox-valkey", "provider": "target_local",
        "kind": "valkey", "version": "8.0.2",
    }]
    assert incompatible["readiness_issues"] == ["valkey_destination_incompatible"]
    assert incompatible["request_fingerprint"] not in {
        plan["request_fingerprint"], partial["request_fingerprint"],
    }
    assert inspected == [
        "gimme:recovery:inspect-postgres",
        "gimme:recovery:inspect-postgres",
    ], "Valkey-only planning must not inspect unselected PostgreSQL data"


def test_restore_component_selector_is_bounded_normalized_and_explicitly_partial() -> None:
    manifest = [{"kind": "postgres"}, {"kind": "valkey"}]

    assert server_module._normalize_restore_components(manifest, None) == [
        "postgres", "valkey",
    ]
    assert server_module._normalize_restore_components(
        manifest, ["valkey", "postgres"]
    ) == ["postgres", "valkey"]
    assert server_module._normalize_restore_components(manifest, ["postgres"]) == [
        "postgres"
    ]
    for invalid in ([], ["postgres", "postgres"], ["filesystem"]):
        with pytest.raises(RecoveryError, match="restore_component_selection_invalid"):
            server_module._normalize_restore_components(manifest, invalid)
    with pytest.raises(RecoveryError, match="restore_component_missing"):
        server_module._normalize_restore_components(
            [{"kind": "postgres"}], ["valkey"]
        )


def test_restore_markers_survive_deployer_prefixes_and_ansi_colours() -> None:
    output = (
        "[integration] \x1b[39mGIMME_POSTGRES_RESTORE_PREFLIGHT|nonempty\x1b[0m\n"
        "[integration] \x1b[32mGIMME_RESTORE_VERIFY|ready\x1b[0m\n"
    )

    assert server_module._bounded_marker_values(
        output, "GIMME_POSTGRES_RESTORE_PREFLIGHT|", {"empty", "nonempty"}
    ) == {"nonempty"}
    assert server_module._bounded_marker_values(
        output, "GIMME_RESTORE_VERIFY|", {"ready"}
    ) == {"ready"}


def test_restore_plan_rejects_a_changed_destination_after_request_start(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    source = tmp_path / "source.dump"
    source.write_bytes(b"source")
    point = recovery_point_id("example-app", "primary", "source-1")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, recovery_module.ComponentDump(
            kind="postgres", local_path=source,
            sha256=hashlib.sha256(b"source").hexdigest(), bytes=6,
            resource_version="17.2",
        ),
    )
    append_restore_event(
        selected.load().backup_destinations["primary"], None, adapter,
        "example-app", "restore-1", "started",
        source_recovery_point_id=point,
        destination_resource="devbox-postgres",
        destination_provider="target_local", destination_kind="postgres",
        destination_version="17.2", safety_recovery_point_id=None,
    )
    monkeypatch.setattr(
        server_module.runner, "run",
        lambda *args, **kwargs: CommandResult(
            ["dep"], 0,
            "GIMME_POSTGRES_RESTORE_PREFLIGHT|nonempty\n"
            "GIMME_POSTGRES_RESTORE_CAPACITY|ready",
        ),
    )

    plan = server_module.plan_restore_deployment(
        "example-app", point, "restore-1"
    )

    assert plan["ready"] is False
    assert plan["readiness_issues"] == ["restore_destination_changed"]


def test_apply_restore_captures_safety_prepares_and_swaps_under_maintenance(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    source_bytes = b"source-postgres-dump"
    source = tmp_path / "source.dump"
    source.write_bytes(source_bytes)
    valkey = tmp_path / "valkey.dump"
    valkey.write_bytes(b"valkey-archive")
    point = recovery_point_id("example-app", "primary", "source-1")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, [
            recovery_module.ComponentDump(
                kind="postgres", local_path=source,
                sha256=hashlib.sha256(source_bytes).hexdigest(),
                bytes=len(source_bytes), resource_version="17.2",
            ),
            recovery_module.ComponentDump(
                kind="valkey", local_path=valkey,
                sha256=hashlib.sha256(b"valkey-archive").hexdigest(),
                bytes=len(b"valkey-archive"), resource_version="8.0.1",
                format="gimme-valkey-v1", records=0,
            ),
        ],
    )
    calls: list[tuple[str, str | None]] = []

    def fake_run(task, *args, **kwargs):
        calls.append((task, kwargs.get("postgres_restore_action")))
        if task == "gimme:recovery:inspect-postgres":
            return CommandResult(
                ["dep"], 0,
                "GIMME_POSTGRES_RESTORE_PREFLIGHT|nonempty\n"
                "GIMME_POSTGRES_RESTORE_CAPACITY|ready",
            )
        if task == "gimme:backup:dump-postgres":
            safety = b"safety-postgres-dump"
            kwargs["backup_local_path"].write_bytes(safety)
            digest = hashlib.sha256(safety).hexdigest()
            return CommandResult(["dep"], 0, f"GIMME_BACKUP|{digest}|{len(safety)}")
        if task == "gimme:recovery:postgres" and kwargs["postgres_restore_action"] == "prepare":
            assert kwargs["backup_local_path"].read_bytes() == source_bytes
        if task == "gimme:recovery:verify-application":
            return CommandResult(["dep"], 0, "GIMME_RESTORE_VERIFY|ready")
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_restore_deployment(
        "example-app", point, "restore-1", ["postgres"]
    )

    result = server_module.apply_restore_deployment(
        "example-app", point, "restore-1", str(plan["plan_id"]),
        str(plan["confirmation"]), ["postgres"],
    )

    assert result == {
        "changed": True,
        "deployment": "example-app",
        "request_id": "restore-1",
        "state": "data_replaced",
        "recovery_required": True,
        "correlation_id": result["correlation_id"],
    }
    assert [item[0] for item in calls] == [
        "gimme:recovery:inspect-postgres",
        "gimme:recovery:inspect-postgres",
        "gimme:recovery:maintenance",
        "gimme:backup:dump-postgres",
        "gimme:recovery:postgres",
        "gimme:recovery:postgres",
    ]
    assert calls[-2:] == [
        ("gimme:recovery:postgres", "prepare"),
        ("gimme:recovery:postgres", "swap"),
    ]
    record = server_module.restore_record_resource("example-app", "restore-1")
    assert record["state"] == "data_replaced"
    assert record["events"] == 6
    assert record["selected_components"] == ["postgres"]
    assert record["untouched_components"] == ["valkey"]
    assert record["partial"] is True
    safety_id = record["safety_recovery_point_id"]
    safety = recovery_module.find_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", str(safety_id),
    )
    assert safety is not None and safety["safety"] is True
    assert "source-postgres-dump" not in str(result)
    assert "safety-postgres-dump" not in str(result)

    verification = server_module.plan_verify_restore("example-app", "restore-1")
    completed = server_module.apply_verify_restore(
        "example-app", "restore-1", str(verification["plan_id"])
    )

    assert completed["state"] == "completed"
    assert completed["recovery_required"] is False
    completed_record = server_module.restore_record_resource(
        "example-app", "restore-1"
    )
    assert completed_record["state"] == "completed"
    assert completed_record["events"] == 9
    assert calls[-6:] == [
        ("gimme:recovery:maintenance", None),
        ("gimme:recovery:verify-application", None),
        ("gimme:recovery:postgres", "cleanup"),
        ("gimme:recovery:maintenance", None),
        ("gimme:recovery:verify-application", None),
        ("gimme:recovery:maintenance", None),
    ]
    assert recovery_module.safety_recovery_point_protected(
        selected.load().backup_destinations["primary"], None, adapter,
        "example-app", safety,
    ) is False


def test_safety_failure_restores_runtime_records_failure_and_retries(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    source_body = b"source"
    source = tmp_path / "source.dump"
    source.write_bytes(source_body)
    point = recovery_point_id("example-app", "primary", "safety-failure")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, recovery_module.ComponentDump(
            kind="postgres", local_path=source,
            sha256=hashlib.sha256(source_body).hexdigest(), bytes=len(source_body),
            resource_version="17.2",
        ),
    )
    actions: list[tuple[str, str | None]] = []
    fail_safety = True

    def fake_run(task, *args, **kwargs):
        nonlocal fail_safety
        action = kwargs.get("recovery_action")
        actions.append((task, action))
        if task == "gimme:recovery:inspect-postgres":
            return CommandResult(
                ["dep"], 0,
                "GIMME_POSTGRES_RESTORE_PREFLIGHT|nonempty\n"
                "GIMME_POSTGRES_RESTORE_CAPACITY|ready",
            )
        if task == "gimme:backup:dump-postgres":
            if fail_safety:
                fail_safety = False
                raise RuntimeError("protected data must not escape")
            safety = b"safety"
            kwargs["backup_local_path"].write_bytes(safety)
            return CommandResult(
                ["dep"], 0,
                f"GIMME_BACKUP|{hashlib.sha256(safety).hexdigest()}|{len(safety)}",
            )
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_restore_deployment(
        "example-app", point, "restore-safety", ["postgres"]
    )
    with pytest.raises(RecoveryError, match="^safety_failed$"):
        server_module.apply_restore_deployment(
            "example-app", point, "restore-safety", str(plan["plan_id"]),
            str(plan["confirmation"]), ["postgres"],
        )

    failed = server_module.restore_record_resource("example-app", "restore-safety")
    assert failed["state"] == "safety_failed"
    assert actions[-1] == ("gimme:recovery:maintenance", "exit")
    assert not any(
        task == "gimme:recovery:postgres" for task, _action in actions
    ), "Safety failure must precede every source mutation"

    retry = server_module.plan_restore_deployment(
        "example-app", point, "restore-safety", ["postgres"]
    )
    completed_stage = server_module.apply_restore_deployment(
        "example-app", point, "restore-safety", str(retry["plan_id"]),
        str(retry["confirmation"]), ["postgres"],
    )

    assert completed_stage["state"] == "data_replaced"
    assert actions.count(("gimme:recovery:maintenance", "enter")) == 2
    assert server_module.restore_record_resource(
        "example-app", "restore-safety"
    )["state"] == "data_replaced"


def test_safety_failure_reports_when_runtime_cannot_be_restored(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    source = tmp_path / "source.dump"
    source.write_bytes(b"source")
    point = recovery_point_id("example-app", "primary", "runtime-failure")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, recovery_module.ComponentDump(
            kind="postgres", local_path=source,
            sha256=hashlib.sha256(b"source").hexdigest(), bytes=6,
            resource_version="17.2",
        ),
    )

    def fake_run(task, *args, **kwargs):
        action = kwargs.get("recovery_action")
        if task == "gimme:recovery:inspect-postgres":
            return CommandResult(
                ["dep"], 0,
                "GIMME_POSTGRES_RESTORE_PREFLIGHT|nonempty\n"
                "GIMME_POSTGRES_RESTORE_CAPACITY|ready",
            )
        if task == "gimme:backup:dump-postgres":
            raise RuntimeError("private provider failure")
        if task == "gimme:recovery:maintenance" and action == "exit":
            raise RuntimeError("private runtime failure")
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_restore_deployment(
        "example-app", point, "restore-runtime-failure", ["postgres"]
    )
    with pytest.raises(RecoveryError, match="^recovery_runtime_restore_failed$"):
        server_module.apply_restore_deployment(
            "example-app", point, "restore-runtime-failure", str(plan["plan_id"]),
            str(plan["confirmation"]), ["postgres"],
        )

    record = server_module.restore_record_resource(
        "example-app", "restore-runtime-failure"
    )
    assert record["state"] == "safety_failed"
    assert "private" not in str(record)


def test_failed_restore_verification_requiesces_and_never_cleans_up_or_exits(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    source = tmp_path / "source.dump"
    source.write_bytes(b"source")
    point = recovery_point_id("example-app", "primary", "source-1")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, recovery_module.ComponentDump(
            kind="postgres", local_path=source,
            sha256=hashlib.sha256(b"source").hexdigest(), bytes=6,
            resource_version="17.2",
        ),
    )
    identity = {
        "source_recovery_point_id": point,
        "destination_resource": "devbox-postgres",
        "destination_provider": "target_local",
        "destination_kind": "postgres",
        "destination_version": "17.2",
    }
    for restore_state in (
        "started", "maintenance_entered", "safety_not_required",
        "artifact_verified", "shadow_verified", "data_replaced",
    ):
        append_restore_event(
            selected.load().backup_destinations["primary"], None, adapter,
            "example-app", "restore-1", restore_state, **identity,
        )
    actions = []

    def fake_run(task, *args, **kwargs):
        if task == "gimme:recovery:maintenance":
            actions.append(kwargs["recovery_action"])
            return CommandResult(["dep"], 0, "ok")
        if task == "gimme:recovery:verify-application":
            raise RuntimeError("secret application failure")
        raise AssertionError(f"unexpected task {task}")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_verify_restore("example-app", "restore-1")

    with pytest.raises(RecoveryError, match="^restore_verification_failed$") as raised:
        server_module.apply_verify_restore(
            "example-app", "restore-1", str(plan["plan_id"])
        )

    assert "secret" not in str(raised.value)
    assert actions == ["resume", "quiesce"]
    assert server_module.restore_record_resource(
        "example-app", "restore-1"
    )["state"] == "verification_failed"


def test_cleanup_and_maintenance_exit_failures_resume_from_authoritative_state(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    source = tmp_path / "source.dump"
    source.write_bytes(b"source")
    point = recovery_point_id("example-app", "primary", "cleanup-source")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, recovery_module.ComponentDump(
            kind="postgres", local_path=source,
            sha256=hashlib.sha256(b"source").hexdigest(), bytes=6,
            resource_version="17.2",
        ),
    )
    identity = {
        "source_recovery_point_id": point,
        "destination_resource": "devbox-postgres",
        "destination_provider": "target_local",
        "destination_kind": "postgres",
        "destination_version": "17.2",
    }
    for restore_state in (
        "started", "maintenance_entered", "safety_not_required",
        "artifact_verified", "shadow_verified", "data_replaced",
    ):
        append_restore_event(
            selected.load().backup_destinations["primary"], None, adapter,
            "example-app", "restore-boundaries", restore_state, **identity,
        )

    cleanup_attempts = 0
    exit_attempts = 0

    def fake_run(task, *args, **kwargs):
        nonlocal cleanup_attempts, exit_attempts
        if task == "gimme:recovery:verify-application":
            return CommandResult(["dep"], 0, "GIMME_RESTORE_VERIFY|ready")
        if (
            task == "gimme:recovery:postgres"
            and kwargs.get("postgres_restore_action") == "cleanup"
        ):
            cleanup_attempts += 1
            if cleanup_attempts == 1:
                raise RuntimeError("private cleanup failure")
        if (
            task == "gimme:recovery:maintenance"
            and kwargs.get("recovery_action") == "exit"
        ):
            exit_attempts += 1
            if exit_attempts == 1:
                raise RuntimeError("private exit failure")
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module, "_run_deployment", fake_run)
    first = server_module.plan_verify_restore("example-app", "restore-boundaries")
    with pytest.raises(RecoveryError, match="^restore_cleanup_failed$"):
        server_module.apply_verify_restore(
            "example-app", "restore-boundaries", str(first["plan_id"])
        )
    assert server_module.restore_record_resource(
        "example-app", "restore-boundaries"
    )["state"] == "verification_succeeded"

    second = server_module.plan_verify_restore("example-app", "restore-boundaries")
    with pytest.raises(RecoveryError, match="^restore_maintenance_exit_failed$"):
        server_module.apply_verify_restore(
            "example-app", "restore-boundaries", str(second["plan_id"])
        )
    assert server_module.restore_record_resource(
        "example-app", "restore-boundaries"
    )["state"] == "cleanup_completed"

    third = server_module.plan_verify_restore("example-app", "restore-boundaries")
    completed = server_module.apply_verify_restore(
        "example-app", "restore-boundaries", str(third["plan_id"])
    )
    assert completed["state"] == "completed"
    assert cleanup_attempts == 2
    assert exit_attempts == 2


def test_apply_restore_failure_stays_in_maintenance_and_retry_resumes_at_swap(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    source = tmp_path / "source.dump"
    source.write_bytes(b"source")
    point = recovery_point_id("example-app", "primary", "source-1")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, recovery_module.ComponentDump(
            kind="postgres", local_path=source,
            sha256=hashlib.sha256(b"source").hexdigest(), bytes=6,
            resource_version="17.2",
        ),
    )
    fail_swap = True
    maintenance_actions: list[str] = []

    def fake_run(task, *args, **kwargs):
        nonlocal fail_swap
        if task == "gimme:recovery:inspect-postgres":
            return CommandResult(
                ["dep"], 0,
                "GIMME_POSTGRES_RESTORE_PREFLIGHT|empty\n"
                "GIMME_POSTGRES_RESTORE_CAPACITY|ready",
            )
        if task == "gimme:recovery:maintenance":
            maintenance_actions.append(kwargs["recovery_action"])
        if task == "gimme:recovery:postgres" and kwargs["postgres_restore_action"] == "swap":
            if fail_swap:
                fail_swap = False
                raise RuntimeError("private database failure")
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_restore_deployment("example-app", point, "restore-1")
    with pytest.raises(RecoveryError, match="^restore_swap_failed$"):
        server_module.apply_restore_deployment(
            "example-app", point, "restore-1", str(plan["plan_id"]),
            str(plan["confirmation"]),
        )
    assert server_module.restore_record_resource(
        "example-app", "restore-1"
    )["state"] == "shadow_verified"
    assert maintenance_actions == ["enter"]

    retry_plan = server_module.plan_restore_deployment(
        "example-app", point, "restore-1"
    )
    result = server_module.apply_restore_deployment(
        "example-app", point, "restore-1", str(retry_plan["plan_id"]),
        str(retry_plan["confirmation"]),
    )

    assert result["state"] == "data_replaced"
    assert maintenance_actions == ["enter"], "retry must preserve the existing maintenance owner"


@pytest.mark.parametrize("failure_boundary", ["artifact", "shadow"])
def test_pre_swap_failure_restores_runtime_and_retry_reenters_maintenance(
    tmp_path, monkeypatch, failure_boundary
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    source = tmp_path / "source.dump"
    source.write_bytes(b"source")
    point = recovery_point_id("example-app", "primary", f"{failure_boundary}-source")
    recovery_module.create_recovery_point(
        "primary", selected.load().backup_destinations["primary"], None, adapter,
        "example-app", point, recovery_module.ComponentDump(
            kind="postgres", local_path=source,
            sha256=hashlib.sha256(b"source").hexdigest(), bytes=6,
            resource_version="17.2",
        ),
    )
    maintenance_actions: list[str] = []
    prepare_attempts = 0

    def fake_run(task, *args, **kwargs):
        nonlocal prepare_attempts
        if task == "gimme:recovery:inspect-postgres":
            return CommandResult(
                ["dep"], 0,
                "GIMME_POSTGRES_RESTORE_PREFLIGHT|empty\n"
                "GIMME_POSTGRES_RESTORE_CAPACITY|ready",
            )
        if task == "gimme:recovery:maintenance":
            maintenance_actions.append(kwargs["recovery_action"])
        if (
            task == "gimme:recovery:postgres"
            and kwargs["postgres_restore_action"] == "prepare"
        ):
            prepare_attempts += 1
            if failure_boundary == "shadow" and prepare_attempts == 1:
                raise RuntimeError("private shadow failure")
        return CommandResult(["dep"], 0, "ok")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    if failure_boundary == "artifact":
        materialize = recovery_module.materialize_recovery_component
        attempts = 0

        def fail_materialize_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RecoveryError("recovery_component_checksum_mismatch")
            return materialize(*args, **kwargs)

        monkeypatch.setattr(
            recovery_module, "materialize_recovery_component", fail_materialize_once
        )

    plan = server_module.plan_restore_deployment(
        "example-app", point, f"restore-{failure_boundary}"
    )
    expected_error = (
        "recovery_component_checksum_mismatch"
        if failure_boundary == "artifact" else "restore_shadow_prepare_failed"
    )
    with pytest.raises(RecoveryError, match=f"^{expected_error}$"):
        server_module.apply_restore_deployment(
            "example-app", point, f"restore-{failure_boundary}", str(plan["plan_id"]),
            str(plan["confirmation"]),
        )
    assert server_module.restore_record_resource(
        "example-app", f"restore-{failure_boundary}"
    )["state"] == f"{failure_boundary}_failed"
    assert maintenance_actions == ["enter", "exit"]

    retry = server_module.plan_restore_deployment(
        "example-app", point, f"restore-{failure_boundary}"
    )
    result = server_module.apply_restore_deployment(
        "example-app", point, f"restore-{failure_boundary}", str(retry["plan_id"]),
        str(retry["confirmation"]),
    )
    assert result["state"] == "data_replaced"
    assert maintenance_actions == ["enter", "exit", "enter"]


def test_delete_recovery_point_requires_both_confirmations_for_the_last_point(
    tmp_path, monkeypatch
) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    install_fake_target_recovery(tmp_path, monkeypatch, adapter)
    creation = server_module.plan_create_recovery_point("example-app", "req-1")
    created = server_module.create_recovery_point(
        "example-app", "req-1", str(creation["plan_id"])
    )
    point_id = str(created["recovery_point"]["recovery_point_id"])
    plan = server_module.plan_delete_recovery_point("example-app", point_id)

    assert plan["components"] == 1
    assert plan["bytes"] == len(b"postgres-dump-1")
    assert "gimme/recovery-points" not in str(plan)
    with pytest.raises(ValueError, match="last Recovery Point"):
        server_module.delete_recovery_point(
            "example-app", point_id, str(plan["plan_id"]), str(plan["confirmation"])
        )

    result = server_module.delete_recovery_point(
        "example-app", point_id, str(plan["plan_id"]), str(plan["confirmation"]),
        str(plan["last_recovery_point_confirmation"]),
    )
    assert result["state"] == "deleted"
    assert server_module.list_recovery_points("example-app")["recovery_points"] == []


def test_partial_recovery_point_deletion_is_visible_and_same_plan_retry_completes(
    tmp_path, monkeypatch
) -> None:
    use_recovery_store(tmp_path, monkeypatch)

    class FailManifestOnce(FakeS3):
        failed = False

        def delete_object(self, destination, credentials, key, version_id=None):
            if key.endswith("/manifest.json") and not self.failed:
                self.failed = True
                raise RuntimeError("private endpoint and credential details")
            super().delete_object(destination, credentials, key, version_id)

    adapter = FailManifestOnce()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    install_fake_target_recovery(tmp_path, monkeypatch, adapter)
    creation = server_module.plan_create_recovery_point("example-app", "req-1")
    created = server_module.create_recovery_point(
        "example-app", "req-1", str(creation["plan_id"])
    )
    point_id = str(created["recovery_point"]["recovery_point_id"])
    plan = server_module.plan_delete_recovery_point("example-app", point_id)
    arguments = (
        "example-app", point_id, str(plan["plan_id"]), str(plan["confirmation"]),
        str(plan["last_recovery_point_confirmation"]),
    )

    with pytest.raises(RecoveryError, match="^recovery_point_deletion_failed$") as raised:
        server_module.delete_recovery_point(*arguments)
    assert "private" not in str(raised.value)
    [partial] = server_module.list_recovery_points("example-app")["recovery_points"]
    assert partial["state"] == "deletion_failed"
    with pytest.raises(RecoveryError, match="^recovery_point_deletion_failed$"):
        server_module.plan_delete_recovery_point("example-app", point_id)

    result = server_module.delete_recovery_point(*arguments)
    assert result["state"] == "deleted"
    duplicate = server_module.delete_recovery_point(*arguments)
    assert duplicate["changed"] is False


def test_rejected_delete_apply_does_not_authorize_an_external_partial_state(
    tmp_path, monkeypatch
) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    install_fake_target_recovery(tmp_path, monkeypatch, adapter)
    creation = server_module.plan_create_recovery_point("example-app", "req-1")
    created = server_module.create_recovery_point(
        "example-app", "req-1", str(creation["plan_id"])
    )
    point_id = str(created["recovery_point"]["recovery_point_id"])
    plan = server_module.plan_delete_recovery_point("example-app", point_id)
    with pytest.raises(ValueError, match="confirmation"):
        server_module.delete_recovery_point(
            "example-app", point_id, str(plan["plan_id"]), "wrong"
        )
    component = f"gimme/recovery-points/example-app/{point_id}/postgres.dump"
    adapter.delete_object(None, None, component, adapter.versions[component])

    with pytest.raises(RecoveryError, match="^recovery_point_deletion_failed$"):
        server_module.delete_recovery_point(
            "example-app", point_id, str(plan["plan_id"]), str(plan["confirmation"]),
            str(plan["last_recovery_point_confirmation"]),
        )


def test_unresolved_safety_recovery_point_cannot_be_deleted(tmp_path, monkeypatch) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    content = b"safety-dump"
    local = tmp_path / "safety.dump"
    local.write_bytes(content)
    point_id = server_module.recovery_module.recovery_point_id(
        "example-app", "primary", "safety-1"
    )
    server_module.recovery_module.create_recovery_point(
        "primary", recovery_state().backup_destinations["primary"], None, adapter,
        "example-app", point_id,
        server_module.ComponentDump(
            kind="postgres", local_path=local,
            sha256=hashlib.sha256(content).hexdigest(), bytes=len(content),
            resource_version="17.2",
        ),
        safety_restore_request_id="restore-1",
    )
    plan = server_module.plan_delete_recovery_point("example-app", point_id)
    assert plan["safety_protected"] is True

    with pytest.raises(RecoveryError, match="^recovery_point_safety_protected$"):
        server_module.delete_recovery_point(
            "example-app", point_id, str(plan["plan_id"]), str(plan["confirmation"]),
            str(plan["last_recovery_point_confirmation"]),
        )

    event = {
        "schema_version": 1, "deployment": "example-app", "request_id": "restore-1",
        "sequence": 0, "state": "completed", "safety_recovery_point_id": point_id,
    }
    body = json.dumps(event, sort_keys=True).encode()
    adapter.put_object(
        recovery_state().backup_destinations["primary"], None,
        restore_event_key("example-app", "restore-1", 0), body,
        hashlib.sha256(body).hexdigest(),
    )
    completed_plan = server_module.plan_delete_recovery_point("example-app", point_id)
    assert completed_plan["safety_protected"] is False


def test_source_recovery_point_is_protected_until_restore_completes(
    tmp_path, monkeypatch
) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    source = tmp_path / "source.dump"
    source.write_bytes(b"source")
    point = recovery_point_id("example-app", "primary", "source-1")
    recovery_module.create_recovery_point(
        "primary", recovery_state().backup_destinations["primary"], None, adapter,
        "example-app", point, recovery_module.ComponentDump(
            kind="postgres", local_path=source,
            sha256=hashlib.sha256(b"source").hexdigest(), bytes=6,
            resource_version="17.2",
        ),
    )
    append_restore_event(
        recovery_state().backup_destinations["primary"], None, adapter,
        "example-app", "restore-1", "started",
        source_recovery_point_id=point,
        destination_resource="devbox-postgres",
        destination_provider="target_local", destination_kind="postgres",
        destination_version="17.2",
    )

    plan = server_module.plan_delete_recovery_point("example-app", point)

    assert plan["restore_protected"] is True
    with pytest.raises(RecoveryError, match="^recovery_point_restore_protected$"):
        server_module.delete_recovery_point(
            "example-app", point, str(plan["plan_id"]), str(plan["confirmation"]),
            str(plan["last_recovery_point_confirmation"]),
        )


def test_create_recovery_point_requires_a_bound_recovery_policy(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="no Recovery Policy bound"):
        server_module.plan_create_recovery_point("example-app", "req-1")


def test_valkey_recovery_quiesces_captures_both_components_and_restores_runtime(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    postgres_only = server_module.plan_create_recovery_point("example-app", "req-1")
    deployment = selected.deployment("example-app")
    selected.save(
        selected.load().model_copy(update={
            "deployments": {
                "example-app": deployment.model_copy(update={
                    "recovery": RecoveryPolicy(
                        destination="primary", valkey=True, quiesce_wait_seconds=45
                    )
                })
            }
        })
    )

    inclusive = server_module.plan_create_recovery_point("example-app", "req-1")

    assert postgres_only["components"] == ["postgres"]
    assert inclusive["components"] == ["postgres", "valkey"]
    assert inclusive["quiesce_wait_seconds"] == 45
    assert inclusive["ready"] is True
    assert inclusive["plan_id"] != postgres_only["plan_id"]
    calls: list[dict] = []
    install_fake_target_recovery(tmp_path, monkeypatch, adapter, calls=calls)
    result = server_module.create_recovery_point(
        "example-app", "req-1", str(inclusive["plan_id"])
    )

    assert len(calls) == 1
    assert calls[0]["recovery_schedule_authority"]["components"] == [
        "postgres", "valkey",
    ]
    assert calls[0]["recovery_schedule_authority"]["valkey_execution"] is not None
    assert [item["kind"] for item in result["recovery_point"]["components"]] == [
        "postgres", "valkey",
    ]


def test_valkey_capture_failure_restores_runtime_and_publishes_nothing(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    deployment = selected.deployment("example-app")
    selected.save(selected.load().model_copy(update={"deployments": {
        "example-app": deployment.model_copy(update={
            "recovery": RecoveryPolicy(destination="primary", valkey=True)
        })
    }}))
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    calls = []

    def fake_run(task, *args, **kwargs):
        calls.append(task)
        raise RuntimeError("private key material must never escape")

    monkeypatch.setattr(server_module, "_run_deployment", fake_run)
    plan = server_module.plan_create_recovery_point("example-app", "req-1")

    with pytest.raises(RecoveryError, match="^recovery_capture_failed$"):
        server_module.create_recovery_point("example-app", "req-1", str(plan["plan_id"]))

    assert calls == ["gimme:recovery:on-demand"]
    assert adapter.objects == {}


def test_valkey_only_restore_safety_captures_exact_selected_component(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    state = selected.load()
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    calls: list[str] = []

    def fake_run(task, *args, **kwargs):
        calls.append(task)
        assert task == "gimme:backup:capture-valkey"
        content = b'{"format":"gimme-valkey-v1"}\n'
        kwargs["backup_local_path"].write_bytes(content)
        return CommandResult(
            ["dep"], 0,
            "GIMME_VALKEY_BACKUP|"
            f"{hashlib.sha256(content).hexdigest()}|{len(content)}|0|"
            "2026-09-20T02:00:00+00:00",
        )

    monkeypatch.setattr(server_module, "_run_deployment", fake_run)
    request_id = "restore-valkey"
    safety_id = recovery_module.safety_recovery_point_id(
        "example-app", "primary", request_id
    )

    safety = server_module._capture_restore_safety(
        "example-app", request_id, safety_id, ["valkey"], state,
        state.deployments["example-app"], "primary",
        state.backup_destinations["primary"], None,
    )

    assert calls == ["gimme:backup:capture-valkey"]
    assert safety["safety"] is True
    assert safety["restore_request_id"] == request_id
    assert [item["kind"] for item in safety["components"]] == ["valkey"]
    assert safety["components"][0]["resource_version"] == "8.0.1"


def test_valkey_only_restore_replaces_prefix_and_completes_without_postgres_mutation(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    state = selected.load()
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    postgres = tmp_path / "source.pg"
    valkey = tmp_path / "source.valkey"
    postgres.write_bytes(b"pg")
    valkey_body = b'{"format":"gimme-valkey-v1"}\n'
    valkey.write_bytes(valkey_body)
    point = recovery_point_id("example-app", "primary", "valkey-source")
    recovery_module.create_recovery_point(
        "primary", state.backup_destinations["primary"], None, adapter,
        "example-app", point, [
            recovery_module.ComponentDump(
                kind="postgres", local_path=postgres,
                sha256=hashlib.sha256(b"pg").hexdigest(), bytes=2,
                resource_version="17.2",
            ),
            recovery_module.ComponentDump(
                kind="valkey", local_path=valkey,
                sha256=hashlib.sha256(valkey_body).hexdigest(),
                bytes=len(valkey_body), resource_version="8.0.1",
                format="gimme-valkey-v1", records=0,
            ),
        ],
    )
    calls: list[tuple[str, str | None]] = []
    valkey_attempts = 0

    def fake_run(task, *args, **kwargs):
        nonlocal valkey_attempts
        calls.append((task, kwargs.get("recovery_action")))
        if task == "gimme:backup:capture-valkey":
            kwargs["backup_local_path"].write_bytes(valkey_body)
            return CommandResult(
                ["dep"], 0,
                "GIMME_VALKEY_BACKUP|"
                f"{hashlib.sha256(valkey_body).hexdigest()}|{len(valkey_body)}|0|"
                "2026-09-20T02:00:00+00:00",
            )
        if task == "gimme:recovery:valkey":
            assert kwargs["backup_local_path"].read_bytes() == valkey_body
            assert kwargs["valkey_restore_records"] == 0
            valkey_attempts += 1
            if valkey_attempts == 1:
                raise RuntimeError("interrupted after prefix mutation")
            return CommandResult(["dep"], 0, "GIMME_VALKEY_RESTORE|0|0")
        if task == "gimme:recovery:verify-application":
            return CommandResult(["dep"], 0, "GIMME_RESTORE_VERIFY|ready")
        return CommandResult(["dep"], 0, "maintenance")

    monkeypatch.setattr(server_module, "_run_deployment", fake_run)
    plan = server_module.plan_restore_deployment(
        "example-app", point, "restore-valkey", ["valkey"]
    )
    assert plan["ready"] is True

    with pytest.raises(RecoveryError, match="^valkey_restore_failed$"):
        server_module.apply_restore_deployment(
            "example-app", point, "restore-valkey", str(plan["plan_id"]),
            str(plan["confirmation"]), ["valkey"],
        )
    assert server_module.restore_record_resource(
        "example-app", "restore-valkey"
    )["state"] == "artifact_verified"

    retry = server_module.plan_restore_deployment(
        "example-app", point, "restore-valkey", ["valkey"]
    )
    applied = server_module.apply_restore_deployment(
        "example-app", point, "restore-valkey", str(retry["plan_id"]),
        str(retry["confirmation"]), ["valkey"],
    )
    assert applied["state"] == "data_replaced"
    assert valkey_attempts == 2
    assert not any("postgres" in task for task, _action in calls)

    verify = server_module.plan_verify_restore("example-app", "restore-valkey")
    completed = server_module.apply_verify_restore(
        "example-app", "restore-valkey", str(verify["plan_id"])
    )
    assert completed["state"] == "completed"
    assert not any("postgres" in task for task, _action in calls)
    record = recovery_module.load_restore_record(
        state.backup_destinations["primary"], None, adapter,
        "example-app", "restore-valkey",
    )
    assert record["selected_components"] == ["valkey"]
    assert record["destination"] == {
        "resource": "devbox-valkey", "provider": "target_local",
        "kind": "valkey", "version": "8.0.1",
    }


def test_full_restore_protects_only_nonempty_components_and_retries_in_order(
    tmp_path, monkeypatch
) -> None:
    selected = use_recovery_store(tmp_path, monkeypatch)
    state = selected.load()

    class PrivateVersionS3(FakeS3):
        def put_object(self, destination, credentials, key, body, sha256):
            written = super().put_object(destination, credentials, key, body, sha256)
            version_id = f"private-object-version-{self.puts}"
            self.versions[key] = version_id
            return dataclasses.replace(written, version_id=version_id)

    adapter = PrivateVersionS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)
    monkeypatch.setattr(
        server_module, "_backup_destination_credentials",
        lambda state, destination: (
            None, ("private-access-key", "private-secret-key")
        ),
    )
    pg_body = b"source-pg"
    private_key = b"gimme:example-app:private-key"
    private_payload = b"private-valkey-payload"
    valkey_body = (
        b'{"format":"gimme-valkey-v1"}\n'
        + json.dumps({
            "dump": base64.b64encode(private_payload).decode(),
            "expires_at_ms": None,
            "key": base64.b64encode(private_key).decode(),
        }, sort_keys=True, separators=(",", ":")).encode()
        + b"\n"
    )
    postgres = tmp_path / "source.pg"
    valkey = tmp_path / "source.valkey"
    postgres.write_bytes(pg_body)
    valkey.write_bytes(valkey_body)
    point = recovery_point_id("example-app", "primary", "full-source")
    recovery_module.create_recovery_point(
        "primary", state.backup_destinations["primary"], None, adapter,
        "example-app", point, [
            recovery_module.ComponentDump(
                kind="postgres", local_path=postgres,
                sha256=hashlib.sha256(pg_body).hexdigest(), bytes=len(pg_body),
                resource_version="17.2",
            ),
            recovery_module.ComponentDump(
                kind="valkey", local_path=valkey,
                sha256=hashlib.sha256(valkey_body).hexdigest(),
                bytes=len(valkey_body), resource_version="8.0.1",
                format="gimme-valkey-v1", records=1,
            ),
        ],
    )
    calls: list[tuple[str, str | None]] = []
    valkey_attempts = 0
    swap_attempts = 0

    def fake_run(task, *args, **kwargs):
        nonlocal valkey_attempts, swap_attempts
        action = kwargs.get("postgres_restore_action")
        calls.append((task, action))
        if task == "gimme:recovery:inspect-postgres":
            return CommandResult(
                ["dep"], 0,
                "GIMME_POSTGRES_RESTORE_PREFLIGHT|empty\n"
                "GIMME_POSTGRES_RESTORE_CAPACITY|ready",
            )
        if task == "gimme:backup:dump-postgres":
            raise AssertionError("empty PostgreSQL destination must not be captured")
        if task == "gimme:backup:capture-valkey":
            kwargs["backup_local_path"].write_bytes(valkey_body)
            return CommandResult(
                ["dep"], 0,
                "GIMME_VALKEY_BACKUP|"
                f"{hashlib.sha256(valkey_body).hexdigest()}|{len(valkey_body)}|1|"
                "2026-09-20T02:00:00+00:00",
            )
        if task == "gimme:recovery:valkey":
            assert kwargs["backup_local_path"].read_bytes() == valkey_body
            valkey_attempts += 1
            return CommandResult(
                ["dep", "private-command-argument"], 0,
                "GIMME_VALKEY_RESTORE|1|0\nprivate-raw-output",
            )
        if task == "gimme:recovery:postgres" and action == "swap":
            swap_attempts += 1
            if swap_attempts == 1:
                raise RuntimeError("private swap interruption")
        if task == "gimme:recovery:verify-application":
            return CommandResult(
                ["dep", "private-command-argument"], 0,
                "GIMME_RESTORE_VERIFY|ready\nprivate-raw-output",
            )
        return CommandResult(
            ["dep", "private-command-argument"], 0, "private-raw-output"
        )

    monkeypatch.setattr(server_module, "_run_deployment", fake_run)
    plan = server_module.plan_restore_deployment("example-app", point, "restore-full")
    assert plan["safety_components"] == ["valkey"]
    with pytest.raises(RecoveryError, match="^restore_swap_failed$"):
        server_module.apply_restore_deployment(
            "example-app", point, "restore-full", str(plan["plan_id"]),
            str(plan["confirmation"]),
        )
    assert server_module.restore_record_resource(
        "example-app", "restore-full"
    )["state"] == "shadow_verified"

    retry = server_module.plan_restore_deployment(
        "example-app", point, "restore-full"
    )
    applied = server_module.apply_restore_deployment(
        "example-app", point, "restore-full", str(retry["plan_id"]),
        str(retry["confirmation"]),
    )

    assert applied["state"] == "data_replaced"
    assert valkey_attempts == 2
    assert swap_attempts == 2
    prepare = calls.index(("gimme:recovery:postgres", "prepare"))
    replace = next(
        index for index, call in enumerate(calls)
        if call[0] == "gimme:recovery:valkey"
    )
    swap = calls.index(("gimme:recovery:postgres", "swap"))
    assert prepare < replace < swap
    record = recovery_module.load_restore_record(
        state.backup_destinations["primary"], None, adapter,
        "example-app", "restore-full",
    )
    assert record["request_fingerprint"] == plan["request_fingerprint"]
    assert record["safety_components"] == ["valkey"]
    assert record["destinations"] == [
        {
            "resource": "devbox-postgres", "provider": "target_local",
            "kind": "postgres", "version": "17.2",
        },
        {
            "resource": "devbox-valkey", "provider": "target_local",
            "kind": "valkey", "version": "8.0.1",
        },
    ]
    verification = server_module.plan_verify_restore("example-app", "restore-full")
    completed = server_module.apply_verify_restore(
        "example-app", "restore-full", str(verification["plan_id"])
    )
    public_surfaces = [
        plan, applied, record, verification, completed,
        server_module.list_recovery_points("example-app"),
        server_module.list_restores("example-app"),
        server_module.restore_record_resource("example-app", "restore-full"),
        server_module.list_operations(subject="example-app"),
    ]
    public_text = json.dumps(public_surfaces, sort_keys=True, default=str)
    public_text += (selected.root / "operations.jsonl").read_text()
    for protected in (
        pg_body.decode(), private_key.decode(), private_payload.decode(),
        base64.b64encode(private_key).decode(),
        base64.b64encode(private_payload).decode(),
        "gimme:example-app:", "gimme_example_app", "gimme/recovery-points",
        "private-object-version", "private-access-key", "private-secret-key",
        "private-command-argument", "private-raw-output", "private swap interruption",
    ):
        assert protected not in public_text
    changed = selected.load()
    changed.resources["devbox-valkey"] = ResourceConfig(
        target="devbox", kind="valkey", version="8.0.2"
    )
    selected.save(changed)
    blocked_verification = server_module.plan_verify_restore(
        "example-app", "restore-full"
    )
    assert blocked_verification["ready"] is False
    assert blocked_verification["readiness_issues"] == [
        "restore_destination_changed"
    ]
    selected.save(state)
    safety = recovery_module.find_recovery_point(
        "primary", state.backup_destinations["primary"], None, adapter,
        "example-app", recovery_module.safety_recovery_point_id(
            "example-app", "primary", "restore-full"
        ),
    )
    assert safety is not None
    assert [item["kind"] for item in safety["components"]] == ["valkey"]


def test_create_recovery_point_rejects_unverified_target_result(tmp_path, monkeypatch) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    monkeypatch.setattr(server_module, "backup_s3", FakeS3())

    def fake_run(task, *args, **kwargs):
        point_id = recovery_module.recovery_point_id("example-app", "primary", "req-1")
        payload = base64.b64encode(json.dumps({
            "changed": True, "recovery_point_id": point_id,
            "retention": {
                "outcome": "succeeded", "error_code": None,
                "deleted": 0, "remaining": 1,
            },
        }).encode()).decode()
        return CommandResult(["dep"], 0, f"GIMME_RECOVERY_RESULT|{payload}")

    monkeypatch.setattr(server_module, "_run_deployment", fake_run)
    plan = server_module.plan_create_recovery_point("example-app", "req-1")

    with pytest.raises(RecoveryError, match="recovery_verification_failed"):
        server_module.create_recovery_point("example-app", "req-1", str(plan["plan_id"]))

    inventory = server_module.list_recovery_points("example-app")
    assert inventory["recovery_points"] == []


MASTER_PASSWORD = "master-plaintext-password"


class FakeRDS:
    def __init__(self, *, fail_master: bool = False, fail_describe: bool = False) -> None:
        self.instances: dict[str, InstanceObservation] = {}
        self.create_calls = 0
        self.modify_calls: list[dict[str, object]] = []
        self.reboot_calls = 0
        self.fail_master = fail_master
        self.fail_describe = fail_describe
        self.secret_payloads: dict[str, dict[str, str]] = {}
        self.secret_versions: dict[str, int] = {}
        self.secret_history: dict[str, dict[str, dict[str, str]]] = {}
        self.secret_current: dict[str, str] = {}
        self.secret_tags: dict[str, dict[str, str]] = {}
        self.deleted_secrets: list[tuple[str, int]] = []
        self.snapshots: dict[str, SnapshotObservation] = {}
        self.delete_instance_calls = 0

    def describe_instance(self, account, network, identifier):
        if self.fail_describe:
            raise ResourceError("aws_rds_describe_throttled")
        return self.instances.get(identifier)

    def create_instance(self, account, network, resource, name, identifier, group_ids):
        self.create_calls += 1
        self.instances[identifier] = InstanceObservation(
            identity=f"arn:aws:rds:us-east-1:123456789012:db:{identifier}",
            status="available", engine_version=resource.engine_version,
            endpoint="db.example.test", port=5432,
            master_secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:m",
            instance_class=resource.instance_class,
            allocated_storage_gb=resource.allocated_storage_gb,
            security_group_ids=tuple(sorted(group_ids)),
            parameter_group_status="in-sync", multi_az=True, storage_encrypted=True,
            deletion_protection=True, publicly_accessible=False,
            backup_retention_days=resource.backup_retention_days,
            backup_window=resource.backup_window,
            maintenance_window=resource.maintenance_window,
            ownership_verified=True, generation=1,
        )
        return self.instances[identifier]

    def modify_instance(self, account, network, resource, name, identifier, changes):
        self.modify_calls.append(dict(changes))
        live = self.instances[identifier]
        renames = {
            "EngineVersion": "engine_version", "DBInstanceClass": "instance_class",
            "AllocatedStorage": "allocated_storage_gb",
        }
        self.instances[identifier] = dataclasses.replace(
            live, **{renames[key]: value for key, value in changes.items() if key in renames}
        )
        return self.instances[identifier]

    def reboot_instance(self, account, network, identifier):
        self.reboot_calls += 1
        return self.instances[identifier]

    def resolve_master_credential(self, account, region, secret_arn):
        if self.fail_master:
            raise ResourceError("aws_rds_master_secret_access_denied")
        return "gimme_admin", MASTER_PASSWORD

    def master_secret_version_fingerprint(self, account, region, secret_arn):
        return "sha256:" + "a" * 64

    def create_workload_secret(self, account, store, name, tags, payload):
        number = self.secret_versions.get(name, 0) + 1
        self.secret_versions[name] = number
        version = f"v{number}"
        self.secret_payloads[name] = payload
        self.secret_history.setdefault(name, {})[version] = dict(payload)
        self.secret_current[name] = version
        self.secret_tags[name] = dict(tags)
        return f"arn:aws:secretsmanager:{store.region}:123456789012:secret:{name}", version

    def restore_workload_secret_version(
        self, account, store, name, restore_version, remove_version
    ):
        if self.secret_current[name] == restore_version:
            return
        assert self.secret_current[name] == remove_version
        self.secret_current[name] = restore_version
        self.secret_payloads[name] = dict(self.secret_history[name][restore_version])

    def resolve_workload_credential(self, account, store, name, version_id):
        document = self.secret_history[name][version_id]
        return document["username"], document["password"]

    def schedule_workload_secret_deletion(self, account, store, name, expected_tags):
        if (name, 30) not in self.deleted_secrets:
            self.deleted_secrets.append((name, 30))

    def list_workload_secret_metadata(self, account, store, resource_name):
        return [
            {
                "deployment": tags["gimme:deployment"],
                "generation": int(tags["gimme:generation"]),
                "secret_arn": (
                    f"arn:aws:secretsmanager:{store.region}:123456789012:secret:{name}"
                ),
                "secret_version_id": self.secret_current[name],
            }
            for name, tags in sorted(self.secret_tags.items())
            if tags.get("gimme:resource") == resource_name
            and name not in {secret for secret, _days in self.deleted_secrets}
        ]

    def disable_deletion_protection(self, account, network, identifier):
        self.instances[identifier] = dataclasses.replace(
            self.instances[identifier], deletion_protection=False
        )
        return self.instances[identifier]

    def describe_final_snapshot(self, account, network, snapshot_id):
        return self.snapshots.get(snapshot_id)

    def create_final_snapshot(
        self, account, network, identifier, snapshot_id, resource_name, generation
    ):
        snapshot = SnapshotObservation(
            identity=f"arn:aws:rds:{network.region}:123456789012:snapshot:{snapshot_id}",
            status="available", instance_identifier=identifier,
            ownership_verified=True, generation=generation,
        )
        self.snapshots[snapshot_id] = snapshot
        return snapshot

    def delete_instance_preserving_backups(self, account, network, identifier):
        self.delete_instance_calls += 1
        self.instances.pop(identifier, None)

    def delete_instance_dependents(self, account, network, identifier):
        return True


class FakeRDSSecrets:
    def __init__(self, adapter: FakeRDS) -> None:
        self.adapter = adapter

    def describe(self, account, store_name, store, secret):
        version = self.adapter.secret_current.get(secret)
        if version is None:
            raise SecretError("aws_secret_metadata_missing")
        return SecretMetadata(version, f"arn:{secret}")

    def resolve(self, account, store_name, store, secret, version_id):
        return json.dumps(self.adapter.secret_history[secret][version_id])


def rds_definition(**updates) -> AWSRDSPostgresResource:
    values = {
        "aws_network": "primary", "administration_target": "adminbox",
        "engine_version": "17.2", "instance_class": "db.t3.medium",
        "allocated_storage_gb": 20, "administration_security_group_id": "sg-0123456789abcdef0",
        "backup_window": "03:00-04:00", "backup_retention_days": 7,
        "maintenance_window": "sun:05:00-sun:06:00",
        "deployment_security_group_ids": {"devbox": "sg-0123456789abcdef1"},
        "workload_secret_store": "workload-secrets",
    }
    values.update(updates)
    return AWSRDSPostgresResource.model_validate(values)


def rds_state(*, bound: bool, recovery: bool = False) -> ControlState:
    base = sample_state()
    adminbox = base.targets["devbox"].model_copy(
        update={
            "host_alias": "adminbox", "hostname": "adminbox.local",
            "bootstrap_hostname": "192.0.2.20", "system_hostname": "adminbox",
            "network": TargetNetwork(mode="local_mdns", mdns_name="adminbox"),
            "role": "administration",
        }
    )
    deployment = base.deployments["example-app"]
    if bound:
        deployment = deployment.model_copy(
            update={"resources": ResourceBindings(database="primary-rds", valkey=LOCAL_VALKEY)}
        )
    if recovery:
        deployment = deployment.model_copy(
            update={"recovery": RecoveryPolicy(destination="primary")}
        )
    return ControlState(
        provider_accounts={"main": AWSProviderAccount(
            account_id="123456789012",
            inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
            resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
        )},
        secret_stores={
            "local-sops": SopsSecretStore(),
            "workload-secrets": AWSSecretsManagerStore(
                provider_account="main", region="us-east-1", prefix="gimme/workload"
            ),
        },
        backup_destinations={"primary": S3BackupDestination(
            bucket="gimme-backups", region="us-east-1", encryption=SSEAES256()
        )},
        aws_networks={"primary": AWSNetwork(
            provider_account="main", region="us-east-1", vpc_id="vpc-0123456789abcdef0",
            private_subnet_ids=["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"],
        )},
        targets={"devbox": base.targets["devbox"], "adminbox": adminbox},
        applications=base.applications,
        resources={**base.resources, "primary-rds": rds_definition()},
        deployments={"example-app": deployment},
    )


def use_rds_store(tmp_path: Path, monkeypatch, *, bound: bool, recovery: bool = False,
                  **adapter_options) -> FakeRDS:
    selected = StateStore(tmp_path / "state")
    selected.save(rds_state(bound=bound, recovery=recovery))
    monkeypatch.setattr(server_module, "store", selected)
    adapter = FakeRDS(**adapter_options)
    monkeypatch.setattr(server_module, "rds_postgres", adapter)
    monkeypatch.setattr(server_module, "aws_secrets", FakeRDSSecrets(adapter))
    monkeypatch.setattr(
        server_module.runner,
        "run",
        lambda *args, **kwargs: CommandResult(
            ["dep"],
            0,
            "GIMME_RESOURCE_VERIFIED|postgres|"
            + base64.b64encode(json.dumps({
                "citext": "1.6", "pgcrypto": "1.3", "uuid-ossp": "1.1",
            }).encode()).decode()
            + "\n",
        ),
    )
    return adapter


def test_rds_resource_is_registered_through_the_existing_resource_tools(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=False)
    larger = rds_definition(instance_class="db.m6g.large")
    plan = server_module.plan_update_resource("primary-rds", larger)
    server_module.update_resource("primary-rds", larger, str(plan["plan_id"]))

    assert server_module.store.load().resources["primary-rds"].instance_class == "db.m6g.large"


def test_list_resources_target_filter_tolerates_provider_backed_resources(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=False)

    assert set(server_module.list_resources(target="devbox")["resources"]) == {
        "devbox-postgres", "devbox-valkey",
    }
    assert "primary-rds" in server_module.list_resources()["resources"]


def test_resource_provision_is_idempotent_and_rejects_stale_plans(tmp_path, monkeypatch) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=False)
    plan = server_module.plan_apply_resource("primary-rds")

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.apply_resource("primary-rds", "plan_" + "0" * 20)
    assert adapter.create_calls == 0

    first = server_module.apply_resource("primary-rds", str(plan["plan_id"]))
    assert first["phase"] == "ready"
    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.apply_resource("primary-rds", str(plan["plan_id"]))

    fresh = server_module.plan_apply_resource("primary-rds")
    server_module.apply_resource("primary-rds", str(fresh["plan_id"]))
    assert adapter.create_calls == 1, "reconciling an existing instance must not recreate it"


def test_resource_provision_surfaces_a_bounded_provider_failure(tmp_path, monkeypatch) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=False)

    def denied(*args, **kwargs):
        raise ResourceError("aws_rds_create_access_denied")

    monkeypatch.setattr(adapter, "create_instance", denied)
    plan = server_module.plan_apply_resource("primary-rds")

    with pytest.raises(ResourceError, match="aws_rds_create_access_denied"):
        server_module.apply_resource("primary-rds", str(plan["plan_id"]))
    assert server_module.inspect_resource("primary-rds")["phase"] == "absent"


def test_bind_resource_never_exposes_credentials_anywhere(tmp_path, monkeypatch) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    seen: dict[str, object] = {}

    def fake_run(task, server, **kwargs):
        seen.update(kwargs, task=task, server=server.host_alias)
        seen["secret_document"] = json.loads(kwargs["secret_file"].read_text())
        return CommandResult(["dep"], 0, "[adminbox] GIMME_RESOURCE_BOUND|gimme_example_app\n")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_bind_resource("example-app")
    result = server_module.bind_resource("example-app", str(plan["plan_id"]))

    document = seen["secret_document"]
    assert seen["task"] == "gimme:resource:bind-postgres"
    assert seen["server"] == "adminbox"
    assert seen["resource_endpoint"] == ("db.example.test", 5432)
    assert seen["resource_trust_bundle_sha256"] == RDS_TRUST_BUNDLE_SHA256
    assert document["master_password"] == MASTER_PASSWORD
    workload_password = document["workload_password"]
    assert not Path(seen["secret_file"]).exists(), "protected secret file must be shredded"
    assert result["secret_reference"] == {
        "store": "workload-secrets", "secret": "primary-rds/example-app",
    }
    everything = " ".join([
        str(plan), str(result), str(server_module.inspect_resource("primary-rds")),
        str(server_module.list_operations(limit=200)),
        (server_module.store.root / "observed-resources" / "primary-rds.json").read_text(),
        (server_module.store.root / "operations.jsonl").read_text(),
    ])
    assert MASTER_PASSWORD not in everything
    assert workload_password not in everything
    assert adapter.secret_payloads["primary-rds/example-app"]["password"] == workload_password
    assert set(adapter.secret_payloads["primary-rds/example-app"]) == {"username", "password"}
    assert adapter.secret_payloads["primary-rds/example-app"]["username"].endswith("_g1")


def test_managed_postgres_binding_becomes_a_version_pinned_laravel_runtime(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )

    plan = server_module.plan_deployment_resources("example-app")
    keys = {item["environment_key"] for item in plan["secret_versions"]}
    assert {"DB_USERNAME", "DB_PASSWORD"} <= keys
    assert plan["ready"] is True

    captured: dict[str, object] = {}

    def capture(task, server, **kwargs):
        if task == "gimme:provision:app":
            captured.update(kwargs)
            captured["secret_document"] = json.loads(kwargs["secret_file"].read_text())
        return CommandResult(["dep"], 0, "")

    monkeypatch.setattr(server_module.runner, "run", capture)
    server_module.apply_deployment_resources("example-app", str(plan["plan_id"]))

    variables = captured["variables"]
    assert variables["DB_CONNECTION"] == "pgsql"
    assert variables["DB_HOST"] == "db.example.test"
    assert variables["DB_SSLMODE"] == "verify-full"
    assert variables["DB_SSLROOTCERT"].endswith("/.gimme/aws-rds-global-bundle.pem")
    assert captured["resource_trust_bundle_sha256"] == RDS_TRUST_BUNDLE_SHA256
    assert set(captured["secret_document"]) == {"DB_USERNAME", "DB_PASSWORD"}
    assert adapter.secret_payloads["primary-rds/example-app"]["password"] not in str(plan)


def test_managed_postgres_binding_pins_allowlisted_extension_versions(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)
    state = server_module.store.load()
    application_name = state.deployments["example-app"].application
    application = state.applications[application_name].model_copy(
        update={"postgres_extensions": ["pgcrypto", "citext"]}
    )
    server_module.store.save(
        state.model_copy(update={"applications": {application_name: application}})
    )
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    plan = server_module.plan_bind_resource("example-app")
    assert plan["postgres_extensions"] == {"citext": "1.6", "pgcrypto": "1.3"}
    captured: dict[str, object] = {}

    def bind(task, server, **kwargs):
        captured.update(kwargs)
        return CommandResult(["dep"], 0, "")

    monkeypatch.setattr(server_module.runner, "run", bind)
    server_module.bind_resource("example-app", str(plan["plan_id"]))
    allocation = resources_postgres_module.load_observed(
        server_module.store.root, "primary-rds"
    )["allocations"]["example-app"]
    assert captured["resource_extensions"] == {"citext": "1.6", "pgcrypto": "1.3"}
    assert allocation["extensions"] == {"citext": "1.6", "pgcrypto": "1.3"}


def test_rebinding_managed_postgres_reuses_the_active_generation_and_secret(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )
    first_password = adapter.secret_payloads["primary-rds/example-app"]["password"]
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )

    allocation = resources_postgres_module.load_observed(
        server_module.store.root, "primary-rds"
    )["allocations"]["example-app"]
    assert allocation["generation"] == 1
    assert adapter.secret_versions["primary-rds/example-app"] == 1
    assert adapter.secret_payloads["primary-rds/example-app"]["password"] == first_password


def test_managed_postgres_credential_rotation_switches_then_retires_old_login(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )
    tasks: list[str] = []

    def succeed(task, server, **kwargs):
        tasks.append(task)
        return CommandResult(["dep"], 0, "")

    monkeypatch.setattr(server_module.runner, "run", succeed)
    plan = server_module.plan_rotate_resource_credential("primary-rds", "example-app")
    result = server_module.apply_rotate_resource_credential(
        "primary-rds", "example-app", str(plan["plan_id"])
    )

    observed = server_module.inspect_resource("primary-rds")["allocations"]["example-app"]
    assert result["generation"] == 2 and observed["status"] == "active"
    allocation = resources_postgres_module.load_observed(
        server_module.store.root, "primary-rds"
    )["allocations"]["example-app"]
    assert allocation["generation"] == 2 and allocation["login_role"].endswith("_g2")
    assert adapter.secret_current["primary-rds/example-app"] == "v2"
    assert tasks[-1] == "gimme:resource:retire-postgres-login"
    assert "password" not in str(result)


def test_managed_postgres_rotation_restores_secret_allocation_and_environment_on_failure(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )
    failed = False

    def fail_candidate_once(task, server, **kwargs):
        nonlocal failed
        if task == "gimme:provision:app" and not failed:
            failed = True
            raise RuntimeError("candidate health failed")
        return CommandResult(["dep"], 0, "")

    monkeypatch.setattr(server_module.runner, "run", fail_candidate_once)
    plan = server_module.plan_rotate_resource_credential("primary-rds", "example-app")

    with pytest.raises(ResourceError, match="aws_rds_rotation_activation_failed"):
        server_module.apply_rotate_resource_credential(
            "primary-rds", "example-app", str(plan["plan_id"])
        )

    allocation = resources_postgres_module.load_observed(
        server_module.store.root, "primary-rds"
    )["allocations"]["example-app"]
    assert allocation["generation"] == 1 and allocation["login_role"].endswith("_g1")
    assert adapter.secret_current["primary-rds/example-app"] == "v1"


def test_managed_postgres_rotation_resumes_after_activation_interruption(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )
    interrupted = False

    def interrupt_once(task, server, **kwargs):
        nonlocal interrupted
        if task == "gimme:provision:app" and not interrupted:
            interrupted = True
            raise KeyboardInterrupt
        return CommandResult(["dep"], 0, "")

    monkeypatch.setattr(server_module.runner, "run", interrupt_once)
    plan = server_module.plan_rotate_resource_credential("primary-rds", "example-app")
    with pytest.raises(KeyboardInterrupt):
        server_module.apply_rotate_resource_credential(
            "primary-rds", "example-app", str(plan["plan_id"])
        )
    marker = resources_postgres_module.load_rotation(
        server_module.store.root, "primary-rds"
    )
    assert marker is not None and marker["phase"] == "published"

    result = server_module.apply_rotate_resource_credential(
        "primary-rds", "example-app", str(plan["plan_id"])
    )
    assert result["generation"] == 2
    assert resources_postgres_module.load_rotation(
        server_module.store.root, "primary-rds"
    ) is None
    assert adapter.secret_versions["primary-rds/example-app"] == 2


def test_managed_postgres_rotation_resumes_an_interrupted_rollback(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )
    activation_calls = 0

    def fail_candidate_and_first_restore(task, server, **kwargs):
        nonlocal activation_calls
        if task == "gimme:provision:app":
            activation_calls += 1
            if activation_calls <= 2:
                raise RuntimeError("activation interrupted")
        return CommandResult(["dep"], 0, "")

    monkeypatch.setattr(server_module.runner, "run", fail_candidate_and_first_restore)
    plan = server_module.plan_rotate_resource_credential("primary-rds", "example-app")
    with pytest.raises(ResourceError, match="aws_rds_rotation_rollback_failed"):
        server_module.apply_rotate_resource_credential(
            "primary-rds", "example-app", str(plan["plan_id"])
        )
    marker = resources_postgres_module.load_rotation(
        server_module.store.root, "primary-rds"
    )
    assert marker is not None and marker["phase"] == "rolling_back"

    with pytest.raises(ResourceError, match="aws_rds_rotation_activation_failed"):
        server_module.apply_rotate_resource_credential(
            "primary-rds", "example-app", str(plan["plan_id"])
        )
    allocation = resources_postgres_module.load_observed(
        server_module.store.root, "primary-rds"
    )["allocations"]["example-app"]
    assert allocation["generation"] == 1
    assert adapter.secret_current["primary-rds/example-app"] == "v1"
    assert resources_postgres_module.load_rotation(
        server_module.store.root, "primary-rds"
    ) is None


def test_managed_postgres_detach_reactivate_and_guarded_purge(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True, recovery=True)
    state = server_module.store.load()
    original_deployment = state.deployments["example-app"]
    server_module.store.save(state.model_copy(update={
        "provider_accounts": {
            "main": state.provider_accounts["main"].model_copy(update={
                "destructive_role_arn": (
                    "arn:aws:iam::123456789012:role/gimme-destroy"
                )
            })
        }
    }))
    backup = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", backup)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )

    def publish_recovery(request: str) -> None:
        body = f"postgres-{request}".encode()
        path = tmp_path / f"{request}.dump"
        path.write_bytes(body)
        recovery_module.create_recovery_point(
            "primary",
            server_module.store.load().backup_destinations["primary"],
            None,
            backup,
            "example-app",
            recovery_module.recovery_point_id("example-app", "primary", request),
            [recovery_module.ComponentDump(
                kind="postgres", local_path=path,
                sha256=hashlib.sha256(body).hexdigest(), bytes=len(body),
                resource_version="17.2",
            )],
        )

    publish_recovery("before-first-detach")
    tasks: list[str] = []

    def succeed(task, server, **kwargs):
        tasks.append(task)
        return CommandResult(["dep"], 0, "")

    monkeypatch.setattr(server_module.runner, "run", succeed)
    removal = server_module.plan_remove_deployment("example-app")
    server_module.remove_deployment(
        "example-app", str(removal["plan_id"]), str(removal["confirmation"])
    )
    detached = resources_postgres_module.load_observed(
        server_module.store.root, "primary-rds"
    )["allocations"]["example-app"]
    assert detached["status"] == "detached"
    assert detached["generation"] == 1
    assert detached["recovery_evidence"]["recovery_point_id"].startswith("rp_")
    assert "gimme:resource:retire-postgres-login" in tasks

    state = server_module.store.load()
    server_module.store.save(state.model_copy(update={
        "deployments": {"example-app": original_deployment}
    }))
    rebind = server_module.plan_bind_resource("example-app")
    assert rebind["reactivates_detached_allocation"] is True
    assert rebind["login_generation"] == 2
    server_module.bind_resource("example-app", str(rebind["plan_id"]))
    active = resources_postgres_module.load_observed(
        server_module.store.root, "primary-rds"
    )["allocations"]["example-app"]
    assert active["status"] == "active" and active["generation"] == 2
    assert active["recovery_evidence"] is None

    publish_recovery("before-second-detach")
    removal = server_module.plan_remove_deployment("example-app")
    server_module.remove_deployment(
        "example-app", str(removal["plan_id"]), str(removal["confirmation"])
    )
    purge = server_module.plan_purge_resource_allocation(
        "primary-rds", "example-app"
    )
    assert purge["confirmation"] == "PURGE example-app FROM primary-rds"
    with pytest.raises(ValueError, match="confirmation must exactly equal"):
        server_module.apply_purge_resource_allocation(
            "primary-rds", "example-app", str(purge["plan_id"]), "PURGE"
        )
    original_clear = resources_postgres_module.clear_allocation_purge
    clear_calls = 0

    def interrupt_after_allocation_removal(*args, **kwargs):
        nonlocal clear_calls
        clear_calls += 1
        if clear_calls == 1:
            raise RuntimeError("interrupted after allocation removal")
        return original_clear(*args, **kwargs)

    monkeypatch.setattr(
        resources_postgres_module,
        "clear_allocation_purge",
        interrupt_after_allocation_removal,
    )
    with pytest.raises(RuntimeError, match="interrupted after allocation removal"):
        server_module.apply_purge_resource_allocation(
            "primary-rds", "example-app", str(purge["plan_id"]),
            str(purge["confirmation"]),
        )
    marker = resources_postgres_module.load_allocation_purge(
        server_module.store.root, "primary-rds", "example-app"
    )
    assert marker is not None and marker["phase"] == "secret_scheduled"

    result = server_module.apply_purge_resource_allocation(
        "primary-rds", "example-app", str(purge["plan_id"]),
        str(purge["confirmation"]),
    )
    assert result["changed"] is False
    assert result["secret_recovery_window_days"] == 30
    assert adapter.deleted_secrets == [("primary-rds/example-app", 30)]
    assert resources_postgres_module.load_observed(
        server_module.store.root, "primary-rds"
    )["allocations"] == {}
    assert tasks[-1] == "gimme:resource:purge-postgres-allocation"


def test_managed_postgres_destroy_requires_evidence_and_retains_final_snapshot(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True, recovery=True)
    state = server_module.store.load()
    server_module.store.save(state.model_copy(update={
        "provider_accounts": {
            "main": state.provider_accounts["main"].model_copy(update={
                "destructive_role_arn": (
                    "arn:aws:iam::123456789012:role/gimme-destroy"
                )
            })
        }
    }))
    backup = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", backup)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )
    body = b"final-postgres-recovery"
    dump = tmp_path / "final.dump"
    dump.write_bytes(body)
    recovery_module.create_recovery_point(
        "primary", server_module.store.load().backup_destinations["primary"], None,
        backup, "example-app",
        recovery_module.recovery_point_id("example-app", "primary", "before-destroy"),
        [recovery_module.ComponentDump(
            kind="postgres", local_path=dump,
            sha256=hashlib.sha256(body).hexdigest(), bytes=len(body),
            resource_version="17.2",
        )],
    )
    monkeypatch.setattr(
        server_module.runner, "run", lambda *a, **k: CommandResult(["dep"], 0, "")
    )
    removal = server_module.plan_remove_deployment("example-app")
    server_module.remove_deployment(
        "example-app", str(removal["plan_id"]), str(removal["confirmation"])
    )
    plan = server_module.plan_destroy_resource("primary-rds")
    assert plan["confirmation"] == "DESTROY RESOURCE primary-rds"
    assert len(plan["detached_allocations"]) == 1

    first = server_module.apply_destroy_resource(
        "primary-rds", str(plan["plan_id"]), str(plan["confirmation"])
    )
    assert first["phase"] == "disabling_protection" and first["destroyed"] is False
    second = server_module.apply_destroy_resource(
        "primary-rds", str(plan["plan_id"]), str(plan["confirmation"])
    )
    assert second["destroyed"] is True
    assert "primary-rds" not in server_module.store.load().resources
    assert second["final_snapshot"] in adapter.snapshots
    assert adapter.snapshots[second["final_snapshot"]].status == "available"
    assert adapter.delete_instance_calls == 1


def test_managed_postgres_reconstructs_unambiguous_active_allocation(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )
    observed_path = (
        server_module.store.root / "observed-resources" / "primary-rds.json"
    )
    observed_path.unlink()

    plan = server_module.plan_apply_resource("primary-rds")
    result = server_module.apply_resource("primary-rds", str(plan["plan_id"]))

    assert result["reconstructed_allocations"] == 1
    allocation = resources_postgres_module.load_observed(
        server_module.store.root, "primary-rds"
    )["allocations"]["example-app"]
    assert allocation["status"] == "active" and allocation["generation"] == 1
    assert allocation["login_role"].endswith("_g1")


def test_managed_postgres_creates_a_verified_manual_recovery_point(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True, recovery=True)
    backup = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", backup)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )
    captured: dict[str, object] = {}

    def capture(task, server, **kwargs):
        if task == "gimme:backup:dump-postgres":
            body = b"managed-postgres-dump"
            kwargs["backup_local_path"].write_bytes(body)
            captured["credential"] = json.loads(kwargs["secret_file"].read_text())
            captured["variables"] = kwargs["variables"]
            return CommandResult(
                ["dep"], 0,
                f"GIMME_BACKUP|{hashlib.sha256(body).hexdigest()}|{len(body)}\n",
            )
        return CommandResult(["dep"], 0, "")

    monkeypatch.setattr(server_module.runner, "run", capture)
    plan = server_module.plan_create_recovery_point("example-app", "managed-manual")
    result = server_module.create_recovery_point(
        "example-app", "managed-manual", str(plan["plan_id"])
    )

    assert result["changed"] is True
    assert result["recovery_point"]["components"][0]["resource_version"] == "17.2"
    assert captured["credential"] == adapter.secret_payloads["primary-rds/example-app"]
    assert captured["variables"]["DB_SSLMODE"] == "verify-full"
    assert server_module.list_recovery_points("example-app")["recovery_points"][0][
        "state"
    ] == "verified"


def test_managed_postgres_reconstruction_refuses_unmatched_or_corrupt_evidence(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )
    observed_path = (
        server_module.store.root / "observed-resources" / "primary-rds.json"
    )
    observed_path.unlink()
    state = server_module.store.load()
    server_module.store.save(state.model_copy(update={"deployments": {}}))
    plan = server_module.plan_apply_resource("primary-rds")
    with pytest.raises(ResourceError, match="aws_rds_reconstruction_ambiguous"):
        server_module.apply_resource("primary-rds", str(plan["plan_id"]))
    assert not observed_path.exists(), "ambiguous reconstruction must not overwrite evidence"

    observed_path.parent.mkdir(parents=True, exist_ok=True)
    observed_path.write_text("{not-json")
    with pytest.raises(ResourceError, match="observed_resource_invalid"):
        server_module.plan_apply_resource("primary-rds")


def test_failed_database_binding_cleans_protected_credentials_before_raising(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    seen = {}

    def fail_run(task, server, **kwargs):
        secret_file = kwargs["secret_file"]
        seen["path"] = secret_file
        seen["document"] = json.loads(secret_file.read_text())
        raise RuntimeError("bounded target failure")

    monkeypatch.setattr(server_module.runner, "run", fail_run)
    plan = server_module.plan_bind_resource("example-app")

    with pytest.raises(RuntimeError, match="bounded target failure") as failure:
        server_module.bind_resource("example-app", str(plan["plan_id"]))

    document = seen["document"]
    assert not Path(seen["path"]).exists()
    assert all(value not in str(failure.value) for value in document.values())
    assert server_module.inspect_resource("primary-rds")["allocations"] == {}


@pytest.mark.parametrize("region", ["us-gov-west-1", "cn-north-1"])
def test_regions_without_a_pinned_trust_bundle_are_refused_before_anything_is_created(
    tmp_path, monkeypatch, region
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True)
    state = server_module.store.load()
    network = state.aws_networks["primary"].model_copy(update={"region": region})
    server_module.store.save(state.model_copy(update={"aws_networks": {"primary": network}}))
    monkeypatch.setattr(
        server_module.runner, "run", lambda *a, **k: pytest.fail("must not reach the target")
    )
    stale = "plan_" + "0" * 20
    refused = pytest.raises(ResourceError, match="aws_rds_tls_region_unsupported")

    with refused:
        server_module.plan_apply_resource("primary-rds")
    with refused:
        server_module.apply_resource("primary-rds", stale)
    with refused:
        server_module.plan_bind_resource("example-app")
    with refused:
        server_module.bind_resource("example-app", stale)
    with refused:
        server_module.register_resource("second-rds", rds_definition())
    assert "second-rds" not in server_module.store.load().resources
    assert adapter.create_calls == 0
    # A stranded Resource must still be inspectable and retainable.
    server_module.inspect_resource("primary-rds")
    server_module.store.save(
        server_module.store.load().model_copy(update={"deployments": {}})
    )
    cleanup = server_module.plan_cleanup_resource("primary-rds")
    server_module.apply_cleanup_resource(
        "primary-rds", str(cleanup["plan_id"]), str(cleanup["confirmation"])
    )
    assert "primary-rds" not in server_module.store.load().resources


def _with_second_network_and_store(monkeypatch) -> None:
    state = server_module.store.load()
    server_module.store.save(state.model_copy(update={
        "aws_networks": {**state.aws_networks, "secondary": state.aws_networks["primary"]},
        "secret_stores": {
            **state.secret_stores,
            "other-secrets": state.secret_stores["workload-secrets"],
        },
    }))


def _bind_example_app(monkeypatch) -> None:
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    monkeypatch.setattr(
        server_module.runner, "run",
        lambda *a, **k: CommandResult(["dep"], 0, "GIMME_RESOURCE_BOUND|gimme_example_app\n"),
    )
    server_module.bind_resource(
        "example-app", str(server_module.plan_bind_resource("example-app")["plan_id"])
    )


def _assert_update_forbidden(code: str, **updates) -> None:
    before = server_module.store.load()
    definition = rds_definition(**{"allocated_storage_gb": 100, **updates})
    with pytest.raises(ResourceError, match=f"^aws_rds_update_forbidden_{code}$"):
        server_module.plan_update_resource("primary-rds", definition)
    with pytest.raises(ResourceError, match=f"^aws_rds_update_forbidden_{code}$"):
        server_module.update_resource("primary-rds", definition, "plan_" + "0" * 20)
    assert server_module.store.load() == before


def test_rds_updates_that_adr_0008_forbids_are_rejected_and_change_nothing(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)
    _with_second_network_and_store(monkeypatch)
    state = server_module.store.load()
    server_module.store.save(state.model_copy(update={"resources": {
        **state.resources, "primary-rds": rds_definition(allocated_storage_gb=100),
    }}))
    _bind_example_app(monkeypatch)

    _assert_update_forbidden("aws_network", aws_network="secondary")
    _assert_update_forbidden("engine_major", engine_version="18.1")
    _assert_update_forbidden("allocated_storage_gb", allocated_storage_gb=50)
    _assert_update_forbidden("workload_secret_store", workload_secret_store="other-secrets")
    _assert_update_forbidden("deployment_security_group_ids", deployment_security_group_ids={})
    local = sample_state().resources["devbox-postgres"]
    with pytest.raises(ResourceError, match="^aws_rds_update_forbidden_provider$"):
        server_module.plan_update_resource("primary-rds", local)
    with pytest.raises(ResourceError, match="^aws_rds_update_forbidden_provider$"):
        server_module.plan_update_resource("devbox-postgres", rds_definition())
    with pytest.raises(KeyError, match="no-such-resource"):
        server_module.plan_update_resource("no-such-resource", rds_definition())


def test_rds_updates_within_the_allowlist_register_as_before(tmp_path, monkeypatch) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=False)
    _with_second_network_and_store(monkeypatch)
    allowed = [
        {"engine_version": "17.5"},
        {"instance_class": "db.m6g.large"},
        {"allocated_storage_gb": 100},
        {"administration_security_group_id": "sg-0123456789abcdef9"},
        {"deployment_security_group_ids": {}},
        {"deployment_security_group_ids": {"devbox": "sg-0123456789abcdef2"}},
        {"retain_on_removal": False},
        {"workload_secret_store": "other-secrets"},
    ]
    for updates in allowed:
        definition = rds_definition(**updates)
        plan = server_module.plan_update_resource("primary-rds", definition)
        server_module.update_resource("primary-rds", definition, str(plan["plan_id"]))
        assert server_module.store.load().resources["primary-rds"] == definition
        server_module.store.save(server_module.store.load().model_copy(update={
            "resources": {**server_module.store.load().resources,
                          "primary-rds": rds_definition()},
        }))


def test_a_target_security_group_can_be_removed_once_no_bound_deployment_uses_it(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=False)
    definition = rds_definition(deployment_security_group_ids={})

    plan = server_module.plan_update_resource("primary-rds", definition)
    server_module.update_resource("primary-rds", definition, str(plan["plan_id"]))

    assert server_module.store.load().resources["primary-rds"] == definition


def test_inspect_resource_reports_drift_only_from_a_successful_live_read(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=False)
    assert "drift" not in server_module.inspect_resource("primary-rds"), "absent instance"
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )

    assert server_module.inspect_resource("primary-rds")["drift"] == {
        "fields": {}, "modification_pending": False,
    }

    identifier = next(iter(adapter.instances))
    adapter.instances[identifier] = dataclasses.replace(
        adapter.instances[identifier], instance_class="db.t3.small", allocated_storage_gb=50,
        security_group_ids=("sg-0123456789abcdef0",), modification_pending=True,
    )
    drift = server_module.inspect_resource("primary-rds")["drift"]
    assert drift["modification_pending"] is True
    assert drift["fields"] == {
        "instance_class": {"desired": "db.t3.medium", "live": "db.t3.small"},
        "allocated_storage_gb": {"desired": 20, "live": 50},
        "security_group_ids": {
            "desired": ["sg-0123456789abcdef0", "sg-0123456789abcdef1"],
            "live": ["sg-0123456789abcdef0"],
        },
    }

    adapter.fail_describe = True
    cached = server_module.inspect_resource("primary-rds")
    assert cached["refresh_error"] == "aws_rds_describe_throttled"
    assert "drift" not in cached


def test_resource_provision_plan_describes_convergence_and_binds_the_security_groups(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=False)
    monkeypatch.setattr(
        adapter, "describe_instance", lambda *a, **k: pytest.fail("planning must stay local")
    )
    plan = server_module.plan_apply_resource("primary-rds")
    effects = " ".join(plan["effects"])

    assert "one immediate modification" in effects
    assert "restarts the instance" in effects and "fails over" in effects
    assert "without forced failover" in effects
    assert "only polled" not in effects
    assert plan["security_group_ids"] == ["sg-0123456789abcdef0", "sg-0123456789abcdef1"]

    changed = rds_definition(deployment_security_group_ids={"devbox": "sg-0123456789abcdef9"})
    server_module.update_resource(
        "primary-rds", changed,
        str(server_module.plan_update_resource("primary-rds", changed)["plan_id"]),
    )
    assert server_module.plan_apply_resource("primary-rds")["plan_id"] != plan["plan_id"]


def test_apply_resource_converges_an_existing_instance_and_reports_only_field_names(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=False)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    larger = rds_definition(instance_class="db.m6g.large", allocated_storage_gb=40)
    server_module.update_resource(
        "primary-rds", larger,
        str(server_module.plan_update_resource("primary-rds", larger)["plan_id"]),
    )

    result = server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )

    assert adapter.modify_calls == [{"DBInstanceClass": "db.m6g.large", "AllocatedStorage": 40}]
    assert adapter.create_calls == 1 and adapter.reboot_calls == 0
    assert result["modified_fields"] == ["AllocatedStorage", "DBInstanceClass"]
    assert result["phase"] == "ready"
    assert MASTER_PASSWORD not in str(result)
    again = server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    assert again["modified_fields"] == [] and len(adapter.modify_calls) == 1


def test_inspect_resource_is_pending_while_a_managed_change_is_unapplied(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=False)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    identifier = next(iter(adapter.instances))
    assert server_module.inspect_resource("primary-rds")["phase"] == "ready"

    adapter.instances[identifier] = dataclasses.replace(
        adapter.instances[identifier], pending_instance_class="db.m6g.large",
        modification_pending=True,
    )
    assert server_module.inspect_resource("primary-rds")["phase"] == "pending"
    adapter.instances[identifier] = dataclasses.replace(
        adapter.instances[identifier], pending_instance_class=None,
    )
    assert server_module.inspect_resource("primary-rds")["phase"] == "ready", (
        "an unmanaged pending value must not hold readiness"
    )


def test_bind_resource_rejects_stale_plans_and_unready_resources(tmp_path, monkeypatch) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)
    monkeypatch.setattr(
        server_module.runner, "run", lambda *a, **k: pytest.fail("must not reach the target")
    )
    plan = server_module.plan_bind_resource("example-app")
    assert plan["resource_ready"] is False

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.bind_resource("example-app", "plan_" + "0" * 20)
    with pytest.raises(ValueError, match="not ready"):
        server_module.bind_resource("example-app", str(plan["plan_id"]))


def test_bind_resource_fails_closed_on_live_rds_policy_drift(tmp_path, monkeypatch) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    identifier = next(iter(adapter.instances))
    adapter.instances[identifier] = dataclasses.replace(
        adapter.instances[identifier], multi_az=False
    )
    inspection = server_module.inspect_resource("primary-rds")
    assert inspection["phase"] == "pending"
    assert "aws_rds_not_ready_multi_az" in inspection["readiness_issues"]
    plan = server_module.plan_bind_resource("example-app")

    with pytest.raises(ResourceError, match="aws_rds_binding_resource_not_ready"):
        server_module.bind_resource("example-app", str(plan["plan_id"]))
    assert adapter.secret_payloads == {}


def test_bind_resource_fails_closed_when_the_master_credential_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True, fail_master=True)
    with pytest.raises(ResourceError, match="aws_rds_master_secret_access_denied"):
        server_module.apply_resource(
            "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
        )
    assert server_module.inspect_resource("primary-rds")["allocations"] == {}


def test_inspect_resource_prefers_live_state_and_falls_back_to_the_cache(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=False)
    assert server_module.inspect_resource("primary-rds")["phase"] == "absent"
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )

    live = server_module.inspect_resource("primary-rds")
    assert (live["source"], live["phase"], live["engine_version"]) == ("live", "ready", "17.2")

    adapter.fail_describe = True
    cached = server_module.inspect_resource("primary-rds")
    assert cached["source"] == "cache"
    assert cached["refresh_error"] == "aws_rds_describe_throttled"
    assert cached["phase"] == "ready"


def test_inspect_resource_reports_target_local_resources_without_provider_calls(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=False)

    assert server_module.inspect_resource("devbox-postgres") == {
        "resource": "devbox-postgres", "provider": "target_local", "target": "devbox",
        "kind": "postgres", "version": "17.2",
    }


def test_cleanup_retains_a_managed_resource_and_requires_exact_confirmation(
    tmp_path, monkeypatch
) -> None:
    adapter = use_rds_store(tmp_path, monkeypatch, bound=False)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    plan = server_module.plan_cleanup_resource("primary-rds")
    assert plan["confirmation"] == "RETAIN primary-rds"

    with pytest.raises(ValueError, match="invalid or stale"):
        server_module.apply_cleanup_resource(
            "primary-rds", "plan_" + "0" * 20, "RETAIN primary-rds"
        )
    with pytest.raises(ValueError, match="confirmation must exactly equal"):
        server_module.apply_cleanup_resource("primary-rds", str(plan["plan_id"]), "yes")
    assert "primary-rds" in server_module.store.load().resources

    result = server_module.apply_cleanup_resource(
        "primary-rds", str(plan["plan_id"]), "RETAIN primary-rds"
    )

    assert (result["changed"], result["resource"], result["retained"]) == (
        True, "primary-rds", True,
    )
    assert "primary-rds" not in server_module.store.load().resources
    tombstone = server_module.store.root / "retained-resources" / "primary-rds.json"
    assert json.loads(tombstone.read_text()) == {
        "schema_version": 1,
        "resource": "primary-rds",
        "provider_account": "main",
        "aws_network": "primary",
        "workload_secret_store": "workload-secrets",
        "identity_fingerprint": resources_postgres_module.identity_fingerprint(
            adapter.instances["gimme-primary-rds"].identity
        ),
        "detached_allocations": 0,
    }
    assert adapter.instances, "cleanup must never delete the provider instance"

    state = server_module.store.load()
    server_module.store.save(state.model_copy(update={
        "secret_stores": {
            name: store
            for name, store in state.secret_stores.items()
            if name != "workload-secrets"
        },
        "aws_networks": {},
    }))
    with pytest.raises(ValueError, match="retained resource"):
        server_module.plan_remove_provider_account("main")
    forget = server_module.plan_forget_resource("primary-rds")
    with pytest.raises(ValueError, match="confirmation must exactly equal"):
        server_module.apply_forget_resource(
            "primary-rds", str(forget["plan_id"]), "FORGET primary-rds"
        )
    server_module.apply_forget_resource(
        "primary-rds", str(forget["plan_id"]),
        "FORGET RETAINED RESOURCE primary-rds",
    )
    assert not tombstone.exists()
    assert server_module.plan_remove_provider_account("main")["kind"] == (
        "provider_account_removal"
    )


def test_cleanup_is_refused_while_a_deployment_still_references_the_resource(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)

    with pytest.raises(ValueError, match="still referenced"):
        server_module.plan_cleanup_resource("primary-rds")


def test_unready_managed_database_binding_blocks_provisioning_and_release(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)
    monkeypatch.setattr(
        server_module.runner, "run", lambda *a, **k: pytest.fail("must not reach the target")
    )

    plan = server_module.plan_deployment_resources("example-app")
    assert plan["ready"] is False
    assert plan["readiness_issues"] == ["postgres_resource_not_ready"]
    with pytest.raises(ValueError, match="not ready"):
        server_module.apply_deployment_resources("example-app", str(plan["plan_id"]))
    with pytest.raises(ValueError, match="postgres_resource_not_ready"):
        server_module.plan_deployment("example-app")


def test_deployment_tasks_send_only_target_local_resources_to_the_recipe(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)
    captured: dict[str, object] = {}

    def fake_run(task, server, **kwargs):
        captured.update(kwargs)
        return CommandResult(["dep"], 0, "")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    server_module._run_deployment("gimme:preflight:runtimes", "example-app")

    assert set(captured["resources"]) == {"cache"}


def test_recovery_points_are_plannable_for_managed_databases(tmp_path, monkeypatch) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True, recovery=True)

    plan = server_module.plan_create_recovery_point("example-app", "req-1")
    assert plan["kind"] == "recovery_point_creation"
