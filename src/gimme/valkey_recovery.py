"""Recovery for a managed ElastiCache Valkey Resource: restore a lost replication group, and
rotate one Deployment's Resource Credential.

Both are resumable. Progress lives in a local marker that also makes every other operation on
the Resource refuse, and the Deployment-side work (refresh `.env`, probe the current release,
restart workers) is a caller-supplied `verify`/`switch` callable, so nothing here touches a
host or returns a credential."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, cast

from gimme.control import (
    AWSElastiCacheValkeyResource,
    AWSNetwork,
    AWSProviderAccount,
    AWSSecretsManagerStore,
)
from gimme.resources_postgres import (
    POLL_BUDGET_SECONDS,
    POLL_INTERVAL_SECONDS,
    ResourceError,
    _observed_path,
    _write_json,
)
from gimme.resources_valkey import (
    ElastiCacheAdapter,
    MARKERS,
    _group_document,
    _validate_observed,
    _version_order,
    binding_username,
    clear_marker,
    derive_binding_user_id,
    derive_group_id,
    group_phase,
    load_observed,
    read_marker,
    refuse_while_busy,
    structural_issues,
    write_marker,
)


def _allocations(root: Path, resource_name: str) -> dict[str, dict[str, object]]:
    observed = load_observed(root, resource_name)
    return dict(cast(dict[str, dict[str, object]], observed["allocations"])) if observed else {}


def _save_allocations(
    root: Path, resource_name: str, allocations: dict[str, dict[str, object]]
) -> None:
    observed = load_observed(root, resource_name)
    if observed is None:
        raise ResourceError("observed_resource_invalid")
    _write_json(
        _observed_path(root, resource_name),
        _validate_observed({**observed, "allocations": allocations}), resource_name,
    )


def restore_targets(root: Path, resource_name: str) -> dict[str, tuple[str, int]]:
    """Every recorded Deployment credential a restored group must authenticate again."""
    return {
        deployment: (str(item["user_id"]), int(cast(int, item.get("generation", 1))))
        for deployment, item in _allocations(root, resource_name).items()
        if item["status"] == "active"
    }


def apply_restore(
    adapter: ElastiCacheAdapter, root: Path, account: AWSProviderAccount, network: AWSNetwork,
    resource: AWSElastiCacheValkeyResource, resource_name: str, store: AWSSecretsManagerStore,
    store_name: str, snapshot_name: str | None, verify: Callable[[str], None],
    *, sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Create the replication group again, from a named snapshot of this Resource or (with no
    snapshot) empty, only when it does not exist. Every recorded Deployment credential is
    restored to its existing password, never rotated. The Resource stays phase 'restoring'
    until `verify` has passed for each of those Deployments, and a later call resumes exactly
    where this one stopped; a failed verification is retried by repeating the call."""
    refuse_while_busy(root, resource_name, allow="restoring")
    group_id = derive_group_id(resource_name)
    previous = load_observed(root, resource_name)
    marker = read_marker(root, "restoring", resource_name)
    live = adapter.describe_group(account, network, group_id)
    if live is not None and marker is None:
        raise ResourceError("aws_elasticache_restore_group_exists")
    if live is not None and marker is not None and marker.get("snapshot") != snapshot_name:
        raise ResourceError("aws_elasticache_restore_snapshot_mismatch")
    if live is None:
        if snapshot_name is None and previous is None:
            raise ResourceError("aws_elasticache_recreate_not_needed")
        if snapshot_name is not None:
            _check_snapshot(adapter, account, network, group_id, resource, snapshot_name)
        marker = {
            "schema_version": 1, "resource": resource_name, "snapshot": snapshot_name,
            "verified": [],
        }
        write_marker(root, "restoring", resource_name, marker)
        live = adapter.create_group(
            account, network, resource, resource_name, group_id, store, store_name,
            snapshot_name=snapshot_name, restore_users=restore_targets(root, resource_name),
        )
        # The real adapter returns nothing: AWS accepting the create is the acknowledgement.
        if live is None:
            live = adapter.describe_group(account, network, group_id)
            if live is None:
                raise ResourceError("aws_elasticache_group_missing_after_create")
    marker = cast(dict[str, object], marker)  # a live group is only reached with a marker
    deadline = now() + POLL_BUDGET_SECONDS
    while live.status not in ("available", "create-failed") and now() < deadline:
        sleep(POLL_INTERVAL_SECONDS)
        refreshed = adapter.describe_group(account, network, group_id)
        if refreshed is None:
            raise ResourceError("aws_elasticache_group_disappeared")
        live = refreshed
    issues = structural_issues(resource, live, group_id)
    allocations = cast(dict[str, object], _allocations(root, resource_name))
    document = _group_document(resource_name, group_id, live, issues, allocations)
    if live.status == "create-failed":
        clear_marker(root, "restoring", resource_name)
        _write_json(
            _observed_path(root, resource_name), _validate_observed(document), resource_name
        )
        raise ResourceError("aws_elasticache_restore_create_failed")
    # Not ready for Deployments until each one has been proven against the restored group.
    _write_json(
        _observed_path(root, resource_name),
        _validate_observed({**document, "phase": "restoring"}), resource_name,
    )
    result: dict[str, object] = {
        "resource": resource_name, "snapshot": snapshot_name, "restored": False,
        "phase": "restoring", "status": live.status,
    }
    # A detached allocation has no live credential or Deployment to prove.
    active = sorted(name for name, item in _allocations(root, resource_name).items()
                    if item["status"] == "active")
    if live.status != "available":
        return {**result, "verified": [], "pending": active}
    verified = list(cast(list[str], marker.get("verified") or []))
    for deployment in active:
        if deployment in verified:
            continue
        try:
            verify(deployment)
        except Exception:
            # Only the registered name is kept, so `inspect_resource` can say which one failed.
            write_marker(root, "restoring", resource_name, {**marker, "verified": verified,
                                                            "failed": deployment})
            raise ResourceError("aws_elasticache_restore_verification_failed") from None
        verified.append(deployment)
        write_marker(root, "restoring", resource_name, {**marker, "verified": verified})
    _write_json(_observed_path(root, resource_name), _validate_observed(document), resource_name)
    clear_marker(root, "restoring", resource_name)
    return {**result, "restored": True, "phase": document["phase"], "verified": verified,
            "pending": []}


