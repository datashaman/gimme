from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap


ROOT = Path(__file__).resolve().parents[2]
STATE_DIRECTORY = Path(os.environ.get("GIMME_STATE_DIR", ROOT / "config"))
STATE_PATH = STATE_DIRECTORY / "state.json"
APPS_ROOT = "/srv/gimme-integration/apps"
HOSTNAME = "gimme-ci.local"
TARGET = "integration"
DEPLOYMENTS = ("smoke-default", "smoke-preview")


def require_disposable_host() -> None:
    if os.environ.get("GIMME_INTEGRATION_DISPOSABLE") != "1" or not os.environ.get("CI"):
        raise RuntimeError("refusing to run without CI=true and GIMME_INTEGRATION_DISPOSABLE=1")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def command_version(*command: str) -> str:
    return subprocess.run(
        command,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.strip()


def deployment(
    name: str,
    *,
    branch: str,
    app_env: str,
    php_version: str,
    composer_version: str,
) -> dict[str, object]:
    return {
        "application": "smoke",
        "target": TARGET,
        "stage": "local",
        "source": {"kind": "branch", "ref": branch},
        "app_env": app_env,
        "app_debug": False,
        "domain": None,
        "health": None,
        "workers": None,
        "scheduler": None,
        "variables": {},
        "secrets": {},
        "runtimes": {
            "php": {"provider": "system", "version": php_version},
            "composer": {"provider": "system", "version": composer_version},
        },
        "resources": {
            "database": "integration-postgres",
            "cache": "integration-valkey",
        },
        "placement": {
            "instance": name,
            "relative_path": f"deployments/{name}",
            "database_identifier": f"gimme_{name.replace('-', '_')}",
            "cache_prefix": f"gimme:{name}:",
            "site_host": f"{name}.gimme-ci.local",
        },
    }


def setup() -> None:
    require_disposable_host()
    remote_user = os.environ["USER"]
    php_version = command_version("php", "-r", "echo PHP_VERSION;")
    composer_version = observed_version(
        command_version("composer", "--version", "--short"),
        r"(\d+(?:\.\d+){1,3})",
        "Composer",
    )
    packages = [
        "acl",
        "git",
        "unzip",
        "avahi-daemon",
        "avahi-utils",
        "caddy",
        "postgresql",
        "python3",
        "valkey-server",
        "valkey-tools",
        "php-cli",
        "php-fpm",
        "php-pgsql",
        "php-curl",
        "php-mbstring",
        "php-xml",
        "php-zip",
        "php-intl",
        "php-bcmath",
        "php-redis",
        "composer",
    ]
    state = {
        "schema_version": 3,
        "targets": {
            TARGET: {
                "host_alias": TARGET,
                "bootstrap_hostname": "127.0.0.1",
                "hostname": HOSTNAME,
                "system_hostname": "gimme-ci",
                "remote_user": remote_user,
                "apps_root": APPS_ROOT,
                "keep_releases": 2,
                "network": {
                    "mode": "local_mdns",
                    "mdns_name": "gimme-ci",
                    "expected_addresses": [],
                },
                "stack": {
                    "package_manager": "apt",
                    "packages": packages,
                    "services": ["avahi-daemon", "caddy", "postgresql", "valkey-server"],
                },
                "runtimes": {"mise_version": None},
            }
        },
        "applications": {
            "smoke": {
                "repository": "https://example.test/gimme/integration-fixture.git",
                "framework": "laravel",
                "frontend": None,
                "artisan": None,
                "default_health": None,
                "php_extensions": [],
            }
        },
        "resources": {
            "integration-postgres": {
                "target": TARGET,
                "kind": "postgres",
                "provider": "target_local",
                "version": "1.0",
            },
            "integration-valkey": {
                "target": TARGET,
                "kind": "valkey",
                "provider": "target_local",
                "version": "1.0",
            },
        },
        "deployments": {
            "smoke-default": deployment(
                "smoke-default",
                branch="main",
                app_env="local",
                php_version=php_version,
                composer_version=composer_version,
            ),
            "smoke-preview": deployment(
                "smoke-preview",
                branch="feature/preview",
                app_env="preview",
                php_version=php_version,
                composer_version=composer_version,
            ),
        },
    }
    write_json(STATE_PATH, state)


def ssh(*arguments: str) -> str:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", HOSTNAME, *arguments],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def ssh_python(program: str) -> None:
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", HOSTNAME, "python3", "-"],
        input=program,
        check=True,
        text=True,
        capture_output=True,
    )


