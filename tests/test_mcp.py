import hashlib
from pathlib import Path

from fastmcp import Client
import pytest

from gimme.config import (
    ArtisanConfig, HealthCheckConfig, HorizonWorkerConfig, SchedulerConfig, StackConfig,
)
from gimme.control import (
    AWSProviderAccount,
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
    ResourceConfig,
    RuntimePin,
    S3BackupDestination,
    SecretReference,
    SSEAES256,
    StateStore,
    TargetConfig,
    TargetNetwork,
)
from gimme.deployer import CommandResult
from gimme.recovery import ObjectMetadata, RecoveryError
import gimme.server as server_module
import gimme.control_plans as control_plans_module
from gimme.server import mcp


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
        resources=ResourceBindings(database="devbox-postgres", cache="devbox-valkey"),
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


class FakeS3:
    def __init__(self, *, versioning: str = "Enabled") -> None:
        self.versioning = versioning
        self.objects: dict[str, bytes] = {}
        self.puts = 0

    def bucket_versioning(self, destination, credentials) -> str:
        return self.versioning

    def put_object(self, destination, credentials, key, body, sha256) -> ObjectMetadata:
        self.puts += 1
        self.objects[key] = body
        return ObjectMetadata(bytes=len(body), sha256=sha256, server_side_encryption="AES256")

    def head_object(self, destination, credentials, key) -> ObjectMetadata | None:
        body = self.objects.get(key)
        if body is None:
            return None
        return ObjectMetadata(
            bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
            server_side_encryption="AES256",
        )

    def get_object(self, destination, credentials, key) -> bytes:
        return self.objects[key]

    def delete_object(self, destination, credentials, key, version_id=None) -> None:
        self.objects.pop(key, None)

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
    assert value["schema_version"] == 4
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


def test_create_recovery_point_requires_a_bound_recovery_policy(tmp_path, monkeypatch) -> None:
    use_store(tmp_path, monkeypatch)

    with pytest.raises(ValueError, match="no Recovery Policy bound"):
        server_module.plan_create_recovery_point("example-app", "req-1")


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
