from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from gimme.config import (
    AppConfig,
    ArtisanConfig,
    ConfigStore,
    FrontendBuildConfig,
    SchedulerConfig,
    WorkerConfig,
)
from gimme.deployer import DeployerRunner
from gimme.plans import (
    app_process_plan,
    app_resource_plan,
    artisan_command_plan,
    stack_plan,
)


ROOT = Path(__file__).resolve().parents[2]
store = ConfigStore(ROOT)
runner = DeployerRunner(ROOT)
mcp = FastMCP(
    "Gimme",
    instructions=(
        "Provision and deploy registered PHP applications on an allowlisted Ubuntu "
        "host. Read plans before applying them. The server does not accept arbitrary "
        "shell commands, SQL, hostnames, or filesystem paths. Laravel command execution "
        "is limited to per-application Artisan allowlists and exact reviewed plans."
    ),
)


READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=True,
)
IDEMPOTENT_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=True,
    openWorldHint=True,
)

ApplicationName = Annotated[
    str,
    Field(
        min_length=1,
        max_length=48,
        pattern=r"^[a-z][a-z0-9-]{0,47}$",
        description="Registered application name from list_apps.",
    ),
]
ArtisanCommand = Annotated[
    str,
    Field(
        min_length=1,
        max_length=120,
        pattern=r"^[a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)*$",
        description="Laravel Artisan command present in the application's allowlist.",
    ),
]
ArtisanArgument = Annotated[
    str,
    Field(
        min_length=1,
        max_length=256,
        description="One literal Artisan positional argument or option.",
    ),
]
ArtisanArguments = Annotated[
    list[ArtisanArgument] | None,
    Field(
        max_length=32,
        description=(
            "Optional argument vector passed literally to Artisan. Each item is bounded and "
            "shell-escaped; environment-selection arguments are rejected."
        ),
    ),
]
WorkerRegistration = Annotated[
    WorkerConfig | None,
    Field(
        description=(
            "Optional standard queue worker or Horizon process definition. Omit to keep "
            "application workers unmanaged."
        )
    ),
]
SchedulerRegistration = Annotated[
    SchedulerConfig | None,
    Field(description="Optional every-minute Laravel scheduler definition."),
]
PlanIdentifier = Annotated[
    str,
    Field(
        pattern=r"^plan_[a-f0-9]{20}$",
        description="Exact plan_id returned by plan_artisan for this invocation.",
    ),
]


def titled(base: ToolAnnotations, title: str) -> ToolAnnotations:
    return base.model_copy(update={"title": title})


@mcp.resource(
    "gimme://config/server",
    name="server_manifest",
    title="Server manifest",
    description=(
        "Validated desired server identity, bootstrap endpoint, SSH user, and "
        "application root."
    ),
    mime_type="application/json",
)
def server_manifest() -> dict[str, object]:
    return store.server().model_dump(mode="json")


@mcp.resource(
    "gimme://config/stack",
    name="stack_manifest",
    title="Stack manifest",
    description="Validated desired APT packages and managed systemd services.",
    mime_type="application/json",
)
def stack_manifest() -> dict[str, object]:
    return store.stack().model_dump(mode="json")


@mcp.resource(
    "gimme://config/apps",
    name="application_registry",
    title="Application registry",
    description="Validated registry of applications available to Gimme.",
    mime_type="application/json",
)
def application_registry() -> dict[str, object]:
    return store.registry().model_dump(mode="json")


@mcp.resource(
    "gimme://apps/{name}",
    name="application_detail",
    title="Application detail",
    description=(
        "Registration, deployment path, and HTTPS URL for one registered application."
    ),
    mime_type="application/json",
)
def application_detail(name: str) -> dict[str, object]:
    server = store.server()
    app = store.app(name)
    return {
        "name": name,
        **app.model_dump(mode="json"),
        "deploy_path": f"{server.apps_root}/{name}",
        "site_url": f"https://{name}.{server.mdns_name}.local",
    }


@mcp.resource(
    "gimme://apps/{name}/releases",
    name="application_releases",
    title="Application releases",
    description="Read-only Deployer release history for one registered application.",
    mime_type="application/json",
)
def application_releases(name: str) -> dict[str, object]:
    server = store.server()
    app = store.app(name)
    result = runner.run("releases", server, app_name=name, app=app)
    return {"application": name, **result.as_dict()}


