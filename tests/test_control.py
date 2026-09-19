import json
from pathlib import Path
from typing import Literal

import pytest
from pydantic import ValidationError

from gimme.config import FrontendBuildConfig, HealthCheckConfig, StackConfig
from gimme.control import (
    ApplicationConfig,
    AWSNetwork,
    AWSProviderAccount,
    AWSRDSPostgresResource,
    AWSSecretsManagerStore,
    ControlState,
    CredentialReferenceBackupAuth,
    DeploymentConfig,
    DeploymentSource,
    RecoveryPolicy,
    Resource,
    ResourceBindings,
    ValkeyBinding,
    ResourceConfig,
    RuntimePin,
    S3BackupDestination,
    SecretReference,
    SSEAES256,
    SSEKMS,
    StateStore,
    TargetConfig,
    TargetNetwork,
    TargetRuntimePolicy,
    new_placement,
)


LOCAL_VALKEY = ValkeyBinding(resource="devbox-valkey", uses=["cache"])


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
        "resources": ResourceBindings(database="devbox-postgres", valkey=LOCAL_VALKEY),
        "placement": new_placement("example-local", target_config),
    }
    values.update(updates)
    return DeploymentConfig.model_validate(values)


def resources() -> dict[str, Resource]:
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

    assert state.schema_version == 5
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
        "resources": {"database": "devbox-postgres", "valkey": LOCAL_VALKEY.model_dump()},
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

    candidate_only = no_health.model_copy(
        update={
            "health_probes": [
                HealthCheckConfig(name="candidate", phases=["candidate"])
            ]
        }
    )
    with pytest.raises(ValidationError, match="candidate and live"):
        ControlState(
            targets={"devbox": public},
            applications={"example": candidate_only},
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
    assert json.loads((tmp_path / "state.json").read_text())["schema_version"] == 5
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600


def test_canonical_state_example_validates_against_current_schema() -> None:
    example = Path(__file__).parents[1] / "config" / "state.example.json"

    state = ControlState.model_validate_json(example.read_text())

    assert state.schema_version == 5
    assert state.targets["devbox"].runtimes.mise_version == "2026.9.9"


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
    document.pop("aws_networks", None)
    document["targets"].pop("adminbox", None)
    document["targets"]["devbox"]["toolchains"] = {
        "node": "22.12.0", "npm": "10.9.0", "pnpm": None, "yarn": None, "bun": None,
    }
    document["targets"]["devbox"]["stack"]["packages"] = [
        package for package in document["targets"]["devbox"]["stack"]["packages"]
        if package not in {"mise", "software-properties-common"}
    ]
    document["targets"]["devbox"].pop("runtimes")
    old_placement = document["deployments"]["example-local"]["placement"]
    document["deployments"]["example-local"].pop("runtimes")
    document["deployments"]["example-local"].pop("resources")
    document["deployments"]["example-local"]["secrets"] = {
        "MAIL_PASSWORD": "example-local/MAIL_PASSWORD"
    }
    store = StateStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps(document))

    migrated = store.state_migration({
        "devbox": {
            "php": "8.4.1", "composer": "2.8.4", "node": "22.12.0",
            "npm": "10.9.0", "postgres": "17.2", "valkey": "8.0.1",
        }
    })

    assert migrated.schema_version == 5
    assert migrated.deployments["example-local"].placement.model_dump(mode="json") == old_placement
    assert migrated.deployments["example-local"].runtimes["node"].provider == "system"
    assert migrated.deployments["example-local"].resources.database == "devbox-postgres"


def test_schema_v3_migration_structures_local_sops_references(tmp_path: Path) -> None:
    document = json.loads((Path(__file__).parents[1] / "config/state.example.json").read_text())
    document["schema_version"] = 3
    document.pop("provider_accounts")
    document.pop("secret_stores")
    document.pop("aws_networks", None)
    document["targets"].pop("adminbox", None)
    document["resources"].pop("example-rds-postgres", None)
    document["resources"].pop("example-elasticache-valkey", None)
    document["deployments"]["example-local"]["secrets"] = {
        "MAIL_PASSWORD": "example-local/mail/MAIL_PASSWORD"
    }
    document["deployments"]["example-local"]["resources"] = {
        "database": "devbox-postgres", "cache": "devbox-valkey"
    }
    tmp_path.mkdir(exist_ok=True)
    (tmp_path / "state.json").write_text(json.dumps(document))

    migrated = StateStore(tmp_path).state_migration({})

    assert migrated.schema_version == 5
    assert migrated.secret_stores["local-sops"].provider == "sops"
    assert migrated.deployments["example-local"].resources.valkey == ValkeyBinding(
        resource="devbox-valkey", uses=["cache"]
    )
    reference = migrated.deployments["example-local"].secrets["MAIL_PASSWORD"]
    assert reference.model_dump() == {
        "store": "local-sops", "secret": "example-local/mail", "field": "MAIL_PASSWORD"
    }


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


