from __future__ import annotations

from typing import Any

from gimme.control import (
    ApplicationConfig,
    ControlState,
    DeploymentConfig,
    StateStore,
    TargetConfig,
)
from gimme.execution import execution_fingerprint


def exact_plan(value: dict[str, Any]) -> dict[str, Any]:
    body = {
        "execution_fingerprint": execution_fingerprint(),
        **{
            key: item
            for key, item in value.items()
            if key not in {"plan_id", "execution_fingerprint"}
        },
    }
    return {"plan_id": StateStore.digest(body), **body}


def migration_plan(state: ControlState, state_directory: str) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "state_migration",
            "schema_version": 3,
            "state_directory": state_directory,
            "targets": state.model_dump(mode="json")["targets"],
            "applications": sorted(state.applications),
            "resources": state.model_dump(mode="json")["resources"],
            "deployments": {
                name: {
                    "application": deployment.application,
                    "target": deployment.target,
                    "stage": deployment.stage,
                    "source": deployment.source.model_dump(mode="json"),
                    "site_host": deployment.placement.site_host,
                    "relative_path": deployment.placement.relative_path,
                    "runtimes": {
                        key: value.model_dump(mode="json")
                        for key, value in deployment.runtimes.items()
                    },
                    "resources": deployment.resources.model_dump(mode="json"),
                }
                for name, deployment in sorted(state.deployments.items())
            },
            "effects": [
                "write one atomic schema-v3 desired-state document",
                "pin observed runtime and target-local resource versions explicitly",
                "preserve existing remote paths, identities, databases, cache prefixes, and URLs",
                "leave legacy manifests and every remote target unchanged",
            ],
        }
    )


def registration_update_plan(
    kind: str,
    name: str,
    current: object,
    proposed: object,
) -> dict[str, Any]:
    current_value = current.model_dump(mode="json")  # type: ignore[attr-defined]
    proposed_value = proposed.model_dump(mode="json")  # type: ignore[attr-defined]
    return exact_plan(
        {
            "kind": kind,
            "name": name,
            "current": current_value,
            "proposed": proposed_value,
            "effects": ["replace local Git-backed desired state only", "make no remote changes"],
        }
    )


def target_stack_plan(
    name: str,
    target: TargetConfig,
    resolution: dict[str, dict[str, str]],
    *,
    package_manager_processes: list[int] | None = None,
    privileged_helper: str = "unknown",
    sites: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    busy = package_manager_processes or []
    unavailable = sorted(
        package
        for package, result in resolution.items()
        if result["installed"] == "missing" and result["candidate"] == "unavailable"
    )
    return exact_plan(
        {
            "kind": "target_stack",
            "target": name,
            "bootstrap_host": target.bootstrap_hostname,
            "normal_host": target.hostname,
            "network": target.network.model_dump(mode="json"),
            "runtime_policy": target.runtimes.model_dump(mode="json"),
            "packages": resolution,
            "services": target.stack.services,
            "sites": sites or [],
            "ready": unavailable == [] and busy == [],
            "mcp_apply_ready": unavailable == [] and busy == [] and privileged_helper == "ready",
            "unavailable_packages": unavailable,
            "package_manager_processes": busy,
            "privileged_helper": privileged_helper,
            "effects": [
                "install only declared packages from configured APT sources",
                *(
                    ["enable the fixed official ppa:jdxcode/mise source and verify mise exactly"]
                    if target.runtimes.mise_version is not None else []
                ),
                "enable declared services",
                "reconcile target-specific Caddy sites",
                (
                    "reconcile Avahi aliases and Caddy internal TLS"
                    if target.network.mode == "local_mdns"
                    else "use public DNS names and Caddy automatic ACME TLS"
                ),
                "install target-bound privileged helpers",
            ],
        }
    )


def deployment_resource_plan(
    name: str,
    deployment: DeploymentConfig,
    target: TargetConfig,
    application: ApplicationConfig,
    *,
    missing_secrets: list[str] | None = None,
) -> dict[str, Any]:
    deploy_path = f"{target.apps_root}/{deployment.placement.relative_path}"
    missing = sorted(missing_secrets or [])
    is_static = application.framework == "static"
    return exact_plan(
        {
            "kind": "deployment_resources",
            "deployment": name,
            "application": deployment.application,
            "target": deployment.target,
            "stage": deployment.stage,
            "site_url": f"https://{deployment.placement.site_host}",
            "deploy_path": deploy_path,
            "database": None if is_static else deployment.placement.database_identifier,
            "cache_prefix": None if is_static else deployment.placement.cache_prefix,
            "runtime": {
                "app_env": deployment.app_env,
                "app_debug": deployment.app_debug,
                "pins": {
                    key: value.model_dump(mode="json")
                    for key, value in deployment.runtimes.items()
                },
            },
            "resource_bindings": deployment.resources.model_dump(mode="json"),
            "secret_references": sorted(deployment.secrets.values()),
            "missing_secret_references": missing,
            "ready": not missing,
            "effects": [
                "reconcile only this target's registered routes",
                "create isolated PostgreSQL and Valkey resources when applicable",
                "atomically reconcile managed and declared environment values",
                "refresh Laravel caches and managed processes when runtime values change",
            ],
        }
    )


def deployment_release_plan(
    name: str,
    deployment: DeploymentConfig,
    target: TargetConfig,
    application: ApplicationConfig,
    revision: str,
    rendered_tasks: str,
    toolchain: dict[str, Any] | None = None,
    processes: dict[str, Any] | None = None,
    readiness_issues: list[str] | None = None,
) -> dict[str, Any]:
    health = application.default_health if deployment.health == "inherit" else deployment.health
    issues = readiness_issues or []
    return exact_plan(
        {
            "kind": "deployment_release",
            "deployment": name,
            "application": deployment.application,
            "target": deployment.target,
            "stage": deployment.stage,
            "source": deployment.source.model_dump(mode="json"),
            "revision": revision,
            "site_url": f"https://{deployment.placement.site_host}",
            "deploy_path": f"{target.apps_root}/{deployment.placement.relative_path}",
            "health": health.model_dump(mode="json") if health is not None else None,
            "frontend": (
                application.frontend.model_dump(mode="json")
                if application.frontend is not None
                else None
            ),
            "runtimes": toolchain,
            "processes": processes,
            "ready": not issues,
            "readiness_issues": issues,
            "deployer_plan": rendered_tasks,
            "effects": [
                "deploy the exact resolved revision",
                "gate activation on the candidate health check when configured",
                "switch the current symlink only after the candidate succeeds",
                "roll back automatically if the live health check fails",
                "gracefully refresh managed queue workers after activation",
            ],
        }
    )


def deployment_removal_plan(
    name: str,
    deployment: DeploymentConfig,
    target: TargetConfig,
) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "deployment_removal",
            "deployment": name,
            "application": deployment.application,
            "target": deployment.target,
            "stage": deployment.stage,
            "site_host": deployment.placement.site_host,
            "deploy_path": f"{target.apps_root}/{deployment.placement.relative_path}",
            "confirmation": f"REMOVE {name}",
            "effects": [
                "remove the deployment route and mDNS publisher when applicable",
                "stop and remove its managed processes",
                "remove its database, Valkey namespace, releases, and storage",
                "remove its local desired-state registration after remote cleanup succeeds",
            ],
        }
    )