def resolved_stack_plan() -> dict[str, object]:
    server = store.server()
    stack = store.stack()
    result = runner.run(
        "gimme:preflight:stack", server, stack=stack, timeout=60, bootstrap=True
    )
    resolution: dict[str, dict[str, str]] = {}
    package_manager_processes: list[int] = []
    privileged_helper = "unknown"
    for raw_line in result.output.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and "] " in line:
            line = line.split("] ", 1)[1]
        if not line.startswith("GIMME_PACKAGE|"):
            if line.startswith("GIMME_APT_BUSY|"):
                value = line.split("|", 1)[1]
                if value != "no":
                    package_manager_processes = [int(pid) for pid in value.split(",")]
            elif line.startswith("GIMME_HELPER|"):
                privileged_helper = line.split("|", 1)[1]
            continue
        _, name, installed, candidate = line.split("|", 3)
        resolution[name] = {"installed": installed, "candidate": candidate}
    missing_results = sorted(set(stack.packages) - set(resolution))
    if missing_results:
        raise RuntimeError(
            "preflight did not return results for configured packages: "
            + ", ".join(missing_results)
        )
    return stack_plan(
        server,
        stack,
        resolution,
        package_manager_processes,
        apps=store.registry().apps,
        privileged_helper=privileged_helper,
    )


def resolved_app_process_plan(name: str) -> dict[str, object]:
    server = store.server()
    stack = store.stack()
    app = store.app(name)
    result = runner.run(
        "gimme:preflight:processes",
        server,
        stack=stack,
        app_name=name,
        app=app,
        timeout=30,
    )
    observations = {
        "helper": "unknown",
        "current_release": "unknown",
        "pcntl": "unknown",
        "posix": "unknown",
        "horizon": "unknown",
    }
    markers = {
        "GIMME_PROCESS_HELPER": "helper",
        "GIMME_CURRENT_RELEASE": "current_release",
        "GIMME_PCNTL": "pcntl",
        "GIMME_POSIX": "posix",
        "GIMME_HORIZON": "horizon",
    }
    for raw_line in result.output.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and "] " in line:
            line = line.split("] ", 1)[1]
        if "|" not in line:
            continue
        marker, value = line.split("|", 1)
        if marker in markers:
            observations[markers[marker]] = value
    return app_process_plan(server, name, app, **observations)


@mcp.tool(
    description=(
        "Inspect the configured Ubuntu host and report its OS, installed runtime "
        "commands, service states, mDNS publisher and host-local resolution observations, "
        "and non-interactive sudo availability. Client-side mDNS resolution is explicitly "
        "reported as not observable. Makes no changes."
    ),
    annotations=titled(READ_ONLY, "Inspect host"),
)
def inspect_host() -> dict[str, object]:
    return runner.run(
        "gimme:inspect", store.server(), stack=store.stack(), timeout=30
    ).as_dict()


@mcp.tool(
    description=(
        "Resolve every desired package against the configured host's current package "
        "metadata and return an exact provisioning plan. Unavailable packages make the "
        "plan unready. Makes no changes and returns a plan_id for provision_stack."
    ),
    annotations=titled(READ_ONLY, "Plan stack provisioning"),
)
def plan_stack() -> dict[str, object]:
    return resolved_stack_plan()


@mcp.tool(
    description=(
        "Apply a previously returned stack plan to the configured host. Installs missing "
        "APT packages and enables PostgreSQL and Valkey; rejects stale or invented plan IDs."
    ),
    annotations=titled(IDEMPOTENT_WRITE, "Provision stack"),
)
def provision_stack(plan_id: str) -> dict[str, object]:
    server = store.server()
    stack = store.stack()
    expected = resolved_stack_plan()
    if plan_id != expected["plan_id"]:
        raise ValueError("plan_id is invalid or stale; call plan_stack again")
    if expected["package_manager_processes"]:
        processes = ", ".join(str(pid) for pid in expected["package_manager_processes"])
        raise ValueError(
            f"package manager is already active (PID: {processes}); wait for it to "
            "finish and do not remove its lock files"
        )
    if not expected["ready"]:
        raise ValueError(
            "stack plan contains unavailable packages; update config/stack.json or "
            "configure an explicitly approved package source"
        )
    if expected["privileged_helper"] != "ready":
        raise ValueError(
            "privileged helper is not bootstrapped; run the documented one-time "
            "GIMME_INTERACTIVE_SUDO=1 stack provisioning command"
        )
    return runner.run(
        "gimme:provision:stack", server, stack=stack, bootstrap=True
    ).as_dict()


@mcp.tool(
    description=(
        "List locally registered PHP applications and their Git repository, framework "
        "recipe, branch, and computed deployment path. Does not contact the host."
    ),
    annotations=titled(READ_ONLY, "List applications"),
)
def list_apps() -> dict[str, object]:
    server = store.server()
    registry = store.registry()
    return {
        "apps": {
            name: {
                **app.model_dump(),
                "deploy_path": f"{server.apps_root}/{name}",
            }
            for name, app in registry.apps.items()
        }
    }