def observed_version(output: str, pattern: str, name: str) -> str:
    match = re.search(pattern, output)
    if match is None:
        raise AssertionError(f"could not parse {name} version from {output!r}")
    return match.group(1)


def pin_resources() -> None:
    require_disposable_host()
    state = json.loads(STATE_PATH.read_text())
    state["resources"]["integration-postgres"]["version"] = observed_version(
        ssh("psql", "--version"),
        r"(\d+(?:\.\d+){0,3})",
        "PostgreSQL",
    )
    state["resources"]["integration-valkey"]["version"] = observed_version(
        ssh("valkey-server", "--version"),
        r"v=(\d+(?:\.\d+){0,3})",
        "Valkey",
    )
    write_json(STATE_PATH, state)


def install_legacy_process_state() -> None:
    legacy_process_state = {
        "version": 1,
        "application": "smoke-default",
        "framework": "laravel",
        "remote_user": ssh("id", "-un"),
        "apps_root": APPS_ROOT,
        "workers": None,
        "scheduler": None,
    }
    ssh_python(
        textwrap.dedent(
            f"""
            import json
            import os
            from pathlib import Path

            directory = Path({str(APPS_ROOT + "/.gimme/processes")!r})
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / "smoke-default.json"
            path.write_text(json.dumps({legacy_process_state!r}) + "\\n")
            os.chmod(path, 0o600)
            """
        )
    )


def verify() -> None:
    require_disposable_host()
    from gimme import server as gimme

    state = json.loads(STATE_PATH.read_text())
    identifiers = {
        state["deployments"][name]["placement"]["database_identifier"]
        for name in DEPLOYMENTS
    }
    if len(identifiers) != len(DEPLOYMENTS):
        raise AssertionError("deployment database identities collided")

    install_legacy_process_state()
    for name in DEPLOYMENTS:
        plan = gimme.plan_deployment_resources(name)
        if not plan["ready"]:
            raise AssertionError(f"deployment resources are not ready: {plan}")
        gimme.apply_deployment_resources(name, str(plan["plan_id"]))

    databases = ssh("psql", "-d", "postgres", "-lqt")
    for database in identifiers:
        if database not in databases:
            raise AssertionError(f"missing PostgreSQL database {database}")

    upgraded_state = json.loads(
        ssh("cat", f"{APPS_ROOT}/.gimme/processes/smoke-default.json")
    )
    expected_path = f"{APPS_ROOT}/deployments/smoke-default"
    if upgraded_state.get("deploy_path") != expected_path:
        raise AssertionError("legacy process desired state was not upgraded")

    for name, expected_env in (
        ("smoke-default", "local"),
        ("smoke-preview", "preview"),
    ):
        env_path = f"{APPS_ROOT}/deployments/{name}/shared/.env"
        values = {
            line.split("=", 1)[0]: line.split("=", 1)[1]
            for line in ssh("cat", env_path).splitlines()
            if "=" in line
        }
        if values.get("APP_ENV") != expected_env or values.get("APP_DEBUG") != "false":
            raise AssertionError(f"incorrect Laravel runtime policy for {name}")
        if values.get("HORIZON_PREFIX") != f"gimme:{name}:horizon:":
            raise AssertionError(f"incorrect Horizon prefix for {name}")

    inspection = str(gimme.inspect_target(TARGET)["output"])
    for name in DEPLOYMENTS:
        marker = f"site.{name}.mdns_publisher=active"
        if marker not in inspection:
            raise AssertionError(f"missing active Avahi publisher: {marker}")

    for service in ("postgresql", "valkey-server", "caddy"):
        status = str(gimme.target_service_status(TARGET, service)["output"])
        if "active (running)" not in status:
            raise AssertionError(f"{service} is not active")


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif sys.argv[1:] == ["pin-resources"]:
        pin_resources()
    elif sys.argv[1:] == ["verify"]:
        verify()
    else:
        raise SystemExit("usage: disposable_vm_smoke.py setup|pin-resources|verify")
