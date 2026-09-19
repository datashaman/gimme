from __future__ import annotations

from typing import Any

from gimme.control import (
    ApplicationConfig,
    AWSElastiCacheValkeyResource,
    AWSRDSPostgresResource,
    ControlState,
    DeploymentConfig,
    S3BackupDestination,
    StateStore,
    TargetConfig,
)
from gimme.execution import execution_fingerprint
from gimme.resources_postgres import desired_security_group_ids


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
            "schema_version": 5,
            "state_directory": state_directory,
            "provider_accounts": sorted(state.provider_accounts),
            "secret_stores": state.model_dump(mode="json")["secret_stores"],
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
                "write one atomic schema-v5 desired-state document",
                "migrate local secret references to the fixed local-sops store",
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
    secret_versions: list[dict[str, str]] | None = None,
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
            "secret_versions": secret_versions or [],
            "secret_issues": missing,
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
    primary = application.default_health if deployment.health == "inherit" else deployment.health
    health = [*([primary] if primary is not None else []), *application.health_probes,
              *deployment.health_probes]
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
            "health": [probe.model_dump(mode="json") for probe in health],
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


def recovery_point_creation_plan(
    deployment_name: str,
    deployment: DeploymentConfig,
    destination_name: str,
    destination: S3BackupDestination,
    request_id: str,
    point_id: str,
) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "recovery_point_creation",
            "deployment": deployment_name,
            "target": deployment.target,
            "destination": destination_name,
            "destination_policy": destination.model_dump(mode="json"),
            "request_id": request_id,
            "recovery_point_id": point_id,
            "components": ["postgres"],
            "effects": [
                "run a transactionally consistent pg_dump of the deployment's isolated database",
                "exclude roles, ownership, ACLs, and credential material from the dump",
                "upload the checksummed component with server-side encryption",
                "publish the immutable Recovery Manifest only after verification succeeds",
                "make no other remote or destination changes",
            ],
        }
    )


def resource_provision_plan(
    resource_name: str,
    resource: AWSRDSPostgresResource,
    observed: dict[str, Any] | None,
) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "resource_provision",
            "resource": resource_name,
            "aws_network": resource.aws_network,
            "administration_target": resource.administration_target,
            "engine_version": resource.engine_version,
            "instance_class": resource.instance_class,
            "allocated_storage_gb": resource.allocated_storage_gb,
            "security_group_ids": list(desired_security_group_ids(resource)),
            "current_phase": observed["phase"] if observed is not None else "absent",
            "effects": [
                "create the RDS instance, its DB subnet group, and its DB parameter group "
                "(rds.force_ssl=1) when absent",
                "converge an existing instance with one immediate modification of only the "
                "fields that differ: same-major engine version, instance class, an increased "
                "storage size, security groups, and the Resource-owned parameter group; a "
                "storage decrease, version downgrade, or major mismatch is refused before "
                "any change",
                "disruption: an instance-class change fails over a Multi-AZ instance, and an "
                "engine version change or parameter group attach restarts the instance",
                "reboot the instance once, without forced failover, when its parameter group "
                "reports pending-reboot and no modification is pending",
                "poll for at most 30 seconds and return a bounded pending phase if not yet ready",
                "never returns, stores, or logs a decrypted credential",
            ],
        }
    )


def valkey_provision_plan(
    resource_name: str,
    resource: AWSElastiCacheValkeyResource,
    observed: dict[str, Any] | None,
) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "resource_provision",
            "resource": resource_name,
            "aws_network": resource.aws_network,
            "administration_target": resource.administration_target,
            "engine_version": resource.engine_version,
            "node_type": resource.node_type,
            "security_group_id": resource.security_group_id,
            "snapshot_window": resource.snapshot_window,
            "snapshot_retention_days": resource.snapshot_retention_days,
            "maintenance_window": resource.maintenance_window,
            "current_phase": observed["phase"] if observed is not None else "absent",
            "effects": [
                "create, when absent, a cache subnet group, a parameter group "
                "(cluster-enabled yes, maxmemory-policy noeviction), a user group whose default "
                "user cannot authenticate, an administrative user, and one replication group "
                "with one shard, one cross-AZ replica, Multi-AZ automatic failover, TLS, "
                "encryption at rest, synchronous durability, and no automatic minor upgrades",
                "write the administrative user's generated password to the workload Secret "
                "Store; it is never returned, stored locally, or logged",
                "converge an existing replication group with one immediate modification of "
                "only the fields that differ: same-major engine version, node type, snapshot "
                "retention and window, and maintenance window; a major mismatch, version "
                "downgrade, node type outside the modifications AWS allows, or any other live "
                "difference from the contract is refused before any change",
                "disruption: an engine version or node type change replaces nodes one at a "
                "time and may fail over the primary, briefly interrupting connections",
                "report the Resource degraded, with fixed reason codes, when an available "
                "group does not meet the durability, topology, encryption, authentication, "
                "snapshot, or maintenance contract, or has an overdue required service update",
                "poll for at most 30 seconds and return a bounded pending phase if not yet ready",
            ],
        }
    )


def resource_binding_plan(
    deployment_name: str,
    deployment: DeploymentConfig,
    resource_name: str,
    observed: dict[str, Any] | None,
) -> dict[str, Any]:
    allocations = observed["allocations"] if observed is not None else {}
    return exact_plan(
        {
            "kind": "resource_binding",
            "deployment": deployment_name,
            "resource": resource_name,
            "database": deployment.placement.database_identifier,
            "resource_ready": observed is not None and observed["phase"] == "ready",
            "already_bound": deployment_name in allocations,
            "effects": [
                "create or reconcile the deployment's isolated database and role through "
                "the Administration Target",
                "create or rotate a tagged Secrets Manager workload secret",
                "never returns, stores, or logs the workload credential",
            ],
        }
    )


def valkey_binding_plan(
    deployment_name: str, resource_name: str, uses: list[str], namespaces: dict[str, str],
    profile: str, observed: dict[str, Any] | None, database: dict[str, Any] | None,
) -> dict[str, Any]:
    allocations = observed["allocations"] if observed is not None else {}
    return exact_plan(
        {
            "kind": "resource_binding",
            "deployment": deployment_name,
            "database": database,
            "valkey": {
                "resource": resource_name,
                "uses": uses,
                "namespaces": namespaces,
                "acl_profile": profile,
                "resource_ready": observed is not None and observed["phase"] == "ready",
                "already_bound": deployment_name in allocations,
            },
            "effects": [
                "create or reconcile this Deployment's own ElastiCache ACL user, limited to its "
                "derived key and channel namespace and the fixed Gimme-owned laravel command "
                "profile, and add it to the Resource's user group",
                "write a Resource Credential secret holding exactly a username and a generated "
                "48-character password to the workload Secret Store; an existing credential is "
                "kept",
                "refuse a Resource that a fresh live read does not report ready, degraded "
                "included",
                "never edit the Valkey security group, and never return, store, or log the "
                "credential",
            ],
        }
    )


def resource_cleanup_plan(
    resource_name: str, *, managed: bool, subject: str = "RDS instance"
) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "resource_cleanup",
            "resource": resource_name,
            "confirmation": f"RETAIN {resource_name}",
            "effects": (
                [
                    "remove local desired-state registration only",
                    f"leave the {subject} and its data intact as a Retained Resource",
                    "write a secret-free Retained Resource tombstone",
                ]
                if managed
                else [
                    "remove local desired-state registration only",
                    "make no remote changes",
                ]
            ),
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
