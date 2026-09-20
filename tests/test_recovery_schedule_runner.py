import json
from types import SimpleNamespace
from datetime import UTC, datetime
from pathlib import Path

from gimme.control import RecoveryPolicy
from gimme.recovery_schedule import (
    policy_fingerprint, scheduled_request_id, stable_delay_seconds,
)
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


def test_systemd_credential_boundary_does_not_reapply_source_inode_rules(tmp_path) -> None:
    runner = runner_namespace()
    target = tmp_path / "target"
    target.write_text('{"access_key_id":"id","secret_access_key":"secret"}')
    target.chmod(0o644)
    credential = tmp_path / "aws"
    credential.symlink_to(target)

    with pytest.raises(runner["RunnerFailure"], match="^credentials_unavailable$"):
        runner["load_credential"](
            credential, {"access_key_id", "secret_access_key"},
        )
    assert runner["load_credential"](
        credential, {"access_key_id", "secret_access_key"}, systemd=True,
    ) == {"access_key_id": "id", "secret_access_key": "secret"}


def runner_authority() -> dict[str, object]:
    policy = RecoveryPolicy(
        destination="primary", valkey=True,
        cadence={"kind": "hourly", "minute": 15},
    )
    return {
        "schema_version": 2, "deployment": "example-app", "target": "devbox",
        "policy_fingerprint": policy_fingerprint(policy),
        "cadence": {"kind": "hourly", "minute": 15},
        "calendar": "*-*-* *:15:00 UTC",
        "stable_delay_seconds": stable_delay_seconds("example-app"),
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
            "encryption": {"method": "aes256"}, "auth_mode": "ambient",
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
        def publish(
            store, deployment, destination, point_id, components, observed_at=None,
            before_publish=None,
        ):
            if before_publish is not None:
                before_publish()
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


def test_scheduled_execution_records_verified_point_and_retention(tmp_path) -> None:
    runner = runner_namespace()
    authority = runner_authority()
    closed = []
    runner["acquire_lock"] = lambda _path: SimpleNamespace(
        close=lambda: closed.append(True)
    )
    runner["capture_recovery"] = lambda *args, **kwargs: {
        "recovery_point_id": "rp_" + "a" * 20
    }
    runner["maintenance"] = lambda *_args, **_kwargs: None

    class Core:
        class BotoObjectStore:
            def __init__(self, _destination, _credentials):
                pass

        @staticmethod
        def recovery_point_id(_deployment, _destination, _request):
            return "rp_" + "a" * 20

        @staticmethod
        def find_recovery_point(_store, _deployment, _destination, _point_id):
            return None

        @staticmethod
        def enforce_retention(_store, _deployment, _destination, retain, replacement):
            assert retain == 7 and replacement == "rp_" + "a" * 20
            return {"outcome": "succeeded", "error_code": None, "deleted": 2,
                    "remaining": 7}

    status_path = tmp_path / "status.json"
    result = runner["execute_scheduled"](
        authority, Core, status_path, tmp_path / "lock", tmp_path / "capture",
        valkey_credential_path=tmp_path / "valkey.json",
        observed_at=datetime(2026, 9, 20, 10, 20, tzinfo=UTC),
    )

    assert result["outcome"] == "succeeded"
    assert result["recovery_point_id"] == "rp_" + "a" * 20
    assert result["retention_deleted"] == 2
    assert json.loads(status_path.read_text()) == result
    assert closed == [True]


def test_runner_maintenance_uses_only_fixed_helper_and_protected_request(
    tmp_path, monkeypatch
) -> None:
    runner = runner_namespace()
    runner["EXPECTED_APPS_ROOT"] = tmp_path
    calls = []

    def execute(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0)

    authority = runner_authority()
    runner["maintenance"]("enter", authority, "scheduled-abc", execute=execute)
    request = tmp_path / ".gimme/recovery-requests/example-app.json"
    assert json.loads(request.read_text()) == {
        "schema_version": 1, "deployment": "example-app",
        "request_id": "scheduled-abc", "quiesce_wait_seconds": 30,
    }
    assert request.stat().st_mode & 0o777 == 0o600
    runner["maintenance"]("exit", authority, "scheduled-abc", execute=execute)
    assert not request.exists()
    assert calls[0][0] == [
        "/usr/bin/sudo", "-n", "/usr/local/sbin/gimme-recovery-maintenance",
        "enter", "example-app", "scheduled-abc",
    ]
    assert "shell" not in calls[0][1]

    runner["maintenance"]("enter", authority, "scheduled-abc", execute=execute)
    with pytest.raises(runner["RunnerFailure"], match="^maintenance_failed$"):
        runner["maintenance"](
            "exit", authority, "scheduled-abc",
            execute=lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
        )
    assert request.exists(), "failed runtime restoration must preserve retry authority"


def test_scheduled_execution_records_busy_without_capture(tmp_path) -> None:
    runner = runner_namespace()
    runner["acquire_lock"] = lambda _path: None
    runner["capture_recovery"] = lambda *args, **kwargs: pytest.fail(
        "busy activation must not capture"
    )
    status_path = tmp_path / "status.json"

    result = runner["execute_scheduled"](
        runner_authority(), SimpleNamespace(), status_path, tmp_path / "lock",
        tmp_path / "capture", observed_at=datetime(2026, 9, 20, 10, 20, tzinfo=UTC),
    )

    assert result["outcome"] == "deployment_busy"
    assert result["recovery_point_id"] is None
    assert json.loads(status_path.read_text()) == result


def test_scheduled_execution_records_expired_session_credentials(tmp_path) -> None:
    runner = runner_namespace()
    runner["acquire_lock"] = lambda _path: SimpleNamespace(close=lambda: None)
    runner["execute_capture_policy"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        runner["RunnerFailure"]("credentials_expired")
    )
    status_path = tmp_path / "status.json"

    result = runner["execute_scheduled"](
        runner_authority(), SimpleNamespace(), status_path, tmp_path / "lock",
        tmp_path / "capture", observed_at=datetime(2026, 9, 20, 10, 20, tzinfo=UTC),
    )

    assert result["outcome"] == "credentials_expired"
    assert result["error_code"] == "credentials_expired"


def test_scheduled_execution_records_fixed_capture_stage_without_raw_error(tmp_path) -> None:
    runner = runner_namespace()
    runner["acquire_lock"] = lambda _path: SimpleNamespace(close=lambda: None)
    runner["execute_capture_policy"] = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        runner["RunnerFailure"]("valkey_capture_failed")
    )

    result = runner["execute_scheduled"](
        runner_authority(), SimpleNamespace(), tmp_path / "status.json",
        tmp_path / "lock", tmp_path / "capture",
        observed_at=datetime(2026, 9, 20, 10, 20, tzinfo=UTC),
    )

    assert result["outcome"] == "capture_failed"
    assert result["error_code"] == "valkey_capture_failed"


