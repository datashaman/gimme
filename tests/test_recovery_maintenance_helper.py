import json
from pathlib import Path
import subprocess
import pytest


ROOT = Path(__file__).resolve().parents[1]


def helper_namespace() -> dict[str, object]:
    source = (ROOT / "scripts" / "gimme-recovery-maintenance").read_text()
    source = source.replace("__GIMME_POLICY_ID__", "test-policy")
    source = source.replace('"__GIMME_APPS_ROOT__"', json.dumps("/srv/gimme/apps"))
    namespace: dict[str, object] = {"__name__": "gimme_recovery_maintenance"}
    exec(compile(source, "gimme-recovery-maintenance", "exec"), namespace)
    return namespace


def authority(root: Path) -> dict[str, object]:
    site = root / "example-app.caddy"
    site.write_text("example.test {\n\trespond 200\n}\n")
    return {
        "site_host": "example.test",
        "site_path": site,
        "units": ["gimme-worker-example-app@1.service", "gimme-scheduler-example-app.timer"],
        "wait_seconds": 30,
    }


def test_enter_is_request_owned_stops_only_registered_units_and_installs_503(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    commands: list[list[str]] = []
    active = {
        "gimme-worker-example-app@1.service",
        "gimme-scheduler-example-app.timer",
    }
    auth = authority(tmp_path)
    monkeypatch.setitem(helper, "MARKER_ROOT", tmp_path / "markers")
    monkeypatch.setitem(helper, "CADDYFILE", tmp_path / "Caddyfile")
    monkeypatch.setitem(helper, "load_authority", lambda *_args: auth)
    monkeypatch.setitem(helper, "succeeds", lambda command: command[-1] in active)
    def run(command):
        commands.append(command)
        if command[:2] == ["systemctl", "stop"]:
            active.discard(command[-1])
    monkeypatch.setitem(helper, "run", run)

    helper["enter"]("example-app", "request-1", 1000, "deployer")

    assert auth["site_path"].read_text() == "example.test {\n\trespond 503\n}\n"
    assert ["systemctl", "stop", "gimme-worker-example-app@1.service"] in commands
    assert ["systemctl", "stop", "gimme-scheduler-example-app.timer"] in commands
    marker = json.loads((tmp_path / "markers" / "example-app.json").read_text())
    assert marker["request_id"] == "request-1"
    assert marker["active_units"] == auth["units"]
    assert "example.test" not in json.dumps(marker), "route identity stays out of markers"

    with pytest.raises(RuntimeError, match="another request"):
        helper["enter"]("example-app", "request-2", 1000, "deployer")


def test_exit_restores_only_previously_active_units_then_normal_route(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    commands: list[list[str]] = []
    auth = authority(tmp_path)
    monkeypatch.setitem(helper, "MARKER_ROOT", tmp_path / "markers")
    monkeypatch.setitem(helper, "CADDYFILE", tmp_path / "Caddyfile")
    monkeypatch.setitem(helper, "load_authority", lambda *_args: auth)
    active = set(auth["units"])
    monkeypatch.setitem(helper, "succeeds", lambda command: command[-1] in active)
    def run(command):
        commands.append(command)
        if command[:2] == ["systemctl", "stop"]:
            active.discard(command[-1])
        elif command[:2] == ["systemctl", "start"]:
            active.add(command[-1])
    monkeypatch.setitem(helper, "run", run)
    helper["enter"]("example-app", "request-1", 1000, "deployer")
    commands.clear()

    helper["exit_maintenance"]("example-app", "request-1", 1000, "deployer")

    assert auth["site_path"].read_text() == "example.test {\n\trespond 200\n}\n"
    assert commands[:4] == [
        ["systemctl", "reset-failed", "gimme-scheduler-example-app.timer"],
        ["systemctl", "start", "gimme-scheduler-example-app.timer"],
        ["systemctl", "reset-failed", "gimme-worker-example-app@1.service"],
        ["systemctl", "start", "gimme-worker-example-app@1.service"],
    ]
    assert not (tmp_path / "markers" / "example-app.json").exists()


def test_exit_refuses_another_requests_marker(tmp_path, monkeypatch) -> None:
    helper = helper_namespace()
    auth = authority(tmp_path)
    monkeypatch.setitem(helper, "MARKER_ROOT", tmp_path / "markers")
    monkeypatch.setitem(helper, "CADDYFILE", tmp_path / "Caddyfile")
    monkeypatch.setitem(helper, "load_authority", lambda *_args: auth)
    active = set(auth["units"])
    monkeypatch.setitem(helper, "succeeds", lambda command: command[-1] in active)
    def run(command):
        if command[:2] == ["systemctl", "stop"]:
            active.discard(command[-1])
    monkeypatch.setitem(helper, "run", run)
    helper["enter"]("example-app", "request-1", 1000, "deployer")

    with pytest.raises(RuntimeError, match="another request"):
        helper["exit_maintenance"]("example-app", "request-2", 1000, "deployer")

    assert auth["site_path"].read_text() == "example.test {\n\trespond 503\n}\n"


def test_exit_retry_is_a_no_op_after_route_was_already_restored(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    auth = authority(tmp_path)
    monkeypatch.setitem(helper, "MARKER_ROOT", tmp_path / "markers")
    monkeypatch.setitem(helper, "CADDYFILE", tmp_path / "Caddyfile")
    monkeypatch.setitem(helper, "load_authority", lambda *_args: auth)
    active = set(auth["units"])
    failed: set[str] = set()
    monkeypatch.setitem(helper, "succeeds", lambda command: command[-1] in active)

    def run(command):
        if command[:2] == ["systemctl", "stop"]:
            active.discard(command[-1])
        elif command[:2] == ["systemctl", "reset-failed"]:
            failed.discard(command[-1])
        elif command[:2] == ["systemctl", "start"]:
            if command[-1] not in failed:
                active.add(command[-1])

    monkeypatch.setitem(helper, "run", run)
    helper["enter"]("example-app", "request-1", 1000, "deployer")
    helper["exit_maintenance"]("example-app", "request-1", 1000, "deployer")

    helper["exit_maintenance"]("example-app", "request-1", 1000, "deployer")

    assert auth["site_path"].read_text() == "example.test {\n\trespond 200\n}\n"
    receipt = json.loads(
        (tmp_path / "markers" / "example-app.exit.json").read_text()
    )
    assert receipt["request_id"] == "request-1"


def test_exit_without_marker_or_matching_receipt_fails_closed(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    auth = authority(tmp_path)
    monkeypatch.setitem(helper, "MARKER_ROOT", tmp_path / "markers")
    monkeypatch.setitem(helper, "load_authority", lambda *_args: auth)

    with pytest.raises(RuntimeError, match="exit state is ambiguous"):
        helper["exit_maintenance"]("example-app", "request-1", 1000, "deployer")


def test_restore_can_resume_then_requiesce_processes_without_restoring_route(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    commands: list[list[str]] = []
    auth = authority(tmp_path)
    monkeypatch.setitem(helper, "MARKER_ROOT", tmp_path / "markers")
    monkeypatch.setitem(helper, "CADDYFILE", tmp_path / "Caddyfile")
    monkeypatch.setitem(helper, "load_authority", lambda *_args: auth)
    active = set(auth["units"])
    failed: set[str] = set()
    monkeypatch.setitem(helper, "succeeds", lambda command: command[-1] in active)

    def run(command):
        commands.append(command)
        if command[:2] == ["systemctl", "stop"]:
            active.discard(command[-1])
        elif command[:2] == ["systemctl", "reset-failed"]:
            failed.discard(command[-1])
        elif command[:2] == ["systemctl", "start"]:
            if command[-1] not in failed:
                active.add(command[-1])

    monkeypatch.setitem(helper, "run", run)
    helper["enter"]("example-app", "request-1", 1000, "deployer")
    commands.clear()
    failed.update(auth["units"])

    helper["resume_processes"]("example-app", "request-1", 1000, "deployer")

    assert active == set(auth["units"])
    assert failed == set()
    assert [command[:2] for command in commands[:4]] == [
        ["systemctl", "reset-failed"], ["systemctl", "start"],
        ["systemctl", "reset-failed"], ["systemctl", "start"],
    ]
    assert auth["site_path"].read_text() == "example.test {\n\trespond 503\n}\n"
    assert (tmp_path / "markers" / "example-app.json").exists()

    helper["quiesce_processes"]("example-app", "request-1", 1000, "deployer")

    assert active == set()
    assert auth["site_path"].read_text() == "example.test {\n\trespond 503\n}\n"


def test_resume_refuses_a_unit_removed_from_registered_process_policy(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    auth = authority(tmp_path)
    monkeypatch.setitem(helper, "MARKER_ROOT", tmp_path / "markers")
    monkeypatch.setitem(helper, "CADDYFILE", tmp_path / "Caddyfile")
    monkeypatch.setitem(helper, "load_authority", lambda *_args: auth)
    active = set(auth["units"])
    monkeypatch.setitem(helper, "succeeds", lambda command: command[-1] in active)

    def run(command):
        if command[:2] == ["systemctl", "stop"]:
            active.discard(command[-1])

    monkeypatch.setitem(helper, "run", run)
    helper["enter"]("example-app", "request-1", 1000, "deployer")
    auth["units"] = ["gimme-scheduler-example-app.timer"]

    with pytest.raises(RuntimeError, match="process policy changed"):
        helper["resume_processes"]("example-app", "request-1", 1000, "deployer")

    assert active == set()
    assert auth["site_path"].read_text() == "example.test {\n\trespond 503\n}\n"


@pytest.mark.parametrize(
    ("command", "code"),
    [
        (["caddy", "validate", "--config", "/etc/caddy/Caddyfile"],
         "maintenance_route_validation_failed"),
        (["systemctl", "reload", "caddy"], "maintenance_route_reload_failed"),
        (["sleep", "1"], "maintenance_quiesce_wait_failed"),
        (["systemctl", "stop", "gimme-worker-example-app@1.service"],
         "maintenance_process_control_failed"),
    ],
)
def test_run_maps_subprocess_failures_to_fixed_phase_codes(
    monkeypatch, command, code
) -> None:
    helper = helper_namespace()

    def failed(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(RuntimeError, match=f"^{code}$"):
        helper["run"](command)
