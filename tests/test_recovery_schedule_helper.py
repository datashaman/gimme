import json
import os
import pwd
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def helper_namespace() -> dict[str, object]:
    source = (ROOT / "scripts" / "gimme-provision-recovery-schedule").read_text()
    source = source.replace("__GIMME_POLICY_ID__", "test-policy")
    source = source.replace('"__GIMME_APPS_ROOT__"', json.dumps("/srv/gimme/apps"))
    source = source.replace('"__GIMME_RUNNER_SHA256__"', '"test-runner-sha256"')
    namespace: dict[str, object] = {"__name__": "gimme_recovery_schedule_helper"}
    exec(compile(source, "gimme-provision-recovery-schedule", "exec"), namespace)
    return namespace


def authority(cadence: dict[str, object] | None = None) -> dict[str, object]:
    selected = cadence or {"kind": "hourly", "minute": 15}
    calendars = {
        "manual": None,
        "hourly": "*-*-* *:15:00 UTC",
        "daily": "*-*-* 02:00:00 UTC",
        "weekly": "Sun *-*-* 02:00:00 UTC",
    }
    return {
        "schema_version": 2,
        "deployment": "example-app",
        "target": "devbox",
        "policy_fingerprint": "a" * 64,
        "cadence": selected,
        "calendar": calendars[str(selected["kind"])],
        "stable_delay_seconds": 123,
        "retain_last": 7,
        "quiesce_wait_seconds": 30,
        "components": ["postgres"],
        "placement": {
            "instance": "example-app",
            "relative_path": "deployments/example-app",
            "database_identifier": "example_app",
            "cache_prefix": "gimme:example-app:",
            "site_host": "example.test",
        },
        "resources": {
            "postgres": {
                "name": "primary-db",
                "provider": "target_local",
                "kind": "postgres",
                "version": "16.4",
            }
        },
        "destination": {
            "name": "primary",
            "provider": "s3_compatible",
            "bucket": "gimme-backups",
            "region": "us-east-1",
            "endpoint": None,
            "addressing": "virtual_hosted",
            "encryption": {"method": "aes256"},
            "auth_mode": "ambient",
        },
        "status_identity": "example-app",
        "valkey_execution": None,
    }


@pytest.mark.parametrize(
    ("cadence", "calendar"),
    [
        ({"kind": "manual"}, None),
        ({"kind": "hourly", "minute": 15}, "*-*-* *:15:00 UTC"),
        ({"kind": "daily", "hour": 2, "minute": 0}, "*-*-* 02:00:00 UTC"),
        (
            {"kind": "weekly", "weekday": "sun", "hour": 2, "minute": 0},
            "Sun *-*-* 02:00:00 UTC",
        ),
    ],
)
def test_authority_accepts_only_derived_calendars(cadence, calendar) -> None:
    helper = helper_namespace()
    value = authority(cadence)
    value["calendar"] = calendar

    assert helper["validate_authority"](value, "example-app") == value

    value["calendar"] = "*-*-* *:*:00"
    with pytest.raises(RuntimeError, match="calendar mismatch"):
        helper["validate_authority"](value, "example-app")


def test_authority_accepts_registered_raw_s3_endpoint() -> None:
    helper = helper_namespace()
    value = authority()
    value["destination"]["endpoint"] = "minio.example.test:9000"

    assert helper["validate_authority"](value, "example-app") == value


def test_authority_accepts_only_bounded_valkey_execution() -> None:
    helper = helper_namespace()
    value = authority()
    value["components"] = ["postgres", "valkey"]
    value["resources"]["valkey"] = {
        "name": "cache", "provider": "aws_elasticache_valkey",
        "kind": "valkey", "version": "8.0",
    }
    value["valkey_execution"] = {
        "prefix": "{gimme:example-app}:", "host": "cache.example.test",
        "port": 6379, "tls": True, "auth_mode": "stored",
    }

    assert helper["validate_authority"](value, "example-app") == value
    value["valkey_execution"]["host"] = "cache; shutdown"
    with pytest.raises(RuntimeError):
        helper["validate_authority"](value, "example-app")


