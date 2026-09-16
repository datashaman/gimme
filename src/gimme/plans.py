from __future__ import annotations

import hashlib
import json
from typing import Any

from gimme.config import AppConfig, ArtisanInvocation, ServerConfig, StackConfig


def environment_deploy_path(server: ServerConfig, name: str, environment: str = "default") -> str:
    base = f"{server.apps_root}/{name}"
    return base if environment == "default" else f"{base}/environments/{environment}"


def environment_site_url(server: ServerConfig, name: str, environment: str = "default") -> str:
    prefix = name if environment == "default" else f"{environment}.{name}"
    return f"https://{prefix}.{server.mdns_name}.local"


def environment_instance(name: str, environment: str = "default") -> str:
    if environment == "default":
        return name
    digest = hashlib.sha256(f"{name}\0{environment}".encode()).hexdigest()[:10]
    return f"{name}--{environment}--{digest}"


def environment_database_identifier(name: str, environment: str = "default") -> str:
    parts = ["gimme", name.replace("-", "_")]
    if environment != "default":
        parts.append(environment.replace("-", "_"))
    candidate = "_".join(parts)
    if environment == "default" and len(candidate) <= 63:
        return candidate
    digest = hashlib.sha256(f"{name}\0{environment}".encode()).hexdigest()[:10]
    return f"{candidate[:52]}_{digest}"


def application_update_plan(
    server: ServerConfig,
    name: str,
    current: AppConfig,
    proposed: AppConfig,
) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "kind": "application_registration_update",
        "host": server.hostname,
        "application": name,
        "current": current.model_dump(mode="json"),
        "proposed": proposed.model_dump(mode="json"),
        "affected_environments": sorted(current.environments),
        "warnings": (
            [
                "APP_DEBUG=true can expose sensitive diagnostics to the local network "
                "for the default environment"
            ]
            if proposed.environment("default").app_debug
            else []
        ),
        "effects": [
            "replace the local application registration only",
            "use the proposed repository and default-environment branch in future plans",
            "preserve additional environment registrations",
            "make no remote host changes",
        ],
    }
    return {"plan_id": plan_id(plan), **plan}


def environment_update_plan(
    server: ServerConfig,
    name: str,
    environment: str,
    current: AppConfig,
    proposed: AppConfig,
) -> dict[str, Any]:
    if environment == "default":
        raise ValueError("use plan_update_app for the default environment")
    plan: dict[str, Any] = {
        "kind": "environment_registration_update",
        "host": server.hostname,
        "application": name,
        "environment": environment,
        "current": current.environment(environment).model_dump(mode="json"),
        "proposed": proposed.environment(environment).model_dump(mode="json"),
        "warnings": (
            [
                "APP_DEBUG=true can expose sensitive diagnostics to the local network "
                f"for environment {environment}"
            ]
            if proposed.environment(environment).app_debug
            else []
        ),
        "effects": [
            "replace the local environment registration only",
            "use the proposed branch and policies in future plans",
            "reuse the environment database, cache namespace, storage, and releases",
            "make no remote host changes",
        ],
    }
    return {"plan_id": plan_id(plan), **plan}


def plan_id(plan: dict[str, Any]) -> str:
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    return "plan_" + hashlib.sha256(encoded).hexdigest()[:20]


