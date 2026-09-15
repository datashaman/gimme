import json
from pathlib import Path

import pytest

from gimme.config import AppConfig, ConfigStore, FrontendBuildConfig


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