def test_manual_valkey_authority_requires_no_runtime_endpoint_or_credential() -> None:
    helper = helper_namespace()
    value = authority({"kind": "manual"})
    value["components"] = ["postgres", "valkey"]
    value["resources"]["valkey"] = {
        "name": "cache", "provider": "aws_elasticache_valkey",
        "kind": "valkey", "version": "9.0",
    }

    assert helper["validate_authority"](value, "example-app") == value


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("cadence", "timezone"), "Africa/Johannesburg"),
        (("destination", "endpoint"), "https://example.test/unsafe/path"),
        (("placement", "relative_path"), "../escape"),
        (("resources", "postgres", "name"), "db; shutdown"),
        (("stable_delay_seconds",), 301),
    ],
)
def test_authority_rejects_unbounded_execution_inputs(path, value) -> None:
    helper = helper_namespace()
    selected = authority()
    target = selected
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    with pytest.raises(RuntimeError):
        helper["validate_authority"](selected, "example-app")


def test_units_use_only_fixed_runner_and_hardening() -> None:
    helper = helper_namespace()
    helper["grp"] = SimpleNamespace(getgrgid=lambda _gid: SimpleNamespace(gr_name="deployer"))
    account = SimpleNamespace(pw_name="deployer", pw_gid=1000)

    service = helper["service_unit"](
        "example-app", account, "deployments/example-app", stored_credentials=False
    )
    stored_service = helper["service_unit"](
        "example-app", account, "deployments/example-app", stored_credentials=True
    )
    timer = helper["timer_unit"]("example-app", "*-*-* *:15:00 UTC")

    assert "ExecStart=/usr/local/libexec/gimme-recovery-runner scheduled example-app" in service
    assert (
        "Environment=AWS_CONFIG_FILE=/dev/null AWS_SHARED_CREDENTIALS_FILE=/dev/null"
        in service
    )
    assert "LoadCredential=authority:/etc/gimme/recovery-schedules/example-app.json" in service
    assert "LoadCredential=aws:" not in service
    assert "LoadCredential=valkey:" not in service
    assert (
        "LoadCredential=aws:/etc/gimme/recovery-schedules/example-app.credentials"
        in stored_service
    )
    assert "NoNewPrivileges=true" not in service
    assert "ProtectSystem=strict" in service
    assert (
        "ReadWritePaths=/srv/gimme/apps/deployments/example-app/shared "
        "/srv/gimme/apps/.gimme/recovery-requests /var/lib/gimme/recovery /etc/caddy"
        in service
    )
    assert "CapabilityBoundingSet=" not in service
    assert "OnCalendar=*-*-* *:15:00 UTC" in timer
    assert "Persistent=true" in timer
    assert "RandomizedDelaySec" not in timer


def test_units_load_valkey_credential_only_from_fixed_installed_path() -> None:
    helper = helper_namespace()
    helper["grp"] = SimpleNamespace(getgrgid=lambda _gid: SimpleNamespace(gr_name="deployer"))
    account = SimpleNamespace(pw_name="deployer", pw_gid=1000)

    service = helper["service_unit"](
        "example-app", account, "deployments/example-app", stored_credentials=False,
        stored_valkey_credentials=True,
    )

    assert (
        "LoadCredential=valkey:/etc/gimme/recovery-schedules/"
        "example-app.valkey-credentials" in service
    )
    assert "LoadCredential=aws:" not in service


