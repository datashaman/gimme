from __future__ import annotations

import re
import socket
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Annotated, Any, Callable, Literal, ParamSpec, TypeVar, cast

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from gimme.control import (
    ApplicationConfig, ControlState, DeploymentConfig, DeploymentRegistration,
    DeploymentSource, ResourceConfig, StateStore, TargetConfig, legacy_app, legacy_server,
    new_placement, target_sites,
)
from gimme.control_plans import (
    deployment_release_plan, deployment_removal_plan, deployment_resource_plan,
    exact_plan, migration_plan, registration_update_plan, target_stack_plan,
)
from gimme.deployer import CommandResult, DeployerRunner
from gimme.journal import OperationJournal
from gimme.secrets import SecretError, protected_secret_file, resolve_secret_references

ROOT = Path(__file__).resolve().parents[2]
store = StateStore.from_environment(ROOT)
runner = DeployerRunner(ROOT)
mcp = FastMCP(
    "Gimme",
    instructions=(
        "Git-backed deployment control plane for explicitly registered Ubuntu targets, "
        "applications, and deployments. Inspect a plan before every remote mutation and "
        "pass its exact plan_id to apply. Secrets are SOPS references, never tool arguments."
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
P = ParamSpec("P")
R = TypeVar("R", bound=dict[str, object])
_suppress_plan_journal: ContextVar[bool] = ContextVar("suppress_plan_journal", default=False)


def _journal() -> OperationJournal:
    return OperationJournal(store.root)


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
    artisan_command: str | None = None, artisan_arguments: list[str] | None = None,
    timeout: int = 900,
) -> CommandResult:
    state, deployment, target, application = _context(name)
    bound_resources = {
        kind: state.resources[resource_name].model_dump(mode="json")
        for kind, resource_name in (
            ("database", deployment.resources.database),
            ("cache", deployment.resources.cache),
        )
        if resource_name is not None
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
        variables=deployment.variables, secret_file=secret_file,
        artisan_command=artisan_command, artisan_arguments=artisan_arguments,
        artisan_allowed_commands=(
            application.artisan.allowed_commands
            if artisan_command is not None and application.artisan is not None
            else None
        ),
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


def _secret_issues(deployment: DeploymentConfig) -> list[str]:
    try:
        resolve_secret_references(store.secrets_path, deployment.secrets)
    except SecretError as exc:
        return [str(exc)]
    return []


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


def _resource_plan(name: str) -> dict[str, Any]:
    _, deployment, target, application = _context(name)
    issues = _secret_issues(deployment) + _dns_issues(deployment, target)
    plan = deployment_resource_plan(name, deployment, target, application,
                                    missing_secrets=issues)
    if issues:
        plan["readiness_issues"] = issues
        plan["plan_id"] = StateStore.digest({k: v for k, v in plan.items() if k != "plan_id"})
    return plan


def _release_plan(name: str, revision: str | None = None) -> dict[str, Any]:
    _, deployment, target, application = _context(name)
    issues = _secret_issues(deployment) + _dns_issues(deployment, target)
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


@mcp.resource("gimme://resources/{name}")
def managed_resource(name: str) -> dict[str, object]:
    return store.load().resources[name].model_dump(mode="json")


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
    """Inspect exact installed versions and plan migration to schema-v3 state."""
    if store.exists() and store.raw_state().get("schema_version") == 3:
        raise ValueError("schema-v3 state already exists")
    observations = _migration_observations()
    return migration_plan(store.state_migration(observations), str(store.root))


@mcp.tool(annotations=WRITE)
@_journal_apply("state_migration")
def apply_state_migration(plan_id: PlanId) -> dict[str, object]:
    """Atomically write schema-v3 state after re-observing exact installed versions."""
    observations = _migration_observations()
    state = store.state_migration(observations)
    expected = migration_plan(state, str(store.root))
    _assert_plan(expected, plan_id)
    store.save(state)
    return {"changed": True, "state_path": str(store.state_path), "schema_version": 3}


@mcp.tool(annotations=READ)
def list_targets() -> dict[str, object]:
    """List every registered target and its desired provisioning policy."""
    return {"targets": store.load().model_dump(mode="json")["targets"]}


@mcp.tool(annotations=READ)
def list_applications() -> dict[str, object]:
    """List reusable registered application source and build definitions."""
    return {"applications": store.load().model_dump(mode="json")["applications"]}


@mcp.tool(annotations=READ)
def list_resources(target: Name | None = None) -> dict[str, object]:
    """List named, version-pinned infrastructure resources."""
    values = store.load().model_dump(mode="json")["resources"]
    if target is not None:
        values = {name: item for name, item in values.items() if item["target"] == target}
    return {"resources": values}


@mcp.tool(annotations=READ)
def list_deployments(target: Name | None = None) -> dict[str, object]:
    """List deployments, optionally restricted to one registered target."""
    values = store.load().model_dump(mode="json")["deployments"]
    if target is not None:
        values = {name: item for name, item in values.items() if item["target"] == target}
    return {"deployments": values}


@mcp.tool(annotations=READ)
def list_operations(limit: int = 50, operation: OperationName | None = None,
                    subject: Name | None = None,
                    correlation_id: CorrelationId | None = None) -> dict[str, object]:
    """List secret-safe journal events, newest first, with optional exact filters."""
    events = _journal().list(limit=limit, operation=operation, subject=subject,
                             correlation_id=correlation_id)
    return {"events": [event.model_dump(mode="json") for event in events]}


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
def register_resource(name: Name, definition: ResourceConfig) -> dict[str, object]:
    """Register a named, exact-version infrastructure resource locally."""
    state = store.load()
    if name in state.resources:
        raise ValueError("resource already exists; use plan_update_resource")
    store.save(_replace(state, "resources", name, definition))
    return {"changed": True, "resource": name}


@mcp.tool(annotations=READ)
@_journal_plan("update_resource", "name")
def plan_update_resource(name: Name, definition: ResourceConfig) -> dict[str, object]:
    """Show the exact before/after state for a resource update."""
    state = store.load()
    _replace(state, "resources", name, definition)
    return registration_update_plan("resource_update", name, state.resources[name], definition)


@mcp.tool(annotations=WRITE)
@_journal_apply("update_resource", "name")
def update_resource(name: Name, definition: ResourceConfig, plan_id: PlanId) -> dict[str, object]:
    """Apply an exact reviewed resource update to local desired state."""
    expected = plan_update_resource(name, definition)
    _assert_plan(expected, plan_id)
    store.save(_replace(store.load(), "resources", name, definition))
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
    runner.run("gimme:reconcile:sites", legacy_server(target), stack=target.stack,
               sites=target_sites(state, deployment.target), network_mode=target.network.mode,
               mise_version=target.runtimes.mise_version, timeout=1800)
    resolved = resolve_secret_references(store.secrets_path, deployment.secrets)
    with protected_secret_file(resolved) as secret_file:
        result = _run_deployment("gimme:provision:app", name, secret_file=secret_file,
                                 timeout=1800)
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
def target_service_status(name: Name,
                          service: Literal["postgresql", "valkey-server", "caddy"]
                          ) -> dict[str, object]:
    """Read status for one allowlisted service on a registered target."""
    target = store.target(name)
    return _result(runner.run("gimme:service:status", legacy_server(target),
                              stack=target.stack, arguments=(f"service={service}",), timeout=60))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
