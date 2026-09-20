from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json

from gimme.control import (
    HourlyRecoveryCadence,
    ManualRecoveryCadence,
    RecoveryCadence,
    RecoveryPolicy,
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