def stack_plan(
    server: ServerConfig,
    stack: StackConfig,
    package_resolution: dict[str, dict[str, str]],
    package_manager_processes: list[int] | None = None,
    apps: dict[str, AppConfig] | None = None,
    privileged_helper: str = "unknown",
) -> dict[str, Any]:
    package_manager_processes = package_manager_processes or []
    apps = apps or {}
    unavailable = sorted(
        name
        for name, details in package_resolution.items()
        if details["candidate"] == "unavailable" and details["installed"] == "missing"
    )
    plan: dict[str, Any] = {
        "kind": "stack",
        "host": server.bootstrap_hostname,
        "managed_hostname": server.hostname,
        "mdns_hostname": f"{server.mdns_name}.local",
        "remote_user": server.remote_user,
        "package_manager": stack.package_manager,
        "packages": package_resolution,
        "services": stack.services,
        "apps_root": server.apps_root,
        "sites": {
            (name if environment == "default" else f"{name}/{environment}"): {
                "url": environment_site_url(server, name, environment),
                "document_root": (
                    f"{environment_deploy_path(server, name, environment)}/current/"
                    f"{app.frontend.output_dir}"
                    if app.framework == "static" and app.frontend is not None
                    else f"{environment_deploy_path(server, name, environment)}/current/public"
                    if app.framework in {"laravel", "symfony"}
                    else f"{environment_deploy_path(server, name, environment)}/current"
                ),
                "tls": "caddy-local-ca",
            }
            for name, app in sorted(apps.items())
            for environment in sorted(app.environments)
        },
        "ready": unavailable == [] and package_manager_processes == [],
        "unavailable_packages": unavailable,
        "package_manager_processes": package_manager_processes,
        "privileged_helper": privileged_helper,
        "mcp_apply_ready": (
            unavailable == [] and package_manager_processes == [] and privileged_helper == "ready"
        ),
        "effects": [
            "update APT package indexes",
            "install missing packages",
            "enable and start every configured systemd service",
            f"set the machine hostname to {server.mdns_name} for mDNS advertisement",
            "advertise each registered application hostname through Avahi",
            "configure each registered application as a Caddy HTTPS site",
            "create the application root owned by the SSH user",
        ],
    }
    return {"plan_id": plan_id(plan), **plan}


def app_resource_plan(
    server: ServerConfig,
    name: str,
    app: AppConfig,
    environment: str = "default",
) -> dict[str, Any]:
    definition = app.environment(environment)
    identifier = environment_database_identifier(name, environment)
    is_static = app.framework == "static"
    deploy_path = environment_deploy_path(server, name, environment)
    plan: dict[str, Any] = {
        "kind": "app_resources",
        "host": server.hostname,
        "application": name,
        "environment": environment,
        "database": None if is_static else identifier,
        "database_role": None if is_static else identifier,
        "cache": None
        if is_static
        else {
            "engine": "valkey",
            "endpoint": "127.0.0.1:6379",
            "prefix": (
                f"gimme:{name}:" if environment == "default" else f"gimme:{name}:{environment}:"
            ),
            "isolation": "namespace only",
        },
        "environment_file": None if is_static else f"{deploy_path}/shared/.env",
        "repository": app.repository,
        "framework": app.framework,
        "branch": definition.branch,
        "runtime": None
        if app.framework != "laravel"
        else {
            "app_env": definition.app_env,
            "app_debug": definition.app_debug,
            "warning": (
                "APP_DEBUG=true can expose sensitive diagnostics to the local network"
                if definition.app_debug
                else None
            ),
        },
        "frontend": app.frontend.model_dump() if app.frontend is not None else None,
        "site_url": environment_site_url(server, name, environment),
        "effects": [
            "reconcile the environment Caddy route and Avahi publisher",
            *(
                []
                if is_static
                else [
                    "create the isolated PostgreSQL database and role",
                    "write the protected shared environment file",
                    "assign an isolated Valkey key prefix",
                    *(
                        [
                            "atomically reconcile the declared Laravel APP_ENV and "
                            "APP_DEBUG values",
                            "refresh cached Laravel configuration and gracefully refresh "
                            "managed processes when runtime values change",
                        ]
                        if app.framework == "laravel"
                        else []
                    ),
                ]
            ),
        ],
    }
    return {"plan_id": plan_id(plan), **plan}


