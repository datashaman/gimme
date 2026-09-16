from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

from gimme.plans import environment_database_identifier, environment_instance


ROOT = Path(__file__).resolve().parents[2]
APPS_ROOT = "/srv/gimme-integration/apps"
HOSTNAME = "gimme-ci.local"


def require_disposable_host() -> None:
    if os.environ.get("GIMME_INTEGRATION_DISPOSABLE") != "1" or not os.environ.get("CI"):
        raise RuntimeError("refusing to run without CI=true and GIMME_INTEGRATION_DISPOSABLE=1")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def setup() -> None:
    require_disposable_host()
    remote_user = os.environ["USER"]
    write_json(
        ROOT / "config/server.json",
        {
            "host_alias": "integration",
            "bootstrap_hostname": "127.0.0.1",
            "hostname": HOSTNAME,
            "mdns_name": "gimme-ci",
            "remote_user": remote_user,
            "apps_root": APPS_ROOT,
            "keep_releases": 2,
        },
    )
    stack = json.loads((ROOT / "config/stack.example.json").read_text())
    write_json(ROOT / "config/stack.json", stack)
    repository = "https://example.test/gimme/integration-fixture.git"
    write_json(
        ROOT / "config/apps.json",
        {
            "apps": {
                "smoke": {
                    "repository": repository,
                    "framework": "laravel",
                    "environments": {
                        "default": {
                            "branch": "main",
                            "app_env": "production",
                            "app_debug": False,
                        },
                        "app": {
                            "branch": "feature/database-identity",
                            "app_env": "local",
                            "app_debug": False,
                        },
                        "branch--preview": {
                            "branch": "feature/instance-b",
                            "app_env": "preview",
                            "app_debug": False,
                        },
                    },
                },
                "smoke--branch": {
                    "repository": repository,
                    "framework": "laravel",
                    "environments": {
                        "default": {
                            "branch": "main",
                            "app_env": "production",
                            "app_debug": False,
                        },
                        "preview": {
                            "branch": "feature/instance-a",
                            "app_env": "preview",
                            "app_debug": False,
                        },
                    },
                },
                "smoke-app": {
                    "repository": repository,
                    "framework": "laravel",
                    "environments": {
                        "default": {
                            "branch": "main",
                            "app_env": "production",
                            "app_debug": False,
                        }
                    },
                },
            }
        },
    )


def ssh(*arguments: str) -> str:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", HOSTNAME, *arguments],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def verify() -> None:
    require_disposable_host()
    from gimme import server as gimme

    default_database = environment_database_identifier("smoke-app")
    branch_database = environment_database_identifier("smoke", "app")
    if default_database == branch_database:
        raise AssertionError("database identities collided")

    first_instance = environment_instance("smoke--branch", "preview")
    second_instance = environment_instance("smoke", "branch--preview")
    if first_instance == second_instance:
        raise AssertionError("process and site identities collided")

    for application, environment in (
        ("smoke-app", "default"),
        ("smoke", "app"),
    ):
        plan = gimme.plan_app_resources(application, environment)
        gimme.provision_app_resources(application, plan["plan_id"], environment)

    databases = ssh("psql", "-d", "postgres", "-lqt")
    for database in (default_database, branch_database):
        if database not in databases:
            raise AssertionError(f"missing PostgreSQL database {database}")

    default_prefix = ssh("grep", "^HORIZON_PREFIX=", f"{APPS_ROOT}/smoke-app/shared/.env")
    branch_prefix = ssh(
        "grep",
        "^HORIZON_PREFIX=",
        f"{APPS_ROOT}/smoke/environments/app/shared/.env",
    )
    if default_prefix != "HORIZON_PREFIX=gimme:smoke-app:horizon:":
        raise AssertionError("default Horizon prefix is not isolated")
    if branch_prefix != "HORIZON_PREFIX=gimme:smoke:app:horizon:":
        raise AssertionError("branch Horizon prefix is not isolated")

    default_runtime = [
        line
        for line in ssh("cat", f"{APPS_ROOT}/smoke-app/shared/.env").splitlines()
        if line.startswith(("APP_ENV=", "APP_DEBUG="))
    ]
    branch_runtime = [
        line
        for line in ssh(
            "cat", f"{APPS_ROOT}/smoke/environments/app/shared/.env"
        ).splitlines()
        if line.startswith(("APP_ENV=", "APP_DEBUG="))
    ]
    if default_runtime != ["APP_ENV=production", "APP_DEBUG=false"]:
        raise AssertionError("default Laravel runtime policy was not reconciled")
    if branch_runtime != ["APP_ENV=local", "APP_DEBUG=false"]:
        raise AssertionError("branch Laravel runtime policy was not reconciled")

    inspection = gimme.inspect_host()["output"]
    for instance in (first_instance, second_instance):
        marker = f"site.{instance}.mdns_publisher=active"
        if marker not in inspection:
            raise AssertionError(f"missing active Avahi publisher: {marker}")


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif sys.argv[1:] == ["verify"]:
        verify()
    else:
        raise SystemExit("usage: disposable_vm_smoke.py setup|verify")
