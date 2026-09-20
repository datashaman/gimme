from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import tempfile
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Annotated, Any, Callable, Iterator, Literal, ParamSpec, TypeVar, cast

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from gimme import recovery as recovery_module
from gimme import recovery_schedule as recovery_schedule_module
from gimme import resources_postgres as resources_postgres_module
from gimme import resources_valkey as resources_valkey_module
from gimme import valkey_contract
from gimme import valkey_recovery
from gimme.control import (
    AWSElastiCacheValkeyResource, AWSNetwork, AWSProviderAccount, AWSRDSPostgresResource,
    AWSSecretsManagerStore, ApplicationConfig,
    ControlState, DeploymentConfig, DeploymentRegistration, DeploymentSource, Resource,
    ResourceConfig, S3BackupDestination, SecretReference, SecretStore, StateStore, TargetConfig,
    ValkeyBinding,
    legacy_app, legacy_server, new_placement, runs_horizon, target_sites,
)
from gimme.control_plans import (
    deployment_release_plan, deployment_removal_plan, deployment_resource_plan,
    deployment_restore_plan,
    exact_plan, migration_plan, recovery_point_creation_plan, recovery_point_deletion_plan,
    registration_update_plan, restore_verification_plan,
    resource_binding_plan, resource_cleanup_plan, resource_provision_plan, target_stack_plan,
    resource_forget_plan, valkey_binding_plan, valkey_destroy_plan,
    valkey_provision_plan, valkey_restore_plan, valkey_rotation_plan,
)
from gimme.deployer import CommandResult, DeployerRunner
from gimme.execution import execution_fingerprint
from gimme.journal import OperationJournal
from gimme.recovery import ComponentDump, RecoveryError, preflight_backup_destination
from gimme.resources_postgres import ResourceError
from gimme.secrets import (
    BotoAWSSecretAdapter, SecretError, load_applied_secret_manifest,
    plan_secret_references, protected_secret_file, resolve_planned_secret_references,
    save_applied_secret_manifest, validate_aws_account, validate_aws_store,
)

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
        timeout=timeout,
    )


@contextmanager
def _recovery_maintenance_window(
    name: str, request_id: str, wait_seconds: int, *, enabled: bool,
) -> Iterator[Callable[[], None]]:
    """Keep request-owned maintenance active until verified uploads are ready to publish."""
    pending = enabled

    def restore() -> None:
        nonlocal pending
        if not pending:
            return
        try:
            _run_deployment(
                "gimme:recovery:maintenance", name,
                recovery_action="exit", recovery_request_id=request_id,
                recovery_quiesce_wait=wait_seconds, timeout=900,
            )
        except Exception:
            raise RecoveryError("recovery_runtime_restore_failed") from None
        pending = False

    if enabled:
        try:
            _run_deployment(
                "gimme:recovery:maintenance", name,
                recovery_action="enter", recovery_request_id=request_id,
                recovery_quiesce_wait=wait_seconds, timeout=900,
            )
        except Exception:
            restore()
            raise RecoveryError("recovery_maintenance_failed") from None
    try:
        yield restore
    finally:
        restore()


def _capture_postgres_dump(
    name: str, local_path: Path, resource_version: str
) -> ComponentDump:
    try:
        result = _run_deployment(
            "gimme:backup:dump-postgres", name,
            backup_local_path=local_path, timeout=1800,
        )
    except Exception:
        raise RecoveryError("recovery_capture_failed") from None
    sha256 = ""
    size = -1
    for raw in result.output.splitlines():
        line = raw.split("] ", 1)[-1].strip()
        if line.startswith("GIMME_BACKUP|"):
            parts = line.split("|", 2)
            if len(parts) == 3 and parts[2].isdigit():
                sha256, size = parts[1], int(parts[2])
    if (
        re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or not 0 <= size <= recovery_module.MAX_COMPONENT_BYTES
        or not local_path.is_file()
        or local_path.is_symlink()
        or local_path.stat().st_size != size
    ):
        raise RecoveryError("recovery_dump_metadata_invalid")
    with local_path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
            raise RecoveryError("recovery_dump_metadata_invalid")
    return ComponentDump(
        kind="postgres", local_path=local_path, sha256=sha256, bytes=size,
        resource_version=resource_version,
    )


def _valkey_resource_version(
    resource: ResourceConfig | AWSElastiCacheValkeyResource,
) -> str:
    if isinstance(resource, ResourceConfig):
        if resource.kind != "valkey":
            raise RecoveryError("recovery_valkey_provenance_invalid")
        return resource.version
    return resource.engine_version


def _valkey_capture_credential(
    state: ControlState, resource_name: str,
    resource: ResourceConfig | AWSElastiCacheValkeyResource,
) -> dict[str, str]:
    if isinstance(resource, ResourceConfig):
        if resource.kind != "valkey":
            raise RecoveryError("recovery_valkey_provenance_invalid")
        return {}
    network = state.aws_networks[resource.aws_network]
    account = state.provider_accounts[network.provider_account]
    workload_store = cast(
        AWSSecretsManagerStore,
        state.secret_stores[resource.workload_secret_store],
    )
    elasticache_valkey.ensure_admin_capture_access(account, network, resource_name)
    return elasticache_valkey.resolve_admin_credential(
        account, workload_store, resource_name
    )


def _capture_valkey_dump(
    name: str, local_path: Path, resource_version: str,
    secret_file: Path | None,
) -> ComponentDump:
    try:
        result = _run_deployment(
            "gimme:backup:capture-valkey", name,
            backup_local_path=local_path, secret_file=secret_file, timeout=1800,
        )
    except Exception:
        raise RecoveryError("recovery_capture_failed") from None
    sha256 = ""
    size = -1
    records = -1
    captured_at = ""
    for raw in result.output.splitlines():
        line = raw.split("] ", 1)[-1].strip()
        if line.startswith("GIMME_VALKEY_BACKUP|"):
            parts = line.split("|", 4)
            if len(parts) == 5 and parts[2].isdigit() and parts[3].isdigit():
                sha256 = parts[1]
                size = int(parts[2])
                records = int(parts[3])
                captured_at = parts[4]
    try:
        capture_time = datetime.fromisoformat(captured_at)
    except ValueError:
        capture_time = None
    if (
        re.fullmatch(r"[0-9a-f]{64}", sha256) is None
        or not 0 <= size <= recovery_module.MAX_COMPONENT_BYTES
        or not 0 <= records <= 100_000
        or not local_path.is_file() or local_path.is_symlink()
        or local_path.stat().st_size != size
        or capture_time is None or capture_time.tzinfo is None
    ):
        raise RecoveryError("recovery_valkey_metadata_invalid")
    with local_path.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
            raise RecoveryError("recovery_valkey_metadata_invalid")
    return ComponentDump(
        kind="valkey", local_path=local_path, sha256=sha256, bytes=size,
        resource_version=resource_version, format="gimme-valkey-v1",
        records=records, captured_at=captured_at,
    )


def _capture_restore_safety(
    name: str, request_id: str, safety_id: str,
    safety_components: list[str], state: ControlState,
    deployment: DeploymentConfig, destination_name: str,
    destination: S3BackupDestination,
    credentials: tuple[str, str] | None,
) -> dict[str, object]:
    """Capture and verify exactly the protected destination components."""
    dumps: list[ComponentDump] = []
    expected_versions: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="gimme-restore-safety-") as directory:
        root = Path(directory)
        if "postgres" in safety_components:
            resource_name = deployment.resources.database
            resource = state.resources.get(resource_name) if resource_name else None
            if not isinstance(resource, ResourceConfig) or resource.kind != "postgres":
                raise RecoveryError("restore_destination_incompatible")
            expected_versions["postgres"] = resource.version
            dumps.append(_capture_postgres_dump(
                name, root / "postgres.dump", resource.version
            ))
        if "valkey" in safety_components:
            binding = deployment.resources.valkey
            resource_name = binding.resource if binding is not None else None
            resource = state.resources.get(resource_name) if resource_name else None
            if not isinstance(
                resource, (ResourceConfig, AWSElastiCacheValkeyResource)
            ) or resource_name is None:
                raise RecoveryError("restore_destination_incompatible")
            version = _valkey_resource_version(resource)
            expected_versions["valkey"] = version
            credential = _valkey_capture_credential(state, resource_name, resource)
            with protected_secret_file(credential) as secret_file:
                dumps.append(_capture_valkey_dump(
                    name, root / "valkey.archive", version, secret_file
                ))
        safety = recovery_module.create_recovery_point(
            destination_name, destination, credentials, backup_s3,
            name, safety_id, dumps, safety_restore_request_id=request_id,
        )
    components = cast(list[dict[str, object]], safety["components"])
    observed = {
        str(component["kind"]): str(component["resource_version"])
        for component in components
    }
    if (
        safety["safety"] is not True
        or safety["restore_request_id"] != request_id
        or observed != expected_versions
        or len(components) != len(safety_components)
    ):
        raise RecoveryError("restore_safety_conflict")
    return safety


def _restore_valkey_component(
    name: str, request_id: str, component: dict[str, object],
    local_source: Path, state: ControlState, deployment: DeploymentConfig,
) -> None:
    binding = deployment.resources.valkey
    resource_name = binding.resource if binding is not None else None
    resource = state.resources.get(resource_name) if resource_name else None
    if not isinstance(
        resource, (ResourceConfig, AWSElastiCacheValkeyResource)
    ) or resource_name is None:
        raise RecoveryError("restore_destination_incompatible")
    credential = _valkey_capture_credential(state, resource_name, resource)
    try:
        with protected_secret_file(credential) as secret_file:
            result = _run_deployment(
                "gimme:recovery:valkey", name,
                backup_local_path=local_source, secret_file=secret_file,
                valkey_restore_request_id=request_id,
                valkey_restore_sha256=str(component["sha256"]),
                valkey_restore_bytes=int(component["bytes"]),
                valkey_restore_records=int(component["records"]),
                timeout=3600,
            )
    except Exception:
        raise RecoveryError("valkey_restore_failed") from None
    markers = []
    for raw in result.output.splitlines():
        line = raw.split("] ", 1)[-1].strip()
        match = re.fullmatch(
            r"GIMME_VALKEY_RESTORE\|([0-9]{1,6})\|([0-9]{1,6})", line
        )
        if match is not None:
            markers.append((int(match[1]), int(match[2])))
    if len(markers) != 1 or sum(markers[0]) != int(component["records"]):
        raise RecoveryError("valkey_verification_failed")


def _revision(name: str) -> str:
    deployment = store.deployment(name)
    if deployment.source.kind == "commit":
        return deployment.source.ref
    result = _run_deployment("gimme:resolve-revision", name, timeout=60)
    for raw in result.output.splitlines():
        line = raw.split("] ", 1)[-1].strip()
        if line.startswith("GIMME_REVISION|"):
            revision = line.split("|", 1)[1]
            if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision):
                return revision
    raise RuntimeError("source did not resolve to one exact Git revision")


