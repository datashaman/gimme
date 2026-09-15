from __future__ import annotations

import hashlib
import json
from typing import Any

from gimme.config import AppConfig, ArtisanInvocation, ServerConfig, StackConfig


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
            name: {
                "url": f"https://{name}.{server.mdns_name}.local",
                "document_root": (
                    f"{server.apps_root}/{name}/current/{app.frontend.output_dir}"
                    if app.framework == "static" and app.frontend is not None
                    else f"{server.apps_root}/{name}/current/public"
                    if app.framework in {"laravel", "symfony"}
                    else f"{server.apps_root}/{name}/current"
                ),
                "tls": "caddy-local-ca",
            }
            for name, app in sorted(apps.items())
        },
        "ready": unavailable == [] and package_manager_processes == [],
        "unavailable_packages": unavailable,
        "package_manager_processes": package_manager_processes,
        "privileged_helper": privileged_helper,
        "mcp_apply_ready": (
            unavailable == []
            and package_manager_processes == []
            and privileged_helper == "ready"
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
    server: ServerConfig, name: str, app: AppConfig
) -> dict[str, Any]:
    identifier = name.replace("-", "_")
    is_static = app.framework == "static"
    plan: dict[str, Any] = {
        "kind": "app_resources",
        "host": server.hostname,
        "application": name,
        "database": None if is_static else f"gimme_{identifier}",
        "database_role": None if is_static else f"gimme_{identifier}",
        "cache": None
        if is_static
        else {
            "engine": "valkey",
            "endpoint": "127.0.0.1:6379",
            "prefix": f"gimme:{name}:",
            "isolation": "namespace only",
        },
        "environment_file": None
        if is_static
        else f"{server.apps_root}/{name}/shared/.env",
        "repository": app.repository,
        "framework": app.framework,
        "branch": app.branch,
        "frontend": app.frontend.model_dump() if app.frontend is not None else None,
        "site_url": f"https://{name}.{server.mdns_name}.local",
    }
    return {"plan_id": plan_id(plan), **plan}


def deployment_plan(
    server: ServerConfig, name: str, app: AppConfig
) -> dict[str, Any]:
    site_url = f"https://{name}.{server.mdns_name}.local"
    health: dict[str, Any] | None = None
    if app.health is not None:
        settings = app.health.model_dump()
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
        "repository": app.repository,
        "branch": app.branch,
        "framework": app.framework,
        "frontend": app.frontend.model_dump() if app.frontend is not None else None,
        "workers": app.workers.model_dump() if app.workers is not None else None,
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


def artisan_command_plan(
    server: ServerConfig,
    name: str,
    app: AppConfig,
    command: str,
    arguments: list[str] | None = None,
) -> dict[str, Any]:
    if app.framework != "laravel" or app.artisan is None:
        raise ValueError("Artisan commands require a registered Laravel application")
    invocation = ArtisanInvocation(command=command, arguments=arguments or [])
    if invocation.command not in app.artisan.allowed_commands:
        raise ValueError(
            f"Artisan command '{invocation.command}' is not allowlisted for application "
            f"'{name}'"
        )
    working_directory = f"{server.apps_root}/{name}/current"
    plan: dict[str, Any] = {
        "kind": "artisan_command",
        "host": server.hostname,
        "application": name,
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
    *,
    helper: str,
    current_release: str,
    pcntl: str,
    posix: str,
    horizon: str,
) -> dict[str, Any]:
    if app.framework != "laravel":
        raise ValueError("managed application processes require a Laravel application")

    worker: dict[str, Any] | None = None
    workers_enabled = app.workers is not None and app.workers.enabled
    if workers_enabled and app.workers is not None:
        if app.workers.driver == "queue":
            worker = {
                "driver": "queue",
                "units": [
                    f"gimme-worker-{name}@{index}.service"
                    for index in range(1, app.workers.processes + 1)
                ],
                "argv": [
                    "/usr/bin/php",
                    "artisan",
                    "queue:work",
                    app.workers.connection,
                    f"--queue={','.join(app.workers.queues)}",
                    f"--sleep={app.workers.sleep_seconds}",
                    f"--tries={app.workers.tries}",
                    f"--timeout={app.workers.timeout_seconds}",
                    f"--memory={app.workers.memory_mb}",
                    f"--max-time={app.workers.max_time_seconds}",
                    f"--max-jobs={app.workers.max_jobs}",
                    f"--backoff={app.workers.backoff_seconds}",
                    "--no-interaction",
                ],
                "stop_wait_seconds": app.workers.timeout_seconds + 30,
            }
        else:
            worker = {
                "driver": "horizon",
                "unit": f"gimme-horizon-{name}.service",
                "argv": ["/usr/bin/php", "artisan", "horizon"],
                "stop_wait_seconds": app.workers.stop_wait_seconds,
            }

    scheduler_enabled = app.scheduler is not None and app.scheduler.enabled
    scheduler = (
        {
            "service": f"gimme-scheduler-{name}.service",
            "timer": f"gimme-scheduler-{name}.timer",
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
        "working_directory": f"{server.apps_root}/{name}/current",
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
                    "set QUEUE_CONNECTION=redis in the protected shared environment "
                    "and clear cached Laravel configuration"
                ]
                if worker is not None and worker["driver"] == "horizon"
                else []
            ),
        ],
    }
    return {"plan_id": plan_id(plan), **plan}
