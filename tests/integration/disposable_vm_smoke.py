from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]
STATE_DIRECTORY = Path(os.environ.get("GIMME_STATE_DIR", ROOT / "config"))
STATE_PATH = STATE_DIRECTORY / "state.json"
APPS_ROOT = "/srv/gimme-integration/apps"
HOSTNAME = "gimme-ci.local"
REPLACEMENT_HOSTNAME = "gimme-ci-replacement.local"
TARGET = "integration"
DEPLOYMENTS = ("smoke-default", "smoke-preview")
BACKUP_DESTINATION = "primary"
BACKUP_BUCKET = "gimme-ci-recovery"
MINIO_ENDPOINT = "https://127.0.0.1:9000"
RECOVERY_DEPLOYMENT = "smoke-default"


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
        "health": "inherit",
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
            "valkey": {"resource": "integration-valkey", "uses": ["cache"]},
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
        "schema_version": 5,
        "provider_accounts": {},
        "secret_stores": {"local-sops": {"provider": "sops"}},
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
                "default_health": {
                    "name": "primary",
                    "phases": ["candidate", "live"],
                    "path": "/up",
                    "expected_status": 200,
                    "attempts": 2,
                    "delay_seconds": 0,
                    "timeout_seconds": 5,
                },
                "health_probes": [],
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
            "integration-valkey-replacement": {
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
        stdout=subprocess.PIPE,
    )
    return result.stdout.strip()


def ssh_python(program: str) -> None:
    subprocess.run(
        ["ssh", "-o", "BatchMode=yes", HOSTNAME, "python3", "-"],
        input=program,
        check=True,
        text=True,
        stdout=subprocess.DEVNULL,
    )


def ssh_python_output(program: str, *, timeout: float | None = None) -> str:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", HOSTNAME, "python3", "-"],
        input=program,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        timeout=timeout,
    ).stdout.strip()


def deployment_route_status(*, tls: bool = False) -> str:
    scheme = "https" if tls else "http"
    port = 443 if tls else 80
    host = f"{RECOVERY_DEPLOYMENT}.gimme-ci.local"
    return ssh(
        "curl", "-ksS" if tls else "-sS", "-o", "/dev/null", "-w", "%{http_code}",
        "--resolve", f"{host}:{port}:127.0.0.1", f"{scheme}://{host}/",
    )


def seed_recovery_valkey_state() -> dict[str, object]:
    """Seed two Deployment prefixes with binary, persistent, expiring, and expired keys."""
    output = ssh_python_output(textwrap.dedent(
        """
        import base64
        import json
        import socket
        import time

        connection = socket.create_connection(("127.0.0.1", 6379), timeout=10)
        reader = connection.makefile("rb")

        def call(*arguments):
            parts = [item if isinstance(item, bytes) else str(item).encode() for item in arguments]
            connection.sendall(
                b"*%d\\r\\n" % len(parts)
                + b"".join(b"$%d\\r\\n%s\\r\\n" % (len(item), item) for item in parts)
            )
            line = reader.readline()
            kind, value = line[:1], line[1:-2]
            if kind == b"+":
                return value
            if kind == b":":
                return int(value)
            if kind == b"$":
                size = int(value)
                if size < 0:
                    return None
                return reader.read(size + 2)[:-2]
            raise RuntimeError("unexpected Valkey response")

        binary_key = b"gimme:smoke-default:\\x00binary"
        persistent_key = b"gimme:smoke-default:persistent"
        expiring_key = b"gimme:smoke-default:expiring"
        expired_key = b"gimme:smoke-default:expired"
        unrelated_key = b"gimme:smoke-preview:unrelated"
        call("SET", binary_key, b"\\x00binary-value\\xff")
        call("SET", persistent_key, b"persistent")
        call("SET", expiring_key, b"expiring", "PX", 3600000)
        call("SET", expired_key, b"expired", "PX", 1)
        call("SET", unrelated_key, b"must-not-appear")
        time.sleep(0.05)
        expected = {}
        for key in (binary_key, persistent_key, expiring_key):
            expected[base64.b64encode(key).decode()] = {
                "dump": base64.b64encode(call("DUMP", key)).decode(),
                "expiry": call("PEXPIRETIME", key),
            }
        print(json.dumps({
            "expected": expected,
            "expired": base64.b64encode(expired_key).decode(),
            "unrelated": base64.b64encode(unrelated_key).decode(),
        }))
        """
    ))
    return json.loads(output)


def observe_recovery_valkey_state() -> dict[str, object]:
    """Read fixed integration keys without accepting a caller-supplied key or prefix."""
    return json.loads(ssh_python_output(textwrap.dedent(
        """
        import base64
        import json
        import socket

        connection = socket.create_connection(("127.0.0.1", 6379), timeout=10)
        reader = connection.makefile("rb")

        def call(*arguments):
            parts = [item if isinstance(item, bytes) else str(item).encode() for item in arguments]
            connection.sendall(
                b"*%d\\r\\n" % len(parts)
                + b"".join(b"$%d\\r\\n%s\\r\\n" % (len(item), item) for item in parts)
            )
            line = reader.readline()
            kind, value = line[:1], line[1:-2]
            if kind == b"+":
                return value
            if kind == b":":
                return int(value)
            if kind == b"$":
                size = int(value)
                if size < 0:
                    return None
                return reader.read(size + 2)[:-2]
            raise RuntimeError("unexpected Valkey response")

        selected = (
            b"gimme:smoke-default:\\x00binary",
            b"gimme:smoke-default:persistent",
            b"gimme:smoke-default:expiring",
            b"gimme:smoke-default:unexpected",
        )
        observed = {}
        for key in selected:
            payload = call("DUMP", key)
            if payload is not None:
                observed[base64.b64encode(key).decode()] = {
                    "dump": base64.b64encode(payload).decode(),
                    "expiry": call("PEXPIRETIME", key),
                }
        unrelated = b"gimme:smoke-preview:unrelated"
        unrelated_dump = call("DUMP", unrelated)
        concurrent = call("GET", b"gimme:smoke-preview:concurrent")
        print(json.dumps({
            "selected": observed,
            "unrelated_dump": (
                None if unrelated_dump is None
                else base64.b64encode(unrelated_dump).decode()
            ),
            "concurrent": (
                None if concurrent is None else int(concurrent)
            ),
        }))
        """
    )))