def _dns_issues(deployment: DeploymentConfig, target: TargetConfig) -> list[str]:
    if target.network.mode != "public_dns":
        return []
    try:
        actual = {item[4][0] for item in socket.getaddrinfo(
            deployment.placement.site_host, 443, type=socket.SOCK_STREAM
        )}
    except socket.gaierror:
        return ["domain does not resolve"]
    return [] if actual & set(target.network.expected_addresses) else [
        "domain does not resolve to a declared target address"
    ]


def _valkey_runtime(
    name: str, state: ControlState, deployment: DeploymentConfig
) -> tuple[dict[str, str], dict[str, SecretReference], dict[str, object] | None, list[str]]:
    """The laravel-cluster-v1 values, credential references, and probe input for a Deployment
    bound to a managed Valkey, or why it is not ready to receive them. Nothing when the
    binding is Target-local."""
    binding = deployment.resources.valkey
    resource = None if binding is None else state.resources.get(binding.resource)
    if binding is None or not isinstance(resource, AWSElastiCacheValkeyResource):
        return {}, {}, None, []
    try:
        observed = resources_valkey_module.load_observed(store.root, binding.resource)
    except ResourceError:
        observed = None  # a corrupt cache must not break unrelated tasks; it is not ready
    if observed is None or observed["phase"] not in (
        ("ready", "restoring") if _restoring_ok.get() else ("ready",)
    ):
        return {}, {}, None, ["valkey_resource_not_ready"]
    if name not in cast(dict[str, object], observed["allocations"]):
        return {}, {}, None, ["valkey_binding_missing"]
    host, port = observed["endpoint"], observed["port"]
    if not isinstance(host, str) or not isinstance(port, int):
        return {}, {}, None, ["valkey_endpoint_missing"]
    return (
        valkey_contract.contract_variables(name, binding.uses, host, port),
        valkey_contract.credential_references(
            resource.workload_secret_store, binding.resource, name
        ),
        valkey_contract.probe_config(
            name, binding.uses, host, port, runs_horizon(deployment.workers)
        ),
        [],
    )


def _secret_plan(name: str, state: ControlState, deployment: DeploymentConfig
                 ) -> tuple[list[dict[str, str]], list[str]]:
    try:
        planned = plan_secret_references(
            state, store.secrets_path,
            {**deployment.secrets, **_valkey_runtime(name, state, deployment)[1]}, aws_secrets,
            load_applied_secret_manifest(store.root, name),
        )
        issues = ["secret_reference_missing" for item in planned if item["status"] == "missing"]
        return planned, issues
    except SecretError as exc:
        return [], [str(exc)]


def _contract_summary(
    name: str, state: ControlState, deployment: DeploymentConfig
) -> dict[str, object] | None:
    """What the contract will inject, without the endpoint or any credential."""
    binding = deployment.resources.valkey
    resource = None if binding is None else state.resources.get(binding.resource)
    if binding is None or not isinstance(resource, AWSElastiCacheValkeyResource):
        return None
    return {
        "contract": valkey_contract.CONTRACT, "resource": binding.resource,
        "uses": list(binding.uses),
        "variables_digest": StateStore.digest(_valkey_runtime(name, state, deployment)[0]),
        "namespaces": resources_valkey_module.namespace_prefixes(name, list(binding.uses)),
        "adapters": {
            key: ("redis" if use in binding.uses else valkey_contract.LOCAL_DRIVERS[key])
            for use, key in valkey_contract.ADAPTER_KEYS.items()
        },
        "credential_keys": [valkey_contract.USERNAME_KEY, valkey_contract.PASSWORD_KEY],
        "probes": valkey_contract.probe_names(
            list(binding.uses), runs_horizon(deployment.workers)
        ),
    }


def _resolved_stack_plan(name: str) -> dict[str, Any]:
    state = store.load()
    target = state.targets[name]
    result = runner.run("gimme:preflight:stack", legacy_server(target), stack=target.stack,
                        sites=target_sites(state, name), network_mode=target.network.mode,
                        mise_version=target.runtimes.mise_version,
                        timeout=60, bootstrap=True)
    resolution: dict[str, dict[str, str]] = {}
    busy: list[int] = []
    helper = "unknown"
    for raw in result.output.splitlines():
        line = raw.split("] ", 1)[-1].strip()
        if line.startswith("GIMME_PACKAGE|"):
            _, package, installed, candidate = line.split("|", 3)
            resolution[package] = {"installed": installed, "candidate": candidate}
        elif line.startswith("GIMME_APT_BUSY|") and not line.endswith("|no"):
            busy = [int(value) for value in line.split("|", 1)[1].split(",")]
        elif line.startswith("GIMME_HELPER|"):
            helper = line.split("|", 1)[1]
    missing = sorted(set(target.stack.packages) - set(resolution))
    if missing:
        raise RuntimeError("preflight omitted configured packages: " + ", ".join(missing))
    return target_stack_plan(name, target, resolution, package_manager_processes=busy,
                             privileged_helper=helper, sites=target_sites(state, name))


def _managed_database_issues(state: ControlState, deployment: DeploymentConfig) -> list[str]:
    binding = deployment.resources.database
    if binding is not None and isinstance(state.resources[binding], AWSRDSPostgresResource):
        return [
            f"database is bound to managed resource {binding}; runtime wiring of managed "
            "database credentials is not implemented yet"
        ]
    return []


def _recovery_schedule_runtime_issues(
    deployment: DeploymentConfig, target: TargetConfig
) -> list[str]:
    if (
        deployment.recovery is not None
        and deployment.recovery.cadence.kind != "manual"
        and "python3-boto3" not in target.stack.packages
    ):
        return ["recovery_schedule_runtime_missing"]
    return []


def _recovery_valkey_execution(
    name: str, state: ControlState, deployment: DeploymentConfig
) -> dict[str, object] | None:
    policy = deployment.recovery
    binding = deployment.resources.valkey
    if policy is None or not policy.valkey or binding is None:
        return None
    resource = state.resources[binding.resource]
    if isinstance(resource, ResourceConfig):
        return {
            "prefix": deployment.placement.cache_prefix,
            "host": "127.0.0.1",
            "port": 6379,
            "tls": False,
            "auth_mode": "none",
        }
    values, credentials, _probe, issues = _valkey_runtime(name, state, deployment)
    if issues or not credentials:
        return None
    return {
        "prefix": f"{{gimme:{name}}}:",
        "host": values["GIMME_VALKEY_HOST"],
        "port": int(values["GIMME_VALKEY_PORT"]),
        "tls": True,
        "auth_mode": "stored",
    }


def _resource_plan(name: str) -> dict[str, Any]:
    state, deployment, target, application = _context(name)
    secret_versions, secret_issues = _secret_plan(name, state, deployment)
    issues = (
        secret_issues
        + _dns_issues(deployment, target)
        + _managed_database_issues(state, deployment)
        + _recovery_schedule_runtime_issues(deployment, target)
        + _valkey_runtime(name, state, deployment)[3]
    )
    schedule = None
    if deployment.recovery is not None:
        destination_name = deployment.recovery.destination
        resource_provenance: dict[str, dict[str, str]] = {}
        for component, resource_name in (
            ("postgres", deployment.resources.database),
            (
                "valkey",
                None if deployment.resources.valkey is None
                else deployment.resources.valkey.resource,
            ),
        ):
            if resource_name is None or (
                component == "valkey" and not deployment.recovery.valkey
            ):
                continue
            resource = state.resources[resource_name]
            version = (
                resource.version if isinstance(resource, ResourceConfig)
                else resource.engine_version
            )
            resource_provenance[component] = {
                "name": resource_name,
                "provider": resource.provider,
                "kind": resource.kind,
                "version": version,
            }
        valkey_execution = _recovery_valkey_execution(name, state, deployment)
        if not deployment.recovery.valkey or valkey_execution is not None:
            authority = recovery_schedule_module.runner_authority(
                name, deployment, destination_name,
                state.backup_destinations[destination_name], resource_provenance,
                valkey_execution,
            )
            schedule = recovery_schedule_module.schedule_plan(authority)
    plan = deployment_resource_plan(
        name, deployment, target, application,
        missing_secrets=issues, secret_versions=secret_versions,
        valkey_contract=_contract_summary(name, state, deployment),
        recovery_schedule=schedule,
    )
    if issues:
        plan["readiness_issues"] = issues
        plan["plan_id"] = StateStore.digest({k: v for k, v in plan.items() if k != "plan_id"})
    return plan


def _release_plan(name: str, revision: str | None = None) -> dict[str, Any]:
    state, deployment, target, application = _context(name)
    _, secret_issues = _secret_plan(name, state, deployment)
    issues = secret_issues + _dns_issues(deployment, target) + _managed_database_issues(
        state, deployment
    ) + _valkey_runtime(name, state, deployment)[3]
    if issues:
        raise ValueError("deployment is not ready: " + "; ".join(issues))
    selected = revision or _revision(name)
    preflight = _run_deployment("gimme:preflight:runtimes", name, revision=selected,
                                timeout=60)
    processes, process_issues = _process_preflight(name, deployment, application)
    rendered = _run_deployment("deploy", name, revision=selected,
                               arguments=("--plan",), timeout=60)
    return deployment_release_plan(name, deployment, target, application, selected,
                                   rendered.output, {"declared": {
                                       key: value.model_dump(mode="json")
                                       for key, value in deployment.runtimes.items()
                                   },
                                                     "preflight": preflight.output},
                                   processes, process_issues)


def _process_preflight(
    name: str, deployment: DeploymentConfig, application: ApplicationConfig
) -> tuple[dict[str, object], list[str]]:
    managed = application.framework == "laravel" and (
        deployment.workers is not None or deployment.scheduler is not None
    )
    if not managed:
        return {"required": False, "observed": {}}, []
    result = _run_deployment("gimme:preflight:processes", name, timeout=60)
    observed: dict[str, str] = {}
    for raw in result.output.splitlines():
        line = raw.split("] ", 1)[-1].strip()
        if line.startswith("GIMME_") and "|" in line:
            key, value = line.split("|", 1)
            observed[key.removeprefix("GIMME_").lower()] = value
    issues = []
    if observed.get("process_helper") != "ready":
        issues.append("privileged process helper requires target bootstrap")
    if observed.get("pcntl") not in {"ready", "not_required"}:
        issues.append("PHP pcntl extension is required for managed workers")
    if observed.get("posix") not in {"ready", "not_required"}:
        issues.append("PHP posix extension is required for Horizon")
    return {"required": True, "observed": observed}, issues


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


