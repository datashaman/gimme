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


def ssh_python_output(program: str) -> str:
    return subprocess.run(
        ["ssh", "-o", "BatchMode=yes", HOSTNAME, "python3", "-"],
        input=program,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
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
        f"SET ROLE {database}; "
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


def verify_postgres_restore(gimme) -> None:
    """Exercise non-empty, failed verification/retry, and empty-replacement Restore."""
    from gimme.control import DeploymentRegistration, RecoveryPolicy
    from gimme.recovery import RecoveryError

    current = gimme.store.deployment(RECOVERY_DEPLOYMENT)
    proposed = DeploymentRegistration.from_deployment(current).model_copy(
        update={"recovery": RecoveryPolicy(
            destination=BACKUP_DESTINATION, valkey=False, quiesce_wait_seconds=1,
        )}
    )
    policy_plan = gimme.plan_update_deployment(RECOVERY_DEPLOYMENT, proposed)
    gimme.update_deployment(
        RECOVERY_DEPLOYMENT, proposed, str(policy_plan["plan_id"])
    )
    install_restore_fixture()
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

    set_restore_probe("after")
    restore = gimme.plan_restore_deployment(
        RECOVERY_DEPLOYMENT, point_id, "ci-nonempty-restore"
    )
    if not restore["ready"] or restore["destination"]["empty"]:
        raise AssertionError(f"non-empty Restore did not require Safety capture: {restore}")
    applied = gimme.apply_restore_deployment(
        RECOVERY_DEPLOYMENT, point_id, "ci-nonempty-restore",
        str(restore["plan_id"]), str(restore["confirmation"]),
    )
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

    gate = f"{APPS_ROOT}/deployments/{RECOVERY_DEPLOYMENT}/shared/force-health-failure"
    ssh("touch", gate)
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
            raise AssertionError(f"Restore failure was not bounded: {exc}") from exc
    else:
        raise AssertionError("forced private health failure unexpectedly passed")
    if gimme.restore_record_resource(
        RECOVERY_DEPLOYMENT, "ci-nonempty-restore"
    )["state"] != "verification_failed":
        raise AssertionError("failed verification was not recorded")
    maintenance_status = deployment_route_status(tls=True)
    if maintenance_status != "503":
        raise AssertionError("failed verification exposed restored data")
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

    database = gimme.store.deployment(
        RECOVERY_DEPLOYMENT
    ).placement.database_identifier
    ssh("dropdb", database)
    ssh("createdb", "--owner", database, database)
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

    verify_postgres_restore(gimme)

    tamper_component(
        "gimme/recovery-points/"
        f"{RECOVERY_DEPLOYMENT}/{result['recovery_point']['recovery_point_id']}/postgres.dump"
    )
    tampered_inventory = gimme.list_recovery_points(RECOVERY_DEPLOYMENT)
    tampered_ids = {item["recovery_point_id"] for item in tampered_inventory["recovery_points"]}
    if point_id in tampered_ids:
        raise AssertionError(f"tampered component was not rejected: {tampered_inventory}")
    if point_id not in tampered_inventory["rejected"]:
        raise AssertionError(f"tampered manifest missing from rejected list: {tampered_inventory}")

    for path in (STATE_PATH, STATE_DIRECTORY / "operations.jsonl"):
        if path.exists() and "gimme-ci-secret" in path.read_text():
            raise AssertionError(f"MinIO credential leaked into {path}")


def tamper_component(key: str) -> None:
    minio_client().put_object(Bucket=BACKUP_BUCKET, Key=key, Body=b"corrupted-after-publish")


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