def test_on_demand_uses_shared_capture_policy_and_deployment_lock(tmp_path) -> None:
    runner = runner_namespace()
    calls = []
    handle = SimpleNamespace(close=lambda: calls.append("closed"))
    runner["acquire_lock"] = lambda path: calls.append(("lock", path)) or handle
    runner["execute_capture_policy"] = lambda *args, **kwargs: (
        calls.append(("capture", args, kwargs)) or {
            "changed": True,
            "recovery_point_id": "rp_" + "a" * 20,
            "retention": {
                "outcome": "succeeded", "error_code": None,
                "deleted": 0, "remaining": 1,
            },
        }
    )

    result = runner["execute_on_demand"](
        runner_authority(), "manual-request", "shared-core",
        tmp_path / "deployment.lock", tmp_path / "capture",
    )

    assert result["changed"] is True
    assert calls[0] == ("lock", tmp_path / "deployment.lock")
    assert calls[1][0] == "capture"
    assert calls[1][1][1:3] == ("manual-request", "shared-core")
    assert calls[-1] == "closed"


def test_on_demand_lock_timeout_performs_no_capture(tmp_path) -> None:
    runner = runner_namespace()
    runner["acquire_lock"] = lambda _path: None
    runner["execute_capture_policy"] = lambda *_args, **_kwargs: pytest.fail(
        "busy on-demand request must not capture"
    )

    with pytest.raises(runner["RunnerFailure"], match="^deployment_busy$"):
        runner["execute_on_demand"](
            runner_authority(), "manual-request", SimpleNamespace(),
            tmp_path / "deployment.lock", tmp_path / "capture",
        )


