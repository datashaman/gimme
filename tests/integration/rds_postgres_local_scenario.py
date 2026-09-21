"""Zero-cost proof of the managed PostgreSQL SQL and TLS lifecycle.

This starts a private disposable PostgreSQL cluster and invokes the exact fixed programs
embedded in deploy/programs.php. It needs PostgreSQL server binaries and OpenSSL, but no AWS
account, network access, or persistent system service.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import tempfile

from gimme.resources_postgres import login_role, owner_role


ROOT = Path(__file__).resolve().parents[2]


def _program(name: str) -> str:
    source = (ROOT / "deploy" / "programs.php").read_text()
    function = f"function {name}(): string"
    try:
        body = source.split(function, 1)[1]
        return body.split("<<<'PYTHON'\n", 1)[1].split("\nPYTHON;", 1)[0]
    except IndexError:
        raise RuntimeError(f"fixed program {name} is missing") from None


def _server_directory() -> Path:
    configured = os.environ.get("GIMME_POSTGRES_BIN")
    candidates = [] if configured is None else [Path(configured)]
    direct = shutil.which("postgres")
    if direct is not None:
        candidates.append(Path(direct).parent)
    pg_config = shutil.which("pg_config")
    if pg_config is not None:
        bindir = subprocess.run(
            [pg_config, "--bindir"], check=True, text=True, capture_output=True
        ).stdout.strip()
        candidates.append(Path(bindir))
    candidates.extend(sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True))
    candidates.extend(
        sorted(Path("/opt/homebrew/opt").glob("postgresql@*/bin"), reverse=True)
    )
    for candidate in candidates:
        if all(
            (candidate / name).is_file() and os.access(candidate / name, os.X_OK)
            for name in ("initdb", "pg_ctl", "postgres")
        ):
            return candidate
    raise RuntimeError("a complete PostgreSQL server binary directory is unavailable")


def _server_bin(name: str) -> str:
    return str(_server_directory() / name)


def available() -> bool:
    try:
        _server_directory()
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False
    return shutil.which("psql") is not None and shutil.which("openssl") is not None


def _port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(command: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, text=True, capture_output=True, env=env, check=False)


def _psql(
    port: int, database: str, username: str, password: str, bundle: Path, sql: str,
    *, succeeds: bool = True,
) -> str:
    result = _run(
        [
            shutil.which("psql") or "psql", "-h", "localhost", "-p", str(port),
            "-U", username, "-d", database, "--no-psqlrc", "-Atq", "-v",
            "ON_ERROR_STOP=1", "-c", sql,
        ],
        env={
            **os.environ,
            "PGPASSWORD": password,
            "PGSSLMODE": "verify-full",
            "PGSSLROOTCERT": str(bundle),
        },
    )
    if (result.returncode == 0) != succeeds:
        raise RuntimeError("disposable PostgreSQL assertion failed")
    return result.stdout.strip()


def _fixed(
    name: str, arguments: list[str], *, succeeds: bool = True,
) -> subprocess.CompletedProcess:
    result = _run(["python3", "-c", _program(name), *arguments])
    if (result.returncode == 0) != succeeds:
        detail = (result.stdout + result.stderr).strip()
        raise RuntimeError(
            f"fixed PostgreSQL program {name} returned an unexpected result: {detail}"
        )
    return result


def _certificates(directory: Path) -> tuple[Path, Path, Path]:
    ca_key = directory / "ca.key"
    ca = directory / "ca.pem"
    server_key = directory / "server.key"
    request = directory / "server.csr"
    server = directory / "server.pem"
    extension = directory / "server.ext"
    extension.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n")
    commands = [
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-subj", "/CN=gimme-disposable-ca", "-keyout", str(ca_key), "-out", str(ca)],
        ["openssl", "req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=localhost",
         "-keyout", str(server_key), "-out", str(request)],
        ["openssl", "x509", "-req", "-in", str(request), "-CA", str(ca),
         "-CAkey", str(ca_key), "-CAcreateserial", "-days", "1", "-extfile",
         str(extension), "-out", str(server)],
    ]
    for command in commands:
        subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    server_key.chmod(0o600)
    return ca, server, server_key


def run_scenario() -> dict[str, object]:
    if not available():
        raise RuntimeError("PostgreSQL server binaries and OpenSSL are required")
    with tempfile.TemporaryDirectory(prefix="gimme-rds-postgres-proof-") as temporary:
        root = Path(temporary)
        data = root / "data"
        ca, certificate, key = _certificates(root)
        master_password = secrets.token_urlsafe(32)
        password_file = root / "master-password"
        password_file.write_text(master_password + "\n")
        password_file.chmod(0o600)
        subprocess.run(
            [
                _server_bin("initdb"), "-D", str(data), "--username=gimme_admin",
                "--auth-host=scram-sha-256", "--auth-local=trust",
                f"--pwfile={password_file}", "--no-instructions",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        password_file.unlink()
        port = _port()
        options = (
            f"-h 127.0.0.1 -p {port} -c ssl=on "
            f"-c ssl_cert_file={certificate} -c ssl_key_file={key}"
        )
        subprocess.run(
            [_server_bin("pg_ctl"), "-D", str(data), "-o", options, "-w", "start"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            digest = hashlib.sha256(ca.read_bytes()).hexdigest()
            master = root / "master.json"
            master.write_text(json.dumps({"username": "gimme_admin", "password": master_password}))
            master.chmod(0o600)
            default_extension = _psql(
                port, "postgres", "gimme_admin", master_password, ca,
                "SELECT default_version FROM pg_available_extensions WHERE name='pgcrypto';",
            )
            if not default_extension:
                raise RuntimeError("the disposable PostgreSQL server lacks pgcrypto")

            databases = ("proof_alpha", "proof_beta")
            passwords = {database: secrets.token_urlsafe(32) for database in databases}
            for database in databases:
                secret = root / f"{database}.json"
                secret.write_text(json.dumps({
                    "master_username": "gimme_admin",
                    "master_password": master_password,
                    "workload_username": login_role(database, 1),
                    "workload_password": passwords[database],
                }))
                secret.chmod(0o600)
                _fixed("managed_postgres_bind_script", [
                    "localhost", str(port), database, owner_role(database),
                    login_role(database, 1), json.dumps({"pgcrypto": default_extension}),
                    str(secret), str(ca), digest,
                ])

            activation = _psql(
                port, databases[0], login_role(databases[0], 1), passwords[databases[0]], ca,
                "SELECT current_user || ':' || current_database() || ':' || ssl "
                "FROM pg_stat_ssl WHERE pid=pg_backend_pid();",
            )
            if not activation.endswith((":t", ":true")):
                raise RuntimeError("workload activation did not use TLS")
            _psql(
                port, databases[1], login_role(databases[0], 1), passwords[databases[0]], ca,
                "SELECT 1;", succeeds=False,
            )
            privilege = _psql(
                port, databases[0], "gimme_admin", master_password, ca,
                f"SELECT rolsuper,rolcreatedb,rolcreaterole FROM pg_roles "
                f"WHERE rolname='{login_role(databases[0], 1)}';",
            )
            extension = _psql(
                port, databases[0], "gimme_admin", master_password, ca,
                "SELECT extversion FROM pg_extension WHERE extname='pgcrypto';",
            )
            if privilege != "f|f|f" or extension != default_extension:
                raise RuntimeError("least privilege or extension pin verification failed")

            alpha = databases[0]
            candidate_password = secrets.token_urlsafe(32)
            candidate = root / "candidate.json"
            candidate.write_text(json.dumps({
                "master_username": "gimme_admin", "master_password": master_password,
                "workload_username": login_role(alpha, 2),
                "workload_password": candidate_password,
            }))
            candidate.chmod(0o600)
            _fixed("managed_postgres_bind_script", [
                "localhost", str(port), alpha, owner_role(alpha), login_role(alpha, 2),
                json.dumps({"pgcrypto": default_extension}), str(candidate), str(ca), digest,
            ])
            _psql(
                port, alpha, login_role(alpha, 2), "intentionally-wrong", ca,
                "SELECT 1;", succeeds=False,
            )
            _fixed("managed_postgres_retire_login_script", [
                "localhost", str(port), login_role(alpha, 2), str(master), str(ca), digest,
            ])
            _psql(
                port, alpha, login_role(alpha, 1), passwords[alpha], ca, "SELECT 1;"
            )

            _fixed("managed_postgres_bind_script", [
                "localhost", str(port), alpha, owner_role(alpha), login_role(alpha, 2),
                json.dumps({"pgcrypto": default_extension}), str(candidate), str(ca), digest,
            ])
            _fixed("managed_postgres_retire_login_script", [
                "localhost", str(port), login_role(alpha, 1), str(master), str(ca), digest,
            ])
            _fixed("managed_postgres_retire_login_script", [
                "localhost", str(port), login_role(alpha, 2), str(master), str(ca), digest,
            ])
            generation_three = root / "generation-three.json"
            generation_three_password = secrets.token_urlsafe(32)
            generation_three.write_text(json.dumps({
                "master_username": "gimme_admin", "master_password": master_password,
                "workload_username": login_role(alpha, 3),
                "workload_password": generation_three_password,
            }))
            generation_three.chmod(0o600)
            _fixed("managed_postgres_bind_script", [
                "localhost", str(port), alpha, owner_role(alpha), login_role(alpha, 3),
                json.dumps({"pgcrypto": default_extension}), str(generation_three),
                str(ca), digest,
            ])
            _psql(
                port, alpha, login_role(alpha, 3), generation_three_password, ca, "SELECT 1;"
            )

            for database, generation in ((alpha, 3), (databases[1], 1)):
                _fixed("managed_postgres_purge_allocation_script", [
                    "localhost", str(port), database, owner_role(database),
                    login_role(database, generation), str(master), str(ca), digest,
                ])
            remaining = _psql(
                port, "postgres", "gimme_admin", master_password, ca,
                "SELECT count(*) FROM pg_database WHERE datname IN ('proof_alpha','proof_beta');",
            )
            if remaining != "0":
                raise RuntimeError("allocation purge left a disposable database")
            report = {
                "state": "passed",
                "tls": "verify-full",
                "isolated_databases": 2,
                "extension": {"name": "pgcrypto", "version": default_extension},
                "least_privilege": True,
                "activation": True,
                "rotation_rollback": True,
                "detached_rebound_generation": 3,
                "purged_allocations": 2,
            }
            encoded = json.dumps(report, sort_keys=True)
            if any(secret in encoded for secret in (
                master_password, *passwords.values(), candidate_password,
                generation_three_password,
            )):
                raise RuntimeError("disposable report exposed a credential")
            return report
        finally:
            subprocess.run(
                [_server_bin("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )


if __name__ == "__main__":
    print(json.dumps(run_scenario(), sort_keys=True, separators=(",", ":")))