@mcp.tool(
    description=(
        "Register or update a PHP application's allowlisted deployment definition. "
        "Laravel definitions may override the default Artisan command allowlist. Changes "
        "only the local registry; it does not connect to or modify the host."
    ),
    annotations=ToolAnnotations(
        title="Register application",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def register_app(
    name: str,
    repository: str,
    framework: Literal[
        "common", "laravel", "symfony", "wordpress", "static"
    ] = "common",
    branch: str = "main",
    frontend: FrontendBuildConfig | None = None,
    artisan: ArtisanConfig | None = None,
    workers: WorkerRegistration = None,
    scheduler: SchedulerRegistration = None,
) -> dict[str, object]:
    app = AppConfig(
        repository=repository,
        framework=framework,
        branch=branch,
        frontend=frontend,
        artisan=artisan,
        workers=workers,
        scheduler=scheduler,
    )
    changed = store.register_app(name, app)
    return {"application": name, "changed": changed, **app.model_dump()}


@mcp.tool(
    description=(
        "Replace only the local worker/Horizon and scheduler definition for an existing "
        "Laravel application while preserving its repository, branch, frontend, and Artisan "
        "settings. Null values disable management; makes no remote changes. Use register_app "
        "for the initial application definition."
    ),
    annotations=ToolAnnotations(
        title="Configure application processes",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def configure_app_processes(
    name: ApplicationName,
    workers: WorkerRegistration = None,
    scheduler: SchedulerRegistration = None,
) -> dict[str, object]:
    changed = store.configure_app_processes(name, workers, scheduler)
    app = store.app(name)
    return {
        "application": name,
        "changed": changed,
        "workers": app.workers.model_dump() if app.workers is not None else None,
        "scheduler": app.scheduler.model_dump() if app.scheduler is not None else None,
    }


@mcp.tool(
    description=(
        "Plan a registered application's PostgreSQL database, database role, Valkey "
        "namespace, and protected remote environment file. Makes no changes."
    ),
    annotations=titled(READ_ONLY, "Plan application resources"),
)
def plan_app_resources(name: str) -> dict[str, object]:
    return app_resource_plan(store.server(), name, store.app(name))


@mcp.tool(
    description=(
        "Create a registered application's PostgreSQL database and role, generate its "
        "password on the host, and write PostgreSQL and Valkey settings to shared/.env. "
        "Requires a matching plan from plan_app_resources."
    ),
    annotations=titled(IDEMPOTENT_WRITE, "Provision application resources"),
)
def provision_app_resources(name: str, plan_id: str) -> dict[str, object]:
    server = store.server()
    app = store.app(name)
    expected = app_resource_plan(server, name, app)
    if plan_id != expected["plan_id"]:
        raise ValueError("plan_id is invalid or stale; call plan_app_resources again")
    if app.framework == "static":
        return {
            "application": name,
            "changed": False,
            "message": "static frontend applications require no database or cache",
        }
    return runner.run("gimme:provision:app", server, app_name=name, app=app).as_dict()


@mcp.tool(
    description=(
        "Return Deployer's task execution plan for a registered application. Makes no "
        "remote changes and returns the current application plan_id for deploy_app."
    ),
    annotations=titled(READ_ONLY, "Plan application deployment"),
)
def plan_deploy(name: str) -> dict[str, object]:
    server = store.server()
    app = store.app(name)
    resource_plan = app_resource_plan(server, name, app)
    result = runner.run(
        "deploy",
        server,
        app_name=name,
        app=app,
        arguments=("--plan",),
        timeout=60,
    )
    return {
        "plan_id": resource_plan["plan_id"],
        "application": name,
        "deployer_plan": result.output,
    }


@mcp.tool(
    description=(
        "Deploy a registered application from Git using its pinned Deployer recipe. "
        "Requires the current plan_id and may run framework migrations defined by that recipe."
    ),
    annotations=ToolAnnotations(
        title="Deploy application",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def deploy_app(name: str, plan_id: str) -> dict[str, object]:
    server = store.server()
    app = store.app(name)
    expected = app_resource_plan(server, name, app)
    if plan_id != expected["plan_id"]:
        raise ValueError("plan_id is invalid or stale; call plan_deploy again")
    return runner.run("deploy", server, app_name=name, app=app).as_dict()


@mcp.tool(
    description=(
        "List retained releases for a registered application and identify the current "
        "release. Makes no changes."
    ),
    annotations=titled(READ_ONLY, "List application releases"),
)
def list_releases(name: str) -> dict[str, object]:
    server = store.server()
    app = store.app(name)
    return runner.run("releases", server, app_name=name, app=app).as_dict()


@mcp.tool(
    description=(
        "Roll a registered application back to its previous good Deployer release. "
        "Changes the live current symlink and marks the replaced release as bad."
    ),
    annotations=ToolAnnotations(
        title="Rollback application",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def rollback_app(name: str, confirmation: str) -> dict[str, object]:
    if confirmation != f"ROLLBACK {name}":
        raise ValueError(f"confirmation must exactly equal 'ROLLBACK {name}'")
    server = store.server()
    app = store.app(name)
    return runner.run("rollback", server, app_name=name, app=app).as_dict()


@mcp.tool(
    description=(
        "Plan one allowlisted Laravel Artisan command in a registered application's current "
        "release. Validates the command and literal argument vector, makes no changes, and "
        "returns the exact argv plus a plan_id for run_artisan."
    ),
    annotations=titled(READ_ONLY, "Plan Artisan command"),
)
def plan_artisan(
    name: ApplicationName,
    command: ArtisanCommand,
    arguments: ArtisanArguments = None,
) -> dict[str, object]:
    return artisan_command_plan(store.server(), name, store.app(name), command, arguments)


@mcp.tool(
    description=(
        "Execute a previously planned, allowlisted Laravel Artisan command inside the current "
        "release and return capped combined output. Executes application code and may mutate "
        "application, database, cache, queue, or filesystem state; rejects stale plan IDs."
    ),
    annotations=ToolAnnotations(
        title="Run Artisan command",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=True,
    ),
)
def run_artisan(
    name: ApplicationName,
    command: ArtisanCommand,
    plan_id: PlanIdentifier,
    arguments: ArtisanArguments = None,
) -> dict[str, object]:
    server = store.server()
    app = store.app(name)
    normalized_arguments = arguments or []
    expected = artisan_command_plan(server, name, app, command, normalized_arguments)
    if plan_id != expected["plan_id"]:
        raise ValueError("plan_id is invalid or stale; call plan_artisan again")
    if app.artisan is None:
        raise ValueError("Artisan commands require a registered Laravel application")
    return runner.run(
        "gimme:artisan",
        server,
        app_name=name,
        app=app,
        artisan_command=command,
        artisan_arguments=normalized_arguments,
        artisan_allowed_commands=app.artisan.allowed_commands,
        timeout=300,
    ).as_dict()


@mcp.tool(
    description=(
        "Inspect prerequisites and return the exact systemd worker, Horizon, and scheduler "
        "process plan for one registered Laravel application. Makes no changes and returns "
        "a plan_id for provision_app_processes."
    ),
    annotations=titled(READ_ONLY, "Plan application processes"),
)
def plan_app_processes(name: ApplicationName) -> dict[str, object]:
    return resolved_app_process_plan(name)


@mcp.tool(
    description=(
        "Apply a reviewed application process plan by reconciling narrowly generated systemd "
        "queue worker or Horizon units and an optional scheduler timer. Requires an exact, "
        "ready plan from plan_app_processes and never runs processes as root."
    ),
    annotations=titled(IDEMPOTENT_WRITE, "Provision application processes"),
)
def provision_app_processes(
    name: ApplicationName,
    plan_id: PlanIdentifier,
) -> dict[str, object]:
    expected = resolved_app_process_plan(name)
    if plan_id != expected["plan_id"]:
        raise ValueError("plan_id is invalid or stale; call plan_app_processes again")
    if not expected["ready"]:
        blockers = ", ".join(str(value) for value in expected["blockers"])
        raise ValueError(f"application process plan is not ready; blockers: {blockers}")
    server = store.server()
    app = store.app(name)
    return runner.run(
        "gimme:provision:processes",
        server,
        app_name=name,
        app=app,
        timeout=1800,
    ).as_dict()


@mcp.tool(
    description=(
        "Report systemd load, active state, substate, PID, and restart count for the configured "
        "queue workers or Horizon master and scheduler timer of one Laravel application. "
        "Returns no journal or application log content and makes no changes."
    ),
    annotations=titled(READ_ONLY, "Get application process status"),
)
def app_process_status(name: ApplicationName) -> dict[str, object]:
    server = store.server()
    app = store.app(name)
    return runner.run(
        "gimme:processes:status",
        server,
        app_name=name,
        app=app,
        timeout=30,
    ).as_dict()


@mcp.tool(
    description=(
        "Report systemd status for PostgreSQL, Valkey, or Caddy on the configured host. "
        "The service name is restricted to this allowlist and no changes are made."
    ),
    annotations=titled(READ_ONLY, "Get service status"),
)
def service_status(
    service: Literal["postgresql", "valkey-server", "caddy"],
) -> dict[str, object]:
    return runner.run(
        "gimme:service:status",
        store.server(),
        arguments=("-o", f"gimme_service={service}"),
        timeout=30,
    ).as_dict()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