def configure_filesystem(helper, tmp_path: Path, selected: dict[str, object]) -> None:
    apps = tmp_path / "apps"
    transfer = apps / ".gimme" / "recovery-schedules"
    transfer.mkdir(parents=True)
    (apps / "deployments" / "example-app" / "shared").mkdir(parents=True)
    state = transfer / "example-app.json"
    state.write_text(json.dumps(selected))
    state.chmod(0o600)
    runner = tmp_path / "gimme-recovery-runner"
    runner.write_text("#!/bin/sh\n")
    runner.chmod(0o755)
    helper["EXPECTED_RUNNER_SHA256"] = helper["hashlib"].sha256(
        runner.read_bytes()
    ).hexdigest()
    class RootOwnedRunner:
        def __fspath__(self):
            return str(runner)

        def __str__(self):
            return str(runner)

        def exists(self):
            return True

        def is_symlink(self):
            return False

        def stat(self):
            details = runner.stat()
            return SimpleNamespace(st_mode=details.st_mode, st_uid=os.getuid())

        def read_bytes(self):
            return runner.read_bytes()

    helper.update({
        "EXPECTED_APPS_ROOT": apps,
        "TRANSFER_ROOT": transfer,
        "AUTHORITY_ROOT": tmp_path / "etc" / "recovery-schedules",
        "STATUS_ROOT": tmp_path / "var" / "recovery-schedules",
        "MAINTENANCE_ROOT": tmp_path / "var" / "recovery",
        "SYSTEMD_ROOT": tmp_path / "systemd",
        "RUNNER": RootOwnedRunner(),
        "ROOT_UID": os.getuid(),
        "ROOT_GID": os.getgid(),
    })


def test_reconcile_installs_exact_units_and_is_idempotent(tmp_path, monkeypatch) -> None:
    helper = helper_namespace()
    configure_filesystem(helper, tmp_path, authority())
    calls: list[list[str]] = []
    active: set[tuple[str, ...]] = set()
    monkeypatch.setitem(helper, "run", lambda command: calls.append(command))
    monkeypatch.setitem(helper, "succeeds", lambda command: tuple(command) in active)
    monkeypatch.setattr(helper["os"], "chown", lambda *_args: None)
    monkeypatch.setattr(helper["os"], "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", pwd.getpwuid(os.getuid()).pw_name)
    monkeypatch.setattr(helper["sys"], "argv", ["helper", "example-app"])

    helper["reconcile"]()

    systemd = helper["SYSTEMD_ROOT"]
    assert (systemd / "gimme-recovery-example-app.service").is_file()
    assert (systemd / "gimme-recovery-example-app.timer").is_file()
    stored = helper["AUTHORITY_ROOT"] / "example-app.json"
    assert stored.stat().st_mode & 0o777 == 0o600
    assert ["systemctl", "daemon-reload"] in calls
    assert ["systemctl", "enable", "gimme-recovery-example-app.timer"] in calls
    assert ["systemctl", "restart", "gimme-recovery-example-app.timer"] in calls
    requests = helper["EXPECTED_APPS_ROOT"] / ".gimme" / "recovery-requests"
    assert requests.is_dir()
    assert requests.stat().st_mode & 0o777 == 0o700
    maintenance = helper["MAINTENANCE_ROOT"]
    assert maintenance.is_dir()
    assert maintenance.stat().st_mode & 0o777 == 0o700

    calls.clear()
    active.update({
        ("systemctl", "is-enabled", "--quiet", "gimme-recovery-example-app.timer"),
        ("systemctl", "is-active", "--quiet", "gimme-recovery-example-app.timer"),
    })
    helper["reconcile"]()
    assert calls == []


