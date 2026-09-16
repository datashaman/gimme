from __future__ import annotations

import re
import socket
from pathlib import Path
from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from gimme.control import (
    ApplicationConfig, ControlState, DeploymentConfig, DeploymentRegistration,
    DeploymentSource, StateStore, TargetConfig, legacy_app, legacy_server,
    new_placement, target_sites,
)
from gimme.control_plans import (
    deployment_release_plan, deployment_removal_plan, deployment_resource_plan,
    exact_plan, migration_plan, registration_update_plan, target_stack_plan,
)
from gimme.deployer import CommandResult, DeployerRunner
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
        network_mode=target.network.mode, toolchains=target.toolchains.model_dump(),
        variables=deployment.variables, secret_file=secret_file,
        artisan_command=artisan_command, artisan_arguments=artisan_arguments,
        artisan_allowed_commands=(application.artisan.allowed_commands
                                  if application.artisan is not None else None),
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
                        toolchains=target.toolchains.model_dump(), timeout=60, bootstrap=True)
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
    preflight = _run_deployment("gimme:preflight:frontend", name, revision=selected,
                                timeout=60)
    rendered = _run_deployment("deploy", name, revision=selected,
                               arguments=("--plan",), timeout=60)
    return deployment_release_plan(name, deployment, target, application, selected,
                                   rendered.output, {"declared": target.toolchains.model_dump(),
                                                     "preflight": preflight.output})


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


@mcp.resource("gimme://deployments/{name}")
def deployment_resource(name: str) -> dict[str, object]:
    return store.deployment(name).model_dump(mode="json")


@mcp.tool(annotations=READ)
def plan_state_migration() -> dict[str, object]:
    """Plan the one-time legacy manifest to schema-v2 state migration."""
    if store.exists():
        raise ValueError("schema-v2 state already exists")
    return migration_plan(store.legacy_migration(), str(store.root))


@mcp.tool(annotations=WRITE)
def apply_state_migration(plan_id: PlanId) -> dict[str, object]:
    """Atomically write schema-v2 state after verifying its exact migration plan."""
    expected = plan_state_migration()
    _assert_plan(expected, plan_id)
    store.save(store.legacy_migration())
    return {"changed": True, "state_path": str(store.state_path), "schema_version": 2}


@mcp.tool(annotations=READ)
def list_targets() -> dict[str, object]:
    """List every registered target and its desired provisioning policy."""
    return {"targets": store.load().model_dump(mode="json")["targets"]}


@mcp.tool(annotations=READ)
def list_applications() -> dict[str, object]:
    """List reusable registered application source and build definitions."""
    return {"applications": store.load().model_dump(mode="json")["applications"]}


@mcp.tool(annotations=READ)
def list_deployments(target: Name | None = None) -> dict[str, object]:
    """List deployments, optionally restricted to one registered target."""
    values = store.load().model_dump(mode="json")["deployments"]
    if target is not None:
        values = {name: item for name, item in values.items() if item["target"] == target}
    return {"deployments": values}


@mcp.tool(annotations=WRITE)
def register_target(name: Name, definition: TargetConfig) -> dict[str, object]:
    """Register a new target locally without contacting it."""
    state = store.load()
    if name in state.targets:
        raise ValueError("target already exists; use plan_update_target")
    store.save(_replace(state, "targets", name, definition))
    return {"changed": True, "target": name}


@mcp.tool(annotations=READ)
def plan_update_target(name: Name, definition: TargetConfig) -> dict[str, object]:
    """Show the exact before/after state for a target update."""
    state = store.load()
    _replace(state, "targets", name, definition)
    return registration_update_plan("target_update", name, state.targets[name], definition)


@mcp.tool(annotations=WRITE)
def update_target(name: Name, definition: TargetConfig, plan_id: PlanId) -> dict[str, object]:
    """Apply an exact reviewed target update to local desired state."""
    expected = plan_update_target(name, definition)
    _assert_plan(expected, plan_id)
    store.save(_replace(store.load(), "targets", name, definition))
    return {"changed": True, "target": name}