def test_backup_destination_rejects_ip_literal_bucket() -> None:
    with pytest.raises(ValidationError, match="bucket"):
        S3BackupDestination(bucket="192.168.1.1", region="us-east-1", encryption=SSEAES256())


def test_backup_destination_requires_matching_region_kms_key() -> None:
    with pytest.raises(ValidationError, match="region"):
        S3BackupDestination(
            bucket="gimme-backups",
            region="us-east-1",
            encryption=SSEKMS(
                kms_key_arn="arn:aws:kms:us-west-2:123456789012:key/"
                "11111111-1111-1111-1111-111111111111"
            ),
        )


def test_backup_destination_endpoint_must_be_a_safe_host() -> None:
    with pytest.raises(ValidationError):
        S3BackupDestination(
            bucket="gimme-backups", region="us-east-1", endpoint="https://evil.test/path",
            encryption=SSEAES256(),
        )


def test_backup_destination_endpoint_accepts_a_safe_non_standard_port() -> None:
    destination = S3BackupDestination(
        bucket="gimme-backups", region="us-east-1", endpoint="127.0.0.1:9000",
        addressing="path", encryption=SSEAES256(),
    )

    assert destination.endpoint == "127.0.0.1:9000"


def test_backup_destination_endpoint_rejects_an_unsafe_port() -> None:
    with pytest.raises(ValidationError, match="port"):
        S3BackupDestination(
            bucket="gimme-backups", region="us-east-1", endpoint="127.0.0.1:99999",
            encryption=SSEAES256(),
        )


def test_backup_destination_endpoint_accepts_a_bracketed_ipv6_literal_with_port() -> None:
    destination = S3BackupDestination(
        bucket="gimme-backups", region="us-east-1", endpoint="[::1]:9000",
        addressing="path", encryption=SSEAES256(),
    )

    assert destination.endpoint == "[::1]:9000"


def test_backup_destination_endpoint_accepts_a_bracketed_ipv6_literal_without_port() -> None:
    destination = S3BackupDestination(
        bucket="gimme-backups", region="us-east-1", endpoint="[2001:db8::1]",
        addressing="path", encryption=SSEAES256(),
    )

    assert destination.endpoint == "[2001:db8::1]"


def test_backup_destination_endpoint_rejects_an_unbracketed_ipv6_literal() -> None:
    with pytest.raises(ValidationError, match="bracketed"):
        S3BackupDestination(
            bucket="gimme-backups", region="us-east-1", endpoint="2001:db8::1",
            encryption=SSEAES256(),
        )


def test_backup_destination_endpoint_rejects_an_unsafe_bracketed_ipv6_port() -> None:
    with pytest.raises(ValidationError, match="port"):
        S3BackupDestination(
            bucket="gimme-backups", region="us-east-1", endpoint="[::1]:99999",
            encryption=SSEAES256(),
        )


def test_backup_destination_endpoint_rejects_an_unterminated_ipv6_bracket() -> None:
    with pytest.raises(ValidationError):
        S3BackupDestination(
            bucket="gimme-backups", region="us-east-1", endpoint="[::1:9000",
            encryption=SSEAES256(),
        )


def test_deployment_recovery_requires_known_destination() -> None:
    devbox = target()
    candidate = deployment(devbox, recovery=RecoveryPolicy(destination="primary"))

    with pytest.raises(ValidationError, match="unknown backup destination"):
        ControlState(
            targets={"devbox": devbox}, applications={"example": application()},
            resources=resources(), deployments={"example-local": candidate},
        )


