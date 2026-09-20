#!/usr/bin/env python3
"""Run real Laravel/Horizon traffic against a disposable TLS cluster and binding ACL."""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from gimme.resources_valkey import laravel_access_string, namespace_prefixes
from gimme.valkey_contract import contract_variables

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "integration" / "laravel-valkey"
DEPLOYMENT = "laravel-live-test"
USERNAME = "gimme-laravel-live-test"
PASSWORD = "gimme-test-" + secrets.token_hex(24)
PID_FILE = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / "gimme-laravel-valkey.pid"


def executable(*names: str) -> str:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    raise RuntimeError(f"missing executable: {' or '.join(names)}")


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def command(arguments: list[str], *, cwd: Path | None = None) -> str:
    result = subprocess.run(arguments, cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def cli(binary: str, ca: Path, port: int, *arguments: str, authenticated: bool = False) -> str:
    credentials = ["--user", "inspector", "--pass", "inspect"] if authenticated else []
    return command([
        binary, "--tls", "--cacert", str(ca), "-h", "localhost", "-p", str(port),
        *credentials, *arguments,
    ])


def certificates(directory: Path) -> Path:
    openssl = executable("openssl")
    (directory / "ext.cnf").write_text(
        "subjectAltName=DNS:localhost\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n"
    )
    command([
        openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
        "-out", "ca.crt", "-subj", "/CN=gimme-integration-ca", "-days", "2",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign",
    ], cwd=directory)
    command([
        openssl, "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "server.key",
        "-out", "server.csr", "-subj", "/CN=localhost",
    ], cwd=directory)
    command([
        openssl, "x509", "-req", "-in", "server.csr", "-CA", "ca.crt", "-CAkey",
        "ca.key", "-CAcreateserial", "-out", "server.crt", "-days", "2", "-extfile",
        "ext.cnf",
    ], cwd=directory)
    return directory / "ca.crt"


def stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def cleanup() -> None:
    """Stop a process left by a cancelled test, without trusting a reused PID."""
    try:
        pid_text, port_text = PID_FILE.read_text().strip().split(":", 1)
        pid, port = int(pid_text), int(port_text)
    except (OSError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return
    inspected = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
    ).stdout
    is_test_server = (
        ("valkey-server" in inspected or "redis-server" in inspected)
        and str(port) in inspected
    )
    if is_test_server:
        os.kill(pid, signal.SIGTERM)
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    PID_FILE.unlink(missing_ok=True)


def run() -> None:
    cleanup()
    server = executable("valkey-server", "redis-server")
    client = executable("valkey-cli", "redis-cli")
    php = executable("php")
    with tempfile.TemporaryDirectory(prefix="gimme-laravel-valkey-") as temporary:
        directory = Path(temporary)
        ca = certificates(directory)
        port, bus_port = free_port(), free_port()
        process = subprocess.Popen([
            server, "--port", "0", "--tls-port", str(port), "--bind", "127.0.0.1",
            "--cluster-port", str(bus_port), "--tls-cert-file", str(directory / "server.crt"),
            "--tls-key-file", str(directory / "server.key"), "--tls-ca-cert-file", str(ca),
            "--tls-auth-clients", "no", "--cluster-enabled", "yes",
            "--cluster-config-file", "nodes.conf", "--cluster-require-full-coverage", "yes",
            "--cluster-announce-hostname", "localhost", "--cluster-preferred-endpoint-type",
            "hostname", "--dir", str(directory), "--save", "", "--appendonly", "no",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PID_FILE.write_text(f"{process.pid}:{port}\n")
        try:
            deadline = time.monotonic() + 15
            while True:
                try:
                    if cli(client, ca, port, "PING") == "PONG":
                        break
                except subprocess.CalledProcessError:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("disposable Valkey did not start")
                time.sleep(0.1)
            cli(client, ca, port, "CLUSTER", "ADDSLOTSRANGE", "0", "16383")
            while "cluster_state:ok" not in cli(client, ca, port, "CLUSTER", "INFO"):
                if time.monotonic() >= deadline:
                    raise RuntimeError("disposable Valkey cluster did not become ready")
                time.sleep(0.1)
            cli(
                client, ca, port, "ACL", "SETUSER", USERNAME, "on", f">{PASSWORD}",
                *laravel_access_string(DEPLOYMENT).split(),
            )
            cli(
                client, ca, port, "ACL", "SETUSER", "inspector", "on", ">inspect",
                "allkeys", "+@all",
            )
            cli(client, ca, port, "ACL", "SETUSER", "default", "off")

            values = contract_variables(
                DEPLOYMENT, ["cache", "session", "queue"], "localhost", port
            )
            environment = {
                **os.environ, **values,
                "GIMME_VALKEY_USERNAME": USERNAME,
                "GIMME_VALKEY_PASSWORD": PASSWORD,
                "GIMME_VALKEY_CA_FILE": str(ca),
            }
            result = subprocess.run(
                [php, str(FIXTURE / "contract.php")], cwd=FIXTURE, env=environment,
                check=False, capture_output=True, text=True,
            )
            if result.returncode != 0:
                output = (result.stderr + result.stdout).replace(PASSWORD, "[redacted]").strip()
                detail = (
                    " | ".join(output.splitlines()[-12:])
                    if output else f"exit {result.returncode}"
                )
                raise RuntimeError(f"real Laravel contract failed: {detail}")
            if result.stdout.strip() != "GIMME_LARAVEL_VALKEY|ready":
                raise RuntimeError("real Laravel contract did not report ready")

            keys = cli(client, ca, port, "KEYS", "*", authenticated=True).splitlines()
            prefixes = tuple(namespace_prefixes(
                DEPLOYMENT, ["cache", "session", "queue"]
            ).values())
            if not keys or any(not key.startswith(prefixes) for key in keys):
                raise RuntimeError("Laravel traffic escaped the derived namespaces")
            escaped = cli(
                client, ca, port, "EXISTS", "{gimme:other}:cache:escape", authenticated=True
            )
            if escaped != "0":
                raise RuntimeError("cross-Deployment write escaped the ACL")
            print("GIMME_LARAVEL_VALKEY|ready")
        finally:
            stop(process)
            PID_FILE.unlink(missing_ok=True)
            if process.poll() is None:
                raise RuntimeError("disposable Valkey process is still running")


if __name__ == "__main__":
    if os.environ.get("GIMME_INTEGRATION_DISPOSABLE") != "1":
        raise SystemExit("refusing to run without GIMME_INTEGRATION_DISPOSABLE=1")
    if sys.argv[1:] == ["cleanup"]:
        cleanup()
        raise SystemExit(0)
    try:
        run()
    except Exception as error:
        print(f"laravel_valkey_smoke_failed:{type(error).__name__}", file=sys.stderr)
        raise