def mutate_recovery_valkey_state(*, empty: bool = False) -> None:
    """Mutate only the fixed selected integration prefix."""
    ssh_python(textwrap.dedent(
        f"""
        import socket

        connection = socket.create_connection(("127.0.0.1", 6379), timeout=10)
        reader = connection.makefile("rb")

        def call(*arguments):
            parts = [item if isinstance(item, bytes) else str(item).encode() for item in arguments]
            connection.sendall(
                b"*%d\\r\\n" % len(parts)
                + b"".join(b"$%d\\r\\n%s\\r\\n" % (len(item), item) for item in parts)
            )
            line = reader.readline()
            if line[:1] not in (b"+", b":"):
                raise RuntimeError("unexpected Valkey response")

        keys = (
            b"gimme:smoke-default:\\x00binary",
            b"gimme:smoke-default:persistent",
            b"gimme:smoke-default:expiring",
            b"gimme:smoke-default:unexpected",
        )
        call("UNLINK", *keys)
        if not {empty!r}:
            call("SET", b"gimme:smoke-default:persistent", b"mutated")
            call("SET", b"gimme:smoke-default:unexpected", b"must-be-cleared")
        """
    ))


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
    valkey_version = observed_version(
        ssh("valkey-server", "--version"),
        r"v=(\d+(?:\.\d+){0,3})",
        "Valkey",
    )
    state["resources"]["integration-valkey"]["version"] = valkey_version
    state["resources"]["integration-valkey-replacement"]["version"] = valkey_version
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
        # postgresql.service is a Type=oneshot meta-unit that wraps the real
        # postgresql@<ver>-main instance, so it reports "active (exited)", never
        # "(running)"; match the unit-type-agnostic "Active: active" line instead.
        if "Active: active" not in status:
            raise AssertionError(f"{service} is not active")


def minio_client():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=MINIO_ENDPOINT,
        region_name="us-east-1",
        config=Config(s3={"addressing_style": "path"}),
    )


def create_minio_bucket() -> None:
    """Provision the bucket outside Gimme, exactly as a real operator would."""
    client = minio_client()
    existing = {bucket["Name"] for bucket in client.list_buckets().get("Buckets", [])}
    if BACKUP_BUCKET not in existing:
        client.create_bucket(Bucket=BACKUP_BUCKET)
    client.put_bucket_versioning(
        Bucket=BACKUP_BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )


def install_restore_fixture() -> None:
    """Install the smallest Laravel-shaped current release needed for private health."""
    deploy_path = f"{APPS_ROOT}/deployments/{RECOVERY_DEPLOYMENT}"
    files = {
        "current/artisan": """#!/usr/bin/env php
<?php
$env = parse_ini_file(__DIR__ . '/../shared/.env', false, INI_SCANNER_RAW);
$root = dirname(__DIR__);
if (in_array('queue:work', $argv, true)) {
    if (is_file($root . '/shared/force-process-failure')) { exit(1); }
    while (true) { sleep(1); }
}
foreach (['optimize', 'optimize:clear', 'queue:restart'] as $command) {
    if (in_array($command, $argv, true)) { exit(0); }
}
$connection = pg_connect(sprintf(
    'host=%s port=%s dbname=%s user=%s password=%s',
    $env['DB_HOST'], $env['DB_PORT'], $env['DB_DATABASE'],
    $env['DB_USERNAME'], $env['DB_PASSWORD']
));
if ($connection === false || !in_array('migrate:status', $argv, true)) {
    exit(1);
}
$result = pg_query($connection, 'SELECT value FROM gimme_restore_probe WHERE id = 1');
exit($result !== false && pg_fetch_result($result, 0, 0) === 'before' ? 0 : 1);
""",
        "current/vendor/autoload.php": r"""<?php
namespace Illuminate\Contracts\Http {
    interface Kernel {}
}
namespace Illuminate\Http {
    final class Request {
        public static function create(...$arguments): self { return new self(); }
    }
}
namespace Smoke {
    final class Response {
        public function __construct(private int $status) {}
        public function getStatusCode(): int { return $this->status; }
    }
    final class Kernel implements \Illuminate\Contracts\Http\Kernel {
        public function handle($request): Response {
            $root = dirname(__DIR__, 2);
            if (is_file($root . '/shared/force-health-failure')) {
                return new Response(500);
            }
            $env = parse_ini_file($root . '/shared/.env', false, INI_SCANNER_RAW);
            $connection = pg_connect(sprintf(
                'host=%s port=%s dbname=%s user=%s password=%s',
                $env['DB_HOST'], $env['DB_PORT'], $env['DB_DATABASE'],
                $env['DB_USERNAME'], $env['DB_PASSWORD']
            ));
            if ($connection === false) { return new Response(500); }
            $result = pg_query(
                $connection,
                'SELECT value FROM gimme_restore_probe WHERE id = 1'
            );
            $ready = $result !== false && pg_fetch_result($result, 0, 0) === 'before';
            return new Response($ready ? 200 : 500);
        }
        public function terminate($request, $response): void {}
    }
    final class App {
        public function make($contract): Kernel { return new Kernel(); }
    }
}
""",
        "current/bootstrap/app.php": "<?php return new \\Smoke\\App();\n",
        "current/bootstrap/cache/.gitignore": "",
        "current/public/index.php": "<?php http_response_code(200); echo 'ready';\n",
    }
    ssh_python(textwrap.dedent(
        f"""
        import os
        from pathlib import Path

        root = Path({deploy_path!r})
        files = {files!r}
        for relative, content in files.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            mode = (
                0o700 if relative.endswith("artisan")
                else 0o644 if relative.endswith("public/index.php")
                else 0o600
            )
            os.chmod(path, mode)
        """
    ))