def _check_snapshot(
    adapter: ElastiCacheAdapter, account: AWSProviderAccount, network: AWSNetwork,
    group_id: str, resource: AWSElastiCacheValkeyResource, snapshot_name: str,
) -> None:
    found = next(
        (item for item in adapter.list_snapshots(account, network, group_id)
         if item.name == snapshot_name), None,
    )
    if found is None:
        raise ResourceError("aws_elasticache_restore_snapshot_missing")
    if found.status != "available":
        raise ResourceError("aws_elasticache_restore_snapshot_unavailable")
    if found.engine_version and _version_order(resource.engine_version, found.engine_version) < 0:
        # A snapshot cannot be restored onto an older engine than took it.
        raise ResourceError("aws_elasticache_restore_engine_older")


def _rollback(
    adapter: ElastiCacheAdapter, root: Path, account: AWSProviderAccount, network: AWSNetwork,
    store: AWSSecretsManagerStore, resource_name: str, deployment: str, marker: dict[str, object],
    switch: Callable[[str], None],
) -> None:
    """Put the previous credential back in service and delete the candidate. Idempotent, so a
    rollback that stopped halfway is finished by repeating it."""
    allocations = _allocations(root, resource_name)
    recorded = allocations[deployment]
    try:
        arn, version = adapter.restore_credential(
            account, store, resource_name, deployment, str(marker["from_username"])
        )
        if version != recorded["secret_version_id"]:
            switch(deployment)
        adapter.remove_user(account, network, resource_name, str(marker["to_user"]))
    except Exception:
        # The marker stays, so repeating the call finishes the rollback.
        raise ResourceError("aws_elasticache_rotate_rollback_failed") from None
    allocations[deployment] = {**recorded, "secret_arn": arn, "secret_version_id": version}
    _save_allocations(root, resource_name, allocations)
    clear_marker(root, "rotating", resource_name)