def deployment_plan(
    server: ServerConfig,
    name: str,
    app: AppConfig,
    environment: str = "default",
    *,
    revision: str | None = None,
) -> dict[str, Any]:
    definition = app.environment(environment)
    site_url = environment_site_url(server, name, environment)
    deploy_path = environment_deploy_path(server, name, environment)
    effective_health = app.effective_health(environment)
    health: dict[str, Any] | None = None
    if effective_health is not None:
        settings = effective_health.model_dump()
        common = {
            "expected_status": settings["expected_status"],
            "attempts": settings["attempts"],
            "delay_seconds": settings["delay_seconds"],
            "timeout_seconds": settings["timeout_seconds"],
        }
        health = {
            "pre_activation": {
                "target": "candidate_release",
                "path": settings["path"],
                **common,
                "failure": "prevent_symlink_switch",
            },
            "post_activation": {
                "target": f"{site_url}{settings['path']}",
                **common,
                "failure": "rollback_previous_release",
            },
        }
    plan: dict[str, Any] = {
        "kind": "application_deploy",
        "host": server.hostname,
        "application": name,
        "environment": environment,
        "repository": app.repository,
        "branch": definition.branch,
        "revision": revision,
        "deploy_path": deploy_path,
        "framework": app.framework,
        "frontend": app.frontend.model_dump() if app.frontend is not None else None,
        "workers": (definition.workers.model_dump() if definition.workers is not None else None),
        "site_url": site_url,
        "health": health,
        "effects": [
            "prepare a new immutable release",
            "run configured candidate health gate before switching current",
            "atomically switch current only after the candidate is healthy",
            "run configured live HTTPS health gate after activation",
            "rollback the symlink automatically if the live gate fails",
            "gracefully restart configured workers after successful activation",
        ],
    }
    return {"plan_id": plan_id(plan), **plan}


def environment_removal_plan(
    server: ServerConfig, name: str, app: AppConfig, environment: str
) -> dict[str, Any]:
    if environment == "default":
        raise ValueError("the default environment cannot be removed")
    definition = app.environment(environment)
    identifier = environment_database_identifier(name, environment)
    plan: dict[str, Any] = {
        "kind": "environment_removal",
        "host": server.hostname,
        "application": name,
        "environment": environment,
        "branch": definition.branch,
        "site_url": environment_site_url(server, name, environment),
        "deploy_path": environment_deploy_path(server, name, environment),
        "database": None if app.framework == "static" else identifier,
        "database_role": None if app.framework == "static" else identifier,
        "cache_prefix": (None if app.framework == "static" else f"gimme:{name}:{environment}:"),
        "effects": [
            "remove the environment Caddy route and Avahi publisher",
            "stop and remove environment worker and scheduler units",
            *(
                []
                if app.framework == "static"
                else [
                    "drop the isolated PostgreSQL database and role",
                    "unlink keys only beneath the isolated Valkey prefix",
                ]
            ),
            "remove the validated environment deploy directory",
            "remove the local environment registration after remote cleanup succeeds",
        ],
    }
    return {"plan_id": plan_id(plan), **plan}


def artisan_command_plan(
    server: ServerConfig,
    name: str,
    app: AppConfig,
    command: str,
    arguments: list[str] | None = None,
    environment: str = "default",
) -> dict[str, Any]:
    if app.framework != "laravel" or app.artisan is None:
        raise ValueError("Artisan commands require a registered Laravel application")
    invocation = ArtisanInvocation(command=command, arguments=arguments or [])
    if invocation.command not in app.artisan.allowed_commands:
        raise ValueError(
            f"Artisan command '{invocation.command}' is not allowlisted for application '{name}'"
        )
    app.environment(environment)
    working_directory = f"{environment_deploy_path(server, name, environment)}/current"
    plan: dict[str, Any] = {
        "kind": "artisan_command",
        "host": server.hostname,
        "application": name,
        "environment": environment,
        "working_directory": working_directory,
        "argv": [
            "php",
            "artisan",
            "--no-interaction",
            invocation.command,
            *invocation.arguments,
        ],
        "effects": [
            "execute allowlisted Laravel application code in the current release",
            "return capped combined stdout and stderr through MCP",
        ],
    }
    return {"plan_id": plan_id(plan), **plan}


