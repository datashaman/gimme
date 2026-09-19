import json
import subprocess
from pathlib import Path

from gimme.config import (
    AppConfig,
    EnvironmentConfig,
    HealthCheckConfig,
    HorizonWorkerConfig,
    SchedulerConfig,
    ServerConfig,
)
from gimme.deployer import DeployerRunner
import gimme.deployer as deployer_module


def server() -> ServerConfig:
    return ServerConfig(
        host_alias="devbox",
        bootstrap_hostname="192.0.2.10",
        hostname="devbox.local",
        mdns_name="devbox",
        remote_user="deployer",
        apps_root="/srv/gimme/apps",
    )


def runner(tmp_path: Path) -> DeployerRunner:
    binary = tmp_path / "vendor/bin/dep"
    binary.parent.mkdir(parents=True)
    binary.touch()
    (tmp_path / "deploy.php").touch()
    return DeployerRunner(tmp_path)


def test_normal_tasks_connect_to_advertised_hostname(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner(tmp_path).run("deploy", server())

    assert captured["GIMME_SSH_HOSTNAME"] == "devbox.local"
    assert captured["GIMME_HOSTNAME"] == "devbox.local"
    assert captured["GIMME_HOST_ALIAS"] == "devbox"


def test_runner_does_not_inherit_unrelated_secrets(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setenv("UNRELATED_API_TOKEN", "must-not-cross-boundary")
    monkeypatch.setenv("GIMME_INTERACTIVE_SUDO", "must-not-cross-boundary")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/test-agent.sock")
    monkeypatch.setattr(subprocess, "run", fake_run)

    runner(tmp_path).run("deploy", server())

    assert "UNRELATED_API_TOKEN" not in captured
    assert "GIMME_INTERACTIVE_SUDO" not in captured
    assert captured["SSH_AUTH_SOCK"] == "/tmp/test-agent.sock"


def test_macos_runner_replaces_an_empty_agent_with_the_launchd_agent(
    tmp_path: Path, monkeypatch
) -> None:
    empty_path = Path("/tmp/gimme-empty-agent.sock")
    launchd_path = Path("/tmp/gimme-launchd-agent.sock")
    captured: dict[str, str] = {}

    def fake_run(command, *args, **kwargs):
        if command == ["/usr/bin/ssh-add", "-l"]:
            return subprocess.CompletedProcess(
                command,
                1 if kwargs["env"]["SSH_AUTH_SOCK"] == str(empty_path) else 0,
                "",
            )
        if command == ["/bin/launchctl", "getenv", "SSH_AUTH_SOCK"]:
            return subprocess.CompletedProcess(command, 0, str(launchd_path) + "\n")
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, "ok")

    monkeypatch.setattr(deployer_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        Path,
        "is_socket",
        lambda path: path in {empty_path, launchd_path},
    )
    monkeypatch.setenv("SSH_AUTH_SOCK", str(empty_path))
    monkeypatch.setattr(subprocess, "run", fake_run)
    runner(tmp_path).run("deploy", server())

    assert captured["SSH_AUTH_SOCK"] == str(launchd_path)


def test_trust_bundle_digest_crosses_the_runner_boundary_under_the_name_the_recipe_reads(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner(tmp_path).run(
        "gimme:resource:bind-postgres", server(), resource_trust_bundle_sha256="a" * 64
    )

    assert captured["GIMME_RESOURCE_TRUST_BUNDLE_SHA256"] == "a" * 64
    recipe = (Path(__file__).resolve().parents[1] / "deploy.php").read_text()
    assert "required_env('GIMME_RESOURCE_TRUST_BUNDLE_SHA256')" in recipe


def test_stack_tasks_can_connect_to_bootstrap_hostname(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner(tmp_path).run("gimme:provision:stack", server(), bootstrap=True)

    assert captured["GIMME_SSH_HOSTNAME"] == "192.0.2.10"
    assert captured["GIMME_HOSTNAME"] == "devbox.local"


def test_interactive_sudo_inherits_all_terminal_streams(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, None)

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = runner(tmp_path).run(
        "gimme:provision:stack",
        server(),
        bootstrap=True,
        interactive_sudo=True,
    )

    assert captured["stdin"] is None
    assert captured["stdout"] is None
    assert captured["stderr"] is None
    assert "--no-interaction" not in captured["command"]
    assert captured["env"]["GIMME_INTERACTIVE_SUDO"] == "1"
    assert result.output == ""


def test_artisan_invocation_crosses_the_runner_boundary_as_json(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner(tmp_path).run(
        "gimme:artisan",
        server(),
        artisan_command="migrate",
        artisan_arguments=["--force"],
        artisan_allowed_commands=["about", "migrate"],
    )

    assert captured["GIMME_ARTISAN_COMMAND"] == "migrate"
    assert json.loads(captured["GIMME_ARTISAN_ARGS_JSON"]) == ["--force"]
    assert json.loads(captured["GIMME_ARTISAN_ALLOWED_JSON"]) == ["about", "migrate"]


def test_process_configuration_crosses_the_runner_boundary_as_json(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        workers=HorizonWorkerConfig(),
        scheduler=SchedulerConfig(),
        health=HealthCheckConfig(path="/up", attempts=5),
    )

    runner(tmp_path).run("gimme:preflight:processes", server(), app_name="example-app", app=app)

    assert json.loads(captured["GIMME_WORKERS_JSON"])["driver"] == "horizon"
    assert json.loads(captured["GIMME_SCHEDULER_JSON"]) == {"enabled": True}
    assert json.loads(captured["GIMME_HEALTH_JSON"]) == [{
        "name": "primary",
        "phases": ["candidate", "live"],
        "path": "/up",
        "expected_status": 200,
        "attempts": 5,
        "delay_seconds": 2,
        "timeout_seconds": 5,
    }]


def test_multiple_health_probes_cross_runner_boundary_with_phase_policy(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        health=HealthCheckConfig(name="framework", path="/up"),
        health_probes=[HealthCheckConfig(name="homepage", path="/", phases=["live"])],
    )

    runner(tmp_path).run("deploy", server(), app_name="example-app", app=app)

    probes = json.loads(captured["GIMME_HEALTH_JSON"])
    assert [(probe["name"], probe["phases"]) for probe in probes] == [
        ("framework", ["candidate", "live"]),
        ("homepage", ["live"]),
    ]


def test_environment_context_crosses_runner_boundary(tmp_path: Path, monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)
    app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        health=HealthCheckConfig(path="/up"),
        environments={
            "default": EnvironmentConfig(branch="main"),
            "feature-x": EnvironmentConfig(
                branch="feature/x", app_env="local", app_debug=True, health=None
            ),
        },
    )

    runner(tmp_path).run(
        "deploy",
        server(),
        app_name="example-app",
        app=app,
        environment_name="feature-x",
        revision="a" * 40,
    )

    assert captured["GIMME_ENVIRONMENT"] == "feature-x"
    assert captured["GIMME_INSTANCE"] == "example-app--feature-x--ef4c19f581"
    assert captured["GIMME_DEPLOY_PATH"] == ("/srv/gimme/apps/example-app/environments/feature-x")
    assert captured["GIMME_SITE_HOST"] == "feature-x.example-app.devbox.local"
    assert captured["GIMME_BRANCH"] == "feature/x"
    assert captured["GIMME_APP_ENV"] == "local"
    assert captured["GIMME_APP_DEBUG"] == "true"
    assert captured["GIMME_REVISION"] == "a" * 40
    assert json.loads(captured["GIMME_HEALTH_JSON"]) == []