def set_restore_probe(value: str) -> None:
    database = json.loads(STATE_PATH.read_text())["deployments"][
        RECOVERY_DEPLOYMENT
    ]["placement"]["database_identifier"]
    if value not in {"before", "after"}:
        raise AssertionError("invalid fixed restore probe value")
    if re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database) is None:
        raise AssertionError("invalid fixed integration database identity")
    statement = (
        f"SET client_min_messages = warning; SET ROLE {database}; "
        "CREATE TABLE IF NOT EXISTS gimme_restore_probe "
        "(id integer PRIMARY KEY, value text NOT NULL); "
        f"INSERT INTO gimme_restore_probe VALUES (1, '{value}') "
        "ON CONFLICT (id) DO UPDATE SET value = EXCLUDED.value"
    )
    ssh_python(textwrap.dedent(
        f"""
        import subprocess
        subprocess.run(
            ["psql", "-v", "ON_ERROR_STOP=1", "-d", {database!r}, "-c", {statement!r}],
            check=True,
        )
        """
    ))


def restore_probe_value() -> str:
    database = json.loads(STATE_PATH.read_text())["deployments"][
        RECOVERY_DEPLOYMENT
    ]["placement"]["database_identifier"]
    return ssh_python_output(textwrap.dedent(
        f"""
        import subprocess
        result = subprocess.run(
            ["psql", "-Atq", "-d", {database!r}, "-c",
             {f"SET ROLE {database}; SELECT value FROM gimme_restore_probe WHERE id = 1"!r}],
            check=True, text=True, stdout=subprocess.PIPE,
        )
        print(result.stdout.strip())
        """
    ))


