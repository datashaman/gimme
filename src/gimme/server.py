from __future__ import annotations

import fcntl
import os
import re
import shutil  # noqa: F401 -- preserved monkeypatch seam for Recovery capacity tests
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Annotated, Any, Callable, Iterator, Literal, ParamSpec, TypeVar, cast

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from gimme import recovery as recovery_module
from gimme import resources_postgres as resources_postgres_module
from gimme import resources_valkey as resources_valkey_module
from gimme import valkey_recovery
from gimme.control import (
    AWSElastiCacheValkeyResource, AWSNetwork, AWSProviderAccount, AWSRDSPostgresResource,
    AWSSecretsManagerStore, ApplicationConfig,
    ControlState, DeploymentConfig, DeploymentRegistration,
    ManualRecoveryCadence, Resource, ResourceConfig, S3BackupDestination, SecretReference,
    SecretStore, StateStore, TargetConfig,
    legacy_app, legacy_server, new_placement, target_sites,
)
from gimme.control_plans import (
    deployment_removal_plan, exact_plan, migration_plan, registration_update_plan,
    resource_cleanup_plan,
    resource_forget_plan, valkey_destroy_plan,
    valkey_restore_plan, valkey_rotation_plan,
)
from gimme.deployer import CommandResult, DeployerRunner
from gimme.deployment_resource_orchestration import DeploymentResourceOrchestrator
from gimme.deployment_release_orchestration import DeploymentReleaseOrchestrator
from gimme.journal import OperationJournal
from gimme.control_plane_registration_orchestration import (
    ControlPlaneRegistrationOrchestrator,
)
from gimme.recovery import ComponentDump
from gimme.recovery_orchestration import RecoveryOrchestrator
from gimme.resource_orchestration import ManagedResourceOrchestrator
from gimme.resources_postgres import ResourceError
from gimme.secrets import BotoAWSSecretAdapter
from gimme.secrets import SecretError as SecretError  # noqa: F401 -- compatibility export
from gimme.target_runtime_orchestration import TargetRuntimeOrchestrator

ROOT = Path(__file__).resolve().parents[2]
store = StateStore.from_environment(ROOT)
runner = DeployerRunner(ROOT)
aws_secrets = BotoAWSSecretAdapter()
backup_s3 = recovery_module.BotoS3Adapter()
rds_postgres = resources_postgres_module.BotoRDSAdapter()
elasticache_valkey = resources_valkey_module.BotoElastiCacheAdapter()
mcp = FastMCP(
    "Gimme",
    instructions=(
        "Git-backed deployment control plane for explicitly registered Ubuntu targets, "
        "applications, and deployments. Inspect a plan before every remote mutation and "
        "pass its exact plan_id to apply. Secrets are bounded references, never tool arguments."
    ),
)
READ = ToolAnnotations(title="Read Gimme control-plane state", readOnlyHint=True,
                       destructiveHint=False, idempotentHint=True, openWorldHint=True)
WRITE = ToolAnnotations(title="Apply an idempotent Gimme change", readOnlyHint=False,
                        destructiveHint=True, idempotentHint=True, openWorldHint=True)
CHANGE = ToolAnnotations(title="Apply a non-idempotent Gimme change", readOnlyHint=False,
                         destructiveHint=True, idempotentHint=False, openWorldHint=True)
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,63}$", min_length=1, max_length=64)]
PlanId = Annotated[str, Field(pattern=r"^plan_[a-f0-9]{20}$")]
SnapshotName = Annotated[
    str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9.-]{0,254}$", min_length=1, max_length=255)
]
CorrelationId = Annotated[str, Field(pattern=r"^corr_[a-f0-9]{32}$")]
OperationName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", max_length=64)]
RequestId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", max_length=64)]
RecoveryPointId = Annotated[str, Field(pattern=r"^rp_[a-f0-9]{20}$")]
RestoreComponent = Literal["postgres", "valkey"]
RestoreComponents = Annotated[list[RestoreComponent], Field(min_length=1, max_length=2)]
P = ParamSpec("P")
R = TypeVar("R", bound=dict[str, object])
_suppress_plan_journal: ContextVar[bool] = ContextVar("suppress_plan_journal", default=False)
# Set only while a restored Resource is being verified against its Deployments, so the
# still-'restoring' Resource yields its contract values to exactly that verification.
_restoring_ok: ContextVar[bool] = ContextVar("restoring_ok", default=False)
_held_deployment_locks: ContextVar[frozenset[str]] = ContextVar(
    "held_deployment_locks", default=frozenset()
)


def _recovery_orchestrator() -> RecoveryOrchestrator:
    """Compose Recovery orchestration from the server's current adapters."""
    return RecoveryOrchestrator(
        backup_s3=backup_s3,
        elasticache_valkey=elasticache_valkey,
        context=_context,
        managed_database_issues=_managed_database_issues,
        backup_destination_credentials=_backup_destination_credentials,
        deployment_resource_lock=_deployment_resource_lock,
        assert_plan=_assert_plan,
        run_deployment=_run_deployment,
        valkey_runtime=_valkey_runtime,
        bounded_marker_values=_bounded_marker_values,
        journal=_journal,
    )


def _managed_resource_orchestrator() -> ManagedResourceOrchestrator:
    """Compose managed Resource orchestration from the current adapters."""
    return ManagedResourceOrchestrator(
        store=store,
        rds_postgres=rds_postgres,
        elasticache_valkey=elasticache_valkey,
        deployment_resource_locks=_deployment_resource_locks,
        assert_plan=_assert_plan,
        runner=runner,
        context=_context,
    )


def _control_plane_registration_orchestrator(
) -> ControlPlaneRegistrationOrchestrator:
    """Compose control-plane registration from the current adapters."""
    return ControlPlaneRegistrationOrchestrator(
        store=store,
        aws_secrets=aws_secrets,
        backup_s3=backup_s3,
        assert_plan=_assert_plan,
        replace=_replace,
        delete=_delete,
        backup_destination_credentials=_backup_destination_credentials,
    )


def _target_runtime_orchestrator() -> TargetRuntimeOrchestrator:
    """Compose Target runtime orchestration from the current adapters."""
    return TargetRuntimeOrchestrator(
        store=store,
        runner=runner,
        context=_context,
        run_deployment=_run_deployment,
        deployment_resource_lock=_deployment_resource_lock,
        assert_plan=_assert_plan,
        result=_result,
    )


def _deployment_resource_orchestrator() -> DeploymentResourceOrchestrator:
    """Compose Deployment Resource orchestration from the current adapters."""
    return DeploymentResourceOrchestrator(
        store=store,
        runner=runner,
        aws_secrets=aws_secrets,
        context=_context,
        run_deployment=_run_deployment,
        deployment_resource_lock=_deployment_resource_lock,
        assert_plan=_assert_plan,
        recovery_schedule_runtime_issues=_recovery_schedule_runtime_issues,
        recovery_schedule_authority=_recovery_schedule_authority,
        backup_destination_credentials=_backup_destination_credentials,
        valkey_capture_credential=_valkey_capture_credential,
        restoring_ok=_restoring_ok.get,
    )


def _deployment_release_orchestrator() -> DeploymentReleaseOrchestrator:
    """Compose Deployment release orchestration from the current adapters."""
    return DeploymentReleaseOrchestrator(
        store=store,
        context=_context,
        run_deployment=_run_deployment,
        secret_plan=_secret_plan,
        dns_issues=_dns_issues,
        managed_database_issues=_managed_database_issues,
        valkey_runtime=_valkey_runtime,
        deployment_resource_lock=_deployment_resource_lock,
        deployment_resource_locks=_deployment_resource_locks,
        assert_plan=_assert_plan,
        replace=_replace,
        result=_result,
    )


def _journal() -> OperationJournal:
    return OperationJournal(store.root)


