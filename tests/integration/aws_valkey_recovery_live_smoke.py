"""Opt-in live recovery and credential-rotation test for managed ElastiCache Valkey.

The operator supplies an isolated, already-registered state directory containing one live
Deployment bound to one managed Valkey Resource. Gimme performs every normal lifecycle action.
The test calls the narrow provider adapter once to delete the exact registered group with an
exact final snapshot, simulating out-of-band loss without adding such a destructive MCP tool.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Callable, Mapping

from gimme.control import AWSElastiCacheValkeyResource, AWSSecretsManagerStore
from gimme.resources_postgres import ResourceError
from gimme import resources_valkey as resources_valkey_module
import gimme.server as gimme


NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
POLL_SECONDS = 30
TIMEOUT_SECONDS = 45 * 60


@dataclass(frozen=True)
class LiveConfiguration:
    resource: str
    deployment: str
    run_id: str
    state_directory: Path


def configuration(environ: Mapping[str, str] = os.environ) -> LiveConfiguration:
    if environ.get("GIMME_AWS_VALKEY_LIVE_CREATE") != "1":
        raise SystemExit(
            "set GIMME_AWS_VALKEY_LIVE_CREATE=1 to authorize live AWS creation and rotation"
        )
    if environ.get("GIMME_AWS_VALKEY_LIVE_DESTROY") != "1":
        raise SystemExit(
            "set GIMME_AWS_VALKEY_LIVE_DESTROY=1 to authorize exact live AWS deletion"
        )
    resource = environ.get("GIMME_AWS_VALKEY_RESOURCE", "")
    deployment = environ.get("GIMME_AWS_VALKEY_DEPLOYMENT", "")
    run_id = environ.get("GIMME_AWS_VALKEY_RUN_ID", "")
    if NAME.fullmatch(resource) is None:
        raise SystemExit("GIMME_AWS_VALKEY_RESOURCE must be a registered bounded name")
    if NAME.fullmatch(deployment) is None:
        raise SystemExit("GIMME_AWS_VALKEY_DEPLOYMENT must be a registered bounded name")
    if RUN_ID.fullmatch(run_id) is None:
        raise SystemExit("GIMME_AWS_VALKEY_RUN_ID must be a fresh bounded run id")
    configured = environ.get("GIMME_STATE_DIR", "")
    state_directory = Path(configured)
    if not configured or not state_directory.is_absolute():
        raise SystemExit("GIMME_STATE_DIR must name an absolute isolated state directory")
    repository_state = Path(__file__).resolve().parents[2] / "config"
    if state_directory.resolve() == repository_state.resolve():
        raise SystemExit("the live test refuses the repository's normal config directory")
    return LiveConfiguration(resource, deployment, run_id, state_directory)


def wait_for(description: str, observe: Callable[[], object], complete: Callable[[object], bool]):
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        observed = observe()
        if complete(observed):
            return observed
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for {description}")
        time.sleep(POLL_SECONDS)


def require_live_context(selected: LiveConfiguration):
    if gimme.store.root.resolve() != selected.state_directory.resolve():
        raise SystemExit("Gimme did not initialize from the authorized isolated state directory")
    state = gimme.store.load()
    resource = state.resources.get(selected.resource)
    deployment = state.deployments.get(selected.deployment)
    if not isinstance(resource, AWSElastiCacheValkeyResource):
        raise SystemExit("the requested Resource is not registered managed Valkey")
    if deployment is None or getattr(deployment.resources.valkey, "resource", None) != (
        selected.resource
    ):
        raise SystemExit("the requested Deployment is not bound to the requested Resource")
    network = state.aws_networks[resource.aws_network]
    account = state.provider_accounts[network.provider_account]
    secret_store = state.secret_stores.get(resource.workload_secret_store)
    if account.destructive_role_arn is None:
        raise SystemExit("the Provider Account has no destructive role")
    if not isinstance(secret_store, AWSSecretsManagerStore):
        raise SystemExit("the Resource workload Secret Store is not AWS Secrets Manager")
    return resource, network, account


def converge_resource(resource: str) -> None:
    def apply_once():
        plan = gimme.plan_apply_resource(resource)
        return gimme.apply_resource(resource, str(plan["plan_id"]))

    result = wait_for(
        "managed Valkey Resource readiness", apply_once,
        lambda item: isinstance(item, dict) and item.get("phase") == "ready",
    )
    if not isinstance(result, dict) or result.get("issues") != []:
        raise RuntimeError("managed Valkey Resource did not converge without issues")


def bind_and_activate(deployment: str) -> None:
    binding = gimme.plan_bind_resource(deployment)
    gimme.bind_resource(deployment, str(binding["plan_id"]))
    activation = gimme.plan_deployment_resources(deployment)
    if not activation["ready"]:
        raise RuntimeError("Deployment resources are not ready for live credential verification")
    gimme.apply_deployment_resources(deployment, str(activation["plan_id"]))


def rotate_credential(resource: str, deployment: str) -> dict[str, object]:
    for _attempt in range(3):
        plan = gimme.plan_rotate_resource_credential(resource, deployment)
        if "password" in json.dumps(plan).lower():
            raise RuntimeError("rotation plan exposed a credential field")
        result = gimme.apply_rotate_resource_credential(
            resource, deployment, str(plan["plan_id"])
        )
        if "password" in json.dumps(result).lower():
            raise RuntimeError("rotation result exposed a credential field")
        if result.get("rotated"):
            return result
    raise RuntimeError("live credential rotation did not complete after bounded recovery")


def loss_snapshot_name(selected: LiveConfiguration) -> str:
    digest = hashlib.sha256(
        f"{selected.resource}\0{selected.run_id}".encode()
    ).hexdigest()[:20]
    return f"gimme-live-loss-{digest}"


def snapshot_status(resource: str, snapshot: str) -> str | None:
    found = gimme.list_resource_snapshots(resource)["snapshots"]
    match = next((item for item in found if item["name"] == snapshot), None)
    return None if match is None else str(match["status"])


def live_marker_path(selected: LiveConfiguration) -> Path:
    digest = hashlib.sha256(
        f"{selected.resource}\0{selected.run_id}".encode()
    ).hexdigest()[:20]
    return gimme.store.root / "live-tests" / f"valkey-recovery-{digest}.json"


def live_marker(selected: LiveConfiguration) -> dict[str, object] | None:
    path = live_marker_path(selected)
    if not path.is_file() or path.is_symlink():
        return None
    if path.stat().st_size > 4096:
        raise RuntimeError("the live recovery marker is invalid")
    try:
        marker = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("the live recovery marker is invalid") from None
    if not isinstance(marker, dict):
        raise RuntimeError("the live recovery marker is invalid")
    expected = {
        "schema_version": 1,
        "resource": selected.resource,
        "run_id": selected.run_id,
        "snapshot": loss_snapshot_name(selected),
    }
    if (
        set(marker) != {*expected, "phase"}
        or any(marker.get(key) != value for key, value in expected.items())
        or marker.get("phase") not in {"deleting", "restoring", "cleanup"}
    ):
        raise RuntimeError("the live recovery marker does not match this exact run")
    return marker


def save_live_marker(selected: LiveConfiguration, phase: str) -> None:
    if phase not in {"deleting", "restoring", "cleanup"}:
        raise RuntimeError("the live recovery marker phase is invalid")
    path = live_marker_path(selected)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    document = {
        "schema_version": 1,
        "resource": selected.resource,
        "run_id": selected.run_id,
        "snapshot": loss_snapshot_name(selected),
        "phase": phase,
    }
    descriptor, temporary = tempfile.mkstemp(prefix=".valkey-recovery-", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def clear_live_marker(selected: LiveConfiguration) -> None:
    live_marker_path(selected).unlink(missing_ok=True)


def simulate_loss_and_restore(selected: LiveConfiguration) -> dict[str, object]:
    resource, network, account = require_live_context(selected)
    group_id = resources_valkey_module.derive_group_id(selected.resource)
    snapshot = loss_snapshot_name(selected)
    status = snapshot_status(selected.resource, snapshot)
    live = gimme.elasticache_valkey.describe_group(account, network, group_id)
    marker = live_marker(selected)
    if marker is None and status is not None:
        raise RuntimeError("the run id is not fresh and has no matching local recovery marker")
    if marker is not None and marker["phase"] == "cleanup" and status is None:
        clear_live_marker(selected)
        return {"phase": "ready", "verified": [selected.deployment], "resumed": True}
    if status is None and live is not None and live.status == "available":
        # Test-only fault injection. The public control plane intentionally has no operation that
        # deletes a still-bound group while preserving its desired state and allocation journal.
        save_live_marker(selected, "deleting")
        gimme.elasticache_valkey.delete_group(account, network, group_id, snapshot)
        live = wait_for(
            "simulated group loss",
            lambda: gimme.elasticache_valkey.describe_group(account, network, group_id),
            lambda item: item is None,
        )
    elif status is None and (live is None or live.status != "deleting"):
        raise RuntimeError("the group is unavailable without this run's recovery snapshot")
    elif live is not None and live.status not in {"available", "deleting", "creating"}:
        raise RuntimeError("the group is in an unsupported state for live recovery")
    wait_for(
        "loss snapshot availability", lambda: snapshot_status(selected.resource, snapshot),
        lambda item: item == "available",
    )
    save_live_marker(selected, "restoring")
    live = gimme.elasticache_valkey.describe_group(account, network, group_id)
    if live is not None and live.status == "deleting":
        live = wait_for(
            "simulated group loss",
            lambda: gimme.elasticache_valkey.describe_group(account, network, group_id),
            lambda item: item is None,
        )

    operation = resources_valkey_module.busy_operation(gimme.store.root, selected.resource)
    if live is None:
        normal = gimme.plan_apply_resource(selected.resource)
        try:
            gimme.apply_resource(selected.resource, str(normal["plan_id"]))
        except ResourceError as exc:
            if str(exc) != "aws_elasticache_group_missing_replace_explicitly":
                raise RuntimeError(
                    f"lost-group safeguard returned unexpected code: {exc}"
                ) from exc
        else:
            raise RuntimeError("ordinary apply silently recreated a lost group")

    if live is None or operation == "restoring":
        plan = gimme.plan_restore_resource(selected.resource, snapshot)

        def restore_once():
            return gimme.apply_restore_resource(
                selected.resource, snapshot, str(plan["plan_id"])
            )

        restored = wait_for(
            "snapshot restore and Deployment verification", restore_once,
            lambda item: isinstance(item, dict) and item.get("phase") == "ready",
        )
    else:
        restored = {"phase": "ready", "verified": [selected.deployment], "resumed": True}
    if not isinstance(restored, dict) or selected.deployment not in restored.get("verified", []):
        raise RuntimeError("restored Resource did not verify the bound Deployment")
    current = gimme.plan_deployment_resources(selected.deployment)
    if not current["ready"] or any(
        item["status"] != "current" for item in current["secret_versions"]
    ):
        raise RuntimeError("restored Deployment did not retain its current credential")

    save_live_marker(selected, "cleanup")
    if not gimme.elasticache_valkey.delete_final_snapshot(account, network, snapshot):
        raise RuntimeError("the exact test snapshot disappeared before cleanup")
    wait_for(
        "test snapshot deletion", lambda: snapshot_status(selected.resource, snapshot),
        lambda item: item is None,
    )
    clear_live_marker(selected)
    return restored


def main() -> None:
    selected = configuration()
    _resource, network, account = require_live_context(selected)
    group_id = resources_valkey_module.derive_group_id(selected.resource)
    live = gimme.elasticache_valkey.describe_group(account, network, group_id)
    recovery_pending = (
        live_marker(selected) is not None
        or live is not None and live.status == "deleting"
        or resources_valkey_module.busy_operation(
            gimme.store.root, selected.resource
        ) == "restoring"
    )
    rotated: dict[str, object] = {"generation": None}
    if not recovery_pending:
        converge_resource(selected.resource)
        bind_and_activate(selected.deployment)
        rotated = rotate_credential(selected.resource, selected.deployment)
    restored = simulate_loss_and_restore(selected)
    inspection = gimme.inspect_resource(selected.resource)
    if inspection.get("phase") != "ready" or inspection.get("issues") != []:
        raise RuntimeError("Resource was not ready after the live recovery matrix")
    print(json.dumps({
        "deployment": selected.deployment,
        "resource": selected.resource,
        "rotation_generation": rotated.get("generation"),
        "restore_phase": restored.get("phase"),
        "state": "passed",
    }, sort_keys=True))


if __name__ == "__main__":
    main()
