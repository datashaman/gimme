from __future__ import annotations

import hashlib
import json
from typing import Any

from gimme.config import AppConfig, ServerConfig, StackConfig


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