@contextmanager
def _deployment_resource_lock(name: str):
    held = _held_deployment_locks.get()
    if name in held:
        yield
        return
    directory = store.root / "deployment-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / f"{name}.lock"
    with path.open("a+") as lock:
        os.chmod(path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        token = _held_deployment_locks.set(held | {name})
        try:
            yield
        finally:
            _held_deployment_locks.reset(token)


@contextmanager
def _deployment_resource_locks(*names: str):
    with ExitStack() as stack:
        for name in sorted(set(names)):
            stack.enter_context(_deployment_resource_lock(name))
        yield


def _subjects(function: Callable[..., object], fields: tuple[str, ...],
              args: tuple[object, ...], kwargs: dict[str, object]) -> dict[str, str]:
    bound = signature(function).bind_partial(*args, **kwargs)
    return {field: str(bound.arguments[field]) for field in fields if field in bound.arguments}


def _journal_plan(operation: str, *subject_fields: str
                  ) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            if _suppress_plan_journal.get():
                return function(*args, **kwargs)
            correlation_id = OperationJournal.correlation_id()
            subjects = _subjects(
                function, subject_fields, cast(tuple[object, ...], args),
                cast(dict[str, object], kwargs),
            )
            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                status, error_code = _classified_failure(exc)
                _journal().append(
                    correlation_id=correlation_id, operation=operation, phase="plan",
                    status=status, subjects=subjects, error_code=error_code,
                )
                raise
            plan_id = result.get("plan_id")
            _journal().append(
                correlation_id=correlation_id, operation=operation, phase="plan",
                status="succeeded", subjects=subjects,
                plan_id=str(plan_id) if plan_id is not None else None,
            )
            return cast(R, {**result, "correlation_id": correlation_id})

        return wrapped
    return decorate


def _journal_apply(operation: str, *subject_fields: str
                   ) -> Callable[[Callable[P, R]], Callable[P, R]]:
    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = signature(function).bind_partial(*args, **kwargs)
            plan_id_value = bound.arguments.get("plan_id")
            plan_id = str(plan_id_value) if plan_id_value is not None else None
            journal = _journal()
            correlation_id = OperationJournal.correlation_id()
            subjects = _subjects(
                function, subject_fields, cast(tuple[object, ...], args),
                cast(dict[str, object], kwargs),
            )
            plan_correlation_id = journal.plan_correlation(plan_id, operation)
            journal.append(
                correlation_id=correlation_id, operation=operation, phase="apply",
                status="started", subjects=subjects, plan_id=plan_id,
                plan_correlation_id=plan_correlation_id,
            )
            token = _suppress_plan_journal.set(True)
            try:
                result = function(*args, **kwargs)
            except Exception as exc:
                status, error_code = _classified_failure(exc)
                journal.append(
                    correlation_id=correlation_id, operation=operation, phase="outcome",
                    status=status, subjects=subjects, plan_id=plan_id,
                    plan_correlation_id=plan_correlation_id, error_code=error_code,
                )
                raise
            finally:
                _suppress_plan_journal.reset(token)
            journal.append(
                correlation_id=correlation_id, operation=operation, phase="outcome",
                status="succeeded", subjects=subjects, plan_id=plan_id,
                plan_correlation_id=plan_correlation_id,
            )
            return cast(R, {**result, "correlation_id": correlation_id})

        return wrapped
    return decorate


def _classified_failure(error: Exception) -> tuple[Literal["failed", "rejected", "stale"], str]:
    if isinstance(error, ValueError) and "invalid or stale" in str(error):
        return "stale", "stale_plan"
    if isinstance(error, (KeyError, ValueError)):
        return "rejected", "policy_rejected"
    return "failed", "operation_failed"


def _result(result: CommandResult) -> dict[str, object]:
    return result.as_dict()


def _deployment_diagnostics(result: CommandResult) -> dict[str, object]:
    checks: list[dict[str, str]] = []
    for line in result.output.splitlines():
        marker = line.find("GIMME_DIAGNOSTIC|")
        if marker < 0:
            continue
        parts = line[marker:].split("|", 3)
        if len(parts) != 4:
            continue
        _, check, status, detail = parts
        if (
            re.fullmatch(
                r"(?:release|artisan|database|writable|php-fpm|laravel-log|"
                r"health\.[a-z][a-z0-9-]{0,31})",
                check,
            ) is None
            or status not in {"ready", "failed", "missing"}
            or re.fullmatch(
                r"(?:none|invalid|invalid-metadata|[0-9a-f]{40,64}|status=(?:[1-5][0-9]{2}|exception)|bytes=\d{1,15},age_seconds=\d{1,15},errors=\d{1,10})",
                detail,
            ) is None
        ):
            continue
        checks.append({"check": check, "status": status, "detail": detail})
    return {
        "healthy": bool(checks) and all(check["status"] == "ready" for check in checks),
        "checks": checks,
    }


def _bounded_marker_values(
    output: str, prefix: str, allowed: set[str],
) -> set[str]:
    values: set[str] = set()
    for line in output.splitlines():
        marker = line.find(prefix)
        if marker < 0:
            continue
        match = re.match(r"[a-z][a-z0-9_-]{0,63}", line[marker + len(prefix):])
        if match is not None and match.group(0) in allowed:
            values.add(match.group(0))
    return values


def _replace(state: ControlState, collection: str, name: str, value: object) -> ControlState:
    document = state.model_dump(mode="json")
    document[collection][name] = value.model_dump(mode="json")  # type: ignore[attr-defined]
    return ControlState.model_validate(document)


def _delete(state: ControlState, collection: str, name: str) -> ControlState:
    document = state.model_dump(mode="json")
    del document[collection][name]
    return ControlState.model_validate(document)


def _assert_plan(expected: dict[str, Any], plan_id: str) -> None:
    if plan_id != expected["plan_id"]:
        raise ValueError("plan_id is invalid or stale; request a fresh plan")


def _context(name: str) -> tuple[ControlState, DeploymentConfig, TargetConfig, ApplicationConfig]:
    state = store.load()
    try:
        deployment = state.deployments[name]
        target = state.targets[deployment.target]
        application = state.applications[deployment.application]
    except KeyError as exc:
        raise KeyError(f"deployment '{name}' is not registered") from exc
    return state, deployment, target, application


def _run_deployment(
    task: str, name: str, *, revision: str | None = None,
    arguments: tuple[str, ...] = (), secret_file: Path | None = None,
    secret_manifest: list[dict[str, str]] | None = None,
    artisan_command: str | None = None, artisan_arguments: list[str] | None = None,
    backup_local_path: Path | None = None,
    recovery_action: str | None = None,
    recovery_request_id: str | None = None,
    recovery_quiesce_wait: int | None = None,
    restore_source_bytes: int | None = None,
    postgres_restore_action: str | None = None,
    postgres_restore_request_id: str | None = None,
    postgres_restore_sha256: str | None = None,
    postgres_restore_bytes: int | None = None,
    valkey_restore_request_id: str | None = None,
    valkey_restore_sha256: str | None = None,
    valkey_restore_bytes: int | None = None,
    valkey_restore_records: int | None = None,
    recovery_schedule_authority: dict[str, object] | None = None,
    recovery_schedule_valkey_file: Path | None = None,
    recovery_on_demand_request_id: str | None = None,
    timeout: int = 900,
) -> CommandResult:
    state, deployment, target, application = _context(name)
    valkey = deployment.resources.valkey
    valkey_resource = None if valkey is None else state.resources[valkey.resource]
    contract_values, _credentials, probe, _issues = _valkey_runtime(name, state, deployment)
    bound_resources = {
        kind: resource.model_dump(mode="json")
        for kind, resource_name in (
            ("database", deployment.resources.database),
            ("cache", None if valkey is None else valkey.resource),
        )
        if resource_name is not None
        and isinstance(resource := state.resources[resource_name], ResourceConfig)
    }
    return runner.run(
        task, legacy_server(target), stack=target.stack,
        app_name=deployment.application, app=legacy_app(application, deployment),
        revision=revision, arguments=arguments,
        deployment_name=name,
        instance_name=deployment.placement.instance,
        deploy_path=f"{target.apps_root}/{deployment.placement.relative_path}",
        site_host=deployment.placement.site_host,
        database_identifier=deployment.placement.database_identifier,
        cache_prefix=(
            f"{{gimme:{name}}}:"
            if task in {"gimme:backup:capture-valkey", "gimme:recovery:valkey"}
            and isinstance(valkey_resource, AWSElastiCacheValkeyResource)
            else deployment.placement.cache_prefix
        ),
        source_kind=deployment.source.kind, sites=target_sites(state, deployment.target),
        network_mode=target.network.mode,
        runtimes={key: value.model_dump(mode="json") for key, value in deployment.runtimes.items()},
        resources=bound_resources, mise_version=target.runtimes.mise_version,
        php_extensions=application.php_extensions,
        variables={**deployment.variables, **contract_values}, valkey_probe=probe,
        secret_file=secret_file,
        secret_manifest=secret_manifest,
        artisan_command=artisan_command, artisan_arguments=artisan_arguments,
        artisan_allowed_commands=(
            application.artisan.allowed_commands
            if artisan_command is not None and application.artisan is not None
            else None
        ),
        backup_local_path=backup_local_path,
        recovery_action=recovery_action,
        recovery_request_id=recovery_request_id,
        recovery_quiesce_wait=recovery_quiesce_wait,
        restore_source_bytes=restore_source_bytes,
        postgres_restore_action=postgres_restore_action,
        postgres_restore_request_id=postgres_restore_request_id,
        postgres_restore_sha256=postgres_restore_sha256,
        postgres_restore_bytes=postgres_restore_bytes,
        valkey_restore_request_id=valkey_restore_request_id,
        valkey_restore_sha256=valkey_restore_sha256,
        valkey_restore_bytes=valkey_restore_bytes,
        valkey_restore_records=valkey_restore_records,
        recovery_schedule_authority=recovery_schedule_authority,
        recovery_schedule_valkey_file=recovery_schedule_valkey_file,
        recovery_on_demand_request_id=recovery_on_demand_request_id,
        timeout=timeout,
    )


@contextmanager
def _recovery_maintenance_window(
    name: str, request_id: str, wait_seconds: int, *, enabled: bool,
) -> Iterator[Callable[[], None]]:
    """Keep request-owned maintenance active until verified uploads are ready to publish."""
    with _recovery_orchestrator()._recovery_maintenance_window(
        name, request_id, wait_seconds, enabled=enabled
    ) as restore:
        yield restore


def _capture_postgres_dump(
    name: str, local_path: Path, resource_version: str
) -> ComponentDump:
    return _recovery_orchestrator()._capture_postgres_dump(name, local_path, resource_version)


def _valkey_resource_version(
    resource: ResourceConfig | AWSElastiCacheValkeyResource,
) -> str:
    return _recovery_orchestrator()._valkey_resource_version(resource)


def _valkey_capture_credential(
    state: ControlState, resource_name: str,
    resource: ResourceConfig | AWSElastiCacheValkeyResource,
) -> dict[str, str]:
    return _recovery_orchestrator()._valkey_capture_credential(state, resource_name, resource)


def _capture_valkey_dump(
    name: str, local_path: Path, resource_version: str,
    secret_file: Path | None,
) -> ComponentDump:
    return _recovery_orchestrator()._capture_valkey_dump(
        name, local_path, resource_version, secret_file
    )


def _capture_restore_safety(
    name: str, request_id: str, safety_id: str,
    safety_components: list[str], state: ControlState,
    deployment: DeploymentConfig, destination_name: str,
    destination: S3BackupDestination,
    credentials: tuple[str, str] | tuple[str, str, str] | None,
) -> dict[str, object]:
    """Capture and verify exactly the protected destination components."""
    return _recovery_orchestrator()._capture_restore_safety(
        name, request_id, safety_id, safety_components, state, deployment,
        destination_name, destination, credentials,
    )


def _restore_valkey_component(
    name: str, request_id: str, component: dict[str, object],
    local_source: Path, state: ControlState, deployment: DeploymentConfig,
) -> None:
    return _recovery_orchestrator()._restore_valkey_component(
        name, request_id, component, local_source, state, deployment
    )


def _dns_issues(deployment: DeploymentConfig, target: TargetConfig) -> list[str]:
    return _deployment_resource_orchestrator().dns_issues(deployment, target)


def _valkey_runtime(
    name: str, state: ControlState, deployment: DeploymentConfig
) -> tuple[dict[str, str], dict[str, SecretReference], dict[str, object] | None, list[str]]:
    return _deployment_resource_orchestrator().valkey_runtime(name, state, deployment)


def _secret_plan(name: str, state: ControlState, deployment: DeploymentConfig
                 ) -> tuple[list[dict[str, str]], list[str]]:
    return _deployment_resource_orchestrator().secret_plan(name, state, deployment)


def _resolved_stack_plan(name: str) -> dict[str, Any]:
    return _target_runtime_orchestrator().resolved_stack_plan(name)


def _managed_database_issues(state: ControlState, deployment: DeploymentConfig) -> list[str]:
    return _deployment_resource_orchestrator().managed_database_issues(state, deployment)


def _recovery_schedule_runtime_issues(
    deployment: DeploymentConfig, target: TargetConfig
) -> list[str]:
    return _recovery_orchestrator()._recovery_schedule_runtime_issues(deployment, target)


def _recovery_valkey_execution(
    name: str, state: ControlState, deployment: DeploymentConfig
) -> dict[str, object] | None:
    return _recovery_orchestrator()._recovery_valkey_execution(name, state, deployment)


def _recovery_schedule_authority(
    name: str, state: ControlState, deployment: DeploymentConfig, *, cleanup: bool = False,
) -> dict[str, object] | None:
    return _recovery_orchestrator()._recovery_schedule_authority(
        name, state, deployment, cleanup=cleanup
    )


def _resource_plan(name: str) -> dict[str, Any]:
    return _deployment_resource_orchestrator().resource_plan(name)


def _migration_targets() -> dict[str, TargetConfig]:
    if store.exists():
        document = store.raw_state()
        targets = document.get("targets", {})
        if not isinstance(targets, dict):
            raise ValueError("state targets are invalid")
        result: dict[str, TargetConfig] = {}
        for name, value in targets.items():
            if not isinstance(name, str) or not isinstance(value, dict):
                raise ValueError("state target is invalid")
            migrated = dict(value)
            migrated.pop("toolchains", None)
            migrated.setdefault("runtimes", {"mise_version": None})
            result[name] = TargetConfig.model_validate(migrated)
        return result
    from gimme.config import ConfigStore

    legacy = ConfigStore(store.legacy_root)
    server = legacy.server()
    return {
        server.host_alias: TargetConfig(
            host_alias=server.host_alias,
            bootstrap_hostname=server.bootstrap_hostname,
            hostname=server.hostname,
            system_hostname=server.mdns_name,
            remote_user=server.remote_user,
            apps_root=server.apps_root,
            keep_releases=server.keep_releases,
            network={"mode": "local_mdns", "mdns_name": server.mdns_name},
            stack=legacy.stack(),
        )
    }


def _migration_observations() -> dict[str, dict[str, str]]:
    observations: dict[str, dict[str, str]] = {}
    for name, target in _migration_targets().items():
        result = runner.run(
            "gimme:inspect:runtimes", legacy_server(target), stack=target.stack, timeout=60
        )
        versions: dict[str, str] = {}
        for raw in result.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_RUNTIME|"):
                _, runtime, version = line.split("|", 2)
                versions[runtime] = version
        observations[name] = versions
    return observations


def _migration_state() -> ControlState:
    if store.exists() and store.raw_state().get("schema_version") == 3:
        return store.state_migration({})
    return store.state_migration(_migration_observations())


@mcp.resource("gimme://state")
def desired_state() -> dict[str, object]:
    """Complete desired state without decrypted secret values."""
    return store.load().model_dump(mode="json")


@mcp.resource("gimme://targets/{name}")
def target_resource(name: str) -> dict[str, object]:
    return store.target(name).model_dump(mode="json")


@mcp.resource("gimme://applications/{name}")
def application_resource(name: str) -> dict[str, object]:
    return store.application(name).model_dump(mode="json")


@mcp.resource("gimme://provider-accounts/{name}")
def provider_account_resource(name: str) -> dict[str, object]:
    return store.load().provider_accounts[name].model_dump(mode="json")


@mcp.resource("gimme://secret-stores/{name}")
def secret_store_resource(name: str) -> dict[str, object]:
    state = store.load()
    value = state.secret_stores[name].model_dump(mode="json")
    if value["provider"] == "aws_secrets_manager":
        value["ownership_tag"] = f"gimme:secret-store={name}"
    return value


@mcp.resource("gimme://backup-destinations/{name}")
def backup_destination_resource(name: str) -> dict[str, object]:
    return store.load().backup_destinations[name].model_dump(mode="json")


@mcp.resource("gimme://resources/{name}")
def managed_resource(name: str) -> dict[str, object]:
    return store.load().resources[name].model_dump(mode="json")


@mcp.resource("gimme://aws-networks/{name}/valkey-options")
def valkey_options(name: str) -> dict[str, object]:
    """Exact Valkey versions and cache node types the registered account offers in one AWS
    Network's region. Read-only; nothing is stored."""
    state = store.load()
    network = state.aws_networks[name]
    options = elasticache_valkey.live_options(
        state.provider_accounts[network.provider_account], network
    )
    return {
        "aws_network": name, "engine_versions": list(options.engine_versions),
        "node_types": list(options.node_types),
    }


@mcp.resource("gimme://deployments/{name}")
def deployment_resource(name: str) -> dict[str, object]:
    return store.deployment(name).model_dump(mode="json")


def _valid_recovery_attempt_status(status: object, name: str) -> bool:
    return _recovery_orchestrator()._valid_recovery_attempt_status(status, name)


def _recovery_schedule_status(
    name: str, observed_at: datetime | None = None,
) -> dict[str, object]:
    return _recovery_orchestrator()._recovery_schedule_status(name, observed_at)


@mcp.tool(annotations=READ)
def get_recovery_schedule_status(name: Name) -> dict[str, object]:
    """Read bounded, secret-safe observed Recovery Schedule status."""
    return _recovery_schedule_status(name)


@mcp.resource("gimme://deployments/{name}/recovery-schedule")
def recovery_schedule_resource(name: str) -> dict[str, object]:
    """Read bounded Recovery Schedule status without raw target output."""
    return _recovery_schedule_status(name)


@mcp.resource("gimme://operations")
def recent_operations_resource() -> dict[str, object]:
    """The 50 most recent secret-safe operation journal events."""
    return {"events": [event.model_dump(mode="json") for event in _journal().list()]}


@mcp.resource("gimme://operations/{correlation_id}")
def operation_trace_resource(correlation_id: str) -> dict[str, object]:
    """Every retained event for one operation correlation ID."""
    events = _journal().list(limit=200, correlation_id=correlation_id)
    return {"correlation_id": correlation_id,
            "events": [event.model_dump(mode="json") for event in reversed(events)]}


@mcp.tool(annotations=READ)
@_journal_plan("state_migration")
def plan_state_migration() -> dict[str, object]:
    """Inspect exact installed versions and plan migration to schema-v5 state."""
    if store.exists() and store.raw_state().get("schema_version") == 5:
        raise ValueError("schema-v5 state already exists")
    return migration_plan(_migration_state(), str(store.root))


@mcp.tool(annotations=WRITE)
@_journal_apply("state_migration")
def apply_state_migration(plan_id: PlanId) -> dict[str, object]:
    """Atomically write schema-v5 state after re-observing exact installed versions."""
    state = _migration_state()
    expected = migration_plan(state, str(store.root))
    _assert_plan(expected, plan_id)
    store.save(state)
    return {"changed": True, "state_path": str(store.state_path), "schema_version": 5}


@mcp.tool(annotations=READ)
def list_targets() -> dict[str, object]:
    """List every registered target and its desired provisioning policy."""
    return {"targets": store.load().model_dump(mode="json")["targets"]}


@mcp.tool(annotations=READ)
def list_applications() -> dict[str, object]:
    """List reusable registered application source and build definitions."""
    return {"applications": store.load().model_dump(mode="json")["applications"]}


@mcp.tool(annotations=READ)
def list_provider_accounts() -> dict[str, object]:
    """List bounded external-provider identities without credentials."""
    return {"provider_accounts": store.load().model_dump(mode="json")["provider_accounts"]}


@mcp.tool(annotations=READ)
def list_secret_stores() -> dict[str, object]:
    """List bounded Secret Store policy without secret names or values."""
    state = store.load()
    values = state.model_dump(mode="json")["secret_stores"]
    for name, value in values.items():
        if value["provider"] == "aws_secrets_manager":
            value["ownership_tag"] = f"gimme:secret-store={name}"
    return {"secret_stores": values}


@mcp.tool(annotations=READ)
def list_resources(target: Name | None = None) -> dict[str, object]:
    """List named, version-pinned infrastructure resources."""
    values = store.load().model_dump(mode="json")["resources"]
    if target is not None:
        values = {name: item for name, item in values.items() if item.get("target") == target}
    return {"resources": values}


@mcp.tool(annotations=READ)
def list_deployments(target: Name | None = None) -> dict[str, object]:
    """List deployments, optionally restricted to one registered target."""
    values = store.load().model_dump(mode="json")["deployments"]
    if target is not None:
        values = {name: item for name, item in values.items() if item.get("target") == target}
    return {"deployments": values}


@mcp.tool(annotations=READ)
def list_operations(limit: int = 50, operation: OperationName | None = None,
                    subject: Name | None = None,
                    correlation_id: CorrelationId | None = None) -> dict[str, object]:
    """List secret-safe journal events, newest first, with optional exact filters."""
    events = _journal().list(limit=limit, operation=operation, subject=subject,
                             correlation_id=correlation_id)
    return {"events": [event.model_dump(mode="json") for event in events]}


def _account_registration_plan(name: str, definition: AWSProviderAccount,
                               *, update: bool) -> dict[str, object]:
    return _control_plane_registration_orchestrator().account_registration_plan(
        name, definition, update=update
    )


@mcp.tool(annotations=READ)
@_journal_plan("register_provider_account", "name")
def plan_register_provider_account(name: Name, definition: AWSProviderAccount) -> dict[str, object]:
    """Verify both exact AWS roles and plan a Provider Account registration."""
    return _control_plane_registration_orchestrator().plan_register_provider_account(
        name, definition
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("register_provider_account", "name")
def register_provider_account(name: Name, definition: AWSProviderAccount,
                              plan_id: PlanId) -> dict[str, object]:
    """Register one verified AWS Provider Account without storing credentials."""
    return _control_plane_registration_orchestrator().register_provider_account(
        name, definition, plan_id
    )


@mcp.tool(annotations=READ)
@_journal_plan("update_provider_account", "name")
def plan_update_provider_account(name: Name, definition: AWSProviderAccount) -> dict[str, object]:
    """Reverify and plan an exact Provider Account policy update."""
    return _control_plane_registration_orchestrator().plan_update_provider_account(
        name, definition
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("update_provider_account", "name")
def update_provider_account(name: Name, definition: AWSProviderAccount,
                            plan_id: PlanId) -> dict[str, object]:
    """Apply one reviewed Provider Account policy update."""
    return _control_plane_registration_orchestrator().update_provider_account(
        name, definition, plan_id
    )


@mcp.tool(annotations=READ)
@_journal_plan("remove_provider_account", "name")
def plan_remove_provider_account(name: Name) -> dict[str, object]:
    """Plan local removal when no Secret Store or Resource references the account."""
    return _control_plane_registration_orchestrator().plan_remove_provider_account(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("remove_provider_account", "name")
def remove_provider_account(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Apply a reviewed local-only Provider Account removal."""
    return _control_plane_registration_orchestrator().remove_provider_account(
        name, plan_id
    )


def _secret_store_registration_plan(name: str, definition: SecretStore,
                                    *, update: bool) -> dict[str, object]:
    return _control_plane_registration_orchestrator().secret_store_registration_plan(
        name, definition, update=update
    )


@mcp.tool(annotations=READ)
@_journal_plan("register_secret_store", "name")
def plan_register_secret_store(name: Name, definition: SecretStore) -> dict[str, object]:
    """Verify bounded store policy and plan an AWS Secret Store registration."""
    return _control_plane_registration_orchestrator().plan_register_secret_store(
        name, definition
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("register_secret_store", "name")
def register_secret_store(name: Name, definition: SecretStore,
                          plan_id: PlanId) -> dict[str, object]:
    """Register one reviewed Secret Store without listing or mutating AWS secrets."""
    return _control_plane_registration_orchestrator().register_secret_store(
        name, definition, plan_id
    )


@mcp.tool(annotations=READ)
@_journal_plan("update_secret_store", "name")
def plan_update_secret_store(name: Name, definition: SecretStore) -> dict[str, object]:
    """Plan an exact bounded Secret Store policy update."""
    return _control_plane_registration_orchestrator().plan_update_secret_store(
        name, definition
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("update_secret_store", "name")
def update_secret_store(name: Name, definition: SecretStore,
                        plan_id: PlanId) -> dict[str, object]:
    """Apply one reviewed Secret Store policy update."""
    return _control_plane_registration_orchestrator().update_secret_store(
        name, definition, plan_id
    )


@mcp.tool(annotations=READ)
@_journal_plan("remove_secret_store", "name")
def plan_remove_secret_store(name: Name) -> dict[str, object]:
    """Plan local Secret Store removal when no Deployment references it."""
    return _control_plane_registration_orchestrator().plan_remove_secret_store(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("remove_secret_store", "name")
def remove_secret_store(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Apply a reviewed local-only Secret Store removal."""
    return _control_plane_registration_orchestrator().remove_secret_store(name, plan_id)


def _backup_destination_credentials(
    state: ControlState, definition: S3BackupDestination
) -> tuple[
    list[dict[str, str]] | None,
    tuple[str, str] | tuple[str, str, str] | None,
]:
    planned = recovery_module.plan_destination_credentials(state, store.secrets_path, definition)
    credentials = recovery_module.resolve_destination_credentials(
        state, store.secrets_path, definition, planned
    )
    return planned, credentials


def _backup_destination_registration_plan(
    name: str, definition: S3BackupDestination, *, update: bool
) -> dict[str, object]:
    return _control_plane_registration_orchestrator().backup_destination_registration_plan(
        name, definition, update=update
    )


def _backup_destination_apply(
    name: str, definition: S3BackupDestination, plan_id: str, *, update: bool
) -> dict[str, object]:
    return _control_plane_registration_orchestrator().backup_destination_apply(
        name, definition, plan_id, update=update
    )


@mcp.tool(annotations=READ)
@_journal_plan("register_backup_destination", "name")
def plan_register_backup_destination(
    name: Name, definition: S3BackupDestination
) -> dict[str, object]:
    """Diff a proposed Backup Destination registration; preflight runs at apply."""
    return _control_plane_registration_orchestrator().plan_register_backup_destination(
        name, definition
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("register_backup_destination", "name")
def register_backup_destination(
    name: Name, definition: S3BackupDestination, plan_id: PlanId
) -> dict[str, object]:
    """Preflight-verify and register one Backup Destination without storing credentials."""
    return _control_plane_registration_orchestrator().register_backup_destination(
        name, definition, plan_id
    )


@mcp.tool(annotations=READ)
@_journal_plan("update_backup_destination", "name")
def plan_update_backup_destination(
    name: Name, definition: S3BackupDestination
) -> dict[str, object]:
    """Diff a proposed Backup Destination policy update; preflight runs at apply."""
    return _control_plane_registration_orchestrator().plan_update_backup_destination(
        name, definition
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("update_backup_destination", "name")
def update_backup_destination(
    name: Name, definition: S3BackupDestination, plan_id: PlanId
) -> dict[str, object]:
    """Preflight-verify and apply one reviewed Backup Destination policy update."""
    return _control_plane_registration_orchestrator().update_backup_destination(
        name, definition, plan_id
    )


@mcp.tool(annotations=READ)
@_journal_plan("remove_backup_destination", "name")
def plan_remove_backup_destination(name: Name) -> dict[str, object]:
    """Plan local Backup Destination removal when no Deployment references it."""
    return _control_plane_registration_orchestrator().plan_remove_backup_destination(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("remove_backup_destination", "name")
def remove_backup_destination(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Apply a reviewed local-only Backup Destination removal."""
    return _control_plane_registration_orchestrator().remove_backup_destination(
        name, plan_id
    )


@mcp.tool(annotations=READ)
def list_backup_destinations() -> dict[str, object]:
    """List registered S3-compatible Backup Destinations without credentials."""
    return {"backup_destinations": store.load().model_dump(mode="json")["backup_destinations"]}


def _recovery_context(
    name: str,
) -> tuple[ControlState, DeploymentConfig, str, S3BackupDestination]:
    return _recovery_orchestrator()._recovery_context(name)


@mcp.tool(annotations=READ)
@_journal_plan("create_recovery_point", "name")
def plan_create_recovery_point(name: Name, request_id: RequestId) -> dict[str, object]:
    """Plan one on-demand PostgreSQL Recovery Point for a recovery-bound deployment."""
    return _recovery_orchestrator().plan_create_recovery_point(name, request_id)


@mcp.tool(annotations=WRITE)
@_journal_apply("create_recovery_point", "name")
def create_recovery_point(name: Name, request_id: RequestId, plan_id: PlanId) -> dict[str, object]:
    """Apply a reviewed on-demand Recovery Point: dump, upload, verify, and publish."""
    return _recovery_orchestrator().create_recovery_point(name, request_id, plan_id)


@mcp.tool(annotations=READ)
def list_recovery_points(name: Name) -> dict[str, object]:
    """List one deployment's Recovery Points from destination-authoritative inventory."""
    return _recovery_orchestrator().list_recovery_points(name)


def _restore_records(name: str) -> list[dict[str, object]]:
    return _recovery_orchestrator()._restore_records(name)


@mcp.tool(annotations=READ)
def list_restores(name: Name) -> dict[str, object]:
    """List destination-authoritative, secret-safe Restore records newest first."""
    return _recovery_orchestrator().list_restores(name)


@mcp.resource("gimme://deployments/{name}/restores/{request_id}")
def restore_record_resource(name: str, request_id: str) -> dict[str, object]:
    """Read the latest public Restore state for one request identity."""
    return _recovery_orchestrator().restore_record_resource(name, request_id)


def _normalize_restore_components(
    manifest_components: list[dict[str, object]],
    requested: list[str] | None,
) -> list[str]:
    return _recovery_orchestrator()._normalize_restore_components(manifest_components, requested)


def _deployment_restore_plan(
    name: str, recovery_point_id: str, request_id: str,
    components: list[str] | None = None,
) -> dict[str, object]:
    return _recovery_orchestrator()._deployment_restore_plan(
        name, recovery_point_id, request_id, components
    )


@mcp.tool(annotations=READ)
@_journal_plan("restore_deployment", "name")
def plan_restore_deployment(
    name: Name, recovery_point_id: RecoveryPointId, request_id: RequestId,
    components: RestoreComponents | None = None,
) -> dict[str, object]:
    """Plan full Restore by default or an explicit bounded component subset."""
    return _recovery_orchestrator().plan_restore_deployment(
        name, recovery_point_id, request_id, components
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("restore_deployment", "name")
def apply_restore_deployment(
    name: Name,
    recovery_point_id: RecoveryPointId,
    request_id: RequestId,
    plan_id: PlanId,
    confirmation: str,
    components: RestoreComponents | None = None,
) -> dict[str, object]:
    """Prepare and activate one reviewed full or partial Deployment Restore.

    The deployment remains in request-owned maintenance for a separate verified
    completion step.
    """
    return _recovery_orchestrator().apply_restore_deployment(
        name, recovery_point_id, request_id, plan_id, confirmation, components
    )


def _restore_verification_plan(name: str, request_id: str) -> dict[str, object]:
    return _recovery_orchestrator()._restore_verification_plan(name, request_id)


@mcp.tool(annotations=READ)
@_journal_plan("verify_restore", "name")
def plan_verify_restore(name: Name, request_id: RequestId) -> dict[str, object]:
    """Plan private application verification and return from Restore maintenance."""
    return _recovery_orchestrator().plan_verify_restore(name, request_id)


@mcp.tool(annotations=WRITE)
@_journal_apply("verify_restore", "name")
def apply_verify_restore(
    name: Name, request_id: RequestId, plan_id: PlanId,
) -> dict[str, object]:
    """Verify restored data privately, clean up, and restore normal routing."""
    return _recovery_orchestrator().apply_verify_restore(name, request_id, plan_id)


def _recovery_point_deletion_plan(
    name: str, point_id: str, *, allow_partial: bool = False
) -> dict[str, object]:
    return _recovery_orchestrator()._recovery_point_deletion_plan(
        name, point_id, allow_partial=allow_partial
    )


@mcp.tool(annotations=READ)
@_journal_plan("delete_recovery_point", "name")
def plan_delete_recovery_point(
    name: Name, recovery_point_id: RecoveryPointId
) -> dict[str, object]:
    """Plan deletion of one manifest-owned Recovery Point without exposing S3 identities."""
    return _recovery_orchestrator().plan_delete_recovery_point(name, recovery_point_id)


def _matching_delete_retry(name: str, plan_id: str) -> bool:
    return _recovery_orchestrator()._matching_delete_retry(name, plan_id)


@mcp.tool(annotations=WRITE)
@_journal_apply("delete_recovery_point", "name")
def delete_recovery_point(
    name: Name,
    recovery_point_id: RecoveryPointId,
    plan_id: PlanId,
    confirmation: str,
    last_recovery_point_confirmation: str | None = None,
) -> dict[str, object]:
    """Delete only exact manifest-owned versions, with the immutable manifest last."""
    return _recovery_orchestrator().delete_recovery_point(
        name, recovery_point_id, plan_id, confirmation,
        last_recovery_point_confirmation,
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("register_target", "name")
def register_target(name: Name, definition: TargetConfig) -> dict[str, object]:
    """Register a new target locally without contacting it."""
    state = store.load()
    if name in state.targets:
        raise ValueError("target already exists; use plan_update_target")
    store.save(_replace(state, "targets", name, definition))
    return {"changed": True, "target": name}


@mcp.tool(annotations=READ)
@_journal_plan("update_target", "name")
def plan_update_target(name: Name, definition: TargetConfig) -> dict[str, object]:
    """Show the exact before/after state for a target update."""
    state = store.load()
    _replace(state, "targets", name, definition)
    return registration_update_plan("target_update", name, state.targets[name], definition)


@mcp.tool(annotations=WRITE)
@_journal_apply("update_target", "name")
def update_target(name: Name, definition: TargetConfig, plan_id: PlanId) -> dict[str, object]:
    """Apply an exact reviewed target update to local desired state."""
    expected = plan_update_target(name, definition)
    _assert_plan(expected, plan_id)
    store.save(_replace(store.load(), "targets", name, definition))
    return {"changed": True, "target": name}


@mcp.tool(annotations=WRITE)
@_journal_apply("register_application", "name")
def register_application(name: Name, definition: ApplicationConfig) -> dict[str, object]:
    """Register reusable application source and build metadata locally."""
    state = store.load()
    if name in state.applications:
        raise ValueError("application already exists; use plan_update_application")
    store.save(_replace(state, "applications", name, definition))
    return {"changed": True, "application": name}


@mcp.tool(annotations=READ)
@_journal_plan("update_application", "name")
def plan_update_application(name: Name, definition: ApplicationConfig) -> dict[str, object]:
    """Show the exact before/after state for an application update."""
    state = store.load()
    _replace(state, "applications", name, definition)
    return registration_update_plan(
        "application_update", name, state.applications[name], definition
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("update_application", "name")
def update_application(name: Name, definition: ApplicationConfig,
                       plan_id: PlanId) -> dict[str, object]:
    """Apply an exact reviewed application update to local desired state."""
    expected = plan_update_application(name, definition)
    _assert_plan(expected, plan_id)
    store.save(_replace(store.load(), "applications", name, definition))
    return {"changed": True, "application": name}


@mcp.tool(annotations=WRITE)
@_journal_apply("register_resource", "name")
def register_resource(name: Name, definition: Resource) -> dict[str, object]:
    """Register a named, exact-version infrastructure resource locally."""
    state = store.load()
    if name in state.resources:
        raise ValueError("resource already exists; use plan_update_resource")
    if isinstance(definition, AWSRDSPostgresResource):
        _refuse_unverifiable_tls_region(state, definition)
    if isinstance(definition, AWSElastiCacheValkeyResource):
        _refuse_unavailable_node_type(state, definition)
    store.save(_replace(state, "resources", name, definition))
    return {"changed": True, "resource": name}


@mcp.tool(annotations=READ)
@_journal_plan("update_resource", "name")
def plan_update_resource(name: Name, definition: Resource) -> dict[str, object]:
    """Show the exact before/after state for a resource update."""
    state = store.load()
    current = state.resources.get(name)
    if current is not None and (
        isinstance(current, AWSElastiCacheValkeyResource)
        or isinstance(definition, AWSElastiCacheValkeyResource)
    ):
        if not (
            isinstance(current, AWSElastiCacheValkeyResource)
            and isinstance(definition, AWSElastiCacheValkeyResource)
        ):
            raise ResourceError("aws_elasticache_update_forbidden_provider")
        resources_valkey_module.validate_update(
            current, definition, resources_valkey_module.load_observed(store.root, name),
            {
                d.target for d in state.deployments.values()
                if d.resources.valkey is not None and d.resources.valkey.resource == name
            },
        )
        if definition.node_type != current.node_type:
            _refuse_unavailable_node_type(state, definition)
    if current is not None and (
        isinstance(current, AWSRDSPostgresResource)
        or isinstance(definition, AWSRDSPostgresResource)
    ):
        if not (
            isinstance(current, AWSRDSPostgresResource)
            and isinstance(definition, AWSRDSPostgresResource)
        ):
            raise ResourceError("aws_rds_update_forbidden_provider")
        resources_postgres_module.validate_update(
            current, definition, resources_postgres_module.load_observed(store.root, name),
            {d.target for d in state.deployments.values() if d.resources.database == name},
        )
    _replace(state, "resources", name, definition)
    return registration_update_plan("resource_update", name, state.resources[name], definition)


@mcp.tool(annotations=WRITE)
@_journal_apply("update_resource", "name")
def update_resource(name: Name, definition: Resource, plan_id: PlanId) -> dict[str, object]:
    """Apply an exact reviewed resource update to local desired state."""
    with _deployment_resource_locks(*_resource_deployment_names(name)):
        expected = plan_update_resource(name, definition)
        _assert_plan(expected, plan_id)
        store.save(_replace(store.load(), "resources", name, definition))
        return {"changed": True, "resource": name}


def _refuse_unverifiable_tls_region(state: ControlState, resource: AWSRDSPostgresResource) -> None:
    # Only the AWS commercial-region trust bundle is pinned, so a us-gov-* or cn-* instance could be
    # created but never bound. Refuse before anything is created.
    return _managed_resource_orchestrator()._refuse_unverifiable_tls_region(state, resource)


def _refuse_unavailable_node_type(
    state: ControlState, resource: AWSElastiCacheValkeyResource
) -> None:
    # The one AWS read registration makes: a node type the account cannot buy in the region
    # would only fail later, at create.
    return _managed_resource_orchestrator()._refuse_unavailable_node_type(state, resource)


def _managed_resource(name: str) -> tuple[ControlState, AWSRDSPostgresResource]:
    return _managed_resource_orchestrator()._managed_resource(name)


def _managed_valkey(name: str) -> tuple[ControlState, AWSElastiCacheValkeyResource] | None:
    return _managed_resource_orchestrator()._managed_valkey(name)


def _resource_provision_plan(name: str) -> dict[str, object]:
    return _managed_resource_orchestrator()._resource_provision_plan(name)


def _resource_deployment_names(name: str) -> list[str]:
    return _managed_resource_orchestrator()._resource_deployment_names(name)


@mcp.tool(annotations=READ)
@_journal_plan("apply_resource", "name")
def plan_apply_resource(name: Name) -> dict[str, object]:
    """Plan provisioning or reconciling one managed AWS RDS PostgreSQL instance or
    ElastiCache Valkey replication group."""
    return _managed_resource_orchestrator().plan_apply_resource(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("apply_resource", "name")
def apply_resource(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Create the RDS instance, or converge an existing one onto desired state with one
    immediate modification, polling at most 30 seconds before returning a bounded pending
    phase. Never returns a decrypted credential."""
    return _managed_resource_orchestrator().apply_resource(name, plan_id)


@mcp.tool(annotations=READ)
def inspect_resource(name: Name) -> dict[str, object]:
    """Read-only, secret-free provider identity, health, and version for one resource."""
    return _managed_resource_orchestrator().inspect_resource(name)


def _inspect_valkey(
    state: ControlState, name: str, resource: AWSElastiCacheValkeyResource
) -> dict[str, object]:
    """Bounded and secret-free: no endpoint, address, ARN, user, or secret identifier."""
    return _managed_resource_orchestrator()._inspect_valkey(state, name, resource)


@mcp.tool(annotations=READ)
@_journal_plan("bind_resource", "name")
def plan_bind_resource(name: Name) -> dict[str, object]:
    """Plan creating this deployment's isolated database, role, and workload secret, and
    its Valkey ACL user, namespace, and credential when it binds a managed Valkey."""
    return _managed_resource_orchestrator().plan_bind_resource(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("bind_resource", "name")
def bind_resource(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Create or reconcile the deployment's isolated database and Valkey ACL user, each with
    its Resource Credential. Never returns a workload username or password."""
    return _managed_resource_orchestrator().bind_resource(name, plan_id)


def _resource_cleanup_plan(name: str) -> dict[str, object]:
    state = store.load()
    resource = state.resources.get(name)
    if resource is None:
        raise KeyError(f"resource '{name}' is not registered")
    if any(
        deployment.resources.database == name
        or getattr(deployment.resources.valkey, "resource", None) == name
        for deployment in state.deployments.values()
    ):
        raise ValueError(f"resource {name} is still referenced by a deployment")
    if isinstance(resource, AWSElastiCacheValkeyResource):
        return resource_cleanup_plan(name, managed=True, subject="ElastiCache replication group")
    return resource_cleanup_plan(name, managed=isinstance(resource, AWSRDSPostgresResource))


@mcp.tool(annotations=READ)
@_journal_plan("cleanup_resource", "name")
def plan_cleanup_resource(name: Name) -> dict[str, object]:
    """Plan non-destructive Resource removal; a managed Resource is retained by default."""
    return _resource_cleanup_plan(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("cleanup_resource", "name")
def apply_cleanup_resource(name: Name, plan_id: PlanId, confirmation: str) -> dict[str, object]:
    """Remove local Resource registration after exact plan and confirmation checks.
    A managed AWS resource and its data are left intact as a Retained Resource."""
    expected = _resource_cleanup_plan(name)
    _assert_plan(expected, plan_id)
    if confirmation != expected["confirmation"]:
        raise ValueError(f"confirmation must exactly equal '{expected['confirmation']}'")
    state = store.load()
    resource = state.resources[name]
    retained = isinstance(resource, (AWSRDSPostgresResource, AWSElastiCacheValkeyResource))
    if isinstance(resource, AWSElastiCacheValkeyResource):
        resources_valkey_module.retain_group(store.root, name, resource.aws_network)
    elif isinstance(resource, AWSRDSPostgresResource):
        resources_postgres_module.retain_resource(store.root, name, resource.aws_network)
    store.save(_delete(state, "resources", name))
    return {"changed": True, "resource": name, "retained": retained}


def _resource_destroy_plan(name: str) -> dict[str, object]:
    state = store.load()
    resource = state.resources.get(name)
    if not isinstance(resource, AWSElastiCacheValkeyResource):
        raise ValueError(f"resource {name} is not a managed ElastiCache Valkey resource")
    if any(
        getattr(deployment.resources.valkey, "resource", None) == name
        for deployment in state.deployments.values()
    ):
        raise ValueError(f"resource {name} is still referenced by a deployment")
    observed = resources_valkey_module.load_observed(store.root, name)
    if observed is not None and set(cast(dict[str, object], observed["allocations"])) & set(
        state.deployments
    ):
        raise ResourceError("aws_elasticache_destroy_bindings_remain")
    account = state.provider_accounts[state.aws_networks[resource.aws_network].provider_account]
    if account.destructive_role_arn is None:
        raise ResourceError("aws_elasticache_destroy_role_missing")
    fingerprint, users = resources_valkey_module.destruction_targets(store.root, name)
    return valkey_destroy_plan(
        name, fingerprint,
        resources_valkey_module.final_snapshot_id(
            resources_valkey_module.derive_group_id(name), fingerprint
        ),
        len(users),
    )


@mcp.tool(annotations=READ)
@_journal_plan("destroy_resource", "name")
def plan_destroy_resource(name: Name) -> dict[str, object]:
    """Plan destroying a managed ElastiCache Valkey Resource and its data, keeping a final
    snapshot. Reads only local state; the destructive role is never assumed while planning."""
    return _resource_destroy_plan(name)


@mcp.tool(annotations=CHANGE)
@_journal_apply("destroy_resource", "name")
def apply_destroy_resource(name: Name, plan_id: PlanId, confirmation: str) -> dict[str, object]:
    """Irreversibly delete the replication group (with a final snapshot) and what Gimme created
    around it, using the Provider Account's destructive role. A group still deleting after 30
    seconds returns phase 'deleting'; repeat the same call to continue."""
    expected = _resource_destroy_plan(name)
    _assert_plan(expected, plan_id)
    if confirmation != expected["confirmation"]:
        raise ValueError(f"confirmation must exactly equal '{expected['confirmation']}'")
    state = store.load()
    resource = cast(AWSElastiCacheValkeyResource, state.resources[name])
    network = state.aws_networks[resource.aws_network]
    observed = resources_valkey_module.load_observed(store.root, name)
    secret_names = (
        ["_admin", *sorted(cast(dict[str, object], observed["allocations"]))]
        if observed else ["_admin"]
    )
    result = resources_valkey_module.apply_destroy(
        elasticache_valkey, store.root, state.provider_accounts[network.provider_account],
        network, name, str(expected["identity_fingerprint"]),
    )
    if result["destroyed"]:
        # Reloaded: the destruction can outlast other edits to desired state.
        store.save(_delete(store.load(), "resources", name))
        resources_valkey_module.record_destroyed_group(
            store.root, name, resource.aws_network, str(expected["identity_fingerprint"])
        )
        resources_valkey_module.record_destroyed_secrets(
            store.root, name, resource.workload_secret_store, secret_names
        )
    return {"changed": True, **result}


def _valkey_context(name: str) -> tuple[
    ControlState, AWSElastiCacheValkeyResource, AWSNetwork, AWSProviderAccount,
    AWSSecretsManagerStore,
]:
    state = store.load()
    resource = state.resources.get(name)
    if not isinstance(resource, AWSElastiCacheValkeyResource):
        raise ValueError(f"resource {name} is not a managed ElastiCache Valkey resource")
    network = state.aws_networks[resource.aws_network]
    workload_store = cast(
        AWSSecretsManagerStore, state.secret_stores[resource.workload_secret_store]
    )
    return state, resource, network, state.provider_accounts[network.provider_account], (
        workload_store
    )


@mcp.tool(annotations=READ)
def list_resource_snapshots(name: Name) -> dict[str, object]:
    """Read-only list of the snapshots of one managed Valkey Resource's replication group,
    including the final snapshot of a destroyed group, newest first. Names and status only."""
    _state, _resource, network, account, _store = _valkey_context(name)
    snapshots = elasticache_valkey.list_snapshots(
        account, network, resources_valkey_module.derive_group_id(name)
    )
    return {"resource": name, "snapshots": [vars(item) for item in snapshots]}


def _final_snapshot_purge_plan(name: str) -> dict[str, object]:
    state = store.load()
    if name in state.resources:
        raise ValueError(f"resource {name} is still registered")
    receipt = resources_valkey_module.load_destroyed_receipt(store.root, name)
    if receipt is None:
        raise KeyError(f"no destroyed Valkey resource named '{name}'")
    network = state.aws_networks.get(receipt["aws_network"])
    if network is None:
        raise ResourceError("aws_elasticache_destroy_receipt_invalid")
    account = state.provider_accounts[network.provider_account]
    if account.destructive_role_arn is None:
        raise ResourceError("aws_elasticache_destroy_role_missing")
    return exact_plan({
        "kind": "valkey_final_snapshot_purge", "resource": name,
        "confirmation": f"PURGE FINAL SNAPSHOT {name}",
        "snapshot": receipt["final_snapshot"],
        "destroys": ["the final snapshot retained after this Resource was destroyed"],
        "retains": ["manual snapshots and Secrets Manager credentials"],
        "authority": "the Provider Account's destructive role, assumed only during apply",
        "irreversible": True,
    })


@mcp.tool(annotations=READ)
@_journal_plan("purge_final_snapshot", "name")
def plan_purge_final_snapshot(name: Name) -> dict[str, object]:
    """Plan deleting only the deterministic final snapshot left by a destroyed Valkey Resource."""
    return _final_snapshot_purge_plan(name)


@mcp.tool(annotations=CHANGE)
@_journal_apply("purge_final_snapshot", "name")
def apply_purge_final_snapshot(
    name: Name, plan_id: PlanId, confirmation: str
) -> dict[str, object]:
    """Delete the exact final snapshot in a reviewed destruction receipt, never a caller-supplied
    snapshot identifier."""
    expected = _final_snapshot_purge_plan(name)
    _assert_plan(expected, plan_id)
    if confirmation != expected["confirmation"]:
        raise ValueError(f"confirmation must exactly equal '{expected['confirmation']}'")
    receipt = resources_valkey_module.load_destroyed_receipt(store.root, name)
    if receipt is None:
        raise KeyError(f"no destroyed Valkey resource named '{name}'")
    state = store.load()
    network = state.aws_networks[receipt["aws_network"]]
    account = state.provider_accounts[network.provider_account]
    deleted = elasticache_valkey.delete_final_snapshot(
        account, network, receipt["final_snapshot"]
    )
    resources_valkey_module.clear_destroyed_receipt(store.root, name)
    return {"changed": deleted, "resource": name, "purged": deleted}


def _retained_secret_purge_plan(name: str) -> dict[str, object]:
    state = store.load()
    if name in state.resources:
        raise ValueError(f"resource {name} is still registered")
    receipt = resources_valkey_module.load_destroyed_secrets(store.root, name)
    if receipt is None:
        raise KeyError(f"no destroyed Valkey credentials named '{name}'")
    store_name, secrets = receipt
    secret_store = state.secret_stores.get(store_name)
    if not isinstance(secret_store, AWSSecretsManagerStore):
        raise ResourceError("aws_elasticache_destroy_receipt_invalid")
    account = state.provider_accounts[secret_store.provider_account]
    if account.destructive_role_arn is None:
        raise ResourceError("aws_elasticache_destroy_role_missing")
    return exact_plan({
        "kind": "valkey_retained_secret_purge", "resource": name,
        "confirmation": f"PURGE RETAINED SECRETS {name}", "credentials": len(secrets),
        "destroys": ["Gimme-owned Valkey administrative and deployment credentials"],
        "authority": "the Provider Account's destructive role, assumed only during apply",
        "irreversible": True,
    })


@mcp.tool(annotations=READ)
@_journal_plan("purge_retained_secrets", "name")
def plan_purge_retained_secrets(name: Name) -> dict[str, object]:
    """Plan deleting only exact Gimme-owned credentials recorded after a Valkey destroy."""
    return _retained_secret_purge_plan(name)


@mcp.tool(annotations=CHANGE)
@_journal_apply("purge_retained_secrets", "name")
def apply_purge_retained_secrets(
    name: Name, plan_id: PlanId, confirmation: str
) -> dict[str, object]:
    """Force-delete receipt-recorded credentials only after ownership verification and
    confirmation."""
    expected = _retained_secret_purge_plan(name)
    _assert_plan(expected, plan_id)
    if confirmation != expected["confirmation"]:
        raise ValueError(f"confirmation must exactly equal '{expected['confirmation']}'")
    store_name, secret_names = cast(
        tuple[str, list[str]], resources_valkey_module.load_destroyed_secrets(store.root, name)
    )
    state = store.load()
    secret_store = cast(AWSSecretsManagerStore, state.secret_stores[store_name])
    account = state.provider_accounts[secret_store.provider_account]
    deleted = elasticache_valkey.delete_retained_secrets(
        account, secret_store, store_name, name, secret_names
    )
    resources_valkey_module.clear_destroyed_secrets(store.root, name)
    return {"changed": deleted > 0, "resource": name, "purged": deleted}


def _binds(state: ControlState, deployment: str, name: str) -> bool:
    bound = state.deployments.get(deployment)
    return bound is not None and getattr(bound.resources.valkey, "resource", None) == name


def _restore_plan(name: str, snapshot: str | None) -> dict[str, object]:
    state, resource, _network, _account, _store = _valkey_context(name)
    observed = resources_valkey_module.load_observed(store.root, name)
    if snapshot is None and observed is None:
        raise ResourceError("aws_elasticache_recreate_not_needed")
    # An allocation whose Deployment is gone still has its user restored, but has nothing to verify.
    return valkey_restore_plan(
        name, snapshot,
        sorted(d for d in valkey_recovery.restore_targets(store.root, name)
               if _binds(state, d, name)),
        resource.engine_version,
    )


def _restore_valkey(name: str, snapshot: str | None) -> dict[str, object]:
    state, resource, network, account, workload_store = _valkey_context(name)

    def verify(deployment: str) -> None:
        if not _binds(store.load(), deployment, name):
            return
        token = _restoring_ok.set(True)
        try:
            _apply_resources(deployment, _resource_plan(deployment))
            _run_deployment("gimme:probe:valkey:current", deployment, timeout=300)
            _run_deployment("gimme:restart:workers", deployment, timeout=300)
        finally:
            _restoring_ok.reset(token)

    return {"changed": True, **valkey_recovery.apply_restore(
        elasticache_valkey, store.root, account, network, resource, name, workload_store,
        resource.workload_secret_store, snapshot, verify,
    )}


@mcp.tool(annotations=READ)
@_journal_plan("restore_resource", "name")
def plan_restore_resource(name: Name, snapshot: SnapshotName) -> dict[str, object]:
    """Plan re-creating a lost managed Valkey replication group from one of its snapshots.
    Reads only local state; apply checks the snapshot against AWS."""
    return _restore_plan(name, snapshot)


@mcp.tool(annotations=CHANGE)
@_journal_apply("restore_resource", "name")
def apply_restore_resource(name: Name, snapshot: SnapshotName, plan_id: PlanId
                           ) -> dict[str, object]:
    """Create the replication group from the snapshot only if it does not exist, restore each
    recorded Deployment credential, then verify every Deployment before the Resource is ready.
    Phase 'restoring' means repeat the same call to continue."""
    with _deployment_resource_locks(*_resource_deployment_names(name)):
        _assert_plan(_restore_plan(name, snapshot), plan_id)
        return _restore_valkey(name, snapshot)


@mcp.tool(annotations=READ)
@_journal_plan("recreate_empty_resource", "name")
def plan_recreate_empty_resource(name: Name) -> dict[str, object]:
    """Plan replacing a lost managed Valkey replication group with an empty one, accepting the
    loss of its data. Reads only local state."""
    return _restore_plan(name, None)


@mcp.tool(annotations=CHANGE)
@_journal_apply("recreate_empty_resource", "name")
def apply_recreate_empty_resource(name: Name, plan_id: PlanId, confirmation: str
                                  ) -> dict[str, object]:
    """Create an empty replication group in place of a lost one after exact confirmation, then
    verify every recorded Deployment as a restore does."""
    with _deployment_resource_locks(*_resource_deployment_names(name)):
        expected = _restore_plan(name, None)
        _assert_plan(expected, plan_id)
        if confirmation != expected["confirmation"]:
            raise ValueError(f"confirmation must exactly equal '{expected['confirmation']}'")
        return _restore_valkey(name, None)


def _rotation_plan(name: str, deployment: str) -> dict[str, object]:
    state, _resource, _network, account, _store = _valkey_context(name)
    if account.destructive_role_arn is None:
        raise ResourceError("aws_elasticache_destroy_role_missing")
    observed = resources_valkey_module.load_observed(store.root, name)
    if (
        observed is None or deployment not in cast(dict[str, object], observed["allocations"])
        or not _binds(state, deployment, name)
    ):
        raise ResourceError("aws_elasticache_rotate_binding_missing")
    return valkey_rotation_plan(
        name, deployment, resources_valkey_module.identity_fingerprint(str(observed["identity"]))
    )


@mcp.tool(annotations=READ)
@_journal_plan("rotate_resource_credential", "name")
def plan_rotate_resource_credential(name: Name, deployment: Name) -> dict[str, object]:
    """Plan replacing one Deployment's Valkey ACL user and Resource Credential. Reads only
    local state; the destructive role is never assumed while planning."""
    return _rotation_plan(name, deployment)


@mcp.tool(annotations=CHANGE)
@_journal_apply("rotate_resource_credential", "name")
def apply_rotate_resource_credential(name: Name, deployment: Name, plan_id: PlanId
                                     ) -> dict[str, object]:
    """Rotate the Deployment's credential with a probed switch and automatic rollback. A
    leftover rotation is finished or rolled back by this same call, which then does nothing
    else. Never returns a username or password."""
    with _deployment_resource_lock(deployment):
        _assert_plan(_rotation_plan(name, deployment), plan_id)
        _state, resource, network, account, workload_store = _valkey_context(name)

        def switch(target: str) -> None:
            _apply_resources(target, _resource_plan(target))
            _run_deployment("gimme:probe:valkey:current", target, timeout=300)
            _run_deployment("gimme:restart:workers", target, timeout=300)

        return {"changed": True, **valkey_recovery.apply_rotation(
            elasticache_valkey, store.root, account, network, resource, name, workload_store,
            resource.workload_secret_store, deployment, switch,
        )}


def _resource_forget_plan(name: str) -> dict[str, object]:
    if name in store.load().resources:
        raise ValueError(f"resource {name} is still registered; only a retained one is forgotten")
    try:
        retained = resources_postgres_module.load_retained(store.root, name) is not None
    except ResourceError:
        retained = True  # a corrupt tombstone can still be forgotten
    if not retained:
        raise KeyError(f"no retained resource named '{name}'")
    return resource_forget_plan(name)


@mcp.tool(annotations=READ)
@_journal_plan("forget_resource", "name")
def plan_forget_resource(name: Name) -> dict[str, object]:
    """Plan deleting a Retained Resource tombstone. Local only."""
    return _resource_forget_plan(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("forget_resource", "name")
def apply_forget_resource(name: Name, plan_id: PlanId, confirmation: str) -> dict[str, object]:
    """Delete a Retained Resource tombstone after exact confirmation. The infrastructure it
    named is not touched and cannot be adopted again."""
    expected = _resource_forget_plan(name)
    _assert_plan(expected, plan_id)
    if confirmation != expected["confirmation"]:
        raise ValueError(f"confirmation must exactly equal '{expected['confirmation']}'")
    resources_postgres_module.forget_retained(store.root, name)
    return {"changed": True, "resource": name}


@mcp.tool(annotations=WRITE)
@_journal_apply("register_deployment", "name")
def register_deployment(name: Name, definition: DeploymentRegistration) -> dict[str, object]:
    """Register a deployment and allocate its immutable placement identities."""
    state = store.load()
    if name in state.deployments:
        raise ValueError("deployment already exists; use plan_update_deployment")
    target = state.targets[definition.target]
    deployment = definition.materialize(new_placement(name, target, domain=definition.domain))
    store.save(_replace(state, "deployments", name, deployment))
    return {"changed": True, "deployment": name,
            "placement": deployment.placement.model_dump(mode="json")}


@mcp.tool(annotations=READ)
@_journal_plan("update_deployment", "name")
def plan_update_deployment(name: Name,
                           definition: DeploymentRegistration) -> dict[str, object]:
    """Show a deployment update while preserving immutable placement fields."""
    state = store.load()
    current = state.deployments[name]
    placement = current.placement
    if definition.domain is not None and definition.domain != placement.site_host:
        placement = placement.model_copy(update={"site_host": definition.domain})
    proposed = definition.materialize(placement)
    _replace(state, "deployments", name, proposed)
    return registration_update_plan("deployment_update", name, current, proposed)


@mcp.tool(annotations=WRITE)
@_journal_apply("update_deployment", "name")
def update_deployment(name: Name, definition: DeploymentRegistration,
                      plan_id: PlanId) -> dict[str, object]:
    """Apply an exact reviewed deployment update to local desired state."""
    with _deployment_resource_lock(name):
        expected = plan_update_deployment(name, definition)
        _assert_plan(expected, plan_id)
        proposed = DeploymentConfig.model_validate(expected["proposed"])
        store.save(_replace(store.load(), "deployments", name, proposed))
        return {"changed": True, "deployment": name}


@mcp.tool(annotations=READ)
def inspect_target(name: Name) -> dict[str, object]:
    """Inspect a target's OS, services, helpers, TLS, and SSH-agent readiness."""
    return _target_runtime_orchestrator().inspect_target(name)


@mcp.tool(annotations=READ)
@_journal_plan("target_stack", "name")
def plan_target_stack(name: Name) -> dict[str, object]:
    """Preflight packages and helpers and return the exact target stack plan."""
    return _target_runtime_orchestrator().plan_target_stack(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("target_stack", "name")
def apply_target_stack(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Reconcile a target stack through its bootstrapped privileged helper."""
    return _target_runtime_orchestrator().apply_target_stack(name, plan_id)


@mcp.tool(annotations=READ)
@_journal_plan("deployment_runtimes", "name")
def plan_deployment_runtimes(name: Name) -> dict[str, object]:
    """Plan exact runtime and extension reconciliation for one deployment."""
    return _target_runtime_orchestrator().plan_deployment_runtimes(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("deployment_runtimes", "name")
def apply_deployment_runtimes(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Install mise pins and verify system runtimes for one deployment."""
    return _target_runtime_orchestrator().apply_deployment_runtimes(name, plan_id)


@mcp.tool(annotations=READ)
@_journal_plan("deployment_resources", "name")
def plan_deployment_resources(name: Name) -> dict[str, object]:
    """Plan routing, database, cache, runtime values, secrets, and processes."""
    return _deployment_resource_orchestrator().plan_deployment_resources(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("deployment_resources", "name")
def apply_deployment_resources(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Reconcile one deployment's route and target-local runtime resources."""
    return _deployment_resource_orchestrator().apply_deployment_resources(name, plan_id)


def _apply_resources(name: str, expected: dict[str, Any]) -> dict[str, object]:
    return _deployment_resource_orchestrator().apply_resources(name, expected)


@mcp.tool(annotations=READ)
@_journal_plan("deployment", "name")
def plan_deployment(name: Name) -> dict[str, object]:
    """Resolve source and pinned runtimes and render the exact Deployer task graph."""
    return _deployment_release_orchestrator().plan_deployment(name)


@mcp.tool(annotations=CHANGE)
@_journal_apply("deployment", "name")
def apply_deployment(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Deploy an exact reviewed revision with health gates and worker refresh."""
    return _deployment_release_orchestrator().apply_deployment(name, plan_id)


@mcp.tool(annotations=READ)
def list_releases(name: Name) -> dict[str, object]:
    """List retained releases for one deployment and identify the current release."""
    return _deployment_release_orchestrator().list_releases(name)


@mcp.tool(annotations=CHANGE)
@_journal_apply("rollback_deployment", "name")
def rollback_deployment(name: Name, confirmation: str) -> dict[str, object]:
    """Restore a deployment's prior retained release after exact confirmation."""
    return _deployment_release_orchestrator().rollback_deployment(name, confirmation)


@mcp.tool(annotations=READ)
@_journal_plan("promotion", "source", "destination")
def plan_promotion(source: Name, destination: Name) -> dict[str, object]:
    """Plan deploying the source deployment's exact live commit to a destination."""
    return _deployment_release_orchestrator().plan_promotion(source, destination)


@mcp.tool(annotations=CHANGE)
@_journal_apply("promotion", "source", "destination")
def promote_deployment(source: Name, destination: Name, plan_id: PlanId) -> dict[str, object]:
    """Promote an exact reviewed live commit and pin the destination after success."""
    return _deployment_release_orchestrator().promote_deployment(
        source, destination, plan_id
    )


@mcp.tool(annotations=READ)
@_journal_plan("remove_deployment", "name")
def plan_remove_deployment(name: Name) -> dict[str, object]:
    """Plan complete cleanup of one deployment and its isolated resources."""
    _, deployment, target, _ = _context(name)
    return deployment_removal_plan(name, deployment, target)


@mcp.tool(annotations=CHANGE)
@_journal_apply("remove_deployment", "name")
def remove_deployment(name: Name, plan_id: PlanId, confirmation: str) -> dict[str, object]:
    """Remove a deployment after exact plan and confirmation checks."""
    with _deployment_resource_lock(name):
        expected = plan_remove_deployment(name)
        _assert_plan(expected, plan_id)
        if confirmation != expected["confirmation"]:
            raise ValueError(f"confirmation must exactly equal '{expected['confirmation']}'")
        state, deployment, _target, _application = _context(name)
        if deployment.recovery is not None:
            cleanup_deployment = deployment.model_copy(update={
                "recovery": deployment.recovery.model_copy(update={
                    "cadence": ManualRecoveryCadence(), "valkey": False,
                })
            })
            authority = _recovery_schedule_authority(name, state, cleanup_deployment)
            if authority is None:
                raise RuntimeError("Recovery Schedule cleanup authority is unavailable")
            try:
                _run_deployment(
                    "gimme:recovery:schedule-reconcile", name,
                    recovery_schedule_authority=authority, timeout=1800,
                )
            except Exception:
                raise RuntimeError("recovery_schedule_cleanup_failed") from None
        result = _run_deployment("gimme:remove:deployment", name, timeout=1800)
        state = _delete(store.load(), "deployments", name)
        store.save(state)
        (store.root / "applied-secrets" / f"{name}.json").unlink(missing_ok=True)
        target = state.targets[expected["target"]]
        runner.run("gimme:reconcile:sites", legacy_server(target), stack=target.stack,
                   sites=target_sites(state, str(expected["target"])),
                   network_mode=target.network.mode,
                   mise_version=target.runtimes.mise_version, timeout=1800)
        return _result(result)


@mcp.tool(annotations=READ)
@_journal_plan("artisan", "name")
def plan_artisan(name: Name, command: str, arguments: list[str] | None = None) -> dict[str, object]:
    """Plan an allowlisted structured Artisan invocation in one deployment."""
    _, _, _, application = _context(name)
    allowed = application.artisan.allowed_commands if application.artisan is not None else []
    if command not in allowed:
        raise ValueError("Artisan command is not allowlisted")
    args = arguments or []
    if len(args) > 32 or any(not value or len(value) > 256 for value in args):
        raise ValueError("Artisan arguments are invalid")
    return exact_plan({"kind": "artisan", "deployment": name,
                       "argv": ["php", "artisan", "--no-interaction", command, *args]})


@mcp.tool(annotations=CHANGE)
@_journal_apply("artisan", "name")
def run_artisan(name: Name, command: str, plan_id: PlanId,
                arguments: list[str] | None = None) -> dict[str, object]:
    """Run an exact reviewed allowlisted Artisan invocation."""
    with _deployment_resource_lock(name):
        expected = plan_artisan(name, command, arguments)
        _assert_plan(expected, plan_id)
        return _result(_run_deployment(
            "gimme:artisan", name, artisan_command=command,
            artisan_arguments=arguments or [],
        ))


@mcp.tool(annotations=READ)
def deployment_process_status(name: Name) -> dict[str, object]:
    """Inspect managed queue, Horizon, and scheduler units for one deployment."""
    return _result(_run_deployment("gimme:processes:status", name, timeout=60))


@mcp.tool(annotations=READ)
def diagnose_deployment(name: Name) -> dict[str, object]:
    """Run fixed secret-safe Laravel deployment diagnostics and live-health checks."""
    return _deployment_diagnostics(
        _run_deployment("gimme:diagnose:deployment", name, timeout=120)
    )


@mcp.tool(annotations=READ)
def target_service_status(name: Name,
                          service: Literal["postgresql", "valkey-server", "caddy"]
                          ) -> dict[str, object]:
    """Read status for one allowlisted service on a registered target."""
    target = store.target(name)
    # A raw "service=..." positional token is parsed by Deployer as a host selector
    # filter, not a config override; -o is required for get('gimme_service') to see it.
    return _result(runner.run("gimme:service:status", legacy_server(target),
                              stack=target.stack,
                              arguments=("-o", f"gimme_service={service}"), timeout=60))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
