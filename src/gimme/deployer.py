from __future__ import annotations

import json
import os
# Deployer is invoked through a fixed argv vector and never through a shell.
import subprocess  # nosec B404
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from gimme.config import AppConfig, ServerConfig, StackConfig


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
        arguments: Sequence[str] = (),
        artisan_command: str | None = None,
        artisan_arguments: Sequence[str] | None = None,
        artisan_allowed_commands: Sequence[str] | None = None,
        timeout: int = 900,
        bootstrap: bool = False,
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
            "--no-interaction",
            *arguments,
        ]
        environment = {
            name: os.environ[name]
            for name in PASSTHROUGH_ENVIRONMENT
            if name in os.environ
        }
        environment.setdefault("PATH", os.defpath)
        environment.update(
            {
                "GIMME_HOSTNAME": server.hostname,
                "GIMME_HOST_ALIAS": server.host_alias,
                "GIMME_BOOTSTRAP_HOSTNAME": server.bootstrap_hostname,
                "GIMME_SSH_HOSTNAME": (
                    server.bootstrap_hostname if bootstrap else server.hostname
                ),
                "GIMME_MDNS_NAME": server.mdns_name,
                "GIMME_REMOTE_USER": server.remote_user,
                "GIMME_APPS_ROOT": server.apps_root,
                "GIMME_KEEP_RELEASES": str(server.keep_releases),
            }
        )
        if stack is not None:
            environment.update(
                {
                    "GIMME_PACKAGE_MANAGER": stack.package_manager,
                    "GIMME_PACKAGES_JSON": json.dumps(stack.packages),
                    "GIMME_SERVICES_JSON": json.dumps(stack.services),
                }
            )
        if app_name is not None and app is not None:
            environment.update(
                {
                    "GIMME_APP": app_name,
                    "GIMME_REPOSITORY": app.repository,
                    "GIMME_FRAMEWORK": app.framework,
                    "GIMME_BRANCH": app.branch,
                    "GIMME_WORKERS_JSON": json.dumps(
                        app.workers.model_dump() if app.workers is not None else None
                    ),
                    "GIMME_SCHEDULER_JSON": json.dumps(
                        app.scheduler.model_dump() if app.scheduler is not None else None
                    ),
                }
            )
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
                    "GIMME_ARTISAN_ALLOWED_JSON": json.dumps(
                        list(artisan_allowed_commands or [])
                    ),
                }
            )

        try:
            # The executable is fixed beneath the project root; shell=False is implicit.
            completed = subprocess.run(  # nosec B603
                command,
                cwd=self.root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise DeployerError(f"Deployer timed out after {timeout} seconds") from exc

        output = completed.stdout
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