def test_deployment_recovery_requires_a_bound_database() -> None:
    devbox = target()
    static_app = ApplicationConfig(
        repository="git@github.com:example/site.git", framework="static",
        frontend=FrontendBuildConfig(package_manager="npm"),
    )
    candidate = deployment(
        devbox, application="site", resources=ResourceBindings(), secrets={},
        runtimes={
            "node": RuntimePin(provider="system", version="22.12.0"),
            "npm": RuntimePin(provider="bundled", version="10.9.0"),
        },
        recovery=RecoveryPolicy(destination="primary"),
    )

    with pytest.raises(ValidationError, match="requires a bound database"):
        ControlState(
            targets={"devbox": devbox}, applications={"site": static_app},
            backup_destinations={
                "primary": S3BackupDestination(
                    bucket="gimme-backups", region="us-east-1", encryption=SSEAES256()
                )
            },
            resources={}, deployments={"example-local": candidate},
        )


def test_deployment_recovery_binds_to_a_registered_destination() -> None:
    devbox = target()
    candidate = deployment(devbox, recovery=RecoveryPolicy(destination="primary"))
    state = ControlState(
        targets={"devbox": devbox}, applications={"example": application()},
        backup_destinations={
            "primary": S3BackupDestination(
                bucket="gimme-backups", region="us-east-1", encryption=SSEAES256()
            )
        },
        resources=resources(), deployments={"example-local": candidate},
    )

    assert state.deployments["example-local"].recovery.destination == "primary"


@pytest.mark.parametrize("wait", [0, 301, 1.5, "30"])
def test_recovery_quiesce_wait_is_a_bounded_integer(wait) -> None:
    with pytest.raises(ValidationError):
        RecoveryPolicy(destination="primary", quiesce_wait_seconds=wait)


@pytest.mark.parametrize("selected", [0, 1, "true", None])
def test_recovery_valkey_selection_is_a_strict_boolean(selected) -> None:
    with pytest.raises(ValidationError):
        RecoveryPolicy(destination="primary", valkey=selected)


def test_recovery_policy_defaults_to_postgres_without_valkey() -> None:
    policy = RecoveryPolicy(destination="primary")

    assert policy.valkey is False
    assert policy.quiesce_wait_seconds == 30


def _target_with_role(
    alias: str, role: Literal["deployment", "administration"] = "deployment"
) -> TargetConfig:
    return TargetConfig(
        host_alias=alias,
        bootstrap_hostname="192.0.2.10",
        hostname=f"{alias}.local",
        system_hostname=alias,
        remote_user="deployer",
        apps_root="/srv/gimme/apps",
        network=TargetNetwork(mode="local_mdns", mdns_name=alias),
        stack=StackConfig(package_manager="apt", packages=["git"], services=[]),
        role=role,
    )


def _aws_network() -> AWSNetwork:
    return AWSNetwork(
        provider_account="main",
        region="us-east-1",
        vpc_id="vpc-0123456789abcdef0",
        private_subnet_ids=["subnet-0123456789abcdef0", "subnet-0123456789abcdef1"],
    )


def _rds_resource(**updates: object) -> AWSRDSPostgresResource:
    values: dict[str, object] = {
        "aws_network": "primary",
        "administration_target": "adminbox",
        "engine_version": "17.2",
        "instance_class": "db.t3.medium",
        "allocated_storage_gb": 20,
        "administration_security_group_id": "sg-0123456789abcdef0",
        "deployment_security_group_ids": {"devbox": "sg-0123456789abcdef1"},
        "workload_secret_store": "workload-secrets",
    }
    values.update(updates)
    return AWSRDSPostgresResource.model_validate(values)


def _rds_state(**deployment_updates: object) -> ControlState:
    devbox = target()
    adminbox = _target_with_role("adminbox", role="administration")
    account = AWSProviderAccount(
        account_id="123456789012",
        inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
        resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
    )
    workload_store = AWSSecretsManagerStore(
        provider_account="main", region="us-east-1", prefix="gimme/workload",
    )
    resource = _rds_resource()
    candidate = deployment(devbox, **deployment_updates)
    return ControlState(
        provider_accounts={"main": account},
        secret_stores={
            "local-sops": {"provider": "sops"},
            "workload-secrets": workload_store,
        },
        aws_networks={"primary": _aws_network()},
        targets={"devbox": devbox, "adminbox": adminbox},
        applications={"example": application()},
        resources={"devbox-postgres": resource, "devbox-valkey": resources()["devbox-valkey"]},
        deployments={"example-local": candidate},
    )


def test_aws_network_requires_two_distinct_subnets() -> None:
    with pytest.raises(ValidationError, match="two distinct data subnet"):
        AWSNetwork(
            provider_account="main", region="us-east-1", vpc_id="vpc-0123456789abcdef0",
            private_subnet_ids=["subnet-0123456789abcdef0", "subnet-0123456789abcdef0"],
        )