def postgres_blocker(database: str, *, wait_for_database: bool = False) -> subprocess.Popen:
    """Hold one identifiable real PostgreSQL session until the caller terminates it."""
    if re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database) is None:
        raise AssertionError("invalid fixed integration database identity")
    program = textwrap.dedent(
        f"""
        import os
        import subprocess
        import time

        database = {database!r}
        if {wait_for_database!r}:
            for _ in range(3000):
                observed = subprocess.run(
                    ["psql", "-Atq", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                     "-c", f"SELECT 1 FROM pg_database WHERE datname = '{{database}}'"],
                    text=True, capture_output=True,
                )
                if observed.returncode == 0 and observed.stdout.strip() == "1":
                    break
                time.sleep(0.01)
            else:
                raise SystemExit(2)
        subprocess.run(
            ["psql", "-d", database, "-c", "SELECT pg_sleep(300)"],
            env={{**os.environ, "PGAPPNAME": "gimme-integration-blocker"}},
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        """
    )
    process = subprocess.Popen(
        ["ssh", "-o", "BatchMode=yes", HOSTNAME, "python3", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if process.stdin is None:
        raise AssertionError("could not open PostgreSQL blocker input")
    process.stdin.write(program.encode())
    process.stdin.close()
    return process


def postgres_admin(statement: str, *, as_postgres: bool = False) -> str:
    """Run one internally fixed statement without OpenSSH shell re-tokenization."""
    return ssh_python_output(textwrap.dedent(
        f"""
        import subprocess
        command = ["psql", "-Atq", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                   "-c", {statement!r}]
        if {as_postgres!r}:
            command = ["sudo", "-n", "-u", "postgres", *command]
        result = subprocess.run(
            command,
            check=True, text=True, stdout=subprocess.PIPE,
        )
        print(result.stdout.strip())
        """
    ))


def wait_for_postgres_blocker(database: str, *, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    statement = (
        "SELECT count(*) FROM pg_stat_activity "
        f"WHERE datname = '{database}' "
        "AND application_name = 'gimme-integration-blocker'"
    )
    while time.monotonic() < deadline:
        if postgres_admin(statement) == "1":
            return
        time.sleep(0.05)
    raise AssertionError("PostgreSQL blocker did not connect")


def stop_postgres_blocker(process: subprocess.Popen, database: str) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)
    statement = (
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
        f"WHERE datname = '{database}' "
        "AND application_name = 'gimme-integration-blocker'"
    )
    postgres_admin(statement)


def set_database_connections(database: str, allowed: bool) -> None:
    if re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database) is None:
        raise AssertionError("invalid fixed integration database identity")
    action = "true" if allowed else "false"
    postgres_admin(
        f'ALTER DATABASE "{database}" ALLOW_CONNECTIONS {action}',
        as_postgres=True,
    )


def complete_restore(gimme, request_id: str) -> None:
    verification = gimme.plan_verify_restore(RECOVERY_DEPLOYMENT, request_id)
    completed = gimme.apply_verify_restore(
        RECOVERY_DEPLOYMENT, request_id, str(verification["plan_id"])
    )
    if completed["state"] != "completed":
        raise AssertionError(f"Restore did not complete: {completed}")
    if deployment_route_status(tls=True) != "200":
        raise AssertionError("completed Restore did not return public routing online")


def assert_restore_maintenance() -> None:
    if deployment_route_status(tls=True) != "503":
        raise AssertionError("data-replaced Restore exposed routing before verification")


def assert_recovery_valkey_state(
    expected: dict[str, object], unrelated_dump: object,
) -> None:
    observed = observe_recovery_valkey_state()
    if observed["selected"] != expected:
        raise AssertionError("Valkey Restore did not reproduce exact payloads and expiries")
    if observed["unrelated_dump"] != unrelated_dump:
        raise AssertionError("Valkey Restore changed the other Deployment prefix")


def verify_valkey_restore(gimme, first_point_id: str, seeded: dict[str, object]) -> None:
    """Exercise both partial variants, full Restore, empty prefix, and isolation."""
    install_restore_fixture()
    set_restore_probe("before")
    unrelated_dump = observe_recovery_valkey_state()["unrelated_dump"]

    mutate_recovery_valkey_state()
    valkey_plan = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, first_point_id, "ci-valkey-partial", ["valkey"]
    )
    if not valkey_plan["ready"] or not valkey_plan["partial"]:
        raise AssertionError(f"Valkey partial Restore was not ready: {valkey_plan}")
    writer_stop = threading.Event()
    writer_ready = threading.Event()
    writer_times: list[float] = []
    writer_errors: list[Exception] = []

    def write_other_deployment_prefix() -> None:
        while not writer_stop.is_set():
            try:
                value = ssh_python_output(textwrap.dedent(
                    """
                    import socket

                    connection = socket.create_connection(("127.0.0.1", 6379), timeout=10)
                    reader = connection.makefile("rb")
                    key = b"gimme:smoke-preview:concurrent"
                    arguments = (b"INCR", key)
                    connection.sendall(
                        b"*2\\r\\n"
                        + b"".join(
                            b"$%d\\r\\n%s\\r\\n" % (len(item), item)
                            for item in arguments
                        )
                    )
                    response = reader.readline()
                    if response[:1] != b":" or int(response[1:-2]) < 1:
                        raise RuntimeError("unexpected Valkey response")
                    print(response[1:-2].decode())
                    """
                ), timeout=15)
                if int(value) < 1:
                    raise AssertionError("concurrent writer did not advance")
                writer_times.append(time.monotonic())
                writer_ready.set()
            except Exception as exc:
                writer_errors.append(exc)
                writer_ready.set()
                return

    writer = threading.Thread(target=write_other_deployment_prefix)
    writer.start()
    if not writer_ready.wait(15):
        writer_stop.set()
        writer.join(timeout=20)
        raise AssertionError("concurrent writer did not start")
    if writer_errors:
        writer_stop.set()
        writer.join(timeout=20)
        raise AssertionError("concurrent writer failed to start") from writer_errors[0]
    restore_started = time.monotonic()
    try:
        gimme.apply_restore_deployment(
            RECOVERY_DEPLOYMENT, first_point_id, "ci-valkey-partial",
            str(valkey_plan["plan_id"]), str(valkey_plan["confirmation"]), ["valkey"],
        )
    finally:
        restore_finished = time.monotonic()
        writer_stop.set()
        writer.join(timeout=20)
    if writer.is_alive():
        raise AssertionError("concurrent writer did not stop")
    if writer_errors:
        raise AssertionError("concurrent writer failed") from writer_errors[0]
    if not any(restore_started <= item <= restore_finished for item in writer_times):
        raise AssertionError("other Deployment did not operate concurrently with Restore")
    if observe_recovery_valkey_state()["concurrent"] is None:
        raise AssertionError("Restore removed the concurrent other-Deployment key")
    assert_restore_maintenance()
    if restore_probe_value() != "before":
        raise AssertionError("Valkey-only Restore changed PostgreSQL")
    assert_recovery_valkey_state(seeded["expected"], unrelated_dump)
    valkey_record = gimme.restore_record_resource(
        RECOVERY_DEPLOYMENT, "ci-valkey-partial"
    )
    safety_id = str(valkey_record["safety_recovery_point_id"])
    safety = next(
        item for item in gimme.list_recovery_points(RECOVERY_DEPLOYMENT)["recovery_points"]
        if item["recovery_point_id"] == safety_id
    )
    if [item["kind"] for item in safety["components"]] != ["valkey"]:
        raise AssertionError(f"Valkey-only Safety captured another component: {safety}")
    complete_restore(gimme, "ci-valkey-partial")

    set_restore_probe("before")
    paired_seed = seed_recovery_valkey_state()
    capture = gimme.plan_create_recovery_point(RECOVERY_DEPLOYMENT, "ci-paired-source")
    created = gimme.create_recovery_point(
        RECOVERY_DEPLOYMENT, "ci-paired-source", str(capture["plan_id"])
    )
    paired_point_id = str(created["recovery_point"]["recovery_point_id"])

    set_restore_probe("after")
    mutate_recovery_valkey_state()
    mutated_valkey = observe_recovery_valkey_state()["selected"]
    postgres_plan = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, paired_point_id, "ci-postgres-partial", ["postgres"]
    )
    gimme.apply_restore_deployment(
        RECOVERY_DEPLOYMENT, paired_point_id, "ci-postgres-partial",
        str(postgres_plan["plan_id"]), str(postgres_plan["confirmation"]), ["postgres"],
    )
    assert_restore_maintenance()
    if restore_probe_value() != "before":
        raise AssertionError("PostgreSQL-only Restore did not restore PostgreSQL")
    if observe_recovery_valkey_state()["selected"] != mutated_valkey:
        raise AssertionError("PostgreSQL-only Restore changed Valkey")
    complete_restore(gimme, "ci-postgres-partial")

    set_restore_probe("after")
    full_plan = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, paired_point_id, "ci-full-restore"
    )
    gimme.apply_restore_deployment(
        RECOVERY_DEPLOYMENT, paired_point_id, "ci-full-restore",
        str(full_plan["plan_id"]), str(full_plan["confirmation"]),
    )
    assert_restore_maintenance()
    if restore_probe_value() != "before":
        raise AssertionError("full Restore did not restore PostgreSQL")
    assert_recovery_valkey_state(paired_seed["expected"], unrelated_dump)
    complete_restore(gimme, "ci-full-restore")

    mutate_recovery_valkey_state(empty=True)
    empty_plan = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, paired_point_id, "ci-empty-valkey", ["valkey"]
    )
    gimme.apply_restore_deployment(
        RECOVERY_DEPLOYMENT, paired_point_id, "ci-empty-valkey",
        str(empty_plan["plan_id"]), str(empty_plan["confirmation"]), ["valkey"],
    )
    assert_restore_maintenance()
    assert_recovery_valkey_state(paired_seed["expected"], unrelated_dump)
    complete_restore(gimme, "ci-empty-valkey")

    from gimme.control import DeploymentRegistration, ResourceBindings, ValkeyBinding

    current = gimme.store.deployment(RECOVERY_DEPLOYMENT)
    current_target = gimme.store.load().targets[TARGET]
    replacement_target = current_target.model_copy(
        update={
            "hostname": REPLACEMENT_HOSTNAME,
            "system_hostname": "gimme-ci-replacement",
            "network": current_target.network.model_copy(
                update={"mdns_name": "gimme-ci-replacement"}
            ),
        }
    )
    target_update = gimme.plan_update_target(TARGET, replacement_target)
    gimme.update_target(TARGET, replacement_target, str(target_update["plan_id"]))
    if gimme.store.load().targets[TARGET].hostname != REPLACEMENT_HOSTNAME:
        raise AssertionError("replacement Target identity was not applied")
    if gimme.store.deployment(RECOVERY_DEPLOYMENT).placement != current.placement:
        raise AssertionError("Target replacement changed immutable Deployment placement")

    replacement_registration = DeploymentRegistration.from_deployment(current).model_copy(
        update={"resources": ResourceBindings(
            database=current.resources.database,
            valkey=ValkeyBinding(
                resource="integration-valkey-replacement", uses=["cache"]
            ),
        )}
    )
    update = gimme.plan_update_deployment(
        RECOVERY_DEPLOYMENT, replacement_registration
    )
    gimme.update_deployment(
        RECOVERY_DEPLOYMENT, replacement_registration, str(update["plan_id"])
    )
    rebound = gimme.store.deployment(RECOVERY_DEPLOYMENT)
    if rebound.placement != current.placement:
        raise AssertionError("Valkey Resource replacement changed immutable placement")

    mutate_recovery_valkey_state()
    replacement = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, paired_point_id, "ci-replacement-valkey", ["valkey"]
    )
    replacement_destination = replacement["destinations"][0]
    replacement_resource = gimme.store.load().resources[
        "integration-valkey-replacement"
    ]
    if (
        not replacement["ready"]
        or replacement_destination["resource"] != "integration-valkey-replacement"
        or replacement_destination["provider"] != "target_local"
        or replacement_destination["kind"] != "valkey"
        or replacement_destination["version"] != replacement_resource.version
    ):
        raise AssertionError(
            f"replacement Valkey Resource was not accepted: {replacement}"
        )
    gimme.apply_restore_deployment(
        RECOVERY_DEPLOYMENT, paired_point_id, "ci-replacement-valkey",
        str(replacement["plan_id"]), str(replacement["confirmation"]), ["valkey"],
    )
    assert_restore_maintenance()
    assert_recovery_valkey_state(paired_seed["expected"], unrelated_dump)
    complete_restore(gimme, "ci-replacement-valkey")


def verify_postgres_restore(gimme) -> None:
    """Exercise non-empty, failed verification/retry, and empty-replacement Restore."""
    from gimme.config import QueueWorkerConfig
    from gimme.control import DeploymentRegistration, RecoveryPolicy
    from gimme.recovery import RecoveryError

    current = gimme.store.deployment(RECOVERY_DEPLOYMENT)
    proposed = DeploymentRegistration.from_deployment(current).model_copy(
        update={
            "recovery": RecoveryPolicy(
                destination=BACKUP_DESTINATION, valkey=False, quiesce_wait_seconds=1,
            ),
            "workers": QueueWorkerConfig(processes=1),
        }
    )
    policy_plan = gimme.plan_update_deployment(RECOVERY_DEPLOYMENT, proposed)
    gimme.update_deployment(
        RECOVERY_DEPLOYMENT, proposed, str(policy_plan["plan_id"])
    )
    install_restore_fixture()
    resources_plan = gimme.plan_deployment_resources(RECOVERY_DEPLOYMENT)
    if not resources_plan["ready"]:
        raise AssertionError(f"worker resources were not ready: {resources_plan}")
    gimme.apply_deployment_resources(
        RECOVERY_DEPLOYMENT, str(resources_plan["plan_id"])
    )
    worker_unit = f"gimme-worker-{RECOVERY_DEPLOYMENT}@1.service"
    if ssh("systemctl", "show", "--property=ActiveState", "--value", worker_unit) != (
        "active"
    ):
        raise AssertionError("managed worker was not active before Restore")
    set_restore_probe("before")

    state = gimme.store.load()
    resource = state.resources["integration-postgres"]
    incompatible = resource.model_copy(
        update={"version": f"{resource.version}.999"}
    )
    version_plan = gimme.plan_update_resource("integration-postgres", incompatible)
    gimme.update_resource(
        "integration-postgres", incompatible, str(version_plan["plan_id"])
    )
    incompatible_capture = gimme.plan_create_recovery_point(
        RECOVERY_DEPLOYMENT, "ci-version-source"
    )
    incompatible_created = gimme.create_recovery_point(
        RECOVERY_DEPLOYMENT, "ci-version-source", str(incompatible_capture["plan_id"])
    )
    incompatible_point_id = str(
        incompatible_created["recovery_point"]["recovery_point_id"]
    )
    revert_plan = gimme.plan_update_resource("integration-postgres", resource)
    gimme.update_resource(
        "integration-postgres", resource, str(revert_plan["plan_id"])
    )
    rejected = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, incompatible_point_id, "ci-version-reject"
    )
    if rejected["readiness_issues"] != ["source_version_incompatible"]:
        raise AssertionError(f"exact-version mismatch was not rejected: {rejected}")

    capture = gimme.plan_create_recovery_point(
        RECOVERY_DEPLOYMENT, "ci-postgres-source"
    )
    created = gimme.create_recovery_point(
        RECOVERY_DEPLOYMENT, "ci-postgres-source", str(capture["plan_id"])
    )
    point_id = str(created["recovery_point"]["recovery_point_id"])
    database = gimme.store.deployment(
        RECOVERY_DEPLOYMENT
    ).placement.database_identifier

    with patch.object(
        gimme.shutil, "disk_usage", return_value=SimpleNamespace(free=0)
    ):
        insufficient = gimme.plan_restore_deployment(
            RECOVERY_DEPLOYMENT, point_id, "ci-capacity-reject"
        )
    if insufficient["readiness_issues"] != ["restore_capacity_insufficient"]:
        raise AssertionError(
            f"insufficient controller capacity was not rejected: {insufficient}"
        )
    if deployment_route_status(tls=True) != "200":
        raise AssertionError("capacity rejection entered maintenance")

    other = gimme.store.deployment("smoke-preview")
    other_with_recovery = DeploymentRegistration.from_deployment(other).model_copy(
        update={"recovery": RecoveryPolicy(
            destination=BACKUP_DESTINATION, valkey=False, quiesce_wait_seconds=1,
        )}
    )
    other_plan = gimme.plan_update_deployment("smoke-preview", other_with_recovery)
    gimme.update_deployment(
        "smoke-preview", other_with_recovery, str(other_plan["plan_id"])
    )
    try:
        gimme.plan_restore_deployment(
            "smoke-preview", point_id, "ci-owner-reject"
        )
    except RecoveryError as exc:
        if str(exc) != "restore_source_missing":
            raise AssertionError(f"cross-Deployment source was not bounded: {exc}") from exc
    else:
        raise AssertionError("cross-Deployment Recovery Point was accepted")

    set_restore_probe("after")
    restore = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, point_id, "ci-nonempty-restore"
    )
    if not restore["ready"] or restore["destination"]["empty"]:
        raise AssertionError(f"non-empty Restore did not require Safety capture: {restore}")
    live_blocker = postgres_blocker(database)
    wait_for_postgres_blocker(database)
    try:
        applied = gimme.apply_restore_deployment(
            RECOVERY_DEPLOYMENT, point_id, "ci-nonempty-restore",
            str(restore["plan_id"]), str(restore["confirmation"]),
        )
    finally:
        stop_postgres_blocker(live_blocker, database)
    if applied["state"] != "data_replaced" or restore_probe_value() != "before":
        raise AssertionError(f"PostgreSQL data was not replaced: {applied}")
    record = gimme.restore_record_resource(
        RECOVERY_DEPLOYMENT, "ci-nonempty-restore"
    )
    safety_id = record["safety_recovery_point_id"]
    if safety_id is None:
        raise AssertionError("non-empty Restore did not publish a Safety Recovery Point")
    safety_plan = gimme.plan_delete_recovery_point(
        RECOVERY_DEPLOYMENT, str(safety_id)
    )
    if not safety_plan["safety_protected"]:
        raise AssertionError("unresolved Safety Recovery Point was not protected")

    artisan = f"{APPS_ROOT}/deployments/{RECOVERY_DEPLOYMENT}/current/artisan"
    blocked_artisan = f"{artisan}.process-failure"
    ssh("mv", artisan, blocked_artisan)
    process_verification = gimme.plan_verify_restore(
        RECOVERY_DEPLOYMENT, "ci-nonempty-restore"
    )
    try:
        try:
            gimme.apply_verify_restore(
                RECOVERY_DEPLOYMENT, "ci-nonempty-restore",
                str(process_verification["plan_id"]),
            )
        except RecoveryError as exc:
            if str(exc) != "restore_verification_failed":
                raise AssertionError(f"process failure was not bounded: {exc}") from exc
        else:
            raise AssertionError("missing managed-process executable unexpectedly verified")
    finally:
        ssh("mv", blocked_artisan, artisan)
        ssh("sudo", "-n", "systemctl", "reset-failed", worker_unit)
    if gimme.restore_record_resource(
        RECOVERY_DEPLOYMENT, "ci-nonempty-restore"
    )["state"] != "verification_failed":
        raise AssertionError("managed-process failure was not recorded")
    assert_restore_maintenance()

    try:
        set_database_connections(database, False)
        verification = gimme.plan_verify_restore(
            RECOVERY_DEPLOYMENT, "ci-nonempty-restore"
        )
        try:
            gimme.apply_verify_restore(
                RECOVERY_DEPLOYMENT, "ci-nonempty-restore",
                str(verification["plan_id"]),
            )
        except RecoveryError as exc:
            if str(exc) != "restore_verification_failed":
                raise AssertionError(f"database failure was not bounded: {exc}") from exc
        else:
            raise AssertionError("disabled database connections unexpectedly verified")
    finally:
        set_database_connections(database, True)
    if gimme.restore_record_resource(
        RECOVERY_DEPLOYMENT, "ci-nonempty-restore"
    )["state"] != "verification_failed":
        raise AssertionError("failed verification was not recorded")
    maintenance_status = deployment_route_status(tls=True)
    if maintenance_status != "503":
        raise AssertionError("failed verification exposed restored data")
    gate = f"{APPS_ROOT}/deployments/{RECOVERY_DEPLOYMENT}/shared/force-health-failure"
    ssh("touch", gate)
    health_retry = gimme.plan_verify_restore(
        RECOVERY_DEPLOYMENT, "ci-nonempty-restore"
    )
    try:
        gimme.apply_verify_restore(
            RECOVERY_DEPLOYMENT, "ci-nonempty-restore",
            str(health_retry["plan_id"]),
        )
    except RecoveryError as exc:
        if str(exc) != "restore_verification_failed":
            raise AssertionError(f"private health failure was not bounded: {exc}") from exc
    else:
        raise AssertionError("forced private health failure unexpectedly passed")
    ssh("rm", "-f", gate)
    retry = gimme.plan_verify_restore(RECOVERY_DEPLOYMENT, "ci-nonempty-restore")
    completed = gimme.apply_verify_restore(
        RECOVERY_DEPLOYMENT, "ci-nonempty-restore", str(retry["plan_id"])
    )
    if completed["state"] != "completed":
        raise AssertionError(f"Restore retry did not complete: {completed}")
    live_status = deployment_route_status(tls=True)
    if live_status != "200":
        raise AssertionError(f"completed Restore did not recover the route: {live_status}")
    if gimme.plan_delete_recovery_point(
        RECOVERY_DEPLOYMENT, str(safety_id)
    )["safety_protected"]:
        raise AssertionError("completed Restore did not release its Safety point")

    set_restore_probe("after")
    compensation_request = "ci-swap-compensation"
    compensation = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, point_id, compensation_request
    )
    shadow_digest = hashlib.sha256(
        f"gimme-postgres-restore-v1\0{database}\0{compensation_request}".encode()
    ).hexdigest()[:24]
    shadow_database = f"gimme_shadow_{shadow_digest}"
    shadow_blocker = postgres_blocker(shadow_database, wait_for_database=True)
    try:
        try:
            gimme.apply_restore_deployment(
                RECOVERY_DEPLOYMENT, point_id, compensation_request,
                str(compensation["plan_id"]), str(compensation["confirmation"]),
            )
        except RecoveryError as exc:
            if str(exc) != "restore_swap_failed":
                raise AssertionError(f"swap failure was not bounded: {exc}") from exc
        else:
            raise AssertionError("shadow connection did not force swap compensation")
        wait_for_postgres_blocker(shadow_database, timeout=5)
    finally:
        stop_postgres_blocker(shadow_blocker, shadow_database)
    if restore_probe_value() != "after":
        raise AssertionError("failed swap did not compensate the original database name")
    if gimme.restore_record_resource(
        RECOVERY_DEPLOYMENT, compensation_request
    )["state"] != "shadow_verified":
        raise AssertionError("failed swap did not remain resumable at shadow verification")
    assert_restore_maintenance()
    compensation_retry = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, point_id, compensation_request
    )
    resumed = gimme.apply_restore_deployment(
        RECOVERY_DEPLOYMENT, point_id, compensation_request,
        str(compensation_retry["plan_id"]), str(compensation_retry["confirmation"]),
    )
    if resumed["state"] != "data_replaced" or restore_probe_value() != "before":
        raise AssertionError("compensated swap did not resume safely")
    complete_restore(gimme, compensation_request)

    ssh("sudo", "-n", "-u", "postgres", "dropdb", database)
    ssh("sudo", "-n", "-u", "postgres", "createdb", "--owner", database, database)
    replacement = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, point_id, "ci-empty-replacement"
    )
    if not replacement["ready"] or not replacement["destination"]["empty"]:
        raise AssertionError(f"empty replacement was not recognized: {replacement}")
    gimme.apply_restore_deployment(
        RECOVERY_DEPLOYMENT, point_id, "ci-empty-replacement",
        str(replacement["plan_id"]), str(replacement["confirmation"]),
    )
    finish = gimme.plan_verify_restore(
        RECOVERY_DEPLOYMENT, "ci-empty-replacement"
    )
    gimme.apply_verify_restore(
        RECOVERY_DEPLOYMENT, "ci-empty-replacement", str(finish["plan_id"])
    )
    replacement_record = gimme.restore_record_resource(
        RECOVERY_DEPLOYMENT, "ci-empty-replacement"
    )
    if replacement_record["safety_recovery_point_id"] is not None:
        raise AssertionError("empty replacement unexpectedly created a Safety point")
    if restore_probe_value() != "before":
        raise AssertionError("empty replacement Restore did not recover PostgreSQL")
    replacement_status = deployment_route_status(tls=True)
    if replacement_status != "200":
        raise AssertionError("empty replacement Restore did not recover the application")

    corrupt_capture = gimme.plan_create_recovery_point(
        RECOVERY_DEPLOYMENT, "ci-corrupt-source"
    )
    corrupt_created = gimme.create_recovery_point(
        RECOVERY_DEPLOYMENT, "ci-corrupt-source", str(corrupt_capture["plan_id"])
    )
    corrupt_point = str(corrupt_created["recovery_point"]["recovery_point_id"])
    corrupt_key = (
        f"gimme/recovery-points/{RECOVERY_DEPLOYMENT}/{corrupt_point}/postgres.dump"
    )
    versions = minio_client().list_object_versions(
        Bucket=BACKUP_BUCKET, Prefix=corrupt_key
    ).get("Versions", [])
    bound = next(
        (item for item in versions if item["Key"] == corrupt_key and item["IsLatest"]),
        None,
    )
    if bound is None:
        raise AssertionError("corruption fixture component version is missing")
    minio_client().delete_object(
        Bucket=BACKUP_BUCKET, Key=corrupt_key, VersionId=str(bound["VersionId"])
    )
    try:
        gimme.plan_restore_deployment(
            RECOVERY_DEPLOYMENT, corrupt_point, "ci-corrupt-reject"
        )
    except RecoveryError as exc:
        if str(exc) != "recovery_manifest_tampered":
            raise AssertionError(f"corrupt source failure was not bounded: {exc}") from exc
    else:
        raise AssertionError("missing bound source version was accepted")
    if deployment_route_status(tls=True) != "200":
        raise AssertionError("source corruption rejection entered maintenance")

    without_worker = DeploymentRegistration.from_deployment(
        gimme.store.deployment(RECOVERY_DEPLOYMENT)
    ).model_copy(update={"workers": None})
    without_worker_plan = gimme.plan_update_deployment(
        RECOVERY_DEPLOYMENT, without_worker
    )
    gimme.update_deployment(
        RECOVERY_DEPLOYMENT, without_worker, str(without_worker_plan["plan_id"])
    )
    cleanup_plan = gimme.plan_deployment_resources(RECOVERY_DEPLOYMENT)
    gimme.apply_deployment_resources(
        RECOVERY_DEPLOYMENT, str(cleanup_plan["plan_id"])
    )
    if ssh("systemctl", "show", "--property=ActiveState", "--value", worker_unit) == (
        "active"
    ):
        raise AssertionError("managed worker remained active after the matrix")


def reconcile_replacement_helper_policy(gimme) -> None:
    """Replacement Target identity changes require a fresh terminal bootstrap policy."""
    before = str(gimme.inspect_target(TARGET)["output"])
    if "privileged_helper=bootstrap_required" not in before:
        raise AssertionError("replacement Target did not invalidate the prior helper policy")
    subprocess.run(["gimme-bootstrap-target", TARGET], check=True)
    after = str(gimme.inspect_target(TARGET)["output"])
    if "privileged_helper=ready" not in after:
        raise AssertionError("replacement Target helper policy was not reconciled")


def verify_backup_destination() -> None:
    """Register a real S3-compatible destination, bind recovery, and prove the tracer."""
    require_disposable_host()
    from gimme import server as gimme
    from gimme.control import S3BackupDestination, SSEAES256

    create_minio_bucket()

    definition = S3BackupDestination(
        bucket=BACKUP_BUCKET,
        region="us-east-1",
        endpoint="127.0.0.1:9000",
        addressing="path",
        encryption=SSEAES256(),
    )
    destination_plan = gimme.plan_register_backup_destination(BACKUP_DESTINATION, definition)
    gimme.register_backup_destination(
        BACKUP_DESTINATION, definition, str(destination_plan["plan_id"])
    )

    current = gimme.store.deployment(RECOVERY_DEPLOYMENT)
    from gimme.control import DeploymentRegistration, RecoveryPolicy

    proposed = DeploymentRegistration.from_deployment(current).model_copy(
        update={"recovery": RecoveryPolicy(
            destination=BACKUP_DESTINATION, valkey=True, quiesce_wait_seconds=1,
        )}
    )
    update_plan = gimme.plan_update_deployment(RECOVERY_DEPLOYMENT, proposed)
    gimme.update_deployment(RECOVERY_DEPLOYMENT, proposed, str(update_plan["plan_id"]))

    seeded = seed_recovery_valkey_state()
    plan = gimme.plan_create_recovery_point(RECOVERY_DEPLOYMENT, "ci-smoke-1")
    result = gimme.create_recovery_point(
        RECOVERY_DEPLOYMENT, "ci-smoke-1", str(plan["plan_id"])
    )
    if not result["changed"]:
        raise AssertionError(
            f"first on-demand Recovery Point apply should not be a no-op: {result}"
        )

    duplicate = gimme.create_recovery_point(
        RECOVERY_DEPLOYMENT, "ci-smoke-1", str(plan["plan_id"])
    )
    if duplicate["changed"]:
        raise AssertionError("duplicate apply with the same request_id must be a no-op")
    if duplicate["recovery_point"] != result["recovery_point"]:
        raise AssertionError("duplicate apply must return the exact published manifest")

    inventory = gimme.list_recovery_points(RECOVERY_DEPLOYMENT)
    point_ids = {item["recovery_point_id"] for item in inventory["recovery_points"]}
    if result["recovery_point"]["recovery_point_id"] not in point_ids:
        raise AssertionError(f"created Recovery Point is missing from inventory: {inventory}")
    if inventory["rejected"]:
        raise AssertionError(f"inventory unexpectedly rejected a manifest: {inventory}")

    point_id = result["recovery_point"]["recovery_point_id"]
    valkey_key = f"gimme/recovery-points/{RECOVERY_DEPLOYMENT}/{point_id}/valkey.dump"
    archive = minio_client().get_object(Bucket=BACKUP_BUCKET, Key=valkey_key)["Body"].read()
    records = [json.loads(line) for line in archive.splitlines()[1:]]
    captured = {record["key"]: record for record in records}
    expected = seeded["expected"]
    if set(captured) != set(expected):
        raise AssertionError("Valkey archive did not isolate the selected Deployment prefix")
    for key, metadata in expected.items():
        if captured[key]["dump"] != metadata["dump"]:
            raise AssertionError("Valkey archive did not preserve a binary DUMP payload")
        expected_expiry = metadata["expiry"]
        actual_expiry = captured[key]["expires_at_ms"]
        if expected_expiry == -1 and actual_expiry is not None:
            raise AssertionError("persistent Valkey key became expiring")
        if expected_expiry > 0 and actual_expiry != expected_expiry:
            raise AssertionError("absolute Valkey expiry changed during capture")
    if seeded["expired"] in captured or seeded["unrelated"] in captured:
        raise AssertionError("expired or unrelated Valkey key appeared in the archive")

    route_status = deployment_route_status()
    if route_status == "503":
        raise AssertionError("Recovery Point capture left the Deployment in maintenance")
    for service in ("postgresql", "valkey-server", "caddy"):
        if "Active: active" not in str(gimme.target_service_status(TARGET, service)["output"]):
            raise AssertionError(f"{service} was not running after recovery capture")

    verify_valkey_restore(gimme, str(point_id), seeded)
    reconcile_replacement_helper_policy(gimme)
    verify_postgres_restore(gimme)

    postgres_key = (
        "gimme/recovery-points/"
        f"{RECOVERY_DEPLOYMENT}/{result['recovery_point']['recovery_point_id']}/postgres.dump"
    )
    bound_version = supersede_component(postgres_key)
    superseded_inventory = gimme.list_recovery_points(RECOVERY_DEPLOYMENT)
    superseded_ids = {
        item["recovery_point_id"] for item in superseded_inventory["recovery_points"]
    }
    if point_id not in superseded_ids:
        raise AssertionError(
            f"unreferenced object version altered the Recovery Point: {superseded_inventory}"
        )
    remove_component_version(postgres_key, bound_version)
    missing_inventory = gimme.list_recovery_points(RECOVERY_DEPLOYMENT)
    missing_point = next(
        (
            item for item in missing_inventory["recovery_points"]
            if item["recovery_point_id"] == point_id
        ),
        None,
    )
    if missing_point is None or (
        missing_point["state"], missing_point["deleted_components"],
        missing_point["remaining_components"],
    ) != ("deletion_failed", 1, 1):
        raise AssertionError(
            f"missing bound component did not enter deletion_failed: {missing_inventory}"
        )

    for path in (STATE_PATH, STATE_DIRECTORY / "operations.jsonl"):
        if path.exists() and "gimme-ci-secret" in path.read_text():
            raise AssertionError(f"MinIO credential leaked into {path}")


def supersede_component(key: str) -> str:
    client = minio_client()
    versions = client.list_object_versions(Bucket=BACKUP_BUCKET, Prefix=key).get(
        "Versions", []
    )
    bound = next(
        (item for item in versions if item["Key"] == key and item["IsLatest"]), None
    )
    if bound is None:
        raise AssertionError("published component version is missing")
    client.put_object(Bucket=BACKUP_BUCKET, Key=key, Body=b"corrupted-after-publish")
    return str(bound["VersionId"])


def remove_component_version(key: str, version_id: str) -> None:
    minio_client().delete_object(
        Bucket=BACKUP_BUCKET, Key=key, VersionId=version_id
    )


if __name__ == "__main__":
    if sys.argv[1:] == ["setup"]:
        setup()
    elif sys.argv[1:] == ["pin-resources"]:
        pin_resources()
    elif sys.argv[1:] == ["verify"]:
        verify()
    elif sys.argv[1:] == ["verify-backup-destination"]:
        verify_backup_destination()
    else:
        raise SystemExit(
            "usage: disposable_vm_smoke.py "
            "setup|pin-resources|verify|verify-backup-destination"
        )
