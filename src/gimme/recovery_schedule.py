from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

from gimme.control import (
    CredentialReferenceBackupAuth,
    DeploymentConfig,
    HourlyRecoveryCadence,
    ManualRecoveryCadence,
    RecoveryCadence,
    RecoveryPolicy,
    S3BackupDestination,
    WeeklyRecoveryCadence,
)


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("recovery schedule timestamps must include a timezone")
    return value.astimezone(UTC)


def systemd_calendar(cadence: RecoveryCadence) -> str | None:
    if isinstance(cadence, ManualRecoveryCadence):
        return None
    if isinstance(cadence, HourlyRecoveryCadence):
        return f"*-*-* *:{cadence.minute:02d}:00 UTC"
    if isinstance(cadence, WeeklyRecoveryCadence):
        return f"{cadence.weekday.title()} *-*-* {cadence.hour:02d}:{cadence.minute:02d}:00 UTC"
    return f"*-*-* {cadence.hour:02d}:{cadence.minute:02d}:00 UTC"


def latest_logical_slot(cadence: RecoveryCadence, observed_at: datetime) -> datetime | None:
    observed = _utc(observed_at)
    if isinstance(cadence, ManualRecoveryCadence):
        return None
    if isinstance(cadence, HourlyRecoveryCadence):
        candidate = observed.replace(minute=cadence.minute, second=0, microsecond=0)
        return candidate if candidate <= observed else candidate - timedelta(hours=1)
    candidate = observed.replace(
        hour=cadence.hour, minute=cadence.minute, second=0, microsecond=0
    )
    if isinstance(cadence, WeeklyRecoveryCadence):
        candidate -= timedelta(days=(candidate.weekday() - WEEKDAYS.index(cadence.weekday)) % 7)
        return candidate if candidate <= observed else candidate - timedelta(days=7)
    return candidate if candidate <= observed else candidate - timedelta(days=1)


def next_logical_slot(cadence: RecoveryCadence, observed_at: datetime) -> datetime | None:
    current = latest_logical_slot(cadence, observed_at)
    if current is None:
        return None
    interval = (
        timedelta(hours=1) if isinstance(cadence, HourlyRecoveryCadence)
        else timedelta(days=7) if isinstance(cadence, WeeklyRecoveryCadence)
        else timedelta(days=1)
    )
    return current + interval


def latest_missed_slot(
    cadence: RecoveryCadence, observed_at: datetime, last_logical_slot: datetime | None,
) -> datetime | None:
    latest = latest_logical_slot(cadence, observed_at)
    if latest is None:
        return None
    return latest if last_logical_slot is None or latest > _utc(last_logical_slot) else None


def stable_delay_seconds(deployment: str) -> int:
    digest = hashlib.sha256(f"gimme-recovery-jitter-v1\0{deployment}".encode()).digest()
    return int.from_bytes(digest[:8], "big") % 301


def policy_fingerprint(policy: RecoveryPolicy) -> str:
    encoded = json.dumps(
        policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def scheduled_request_id(
    deployment: str, policy: RecoveryPolicy, logical_slot: datetime,
) -> str:
    slot = _utc(logical_slot).isoformat()
    digest = hashlib.sha256(
        f"gimme-scheduled-recovery-v1\0{deployment}\0{policy_fingerprint(policy)}\0{slot}".encode()
    ).hexdigest()[:20]
    return f"scheduled-{digest}"


def effective_execution(logical_slot: datetime, deployment: str) -> datetime:
    return _utc(logical_slot) + timedelta(seconds=stable_delay_seconds(deployment))


def runner_authority(
    deployment_name: str,
    deployment: DeploymentConfig,
    destination_name: str,
    destination: S3BackupDestination,
    resource_provenance: dict[str, dict[str, str]],
    valkey_execution: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the strict secret-reference-free authority consumed by a target runner."""
    policy = deployment.recovery
    if policy is None or policy.destination != destination_name:
        raise ValueError("Recovery Schedule destination authority mismatch")
    expected_resources = {
        "postgres": deployment.resources.database,
        **(
            {"valkey": deployment.resources.valkey.resource}
            if policy.valkey and deployment.resources.valkey is not None else {}
        ),
    }
    if (
        None in expected_resources.values()
        or set(resource_provenance) != set(expected_resources)
        or any(
            set(item) != {"name", "provider", "kind", "version"}
            or item["name"] != expected_resources[component]
            or item["kind"] != component
            for component, item in resource_provenance.items()
        )
    ):
        raise ValueError("Recovery Schedule resource authority mismatch")
    if policy.valkey:
        if (
            not isinstance(valkey_execution, dict)
            or set(valkey_execution) != {"prefix", "host", "port", "tls", "auth_mode"}
            or valkey_execution.get("auth_mode") not in {"none", "stored"}
        ):
            raise ValueError("Recovery Schedule Valkey execution authority mismatch")
    elif valkey_execution is not None:
        raise ValueError("Recovery Schedule Valkey execution authority mismatch")
    cadence = policy.cadence.model_dump(mode="json")
    authority: dict[str, object] = {
        "schema_version": 2,
        "deployment": deployment_name,
        "target": deployment.target,
        "policy_fingerprint": policy_fingerprint(policy),
        "cadence": cadence,
        "calendar": systemd_calendar(policy.cadence),
        "stable_delay_seconds": stable_delay_seconds(deployment_name),
        "retain_last": policy.retain_last,
        "quiesce_wait_seconds": policy.quiesce_wait_seconds,
        "components": ["postgres", *(["valkey"] if policy.valkey else [])],
        "placement": deployment.placement.model_dump(mode="json"),
        "resources": resource_provenance,
        "destination": {
            "name": destination_name,
            "provider": destination.provider,
            "bucket": destination.bucket,
            "region": destination.region,
            "endpoint": destination.endpoint,
            "addressing": destination.addressing,
            "encryption": destination.encryption.model_dump(mode="json"),
            "auth_mode": (
                "stored"
                if isinstance(destination.auth, CredentialReferenceBackupAuth)
                else "ambient"
            ),
        },
        "status_identity": deployment_name,
        "valkey_execution": valkey_execution,
    }
    return authority


def schedule_plan(authority: dict[str, object]) -> dict[str, object]:
    """Project private runner authority into an inspectable secret-safe plan."""
    deployment = str(authority["deployment"])
    cadence = authority["cadence"]
    enabled = isinstance(cadence, dict) and cadence.get("kind") != "manual"
    destination = authority["destination"]
    if not isinstance(destination, dict) or destination.get("auth_mode") not in {
        "ambient", "stored",
    }:
        raise ValueError("Recovery Schedule destination authority is invalid")
    fingerprint = hashlib.sha256(json.dumps(
        authority, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    return {
        "enabled": enabled,
        "cadence": cadence,
        "calendar": authority["calendar"],
        "logical_timezone": "UTC",
        "stable_delay_seconds": authority["stable_delay_seconds"],
        "policy_fingerprint": authority["policy_fingerprint"],
        "authority_fingerprint": fingerprint,
        "auth_mode": destination["auth_mode"],
        "service": f"gimme-recovery-{deployment}.service" if enabled else None,
        "timer": f"gimme-recovery-{deployment}.timer" if enabled else None,
    }