def test_aws_rds_resource_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _rds_resource(unexpected="nope")


def test_aws_rds_resource_requires_exact_engine_version() -> None:
    with pytest.raises(ValidationError, match="engine_version"):
        _rds_resource(engine_version=">=17")


def test_aws_rds_resource_deployment_security_groups_must_be_exact() -> None:
    with pytest.raises(ValidationError, match="deployment_security_group_ids"):
        _rds_resource(deployment_security_group_ids={"devbox": "not-a-security-group"})


def test_control_state_binds_a_deployment_to_a_managed_postgres_resource() -> None:
    state = _rds_state(
        resources=ResourceBindings(database="devbox-postgres", valkey=LOCAL_VALKEY)
    )

    assert state.resources["devbox-postgres"].provider == "aws_rds_postgres"
    assert state.deployments["example-local"].resources.database == "devbox-postgres"


def test_managed_postgres_resource_requires_a_registered_aws_network() -> None:
    devbox = target()
    resource = _rds_resource(aws_network="missing")
    with pytest.raises(ValidationError, match="unknown AWS Network"):
        ControlState(
            targets={"devbox": devbox}, applications={"example": application()},
            resources={"devbox-postgres": resource,
                      "devbox-valkey": resources()["devbox-valkey"]},
            deployments={"example-local": deployment(devbox, resources=ResourceBindings())},
        )


def test_managed_postgres_resource_requires_an_administration_target() -> None:
    devbox = target()
    account = AWSProviderAccount(
        account_id="123456789012",
        inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
        resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
    )
    resource = _rds_resource(administration_target="devbox")
    with pytest.raises(ValidationError, match="administration Target"):
        ControlState(
            provider_accounts={"main": account},
            aws_networks={"primary": _aws_network()},
            targets={"devbox": devbox}, applications={"example": application()},
            resources={"devbox-postgres": resource,
                      "devbox-valkey": resources()["devbox-valkey"]},
            deployments={"example-local": deployment(devbox, resources=ResourceBindings())},
        )


def test_administration_target_cannot_host_a_deployment() -> None:
    devbox = target().model_copy(update={"role": "administration"})
    with pytest.raises(ValidationError, match="Deployment Target"):
        ControlState(
            targets={"devbox": devbox}, applications={"example": application()},
            resources=resources(), deployments={"example-local": deployment(devbox)},
        )


def test_deployment_cannot_bind_managed_postgres_from_an_ineligible_target() -> None:
    devbox = target()
    other = _target_with_role("other")
    account = AWSProviderAccount(
        account_id="123456789012",
        inspection_role_arn="arn:aws:iam::123456789012:role/gimme-inspect",
        resolver_role_arn="arn:aws:iam::123456789012:role/gimme-resolve",
    )
    adminbox = _target_with_role("adminbox", role="administration")
    workload_store = AWSSecretsManagerStore(
        provider_account="main", region="us-east-1", prefix="gimme/workload",
    )
    resource = _rds_resource(deployment_security_group_ids={"other": "sg-0123456789abcdef1"})
    with pytest.raises(ValidationError, match="eligible Deployment Target"):
        ControlState(
            provider_accounts={"main": account},
            secret_stores={"local-sops": {"provider": "sops"},
                          "workload-secrets": workload_store},
            aws_networks={"primary": _aws_network()},
            targets={"devbox": devbox, "other": other, "adminbox": adminbox},
            applications={"example": application()},
            resources={"devbox-postgres": resource,
                      "devbox-valkey": resources()["devbox-valkey"]},
            deployments={"example-local": deployment(
                devbox,
                resources=ResourceBindings(database="devbox-postgres", valkey=LOCAL_VALKEY),
            )},
        )


def test_backup_destination_rejects_credential_reference_to_unknown_store() -> None:
    devbox = target()
    with pytest.raises(ValidationError, match="unknown secret store"):
        ControlState(
            targets={"devbox": devbox}, applications={"example": application()},
            resources=resources(),
            backup_destinations={
                "primary": S3BackupDestination(
                    bucket="gimme-backups", region="us-east-1", encryption=SSEAES256(),
                    auth=CredentialReferenceBackupAuth(
                        access_key_id=SecretReference(
                            store="missing", secret="minio", field="access_key_id"
                        ),
                        secret_access_key=SecretReference(
                            store="missing", secret="minio", field="secret_access_key"
                        ),
                    ),
                )
            },
        )