def test_runner_main_consumes_only_named_systemd_credentials(tmp_path, monkeypatch) -> None:
    runner = runner_namespace()
    credentials = tmp_path / "credentials"
    state = tmp_path / "state"
    credentials.mkdir()
    state.mkdir()
    authority = runner_authority()
    authority_path = credentials / "authority"
    authority_path.write_text(json.dumps(authority))
    authority_path.chmod(0o600)
    valkey = credentials / "valkey"
    valkey.write_text('{"username":"admin","password":"secret"}')
    valkey.chmod(0o600)
    observed = {}
    runner["load_target_capture"] = lambda: "shared-core"

    def execute(*args, **kwargs):
        observed["args"] = args
        observed["kwargs"] = kwargs
        return {"outcome": "succeeded"}

    runner["execute_scheduled"] = execute
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setattr(
        runner["sys"], "argv", ["gimme-recovery-runner", "scheduled", "example-app"]
    )

    assert runner["main"]() == 0
    assert observed["args"][0] == authority
    assert observed["args"][1] == "shared-core"
    assert observed["args"][3] == Path(
        "/srv/gimme/apps/deployments/example-app/shared/.gimme-resource.lock"
    )
    assert observed["kwargs"]["aws_credentials"] is None
    assert observed["kwargs"]["valkey_credential_path"] == valkey


def test_runner_main_records_bounded_startup_failure(tmp_path, monkeypatch) -> None:
    runner = runner_namespace()
    credentials = tmp_path / "credentials"
    state = tmp_path / "state"
    credentials.mkdir()
    state.mkdir()
    authority = runner_authority()
    authority_path = credentials / "authority"
    authority_path.write_text(json.dumps(authority))
    authority_path.chmod(0o600)
    valkey = credentials / "valkey"
    valkey.write_text('{"username":"admin","password":"secret"}')
    valkey.chmod(0o600)
    runner["load_target_capture"] = lambda: (_ for _ in ()).throw(
        runner["RunnerFailure"]("policy_stale")
    )
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setattr(
        runner["sys"], "argv", ["gimme-recovery-runner", "scheduled", "example-app"]
    )

    assert runner["main"]() == 1
    status = json.loads((state / "status.json").read_text())
    assert status["deployment"] == "example-app"
    assert status["outcome"] == "policy_stale"
    assert status["error_code"] == "policy_stale"
    assert status["last_logical_slot"] is not None
    assert set(status) == runner["STATUS_KEYS"]


def test_runner_main_emits_one_bounded_on_demand_result(tmp_path, monkeypatch, capsys) -> None:
    runner = runner_namespace()
    credentials = tmp_path / "credentials"
    state = tmp_path / "state"
    credentials.mkdir()
    state.mkdir()
    authority = runner_authority()
    authority_path = credentials / "authority"
    authority_path.write_text(json.dumps(authority))
    authority_path.chmod(0o600)
    valkey = credentials / "valkey"
    valkey.write_text('{"username":"admin","password":"secret"}')
    valkey.chmod(0o600)
    runner["load_target_capture"] = lambda: "shared-core"
    expected = {
        "changed": True, "recovery_point_id": "rp_" + "a" * 20,
        "retention": {
            "outcome": "succeeded", "error_code": None,
            "deleted": 0, "remaining": 1,
        },
    }
    runner["execute_on_demand"] = lambda *args, **kwargs: expected
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    monkeypatch.setenv("STATE_DIRECTORY", str(state))
    monkeypatch.setattr(runner["sys"], "argv", [
        "gimme-recovery-runner", "on-demand", "example-app", "manual-request",
    ])

    assert runner["main"]() == 0
    marker = capsys.readouterr().out.strip()
    assert marker.startswith("GIMME_RECOVERY_RESULT|")
    decoded = runner["base64"].b64decode(marker.split("|", 1)[1])
    assert json.loads(decoded) == expected


def test_runner_status_emits_only_one_bounded_canonical_marker(
    tmp_path, monkeypatch, capsys
) -> None:
    runner = runner_namespace()
    root = tmp_path / "status"
    path = root / "example-app" / "status.json"
    path.parent.mkdir(parents=True)
    now = datetime(2026, 9, 20, 10, 15, tzinfo=UTC)
    status = runner["attempt_status"](
        "example-app", now, now, "succeeded", finished_at=now
    )
    path.write_text(json.dumps(status))
    path.chmod(0o600)
    runner["STATUS_ROOT"] = root
    monkeypatch.setattr(
        runner["sys"], "argv", ["gimme-recovery-runner", "status", "example-app"]
    )

    assert runner["main"]() == 0
    marker = capsys.readouterr().out.strip()
    assert marker.startswith("GIMME_RECOVERY_STATUS|")
    decoded = runner["base64"].b64decode(marker.split("|", 1)[1])
    assert json.loads(decoded) == status