def test_reconcile_rejects_a_tampered_runner_before_unit_mutation(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    configure_filesystem(helper, tmp_path, authority())
    Path(str(helper["RUNNER"])).write_text("#!/bin/sh\nexit 1\n")
    calls: list[list[str]] = []
    monkeypatch.setitem(helper, "run", lambda command: calls.append(command))
    monkeypatch.setitem(helper, "succeeds", lambda _command: False)
    monkeypatch.setattr(helper["os"], "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", pwd.getpwuid(os.getuid()).pw_name)
    monkeypatch.setattr(helper["sys"], "argv", ["helper", "example-app"])

    with pytest.raises(RuntimeError, match="validated Recovery Schedule runner"):
        helper["reconcile"]()

    assert calls == []
    assert not helper["SYSTEMD_ROOT"].exists()


def test_manual_cadence_removes_units_authority_and_credentials(tmp_path, monkeypatch) -> None:
    helper = helper_namespace()
    selected = authority({"kind": "manual"})
    selected["destination"]["auth_mode"] = "stored"
    configure_filesystem(helper, tmp_path, selected)
    systemd = helper["SYSTEMD_ROOT"]
    systemd.mkdir(parents=True)
    authority_root = helper["AUTHORITY_ROOT"]
    authority_root.mkdir(parents=True)
    for path in (
        systemd / "gimme-recovery-example-app.service",
        systemd / "gimme-recovery-example-app.timer",
        authority_root / "example-app.json",
        authority_root / "example-app.credentials",
        authority_root / "example-app.valkey-credentials",
    ):
        path.write_text("old")
    calls: list[list[str]] = []
    monkeypatch.setitem(helper, "run", lambda command: calls.append(command))
    monkeypatch.setitem(helper, "succeeds", lambda _command: True)
    monkeypatch.setattr(helper["os"], "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", pwd.getpwuid(os.getuid()).pw_name)
    monkeypatch.setattr(helper["sys"], "argv", ["helper", "example-app"])

    helper["reconcile"]()

    assert not list(systemd.glob("gimme-recovery-example-app.*"))
    assert not list(authority_root.glob("example-app*"))
    assert ["systemctl", "disable", "--now", "gimme-recovery-example-app.timer"] in calls
    assert ["systemctl", "daemon-reload"] in calls


def test_stored_credentials_are_validated_installed_and_transfer_removed(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    selected = authority()
    selected["destination"]["auth_mode"] = "stored"
    configure_filesystem(helper, tmp_path, selected)
    transfer = helper["TRANSFER_ROOT"] / "example-app.credentials"
    transfer.write_text(json.dumps({
        "access_key_id": "access-canary",
        "secret_access_key": "secret-canary",
    }))
    transfer.chmod(0o600)
    monkeypatch.setitem(helper, "run", lambda _command: None)
    monkeypatch.setitem(helper, "succeeds", lambda _command: False)
    monkeypatch.setattr(helper["os"], "chown", lambda *_args: None)
    monkeypatch.setattr(helper["os"], "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", pwd.getpwuid(os.getuid()).pw_name)
    monkeypatch.setattr(helper["sys"], "argv", ["helper", "example-app"])

    helper["reconcile"]()

    installed = helper["AUTHORITY_ROOT"] / "example-app.credentials"
    assert json.loads(installed.read_text()) == {
        "access_key_id": "access-canary",
        "secret_access_key": "secret-canary",
    }
    assert installed.stat().st_mode & 0o777 == 0o600
    assert not transfer.exists()
    unit = (helper["SYSTEMD_ROOT"] / "gimme-recovery-example-app.service").read_text()
    assert "LoadCredential=aws:" in unit
    assert "access-canary" not in unit
    assert "secret-canary" not in unit


def test_session_credentials_are_installed_without_entering_the_unit(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    selected = authority()
    selected["destination"]["auth_mode"] = "stored"
    configure_filesystem(helper, tmp_path, selected)
    transfer = helper["TRANSFER_ROOT"] / "example-app.credentials"
    transfer.write_text(json.dumps({
        "access_key_id": "access-canary", "secret_access_key": "secret-canary",
        "session_token": "session-canary",
    }))
    transfer.chmod(0o600)
    monkeypatch.setitem(helper, "run", lambda _command: None)
    monkeypatch.setitem(helper, "succeeds", lambda _command: False)
    monkeypatch.setattr(helper["os"], "chown", lambda *_args: None)
    monkeypatch.setattr(helper["os"], "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", pwd.getpwuid(os.getuid()).pw_name)
    monkeypatch.setattr(helper["sys"], "argv", ["helper", "example-app"])

    helper["reconcile"]()

    installed = helper["AUTHORITY_ROOT"] / "example-app.credentials"
    assert json.loads(installed.read_text())["session_token"] == "session-canary"
    unit = (helper["SYSTEMD_ROOT"] / "gimme-recovery-example-app.service").read_text()
    assert "LoadCredential=aws:" in unit
    assert "session-canary" not in unit


@pytest.mark.parametrize(
    "credentials",
    [
        {"access_key_id": "access"},
        {"access_key_id": "access", "secret_access_key": "line\nbreak"},
        {"access_key_id": "access", "secret_access_key": "secret", "extra": "value"},
    ],
)
def test_invalid_stored_credentials_fail_before_mutation_and_remove_transfer(
    tmp_path, credentials
) -> None:
    helper = helper_namespace()
    selected = authority()
    selected["destination"]["auth_mode"] = "stored"
    configure_filesystem(helper, tmp_path, selected)
    transfer = helper["TRANSFER_ROOT"] / "example-app.credentials"
    transfer.write_text(json.dumps(credentials))
    transfer.chmod(0o600)

    with pytest.raises(RuntimeError, match="credential"):
        helper["load_transferred_credentials"]("example-app", os.getuid(), "stored")

    assert not transfer.exists()
    assert not helper["AUTHORITY_ROOT"].exists()


def test_missing_stored_credential_error_does_not_expose_path(tmp_path) -> None:
    helper = helper_namespace()
    configure_filesystem(helper, tmp_path, authority())

    with pytest.raises(RuntimeError) as failure:
        helper["load_transferred_credentials"]("example-app", os.getuid(), "stored")

    assert str(failure.value) == "Recovery Schedule credential transfer is unavailable"
    assert ".credentials" not in str(failure.value)


def test_stored_valkey_credentials_are_independently_installed_and_removed(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace()
    selected = authority()
    selected["components"] = ["postgres", "valkey"]
    selected["resources"]["valkey"] = {
        "name": "cache", "provider": "aws_elasticache_valkey",
        "kind": "valkey", "version": "9.0",
    }
    selected["valkey_execution"] = {
        "prefix": "{gimme:example-app}:", "host": "cache.example.test",
        "port": 6379, "tls": True, "auth_mode": "stored",
    }
    configure_filesystem(helper, tmp_path, selected)
    transfer = helper["TRANSFER_ROOT"] / "example-app.valkey-credentials"
    transfer.write_text(json.dumps({"username": "admin", "password": "secret-canary"}))
    transfer.chmod(0o600)
    monkeypatch.setitem(helper, "run", lambda _command: None)
    monkeypatch.setitem(helper, "succeeds", lambda _command: False)
    monkeypatch.setattr(helper["os"], "chown", lambda *_args: None)
    monkeypatch.setattr(helper["os"], "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", pwd.getpwuid(os.getuid()).pw_name)
    monkeypatch.setattr(helper["sys"], "argv", ["helper", "example-app"])

    helper["reconcile"]()

    installed = helper["AUTHORITY_ROOT"] / "example-app.valkey-credentials"
    assert json.loads(installed.read_text()) == {
        "username": "admin", "password": "secret-canary",
    }
    assert installed.stat().st_mode & 0o777 == 0o600
    assert not transfer.exists()
    unit = (helper["SYSTEMD_ROOT"] / "gimme-recovery-example-app.service").read_text()
    assert "LoadCredential=valkey:" in unit
    assert "admin" not in unit
    assert "secret-canary" not in unit


@pytest.mark.parametrize(
    "credentials",
    [
        {"username": "admin"},
        {"username": "admin", "password": "line\nbreak"},
        {"username": "admin", "password": "secret", "extra": "value"},
    ],
)
def test_invalid_valkey_credentials_fail_before_mutation_and_remove_transfer(
    tmp_path, credentials
) -> None:
    helper = helper_namespace()
    configure_filesystem(helper, tmp_path, authority())
    transfer = helper["TRANSFER_ROOT"] / "example-app.valkey-credentials"
    transfer.write_text(json.dumps(credentials))
    transfer.chmod(0o600)

    with pytest.raises(RuntimeError, match="credential"):
        helper["load_transferred_valkey_credentials"](
            "example-app", os.getuid(), "stored"
        )

    assert not transfer.exists()
    assert not helper["AUTHORITY_ROOT"].exists()
