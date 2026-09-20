import json
from datetime import UTC, datetime
from pathlib import Path

from gimme.control import RecoveryPolicy
from gimme.recovery_schedule import scheduled_request_id
import pytest


ROOT = Path(__file__).resolve().parents[1]


def runner_namespace() -> dict[str, object]:
    source = (ROOT / "scripts" / "gimme-recovery-runner").read_text()
    source = source.replace('"__GIMME_APPS_ROOT__"', '"/srv/gimme/apps"')
    namespace = {"__name__": "gimme_recovery_runner"}
    exec(compile(source, "gimme-recovery-runner", "exec"), namespace)
    return namespace


def test_runner_slot_and_request_match_control_plane() -> None:
    runner = runner_namespace()
    observed = datetime(2026, 9, 20, 9, 20, tzinfo=UTC)
    slot = runner["latest_slot"]({"kind": "hourly", "minute": 15}, observed)
    assert slot == datetime(2026, 9, 20, 9, 15, tzinfo=UTC)
    policy = RecoveryPolicy(destination="primary", cadence={"kind": "hourly", "minute": 15})
    fingerprint = runner["hashlib"].sha256(
        json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert runner["request_id"]("example-app", fingerprint, slot) == scheduled_request_id(
        "example-app", policy, slot
    )


def test_runner_lock_times_out_without_queueing(monkeypatch, tmp_path) -> None:
    runner = runner_namespace()
    monkeypatch.setattr(
        runner["fcntl"], "flock",
        lambda *_args: (_ for _ in ()).throw(BlockingIOError()),
    )
    values = iter([0.0, 0.0, 0.0, 300.0])
    assert runner["acquire_lock"](
        tmp_path / "deployment.lock", monotonic=lambda: next(values), pause=lambda _v: None
    ) is None


def test_status_is_atomic_bounded_and_secret_safe(tmp_path) -> None:
    runner = runner_namespace()
    now = datetime(2026, 9, 20, 9, 15, tzinfo=UTC)
    status = runner["attempt_status"](
        "example-app", now, now, "deployment_busy", finished_at=now,
        error_code="deployment_busy",
    )
    path = tmp_path / "status.json"
    runner["atomic_status"](path, status)
    assert json.loads(path.read_text()) == status
    assert path.stat().st_mode & 0o777 == 0o600

    status["error_code"] = "provider said secret-canary"
    with pytest.raises(runner["RunnerFailure"]):
        runner["atomic_status"](path, status)
    assert "secret-canary" not in path.read_text()
