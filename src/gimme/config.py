from __future__ import annotations

import json
import os
import re
import tempfile
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


APP_NAME = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
ENVIRONMENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
SSH_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$")
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,79}$")
SERVICE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9@_.:-]{0,79}$")
DNS_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$"
)
SCP_REPOSITORY = re.compile(
    r"^git@(?P<host>[a-zA-Z0-9.-]+):(?P<path>[a-zA-Z0-9._~/-]+)$"
)
REPOSITORY_PATH = re.compile(r"^[a-zA-Z0-9._~/-]+$")
RELATIVE_DIRECTORY = re.compile(r"^[a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+)*$")
ABSOLUTE_DIRECTORY = re.compile(r"^/(?:[a-zA-Z0-9._-]+/)*[a-zA-Z0-9._-]+$")
ARTISAN_COMMAND = re.compile(r"^[a-z][a-z0-9-]*(?::[a-z][a-z0-9-]*)*$")
QUEUE_CONNECTION = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
QUEUE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,63}$")
SAFE_APPS_ROOTS = (Path("/srv"), Path("/var/www"), Path("/opt"), Path("/home"))
DEFAULT_ARTISAN_COMMANDS = (
    "about",
    "cache:clear",
    "config:cache",
    "config:clear",
    "horizon:continue",
    "horizon:pause",
    "horizon:status",
    "horizon:terminate",
    "migrate",
    "migrate:status",
    "optimize",
    "optimize:clear",
    "queue:restart",
    "route:cache",
    "route:clear",
    "schedule:list",
    "storage:link",
    "view:cache",
    "view:clear",
)


def _valid_endpoint(value: str) -> str:
    if value.startswith("-") or any(char.isspace() for char in value):
        raise ValueError("host endpoint is not a safe IP address or DNS name")
    try:
        ip_address(value)
    except ValueError:
        if DNS_NAME.fullmatch(value) is None:
            raise ValueError("host endpoint is not a safe IP address or DNS name")
    return value


def _safe_repository_path(value: str) -> bool:
    parts = value.strip("/").split("/")
    return (
        REPOSITORY_PATH.fullmatch(value) is not None
        and bool(parts)
        and all(part not in {"", ".", ".."} for part in parts)
    )


