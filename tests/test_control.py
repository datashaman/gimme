import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from gimme.config import FrontendBuildConfig, HealthCheckConfig, StackConfig
from gimme.control import (
    ApplicationConfig,
    ControlState,
    DeploymentConfig,
    DeploymentSource,
    StateStore,
    TargetConfig,
    TargetNetwork,
    TargetToolchains,
    new_placement,
)


def target(mode: str = "local_mdns") -> TargetConfig:
    network = (
        TargetNetwork(mode="local_mdns", mdns_name="devbox")
        if mode == "local_mdns"
        else TargetNetwork(mode="public_dns", expected_addresses=["192.0.2.10"])
    )
    return TargetConfig(
        host_alias="devbox",
        bootstrap_hostname="192.0.2.10",
        hostname="devbox.local" if mode == "local_mdns" else "deploy.example.test",
        system_hostname="devbox",
        remote_user="deployer",
        apps_root="/srv/gimme/apps",
        network=network,
        stack=StackConfig(package_manager="apt", packages=["git"], services=[]),
    )


def application() -> ApplicationConfig:
    return ApplicationConfig(
        repository="git@github.com:example/application.git",
        framework="laravel",
        default_health=HealthCheckConfig(path="/up"),
    )


def deployment(target_config: TargetConfig, **updates: object) -> DeploymentConfig:
    values = {
        "application": "example",
        "target": "devbox",
        "stage": "local",
        "source": DeploymentSource(kind="branch", ref="main"),
        "app_env": "local",
        "app_debug": True,
        "placement": new_placement("example-local", target_config),
    }
    values.update(updates)
    return DeploymentConfig.model_validate(values)


def test_control_state_references_registered_target_and_application() -> None:
    devbox = target()
    state = ControlState(
        targets={"devbox": devbox},
        applications={"example": application()},
        deployments={"example-local": deployment(devbox)},
    )

    assert state.schema_version == 2
    assert state.deployments["example-local"].placement.site_host == (
        "example-local.devbox.local"
    )


def test_production_policy_is_hard() -> None:
    public = target("public_dns")
    unsafe = {
        "application": "example",
        "target": "devbox",
        "stage": "production",
        "source": {"kind": "branch", "ref": "main"},
        "app_env": "production",
        "app_debug": False,
        "domain": "app.example.test",
        "health": "inherit",
        "placement": new_placement(
            "example-production", public, domain="app.example.test"
        ),
    }

    with pytest.raises(ValidationError, match="exact commit"):
        ControlState(
            targets={"devbox": public},
            applications={"example": application()},
            deployments={"example-production": DeploymentConfig.model_validate(unsafe)},
        )


def test_staging_requires_health_and_debug_off() -> None:
    public = target("public_dns")
    no_health = ApplicationConfig(
        repository="https://github.com/example/application.git", framework="laravel"
    )
    candidate = deployment(
        public,
        stage="staging",
        app_env="staging",
        app_debug=False,
        domain="staging.example.test",
        health="inherit",
        placement=new_placement(
            "example-staging", public, domain="staging.example.test"
        ),
    )

    with pytest.raises(ValidationError, match="health gate"):
        ControlState(
            targets={"devbox": public},
            applications={"example": no_health},
            deployments={"example-staging": candidate},
        )


def test_environment_values_cannot_override_managed_keys() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        deployment(target(), variables={"APP_ENV": "hacked"})


@pytest.mark.parametrize("manager", ["npm", "pnpm", "yarn", "bun"])
def test_all_frontend_managers_are_supported(manager: str) -> None:
    frontend = FrontendBuildConfig(package_manager=manager)
    assert frontend.package_manager == manager


def test_toolchain_versions_are_exact() -> None:
    assert TargetToolchains(node="22.12.0", pnpm="9.15.0").pnpm == "9.15.0"
    with pytest.raises(ValidationError):
        TargetToolchains(node=">=22")


def test_state_store_writes_one_atomic_versioned_document(tmp_path: Path) -> None:
    devbox = target()
    state = ControlState(
        targets={"devbox": devbox},
        applications={"example": application()},
        deployments={"example-local": deployment(devbox)},
    )
    store = StateStore(tmp_path)
    store.save(state)

    assert store.load() == state
    assert json.loads((tmp_path / "state.json").read_text())["schema_version"] == 2
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


def test_legacy_migration_preserves_remote_placement(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "server.json").write_text(json.dumps({
        "host_alias": "devbox",
        "bootstrap_hostname": "192.0.2.10",
        "hostname": "devbox.local",
        "mdns_name": "devbox",
        "remote_user": "deployer",
        "apps_root": "/srv/gimme/apps",
        "keep_releases": 5,
    }))
    (config / "stack.json").write_text(json.dumps({
        "package_manager": "apt", "packages": ["git"], "services": []
    }))
    (config / "apps.json").write_text(json.dumps({"apps": {"example": {
        "repository": "git@github.com:example/application.git",
        "framework": "laravel",
        "environments": {
            "default": {"branch": "main", "app_env": "local", "app_debug": True},
            "feature": {"branch": "feature/demo", "app_env": "local", "app_debug": True},
        },
    }}}))

    migrated = StateStore(config, tmp_path).legacy_migration()

    assert migrated.deployments["example"].placement.relative_path == "example"
    feature = migrated.deployments["example-feature"].placement
    assert feature.relative_path == "example/environments/feature"
    assert feature.site_host == "feature.example.devbox.local"
    assert feature.database_identifier.startswith("gimme_example_feature_")
