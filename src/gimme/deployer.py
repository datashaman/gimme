from __future__ import annotations

import json
import os

# Deployer is invoked through a fixed argv vector and never through a shell.
import subprocess  # nosec B404
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from gimme.config import AppConfig, ServerConfig, StackConfig
from gimme.plans import (
    environment_database_identifier,
    environment_deploy_path,
    environment_instance,
    environment_site_url,
)


MAX_OUTPUT = 24_000
PASSTHROUGH_ENVIRONMENT = (
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LOGNAME",
    "PATH",
    "SHELL",
    "SSH_AUTH_SOCK",
    "TMPDIR",
    "USER",
    "XDG_CACHE_HOME",
    "XDG_CONFIG_HOME",
)


def _agent_has_identities(socket_path: str) -> bool:
    path = Path(socket_path)
    if not path.is_absolute() or not path.is_socket():
        return False
    result = subprocess.run(  # nosec B603
        ["/usr/bin/ssh-add", "-l"],
        env={"PATH": os.defpath, "SSH_AUTH_SOCK": socket_path},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
        check=False,
    )
    return result.returncode == 0


def _preferred_ssh_auth_sock(current: str | None) -> str | None:
    if sys.platform != "darwin" or (current is not None and _agent_has_identities(current)):
        return current
    result = subprocess.run(  # nosec B603
        ["/bin/launchctl", "getenv", "SSH_AUTH_SOCK"],
        env={"PATH": os.defpath},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=5,
        check=False,
    )
    candidate = result.stdout.strip()
    if result.returncode == 0 and _agent_has_identities(candidate):
        return candidate
    return current


class DeployerError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    command: list[str]
    exit_code: int
    output: str

    def as_dict(self) -> dict[str, object]:
        return {
            "command": self.command,
            "exit_code": self.exit_code,
            "output": self.output,
        }