@mcp.tool(annotations=WRITE)
def register_application(name: Name, definition: ApplicationConfig) -> dict[str, object]:
    """Register reusable application source and build metadata locally."""
    state = store.load()
    if name in state.applications:
        raise ValueError("application already exists; use plan_update_application")
    store.save(_replace(state, "applications", name, definition))
    return {"changed": True, "application": name}


@mcp.tool(annotations=READ)
def plan_update_application(name: Name, definition: ApplicationConfig) -> dict[str, object]:
    """Show the exact before/after state for an application update."""
    state = store.load()
    _replace(state, "applications", name, definition)
    return registration_update_plan(
        "application_update", name, state.applications[name], definition
    )


@mcp.tool(annotations=WRITE)
def update_application(name: Name, definition: ApplicationConfig,
                       plan_id: PlanId) -> dict[str, object]:
    """Apply an exact reviewed application update to local desired state."""
    expected = plan_update_application(name, definition)
    _assert_plan(expected, plan_id)
    store.save(_replace(store.load(), "applications", name, definition))
    return {"changed": True, "application": name}


@mcp.tool(annotations=WRITE)
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
                              toolchains=target.toolchains.model_dump(), timeout=60))


@mcp.tool(annotations=READ)
def plan_target_stack(name: Name) -> dict[str, object]:
    """Preflight packages and helpers and return the exact target stack plan."""
    return _resolved_stack_plan(name)


@mcp.tool(annotations=WRITE)
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
                              toolchains=target.toolchains.model_dump(), timeout=1800))


@mcp.tool(annotations=READ)
def plan_deployment_resources(name: Name) -> dict[str, object]:
    """Plan routing, database, cache, runtime values, secrets, and processes."""
    return _resource_plan(name)


@mcp.tool(annotations=WRITE)
def apply_deployment_resources(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Reconcile one deployment's route and target-local runtime resources."""
    expected = _resource_plan(name)
    _assert_plan(expected, plan_id)
    if not expected["ready"]:
        raise ValueError("deployment resources are not ready; inspect readiness_issues")
    state, deployment, target, _ = _context(name)
    runner.run("gimme:reconcile:sites", legacy_server(target), stack=target.stack,
               sites=target_sites(state, deployment.target), network_mode=target.network.mode,
               toolchains=target.toolchains.model_dump(), timeout=1800)
    resolved = resolve_secret_references(store.secrets_path, deployment.secrets)
    with protected_secret_file(resolved) as secret_file:
        result = _run_deployment("gimme:provision:app", name, secret_file=secret_file,
                                 timeout=1800)
    return _result(result)


@mcp.tool(annotations=READ)
def plan_deployment(name: Name) -> dict[str, object]:
    """Resolve source and toolchains and render the exact Deployer task graph."""
    return _release_plan(name)


@mcp.tool(annotations=CHANGE)
def apply_deployment(name: Name, plan_id: PlanId) -> dict[str, object]:
    """Deploy an exact reviewed revision with health gates and worker refresh."""
    expected = _release_plan(name)
    _assert_plan(expected, plan_id)
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
def rollback_deployment(name: Name, confirmation: str) -> dict[str, object]:
    """Restore a deployment's prior retained release after exact confirmation."""
    expected = f"ROLLBACK {name}"
    if confirmation != expected:
        raise ValueError(f"confirmation must exactly equal '{expected}'")
    return _result(_run_deployment("rollback", name))


@mcp.tool(annotations=READ)
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
def promote_deployment(source: Name, destination: Name, plan_id: PlanId) -> dict[str, object]:
    """Promote an exact reviewed live commit and pin the destination after success."""
    expected = plan_promotion(source, destination)
    _assert_plan(expected, plan_id)
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
def plan_remove_deployment(name: Name) -> dict[str, object]:
    """Plan complete cleanup of one deployment and its isolated resources."""
    _, deployment, target, _ = _context(name)
    return deployment_removal_plan(name, deployment, target)


@mcp.tool(annotations=CHANGE)
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
               toolchains=target.toolchains.model_dump(), timeout=1800)
    return _result(result)


@mcp.tool(annotations=READ)
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