def _valid_git_branch(value: str) -> bool:
    return not (
        value.startswith(("-", ".", "/"))
        or value.endswith((".", "/", ".lock"))
        or value == "@"
        or ".." in value
        or "@{" in value
        or "//" in value
        or any(
            char.isspace()
            or ord(char) < 32
            or ord(char) == 127
            or char in "~^:?*[\\"
            for char in value
        )
    )


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_alias: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    bootstrap_hostname: str = Field(min_length=1, max_length=253)
    hostname: str = Field(min_length=1, max_length=253)
    mdns_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    remote_user: str
    apps_root: str
    keep_releases: int = Field(default=5, ge=2, le=20)

    @field_validator("bootstrap_hostname", "hostname")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        return _valid_endpoint(value)

    @field_validator("remote_user")
    @classmethod
    def valid_remote_user(cls, value: str) -> str:
        if not SSH_NAME.fullmatch(value):
            raise ValueError("remote_user is not a safe Unix user name")
        return value

    @field_validator("apps_root")
    @classmethod
    def absolute_apps_root(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute() or ".." in path.parts:
            raise ValueError("apps_root must be an absolute path without '..'")
        normalized = Path(value.rstrip("/"))
        if ABSOLUTE_DIRECTORY.fullmatch(str(normalized)) is None:
            raise ValueError("apps_root contains unsafe path characters")
        if not any(
            normalized != root and normalized.is_relative_to(root)
            for root in SAFE_APPS_ROOTS
        ):
            raise ValueError("apps_root must be beneath /srv, /var/www, /opt, or /home")
        return str(normalized)


class FrontendBuildConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_manager: Literal["npm"] = "npm"
    build_script: str = Field(default="build", pattern=r"^[a-zA-Z0-9:_-]{1,64}$")
    output_dir: str = Field(default="dist", min_length=1, max_length=160)

    @field_validator("output_dir")
    @classmethod
    def safe_relative_output_dir(cls, value: str) -> str:
        path = Path(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "." in path.parts
            or value.startswith("-")
            or RELATIVE_DIRECTORY.fullmatch(value) is None
        ):
            raise ValueError("output_dir must be a safe relative path")
        return value.rstrip("/")


class ArtisanConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_commands: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ARTISAN_COMMANDS), max_length=32
    )

    @field_validator("allowed_commands")
    @classmethod
    def valid_commands(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("Artisan commands must not contain duplicates")
        invalid = [command for command in value if ARTISAN_COMMAND.fullmatch(command) is None]
        if invalid:
            raise ValueError(f"invalid Artisan commands: {', '.join(invalid)}")
        return value


class ArtisanInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    command: str = Field(min_length=1, max_length=120, pattern=ARTISAN_COMMAND.pattern)
    arguments: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("arguments")
    @classmethod
    def safe_arguments(cls, value: list[str]) -> list[str]:
        for argument in value:
            if (
                not argument
                or len(argument) > 256
                or any(ord(char) < 32 or ord(char) == 127 for char in argument)
                or argument == "--env"
                or argument.startswith("--env=")
            ):
                raise ValueError(
                    "Artisan arguments must be non-empty, at most 256 characters, "
                    "control-character-free, and must not select another environment"
                )
        return value


class QueueWorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver: Literal["queue"] = Field(
        default="queue", description="Use standard Laravel queue:work processes."
    )
    enabled: bool = Field(default=True, description="Whether these worker units should run.")
    processes: int = Field(
        default=1, ge=1, le=16, description="Number of identical systemd worker instances."
    )
    connection: str = Field(
        default="database",
        pattern=QUEUE_CONNECTION.pattern,
        description="Laravel queue connection passed literally to queue:work.",
    )
    queues: list[str] = Field(
        default_factory=lambda: ["default"],
        min_length=1,
        max_length=16,
        description="Ordered queue names passed to queue:work.",
    )
    sleep_seconds: int = Field(
        default=3, ge=0, le=60, description="Seconds to sleep when no job is available."
    )
    tries: int = Field(
        default=3, ge=0, le=100, description="Maximum attempts; zero means unlimited."
    )
    timeout_seconds: int = Field(
        default=60, ge=1, le=86400, description="Worker job timeout in seconds."
    )
    memory_mb: int = Field(
        default=256, ge=32, le=8192, description="Worker memory ceiling in megabytes."
    )
    max_time_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Maximum worker lifetime before systemd restarts it.",
    )
    max_jobs: int = Field(
        default=0,
        ge=0,
        le=100000,
        description="Maximum jobs per worker lifetime; zero means unlimited.",
    )
    backoff_seconds: int = Field(
        default=0, ge=0, le=86400, description="Retry delay after an unhandled job error."
    )

    @field_validator("queues")
    @classmethod
    def valid_queues(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("queue names must not contain duplicates")
        invalid = [name for name in value if QUEUE_NAME.fullmatch(name) is None]
        if invalid:
            raise ValueError(f"invalid queue names: {', '.join(invalid)}")
        return value


class HorizonWorkerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    driver: Literal["horizon"] = Field(
        default="horizon", description="Use one Laravel Horizon master process."
    )
    enabled: bool = Field(default=True, description="Whether the Horizon unit should run.")
    stop_wait_seconds: int = Field(
        default=3600,
        ge=60,
        le=86400,
        description="Maximum graceful systemd stop time for active Horizon jobs.",
    )


WorkerConfig = Annotated[
    QueueWorkerConfig | HorizonWorkerConfig,
    Field(discriminator="driver"),
]


class SchedulerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=True, description="Run Laravel schedule:run from a systemd timer every minute."
    )


class HealthCheckConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        default="/up",
        min_length=1,
        max_length=200,
        description="Absolute Laravel URL path checked before and after activation.",
    )
    expected_status: int = Field(
        default=200,
        ge=200,
        le=399,
        description="Exact successful HTTP status required from both health gates.",
    )
    attempts: int = Field(
        default=10,
        ge=1,
        le=30,
        description="Maximum attempts for each health gate.",
    )
    delay_seconds: int = Field(
        default=2,
        ge=0,
        le=30,
        description="Delay between health attempts in seconds.",
    )
    timeout_seconds: int = Field(
        default=5,
        ge=1,
        le=30,
        description="Timeout for one health attempt in seconds.",
    )

    @field_validator("path")
    @classmethod
    def safe_absolute_url_path(cls, value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            raise ValueError("health path must be an absolute single-slash URL path")
        if any(character in value for character in ("?", "#", "%", "\\")):
            raise ValueError("health path must not contain query, fragment, or escapes")
        segments = value.strip("/").split("/") if value != "/" else []
        if any(
            segment in {"", ".", ".."}
            or re.fullmatch(r"[a-zA-Z0-9._~-]+", segment) is None
            for segment in segments
        ):
            raise ValueError("health path contains unsafe path segments")
        return value


class EnvironmentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branch: str = Field(default="main", min_length=1, max_length=120)
    health: Literal["inherit"] | HealthCheckConfig | None = "inherit"
    workers: WorkerConfig | None = None
    scheduler: SchedulerConfig | None = None

    @field_validator("branch")
    @classmethod
    def valid_branch(cls, value: str) -> str:
        if not _valid_git_branch(value):
            raise ValueError("branch is not a safe Git branch name")
        return value


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=500)
    framework: Literal["common", "laravel", "symfony", "wordpress", "static"] = "common"
    frontend: FrontendBuildConfig | None = None
    artisan: ArtisanConfig | None = None
    health: HealthCheckConfig | None = None
    environments: dict[str, EnvironmentConfig] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_default_environment(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        migrated = dict(value)
        legacy_present = any(
            key in migrated for key in ("branch", "workers", "scheduler")
        )
        environments = migrated.get("environments")
        if environments is None:
            environments = {}
        elif not isinstance(environments, dict):
            return migrated
        else:
            environments = dict(environments)
        if "default" not in environments:
            environments["default"] = {
                "branch": migrated.pop("branch", "main"),
                "workers": migrated.pop("workers", None),
                "scheduler": migrated.pop("scheduler", None),
            }
        elif legacy_present:
            raise ValueError(
                "legacy branch/process fields cannot be combined with environments.default"
            )
        migrated["environments"] = environments
        return migrated

    @model_validator(mode="after")
    def validate_framework_configuration(self) -> "AppConfig":
        if self.framework == "static" and self.frontend is None:
            raise ValueError("static applications require frontend build configuration")
        if self.framework == "laravel" and self.artisan is None:
            self.artisan = ArtisanConfig()
        elif self.framework != "laravel" and self.artisan is not None:
            raise ValueError("Artisan configuration is supported only for Laravel applications")
        if "default" not in self.environments:
            raise ValueError("applications require an environments.default definition")
        invalid_names = [
            name for name in self.environments if ENVIRONMENT_NAME.fullmatch(name) is None
        ]
        if invalid_names:
            raise ValueError(f"invalid environment names: {', '.join(invalid_names)}")
        has_environment_laravel_settings = any(
            environment.workers is not None
            or environment.scheduler is not None
            or environment.health != "inherit"
            for environment in self.environments.values()
        )
        if self.framework != "laravel" and (
            has_environment_laravel_settings or self.health is not None
        ):
            raise ValueError(
                "worker, scheduler, and health configuration require a Laravel application"
            )
        return self

    @field_validator("repository")
    @classmethod
    def valid_repository(cls, value: str) -> str:
        if any(char in value for char in ("\n", "\r", "\x00")):
            raise ValueError("repository contains invalid characters")
        if value.startswith("git@"):
            match = SCP_REPOSITORY.fullmatch(value)
            if match is None or DNS_NAME.fullmatch(match["host"]) is None:
                raise ValueError("repository must be a safe HTTPS or SSH Git URL")
            if not _safe_repository_path(match["path"]):
                raise ValueError("repository contains an unsafe path")
            return value
        try:
            parsed = urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise ValueError("repository must be a safe HTTPS or SSH Git URL") from exc
        if parsed.scheme not in {"https", "ssh"} or parsed.hostname is None:
            raise ValueError("repository must be a safe HTTPS or SSH Git URL")
        if parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError("repository must not contain credentials, query, or fragment")
        if parsed.scheme == "https" and parsed.username is not None:
            raise ValueError("HTTPS repository must not contain credentials")
        if parsed.username is not None and (
            parsed.username.startswith("-")
            or SSH_NAME.fullmatch(parsed.username) is None
        ):
            raise ValueError("SSH repository contains an unsafe user name")
        if port is not None and parsed.scheme != "ssh":
            raise ValueError("HTTPS repository must use its default port")
        _valid_endpoint(parsed.hostname)
        if not _safe_repository_path(parsed.path):
            raise ValueError("repository contains an unsafe path")
        return value

    def environment(self, name: str = "default") -> EnvironmentConfig:
        if ENVIRONMENT_NAME.fullmatch(name) is None:
            raise ValueError("environment name is unsafe")
        try:
            return self.environments[name]
        except KeyError as exc:
            raise KeyError(f"environment '{name}' is not registered") from exc

    def effective_health(self, name: str = "default") -> HealthCheckConfig | None:
        override = self.environment(name).health
        return self.health if override == "inherit" else override

    @property
    def branch(self) -> str:
        return self.environment().branch

    @property
    def workers(self) -> WorkerConfig | None:
        return self.environment().workers

    @property
    def scheduler(self) -> SchedulerConfig | None:
        return self.environment().scheduler


class AppRegistry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    apps: dict[str, AppConfig] = Field(default_factory=dict)

    @field_validator("apps")
    @classmethod
    def valid_names(cls, value: dict[str, AppConfig]) -> dict[str, AppConfig]:
        invalid = [name for name in value if not APP_NAME.fullmatch(name)]
        if invalid:
            raise ValueError(f"invalid application names: {', '.join(invalid)}")
        return value


class StackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_manager: Literal["apt"]
    packages: list[str] = Field(min_length=1)
    services: list[str] = Field(default_factory=list)

    @field_validator("packages")
    @classmethod
    def valid_packages(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("packages must not contain duplicates")
        invalid = [name for name in value if not PACKAGE_NAME.fullmatch(name)]
        if invalid:
            raise ValueError(f"invalid package names: {', '.join(invalid)}")
        return value

    @field_validator("services")
    @classmethod
    def valid_services(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("services must not contain duplicates")
        invalid = [name for name in value if not SERVICE_NAME.fullmatch(name)]
        if invalid:
            raise ValueError(f"invalid service names: {', '.join(invalid)}")
        return value


class ConfigStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.server_path = self.root / "config" / "server.json"
        self.stack_path = self.root / "config" / "stack.json"
        self.apps_path = self.root / "config" / "apps.json"

    def server(self) -> ServerConfig:
        return ServerConfig.model_validate_json(self.server_path.read_text())

    def registry(self) -> AppRegistry:
        return AppRegistry.model_validate_json(self.apps_path.read_text())

    def stack(self) -> StackConfig:
        return StackConfig.model_validate_json(self.stack_path.read_text())

    def app(self, name: str) -> AppConfig:
        self.validate_app_name(name)
        registry = self.registry()
        try:
            return registry.apps[name]
        except KeyError as exc:
            raise KeyError(
                f"application '{name}' is not registered; use register_app first"
            ) from exc

    def environment(self, name: str, environment: str = "default") -> EnvironmentConfig:
        self.validate_environment_name(environment)
        return self.app(name).environment(environment)

    def register_app(self, name: str, app: AppConfig) -> bool:
        self.validate_app_name(name)
        registry = self.registry()
        changed = registry.apps.get(name) != app
        registry.apps[name] = app
        if changed:
            self._atomic_json_write(self.apps_path, registry.model_dump(mode="json"))
        return changed

    def register_environment(
        self, name: str, environment: str, definition: EnvironmentConfig
    ) -> bool:
        self.validate_environment_name(environment, allow_default=False)
        app = self.app(name)
        environments = dict(app.environments)
        changed = environments.get(environment) != definition
        environments[environment] = definition
        if changed:
            updated = AppConfig.model_validate(
                {**app.model_dump(mode="python"), "environments": environments}
            )
            self.register_app(name, updated)
        return changed

    def configure_app_processes(
        self,
        name: str,
        workers: WorkerConfig | None,
        scheduler: SchedulerConfig | None,
    ) -> bool:
        return self.configure_environment_processes(
            name, "default", workers, scheduler
        )

    def configure_environment_processes(
        self,
        name: str,
        environment: str,
        workers: WorkerConfig | None,
        scheduler: SchedulerConfig | None,
    ) -> bool:
        app = self.app(name)
        self.validate_environment_name(environment)
        environments = dict(app.environments)
        definition = EnvironmentConfig.model_validate(
            {
                **app.environment(environment).model_dump(mode="python"),
                "workers": workers,
                "scheduler": scheduler,
            }
        )
        environments[environment] = definition
        updated = AppConfig.model_validate(
            {
                **app.model_dump(mode="python"),
                "environments": environments,
            }
        )
        return self.register_app(name, updated)

    def configure_app_health(
        self,
        name: str,
        health: HealthCheckConfig | None,
    ) -> bool:
        app = self.app(name)
        updated = AppConfig.model_validate(
            {
                **app.model_dump(mode="python"),
                "health": health,
            }
        )
        return self.register_app(name, updated)

    def configure_environment_health(
        self,
        name: str,
        environment: str,
        health: Literal["inherit"] | HealthCheckConfig | None,
    ) -> bool:
        app = self.app(name)
        self.validate_environment_name(environment)
        environments = dict(app.environments)
        environments[environment] = EnvironmentConfig.model_validate(
            {
                **app.environment(environment).model_dump(mode="python"),
                "health": health,
            }
        )
        updated = AppConfig.model_validate(
            {**app.model_dump(mode="python"), "environments": environments}
        )
        return self.register_app(name, updated)

    def remove_environment(self, name: str, environment: str) -> bool:
        self.validate_environment_name(environment, allow_default=False)
        app = self.app(name)
        if environment not in app.environments:
            return False
        environments = dict(app.environments)
        del environments[environment]
        updated = AppConfig.model_validate(
            {**app.model_dump(mode="python"), "environments": environments}
        )
        return self.register_app(name, updated)

    @staticmethod
    def validate_app_name(name: str) -> None:
        if not APP_NAME.fullmatch(name):
            raise ValueError(
                "application name must start with a letter and contain only "
                "lowercase letters, digits, and hyphens (maximum 48 characters)"
            )

    @staticmethod
    def validate_environment_name(name: str, *, allow_default: bool = True) -> None:
        if ENVIRONMENT_NAME.fullmatch(name) is None or (not allow_default and name == "default"):
            raise ValueError(
                "environment name must start with a letter and contain only lowercase "
                "letters, digits, and hyphens (maximum 32 characters); default is reserved"
            )

    @staticmethod
    def _atomic_json_write(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise
