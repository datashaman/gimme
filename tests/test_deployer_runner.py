import subprocess
from pathlib import Path

from gimme.config import ServerConfig
from gimme.deployer import DeployerRunner


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


def test_normal_tasks_connect_to_advertised_hostname(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner(tmp_path).run("deploy", server())

    assert captured["GIMME_SSH_HOSTNAME"] == "devbox.local"
    assert captured["GIMME_HOSTNAME"] == "devbox.local"


def test_stack_tasks_can_connect_to_bootstrap_hostname(
    tmp_path: Path, monkeypatch
) -> None:
    captured: dict[str, str] = {}

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return subprocess.CompletedProcess(args[0], 0, "ok")

    monkeypatch.setattr(subprocess, "run", fake_run)

    runner(tmp_path).run("gimme:provision:stack", server(), bootstrap=True)

    assert captured["GIMME_SSH_HOSTNAME"] == "192.0.2.10"
    assert captured["GIMME_HOSTNAME"] == "devbox.local"
