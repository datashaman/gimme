from __future__ import annotations

from typing import Any

from pydantic import BaseModel

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
            "schema_version": 6,
            "state_directory": state_directory,
            "provider_accounts": sorted(state.provider_accounts),
            "secret_stores": state.model_dump(mode="json")["secret_stores"],
            "artifact_stores": state.model_dump(mode="json")["artifact_stores"],
            "targets": state.model_dump(mode="json")["targets"],
            "applications": state.model_dump(mode="json")["applications"],
            "resources": state.model_dump(mode="json")["resources"],
            "deployments": {
                name: {
                    "application": deployment.application,
                    "target": deployment.target,
                    "stage": deployment.stage,
                    "release_mode": deployment.release_mode,
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
                "write one atomic schema-v6 desired-state document",
                "record every operator-selected release mode without inference",
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
    current: BaseModel,
    proposed: BaseModel,
) -> dict[str, Any]:
    current_value = current.model_dump(mode="json")
    proposed_value = proposed.model_dump(mode="json")
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
    valkey_contract: dict[str, Any] | None = None,
    recovery_schedule: dict[str, object] | None = None,
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
            **({"valkey_contract": valkey_contract} if valkey_contract is not None else {}),
            **({"recovery_schedule": recovery_schedule} if recovery_schedule is not None else {}),
            "secret_versions": secret_versions or [],
            "secret_issues": missing,
            "ready": not missing,
            "effects": [
                "reconcile only this target's registered routes",
                "create isolated PostgreSQL and Valkey resources when applicable",
                "atomically reconcile managed and declared environment values",
                "refresh Laravel caches and managed processes when runtime values change",
                *(
                    ["reconcile the fixed policy-bound Recovery Schedule authority"]
                    if recovery_schedule is not None else []
                ),
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
    artifact: dict[str, object] | None = None,
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
            "release_mode": deployment.release_mode,
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
            "artifact": artifact,
            "runtimes": toolchain,
            "processes": processes,
            "ready": not issues,
            "readiness_issues": issues,
            "deployer_plan": rendered_tasks,
            "effects": [
                (
                    "download and verify the exact reviewed artifact versions on the Target"
                    if deployment.release_mode == "artifact"
                    else "deploy the exact resolved revision"
                ),
                *(
                    [
                        "safely extract and verify the immutable tree before mutable release work",
                        "perform no Git checkout or dependency/frontend build on the Target",
                    ]
                    if deployment.release_mode == "artifact" else []
                ),
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
    policy = deployment.recovery
    if policy is None:
        raise ValueError("deployment has no Recovery Policy bound")
    return exact_plan(
        {
            "kind": "recovery_point_creation",
            "deployment": deployment_name,
            "target": deployment.target,
            "destination": destination_name,
            "destination_policy": destination.model_dump(mode="json"),
            "request_id": request_id,
            "recovery_point_id": point_id,
            "components": [
                "postgres", *(["valkey"] if policy.valkey else [])
            ],
            "quiesce_wait_seconds": policy.quiesce_wait_seconds,
            "cadence": policy.cadence.model_dump(mode="json"),
            "retain_last": policy.retain_last,
            "ready": True,
            "readiness_issues": [],
            "effects": [
                *(
                    [
                        "place only this deployment route into request-owned maintenance",
                        "drain and stop only this deployment's managed writers",
                    ]
                    if policy.valkey else []
                ),
                "capture the deployment's isolated PostgreSQL database",
                *(
                    ["capture only this deployment's registered Valkey prefix"]
                    if policy.valkey else []
                ),
                "exclude roles, ownership, ACLs, and credential material from the dump",
                "upload every checksummed component with server-side encryption",
                "publish one immutable Recovery Manifest only after all components verify",
                *(
                    ["restore managed processes and normal routing after capture"]
                    if policy.valkey else []
                ),
                "make no other remote or destination changes",
            ],
        }
    )


def deployment_restore_plan(
    deployment_name: str, recovery_point_id: str, request_id: str,
    source_components: list[dict[str, object]], destination_resource: str,
    destination_version: str, destination_empty: bool,
    selected_components: list[str],
    safety_components: list[str] | None = None,
    capacity_ready: bool = True,
    valkey_destination: dict[str, str] | None = None,
    request_fingerprint: str | None = None,
    restore_state: str | None = None, request_conflict: bool = False,
    destination_changed: bool = False,
) -> dict[str, Any]:
    available_components = [str(component.get("kind")) for component in source_components]
    untouched_components = [
        component for component in available_components
        if component not in selected_components
    ]
    partial = bool(untouched_components)
    protected_components = (
        ([] if destination_empty else list(selected_components))
        if safety_components is None else safety_components
    )
    postgres = next(
        (component for component in source_components if component.get("kind") == "postgres"),
        None,
    )
    source_version = "" if postgres is None else str(postgres.get("resource_version", ""))
    source_bytes = -1 if postgres is None else postgres.get("bytes", -1)
    valkey = next(
        (component for component in source_components if component.get("kind") == "valkey"),
        None,
    )
    valkey_source_version = "" if valkey is None else str(
        valkey.get("resource_version", "")
    )
    issues = [
        *(
            ["valkey_destination_incompatible"]
            if "valkey" in selected_components and (
                valkey is None or valkey_destination is None
                or valkey_source_version != valkey_destination.get("version")
            ) else []
        ),
        *(
            ["source_version_incompatible"]
            if "postgres" in selected_components
            and source_version != destination_version else []
        ),
        *(
            ["source_artifact_too_large"]
            if "postgres" in selected_components and (
                not isinstance(source_bytes, int)
            or isinstance(source_bytes, bool)
            or not 0 <= source_bytes <= 512 * 1024 * 1024
            ) else []
        ),
        *(["restore_capacity_insufficient"] if not capacity_ready else []),
        *(["restore_request_conflict"] if request_conflict else []),
        *(["restore_destination_changed"] if destination_changed else []),
    ]
    return exact_plan({
        "kind": "deployment_restore",
        "deployment": deployment_name,
        "request_id": request_id,
        "request_fingerprint": request_fingerprint,
        "selected_components": selected_components,
        "untouched_components": untouched_components,
        "partial": partial,
        "safety_components": protected_components,
        "source": {
            "recovery_point_id": recovery_point_id,
            "provider": "target_local", "kind": "postgres", "version": source_version,
        },
        "destination": {
            "resource": destination_resource, "provider": "target_local",
            "kind": "postgres", "version": destination_version,
            "empty": destination_empty,
        },
        "destinations": [
            *([{
                "resource": destination_resource, "provider": "target_local",
                "kind": "postgres", "version": destination_version,
                "empty": destination_empty,
            }] if "postgres" in selected_components else []),
            *([valkey_destination] if "valkey" in selected_components
               and valkey_destination is not None else []),
        ],
        "ready": not issues,
        "readiness_issues": issues,
        "restore_state": restore_state,
        "confirmation": (
            f"PARTIAL RESTORE DEPLOYMENT {deployment_name} FROM {recovery_point_id} "
            f"COMPONENTS {','.join(selected_components)} BREAK CONSISTENCY WITH "
            f"{','.join(untouched_components)}"
            if partial else
            f"RESTORE DEPLOYMENT {deployment_name} FROM {recovery_point_id} "
            f"COMPONENTS {','.join(selected_components)}"
        ),
        "effects": [
            "require bounded controller, application, and PostgreSQL staging capacity",
            "enter request-owned maintenance and stop only managed writers",
            *(
                ["create and verify a protected Safety Recovery Point"]
                if protected_components else []
            ),
            *(
                ["verify the exact PostgreSQL artifact before loading a shadow database",
                 "swap only the deployment database after shadow verification"]
                if "postgres" in selected_components else []
            ),
            *(
                ["verify and replace only the registered Valkey prefix"]
                if "valkey" in selected_components else []
            ),
            *(
                ["leave unselected components untouched and accept intentionally mixed state"]
                if partial else []
            ),
            "restore processes and routing only after post-swap verification",
        ],
    })


def restore_verification_plan(
    deployment_name: str, request_id: str, restore: dict[str, object],
    *, identity_conflict: bool = False,
) -> dict[str, Any]:
    state = str(restore["state"])
    state_ready = state in {
        "data_replaced", "verification_failed", "verification_succeeded",
        "cleanup_completed", "completed",
    }
    ready = state_ready and not identity_conflict
    return exact_plan({
        "kind": "restore_verification",
        "deployment": deployment_name,
        "request_id": request_id,
        "source_recovery_point_id": restore["source_recovery_point_id"],
        "request_fingerprint": restore["request_fingerprint"],
        "destinations": restore["destinations"],
        "selected_components": restore["selected_components"],
        "untouched_components": restore["untouched_components"],
        "partial": restore["partial"],
        "safety_components": restore["safety_components"],
        "state": state,
        "ready": ready,
        "readiness_issues": [
            *([] if state_ready else ["restore_data_not_replaced"]),
            *(["restore_destination_changed"] if identity_conflict else []),
        ],
        "effects": [
            "keep public routing on the fixed maintenance response",
            "resume only the managed processes active before restore",
            "verify restored database connectivity and configured live-health probes privately",
            "re-quiesce managed processes and remain in maintenance on any failure",
            "drop the verified previous database only after private verification",
            "restore normal routing only after cleanup and a final private verification",
        ],
    })


def recovery_point_deletion_plan(
    deployment_name: str,
    destination_name: str,
    point_id: str,
    *,
    components: int,
    bytes: int,
    final_verified_point: bool,
    safety_protected: bool,
    restore_protected: bool,
    inventory_fingerprint: str,
    manifest_fingerprint: str,
    state: str,
) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "recovery_point_deletion",
            "deployment": deployment_name,
            "destination": destination_name,
            "recovery_point_id": point_id,
            "state": state,
            "components": components,
            "bytes": bytes,
            "safety_protected": safety_protected,
            "restore_protected": restore_protected,
            "final_verified_point": final_verified_point,
            "inventory_fingerprint": inventory_fingerprint,
            "manifest_fingerprint": manifest_fingerprint,
            "confirmation": f"DELETE RECOVERY POINT {deployment_name} {point_id}",
            "last_recovery_point_confirmation": (
                f"DELETE LAST RECOVERY POINT {deployment_name} {point_id}"
                if final_verified_point else None
            ),
            "effects": [
                "delete only the exact component versions named by the immutable manifest",
                "verify every exact component version is absent before continuing",
                "delete and verify the exact immutable manifest version last",
                "leave policy, unrelated versions, and every other Recovery Point unchanged",
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


def valkey_destroy_plan(
    resource_name: str, fingerprint: str, final_snapshot: str, users: int
) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "resource_destroy",
            "resource": resource_name,
            "confirmation": f"DESTROY RESOURCE {resource_name}",
            "identity_fingerprint": fingerprint,
            "final_snapshot": final_snapshot,
            "destroys": [
                "the ElastiCache replication group and all its data",
                "its automatic snapshots",
                "the ElastiCache user group",
                f"{users} ElastiCache users created by Gimme",
                "the cache parameter group and cache subnet group created by Gimme",
                "the local Resource registration and observation",
            ],
            "retains": [
                "a final snapshot of the group, which you must delete yourself when done",
                "manual snapshots",
                "the Resource Credential and administrative secrets in Secrets Manager",
            ],
            "authority": "the Provider Account's destructive role, assumed only during apply",
            "irreversible": True,
        }
    )


def valkey_restore_plan(
    resource_name: str, snapshot: str | None, deployments: list[str], engine_version: str
) -> dict[str, Any]:
    """Restore from a named snapshot, or (no snapshot) recreate an empty group. Local state only,
    so the plan is the same before, during, and after a resumed apply."""
    empty = snapshot is None
    return exact_plan(
        {
            "kind": "resource_recreate_empty" if empty else "resource_restore",
            "resource": resource_name,
            "snapshot": snapshot,
            **({"confirmation": f"RECREATE EMPTY RESOURCE {resource_name}"} if empty else {}),
            "engine_version": engine_version,
            "deployments": deployments,
            "effects": [
                "create the replication group again only if it does not exist"
                + (" and hold no data" if empty else " from the snapshot"),
                "recreate the ElastiCache user group and any missing ACL user from the "
                "credentials already in the Secret Store; no credential is rotated",
                "for each recorded Deployment: refresh its environment to the new endpoint, "
                "probe the current release, and restart its workers",
                "the Resource is phase 'restoring' and takes no other operation until every "
                "Deployment has passed; repeat the same call after a failure or a pending group",
            ],
            "authority": "the Provider Account's inspection and resolver roles",
            "irreversible": empty,
        }
    )


def valkey_rotation_plan(
    resource_name: str, deployment: str, fingerprint: str
) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "resource_credential_rotate",
            "resource": resource_name,
            "deployment": deployment,
            "identity_fingerprint": fingerprint,
            "effects": [
                "create a new ACL user for the Deployment and make its credential the secret's "
                "current version, keeping the previous version",
                "refresh the Deployment's environment, probe the current release, and restart "
                "its workers",
                "on success delete the previous ACL user; on any failure restore the previous "
                "credential and delete the new user",
                "a leftover rotation is finished or rolled back by repeating the same call",
            ],
            "authority": "the Provider Account's destructive role, to delete an ACL user",
        }
    )


def resource_forget_plan(resource_name: str) -> dict[str, Any]:
    return exact_plan(
        {
            "kind": "resource_forget",
            "resource": resource_name,
            "confirmation": f"FORGET {resource_name}",
            "effects": [
                "delete the local Retained Resource tombstone only",
                "make no remote changes",
                "the retained infrastructure stays in AWS and cannot be adopted again",
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