def app_process_plan(
    server: ServerConfig,
    name: str,
    app: AppConfig,
    environment: str = "default",
    *,
    helper: str,
    current_release: str,
    pcntl: str,
    posix: str,
    horizon: str,
) -> dict[str, Any]:
    if app.framework != "laravel":
        raise ValueError("managed application processes require a Laravel application")

    definition = app.environment(environment)
    instance = environment_instance(name, environment)
    worker: dict[str, Any] | None = None
    workers_enabled = definition.workers is not None and definition.workers.enabled
    if workers_enabled and definition.workers is not None:
        if definition.workers.driver == "queue":
            worker = {
                "driver": "queue",
                "units": [
                    f"gimme-worker-{instance}@{index}.service"
                    for index in range(1, definition.workers.processes + 1)
                ],
                "argv": [
                    "/usr/bin/php",
                    "artisan",
                    "queue:work",
                    definition.workers.connection,
                    f"--queue={','.join(definition.workers.queues)}",
                    f"--sleep={definition.workers.sleep_seconds}",
                    f"--tries={definition.workers.tries}",
                    f"--timeout={definition.workers.timeout_seconds}",
                    f"--memory={definition.workers.memory_mb}",
                    f"--max-time={definition.workers.max_time_seconds}",
                    f"--max-jobs={definition.workers.max_jobs}",
                    f"--backoff={definition.workers.backoff_seconds}",
                    "--no-interaction",
                ],
                "stop_wait_seconds": definition.workers.timeout_seconds + 30,
            }
        else:
            worker = {
                "driver": "horizon",
                "unit": f"gimme-horizon-{instance}.service",
                "argv": ["/usr/bin/php", "artisan", "horizon"],
                "stop_wait_seconds": definition.workers.stop_wait_seconds,
            }

    scheduler_enabled = definition.scheduler is not None and definition.scheduler.enabled
    scheduler = (
        {
            "service": f"gimme-scheduler-{instance}.service",
            "timer": f"gimme-scheduler-{instance}.timer",
            "argv": ["/usr/bin/php", "artisan", "--no-interaction", "schedule:run"],
            "calendar": "*-*-* *:*:00",
        }
        if scheduler_enabled
        else None
    )

    blockers: list[str] = []
    if helper != "ready":
        blockers.append("privileged_helper")
    if (workers_enabled or scheduler_enabled) and current_release != "ready":
        blockers.append("current_release")
    if workers_enabled and pcntl != "ready":
        blockers.append("pcntl")
    if worker is not None and worker["driver"] == "horizon" and posix != "ready":
        blockers.append("posix")
    if worker is not None and worker["driver"] == "horizon" and horizon != "ready":
        blockers.append("horizon")

    plan: dict[str, Any] = {
        "kind": "app_processes",
        "host": server.hostname,
        "application": name,
        "environment": environment,
        "working_directory": f"{environment_deploy_path(server, name, environment)}/current",
        "worker": worker,
        "scheduler": scheduler,
        "preflight": {
            "privileged_helper": helper,
            "current_release": current_release,
            "pcntl": pcntl,
            "posix": posix,
            "horizon": horizon,
        },
        "ready": blockers == [],
        "blockers": blockers,
        "effects": [
            "reconcile root-owned systemd units for this application",
            "run configured processes as the deployment user, never root",
            "disable obsolete Gimme-managed process units for this application",
            *(
                [
                    "set QUEUE_CONNECTION=redis and an isolated HORIZON_PREFIX in the "
                    "protected shared environment, then clear cached Laravel configuration"
                ]
                if worker is not None and worker["driver"] == "horizon"
                else []
            ),
        ],
    }
    return {"plan_id": plan_id(plan), **plan}
