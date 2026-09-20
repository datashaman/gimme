import dataclasses
import hashlib
import json
import threading
from pathlib import Path

from fastmcp import Client
import pytest

from gimme.config import (
    ArtisanConfig, HealthCheckConfig, HorizonWorkerConfig, SchedulerConfig, StackConfig,
)
from gimme.control import (
    AWSNetwork,
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
)
from gimme.deployer import CommandResult
from gimme.recovery import ObjectMetadata, RecoveryError
from gimme.recovery import append_restore_event, recovery_point_id, restore_event_key
from gimme.resources_postgres import RDS_TRUST_BUNDLE_SHA256, InstanceObservation, ResourceError
import gimme.server as server_module
import gimme.control_plans as control_plans_module
import gimme.recovery as recovery_module
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


def use_store(tmp_path: Path, monkeypatch) -> StateStore:
    selected = StateStore(tmp_path / "state")
    selected.save(sample_state())
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


async def test_hard_v4_tool_surface() -> None:
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
        "plan_target_stack",
        "apply_target_stack",
        "plan_deployment_runtimes",
        "apply_deployment_runtimes",
        "plan_deployment_resources",
        "apply_deployment_resources",
        "plan_deployment",
        "apply_deployment",
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
        "plan_create_recovery_point",
        "create_recovery_point",
        "list_recovery_points",
        "list_restores",
        "plan_restore_deployment",
        "apply_restore_deployment",
        "plan_verify_restore",
        "apply_verify_restore",
    }
    assert {str(resource.uri) for resource in resources} == {
        "gimme://state", "gimme://operations"
    }
    assert {template.uriTemplate for template in templates} == {
        "gimme://targets/{name}",
        "gimme://applications/{name}",
        "gimme://resources/{name}",
        "gimme://deployments/{name}",
        "gimme://operations/{correlation_id}",
        "gimme://provider-accounts/{name}",
        "gimme://secret-stores/{name}",
        "gimme://backup-destinations/{name}",
        "gimme://aws-networks/{name}/valkey-options",
        "gimme://deployments/{name}/restores/{request_id}",
    }
    assert all(tool.annotations is not None for tool in tools)
    reference = (Path(__file__).parents[1] / "docs" / "reference" / "mcp.md").read_text()
    assert all(f"`{name}`" in reference for name in names)
    assert all(f"`{template.uriTemplate}`" in reference for template in templates)


def test_register_deployment_allocates_immutable_placement(tmp_path, monkeypatch) -> None:
    selected = use_store(tmp_path, monkeypatch)
    definition = DeploymentRegistration(
        application="example-app",
        target="devbox",
        stage="preview",
        source=DeploymentSource(kind="branch", ref="feature/demo"),
        app_env="local",
        runtimes=sample_state().deployments["example-app"].runtimes,
        resources=sample_state().deployments["example-app"].resources,
    )
    result = server_module.register_deployment("example-preview", definition)
    placement = selected.deployment("example-preview").placement

    assert result["placement"] == placement.model_dump(mode="json")
    assert placement.relative_path == "deployments/example-preview"
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
    assert value["schema_version"] == 5
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

    def fake_run(*args, **kwargs):
        calls.append(kwargs)
        content = b"pg-dump-bytes"
        kwargs["backup_local_path"].write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        return CommandResult(["dep"], 0, f"[integration] GIMME_BACKUP|{sha256}|{len(content)}\n")

    monkeypatch.setattr(server_module.runner, "run", fake_run)

    plan = server_module.plan_create_recovery_point("example-app", "req-1")
    result = server_module.create_recovery_point(
        "example-app", "req-1", str(plan["plan_id"])
    )
    assert result["changed"] is True
    assert len(calls) == 1

    duplicate = server_module.create_recovery_point(
        "example-app", "req-1", str(plan["plan_id"])
    )
    assert duplicate["changed"] is False
    assert len(calls) == 1, "duplicate apply must not re-run pg_dump"

    inventory = server_module.list_recovery_points("example-app")
    assert len(inventory["recovery_points"]) == 1
    assert inventory["recovery_points"][0]["recovery_point_id"] == (
        result["recovery_point"]["recovery_point_id"]
    )
    encoded = str(inventory)
    assert "pg-dump-bytes" not in encoded
    assert "gimme/recovery-points" not in encoded
    assert "version_id" not in encoded


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

    def fake_run(task, *args, **kwargs):
        calls.append(task)
        return CommandResult(["dep"], 0, "GIMME_POSTGRES_RESTORE_PREFLIGHT|nonempty")

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
    monkeypatch.setattr(
        server_module.runner, "run",
        lambda *args, **kwargs: CommandResult(
            ["dep"], 0, "GIMME_POSTGRES_RESTORE_PREFLIGHT|empty"
        ),
    )

    plan = server_module.plan_restore_deployment("example-app", point, "restore-1")

    assert plan["ready"] is False
    assert plan["readiness_issues"] == ["valkey_restore_unsupported"]
    assert plan["selected_components"] == ["postgres", "valkey"]
    assert plan["untouched_components"] == []
    assert plan["partial"] is False

    partial = server_module.plan_restore_deployment(
        "example-app", point, "restore-2", ["postgres"]
    )

    assert partial["ready"] is True
    assert partial["selected_components"] == ["postgres"]
    assert partial["untouched_components"] == ["valkey"]
    assert partial["partial"] is True
    assert partial["confirmation"] == (
        f"PARTIAL RESTORE DEPLOYMENT example-app FROM {point} COMPONENTS postgres "
        "BREAK CONSISTENCY WITH valkey"
    )


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
            ["dep"], 0, "GIMME_POSTGRES_RESTORE_PREFLIGHT|nonempty"
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
            return CommandResult(["dep"], 0, "GIMME_POSTGRES_RESTORE_PREFLIGHT|nonempty")
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
            return CommandResult(["dep"], 0, "GIMME_POSTGRES_RESTORE_PREFLIGHT|empty")
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


