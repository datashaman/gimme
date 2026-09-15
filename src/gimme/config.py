from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


APP_NAME = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
SSH_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$")
PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]{0,79}$")
SERVICE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9@_.:-]{0,79}$")


class ServerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_alias: str = Field(pattern=r"^[a-z][a-z0-9-]{0,31}$")
    bootstrap_hostname: str = Field(min_length=1, max_length=253)
    hostname: str = Field(min_length=1, max_length=253)
    mdns_name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    remote_user: str
    apps_root: str
    keep_releases: int = Field(default=5, ge=2, le=20)

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
        return value.rstrip("/")


class FrontendBuildConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    package_manager: Literal["npm"] = "npm"
    build_script: str = Field(default="build", pattern=r"^[a-zA-Z0-9:_-]{1,64}$")
    output_dir: str = Field(default="dist", min_length=1, max_length=160)

    @field_validator("output_dir")
    @classmethod
    def safe_relative_output_dir(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or value.startswith("-"):
            raise ValueError("output_dir must be a safe relative path")
        return value.rstrip("/")


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=500)
    framework: Literal["common", "laravel", "symfony", "wordpress", "static"] = "common"
    branch: str = Field(default="main", min_length=1, max_length=120)
    frontend: FrontendBuildConfig | None = None

    @model_validator(mode="after")
    def static_requires_frontend_build(self) -> "AppConfig":
        if self.framework == "static" and self.frontend is None:
            raise ValueError("static applications require frontend build configuration")
        return self

    @field_validator("repository")
    @classmethod
    def valid_repository(cls, value: str) -> str:
        if not value.startswith(("https://", "ssh://", "git@")):
            raise ValueError("repository must be an HTTPS or SSH Git URL")
        if any(char in value for char in ("\n", "\r", "\x00")):
            raise ValueError("repository contains invalid characters")
        return value

    @field_validator("branch")
    @classmethod
    def valid_branch(cls, value: str) -> str:
        if value.startswith("-") or any(char.isspace() for char in value):
            raise ValueError("branch must not start with '-' or contain whitespace")
        return value


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

    def register_app(self, name: str, app: AppConfig) -> bool:
        self.validate_app_name(name)
        registry = self.registry()
        changed = registry.apps.get(name) != app
        registry.apps[name] = app
        if changed:
            self._atomic_json_write(self.apps_path, registry.model_dump(mode="json"))
        return changed

    @staticmethod
    def validate_app_name(name: str) -> None:
        if not APP_NAME.fullmatch(name):
            raise ValueError(
                "application name must start with a letter and contain only "
                "lowercase letters, digits, and hyphens (maximum 48 characters)"
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
