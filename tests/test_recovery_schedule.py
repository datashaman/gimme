from datetime import UTC, datetime, timedelta, timezone

import pytest

from gimme.control import RecoveryPolicy
from gimme.recovery_schedule import (
    effective_execution,
    latest_logical_slot,
    latest_missed_slot,
    next_logical_slot,
    policy_fingerprint,
    scheduled_request_id,
    stable_delay_seconds,
    systemd_calendar,
)


def policy(cadence: dict[str, object], **updates) -> RecoveryPolicy:
    return RecoveryPolicy(destination="primary", cadence=cadence, **updates)


@pytest.mark.parametrize(
    ("cadence", "calendar"),
    [
        ({"kind": "manual"}, None),
        ({"kind": "hourly", "minute": 0}, "*-*-* *:00:00 UTC"),
        ({"kind": "hourly", "minute": 59}, "*-*-* *:59:00 UTC"),
        ({"kind": "daily", "hour": 0, "minute": 0}, "*-*-* 00:00:00 UTC"),
        ({"kind": "daily", "hour": 23, "minute": 59}, "*-*-* 23:59:00 UTC"),
        (
            {"kind": "weekly", "weekday": "sun", "hour": 2, "minute": 0},
            "Sun *-*-* 02:00:00 UTC",
        ),
    ],
)
def test_systemd_calendars_are_exact_utc(cadence, calendar) -> None:
    assert systemd_calendar(policy(cadence).cadence) == calendar


@pytest.mark.parametrize(
    ("cadence", "observed", "latest", "next_slot"),
    [
        ({"kind": "manual"}, "2026-09-20T10:00:00+00:00", None, None),
        (
            {"kind": "hourly", "minute": 15}, "2026-09-20T10:14:59+00:00",
            "2026-09-20T09:15:00+00:00", "2026-09-20T10:15:00+00:00",
        ),
        (
            {"kind": "hourly", "minute": 15}, "2026-09-20T10:15:00+00:00",
            "2026-09-20T10:15:00+00:00", "2026-09-20T11:15:00+00:00",
        ),
        (
            {"kind": "daily", "hour": 2, "minute": 0}, "2026-09-20T01:59:59+00:00",
            "2026-09-19T02:00:00+00:00", "2026-09-20T02:00:00+00:00",
        ),
        (
            {"kind": "weekly", "weekday": "sun", "hour": 2, "minute": 0},
            "2026-09-20T01:59:59+00:00", "2026-09-13T02:00:00+00:00",
            "2026-09-20T02:00:00+00:00",
        ),
    ],
)
def test_logical_slot_boundaries(cadence, observed, latest, next_slot) -> None:
    selected = policy(cadence).cadence
    instant = datetime.fromisoformat(observed)

    assert latest_logical_slot(selected, instant) == (
        None if latest is None else datetime.fromisoformat(latest)
    )
    assert next_logical_slot(selected, instant) == (
        None if next_slot is None else datetime.fromisoformat(next_slot)
    )


def test_schedule_converts_to_utc_and_rejects_naive_time() -> None:
    cadence = policy({"kind": "daily", "hour": 2}).cadence
    observed = datetime(2026, 9, 20, 4, tzinfo=timezone(timedelta(hours=2)))

    assert latest_logical_slot(cadence, observed) == datetime(2026, 9, 20, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone"):
        latest_logical_slot(cadence, datetime(2026, 9, 20, 2))


def test_latest_missed_slot_coalesces_to_only_the_newest_slot() -> None:
    cadence = policy({"kind": "hourly", "minute": 10}).cadence
    observed = datetime(2026, 9, 20, 10, 55, tzinfo=UTC)

    assert latest_missed_slot(cadence, observed, None) == datetime(
        2026, 9, 20, 10, 10, tzinfo=UTC
    )
    assert latest_missed_slot(
        cadence, observed, datetime(2026, 9, 20, 9, 10, tzinfo=UTC)
    ) == datetime(2026, 9, 20, 10, 10, tzinfo=UTC)
    assert latest_missed_slot(
        cadence, observed, datetime(2026, 9, 20, 10, 10, tzinfo=UTC)
    ) is None


def test_jitter_and_request_identity_are_stable_bounded_and_policy_scoped() -> None:
    first = policy({"kind": "daily"})
    changed = policy({"kind": "daily"}, retain_last=8)
    slot = datetime(2026, 9, 20, 2, tzinfo=UTC)

    delay = stable_delay_seconds("example-live")
    assert 0 <= delay <= 300
    assert delay == stable_delay_seconds("example-live")
    assert effective_execution(slot, "example-live") == slot + timedelta(seconds=delay)
    request = scheduled_request_id("example-live", first, slot)
    assert request.startswith("scheduled-") and len(request) == 30
    assert request == scheduled_request_id("example-live", first, slot)
    assert request == scheduled_request_id(
        "example-live", first, slot.astimezone(timezone(timedelta(hours=2)))
    )
    assert request != scheduled_request_id("example-live", changed, slot)
    assert policy_fingerprint(first) != policy_fingerprint(changed)