def test_delete_recovery_point_requires_both_confirmations_for_the_last_point(
    tmp_path, monkeypatch
) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    adapter = FakeS3()
    monkeypatch.setattr(server_module, "backup_s3", adapter)

    def fake_run(*args, **kwargs):
        content = b"pg-dump-bytes"
        kwargs["backup_local_path"].write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        return CommandResult(["dep"], 0, f"GIMME_BACKUP|{digest}|{len(content)}")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    creation = server_module.plan_create_recovery_point("example-app", "req-1")
    created = server_module.create_recovery_point(
        "example-app", "req-1", str(creation["plan_id"])
    )
    point_id = str(created["recovery_point"]["recovery_point_id"])
    plan = server_module.plan_delete_recovery_point("example-app", point_id)

    assert plan["components"] == 1
    assert plan["bytes"] == len(b"pg-dump-bytes")
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

    def fake_run(*args, **kwargs):
        content = b"pg-dump-bytes"
        kwargs["backup_local_path"].write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        return CommandResult(["dep"], 0, f"GIMME_BACKUP|{digest}|{len(content)}")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
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

    def fake_run(*args, **kwargs):
        content = b"pg-dump-bytes"
        kwargs["backup_local_path"].write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        return CommandResult(["dep"], 0, f"GIMME_BACKUP|{digest}|{len(content)}")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
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
    calls = []

    def fake_run(task, *args, **kwargs):
        calls.append((task, kwargs.get("recovery_action")))
        if task == "gimme:backup:dump-postgres":
            content = b"postgres-dump"
            kwargs["backup_local_path"].write_bytes(content)
            return CommandResult(
                ["dep"], 0,
                f"GIMME_BACKUP|{hashlib.sha256(content).hexdigest()}|{len(content)}",
            )
        if task == "gimme:backup:capture-valkey":
            content = b'{"format":"gimme-valkey-v1"}\n'
            kwargs["backup_local_path"].write_bytes(content)
            return CommandResult(
                ["dep"], 0,
                "GIMME_VALKEY_BACKUP|"
                f"{hashlib.sha256(content).hexdigest()}|{len(content)}|0|"
                "2026-09-19T10:00:00+00:00",
            )
        return CommandResult(["dep"], 0, "maintenance")

    monkeypatch.setattr(server_module, "_run_deployment", fake_run)
    result = server_module.create_recovery_point(
        "example-app", "req-1", str(inclusive["plan_id"])
    )

    assert calls == [
        ("gimme:recovery:maintenance", "enter"),
        ("gimme:backup:dump-postgres", None),
        ("gimme:backup:capture-valkey", None),
        ("gimme:recovery:maintenance", "exit"),
    ]
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
        calls.append((task, kwargs.get("recovery_action")))
        if task == "gimme:backup:dump-postgres":
            content = b"postgres-dump"
            kwargs["backup_local_path"].write_bytes(content)
            return CommandResult(
                ["dep"], 0,
                f"GIMME_BACKUP|{hashlib.sha256(content).hexdigest()}|{len(content)}",
            )
        if task == "gimme:backup:capture-valkey":
            raise RuntimeError("private key material must never escape")
        return CommandResult(["dep"], 0, "maintenance")

    monkeypatch.setattr(server_module, "_run_deployment", fake_run)
    plan = server_module.plan_create_recovery_point("example-app", "req-1")

    with pytest.raises(RecoveryError, match="^recovery_capture_failed$"):
        server_module.create_recovery_point("example-app", "req-1", str(plan["plan_id"]))

    assert calls[-1] == ("gimme:recovery:maintenance", "exit")
    assert adapter.objects == {}


