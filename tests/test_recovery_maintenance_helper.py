import json
from pathlib import Path
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
    assert commands[:2] == [
        ["systemctl", "start", "gimme-scheduler-example-app.timer"],
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
