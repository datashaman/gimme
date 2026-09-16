import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def helper_namespace() -> dict[str, object]:
    source = (ROOT / "scripts" / "gimme-provision-processes").read_text()
    source = source.replace("__GIMME_POLICY_ID__", "test-policy")
    source = source.replace('"__GIMME_APPS_ROOT__"', json.dumps("/srv/gimme/apps"))
    namespace: dict[str, object] = {"__name__": "gimme_process_helper"}
    exec(compile(source, "gimme-provision-processes", "exec"), namespace)
    return namespace


def queue_config() -> dict[str, object]:
    return {
        "driver": "queue",
        "enabled": True,
        "processes": 2,
        "connection": "database",
        "queues": ["high", "default"],
        "sleep_seconds": 3,
        "tries": 3,
        "timeout_seconds": 90,
        "memory_mb": 256,
        "max_time_seconds": 3600,
        "max_jobs": 0,
        "backoff_seconds": 0,
    }


def test_queue_worker_unit_is_bounded_and_hardened(monkeypatch) -> None:
    helper = helper_namespace()
    monkeypatch.setitem(
        helper["service_header"].__globals__,
        "grp",
        SimpleNamespace(getgrgid=lambda _gid: SimpleNamespace(gr_name="deployer")),
    )
    account = SimpleNamespace(pw_name="deployer", pw_gid=1000)

    unit = helper["render_queue_worker_unit"](
        "example-app", account, Path("/srv/gimme/apps/example-app"), queue_config()
    )

    assert "WorkingDirectory=/srv/gimme/apps/example-app/current" in unit
    assert 'WorkingDirectory="' not in unit
    assert '"queue:work" "database" "--queue=high,default"' in unit
    assert "User=deployer" in unit
    assert "Group=deployer" in unit
    assert "TimeoutStopSec=120" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "CapabilityBoundingSet=" in unit
    assert "ReadWritePaths=" in unit
    assert 'ReadWritePaths="' not in unit
    assert "Environment=APP_ENV=" not in unit


def test_horizon_and_scheduler_units_have_correct_lifecycle(monkeypatch) -> None:
    helper = helper_namespace()
    monkeypatch.setitem(
        helper["service_header"].__globals__,
        "grp",
        SimpleNamespace(getgrgid=lambda _gid: SimpleNamespace(gr_name="deployer")),
    )
    account = SimpleNamespace(pw_name="deployer", pw_gid=1000)
    root = Path("/srv/gimme/apps/example-app")

    horizon = helper["render_horizon_unit"](
        "example-app",
        account,
        root,
        {"driver": "horizon", "enabled": True, "stop_wait_seconds": 3600},
    )
    scheduler = helper["render_scheduler_service"]("example-app", account, root)
    timer = helper["render_scheduler_timer"]("example-app")

    assert 'ExecStart="/usr/bin/php" "artisan" "horizon"' in horizon
    assert "Restart=always" in horizon
    assert "TimeoutStopSec=3600" in horizon
    assert "Environment=APP_ENV=" not in horizon
    assert '"--no-interaction" "schedule:run"' in scheduler
    assert "Type=oneshot" in scheduler
    assert "OnCalendar=*-*-* *:*:00" in timer
    assert "Persistent=true" in timer


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None,
    reason="systemd-analyze is available on Linux CI",
)
def test_rendered_process_units_pass_systemd_verification(tmp_path, monkeypatch) -> None:
    helper = helper_namespace()
    monkeypatch.setitem(
        helper["service_header"].__globals__,
        "grp",
        SimpleNamespace(getgrgid=lambda _gid: SimpleNamespace(gr_name="root")),
    )
    account = SimpleNamespace(pw_name="root", pw_gid=0)
    app_root = tmp_path / "apps" / "example-app"
    (app_root / "current" / "bootstrap" / "cache").mkdir(parents=True)
    (app_root / "shared").mkdir()
    units = {
        "gimme-horizon-example-app.service": helper["render_horizon_unit"](
            "example-app",
            account,
            app_root,
            {"driver": "horizon", "enabled": True, "stop_wait_seconds": 3600},
        ),
        "gimme-scheduler-example-app.service": helper["render_scheduler_service"](
            "example-app", account, app_root
        ),
        "gimme-scheduler-example-app.timer": helper["render_scheduler_timer"]("example-app"),
    }
    paths = []
    for name, content in units.items():
        path = tmp_path / name
        path.write_text(content)
        paths.append(str(path))

    result = subprocess.run(
        ["systemd-analyze", "verify", *paths],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_process_helper_rejects_unknown_or_injected_configuration() -> None:
    helper = helper_namespace()
    config = queue_config()
    config["command"] = "sh -c reboot"

    with pytest.raises(RuntimeError, match="unknown or missing"):
        helper["validate_workers"](config)

    config = queue_config()
    config["queues"] = ["default; reboot"]
    with pytest.raises(RuntimeError, match="invalid"):
        helper["validate_workers"](config)

    with pytest.raises(RuntimeError, match="unexpected executable"):
        helper["fixed_command"](["sh", "-c", "reboot"])


def test_reconcile_writes_and_starts_only_declared_queue_instances(
    tmp_path, monkeypatch, capsys
) -> None:
    helper = helper_namespace()
    apps_root = tmp_path / "apps"
    current = apps_root / "example-app" / "current"
    (current / "bootstrap" / "cache").mkdir(parents=True)
    (current / "artisan").touch()
    (apps_root / "example-app" / "shared").mkdir()
    systemd_root = tmp_path / "systemd"
    systemd_root.mkdir()
    account = SimpleNamespace(pw_name="deployer", pw_gid=1000, pw_uid=1000)
    commands: list[list[str]] = []

    monkeypatch.setitem(helper, "EXPECTED_APPS_ROOT", apps_root)
    monkeypatch.setitem(helper, "SYSTEMD_ROOT", systemd_root)
    monkeypatch.setitem(
        helper,
        "load_state",
        lambda *_args: {
            "deploy_path": apps_root / "example-app",
            "workers": queue_config(),
            "scheduler": {"enabled": True},
        },
    )
    monkeypatch.setitem(helper, "listed_worker_instances", lambda _app: set())
    monkeypatch.setitem(helper, "succeeds", lambda _command: False)
    monkeypatch.setitem(helper, "run", lambda command, **_kwargs: commands.append(command))
    monkeypatch.setattr(helper["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(helper["pwd"], "getpwnam", lambda _user: account)
    monkeypatch.setattr(helper["grp"], "getgrgid", lambda _gid: SimpleNamespace(gr_name="deployer"))
    monkeypatch.setattr(helper["sys"], "argv", ["gimme-provision-processes", "example-app"])
    monkeypatch.setenv("SUDO_USER", "deployer")

    helper["reconcile"]()

    assert (systemd_root / "gimme-worker-example-app@.service").is_file()
    assert (systemd_root / "gimme-scheduler-example-app.timer").is_file()
    assert ["systemctl", "enable", "gimme-worker-example-app@1.service"] in commands
    assert ["systemctl", "restart", "gimme-worker-example-app@2.service"] in commands
    assert ["systemctl", "enable", "gimme-scheduler-example-app.timer"] in commands
    assert "process.units_changed=yes" in capsys.readouterr().out

    helper["reconcile"]()
    assert "process.units_changed=no" in capsys.readouterr().out


def test_teardown_does_not_require_a_current_release(tmp_path, monkeypatch) -> None:
    helper = helper_namespace()
    apps_root = tmp_path / "apps"
    deploy_path = apps_root / "example-app" / "environments" / "feature-x"
    deploy_path.mkdir(parents=True)
    systemd_root = tmp_path / "systemd"
    systemd_root.mkdir()
    account = SimpleNamespace(pw_name="deployer", pw_gid=1000, pw_uid=1000)

    monkeypatch.setitem(helper, "EXPECTED_APPS_ROOT", apps_root)
    monkeypatch.setitem(helper, "SYSTEMD_ROOT", systemd_root)
    monkeypatch.setitem(
        helper,
        "load_state",
        lambda *_args: {
            "deploy_path": deploy_path,
            "workers": None,
            "scheduler": None,
        },
    )
    monkeypatch.setitem(helper, "listed_worker_instances", lambda _app: set())
    monkeypatch.setitem(helper, "succeeds", lambda _command: False)
    monkeypatch.setitem(helper, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(helper["os"], "geteuid", lambda: 0)
    monkeypatch.setattr(helper["pwd"], "getpwnam", lambda _user: account)
    monkeypatch.setattr(
        helper["sys"],
        "argv",
        [
            "gimme-provision-processes",
            "example-app--feature-x",
        ],
    )
    monkeypatch.setenv("SUDO_USER", "deployer")

    helper["reconcile"]()