def _recovery_schedule_status(
    name: str, observed_at: datetime | None = None,
) -> dict[str, object]:
    _state, deployment, _target, _application = _context(name)
    if deployment.recovery is None:
        raise ValueError(f"deployment {name} has no Recovery Policy bound")
    cadence = deployment.recovery.cadence
    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    logical_next = recovery_schedule_module.next_logical_slot(cadence, observed)
    effective_next = (
        None if logical_next is None
        else recovery_schedule_module.effective_execution(logical_next, name)
    )
    result: dict[str, object] = {
        "deployment": name,
        "cadence": cadence.model_dump(mode="json"),
        "logical_next_utc": None if logical_next is None else logical_next.isoformat(),
        "effective_next_utc": None if effective_next is None else effective_next.isoformat(),
        "timer_state": "disabled" if logical_next is None else "unavailable",
        "timer_enabled": False if logical_next is None else None,
        "timer_active": False if logical_next is None else None,
        "last_logical_slot": None,
        "started_at": None,
        "finished_at": None,
        "outcome": None if logical_next is None else "status_unavailable",
        "error_code": None if logical_next is None else "status_unavailable",
        "recovery_point_id": None,
        "last_verified_recovery_point_id": None,
        "retention_outcome": None,
        "retention_deleted": 0,
        "retention_remaining": 0,
    }
    if logical_next is None:
        return result
    try:
        observation = _run_deployment(
            "gimme:recovery:schedule-status", name, timeout=60
        )
        markers = []
        for raw in observation.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_RECOVERY_TIMER|"):
                markers.append(line.split("|"))
        if (
            len(markers) != 1 or len(markers[0]) != 3
            or markers[0][1] not in {"missing", "enabled", "disabled"}
            or markers[0][2] not in {"active", "inactive"}
        ):
            return result
        configured, activity = markers[0][1:]
        result.update({
            "timer_state": (
                "missing" if configured == "missing" else activity
            ),
            "timer_enabled": configured == "enabled",
            "timer_active": activity == "active",
            "outcome": None,
            "error_code": None,
        })
    except Exception:
        return result
    return result


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
    state = store.load()
    exists = name in state.provider_accounts
    if update != exists:
        message = "provider account already exists" if exists else "provider account missing"
        raise ValueError(message)
    validate_aws_account(definition, aws_secrets)
    proposed = _replace(state, "provider_accounts", name, definition)
    return exact_plan({
        "kind": "provider_account_update" if update else "provider_account_registration",
        "name": name,
        "current": (state.provider_accounts[name].model_dump(mode="json") if exists else None),
        "proposed": proposed.provider_accounts[name].model_dump(mode="json"),
        "identity_verified": True,
        "effects": ["replace local desired state only", "make no AWS changes"],
    })


@mcp.tool(annotations=READ)
@_journal_plan("register_provider_account", "name")
def plan_register_provider_account(name: Name, definition: AWSProviderAccount) -> dict[str, object]:
    """Verify both exact AWS roles and plan a Provider Account registration."""
    return _account_registration_plan(name, definition, update=False)


@mcp.tool(annotations=WRITE)
@_journal_apply("register_provider_account", "name")
def register_provider_account(name: Name, definition: AWSProviderAccount,
                              plan_id: PlanId) -> dict[str, object]:
    """Register one verified AWS Provider Account without storing credentials."""
    expected = _account_registration_plan(name, definition, update=False)
    _assert_plan(expected, plan_id)
    store.save(_replace(store.load(), "provider_accounts", name, definition))
    return {"changed": True, "provider_account": name}


@mcp.tool(annotations=READ)
@_journal_plan("update_provider_account", "name")
def plan_update_provider_account(name: Name, definition: AWSProviderAccount) -> dict[str, object]:
    """Reverify and plan an exact Provider Account policy update."""
    return _account_registration_plan(name, definition, update=True)


@mcp.tool(annotations=WRITE)
@_journal_apply("update_provider_account", "name")
def update_provider_account(name: Name, definition: AWSProviderAccount,
                            plan_id: PlanId) -> dict[str, object]:
    """Apply one reviewed Provider Account policy update."""
    expected = _account_registration_plan(name, definition, update=True)
    _assert_plan(expected, plan_id)
    store.save(_replace(store.load(), "provider_accounts", name, definition))
    return {"changed": True, "provider_account": name}


@mcp.tool(annotations=READ)
@_journal_plan("remove_provider_account", "name")
def plan_remove_provider_account(name: Name) -> dict[str, object]:
    """Plan local removal when no Secret Store or Resource references the account."""
    state = store.load()
    if name not in state.provider_accounts:
        raise KeyError("provider account is not registered")
    stores = sorted(store_name for store_name, value in state.secret_stores.items()
                    if isinstance(value, AWSSecretsManagerStore)
                    and value.provider_account == name)
    if stores:
        raise ValueError("provider account is still referenced by a secret store")
    return exact_plan({"kind": "provider_account_removal", "name": name,
                       "effects": ["remove local desired state only", "make no AWS changes"]})


