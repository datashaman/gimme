"""Explicitly authorized, bounded live AWS RDS PostgreSQL lifecycle proof."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import time
from typing import Callable, Mapping

from gimme.control import AWSRDSPostgresResource, AWSSecretsManagerStore
import gimme.server as gimme


NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
POLL_SECONDS = 30
TIMEOUT_SECONDS = 60 * 60


@dataclass(frozen=True)
class LiveConfiguration:
    resource: str
    deployment: str
    run_id: str
    state_directory: Path
    destroy: bool


def configuration(environ: Mapping[str, str] = os.environ) -> LiveConfiguration:
    if environ.get("GIMME_AWS_RDS_LIVE_CREATE") != "1":
        raise SystemExit(
            "set GIMME_AWS_RDS_LIVE_CREATE=1 to authorize billable RDS creation and rotation"
        )
    destroy_value = environ.get("GIMME_AWS_RDS_LIVE_DESTROY")
    if destroy_value not in (None, "", "1"):
        raise SystemExit("GIMME_AWS_RDS_LIVE_DESTROY must be absent or exactly 1")
    resource = environ.get("GIMME_AWS_RDS_RESOURCE", "")
    deployment = environ.get("GIMME_AWS_RDS_DEPLOYMENT", "")
    run_id = environ.get("GIMME_AWS_RDS_RUN_ID", "")
    if NAME.fullmatch(resource) is None:
        raise SystemExit("GIMME_AWS_RDS_RESOURCE must be a registered bounded name")
    if NAME.fullmatch(deployment) is None:
        raise SystemExit("GIMME_AWS_RDS_DEPLOYMENT must be a registered bounded name")
    if RUN_ID.fullmatch(run_id) is None:
        raise SystemExit("GIMME_AWS_RDS_RUN_ID must be a fresh bounded run id")
    configured = environ.get("GIMME_STATE_DIR", "")
    state_directory = Path(configured)
    if not configured or not state_directory.is_absolute():
        raise SystemExit("GIMME_STATE_DIR must name an absolute isolated state directory")
    repository_state = Path(__file__).resolve().parents[2] / "config"
    if state_directory.resolve() == repository_state.resolve():
        raise SystemExit("the live test refuses the repository's normal config directory")
    return LiveConfiguration(
        resource, deployment, run_id, state_directory, destroy_value == "1"
    )


def wait_for(description: str, operation: Callable[[], dict[str, object]], phase: str):
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        result = operation()
        if result.get("phase") == phase:
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for {description}")
        time.sleep(POLL_SECONDS)


def require_live_context(selected: LiveConfiguration):
    if gimme.store.root.resolve() != selected.state_directory.resolve():
        raise SystemExit("Gimme did not initialize from the authorized isolated state directory")
    state = gimme.store.load()
    resource = state.resources.get(selected.resource)
    deployment = state.deployments.get(selected.deployment)
    if not isinstance(resource, AWSRDSPostgresResource):
        raise SystemExit("the requested Resource is not registered managed PostgreSQL")
    if deployment is None or deployment.resources.database != selected.resource:
        raise SystemExit("the requested Deployment is not bound to the requested Resource")
    network = state.aws_networks[resource.aws_network]
    account = state.provider_accounts[network.provider_account]
    store = state.secret_stores.get(resource.workload_secret_store)
    if not isinstance(store, AWSSecretsManagerStore):
        raise SystemExit("the workload Secret Store is not AWS Secrets Manager")
    if selected.destroy and account.destructive_role_arn is None:
        raise SystemExit("destructive cleanup requires the Provider Account destructive role")
    if selected.destroy and deployment.recovery is None:
        raise SystemExit("destructive cleanup requires a bound Recovery Policy")
    return deployment


def _converge_resource(name: str) -> dict[str, object]:
    def apply_once() -> dict[str, object]:
        plan = gimme.plan_apply_resource(name)
        return gimme.apply_resource(name, str(plan["plan_id"]))

    return wait_for("RDS readiness", apply_once, "ready")


def _bind_activate_rotate(resource: str, deployment: str) -> dict[str, object]:
    binding = gimme.plan_bind_resource(deployment)
    gimme.bind_resource(deployment, str(binding["plan_id"]))
    activation = gimme.plan_deployment_resources(deployment)
    if not activation["ready"]:
        raise RuntimeError("Deployment resources are not ready for live activation")
    gimme.apply_deployment_resources(deployment, str(activation["plan_id"]))
    rotation = gimme.plan_rotate_resource_credential(resource, deployment)
    result = gimme.apply_rotate_resource_credential(
        resource, deployment, str(rotation["plan_id"])
    )
    if not result.get("rotated"):
        raise RuntimeError("live workload credential rotation did not complete")
    return result


def run_live(selected: LiveConfiguration) -> dict[str, object]:
    deployment = require_live_context(selected)
    converged = _converge_resource(selected.resource)
    inspection = gimme.inspect_resource(selected.resource)
    if inspection.get("phase") != "ready" or inspection.get("readiness_issues") != []:
        raise RuntimeError("live RDS inspection did not report ready")
    rotated = _bind_activate_rotate(selected.resource, selected.deployment)
    recovery_point = None
    if deployment.recovery is not None:
        request_id = f"rds-live-{selected.run_id}"
        recovery = gimme.plan_create_recovery_point(selected.deployment, request_id)
        captured = gimme.create_recovery_point(
            selected.deployment, request_id, str(recovery["plan_id"])
        )
        point = captured.get("recovery_point")
        if not isinstance(point, dict) or point.get("state") != "verified":
            raise RuntimeError("live managed PostgreSQL Recovery Point was not verified")
        recovery_point = "verified"

    cleanup = "retained_by_default"
    snapshot = None
    if selected.destroy:
        removal = gimme.plan_remove_deployment(selected.deployment)
        gimme.remove_deployment(
            selected.deployment,
            str(removal["plan_id"]),
            str(removal["confirmation"]),
        )
        destruction = gimme.plan_destroy_resource(selected.resource)

        def destroy_once() -> dict[str, object]:
            return gimme.apply_destroy_resource(
                selected.resource,
                str(destruction["plan_id"]),
                str(destruction["confirmation"]),
            )

        destroyed = wait_for("RDS destruction", destroy_once, "destroyed")
        cleanup = "destroyed_snapshot_retained"
        snapshot = "verified" if destroyed.get("final_snapshot") else None

    report = {
        "state": "passed",
        "resource": selected.resource,
        "deployment": selected.deployment,
        "topology": {
            "multi_az": inspection.get("multi_az"),
            "storage_encrypted": inspection.get("storage_encrypted"),
            "publicly_accessible": inspection.get("publicly_accessible"),
        },
        "readiness": converged.get("phase"),
        "binding": "activated",
        "rotation_generation": rotated.get("generation"),
        "recovery": recovery_point or "not_configured",
        "cleanup": cleanup,
        "final_snapshot": snapshot,
    }
    encoded = json.dumps(report, sort_keys=True)
    if any(word in encoded.lower() for word in ("password", "secret_arn", "endpoint")):
        raise RuntimeError("live output included a forbidden field")
    return report


def main() -> None:
    print(json.dumps(run_live(configuration()), sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
