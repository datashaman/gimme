from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gimme.config import (
    APP_NAME,
    LARAVEL_APP_ENV,
    ABSOLUTE_DIRECTORY,
    ArtisanConfig,
    ConfigStore,
    FrontendBuildConfig,
    HealthCheckConfig,
    SchedulerConfig,
    StackConfig,
    WorkerConfig,
    _valid_endpoint,
    _valid_git_branch,
)


TARGET_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DEPLOYMENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DOMAIN_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])$"
)
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SECRET_REF = re.compile(r"^[a-z][a-z0-9-]{0,63}(?:/[A-Z][A-Z0-9_]{0,63})+$")
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][a-zA-Z0-9.-]+)?$")
COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
RELATIVE_PATH = re.compile(r"^[a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+)*$")
RESERVED_ENV_KEYS = {
    "APP_ENV",
    "APP_DEBUG",
    "APP_URL",
    "DB_CONNECTION",
    "DB_HOST",
    "DB_PORT",
    "DB_DATABASE",
    "DB_USERNAME",
    "DB_PASSWORD",
    "CACHE_STORE",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PREFIX",
    "QUEUE_CONNECTION",
    "HORIZON_PREFIX",
}


class TargetNetwork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["local_mdns", "public_dns"]
    mdns_name: str | None = None
    expected_addresses: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def coherent(self) -> "TargetNetwork":
        if self.mode == "local_mdns":
            if self.mdns_name is None or TARGET_NAME.fullmatch(self.mdns_name) is None:
                raise ValueError("local_mdns targets require a safe mdns_name")
            if self.expected_addresses:
                raise ValueError("local_mdns targets do not accept expected_addresses")
        else:
            if self.mdns_name is not None:
                raise ValueError("public_dns targets do not accept mdns_name")
            if not self.expected_addresses:
                raise ValueError("public_dns targets require expected_addresses")
            for address in self.expected_addresses:
                try:
                    ip_address(address)
                except ValueError as exc:
                    raise ValueError(
                        "public_dns expected_addresses must contain literal IP addresses"
                    ) from exc
        return self


RuntimeName = Literal[
    "php", "composer", "node", "npm", "pnpm", "yarn", "bun",
    "python", "ruby", "go", "java",
]


class RuntimePin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["system", "mise", "bundled"]
    version: str

    @field_validator("version")
    @classmethod
    def exact_version(cls, value: str) -> str:
        if VERSION.fullmatch(value) is None:
            raise ValueError("runtime versions must be exact bounded version strings")
        return value


class TargetRuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mise_version: str | None = None

    @field_validator("mise_version")
    @classmethod
    def exact_mise_version(cls, value: str | None) -> str | None:
        if value is not None and VERSION.fullmatch(value) is None:
            raise ValueError("mise_version must be an exact version")
        return value


class ResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern=TARGET_NAME.pattern)
    kind: Literal["postgres", "valkey"]
    provider: Literal["target_local"] = "target_local"
    version: str

    @field_validator("version")
    @classmethod
    def exact_version(cls, value: str) -> str:
        if VERSION.fullmatch(value) is None:
            raise ValueError("resource versions must be exact bounded version strings")
        return value


class ResourceBindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: str | None = Field(default=None, pattern=DEPLOYMENT_NAME.pattern)
    cache: str | None = Field(default=None, pattern=DEPLOYMENT_NAME.pattern)


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_alias: str = Field(pattern=TARGET_NAME.pattern)
    bootstrap_hostname: str
    hostname: str
    system_hostname: str = Field(pattern=TARGET_NAME.pattern)
    remote_user: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$")
    apps_root: str
    keep_releases: int = Field(default=5, ge=2, le=20)
    network: TargetNetwork
    stack: StackConfig
    runtimes: TargetRuntimePolicy = Field(default_factory=TargetRuntimePolicy)

    @field_validator("bootstrap_hostname", "hostname")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return _valid_endpoint(value)

    @field_validator("apps_root")
    @classmethod
    def safe_apps_root(cls, value: str) -> str:
        if ABSOLUTE_DIRECTORY.fullmatch(value.rstrip("/")) is None or ".." in Path(value).parts:
            raise ValueError("apps_root must be a safe absolute directory")
        return value.rstrip("/")

    @model_validator(mode="after")
    def coherent_network_identity(self) -> "TargetConfig":
        if self.network.mode == "local_mdns":
            expected = f"{self.network.mdns_name}.local"
            if self.hostname != expected or self.system_hostname != self.network.mdns_name:
                raise ValueError(
                    "local_mdns hostname/system_hostname must match the advertised name"
                )
        required_mise_packages = {"mise", "software-properties-common"}
        declared = set(self.stack.packages)
        if self.runtimes.mise_version is not None and not required_mise_packages <= declared:
            raise ValueError(
                "mise targets require mise and software-properties-common packages"
            )
        if "mise" in declared and self.runtimes.mise_version is None:
            raise ValueError("the mise package requires an exact mise_version")
        return self


class ApplicationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=500)
    framework: Literal["common", "laravel", "symfony", "wordpress", "static"] = "common"
    frontend: FrontendBuildConfig | None = None
    artisan: ArtisanConfig | None = None
    default_health: HealthCheckConfig | None = None
    health_probes: list[HealthCheckConfig] = Field(default_factory=list, max_length=7)
    php_extensions: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("php_extensions")
    @classmethod
    def safe_php_extensions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"[a-z][a-z0-9_]{0,47}", extension) is None
            for extension in value
        ):
            raise ValueError("php_extensions must be unique safe extension names")
        return sorted(value)

    @model_validator(mode="after")
    def coherent(self) -> "ApplicationConfig":
        # Reuse the hardened repository/framework validation in the legacy model.
        from gimme.config import AppConfig, EnvironmentConfig

        validated = AppConfig(
            repository=self.repository,
            framework=self.framework,
            frontend=self.frontend,
            artisan=self.artisan,
            health=self.default_health,
            health_probes=self.health_probes,
            environments={"default": EnvironmentConfig()},
        )
        self.artisan = validated.artisan
        return self

class DeploymentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["branch", "tag", "commit"]
    ref: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def valid_ref(self) -> "DeploymentSource":
        if self.kind == "commit":
            if COMMIT.fullmatch(self.ref) is None:
                raise ValueError("commit sources require a 40- or 64-character lowercase hash")
        elif not _valid_git_branch(self.ref):
            raise ValueError("branch and tag sources require a safe Git ref")
        return self


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str = Field(pattern=r"^[a-z][a-z0-9-]{0,93}$")
    relative_path: str
    database_identifier: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    cache_prefix: str = Field(pattern=r"^[a-zA-Z0-9:_-]{1,160}$")
    site_host: str

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        parts = Path(value).parts
        if (
            RELATIVE_PATH.fullmatch(value) is None
            or ".." in parts
            or parts[0].startswith(".")
            or parts == ("deployments",)
        ):
            raise ValueError("relative_path must be safe and relative")
        return value

    @field_validator("site_host")
    @classmethod
    def safe_site_host(cls, value: str) -> str:
        return _valid_endpoint(value)


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application: str = Field(pattern=APP_NAME.pattern)
    target: str = Field(pattern=TARGET_NAME.pattern)
    stage: Literal["local", "preview", "staging", "production"]
    source: DeploymentSource
    app_env: str = "production"
    app_debug: bool = Field(default=False, strict=True)
    domain: str | None = None
    health: Literal["inherit"] | HealthCheckConfig | None = "inherit"
    health_probes: list[HealthCheckConfig] = Field(default_factory=list, max_length=7)
    workers: WorkerConfig | None = None
    scheduler: SchedulerConfig | None = None
    variables: dict[str, str] = Field(default_factory=dict, max_length=128)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=128)
    runtimes: dict[RuntimeName, RuntimePin]
    resources: ResourceBindings = Field(default_factory=ResourceBindings)
    placement: Placement

    @model_validator(mode="after")
    def unique_own_health_probes(self) -> "DeploymentConfig":
        probes = [*([self.health] if isinstance(self.health, HealthCheckConfig) else []),
                  *self.health_probes]
        names = [probe.name for probe in probes]
        if len(names) != len(set(names)):
            raise ValueError("deployment health probe names must be unique")
        return self

    @field_validator("app_env")
    @classmethod
    def safe_app_env(cls, value: str) -> str:
        if LARAVEL_APP_ENV.fullmatch(value) is None:
            raise ValueError("app_env is unsafe")
        return value

    @field_validator("domain")
    @classmethod
    def safe_domain(cls, value: str | None) -> str | None:
        if value is not None and DOMAIN_NAME.fullmatch(value) is None:
            raise ValueError("domain must be a lowercase fully qualified DNS name")
        return value

    @field_validator("variables")
    @classmethod
    def safe_variables(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if ENV_KEY.fullmatch(key) is None or key in RESERVED_ENV_KEYS:
                raise ValueError(f"environment key is reserved or unsafe: {key}")
            if len(item) > 4096 or "\x00" in item or "\n" in item or "\r" in item:
                raise ValueError(f"environment value is unsafe: {key}")
        return value

    @field_validator("secrets")
    @classmethod
    def safe_secrets(cls, value: dict[str, str]) -> dict[str, str]:
        for key, reference in value.items():
            if ENV_KEY.fullmatch(key) is None or key in RESERVED_ENV_KEYS:
                raise ValueError(f"secret environment key is reserved or unsafe: {key}")
            if SECRET_REF.fullmatch(reference) is None:
                raise ValueError(f"secret reference is unsafe: {reference}")
        return value


class DeploymentRegistration(BaseModel):
    """User-owned deployment fields; placement is allocated once by Gimme."""

    model_config = ConfigDict(extra="forbid")

    application: str = Field(pattern=APP_NAME.pattern)
    target: str = Field(pattern=TARGET_NAME.pattern)
    stage: Literal["local", "preview", "staging", "production"]
    source: DeploymentSource
    app_env: str = "production"
    app_debug: bool = Field(default=False, strict=True)
    domain: str | None = None
    health: Literal["inherit"] | HealthCheckConfig | None = "inherit"
    health_probes: list[HealthCheckConfig] = Field(default_factory=list, max_length=7)
    workers: WorkerConfig | None = None
    scheduler: SchedulerConfig | None = None
    variables: dict[str, str] = Field(default_factory=dict, max_length=128)
    secrets: dict[str, str] = Field(default_factory=dict, max_length=128)
    runtimes: dict[RuntimeName, RuntimePin]
    resources: ResourceBindings = Field(default_factory=ResourceBindings)

    def materialize(self, placement: Placement) -> DeploymentConfig:
        return DeploymentConfig(**self.model_dump(), placement=placement)

    @classmethod
    def from_deployment(cls, deployment: DeploymentConfig) -> "DeploymentRegistration":
        return cls.model_validate(deployment.model_dump(exclude={"placement"}))

    @field_validator("domain")
    @classmethod
    def safe_domain(cls, value: str | None) -> str | None:
        if value is not None and DOMAIN_NAME.fullmatch(value) is None:
            raise ValueError("domain must be a lowercase fully qualified DNS name")
        return value

    @field_validator("variables")
    @classmethod
    def safe_variables(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if ENV_KEY.fullmatch(key) is None or key in RESERVED_ENV_KEYS:
                raise ValueError(f"environment key is reserved or unsafe: {key}")
            if len(item) > 4096 or "\x00" in item or "\n" in item or "\r" in item:
                raise ValueError(f"environment value is unsafe: {key}")
        return value

    @field_validator("secrets")
    @classmethod
    def safe_secrets(cls, value: dict[str, str]) -> dict[str, str]:
        for key, reference in value.items():
            if ENV_KEY.fullmatch(key) is None or key in RESERVED_ENV_KEYS:
                raise ValueError(f"secret environment key is reserved or unsafe: {key}")
            if SECRET_REF.fullmatch(reference) is None:
                raise ValueError(f"secret reference is unsafe: {reference}")
        return value


class ControlState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[3] = 3
    targets: dict[str, TargetConfig] = Field(default_factory=dict)
    applications: dict[str, ApplicationConfig] = Field(default_factory=dict)
    resources: dict[str, ResourceConfig] = Field(default_factory=dict)
    deployments: dict[str, DeploymentConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references_exist(self) -> "ControlState":
        for name in self.targets:
            if TARGET_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid target name: {name}")
        for name in self.applications:
            if APP_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid application name: {name}")
        target_resource_versions: dict[tuple[str, str], str] = {}
        for name, resource in self.resources.items():
            if DEPLOYMENT_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid resource name: {name}")
            if resource.target not in self.targets:
                raise ValueError(f"resource {name} references an unknown target")
            key = (resource.target, resource.kind)
            previous = target_resource_versions.setdefault(key, resource.version)
            if previous != resource.version:
                raise ValueError(
                    f"target-local {resource.kind} resources on {resource.target} "
                    "must use one version"
                )
        for name, deployment in self.deployments.items():
            if DEPLOYMENT_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid deployment name: {name}")
            if deployment.application not in self.applications:
                raise ValueError(f"deployment {name} references an unknown application")
            if deployment.target not in self.targets:
                raise ValueError(f"deployment {name} references an unknown target")
            validate_stage_policy(
                deployment,
                self.targets[deployment.target],
                self.applications[deployment.application],
            )
            validate_runtime_policy(
                deployment,
                self.targets[deployment.target],
                self.applications[deployment.application],
            )
            for binding, kind in (
                (deployment.resources.database, "postgres"),
                (deployment.resources.cache, "valkey"),
            ):
                if binding is None:
                    continue
                resource = self.resources.get(binding)
                if resource is None:
                    raise ValueError(f"deployment {name} references unknown resource {binding}")
                if resource.target != deployment.target or resource.kind != kind:
                    raise ValueError(f"deployment {name} has an incompatible {kind} binding")
            is_static = self.applications[deployment.application].framework == "static"
            has_database = deployment.resources.database is not None
            has_cache = deployment.resources.cache is not None
            if is_static and (has_database or has_cache):
                raise ValueError(f"static deployment {name} cannot bind database or cache")
            if not is_static and (
                not has_database or not has_cache
            ):
                raise ValueError(f"deployment {name} requires database and cache bindings")
        return self


def validate_runtime_policy(
    deployment: DeploymentConfig,
    target: TargetConfig,
    application: ApplicationConfig,
) -> None:
    pins = deployment.runtimes
    if application.framework != "static":
        if "php" not in pins or "composer" not in pins:
            raise ValueError("non-static deployments require php and composer runtime pins")
        if pins["php"].provider != "system":
            raise ValueError("PHP-FPM deployments currently require the system PHP provider")
        if pins["composer"].provider != "system":
            raise ValueError("Composer currently requires the system provider")
    frontend = application.frontend
    if frontend is not None:
        manager = frontend.package_manager
        if manager in {"npm", "pnpm", "yarn"} and "node" not in pins:
            raise ValueError(f"{manager} deployments require a node runtime pin")
        if manager not in pins:
            raise ValueError(f"frontend package manager {manager} requires a runtime pin")
        if manager == "npm" and pins["npm"].provider != "bundled":
            raise ValueError("npm must use the bundled provider from the selected Node runtime")
    uses_mise = any(pin.provider == "mise" for pin in pins.values())
    if uses_mise and target.runtimes.mise_version is None:
        raise ValueError("mise runtime pins require target.runtimes.mise_version")
    for name, pin in pins.items():
        if pin.provider == "bundled" and name != "npm":
            raise ValueError("only npm supports the bundled runtime provider")


def validate_stage_policy(
    deployment: DeploymentConfig,
    target: TargetConfig,
    application: ApplicationConfig,
) -> None:
    health = application.default_health if deployment.health == "inherit" else deployment.health
    probes = [*([health] if health is not None else []), *application.health_probes,
              *deployment.health_probes]
    names = [probe.name for probe in probes]
    if len(names) != len(set(names)):
        raise ValueError("effective health probe names must be unique")
    if target.network.mode == "local_mdns":
        if deployment.domain is not None:
            raise ValueError("local_mdns deployments derive their domain")
    elif deployment.domain is None:
        raise ValueError("public_dns deployments require an explicit domain")
    if deployment.stage in {"staging", "production"}:
        if deployment.app_debug:
            raise ValueError(f"{deployment.stage} deployments cannot enable APP_DEBUG")
        phases = {phase for probe in probes for phase in probe.phases}
        if not {"candidate", "live"} <= phases:
            raise ValueError(
                f"{deployment.stage} deployments require candidate and live health gates"
            )
    if deployment.stage == "production":
        if target.network.mode != "public_dns":
            raise ValueError("production deployments require a public_dns target")
        if deployment.app_env != "production":
            raise ValueError("production deployments require APP_ENV=production")
        if deployment.source.kind != "commit":
            raise ValueError("production deployments require an exact commit source")


def new_placement(
    name: str, target: TargetConfig, *, domain: str | None = None
) -> Placement:
    digest = hashlib.sha256(name.encode()).hexdigest()[:10]
    normalized = name.replace("-", "_")
    database = f"gimme_{normalized}"
    if len(database) > 63:
        database = f"{database[:52]}_{digest}"
    site_label = name if len(name) <= 63 else f"{name[:52]}-{digest}"
    site_host = domain or f"{site_label}.{target.network.mdns_name}.local"
    return Placement(
        instance=name,
        relative_path=f"deployments/{name}",
        database_identifier=database,
        cache_prefix=f"gimme:{name}:",
        site_host=site_host,
    )


class StateStore:
    def __init__(self, root: Path, legacy_root: Path | None = None) -> None:
        self.root = root.expanduser().resolve()
        self.state_path = self.root / "state.json"
        self.secrets_path = self.root / "secrets.enc.json"
        self.lock_path = self.root / ".gimme.lock"
        self.legacy_root = (legacy_root or Path.cwd()).resolve()

    @classmethod
    def from_environment(cls, legacy_root: Path) -> "StateStore":
        configured = os.environ.get("GIMME_STATE_DIR")
        root = Path(configured) if configured else legacy_root / "config"
        return cls(root, legacy_root)

    def exists(self) -> bool:
        return self.state_path.is_file()

    def load(self) -> ControlState:
        if not self.exists():
            raise RuntimeError("state migration required; call plan_state_migration")
        document = self.raw_state()
        if document.get("schema_version") != 3:
            raise RuntimeError("state migration required; call plan_state_migration")
        return ControlState.model_validate(document)

    def raw_state(self) -> dict[str, object]:
        if not self.exists():
            raise RuntimeError("desired state does not exist")
        value = json.loads(self.state_path.read_text())
        if not isinstance(value, dict):
            raise RuntimeError("desired state must be a JSON object")
        return value

    def save(self, state: ControlState) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._atomic_json_write(self.state_path, state.model_dump(mode="json"))

    def target(self, name: str) -> TargetConfig:
        try:
            return self.load().targets[name]
        except KeyError as exc:
            raise KeyError(f"target '{name}' is not registered") from exc

    def application(self, name: str) -> ApplicationConfig:
        try:
            return self.load().applications[name]
        except KeyError as exc:
            raise KeyError(f"application '{name}' is not registered") from exc

    def deployment(self, name: str) -> DeploymentConfig:
        try:
            return self.load().deployments[name]
        except KeyError as exc:
            raise KeyError(f"deployment '{name}' is not registered") from exc

    def legacy_migration(
        self, observations: dict[str, dict[str, str]]
    ) -> ControlState:
        legacy = ConfigStore(self.legacy_root)
        server = legacy.server()
        stack = legacy.stack()
        registry = legacy.registry()
        target_name = server.host_alias
        target = TargetConfig(
            host_alias=server.host_alias,
            bootstrap_hostname=server.bootstrap_hostname,
            hostname=server.hostname,
            system_hostname=server.mdns_name,
            remote_user=server.remote_user,
            apps_root=server.apps_root,
            keep_releases=server.keep_releases,
            network=TargetNetwork(mode="local_mdns", mdns_name=server.mdns_name),
            stack=stack,
        )
        applications: dict[str, ApplicationConfig] = {}
        resources: dict[str, ResourceConfig] = {}
        deployments: dict[str, DeploymentConfig] = {}
        from gimme.plans import (
            environment_database_identifier,
            environment_deploy_path,
            environment_instance,
            environment_site_url,
        )

        for app_name, app in registry.apps.items():
            applications[app_name] = ApplicationConfig(
                repository=app.repository,
                framework=app.framework,
                frontend=app.frontend,
                artisan=app.artisan,
                default_health=app.health,
            )
            for environment, definition in app.environments.items():
                deployment_name = (
                    app_name if environment == "default" else f"{app_name}-{environment}"
                )
                if len(deployment_name) > 64:
                    digest = hashlib.sha256(
                        f"{app_name}\0{environment}".encode()
                    ).hexdigest()[:8]
                    deployment_name = f"{deployment_name[:55]}-{digest}"
                if deployment_name in deployments:
                    digest = hashlib.sha256(f"{app_name}\0{environment}".encode()).hexdigest()[:8]
                    deployment_name = f"{deployment_name[:54]}-{digest}"
                deploy_path = environment_deploy_path(server, app_name, environment)
                relative_path = str(Path(deploy_path).relative_to(server.apps_root))
                deployments[deployment_name] = DeploymentConfig(
                    application=app_name,
                    target=target_name,
                    stage="local" if environment == "default" else "preview",
                    source=DeploymentSource(kind="branch", ref=definition.branch),
                    app_env=definition.app_env,
                    app_debug=definition.app_debug,
                    health=definition.health,
                    workers=definition.workers,
                    scheduler=definition.scheduler,
                    runtimes=self._observed_runtime_pins(
                        observations[target_name], app.frontend.package_manager
                        if app.frontend is not None else None,
                        backend=app.framework != "static",
                    ),
                    resources=ResourceBindings(
                        database=f"{target_name}-postgres",
                        cache=f"{target_name}-valkey",
                    ) if app.framework != "static" else ResourceBindings(),
                    placement=Placement(
                        instance=environment_instance(app_name, environment),
                        relative_path=relative_path,
                        database_identifier=environment_database_identifier(app_name, environment),
                        cache_prefix=(
                            f"gimme:{app_name}:"
                            if environment == "default"
                            else f"gimme:{app_name}:{environment}:"
                        ),
                        site_host=environment_site_url(server, app_name, environment).removeprefix(
                            "https://"
                        ),
                    ),
                )
                if app.framework != "static":
                    resources[f"{target_name}-postgres"] = ResourceConfig(
                        target=target_name,
                        kind="postgres",
                        version=self._observed(observations[target_name], "postgres"),
                    )
                    resources[f"{target_name}-valkey"] = ResourceConfig(
                        target=target_name,
                        kind="valkey",
                        version=self._observed(observations[target_name], "valkey"),
                    )
        return ControlState(
            targets={target_name: target},
            applications=applications,
            resources=resources,
            deployments=deployments,
        )

    def state_migration(
        self, observations: dict[str, dict[str, str]]
    ) -> ControlState:
        if not self.exists():
            return self.legacy_migration(observations)
        document = self.raw_state()
        if document.get("schema_version") == 3:
            raise ValueError("schema-v3 state already exists")
        if document.get("schema_version") != 2:
            raise ValueError("only schema-v2 state can be migrated")
        targets = document.get("targets")
        applications = document.get("applications")
        deployments = document.get("deployments")
        if not all(isinstance(value, dict) for value in (targets, applications, deployments)):
            raise ValueError("schema-v2 state collections are invalid")
        migrated = json.loads(json.dumps(document))
        migrated["schema_version"] = 3
        migrated["resources"] = {}
        for target_name, target in migrated["targets"].items():
            if not isinstance(target, dict):
                raise ValueError(f"target {target_name} is invalid")
            old_toolchains = target.pop("toolchains", {})
            observed = observations.get(target_name)
            if observed is None:
                raise ValueError(f"runtime observations are missing for target {target_name}")
            target["runtimes"] = {
                "mise_version": observed.get("mise") or None,
            }
            target["_gimme_old_toolchains"] = old_toolchains
        for deployment_name, deployment in migrated["deployments"].items():
            if not isinstance(deployment, dict):
                raise ValueError(f"deployment {deployment_name} is invalid")
            target_name = deployment["target"]
            application_name = deployment["application"]
            target = migrated["targets"][target_name]
            old_toolchains = target.pop("_gimme_old_toolchains", {})
            target["_gimme_old_toolchains"] = old_toolchains
            application = migrated["applications"][application_name]
            manager = None
            if isinstance(application, dict) and isinstance(application.get("frontend"), dict):
                manager = application["frontend"].get("package_manager")
            deployment["runtimes"] = {
                name: pin.model_dump(mode="json")
                for name, pin in self._observed_runtime_pins(
                    observations[target_name], manager, old_toolchains,
                    backend=application.get("framework") != "static"
                ).items()
            }
            if isinstance(application, dict) and application.get("framework") != "static":
                database_resource = f"{target_name}-postgres"
                cache_resource = f"{target_name}-valkey"
                deployment["resources"] = {
                    "database": database_resource,
                    "cache": cache_resource,
                }
                migrated["resources"][database_resource] = {
                    "target": target_name,
                    "kind": "postgres",
                    "provider": "target_local",
                    "version": self._observed(observations[target_name], "postgres"),
                }
                migrated["resources"][cache_resource] = {
                    "target": target_name,
                    "kind": "valkey",
                    "provider": "target_local",
                    "version": self._observed(observations[target_name], "valkey"),
                }
            else:
                deployment["resources"] = {"database": None, "cache": None}
        for target in migrated["targets"].values():
            target.pop("_gimme_old_toolchains", None)
        return ControlState.model_validate(migrated)

    @staticmethod
    def _observed(observations: dict[str, str], name: str) -> str:
        version = observations.get(name, "")
        if VERSION.fullmatch(version) is None:
            raise ValueError(f"an exact observed {name} version is required for migration")
        return version

    @classmethod
    def _observed_runtime_pins(
        cls,
        observations: dict[str, str],
        package_manager: str | None,
        old_toolchains: object = None,
        *,
        backend: bool = True,
    ) -> dict[RuntimeName, RuntimePin]:
        pins: dict[RuntimeName, RuntimePin] = {}
        if backend:
            pins.update({
                "php": RuntimePin(
                    provider="system", version=cls._observed(observations, "php")
                ),
                "composer": RuntimePin(
                    provider="system", version=cls._observed(observations, "composer")
                ),
            })
        if package_manager is None:
            return pins
        previous = old_toolchains if isinstance(old_toolchains, dict) else {}
        if package_manager in {"npm", "pnpm", "yarn"}:
            node_version = str(previous.get("node") or observations.get("node") or "")
            pins["node"] = RuntimePin(provider="system", version=node_version)
        manager_version = str(
            previous.get(package_manager) or observations.get(package_manager) or ""
        )
        pins[package_manager] = RuntimePin(
            provider="bundled" if package_manager == "npm" else "system",
            version=manager_version,
        )
        return pins

    @staticmethod
    def digest(value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return "plan_" + hashlib.sha256(encoded).hexdigest()[:20]

    @staticmethod
    def _atomic_json_write(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def legacy_server(target: TargetConfig):
    from gimme.config import ServerConfig

    return ServerConfig(
        host_alias=target.host_alias,
        bootstrap_hostname=target.bootstrap_hostname,
        hostname=target.hostname,
        mdns_name=target.network.mdns_name or target.system_hostname,
        remote_user=target.remote_user,
        apps_root=target.apps_root,
        keep_releases=target.keep_releases,
    )


def legacy_app(application: ApplicationConfig, deployment: DeploymentConfig):
    from gimme.config import AppConfig, EnvironmentConfig

    health = application.default_health if deployment.health == "inherit" else deployment.health
    return AppConfig(
        repository=application.repository,
        framework=application.framework,
        frontend=application.frontend,
        artisan=application.artisan,
        health=application.default_health,
        health_probes=application.health_probes,
        environments={
            "default": EnvironmentConfig(
                branch=deployment.source.ref,
                app_env=deployment.app_env,
                app_debug=deployment.app_debug,
                health=health,
                health_probes=deployment.health_probes,
                workers=deployment.workers,
                scheduler=deployment.scheduler,
            )
        },
    )


def target_sites(state: ControlState, target_name: str) -> list[dict[str, str]]:
    sites: list[dict[str, str]] = []
    for deployment_name, deployment in sorted(state.deployments.items()):
        if deployment.target != target_name:
            continue
        application = state.applications[deployment.application]
        relative_root = {
            "laravel": "public",
            "symfony": "public",
            "static": (
                application.frontend.output_dir if application.frontend is not None else "dist"
            ),
        }.get(application.framework, "")
        document_root = (
            f"{state.targets[target_name].apps_root}/"
            f"{deployment.placement.relative_path}/current"
        )
        if relative_root:
            document_root += f"/{relative_root}"
        php_socket = ""
        if application.framework != "static":
            php = deployment.runtimes.get("php")
            if php is None:
                raise ValueError(f"deployment {deployment_name} has no PHP runtime")
            parts = php.version.split(".")
            if len(parts) < 2:
                raise ValueError("PHP runtime versions must include major and minor")
            php_socket = f"/run/php/php{parts[0]}.{parts[1]}-fpm.sock"
        sites.append(
            {
                "deployment": deployment_name,
                "instance": deployment.placement.instance,
                "framework": application.framework,
                "site_host": deployment.placement.site_host,
                "document_root": document_root,
                "php_fpm_socket": php_socket,
            }
        )
    return sites
