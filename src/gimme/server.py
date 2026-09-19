from __future__ import annotations

import fcntl
import os
import re
import socket
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, ParamSpec, TypeVar, cast

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from gimme import recovery as recovery_module
from gimme import resources_postgres as resources_postgres_module
from gimme import resources_valkey as resources_valkey_module
from gimme import valkey_contract
from gimme.control import (
    AWSElastiCacheValkeyResource, AWSProviderAccount, AWSRDSPostgresResource,
    AWSSecretsManagerStore, ApplicationConfig,
    ControlState, DeploymentConfig, DeploymentRegistration, DeploymentSource, Resource,
    ResourceConfig, S3BackupDestination, SecretReference, SecretStore, StateStore, TargetConfig,
    ValkeyBinding,
    legacy_app, legacy_server, new_placement, target_sites,
)
from gimme.control_plans import (
    deployment_release_plan, deployment_removal_plan, deployment_resource_plan,
    exact_plan, migration_plan, recovery_point_creation_plan, registration_update_plan,
    resource_binding_plan, resource_cleanup_plan, resource_provision_plan, target_stack_plan,
    valkey_binding_plan,
    valkey_provision_plan,
)
from gimme.deployer import CommandResult, DeployerRunner
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
CorrelationId = Annotated[str, Field(pattern=r"^corr_[a-f0-9]{32}$")]
OperationName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", max_length=64)]
RequestId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$", max_length=64)]
P = ParamSpec("P")
R = TypeVar("R", bound=dict[str, object])
_suppress_plan_journal: ContextVar[bool] = ContextVar("suppress_plan_journal", default=False)


def _journal() -> OperationJournal:
    return OperationJournal(store.root)


@contextmanager
def _deployment_resource_lock(name: str):
    directory = store.root / "deployment-locks"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / f"{name}.lock"
    with path.open("a+") as lock:
        os.chmod(path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
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
    timeout: int = 900,
) -> CommandResult:
    state, deployment, target, application = _context(name)
    valkey = deployment.resources.valkey
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
        instance_name=deployment.placement.instance,
        deploy_path=f"{target.apps_root}/{deployment.placement.relative_path}",
        site_host=deployment.placement.site_host,
        database_identifier=deployment.placement.database_identifier,
        cache_prefix=deployment.placement.cache_prefix,
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
        timeout=timeout,
    )


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
    observed = resources_valkey_module.load_observed(store.root, binding.resource)
    if observed is None or observed["phase"] != "ready":
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
        valkey_contract.probe_config(name, binding.uses, host, port),
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
        "probes": valkey_contract.probe_names(list(binding.uses)),
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


def _resource_plan(name: str) -> dict[str, Any]:
    state, deployment, target, application = _context(name)
    secret_versions, secret_issues = _secret_plan(name, state, deployment)
    issues = secret_issues + _dns_issues(deployment, target) + _managed_database_issues(
        state, deployment
    ) + _valkey_runtime(name, state, deployment)[3]
    plan = deployment_resource_plan(name, deployment, target, application,
                                    missing_secrets=issues, secret_versions=secret_versions,
                                    valkey_contract=_contract_summary(name, state, deployment))
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
            return {"changed": False, "recovery_point": existing}
        with tempfile.TemporaryDirectory(prefix="gimme-recovery-") as directory:
            local_path = Path(directory) / "postgres.dump"
            result = _run_deployment(
                "gimme:backup:dump-postgres", name, backup_local_path=local_path, timeout=1800,
            )
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
                or size < 0
                or not local_path.is_file()
                or local_path.stat().st_size != size
            ):
                raise RecoveryError("recovery_dump_metadata_invalid")
            dump = ComponentDump(
                kind="postgres", local_path=local_path, sha256=sha256, bytes=size
            )
            manifest = recovery_module.create_recovery_point(
                destination_name, destination, credentials, backup_s3, name, point_id, dump,
            )
    return {"changed": True, "recovery_point": manifest}


@mcp.tool(annotations=READ)
def list_recovery_points(name: Name) -> dict[str, object]:
    """List one deployment's Recovery Points from destination-authoritative inventory."""
    state, _deployment, destination_name, destination = _recovery_context(name)
    _, credentials = _backup_destination_credentials(state, destination)
    return recovery_module.list_recovery_points(
        destination_name, destination, credentials, backup_s3, name
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
    expected = _resource_provision_plan(name)
    _assert_plan(expected, plan_id)
    if (valkey := _managed_valkey(name)) is not None:
        state, cache = valkey
        network = state.aws_networks[cache.aws_network]
        workload_store = cast(
            AWSSecretsManagerStore, state.secret_stores[cache.workload_secret_store]
        )
        return {"changed": True, **resources_valkey_module.apply_provision(
            elasticache_valkey, store.root, state.provider_accounts[network.provider_account],
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
    if live is not None:
        issues = resources_valkey_module.structural_issues(resource, live, group_id)
        result.update(
            phase=resources_valkey_module.group_phase(live, issues), status=live.status,
            engine_version=live.engine_version,
            effective_durability=live.effective_durability, issues=issues,
            drift=resources_valkey_module.group_drift(resource, live),
        )
    elif observed is not None:
        result.update(
            status=observed["status"], engine_version=observed["engine_version"],
            effective_durability=observed["effective_durability"], issues=observed["issues"],
        )
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
    with _deployment_resource_lock(name):
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
    expected = _resource_plan(name)
    _assert_plan(expected, plan_id)
    if not expected["ready"]:
        raise ValueError("deployment resources are not ready; inspect readiness_issues")
    state, deployment, target, _ = _context(name)
    resolved = resolve_planned_secret_references(
        state, store.secrets_path,
        {**deployment.secrets, **_valkey_runtime(name, state, deployment)[1]},
        cast(list[dict[str, str]], expected["secret_versions"]), aws_secrets,
    )
    with _deployment_resource_lock(name):
        runner.run("gimme:reconcile:sites", legacy_server(target), stack=target.stack,
                   sites=target_sites(state, deployment.target),
                   network_mode=target.network.mode,
                   mise_version=target.runtimes.mise_version, timeout=1800)
        with protected_secret_file(resolved) as secret_file:
            result = _run_deployment("gimme:provision:app", name, secret_file=secret_file,
                                     secret_manifest=cast(
                                         list[dict[str, str]], expected["secret_versions"]
                                     ),
                                     timeout=1800)
        save_applied_secret_manifest(
            store.root, name, cast(list[dict[str, str]], expected["secret_versions"])
        )
    return _result(result)


@mcp.tool(annotations=READ)
@_journal_plan("deployment", "name")
def plan_deployment(name: Name) -> dict[str, object]:
    """Resolve source and pinned runtimes and render the exact Deployer task graph."""
    return _release_plan(name)


@mcp.tool(annotations=CHANGE)
@_journal_apply("deployment", "name")
def apply_deployment(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Deploy an exact reviewed revision with health gates and worker refresh."""
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
    expected = plan_promotion(source, destination)
    _assert_plan(expected, plan_id)
    release = cast(dict[str, object], expected["release"])
    if not release["ready"]:
        raise ValueError("destination deployment is not ready; inspect release readiness_issues")
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
    expected = plan_artisan(name, command, arguments)
    _assert_plan(expected, plan_id)
    return _result(_run_deployment("gimme:artisan", name, artisan_command=command,
                                   artisan_arguments=arguments or []))


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
