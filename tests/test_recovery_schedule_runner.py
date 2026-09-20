import json
from types import SimpleNamespace
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


def runner_authority() -> dict[str, object]:
    return {
        "schema_version": 2, "deployment": "example-app", "target": "devbox",
        "policy_fingerprint": "a" * 64,
        "cadence": {"kind": "hourly", "minute": 15},
        "calendar": "*-*-* *:15:00 UTC", "stable_delay_seconds": 10,
        "retain_last": 7, "quiesce_wait_seconds": 30,
        "components": ["postgres", "valkey"],
        "placement": {
            "instance": "example-app", "relative_path": "deployments/example-app",
            "database_identifier": "example_app", "cache_prefix": "gimme:example-app:",
            "site_host": "example.test",
        },
        "resources": {
            "postgres": {
                "name": "primary-db", "provider": "target_local",
                "kind": "postgres", "version": "17.2",
            },
            "valkey": {
                "name": "cache", "provider": "aws_elasticache_valkey",
                "kind": "valkey", "version": "9.0",
            },
        },
        "destination": {
            "name": "primary", "provider": "s3_compatible", "bucket": "backups",
            "region": "us-east-1", "endpoint": None, "addressing": "virtual_hosted",
            "encryption": {"method": "AES256"}, "auth_mode": "ambient",
        },
        "status_identity": "example-app",
        "valkey_execution": {
            "prefix": "{gimme:example-app}:", "host": "cache.example.test",
            "port": 6379, "tls": True, "auth_mode": "stored",
        },
    }


def test_runner_executes_postgres_and_valkey_through_shared_capture_core(tmp_path) -> None:
    runner = runner_namespace()
    calls = []

    class Core:
        class BotoObjectStore:
            def __init__(self, destination, credentials):
                calls.append(("store", destination["name"], credentials))

        @staticmethod
        def recovery_point_id(deployment, destination, request):
            calls.append(("identity", deployment, destination, request))
            return "rp_" + "a" * 20

        @staticmethod
        def capture_postgres(database, version, directory):
            path = directory / "postgres.dump"
            path.write_bytes(b"pg")
            calls.append(("postgres", database, version))
            return SimpleNamespace(path=path)

        @staticmethod
        def capture_valkey(prefix, host, port, tls, version, directory, credential):
            path = directory / "valkey.dump"
            path.write_bytes(b"vk")
            calls.append(("valkey", prefix, host, port, tls, version, credential))
            return SimpleNamespace(path=path)

        @staticmethod
        def publish(store, deployment, destination, point_id, components, observed_at=None):
            calls.append(("publish", deployment, destination, point_id, len(components)))
            return {"recovery_point_id": point_id, "components": [1, 2]}

    credential = tmp_path / "valkey.json"
    credential.write_text('{"username":"admin","password":"secret"}')
    result = runner["capture_recovery"](
        runner_authority(), "scheduled-abc", tmp_path, Core,
        valkey_credential_path=credential,
    )

    assert result["recovery_point_id"] == "rp_" + "a" * 20
    assert [item[0] for item in calls] == [
        "store", "identity", "postgres", "valkey", "publish",
    ]
    assert not (tmp_path / "postgres.dump").exists()
    assert not (tmp_path / "valkey.dump").exists()


def test_runner_credential_loader_rejects_unbounded_or_malformed_values(tmp_path) -> None:
    runner = runner_namespace()
    path = tmp_path / "credential.json"
    path.write_text(json.dumps({"username": "admin", "password": "secret"}))
    path.chmod(0o600)
    assert runner["load_credential"](path, {"username", "password"}) == {
        "username": "admin", "password": "secret",
    }

    path.write_text(json.dumps({"username": "admin", "password": "secret\ncanary"}))
    with pytest.raises(runner["RunnerFailure"], match="^credentials_unavailable$"):
        runner["load_credential"](path, {"username", "password"})


def test_runner_capture_failure_never_returns_provider_or_secret_text(tmp_path) -> None:
    runner = runner_namespace()

    class Core:
        class BotoObjectStore:
            def __init__(self, _destination, _credentials):
                raise RuntimeError("provider leaked secret-canary")

    with pytest.raises(runner["RunnerFailure"]) as failure:
        runner["capture_recovery"](
            runner_authority(), "scheduled-abc", tmp_path, Core,
            valkey_credential_path=tmp_path / "valkey.json",
        )
    assert str(failure.value) == "capture_failed"
    assert "secret-canary" not in str(failure.value)