class DeployerRunner:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.binary = self.root / "vendor" / "bin" / "dep"
        self.recipe = self.root / "deploy.php"

    def run(
        self,
        task: str,
        server: ServerConfig,
        *,
        stack: StackConfig | None = None,
        app_name: str | None = None,
        app: AppConfig | None = None,
        environment_name: str = "default",
        revision: str | None = None,
        exclude_instance: str | None = None,
        arguments: Sequence[str] = (),
        artisan_command: str | None = None,
        artisan_arguments: Sequence[str] | None = None,
        artisan_allowed_commands: Sequence[str] | None = None,
        instance_name: str | None = None,
        deploy_path: str | None = None,
        site_host: str | None = None,
        database_identifier: str | None = None,
        cache_prefix: str | None = None,
        source_kind: str = "branch",
        sites: Sequence[dict[str, str]] | None = None,
        network_mode: str = "local_mdns",
        runtimes: dict[str, dict[str, str]] | None = None,
        resources: dict[str, dict[str, str]] | None = None,
        mise_version: str | None = None,
        php_extensions: Sequence[str] = (),
        variables: dict[str, str] | None = None,
        valkey_probe: dict[str, object] | None = None,
        secret_file: Path | None = None,
        secret_manifest: Sequence[dict[str, str]] | None = None,
        backup_local_path: Path | None = None,
        recovery_action: str | None = None,
        recovery_request_id: str | None = None,
        recovery_quiesce_wait: int | None = None,
        restore_source_bytes: int | None = None,
        postgres_restore_action: str | None = None,
        postgres_restore_request_id: str | None = None,
        postgres_restore_sha256: str | None = None,
        postgres_restore_bytes: int | None = None,
        valkey_restore_request_id: str | None = None,
        valkey_restore_sha256: str | None = None,
        valkey_restore_bytes: int | None = None,
        valkey_restore_records: int | None = None,
        resource_endpoint: tuple[str, int] | None = None,
        resource_database: str | None = None,
        resource_trust_bundle_sha256: str | None = None,
        timeout: int = 900,
        bootstrap: bool = False,
        interactive_sudo: bool = False,
    ) -> CommandResult:
        if not self.binary.is_file():
            raise DeployerError(
                "Deployer is not installed. Run `composer install` in the project root."
            )
        if not self.recipe.is_file():
            raise DeployerError(f"Deployer recipe is missing: {self.recipe}")

        command = [
            str(self.binary),
            "--file",
            str(self.recipe),
            task,
            server.host_alias,
            *([] if interactive_sudo else ["--no-interaction"]),
            *arguments,
        ]
        environment = {
            name: os.environ[name] for name in PASSTHROUGH_ENVIRONMENT if name in os.environ
        }
        environment.setdefault("PATH", os.defpath)
        preferred_agent = _preferred_ssh_auth_sock(environment.get("SSH_AUTH_SOCK"))
        if preferred_agent is not None:
            environment["SSH_AUTH_SOCK"] = preferred_agent
        environment.update(
            {
                "GIMME_HOSTNAME": server.hostname,
                "GIMME_HOST_ALIAS": server.host_alias,
                "GIMME_BOOTSTRAP_HOSTNAME": server.bootstrap_hostname,
                "GIMME_SSH_HOSTNAME": (server.bootstrap_hostname if bootstrap else server.hostname),
                "GIMME_MDNS_NAME": server.mdns_name,
                "GIMME_REMOTE_USER": server.remote_user,
                "GIMME_APPS_ROOT": server.apps_root,
                "GIMME_KEEP_RELEASES": str(server.keep_releases),
                "GIMME_NETWORK_MODE": network_mode,
                "GIMME_RUNTIMES_JSON": json.dumps(runtimes or {}),
                "GIMME_RESOURCES_JSON": json.dumps(resources or {}),
                "GIMME_MISE_VERSION": mise_version or "",
                "GIMME_PHP_EXTENSIONS_JSON": json.dumps(list(php_extensions)),
                "GIMME_VARIABLES_JSON": json.dumps(variables or {}),
            }
        )
        if valkey_probe is not None:
            environment["GIMME_VALKEY_PROBE_JSON"] = json.dumps(valkey_probe)
        if sites is not None:
            environment["GIMME_SITES_JSON"] = json.dumps(list(sites))
        if interactive_sudo:
            environment["GIMME_INTERACTIVE_SUDO"] = "1"
        if stack is not None:
            environment.update(
                {
                    "GIMME_PACKAGE_MANAGER": stack.package_manager,
                    "GIMME_PACKAGES_JSON": json.dumps(stack.packages),
                    "GIMME_SERVICES_JSON": json.dumps(stack.services),
                }
            )
            if any(
                value is not None
                for value in (
                    instance_name,
                    deploy_path,
                    site_host,
                    database_identifier,
                    cache_prefix,
                )
            ):
                environment["GIMME_CONTROL_V3"] = "1"
        if secret_file is not None:
            if secret_file.is_symlink():
                raise ValueError("secret_file must be a regular local file")
            resolved_secret_file = secret_file.resolve()
            if not resolved_secret_file.is_file():
                raise ValueError("secret_file must be a regular local file")
            environment["GIMME_SECRET_FILE"] = str(resolved_secret_file)
        if secret_manifest is not None:
            environment["GIMME_SECRET_MANIFEST_JSON"] = json.dumps(list(secret_manifest))
        if backup_local_path is not None:
            environment["GIMME_BACKUP_LOCAL_PATH"] = str(backup_local_path)
        if any(value is not None for value in (
            recovery_action, recovery_request_id, recovery_quiesce_wait
        )):
            if (
                recovery_action not in {"enter", "resume", "quiesce", "exit"}
                or recovery_request_id is None
                or recovery_quiesce_wait is None
            ):
                raise ValueError("recovery maintenance inputs must be complete")
            environment["GIMME_RECOVERY_ACTION"] = recovery_action
            environment["GIMME_RECOVERY_REQUEST_ID"] = recovery_request_id
            environment["GIMME_RECOVERY_QUIESCE_WAIT"] = str(recovery_quiesce_wait)
        if restore_source_bytes is not None:
            if not 0 <= restore_source_bytes <= 512 * 1024 * 1024:
                raise ValueError("restore source size is invalid")
            environment["GIMME_RESTORE_SOURCE_BYTES"] = str(restore_source_bytes)
        if any(value is not None for value in (
            postgres_restore_action, postgres_restore_request_id,
            postgres_restore_sha256, postgres_restore_bytes
        )):
            if (
                postgres_restore_action not in {"prepare", "swap", "cleanup"}
                or postgres_restore_request_id is None
                or postgres_restore_sha256 is None
                or postgres_restore_bytes is None
            ):
                raise ValueError("PostgreSQL restore inputs must be complete")
            environment["GIMME_POSTGRES_RESTORE_ACTION"] = postgres_restore_action
            environment["GIMME_POSTGRES_RESTORE_REQUEST_ID"] = postgres_restore_request_id
            environment["GIMME_POSTGRES_RESTORE_SHA256"] = postgres_restore_sha256
            environment["GIMME_POSTGRES_RESTORE_BYTES"] = str(postgres_restore_bytes)
        if any(value is not None for value in (
            valkey_restore_request_id, valkey_restore_sha256,
            valkey_restore_bytes, valkey_restore_records
        )):
            if (
                valkey_restore_request_id is None
                or valkey_restore_sha256 is None
                or valkey_restore_bytes is None
                or valkey_restore_records is None
            ):
                raise ValueError("Valkey restore inputs must be complete")
            environment["GIMME_VALKEY_RESTORE_REQUEST_ID"] = valkey_restore_request_id
            environment["GIMME_VALKEY_RESTORE_SHA256"] = valkey_restore_sha256
            environment["GIMME_VALKEY_RESTORE_BYTES"] = str(valkey_restore_bytes)
            environment["GIMME_VALKEY_RESTORE_RECORDS"] = str(valkey_restore_records)
        if resource_endpoint is not None:
            host, port = resource_endpoint
            environment["GIMME_RESOURCE_ENDPOINT"] = host
            environment["GIMME_RESOURCE_PORT"] = str(port)
        if resource_trust_bundle_sha256 is not None:
            environment["GIMME_RESOURCE_TRUST_BUNDLE_SHA256"] = resource_trust_bundle_sha256
        if resource_database is not None:
            environment["GIMME_DATABASE_IDENTIFIER"] = resource_database
        if exclude_instance is not None:
            environment["GIMME_EXCLUDE_INSTANCE"] = exclude_instance
        if app_name is not None and app is not None:
            definition = app.environment(environment_name)
            site_url = environment_site_url(server, app_name, environment_name)
            selected_site_host = site_host or site_url.removeprefix("https://")
            selected_deploy_path = deploy_path or environment_deploy_path(
                server, app_name, environment_name
            )
            selected_database = database_identifier or environment_database_identifier(
                app_name, environment_name
            )
            environment.update(
                {
                    "GIMME_APP": app_name,
                    "GIMME_ENVIRONMENT": environment_name,
                    "GIMME_INSTANCE": instance_name
                    or environment_instance(app_name, environment_name),
                    "GIMME_DEPLOY_PATH": selected_deploy_path,
                    "GIMME_SITE_HOST": selected_site_host,
                    "GIMME_DATABASE_IDENTIFIER": selected_database,
                    "GIMME_CACHE_PREFIX": cache_prefix
                    or (
                        f"gimme:{app_name}:"
                        if environment_name == "default"
                        else f"gimme:{app_name}:{environment_name}:"
                    ),
                    "GIMME_REPOSITORY": app.repository,
                    "GIMME_FRAMEWORK": app.framework,
                    "GIMME_BRANCH": definition.branch,
                    "GIMME_SOURCE_KIND": source_kind,
                    "GIMME_APP_ENV": definition.app_env,
                    "GIMME_APP_DEBUG": "true" if definition.app_debug else "false",
                    "GIMME_WORKERS_JSON": json.dumps(
                        definition.workers.model_dump() if definition.workers is not None else None
                    ),
                    "GIMME_SCHEDULER_JSON": json.dumps(
                        definition.scheduler.model_dump()
                        if definition.scheduler is not None
                        else None
                    ),
                    "GIMME_HEALTH_JSON": json.dumps(
                        [probe.model_dump() for probe in
                         app.effective_health_probes(environment_name)]
                    ),
                }
            )
            if revision is not None:
                environment["GIMME_REVISION"] = revision
            if app.frontend is not None:
                environment.update(
                    {
                        "GIMME_FRONTEND": "1",
                        "GIMME_FRONTEND_PACKAGE_MANAGER": app.frontend.package_manager,
                        "GIMME_FRONTEND_BUILD_SCRIPT": app.frontend.build_script,
                        "GIMME_FRONTEND_OUTPUT_DIR": app.frontend.output_dir,
                    }
                )

        artisan_values = (
            artisan_command,
            artisan_arguments,
            artisan_allowed_commands,
        )
        if any(value is not None for value in artisan_values):
            if any(value is None for value in artisan_values):
                raise ValueError("complete Artisan invocation context is required")
            environment.update(
                {
                    "GIMME_ARTISAN_COMMAND": artisan_command,
                    "GIMME_ARTISAN_ARGS_JSON": json.dumps(list(artisan_arguments or [])),
                    "GIMME_ARTISAN_ALLOWED_JSON": json.dumps(list(artisan_allowed_commands or [])),
                }
            )

        try:
            # The executable is fixed beneath the project root; shell=False is implicit.
            # Interactive bootstrap must inherit the complete terminal. Capturing only
            # stdout makes Symfony's hidden sudo question non-interactive even when stdin
            # still points at the user's terminal.
            completed = subprocess.run(  # nosec B603
                command,
                cwd=self.root,
                env=environment,
                stdin=None if interactive_sudo else subprocess.DEVNULL,
                stdout=None if interactive_sudo else subprocess.PIPE,
                stderr=None if interactive_sudo else subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeployerError(f"Deployer timed out after {timeout} seconds") from exc

        output = completed.stdout or ""
        if len(output) > MAX_OUTPUT:
            output = output[-MAX_OUTPUT:]
            output = "[earlier output truncated]\n" + output
        result = CommandResult(command, completed.returncode, output)
        if completed.returncode != 0:
            hint = ""
            if "password is required" in output or "a password is required" in output:
                hint = (
                    " Non-interactive sudo is unavailable; complete the documented "
                    "one-time privilege bootstrap on the host."
                )
            raise DeployerError(
                f"Deployer task '{task}' failed with exit code "
                f"{completed.returncode}.{hint}\n{output}"
            )
        return result
