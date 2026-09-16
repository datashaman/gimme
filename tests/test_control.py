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
    ResourceBindings,
    ResourceConfig,
    RuntimePin,
    StateStore,
    TargetConfig,
    TargetNetwork,
    TargetRuntimePolicy,
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
        "runtimes": {
            "php": RuntimePin(provider="system", version="8.4.1"),
            "composer": RuntimePin(provider="system", version="2.8.4"),
        },
        "resources": ResourceBindings(database="devbox-postgres", cache="devbox-valkey"),
        "placement": new_placement("example-local", target_config),
    }
    values.update(updates)
    return DeploymentConfig.model_validate(values)


def resources() -> dict[str, ResourceConfig]:
    return {
        "devbox-postgres": ResourceConfig(target="devbox", kind="postgres", version="17.2"),
        "devbox-valkey": ResourceConfig(target="devbox", kind="valkey", version="8.0.1"),
    }


def test_control_state_references_registered_target_and_application() -> None:
    devbox = target()
    state = ControlState(
        targets={"devbox": devbox},
        applications={"example": application()},
        resources=resources(),
        deployments={"example-local": deployment(devbox)},
    )

    assert state.schema_version == 3
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
        "runtimes": {
            "php": {"provider": "system", "version": "8.4.1"},
            "composer": {"provider": "system", "version": "2.8.4"},
        },
        "resources": {"database": "devbox-postgres", "cache": "devbox-valkey"},
        "placement": new_placement(
            "example-production", public, domain="app.example.test"
        ),
    }

    with pytest.raises(ValidationError, match="exact commit"):
        ControlState(
            targets={"devbox": public},
            applications={"example": application()},
            resources=resources(),
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
            resources=resources(),
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
    assert RuntimePin(provider="mise", version="22.12.0").version == "22.12.0"
    assert TargetRuntimePolicy(mise_version="2026.9.3").mise_version == "2026.9.3"
    with pytest.raises(ValidationError):
        RuntimePin(provider="mise", version=">=22")


def test_state_store_writes_one_atomic_versioned_document(tmp_path: Path) -> None:
    devbox = target()
    state = ControlState(
        targets={"devbox": devbox},
        applications={"example": application()},
        resources=resources(),
        deployments={"example-local": deployment(devbox)},
    )
    store = StateStore(tmp_path)
    store.save(state)

    assert store.load() == state
    assert json.loads((tmp_path / "state.json").read_text())["schema_version"] == 3
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

    migrated = StateStore(config, tmp_path).legacy_migration({
        "devbox": {
            "php": "8.4.1", "composer": "2.8.4", "postgres": "17.2",
            "valkey": "8.0.1",
        }
    })

    assert migrated.deployments["example"].placement.relative_path == "example"
    feature = migrated.deployments["example-feature"].placement
    assert feature.relative_path == "example/environments/feature"
    assert feature.site_host == "feature.example.devbox.local"
    assert feature.database_identifier.startswith("gimme_example_feature_")


def test_schema_v2_migration_pins_observed_versions_without_changing_placement(
    tmp_path: Path,
) -> None:
    document = json.loads((Path(__file__).parents[1] / "config/state.example.json").read_text())
    document["schema_version"] = 2
    document.pop("resources")
    document["targets"]["devbox"]["toolchains"] = {
        "node": "22.12.0", "npm": "10.9.0", "pnpm": None, "yarn": None, "bun": None,
    }
    document["targets"]["devbox"].pop("runtimes")
    old_placement = document["deployments"]["example-local"]["placement"]
    document["deployments"]["example-local"].pop("runtimes")
    document["deployments"]["example-local"].pop("resources")
    store = StateStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps(document))

    migrated = store.state_migration({
        "devbox": {
            "php": "8.4.1", "composer": "2.8.4", "node": "22.12.0",
            "npm": "10.9.0", "postgres": "17.2", "valkey": "8.0.1",
        }
    })

    assert migrated.schema_version == 3
    assert migrated.deployments["example-local"].placement.model_dump(mode="json") == old_placement
    assert migrated.deployments["example-local"].runtimes["node"].provider == "system"
    assert migrated.deployments["example-local"].resources.database == "devbox-postgres"


def test_mise_pin_requires_an_exact_target_mise_version() -> None:
    devbox = target()
    app = application().model_copy(update={"frontend": FrontendBuildConfig(package_manager="pnpm")})
    candidate = deployment(devbox, runtimes={
        "php": RuntimePin(provider="system", version="8.4.1"),
        "composer": RuntimePin(provider="system", version="2.8.4"),
        "node": RuntimePin(provider="mise", version="22.12.0"),
        "pnpm": RuntimePin(provider="mise", version="9.15.0"),
    })

    with pytest.raises(ValidationError, match="mise_version"):
        ControlState(
            targets={"devbox": devbox}, applications={"example": app},
            resources=resources(), deployments={"example-local": candidate},
        )


def test_mise_target_requires_fixed_repository_bootstrap_packages() -> None:
    with pytest.raises(ValidationError, match="software-properties-common"):
        TargetConfig.model_validate({
            **target().model_dump(mode="json"),
            "runtimes": {"mise_version": "2026.9.9"},
        })