@mcp.tool(annotations=WRITE)
@_journal_apply("remove_provider_account", "name")
def remove_provider_account(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Apply a reviewed local-only Provider Account removal."""
    expected = plan_remove_provider_account(name)
    _assert_plan(expected, plan_id)
    store.save(_delete(store.load(), "provider_accounts", name))
    return {"changed": True, "provider_account": name}


def _secret_store_registration_plan(name: str, definition: SecretStore,
                                    *, update: bool) -> dict[str, object]:
    if name == "local-sops":
        raise ValueError("the built-in local-sops store cannot be registered or updated")
    state = store.load()
    exists = name in state.secret_stores
    if update != exists:
        raise ValueError("secret store already exists" if exists else "secret store missing")
    if not isinstance(definition, AWSSecretsManagerStore):
        raise ValueError("only AWS Secrets Manager stores can be registered")
    account = state.provider_accounts.get(definition.provider_account)
    if account is None:
        raise ValueError("secret store references an unknown provider account")
    validate_aws_store(definition, aws_secrets)
    aws_secrets.verify_role(account, account.inspection_role_arn)
    proposed = _replace(state, "secret_stores", name, definition)
    return exact_plan({
        "kind": "secret_store_update" if update else "secret_store_registration",
        "name": name,
        "current": (state.secret_stores[name].model_dump(mode="json") if exists else None),
        "proposed": proposed.secret_stores[name].model_dump(mode="json"),
        "ownership_tag": f"gimme:secret-store={name}",
        "identity_verified": True,
        "effects": ["replace local desired state only", "make no AWS changes"],
    })


@mcp.tool(annotations=READ)
@_journal_plan("register_secret_store", "name")
def plan_register_secret_store(name: Name, definition: SecretStore) -> dict[str, object]:
    """Verify bounded store policy and plan an AWS Secret Store registration."""
    return _secret_store_registration_plan(name, definition, update=False)


@mcp.tool(annotations=WRITE)
@_journal_apply("register_secret_store", "name")
def register_secret_store(name: Name, definition: SecretStore,
                          plan_id: PlanId) -> dict[str, object]:
    """Register one reviewed Secret Store without listing or mutating AWS secrets."""
    expected = _secret_store_registration_plan(name, definition, update=False)
    _assert_plan(expected, plan_id)
    store.save(_replace(store.load(), "secret_stores", name, definition))
    return {"changed": True, "secret_store": name}


@mcp.tool(annotations=READ)
@_journal_plan("update_secret_store", "name")
def plan_update_secret_store(name: Name, definition: SecretStore) -> dict[str, object]:
    """Plan an exact bounded Secret Store policy update."""
    return _secret_store_registration_plan(name, definition, update=True)


@mcp.tool(annotations=WRITE)
@_journal_apply("update_secret_store", "name")
def update_secret_store(name: Name, definition: SecretStore,
                        plan_id: PlanId) -> dict[str, object]:
    """Apply one reviewed Secret Store policy update."""
    expected = _secret_store_registration_plan(name, definition, update=True)
    _assert_plan(expected, plan_id)
    store.save(_replace(store.load(), "secret_stores", name, definition))
    return {"changed": True, "secret_store": name}


@mcp.tool(annotations=READ)
@_journal_plan("remove_secret_store", "name")
def plan_remove_secret_store(name: Name) -> dict[str, object]:
    """Plan local Secret Store removal when no Deployment references it."""
    if name == "local-sops":
        raise ValueError("the built-in local-sops store cannot be removed")
    state = store.load()
    if name not in state.secret_stores:
        raise KeyError("secret store is not registered")
    if any(reference.store == name for deployment in state.deployments.values()
           for reference in deployment.secrets.values()):
        raise ValueError("secret store is still referenced by a deployment")
    return exact_plan({"kind": "secret_store_removal", "name": name,
                       "effects": ["remove local desired state only", "make no AWS changes"]})


@mcp.tool(annotations=WRITE)
@_journal_apply("remove_secret_store", "name")
def remove_secret_store(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Apply a reviewed local-only Secret Store removal."""
    expected = plan_remove_secret_store(name)
    _assert_plan(expected, plan_id)
    store.save(_delete(store.load(), "secret_stores", name))
    return {"changed": True, "secret_store": name}


def _backup_destination_credentials(
    state: ControlState, definition: S3BackupDestination
) -> tuple[list[dict[str, str]] | None, tuple[str, str] | None]:
    planned = recovery_module.plan_destination_credentials(state, store.secrets_path, definition)
    credentials = recovery_module.resolve_destination_credentials(
        state, store.secrets_path, definition, planned
    )
    return planned, credentials


def _backup_destination_registration_plan(
    name: str, definition: S3BackupDestination, *, update: bool
) -> dict[str, object]:
    """Diff the proposed destination locally. Never calls the destination: a plan tool
    must not touch remote state, and definition.endpoint is caller-supplied."""
    state = store.load()
    exists = name in state.backup_destinations
    if update != exists:
        message = (
            "backup destination already exists" if exists else "backup destination missing"
        )
        raise ValueError(message)
    proposed = _replace(state, "backup_destinations", name, definition)
    return exact_plan(
        {
            "kind": "backup_destination_update" if update else "backup_destination_registration",
            "name": name,
            "current": (
                state.backup_destinations[name].model_dump(mode="json") if exists else None
            ),
            "proposed": proposed.backup_destinations[name].model_dump(mode="json"),
            "preflight_verified": False,
            "preflight": "deferred to apply; plan performs no live destination calls",
            "effects": ["replace local desired state only", "make no destination changes"],
        }
    )


def _backup_destination_apply(
    name: str, definition: S3BackupDestination, plan_id: str, *, update: bool
) -> dict[str, object]:
    expected = _backup_destination_registration_plan(name, definition, update=update)
    _assert_plan(expected, plan_id)
    state = store.load()
    _, credentials = _backup_destination_credentials(state, definition)
    preflight_backup_destination(definition, credentials, backup_s3)
    store.save(_replace(store.load(), "backup_destinations", name, definition))
    return {"changed": True, "backup_destination": name}


@mcp.tool(annotations=READ)
@_journal_plan("register_backup_destination", "name")
def plan_register_backup_destination(
    name: Name, definition: S3BackupDestination
) -> dict[str, object]:
    """Diff a proposed Backup Destination registration; preflight runs at apply."""
    return _backup_destination_registration_plan(name, definition, update=False)


@mcp.tool(annotations=WRITE)
@_journal_apply("register_backup_destination", "name")
def register_backup_destination(
    name: Name, definition: S3BackupDestination, plan_id: PlanId
) -> dict[str, object]:
    """Preflight-verify and register one Backup Destination without storing credentials."""
    return _backup_destination_apply(name, definition, plan_id, update=False)


@mcp.tool(annotations=READ)
@_journal_plan("update_backup_destination", "name")
def plan_update_backup_destination(
    name: Name, definition: S3BackupDestination
) -> dict[str, object]:
    """Diff a proposed Backup Destination policy update; preflight runs at apply."""
    return _backup_destination_registration_plan(name, definition, update=True)


@mcp.tool(annotations=WRITE)
@_journal_apply("update_backup_destination", "name")
def update_backup_destination(
    name: Name, definition: S3BackupDestination, plan_id: PlanId
) -> dict[str, object]:
    """Preflight-verify and apply one reviewed Backup Destination policy update."""
    return _backup_destination_apply(name, definition, plan_id, update=True)


@mcp.tool(annotations=READ)
@_journal_plan("remove_backup_destination", "name")
def plan_remove_backup_destination(name: Name) -> dict[str, object]:
    """Plan local Backup Destination removal when no Deployment references it."""
    state = store.load()
    if name not in state.backup_destinations:
        raise KeyError("backup destination is not registered")
    if any(
        deployment.recovery is not None and deployment.recovery.destination == name
        for deployment in state.deployments.values()
    ):
        raise ValueError("backup destination is still referenced by a deployment")
    return exact_plan(
        {
            "kind": "backup_destination_removal",
            "name": name,
            "effects": ["remove local desired state only", "make no destination changes"],
        }
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("remove_backup_destination", "name")
def remove_backup_destination(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Apply a reviewed local-only Backup Destination removal."""
    expected = plan_remove_backup_destination(name)
    _assert_plan(expected, plan_id)
    store.save(_delete(store.load(), "backup_destinations", name))
    return {"changed": True, "backup_destination": name}


@mcp.tool(annotations=READ)
def list_backup_destinations() -> dict[str, object]:
    """List registered S3-compatible Backup Destinations without credentials."""
    return {"backup_destinations": store.load().model_dump(mode="json")["backup_destinations"]}


def _recovery_context(
    name: str,
) -> tuple[ControlState, DeploymentConfig, str, S3BackupDestination]:
    state, deployment, _target, _application = _context(name)
    if deployment.recovery is None:
        raise ValueError(f"deployment {name} has no Recovery Policy bound")
    if _managed_database_issues(state, deployment):
        raise ValueError(
            f"deployment {name} database is a managed resource; "
            "Recovery Points support target-local PostgreSQL only"
        )
    destination_name = deployment.recovery.destination
    destination = state.backup_destinations[destination_name]
    return state, deployment, destination_name, destination


@mcp.tool(annotations=READ)
@_journal_plan("create_recovery_point", "name")
def plan_create_recovery_point(name: Name, request_id: RequestId) -> dict[str, object]:
    """Plan one on-demand PostgreSQL Recovery Point for a recovery-bound deployment."""
    _state, deployment, destination_name, destination = _recovery_context(name)
    point_id = recovery_module.recovery_point_id(name, destination_name, request_id)
    return recovery_point_creation_plan(
        name, deployment, destination_name, destination, request_id, point_id
    )


@mcp.tool(annotations=WRITE)
@_journal_apply("create_recovery_point", "name")
def create_recovery_point(name: Name, request_id: RequestId, plan_id: PlanId) -> dict[str, object]:
    """Apply a reviewed on-demand Recovery Point: dump, upload, verify, and publish."""
    state, deployment, destination_name, destination = _recovery_context(name)
    point_id = recovery_module.recovery_point_id(name, destination_name, request_id)
    expected = recovery_point_creation_plan(
        name, deployment, destination_name, destination, request_id, point_id
    )
    _assert_plan(expected, plan_id)
    _, credentials = _backup_destination_credentials(state, destination)
    with _deployment_resource_lock(name):
        existing = recovery_module.find_recovery_point(
            destination_name, destination, credentials, backup_s3, name, point_id
        )
        if existing is not None:
            return {
                "changed": False,
                "recovery_point": recovery_module.public_recovery_point(existing),
                "retention": recovery_module.enforce_recovery_retention(
                    destination_name, destination, credentials, backup_s3, name,
                    deployment.recovery.retain_last, point_id,
                ),
            }
        database_name = deployment.resources.database
        database = state.resources[database_name] if database_name is not None else None
        if not isinstance(database, ResourceConfig):
            raise RecoveryError("recovery_database_provenance_invalid")
        with tempfile.TemporaryDirectory(prefix="gimme-recovery-") as directory:
            local_path = Path(directory) / "postgres.dump"
            valkey_path = Path(directory) / "valkey.archive"
            binding = deployment.resources.valkey
            valkey_resource_name = binding.resource if binding is not None else None
            valkey_resource = (
                state.resources[valkey_resource_name]
                if valkey_resource_name is not None else None
            )
            admin_credential: dict[str, str] = {}
            valkey_version = ""
            if deployment.recovery.valkey:
                if not isinstance(
                    valkey_resource, (ResourceConfig, AWSElastiCacheValkeyResource)
                ) or valkey_resource_name is None:
                    raise RecoveryError("recovery_valkey_provenance_invalid")
                valkey_version = _valkey_resource_version(valkey_resource)
                admin_credential = _valkey_capture_credential(
                    state, valkey_resource_name, valkey_resource
                )
            with _recovery_maintenance_window(
                name, request_id, deployment.recovery.quiesce_wait_seconds,
                enabled=deployment.recovery.valkey,
            ) as restore_runtime:
                with protected_secret_file(admin_credential) as valkey_secret:
                    postgres_dump = _capture_postgres_dump(
                        name, local_path, database.version
                    )
                    if deployment.recovery.valkey:
                        valkey_dump = _capture_valkey_dump(
                            name, valkey_path, valkey_version, valkey_secret
                        )
                dumps = [postgres_dump]
                if deployment.recovery.valkey:
                    dumps.append(valkey_dump)
                manifest = recovery_module.create_recovery_point(
                    destination_name, destination, credentials, backup_s3, name, point_id, dumps,
                    before_publish=restore_runtime if deployment.recovery.valkey else None,
                )
        return {
            "changed": True,
            "recovery_point": recovery_module.public_recovery_point(manifest),
            "retention": recovery_module.enforce_recovery_retention(
                destination_name, destination, credentials, backup_s3, name,
                deployment.recovery.retain_last, point_id,
            ),
        }


@mcp.tool(annotations=READ)
def list_recovery_points(name: Name) -> dict[str, object]:
    """List one deployment's Recovery Points from destination-authoritative inventory."""
    state, _deployment, destination_name, destination = _recovery_context(name)
    _, credentials = _backup_destination_credentials(state, destination)
    inventory = recovery_module.list_recovery_points(
        destination_name, destination, credentials, backup_s3, name
    )
    return {
        **inventory,
        "recovery_points": [
            recovery_module.public_recovery_point(item)
            for item in inventory["recovery_points"]  # type: ignore[union-attr]
        ],
    }


def _restore_records(name: str) -> list[dict[str, object]]:
    state, _deployment, _destination_name, destination = _recovery_context(name)
    _, credentials = _backup_destination_credentials(state, destination)
    return recovery_module.list_restore_records(
        destination, credentials, backup_s3, name
    )


@mcp.tool(annotations=READ)
def list_restores(name: Name) -> dict[str, object]:
    """List destination-authoritative, secret-safe Restore records newest first."""
    return {"deployment": name, "restores": _restore_records(name)}


@mcp.resource("gimme://deployments/{name}/restores/{request_id}")
def restore_record_resource(name: str, request_id: str) -> dict[str, object]:
    """Read the latest public Restore state for one request identity."""
    state, _deployment, _destination_name, destination = _recovery_context(name)
    _, credentials = _backup_destination_credentials(state, destination)
    return recovery_module.load_restore_record(
        destination, credentials, backup_s3, name, request_id
    )


def _normalize_restore_components(
    manifest_components: list[dict[str, object]],
    requested: list[str] | None,
) -> list[str]:
    available = [str(component.get("kind")) for component in manifest_components]
    if (
        not available
        or len(available) != len(set(available))
        or not set(available) <= {"postgres", "valkey"}
    ):
        raise RecoveryError("restore_component_manifest_invalid")
    if requested is None:
        return available
    if (
        not 1 <= len(requested) <= 2
        or len(requested) != len(set(requested))
        or not set(requested) <= {"postgres", "valkey"}
    ):
        raise RecoveryError("restore_component_selection_invalid")
    if not set(requested) <= set(available):
        raise RecoveryError("restore_component_missing")
    return [component for component in available if component in requested]


def _deployment_restore_plan(
    name: str, recovery_point_id: str, request_id: str,
    components: list[str] | None = None,
) -> dict[str, object]:
    state, deployment, destination_name, destination = _recovery_context(name)
    _, credentials = _backup_destination_credentials(state, destination)
    manifest = recovery_module.find_recovery_point(
        destination_name, destination, credentials, backup_s3, name, recovery_point_id
    )
    if manifest is None:
        raise RecoveryError("restore_source_missing")
    manifest_components = cast(list[dict[str, object]], manifest["components"])
    selected_components = _normalize_restore_components(
        manifest_components, components
    )
    available_components = [str(item["kind"]) for item in manifest_components]
    untouched_components = [
        component for component in available_components
        if component not in selected_components
    ]
    valkey_destination: dict[str, str] | None = None
    if "valkey" in selected_components:
        binding = deployment.resources.valkey
        valkey_resource_name = binding.resource if binding is not None else None
        valkey_resource = (
            state.resources.get(valkey_resource_name)
            if valkey_resource_name is not None else None
        )
        if isinstance(valkey_resource, ResourceConfig) and valkey_resource.kind == "valkey":
            valkey_destination = {
                "resource": str(valkey_resource_name), "provider": "target_local",
                "kind": "valkey", "version": valkey_resource.version,
            }
        elif isinstance(valkey_resource, AWSElastiCacheValkeyResource):
            valkey_destination = {
                "resource": str(valkey_resource_name),
                "provider": "aws_elasticache_valkey", "kind": "valkey",
                "version": valkey_resource.engine_version,
            }
    resource_name = deployment.resources.database
    resource = state.resources[resource_name] if resource_name is not None else None
    if not isinstance(resource, ResourceConfig) or resource.kind != "postgres":
        raise RecoveryError("restore_destination_incompatible")
    states: set[str] = set()
    capacity_ready = True
    if "postgres" in selected_components:
        postgres_source = next(
            item for item in manifest_components if item["kind"] == "postgres"
        )
        source_bytes = int(postgres_source["bytes"])
        observation = _run_deployment(
            "gimme:recovery:inspect-postgres", name,
            restore_source_bytes=source_bytes, timeout=60,
        )
        states = _bounded_marker_values(
            observation.output, "GIMME_POSTGRES_RESTORE_PREFLIGHT|",
            {"empty", "nonempty"},
        )
        if len(states) != 1 or not states <= {"empty", "nonempty"}:
            raise RecoveryError("restore_destination_inspection_failed")
        capacity = _bounded_marker_values(
            observation.output, "GIMME_POSTGRES_RESTORE_CAPACITY|",
            {"ready", "insufficient"},
        )
        if len(capacity) != 1:
            raise RecoveryError("restore_destination_inspection_failed")
        required_bytes = source_bytes * 2 + 64 * 1024 * 1024
        capacity_ready = (
            capacity == {"ready"}
            and shutil.disk_usage(tempfile.gettempdir()).free >= required_bytes
        )
    try:
        existing_restore = recovery_module.load_restore_record(
            destination, credentials, backup_s3, name, request_id
        )
    except RecoveryError as exc:
        if str(exc) != "restore_record_missing":
            raise
        existing_restore = None
    expected_destination = {
        "resource": resource_name, "provider": "target_local",
        "kind": "postgres", "version": resource.version,
    }
    if selected_components == ["valkey"] and valkey_destination is not None:
        expected_destination = valkey_destination
    request_conflict = existing_restore is not None and (
        existing_restore["source_recovery_point_id"] != recovery_point_id
        or existing_restore["destination"] != expected_destination
        or existing_restore["selected_components"] != selected_components
        or existing_restore["untouched_components"] != untouched_components
        or existing_restore["partial"] != bool(untouched_components)
        or existing_restore["safety_recovery_point_id"] not in {
            None,
            recovery_module.safety_recovery_point_id(
                name, destination_name, request_id
            ),
        }
    )
    # Ambiguous or uninspected selected destinations require Safety capture.
    observed_empty = "postgres" in selected_components and states == {"empty"}
    observed_safety_components = [
        component for component in selected_components
        if component == "valkey" or component == "postgres" and not observed_empty
    ]
    safety_components = (
        observed_safety_components if existing_restore is None
        else cast(list[str], existing_restore["safety_components"])
    )
    original_empty = (
        "postgres" in selected_components and "postgres" not in safety_components
    )
    destination_changed = (
        existing_restore is not None
        and existing_restore["state"] in {
            "started", "maintenance_entered", "safety_failed",
            "artifact_failed", "shadow_failed",
        }
        and original_empty != observed_empty
    )
    selected_destinations = [
        *([{
            "resource": resource_name, "provider": "target_local",
            "kind": "postgres", "version": resource.version,
            "empty": original_empty,
        }] if "postgres" in selected_components else []),
        *(
            [valkey_destination]
            if "valkey" in selected_components and valkey_destination is not None
            else []
        ),
    ]
    request_fingerprint = StateStore.digest({
        "kind": "deployment_restore_request",
        "deployment": name,
        "source_manifest": manifest,
        "selected_components": selected_components,
        "untouched_components": untouched_components,
        "destinations": selected_destinations,
        "recovery_policy": deployment.recovery.model_dump(mode="json"),
        "placement": deployment.placement.model_dump(mode="json"),
        "execution_fingerprint": execution_fingerprint(),
    })
    record_destinations = [
        {key: value for key, value in item.items() if key != "empty"}
        for item in selected_destinations
    ]
    if existing_restore is not None and (
        existing_restore.get("request_fingerprint") not in {None, request_fingerprint}
        or existing_restore.get("request_fingerprint") is not None
        and existing_restore.get("destinations") != record_destinations
    ):
        request_conflict = True
    return deployment_restore_plan(
        name, recovery_point_id, request_id,
        manifest_components,
        resource_name, resource.version,
        original_empty,
        selected_components,
        safety_components=safety_components,
        capacity_ready=capacity_ready,
        valkey_destination=valkey_destination,
        request_fingerprint=request_fingerprint,
        restore_state=None if existing_restore is None else str(existing_restore["state"]),
        request_conflict=request_conflict,
        destination_changed=destination_changed,
    )


@mcp.tool(annotations=READ)
@_journal_plan("restore_deployment", "name")
def plan_restore_deployment(
    name: Name, recovery_point_id: RecoveryPointId, request_id: RequestId,
    components: RestoreComponents | None = None,
) -> dict[str, object]:
    """Plan full Restore by default or an explicit bounded component subset."""
    return _deployment_restore_plan(name, recovery_point_id, request_id, components)


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
    with _deployment_resource_lock(name):
        expected = _deployment_restore_plan(
            name, recovery_point_id, request_id, components
        )
        _assert_plan(expected, plan_id)
        if not expected["ready"]:
            raise RecoveryError("restore_not_ready")
        if confirmation != expected["confirmation"]:
            raise ValueError("restore confirmation is invalid")
        state, deployment, destination_name, destination = _recovery_context(name)
        _, credentials = _backup_destination_credentials(state, destination)
        manifest = recovery_module.find_recovery_point(
            destination_name, destination, credentials, backup_s3, name,
            recovery_point_id,
        )
        if manifest is None:
            raise RecoveryError("restore_source_missing")
        selected_components = cast(list[str], expected["selected_components"])
        safety_components = cast(list[str], expected["safety_components"])
        untouched_components = cast(list[str], expected["untouched_components"])
        source_components = {
            str(item["kind"]): item
            for item in manifest["components"]  # type: ignore[union-attr]
            if item["kind"] in selected_components
        }
        if set(source_components) != set(selected_components):
            raise RecoveryError("restore_component_missing")
        resource_name = deployment.resources.database
        resource = state.resources[resource_name] if resource_name is not None else None
        if not isinstance(resource, ResourceConfig) or resource.kind != "postgres":
            raise RecoveryError("restore_destination_incompatible")
        existing = (
            recovery_module.load_restore_record(
                destination, credentials, backup_s3, name, request_id
            )
            if expected["restore_state"] is not None else None
        )
        safety_id = (
            recovery_module.safety_recovery_point_id(
                name, destination_name, request_id
            ) if safety_components else None
        )
        if existing is not None:
            safety_id = cast(str | None, existing["safety_recovery_point_id"])
        destinations = cast(list[dict[str, object]], expected["destinations"])
        if len(destinations) != len(selected_components):
            raise RecoveryError("restore_destination_incompatible")
        primary_destination = destinations[0]
        record_destinations = [
            {key: value for key, value in item.items() if key != "empty"}
            for item in destinations
        ]
        identity = {
            "source_recovery_point_id": recovery_point_id,
            "destination_provider": str(primary_destination["provider"]),
            "destination_resource": str(primary_destination["resource"]),
            "destination_kind": str(primary_destination["kind"]),
            "destination_version": str(primary_destination["version"]),
            "safety_recovery_point_id": safety_id,
            "selected_components": selected_components,
            "untouched_components": untouched_components,
            "partial": bool(expected["partial"]),
            "destinations": (
                record_destinations if existing is None
                else cast(list[dict[str, object]], existing["destinations"])
            ),
            "request_fingerprint": (
                str(expected["request_fingerprint"]) if existing is None
                else cast(str | None, existing["request_fingerprint"])
            ),
            "safety_components": safety_components,
        }
        current = None if existing is None else str(existing["state"])
        changed = False

        def advance(next_state: str) -> None:
            nonlocal changed, current
            recovery_module.append_restore_event(
                destination, credentials, backup_s3, name, request_id, next_state,
                **identity,
            )
            current = next_state
            changed = True

        if current is None:
            advance("started")
        resume_failure = current if current in {"artifact_failed", "shadow_failed"} else None
        if current in {"started", "safety_failed", "artifact_failed", "shadow_failed"}:
            try:
                _run_deployment(
                    "gimme:recovery:maintenance", name,
                    recovery_action="enter", recovery_request_id=request_id,
                    recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                    timeout=900,
                )
            except Exception:
                raise RecoveryError("restore_maintenance_failed") from None
            if resume_failure == "artifact_failed":
                advance("safety_not_required" if safety_id is None else "safety_verified")
            elif resume_failure == "shadow_failed":
                advance("artifact_verified")
            else:
                advance("maintenance_entered")
        if current == "maintenance_entered":
            if safety_id is None:
                advance("safety_not_required")
            else:
                try:
                    _capture_restore_safety(
                        name, request_id, safety_id, safety_components,
                        state, deployment, destination_name, destination, credentials,
                    )
                except Exception:
                    runtime_restored = True
                    try:
                        _run_deployment(
                            "gimme:recovery:maintenance", name,
                            recovery_action="exit", recovery_request_id=request_id,
                            recovery_quiesce_wait=(
                                deployment.recovery.quiesce_wait_seconds
                            ),
                            timeout=900,
                        )
                    except Exception:
                        runtime_restored = False
                    advance("safety_failed")
                    raise RecoveryError(
                        "safety_failed" if runtime_restored
                        else "recovery_runtime_restore_failed"
                    ) from None
                advance("safety_verified")
        resume_valkey_from_shadow = (
            current == "shadow_verified" and "valkey" in selected_components
        )
        with tempfile.TemporaryDirectory(prefix="gimme-restore-source-") as directory:
            local_sources = {
                "postgres": Path(directory) / "postgres.dump",
                "valkey": Path(directory) / "valkey.archive",
            }
            try:
                if current in {
                    "safety_verified", "safety_not_required", "artifact_verified"
                }:
                    for selected_kind in selected_components:
                        source_components[selected_kind] = (
                            recovery_module.materialize_recovery_component(
                                destination_name, destination, credentials, backup_s3,
                                name, recovery_point_id, selected_kind,
                                local_sources[selected_kind],
                            )
                        )
                elif resume_valkey_from_shadow:
                    # Valkey mutation has no finer-grained authoritative transition: a
                    # retry must clear and replay the complete prefix before any
                    # PostgreSQL swap. The per-call operation directory is ephemeral,
                    # so rematerialize the exact bound archive for that replay.
                    source_components["valkey"] = (
                        recovery_module.materialize_recovery_component(
                            destination_name, destination, credentials, backup_s3,
                            name, recovery_point_id, "valkey", local_sources["valkey"],
                        )
                    )
            except Exception as exc:
                if resume_valkey_from_shadow:
                    if isinstance(exc, RecoveryError):
                        raise RecoveryError(str(exc)) from None
                    raise RecoveryError("restore_artifact_failed") from None
                advance("artifact_failed")
                try:
                    _run_deployment(
                        "gimme:recovery:maintenance", name,
                        recovery_action="exit", recovery_request_id=request_id,
                        recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                        timeout=900,
                    )
                except Exception:
                    raise RecoveryError("recovery_runtime_restore_failed") from None
                if isinstance(exc, RecoveryError):
                    raise RecoveryError(str(exc)) from None
                raise RecoveryError("restore_artifact_failed") from None
            if current in {"safety_verified", "safety_not_required"}:
                advance("artifact_verified")
            if current == "artifact_verified":
                if "postgres" in selected_components:
                    postgres_component = source_components["postgres"]
                    try:
                        _run_deployment(
                            "gimme:recovery:postgres", name,
                            backup_local_path=local_sources["postgres"],
                            postgres_restore_action="prepare",
                            postgres_restore_request_id=request_id,
                            postgres_restore_sha256=str(postgres_component["sha256"]),
                            postgres_restore_bytes=int(postgres_component["bytes"]),
                            timeout=3600,
                        )
                    except Exception:
                        advance("shadow_failed")
                        try:
                            _run_deployment(
                                "gimme:recovery:maintenance", name,
                                recovery_action="exit", recovery_request_id=request_id,
                                recovery_quiesce_wait=(
                                    deployment.recovery.quiesce_wait_seconds
                                ),
                                timeout=900,
                            )
                        except Exception:
                            raise RecoveryError(
                                "recovery_runtime_restore_failed"
                            ) from None
                        raise RecoveryError("restore_shadow_prepare_failed") from None
                if "valkey" in selected_components:
                    _restore_valkey_component(
                        name, request_id, source_components["valkey"],
                        local_sources["valkey"], state, deployment,
                    )
                advance("shadow_verified")
            elif resume_valkey_from_shadow:
                _restore_valkey_component(
                    name, request_id, source_components["valkey"],
                    local_sources["valkey"], state, deployment,
                )
        if current == "shadow_verified":
            if "postgres" in selected_components:
                postgres_component = source_components["postgres"]
                try:
                    _run_deployment(
                        "gimme:recovery:postgres", name,
                        postgres_restore_action="swap",
                        postgres_restore_request_id=request_id,
                        postgres_restore_sha256=str(postgres_component["sha256"]),
                        postgres_restore_bytes=int(postgres_component["bytes"]),
                        timeout=300,
                    )
                except Exception:
                    raise RecoveryError("restore_swap_failed") from None
            advance("data_replaced")
        return {
            "changed": changed,
            "deployment": name,
            "request_id": request_id,
            "state": current,
            "recovery_required": current != "completed",
        }


def _restore_verification_plan(name: str, request_id: str) -> dict[str, object]:
    state, deployment, _destination_name, destination = _recovery_context(name)
    _, credentials = _backup_destination_credentials(state, destination)
    restore = recovery_module.load_restore_record(
        destination, credentials, backup_s3, name, request_id
    )
    observed: list[dict[str, object]] = []
    for kind in cast(list[str], restore["selected_components"]):
        resource_name = (
            deployment.resources.database if kind == "postgres"
            else deployment.resources.valkey.resource
            if deployment.resources.valkey is not None else None
        )
        resource = state.resources.get(resource_name) if resource_name else None
        if (
            kind == "postgres" and isinstance(resource, ResourceConfig)
            and resource.kind == "postgres"
        ):
            observed.append({
                "resource": resource_name, "provider": "target_local",
                "kind": "postgres", "version": resource.version,
            })
        elif (
            kind == "valkey" and isinstance(resource, ResourceConfig)
            and resource.kind == "valkey"
        ):
            observed.append({
                "resource": resource_name, "provider": "target_local",
                "kind": "valkey", "version": resource.version,
            })
        elif kind == "valkey" and isinstance(resource, AWSElastiCacheValkeyResource):
            observed.append({
                "resource": resource_name, "provider": "aws_elasticache_valkey",
                "kind": "valkey", "version": resource.engine_version,
            })
    recorded = cast(list[dict[str, object]], restore["destinations"])
    if restore["request_fingerprint"] is None:
        observed = observed[:len(recorded)]
    return restore_verification_plan(
        name, request_id, restore, identity_conflict=observed != recorded
    )


@mcp.tool(annotations=READ)
@_journal_plan("verify_restore", "name")
def plan_verify_restore(name: Name, request_id: RequestId) -> dict[str, object]:
    """Plan private application verification and return from Restore maintenance."""
    return _restore_verification_plan(name, request_id)


@mcp.tool(annotations=WRITE)
@_journal_apply("verify_restore", "name")
def apply_verify_restore(
    name: Name, request_id: RequestId, plan_id: PlanId,
) -> dict[str, object]:
    """Verify restored data privately, clean up, and restore normal routing."""
    with _deployment_resource_lock(name):
        expected = _restore_verification_plan(name, request_id)
        _assert_plan(expected, plan_id)
        if not expected["ready"]:
            raise RecoveryError("restore_verification_not_ready")
        state, deployment, destination_name, destination = _recovery_context(name)
        _, credentials = _backup_destination_credentials(state, destination)
        restore = recovery_module.load_restore_record(
            destination, credentials, backup_s3, name, request_id
        )
        restore_destination = cast(dict[str, object], restore["destination"])
        identity = {
            "source_recovery_point_id": str(restore["source_recovery_point_id"]),
            "destination_provider": str(restore_destination["provider"]),
            "destination_resource": str(restore_destination["resource"]),
            "destination_kind": str(restore_destination["kind"]),
            "destination_version": str(restore_destination["version"]),
            "safety_recovery_point_id": cast(
                str | None, restore["safety_recovery_point_id"]
            ),
            "selected_components": cast(list[str], restore["selected_components"]),
            "untouched_components": cast(list[str], restore["untouched_components"]),
            "partial": bool(restore["partial"]),
            "destinations": cast(
                list[dict[str, object]], restore["destinations"]
            ),
            "request_fingerprint": cast(
                str | None, restore["request_fingerprint"]
            ),
            "safety_components": cast(
                list[str], restore["safety_components"]
            ),
        }
        current = str(restore["state"])
        changed = False

        def advance(next_state: str) -> None:
            nonlocal changed, current
            recovery_module.append_restore_event(
                destination, credentials, backup_s3, name, request_id, next_state,
                **identity,
            )
            current = next_state
            changed = True

        def verify_runtime() -> None:
            try:
                _run_deployment(
                    "gimme:recovery:maintenance", name,
                    recovery_action="resume", recovery_request_id=request_id,
                    recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                    timeout=900,
                )
                result = _run_deployment(
                    "gimme:recovery:verify-application", name, timeout=900
                )
                markers = _bounded_marker_values(
                    result.output, "GIMME_RESTORE_VERIFY|", {"ready"}
                )
                if markers != {"ready"}:
                    raise RecoveryError("restore_verification_failed")
            except Exception:
                quiesce_failed = False
                try:
                    _run_deployment(
                        "gimme:recovery:maintenance", name,
                        recovery_action="quiesce", recovery_request_id=request_id,
                        recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                        timeout=900,
                    )
                except Exception:
                    quiesce_failed = True
                advance("verification_failed")
                raise RecoveryError(
                    "restore_quiesce_failed"
                    if quiesce_failed else "restore_verification_failed"
                ) from None

        if current in {"data_replaced", "verification_failed"}:
            verify_runtime()
            advance("verification_succeeded")
        elif current == "verification_succeeded":
            verify_runtime()
        if current == "verification_succeeded":
            if "postgres" in cast(list[str], restore["selected_components"]):
                manifest = recovery_module.find_recovery_point(
                    destination_name, destination, credentials, backup_s3, name,
                    str(restore["source_recovery_point_id"]),
                )
                if manifest is None:
                    raise RecoveryError("restore_source_missing")
                component = next(
                    (
                        item for item in manifest["components"]  # type: ignore[union-attr]
                        if item["kind"] == "postgres"
                    ),
                    None,
                )
                if component is None:
                    raise RecoveryError("restore_component_missing")
                try:
                    _run_deployment(
                        "gimme:recovery:postgres", name,
                        postgres_restore_action="cleanup",
                        postgres_restore_request_id=request_id,
                        postgres_restore_sha256=str(component["sha256"]),
                        postgres_restore_bytes=int(component["bytes"]),
                        timeout=300,
                    )
                except Exception:
                    raise RecoveryError("restore_cleanup_failed") from None
            advance("cleanup_completed")
        if current == "cleanup_completed":
            verify_runtime()
            try:
                _run_deployment(
                    "gimme:recovery:maintenance", name,
                    recovery_action="exit", recovery_request_id=request_id,
                    recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                    timeout=900,
                )
            except Exception:
                raise RecoveryError("restore_maintenance_exit_failed") from None
            advance("completed")
        return {
            "changed": changed,
            "deployment": name,
            "request_id": request_id,
            "state": current,
            "recovery_required": current != "completed",
        }


def _recovery_point_deletion_plan(
    name: str, point_id: str, *, allow_partial: bool = False
) -> dict[str, object]:
    state, _deployment, destination_name, destination = _recovery_context(name)
    _, credentials = _backup_destination_credentials(state, destination)
    inventory = recovery_module.list_recovery_points(
        destination_name, destination, credentials, backup_s3, name
    )
    selected = next(
        (
            item for item in inventory["recovery_points"]  # type: ignore[union-attr]
            if item["recovery_point_id"] == point_id
        ),
        None,
    )
    if selected is None:
        raise RecoveryError("recovery_manifest_missing")
    if selected["state"] == "deletion_failed" and not allow_partial:
        raise RecoveryError("recovery_point_deletion_failed")
    targets = recovery_module.recovery_point_deletion_targets(
        destination_name, destination, credentials, backup_s3, name, point_id
    )
    effective_verified = sum(
        item["state"] == "verified"
        and not recovery_module.safety_recovery_point_protected(
            destination, credentials, backup_s3, name, item
        )
        for item in inventory["recovery_points"]  # type: ignore[union-attr]
    ) + (1 if selected["state"] == "deletion_failed" else 0)
    safety_protected = recovery_module.safety_recovery_point_protected(
        destination, credentials, backup_s3, name, targets["manifest"]  # type: ignore[arg-type]
    )
    restore_protected = recovery_module.recovery_point_source_protected(
        destination, credentials, backup_s3, name, point_id
    )
    normalized_inventory = sorted(
        (
            str(item["recovery_point_id"]),
            "verified"
            if item is selected and item["state"] == "deletion_failed"
            else str(item["state"]),
        )
        for item in inventory["recovery_points"]  # type: ignore[union-attr]
    )
    inventory_fingerprint = hashlib.sha256(
        json.dumps(normalized_inventory, separators=(",", ":")).encode()
    ).hexdigest()
    manifest_fingerprint = hashlib.sha256(
        json.dumps(
            {
                "manifest": targets["manifest"],
                "version": targets["manifest_version_id"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return recovery_point_deletion_plan(
        name,
        destination_name,
        point_id,
        components=int(targets["components"]),
        bytes=int(targets["bytes"]),
        final_verified_point=effective_verified == 1,
        safety_protected=safety_protected,
        restore_protected=restore_protected,
        inventory_fingerprint=inventory_fingerprint,
        manifest_fingerprint=manifest_fingerprint,
        state="verified",
    )


@mcp.tool(annotations=READ)
@_journal_plan("delete_recovery_point", "name")
def plan_delete_recovery_point(
    name: Name, recovery_point_id: RecoveryPointId
) -> dict[str, object]:
    """Plan deletion of one manifest-owned Recovery Point without exposing S3 identities."""
    return _recovery_point_deletion_plan(name, recovery_point_id)


def _matching_delete_retry(name: str, plan_id: str) -> bool:
    events = _journal().list(limit=200, operation="delete_recovery_point", subject=name)
    outcomes = [
        event for event in events
        if event.phase == "outcome" and event.plan_id == plan_id
        and event.status in {"failed", "succeeded"}
    ]
    return bool(outcomes) and _journal().plan_correlation(
        plan_id, "delete_recovery_point"
    ) is not None


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
    retry = _matching_delete_retry(name, plan_id)
    with _deployment_resource_lock(name):
        try:
            expected = _recovery_point_deletion_plan(
                name, recovery_point_id, allow_partial=retry
            )
        except RecoveryError as exc:
            if retry and str(exc) == "recovery_manifest_missing":
                return {
                    "changed": False,
                    "recovery_point_id": recovery_point_id,
                    "state": "deleted",
                }
            raise
        _assert_plan(expected, plan_id)
        if expected["safety_protected"]:
            raise RecoveryError("recovery_point_safety_protected")
        if expected["restore_protected"]:
            raise RecoveryError("recovery_point_restore_protected")
        if confirmation != expected["confirmation"]:
            raise ValueError("recovery point deletion confirmation is invalid")
        required_last = expected["last_recovery_point_confirmation"]
        if required_last is not None and last_recovery_point_confirmation != required_last:
            raise ValueError("last Recovery Point deletion confirmation is invalid")
        state, _deployment, destination_name, destination = _recovery_context(name)
        _, credentials = _backup_destination_credentials(state, destination)
        result = recovery_module.delete_recovery_point_versions(
            destination_name, destination, credentials, backup_s3, name, recovery_point_id
        )
    return {"changed": True, **result}


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
    network = state.aws_networks.get(resource.aws_network)
    if network is not None and network.region.startswith(("us-gov-", "cn-")):
        raise ResourceError("aws_rds_tls_region_unsupported")


def _refuse_unavailable_node_type(
    state: ControlState, resource: AWSElastiCacheValkeyResource
) -> None:
    # The one AWS read registration makes: a node type the account cannot buy in the region
    # would only fail later, at create.
    network = state.aws_networks[resource.aws_network]
    options = elasticache_valkey.live_options(
        state.provider_accounts[network.provider_account], network
    )
    if resource.node_type not in options.node_types:
        raise ResourceError("aws_elasticache_node_type_unavailable")


def _managed_resource(name: str) -> tuple[ControlState, AWSRDSPostgresResource]:
    state = store.load()
    resource = state.resources.get(name)
    if resource is None:
        raise KeyError(f"resource '{name}' is not registered")
    if not isinstance(resource, AWSRDSPostgresResource):
        raise ValueError(f"resource '{name}' is not a managed AWS RDS PostgreSQL resource")
    _refuse_unverifiable_tls_region(state, resource)
    return state, resource


def _managed_valkey(name: str) -> tuple[ControlState, AWSElastiCacheValkeyResource] | None:
    state = store.load()
    resource = state.resources.get(name)
    return (state, resource) if isinstance(resource, AWSElastiCacheValkeyResource) else None


def _resource_provision_plan(name: str) -> dict[str, object]:
    if (valkey := _managed_valkey(name)) is not None:
        observed = resources_valkey_module.load_observed(store.root, name)
        return valkey_provision_plan(name, valkey[1], observed)
    _state, resource = _managed_resource(name)
    observed = resources_postgres_module.load_observed(store.root, name)
    return resource_provision_plan(name, resource, observed)


def _resource_deployment_names(name: str) -> list[str]:
    return sorted(
        deployment_name
        for deployment_name, deployment in store.load().deployments.items()
        if (
            deployment.resources.database == name
            or (
                deployment.resources.valkey is not None
                and deployment.resources.valkey.resource == name
            )
        )
    )


@mcp.tool(annotations=READ)
@_journal_plan("apply_resource", "name")
def plan_apply_resource(name: Name) -> dict[str, object]:
    """Plan provisioning or reconciling one managed AWS RDS PostgreSQL instance or
    ElastiCache Valkey replication group."""
    return _resource_provision_plan(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("apply_resource", "name")
def apply_resource(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Create the RDS instance, or converge an existing one onto desired state with one
    immediate modification, polling at most 30 seconds before returning a bounded pending
    phase. Never returns a decrypted credential."""
    with _deployment_resource_locks(*_resource_deployment_names(name)):
        expected = _resource_provision_plan(name)
        _assert_plan(expected, plan_id)
        if (valkey := _managed_valkey(name)) is not None:
            state, cache = valkey
            network = state.aws_networks[cache.aws_network]
            workload_store = cast(
                AWSSecretsManagerStore, state.secret_stores[cache.workload_secret_store]
            )
            return {"changed": True, **resources_valkey_module.apply_provision(
                elasticache_valkey, store.root,
                state.provider_accounts[network.provider_account],
                network, cache, name, workload_store, cache.workload_secret_store,
            )}
        state, resource = _managed_resource(name)
        network = state.aws_networks[resource.aws_network]
        account = state.provider_accounts[network.provider_account]
        result = resources_postgres_module.apply_provision(
            rds_postgres, store.root, account, network, resource, name
        )
        return {"changed": True, **result}


@mcp.tool(annotations=READ)
def inspect_resource(name: Name) -> dict[str, object]:
    """Read-only, secret-free provider identity, health, and version for one resource."""
    state = store.load()
    resource = state.resources.get(name)
    if resource is None:
        raise KeyError(f"resource '{name}' is not registered")
    if isinstance(resource, AWSElastiCacheValkeyResource):
        return _inspect_valkey(state, name, resource)
    if not isinstance(resource, AWSRDSPostgresResource):
        return {
            "resource": name, "provider": resource.provider, "target": resource.target,
            "kind": resource.kind, "version": resource.version,
        }
    observed = resources_postgres_module.load_observed(store.root, name)
    network = state.aws_networks[resource.aws_network]
    account = state.provider_accounts[network.provider_account]
    live: resources_postgres_module.InstanceObservation | None = None
    refresh_error: str | None = None
    try:
        live = rds_postgres.describe_instance(
            account, network, resources_postgres_module.derive_instance_identifier(name)
        )
    except ResourceError as exc:
        refresh_error = str(exc)
    result: dict[str, object] = {
        "resource": name, "provider": "aws_rds_postgres",
        "phase": "absent" if observed is None else observed["phase"],
        "source": "cache" if live is None else "live",
    }
    if refresh_error is not None:
        result["refresh_error"] = refresh_error
    if live is not None:
        result.update(
            phase="ready" if live.status == "available" and not live.converging else "pending",
            status=live.status, engine_version=live.engine_version, identity=live.identity,
            endpoint=live.endpoint, port=live.port,
            drift=resources_postgres_module.instance_drift(resource, live),
        )
    elif observed is not None:
        result.update(
            status=observed["status"], engine_version=observed["engine_version"],
            identity=observed["identity"], endpoint=observed["endpoint"], port=observed["port"],
        )
    if observed is not None:
        result["allocations"] = {
            deployment_name: {
                "database": allocation["database_identifier"], "status": allocation["status"],
            }
            for deployment_name, allocation in cast(
                dict[str, dict[str, object]], observed["allocations"]
            ).items()
        }
    return result


def _inspect_valkey(
    state: ControlState, name: str, resource: AWSElastiCacheValkeyResource
) -> dict[str, object]:
    """Bounded and secret-free: no endpoint, address, ARN, user, or secret identifier."""
    observed = resources_valkey_module.load_observed(store.root, name)
    network = state.aws_networks[resource.aws_network]
    group_id = resources_valkey_module.derive_group_id(name)
    live: resources_valkey_module.GroupObservation | None = None
    refresh_error: str | None = None
    try:
        live = elasticache_valkey.describe_group(
            state.provider_accounts[network.provider_account], network, group_id
        )
    except ResourceError as exc:
        refresh_error = str(exc)
    result: dict[str, object] = {
        "resource": name, "provider": resource.provider, "kind": resource.kind,
        "phase": "absent" if observed is None else observed["phase"],
        "source": "cache" if live is None else "live",
    }
    if refresh_error is not None:
        result["refresh_error"] = refresh_error
    operation = resources_valkey_module.busy_operation(store.root, name)
    if operation is not None:
        result["operation"] = operation
        progress = resources_valkey_module.operation_progress(store.root, name, operation)
        if progress:
            result["progress"] = progress
    if live is not None:
        issues = resources_valkey_module.structural_issues(resource, live, group_id)
        result.update(
            phase="restoring" if operation == "restoring"
            else progress.get("phase", "destroying") if operation == "destroying"
            else "pending" if operation == "provisioning"
            else resources_valkey_module.group_phase(live, issues), status=live.status,
            engine_version=live.engine_version,
            effective_durability=live.effective_durability, issues=issues,
            drift=resources_valkey_module.group_drift(resource, live),
        )
    elif observed is not None:
        result.update(
            phase=progress.get("phase", "destroying") if operation == "destroying"
            else "pending" if operation == "provisioning"
            else result["phase"],
            status=observed["status"], engine_version=observed["engine_version"],
            effective_durability=observed["effective_durability"], issues=observed["issues"],
        )
    elif operation == "provisioning":
        result["phase"] = "pending"
    if observed is not None:
        result["allocations"] = {
            deployment_name: {"status": allocation["status"]}
            for deployment_name, allocation in cast(
                dict[str, dict[str, object]], observed["allocations"]
            ).items()
        }
    return result


def _managed_valkey_binding(
    name: str,
) -> tuple[ControlState, DeploymentConfig, str, AWSElastiCacheValkeyResource] | None:
    state, deployment, _target, _application = _context(name)
    binding = deployment.resources.valkey
    resource = None if binding is None else state.resources.get(binding.resource)
    if binding is None or not isinstance(resource, AWSElastiCacheValkeyResource):
        return None
    return state, deployment, binding.resource, resource


def _database_binding_plan(name: str) -> dict[str, object]:
    _state, deployment, _target, _application = _context(name)
    resource_name = deployment.resources.database
    if resource_name is None:
        raise ValueError(f"deployment {name} has no bound database resource")
    _managed_resource(resource_name)
    observed = resources_postgres_module.load_observed(store.root, resource_name)
    return resource_binding_plan(name, deployment, resource_name, observed)


def _resource_binding_plan(name: str) -> dict[str, object]:
    valkey = _managed_valkey_binding(name)
    if valkey is None:
        return _database_binding_plan(name)
    state, deployment, resource_name, _resource = valkey
    binding = cast(ValkeyBinding, deployment.resources.valkey)
    database = deployment.resources.database
    return valkey_binding_plan(
        name, resource_name, binding.uses,
        resources_valkey_module.namespace_prefixes(name, binding.uses),
        resources_valkey_module.LARAVEL_PROFILE,
        resources_valkey_module.load_observed(store.root, resource_name),
        _database_binding_plan(name)
        if isinstance(state.resources.get(database or ""), AWSRDSPostgresResource) else None,
    )


@mcp.tool(annotations=READ)
@_journal_plan("bind_resource", "name")
def plan_bind_resource(name: Name) -> dict[str, object]:
    """Plan creating this deployment's isolated database, role, and workload secret, and
    its Valkey ACL user, namespace, and credential when it binds a managed Valkey."""
    return _resource_binding_plan(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("bind_resource", "name")
def bind_resource(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Create or reconcile the deployment's isolated database and Valkey ACL user, each with
    its Resource Credential. Never returns a workload username or password."""
    with _deployment_resource_lock(name):
        expected = _resource_binding_plan(name)
        _assert_plan(expected, plan_id)
        valkey = _managed_valkey_binding(name)
        if valkey is None:
            return {"changed": True, **_bind_database(name, expected)}
        state, deployment, resource_name, resource = valkey
        ready = cast(dict[str, object], expected["valkey"])["resource_ready"]
        database = cast(dict[str, object] | None, expected["database"])
        if not ready or (database is not None and not database["resource_ready"]):
            raise ValueError("managed resource is not ready; run apply_resource first")
        result: dict[str, object] = {"changed": True}
        if database is not None:
            result.update(_bind_database(name, database))
        network = state.aws_networks[resource.aws_network]
        workload_store = cast(
            AWSSecretsManagerStore, state.secret_stores[resource.workload_secret_store]
        )
        result["valkey"] = resources_valkey_module.apply_binding(
            elasticache_valkey, store.root, state.provider_accounts[network.provider_account],
            network, resource, resource_name, workload_store, resource.workload_secret_store,
            name, cast(ValkeyBinding, deployment.resources.valkey).uses,
        )
    return result


def _bind_database(name: str, expected: dict[str, object]) -> dict[str, object]:
    if not expected["resource_ready"]:
        raise ValueError("managed resource is not ready; run apply_resource first")
    state, deployment, _target, _application = _context(name)
    resource_name = str(expected["resource"])
    _state, resource = _managed_resource(resource_name)
    network = state.aws_networks[resource.aws_network]
    account = state.provider_accounts[network.provider_account]
    admin_target = state.targets[resource.administration_target]
    store_name = resource.workload_secret_store
    workload_store = state.secret_stores[store_name]
    if not isinstance(workload_store, AWSSecretsManagerStore):
        raise ValueError("workload_secret_store must be an AWS Secrets Manager store")
    observed = resources_postgres_module.load_observed(store.root, resource_name)
    if observed is None or observed["master_secret_arn"] is None:
        raise ResourceError("aws_rds_master_secret_missing")
    master_username, master_password = rds_postgres.resolve_master_credential(
        account, network.region, str(observed["master_secret_arn"])
    )
    database_identifier = deployment.placement.database_identifier
    workload_password = resources_postgres_module.generate_workload_password()
    payload = {
        "master_username": master_username,
        "master_password": master_password,
        "workload_password": workload_password,
    }
    with _deployment_resource_lock(name), protected_secret_file(payload) as secret_file:
        runner.run(
            "gimme:resource:bind-postgres", legacy_server(admin_target), stack=admin_target.stack,
            resource_endpoint=(str(observed["endpoint"]), int(cast(int, observed["port"]))),
            resource_database=database_identifier, secret_file=secret_file,
            resource_trust_bundle_sha256=resources_postgres_module.RDS_TRUST_BUNDLE_SHA256,
            timeout=120,
        )
        summary = resources_postgres_module.persist_binding(
            rds_postgres, store.root, account, workload_store, store_name, resource_name, name,
            database_identifier, database_identifier, workload_password,
            str(observed["endpoint"]), int(cast(int, observed["port"])),
        )
    return summary


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
    target = store.target(name)
    return _result(runner.run("gimme:inspect", legacy_server(target), stack=target.stack,
                              sites=target_sites(store.load(), name),
                              network_mode=target.network.mode,
                              mise_version=target.runtimes.mise_version, timeout=60))


@mcp.tool(annotations=READ)
@_journal_plan("target_stack", "name")
def plan_target_stack(name: Name) -> dict[str, object]:
    """Preflight packages and helpers and return the exact target stack plan."""
    return _resolved_stack_plan(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("target_stack", "name")
def apply_target_stack(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Reconcile a target stack through its bootstrapped privileged helper."""
    expected = _resolved_stack_plan(name)
    _assert_plan(expected, plan_id)
    if not expected["mcp_apply_ready"]:
        raise ValueError("target is not ready for MCP apply; run gimme-bootstrap-target")
    state = store.load()
    target = state.targets[name]
    return _result(runner.run("gimme:provision:stack", legacy_server(target),
                              stack=target.stack, sites=target_sites(state, name),
                              network_mode=target.network.mode,
                              mise_version=target.runtimes.mise_version, timeout=1800))


@mcp.tool(annotations=READ)
@_journal_plan("deployment_runtimes", "name")
def plan_deployment_runtimes(name: Name) -> dict[str, object]:
    """Plan exact runtime and extension reconciliation for one deployment."""
    state, deployment, target, application = _context(name)
    return exact_plan({
        "kind": "deployment_runtimes",
        "deployment": name,
        "target": deployment.target,
        "mise_version": target.runtimes.mise_version,
        "runtimes": {
            key: value.model_dump(mode="json")
            for key, value in deployment.runtimes.items()
        },
        "php_extensions": application.php_extensions,
        "effects": [
            "install only declared mise-managed runtime versions",
            "verify exact system and bundled runtime versions",
            "leave every other installed runtime version available",
        ],
    })


@mcp.tool(annotations=WRITE)
@_journal_apply("deployment_runtimes", "name")
def apply_deployment_runtimes(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Install mise pins and verify system runtimes for one deployment."""
    with _deployment_resource_lock(name):
        expected = plan_deployment_runtimes(name)
        _assert_plan(expected, plan_id)
        result = _run_deployment("gimme:provision:runtimes", name, timeout=1800)
        _run_deployment("gimme:preflight:runtimes", name, timeout=120)
        return _result(result)


@mcp.tool(annotations=READ)
@_journal_plan("deployment_resources", "name")
def plan_deployment_resources(name: Name) -> dict[str, object]:
    """Plan routing, database, cache, runtime values, secrets, and processes."""
    return _resource_plan(name)


@mcp.tool(annotations=WRITE)
@_journal_apply("deployment_resources", "name")
def apply_deployment_resources(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Reconcile one deployment's route and target-local runtime resources."""
    with _deployment_resource_lock(name):
        expected = _resource_plan(name)
        _assert_plan(expected, plan_id)
        return _apply_resources(name, expected)


def _apply_resources(name: str, expected: dict[str, Any]) -> dict[str, object]:
    if not expected["ready"]:
        raise ValueError("deployment resources are not ready; inspect readiness_issues")
    state, deployment, target, _ = _context(name)
    resolved = resolve_planned_secret_references(
        state, store.secrets_path,
        {**deployment.secrets, **_valkey_runtime(name, state, deployment)[1]},
        cast(list[dict[str, str]], expected["secret_versions"]), aws_secrets,
    )
    try:
        with _deployment_resource_lock(name):
            runner.run("gimme:reconcile:sites", legacy_server(target), stack=target.stack,
                       sites=target_sites(state, deployment.target),
                       network_mode=target.network.mode,
                       mise_version=target.runtimes.mise_version, timeout=1800)
            with protected_secret_file(resolved) as secret_file:
                _run_deployment("gimme:provision:app", name, secret_file=secret_file,
                                secret_manifest=cast(
                                    list[dict[str, str]], expected["secret_versions"]
                                ),
                                timeout=1800)
            save_applied_secret_manifest(
                store.root, name, cast(list[dict[str, str]], expected["secret_versions"])
            )
    except Exception:
        # Remote activation includes transactional rollback, but neither successful nor
        # failed Deployer output is a safe MCP surface after plaintext resolution.
        raise SecretError("deployment_secret_activation_failed") from None
    return {"changed": True, "deployment": name}


@mcp.tool(annotations=READ)
@_journal_plan("deployment", "name")
def plan_deployment(name: Name) -> dict[str, object]:
    """Resolve source and pinned runtimes and render the exact Deployer task graph."""
    return _release_plan(name)


@mcp.tool(annotations=CHANGE)
@_journal_apply("deployment", "name")
def apply_deployment(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Deploy an exact reviewed revision with health gates and worker refresh."""
    with _deployment_resource_lock(name):
        expected = _release_plan(name)
        _assert_plan(expected, plan_id)
        if not expected["ready"]:
            raise ValueError("deployment is not ready; inspect readiness_issues")
        result = _run_deployment("deploy", name, revision=str(expected["revision"]),
                                 timeout=1800)
        _, deployment, _, application = _context(name)
        if application.framework == "laravel" and (
            deployment.workers is not None or deployment.scheduler is not None
        ):
            _run_deployment("gimme:provision:processes", name, timeout=1800)
        return _result(result)


@mcp.tool(annotations=READ)
def list_releases(name: Name) -> dict[str, object]:
    """List retained releases for one deployment and identify the current release."""
    return _result(_run_deployment("releases", name))


@mcp.tool(annotations=CHANGE)
@_journal_apply("rollback_deployment", "name")
def rollback_deployment(name: Name, confirmation: str) -> dict[str, object]:
    """Restore a deployment's prior retained release after exact confirmation."""
    expected = f"ROLLBACK {name}"
    if confirmation != expected:
        raise ValueError(f"confirmation must exactly equal '{expected}'")
    with _deployment_resource_lock(name):
        return _result(_run_deployment("rollback", name))


@mcp.tool(annotations=READ)
@_journal_plan("promotion", "source", "destination")
def plan_promotion(source: Name, destination: Name) -> dict[str, object]:
    """Plan deploying the source deployment's exact live commit to a destination."""
    state, source_deployment, _, _ = _context(source)
    destination_deployment = state.deployments[destination]
    if source_deployment.application != destination_deployment.application:
        raise ValueError("promotion requires deployments of the same application")
    current = _run_deployment("gimme:current-revision", source, timeout=60)
    revision = ""
    for raw in current.output.splitlines():
        line = raw.split("] ", 1)[-1].strip()
        if line.startswith("GIMME_CURRENT_REVISION|"):
            revision = line.split("|", 1)[1]
    if re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", revision) is None:
        raise RuntimeError("source deployment has no exact current revision")
    return exact_plan({"kind": "promotion", "source": source, "destination": destination,
                       "revision": revision, "release": _release_plan(destination, revision)})


@mcp.tool(annotations=CHANGE)
@_journal_apply("promotion", "source", "destination")
def promote_deployment(source: Name, destination: Name, plan_id: PlanId) -> dict[str, object]:
    """Promote an exact reviewed live commit and pin the destination after success."""
    with _deployment_resource_locks(source, destination):
        expected = plan_promotion(source, destination)
        _assert_plan(expected, plan_id)
        release = cast(dict[str, object], expected["release"])
        if not release["ready"]:
            raise ValueError(
                "destination deployment is not ready; inspect release readiness_issues"
            )
        revision = str(expected["revision"])
        result = _run_deployment("deploy", destination, revision=revision, timeout=1800)
        state = store.load()
        deployment = state.deployments[destination].model_copy(
            update={"source": DeploymentSource(kind="commit", ref=revision)})
        application = state.applications[deployment.application]
        if application.framework == "laravel" and (
            deployment.workers is not None or deployment.scheduler is not None
        ):
            _run_deployment("gimme:provision:processes", destination, timeout=1800)
        store.save(_replace(state, "deployments", destination, deployment))
        return _result(result)


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
