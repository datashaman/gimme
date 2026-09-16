import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gimme.config import (
    AppConfig,
    ArtisanConfig,
    ArtisanInvocation,
    ConfigStore,
    FrontendBuildConfig,
    HealthCheckConfig,
    HorizonWorkerConfig,
    QueueWorkerConfig,
    SchedulerConfig,
    ServerConfig,
    EnvironmentConfig,
)


def make_store(tmp_path: Path) -> ConfigStore:
    config = tmp_path / "config"
    config.mkdir()
    (config / "server.json").write_text(
        json.dumps(
            {
                "host_alias": "devbox",
                "bootstrap_hostname": "192.0.2.10",
                "hostname": "devbox.local",
                "mdns_name": "devbox",
                "remote_user": "deployer",
                "apps_root": "/srv/gimme/apps",
                "keep_releases": 5,
            }
        )
    )
    (config / "apps.json").write_text('{"apps": {}}')
    (config / "stack.json").write_text(
        '{"package_manager":"apt","packages":["postgresql","valkey-server"],'
        '"services":["postgresql","valkey-server"]}'
    )
    return ConfigStore(tmp_path)


def test_register_app_is_idempotent(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    app = AppConfig(repository="git@example.test:acme/app.git", framework="laravel")

    assert store.register_app("acme", app) is True
    assert store.register_app("acme", app) is False
    assert store.app("acme") == app


def test_legacy_app_definition_becomes_a_default_environment() -> None:
    app = AppConfig.model_validate(
        {
            "repository": "git@example.test:acme/app.git",
            "framework": "laravel",
            "branch": "stable",
            "workers": {"driver": "horizon", "enabled": True},
            "scheduler": {"enabled": True},
        }
    )

    assert app.environment("default").branch == "stable"
    assert app.environment("default").workers.driver == "horizon"
    assert app.environment("default").scheduler.enabled is True
    assert "branch" not in app.model_dump()
    assert app.model_dump()["environments"]["default"]["branch"] == "stable"


def test_register_additional_environment_is_idempotent_and_preserves_default(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    store.register_app(
        "acme",
        AppConfig(
            repository="git@example.test:acme/app.git",
            framework="laravel",
            branch="main",
            workers=HorizonWorkerConfig(),
        ),
    )

    environment = EnvironmentConfig(branch="feature/worktrees")
    assert store.register_environment("acme", "feature-x", environment) is True
    assert store.register_environment("acme", "feature-x", environment) is False
    assert store.environment("acme", "default").branch == "main"
    assert store.environment("acme", "feature-x").branch == "feature/worktrees"
    assert store.environment("acme", "feature-x").workers is None


@pytest.mark.parametrize("name", ["default", "../bad", "Bad", "bad_name", "x" * 33])
def test_rejects_reserved_or_unsafe_additional_environment_names(
    tmp_path: Path, name: str
) -> None:
    store = make_store(tmp_path)
    store.register_app("acme", AppConfig(repository="https://example.test/app.git"))

    with pytest.raises(ValueError):
        store.register_environment("acme", name, EnvironmentConfig(branch="feature/x"))


def test_configure_app_processes_preserves_the_deployment_definition(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    original = AppConfig(
        repository="git@example.test:acme/app.git",
        framework="laravel",
        branch="stable",
    )
    store.register_app("acme", original)

    changed = store.configure_app_processes(
        "acme", HorizonWorkerConfig(), SchedulerConfig()
    )
    updated = store.app("acme")

    assert changed is True
    assert updated.repository == original.repository
    assert updated.branch == "stable"
    assert updated.workers.driver == "horizon"
    assert updated.scheduler.enabled is True


def test_configure_app_health_preserves_the_deployment_definition(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    original = AppConfig(
        repository="git@example.test:acme/app.git",
        framework="laravel",
        branch="stable",
        workers=HorizonWorkerConfig(),
    )
    store.register_app("acme", original)

    health = HealthCheckConfig(
        path="/up",
        expected_status=200,
        attempts=5,
        delay_seconds=2,
        timeout_seconds=3,
    )
    changed = store.configure_app_health("acme", health)
    updated = store.app("acme")

    assert changed is True
    assert updated.repository == original.repository
    assert updated.branch == "stable"
    assert updated.workers == original.workers
    assert updated.health == health


@pytest.mark.parametrize(
    "path",
    ["up", "//up", "/../secret", "/up?debug=1", "/up#fragment", "/up%0aevil"],
)
def test_health_path_is_a_bounded_absolute_url_path(path: str) -> None:
    with pytest.raises(ValidationError):
        HealthCheckConfig(path=path)


def test_health_checks_require_laravel() -> None:
    with pytest.raises(ValidationError, match="Laravel"):
        AppConfig(
            repository="https://example.test/app.git",
            framework="symfony",
            health=HealthCheckConfig(),
        )


@pytest.mark.parametrize("name", ["../bad", "Bad", "-bad", "bad_name", ""])
def test_rejects_unsafe_app_names(tmp_path: Path, name: str) -> None:
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        store.register_app(name, AppConfig(repository="https://example.test/app.git"))


def test_rejects_non_git_repository() -> None:
    with pytest.raises(ValueError):
        AppConfig(repository="/tmp/application")


@pytest.mark.parametrize(
    "repository",
    [
        "https://token@github.com/acme/app.git",
        "https://github.com/acme/app.git?token=secret",
        "https://github.com/acme/app.git#fragment",
        "git@github.com:../app.git",
        "git@github.com:acme/app.git\nmalicious",
        "ssh://-oProxyCommand@github.com/acme/app.git",
        "https://github.com/acme/app%0aevil.git",
    ],
)
def test_rejects_credentialed_or_malformed_git_repositories(repository: str) -> None:
    with pytest.raises(ValueError):
        AppConfig(repository=repository)


@pytest.mark.parametrize(
    "repository",
    [
        "https://github.com/acme/app.git",
        "ssh://git@github.com/acme/app.git",
        "git@github.com:acme/app.git",
    ],
)
def test_accepts_credential_free_git_repositories(repository: str) -> None:
    assert AppConfig(repository=repository).repository == repository


@pytest.mark.parametrize(
    "branch",
    [
        "-main",
        "feature branch",
        "main..evil",
        "feature@{1}",
        "topic.lock",
        "a~b",
        "main\x01evil",
    ],
)
def test_rejects_unsafe_git_refs(branch: str) -> None:
    with pytest.raises(ValueError):
        AppConfig(repository="https://github.com/acme/app.git", branch=branch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("hostname", "-oProxyCommand=evil"),
        ("hostname", "host/name"),
        ("bootstrap_hostname", "host\nname"),
        ("apps_root", "/etc/gimme"),
        ("apps_root", "/"),
        ("apps_root", "/srv/gimme/%n"),
        ("apps_root", "/srv/gimme/app root"),
    ],
)
def test_rejects_unsafe_server_boundaries(field: str, value: str) -> None:
    values = {
        "host_alias": "devbox",
        "bootstrap_hostname": "192.0.2.10",
        "hostname": "devbox.local",
        "mdns_name": "devbox",
        "remote_user": "deployer",
        "apps_root": "/srv/gimme/apps",
    }
    values[field] = value

    with pytest.raises(ValueError):
        ServerConfig(**values)


def test_stack_manifest_is_validated(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert store.stack().packages == ["postgresql", "valkey-server"]

    store.stack_path.write_text(
        '{"package_manager":"apt","packages":["postgresql; reboot"],"services":[]}'
    )
    with pytest.raises(ValueError):
        store.stack()


def test_static_app_requires_safe_frontend_build() -> None:
    with pytest.raises(ValueError):
        AppConfig(repository="https://example.test/site.git", framework="static")

    app = AppConfig(
        repository="https://example.test/site.git",
        framework="static",
        frontend=FrontendBuildConfig(output_dir="dist"),
    )
    assert app.frontend.output_dir == "dist"

    with pytest.raises(ValueError):
        FrontendBuildConfig(output_dir="../outside")

    with pytest.raises(ValueError):
        FrontendBuildConfig(output_dir="dist;touch-pwned")


def test_laravel_apps_receive_a_conservative_artisan_allowlist() -> None:
    app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
    )

    assert app.artisan is not None
    assert "about" in app.artisan.allowed_commands
    assert "migrate" in app.artisan.allowed_commands
    assert "tinker" not in app.artisan.allowed_commands
    assert "db:wipe" not in app.artisan.allowed_commands
    assert "migrate:fresh" not in app.artisan.allowed_commands


def test_artisan_configuration_is_laravel_only_and_strictly_validated() -> None:
    with pytest.raises(ValidationError):
        AppConfig(
            repository="https://example.test/app.git",
            framework="symfony",
            artisan=ArtisanConfig(allowed_commands=["about"]),
        )

    with pytest.raises(ValidationError):
        ArtisanConfig(allowed_commands=["about", "about"])

    with pytest.raises(ValidationError):
        ArtisanConfig(allowed_commands=["about; reboot"])


@pytest.mark.parametrize(
    "arguments",
    [
        ["safe\nunsafe"],
        ["\x00"],
        ["x" * 257],
        ["argument"] * 33,
        ["--env=testing"],
        ["--env", "testing"],
    ],
)
def test_artisan_invocation_rejects_unsafe_arguments(arguments: list[str]) -> None:
    with pytest.raises(ValidationError):
        ArtisanInvocation(command="about", arguments=arguments)


def test_laravel_process_configuration_supports_queue_workers_or_horizon() -> None:
    queue_app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        workers=QueueWorkerConfig(
            processes=3,
            connection="database",
            queues=["high", "default"],
        ),
        scheduler=SchedulerConfig(),
    )
    horizon_app = AppConfig(
        repository="https://example.test/app.git",
        framework="laravel",
        workers=HorizonWorkerConfig(stop_wait_seconds=3600),
    )

    assert queue_app.workers.driver == "queue"
    assert queue_app.workers.processes == 3
    assert queue_app.scheduler.enabled is True
    assert horizon_app.workers.driver == "horizon"


def test_process_configuration_is_laravel_only_and_rejects_unsafe_values() -> None:
    with pytest.raises(ValidationError, match="Laravel"):
        AppConfig(
            repository="https://example.test/app.git",
            framework="symfony",
            workers=QueueWorkerConfig(),
        )

    with pytest.raises(ValidationError):
        QueueWorkerConfig(processes=0)

    with pytest.raises(ValidationError):
        QueueWorkerConfig(queues=["default; reboot"])

    with pytest.raises(ValidationError):
        QueueWorkerConfig(queues=["default", "default"])
