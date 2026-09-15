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
    ServerConfig,
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