def test_create_recovery_point_rejects_mismatched_dump_metadata(tmp_path, monkeypatch) -> None:
    use_recovery_store(tmp_path, monkeypatch)
    monkeypatch.setattr(server_module, "backup_s3", FakeS3())

    def fake_run(*args, **kwargs):
        kwargs["backup_local_path"].write_bytes(b"actual-bytes")
        return CommandResult(["dep"], 0, "[integration] GIMME_BACKUP|" + "0" * 64 + "|999\n")

    monkeypatch.setattr(server_module.runner, "run", fake_run)
    plan = server_module.plan_create_recovery_point("example-app", "req-1")

    with pytest.raises(RecoveryError, match="recovery_dump_metadata_invalid"):
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

    def create_workload_secret(self, account, store, name, tags, payload):
        self.secret_payloads[name] = payload
        return f"arn:aws:secretsmanager:{store.region}:123456789012:secret:{name}", "v1"


def rds_definition(**updates) -> AWSRDSPostgresResource:
    values = {
        "aws_network": "primary", "administration_target": "adminbox",
        "engine_version": "17.2", "instance_class": "db.t3.medium",
        "allocated_storage_gb": 20, "administration_security_group_id": "sg-0123456789abcdef0",
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


def test_bind_resource_fails_closed_when_the_master_credential_is_unavailable(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True, fail_master=True)
    server_module.apply_resource(
        "primary-rds", str(server_module.plan_apply_resource("primary-rds")["plan_id"])
    )
    monkeypatch.setattr(
        server_module.runner, "run", lambda *a, **k: pytest.fail("must not reach the target")
    )
    plan = server_module.plan_bind_resource("example-app")

    with pytest.raises(ResourceError, match="aws_rds_master_secret_access_denied"):
        server_module.bind_resource("example-app", str(plan["plan_id"]))
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
    assert (server_module.store.root / "retained-resources" / "primary-rds.json").is_file()
    assert adapter.instances, "cleanup must never delete the provider instance"


def test_cleanup_is_refused_while_a_deployment_still_references_the_resource(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)

    with pytest.raises(ValueError, match="still referenced"):
        server_module.plan_cleanup_resource("primary-rds")


def test_managed_database_binding_blocks_local_provisioning_and_release(
    tmp_path, monkeypatch
) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True)
    monkeypatch.setattr(
        server_module.runner, "run", lambda *a, **k: pytest.fail("must not reach the target")
    )

    plan = server_module.plan_deployment_resources("example-app")
    assert plan["ready"] is False
    assert "managed resource primary-rds" in " ".join(plan["readiness_issues"])
    with pytest.raises(ValueError, match="not ready"):
        server_module.apply_deployment_resources("example-app", str(plan["plan_id"]))
    with pytest.raises(ValueError, match="managed resource primary-rds"):
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


def test_recovery_points_are_refused_for_managed_databases(tmp_path, monkeypatch) -> None:
    use_rds_store(tmp_path, monkeypatch, bound=True, recovery=True)

    with pytest.raises(ValueError, match="target-local PostgreSQL only"):
        server_module.plan_create_recovery_point("example-app", "req-1")