def _finish_rotation(
    adapter: ElastiCacheAdapter, root: Path, account: AWSProviderAccount, network: AWSNetwork,
    resource_name: str, deployment: str, marker: dict[str, object],
) -> None:
    allocations = _allocations(root, resource_name)
    allocations[deployment] = {
        **allocations[deployment],
        "user_id": marker["to_user"], "secret_arn": marker["to_arn"],
        "secret_version_id": marker["to_version"], "status": "active",
        "generation": marker["generation"],
    }
    _save_allocations(root, resource_name, allocations)
    adapter.remove_user(account, network, resource_name, str(marker["from_user"]))
    clear_marker(root, "rotating", resource_name)


def apply_rotation(
    adapter: ElastiCacheAdapter, root: Path, account: AWSProviderAccount, network: AWSNetwork,
    resource: AWSElastiCacheValkeyResource, resource_name: str, store: AWSSecretsManagerStore,
    store_name: str, deployment: str, switch: Callable[[str], None],
) -> dict[str, object]:
    """Replace one Deployment's ACL user and Resource Credential with a new generation, without
    a window in which the credential in use is invalid: the candidate user exists and is a group
    member before the secret points at it, `switch` (refresh `.env`, probe the current release,
    restart workers) proves it, and only then is the previous user deleted. A failed switch puts
    the previous credential back. A leftover marker is resolved by this call alone: a switch
    that succeeded is finished, anything else is rolled back, and the caller plans again to
    start a new rotation."""
    if account.destructive_role_arn is None:
        raise ResourceError("aws_elasticache_destroy_role_missing")
    refuse_while_busy(root, resource_name, allow="rotating")
    allocations = _allocations(root, resource_name)
    if deployment not in allocations:
        raise ResourceError("aws_elasticache_rotate_binding_missing")
    marker = read_marker(root, "rotating", resource_name)
    if marker is not None:
        if marker.get("deployment") != deployment:
            raise ResourceError(MARKERS["rotating"])
        if marker.get("phase") == "cleanup":
            _finish_rotation(adapter, root, account, network, resource_name, deployment, marker)
            return {"resource": resource_name, "deployment": deployment, "rotated": True,
                    "resumed": True}
        _rollback(adapter, root, account, network, store, resource_name, deployment, marker,
                  switch)
        return {"resource": resource_name, "deployment": deployment, "rotated": False,
                "resumed": True}
    group_id = derive_group_id(resource_name)
    live = adapter.describe_group(account, network, group_id)
    if live is None or group_phase(live, structural_issues(resource, live, group_id)) != "ready":
        raise ResourceError("aws_elasticache_rotate_resource_not_ready")
    current = allocations[deployment]
    generation = int(cast(int, current.get("generation", 1))) + 1
    marker = {
        "schema_version": 1, "resource": resource_name, "deployment": deployment,
        "phase": "switching", "from_user": current["user_id"],
        "from_username": binding_username(deployment, generation - 1),
        "to_user": derive_binding_user_id(group_id, deployment, generation),
        "generation": generation,
    }
    write_marker(root, "rotating", resource_name, marker)
    try:
        user_id, arn, version = adapter.begin_rotation(
            account, network, store, store_name, resource_name, group_id, deployment, generation
        )
    except ResourceError as exc:
        if str(exc) == "aws_elasticache_rotate_candidate_exists":
            # Not created by this rotation, so it is neither rolled back nor deleted.
            clear_marker(root, "rotating", resource_name)
            raise
        _rollback(adapter, root, account, network, store, resource_name, deployment, marker,
                  switch)
        raise
    try:
        switch(deployment)
    except Exception:
        _rollback(adapter, root, account, network, store, resource_name, deployment, marker,
                  switch)
        raise ResourceError("aws_elasticache_rotate_switch_failed") from None
    write_marker(root, "rotating", resource_name, {
        **marker, "phase": "cleanup", "to_user": user_id, "to_arn": arn, "to_version": version,
    })
    _finish_rotation(
        adapter, root, account, network, resource_name, deployment,
        {**marker, "to_user": user_id, "to_arn": arn, "to_version": version},
    )
    return {"resource": resource_name, "deployment": deployment, "rotated": True,
            "resumed": False, "generation": generation}
