#!/usr/bin/env python3
"""Run real Laravel/Horizon traffic against a disposable TLS cluster and binding ACL."""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from gimme.resources_valkey import binding_username, laravel_access_string, namespace_prefixes
from gimme.valkey_contract import contract_variables

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "integration" / "laravel-valkey"
DEPLOYMENT = "laravel-live-test"
USERNAME = "gimme-laravel-live-test"
PASSWORD = "gimme-test-" + secrets.token_hex(24)
USES = ("cache", "session", "queue")
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


class Held:
    """One authenticated connection kept open across a rotation, as a running worker holds it."""

    def __init__(self, ca: Path, port: int, username: str, password: str) -> None:
        context = ssl.create_default_context(cafile=str(ca))
        raw = socket.create_connection(("localhost", port), timeout=5)
        self.socket = context.wrap_socket(raw, server_hostname="localhost")
        assert self.call("AUTH", username, password) == b"+OK"

    def call(self, *arguments: str) -> bytes:
        self.socket.sendall(
            f"*{len(arguments)}\r\n".encode()
            + b"".join(f"${len(item)}\r\n{item}\r\n".encode() for item in arguments)
        )
        reply = self.socket.recv(256)
        if not reply:
            raise ConnectionError("closed by the server")
        return reply.strip()

    def alive(self) -> bool:
        try:
            return self.call("PING") == b"+PONG"
        except (OSError, ssl.SSLError):
            return False

    def close(self) -> None:
        self.socket.close()


class Server:
    """A disposable TLS Valkey cluster whose ACL users are saved, so a restart keeps them."""

    def __init__(self, binary: str, client: str, directory: Path, ca: Path) -> None:
        self.binary, self.client, self.directory, self.ca = binary, client, directory, ca
        self.port, self.bus_port = free_port(), free_port()
        self.process: subprocess.Popen[bytes] | None = None
        self.locked = False
        (directory / "users.acl").write_text("")

    def start(self) -> None:
        self.process = subprocess.Popen([
            self.binary, "--port", "0", "--tls-port", str(self.port), "--bind", "127.0.0.1",
            "--cluster-port", str(self.bus_port),
            "--tls-cert-file", str(self.directory / "server.crt"),
            "--tls-key-file", str(self.directory / "server.key"),
            "--tls-ca-cert-file", str(self.ca), "--tls-auth-clients", "no",
            "--cluster-enabled", "yes", "--cluster-config-file", "nodes.conf",
            "--cluster-require-full-coverage", "yes", "--cluster-announce-hostname", "localhost",
            "--cluster-preferred-endpoint-type", "hostname", "--aclfile",
            str(self.directory / "users.acl"), "--dir", str(self.directory), "--save", "",
            "--appendonly", "no",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        PID_FILE.write_text(f"{self.process.pid}:{self.port}\n")

    def stop(self) -> None:
        if self.process is not None:
            stop(self.process)
        PID_FILE.unlink(missing_ok=True)

    def running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def cli(self, *arguments: str, authenticated: bool = False) -> str:
        """Once the default user is off, every call is the inspector's. The client exits 0 on
        an error reply, so an error reply is raised here rather than mistaken for output."""
        output = cli(
            self.client, self.ca, self.port, *arguments,
            authenticated=authenticated or self.locked,
        )
        if output.split(" ", 1)[0] in {"NOAUTH", "ERR", "NOPERM", "WRONGPASS", "WRONGTYPE"}:
            raise RuntimeError(f"disposable Valkey refused {arguments[0]}: {output[:80]}")
        return output

    def wait_ready(self, *, first: bool = False) -> None:
        deadline = time.monotonic() + 15
        while True:
            try:
                if self.cli("PING") == "PONG":
                    break
            except subprocess.CalledProcessError:
                pass
            if time.monotonic() >= deadline:
                raise RuntimeError("disposable Valkey did not start")
            time.sleep(0.1)
        if first:
            self.cli("CLUSTER", "ADDSLOTSRANGE", "0", "16383")
        while "cluster_state:ok" not in self.cli("CLUSTER", "INFO"):
            if time.monotonic() >= deadline:
                raise RuntimeError("disposable Valkey cluster did not become ready")
            time.sleep(0.1)

    def users(self) -> list[str]:
        return self.cli("ACL", "USERS").split()


class Contract:
    """Runs the real Laravel and Horizon contract as one credential, in a fresh PHP process."""

    def __init__(self, server: Server, uses: list[str] | None = None) -> None:
        self.server = server
        self.uses = uses or list(USES)
        self.values = contract_variables(DEPLOYMENT, self.uses, "localhost", server.port)
        self.passwords: set[str] = set()

    def environment(self, username: str, password: str) -> dict[str, str]:
        self.passwords.add(password)
        return {
            **os.environ, **self.values,
            "GIMME_VALKEY_USERNAME": username,
            "GIMME_VALKEY_PASSWORD": password,
            "GIMME_VALKEY_CA_FILE": str(self.server.ca),
        }

    def scrub(self, output: str) -> str:
        for password in self.passwords:
            output = output.replace(password, "[redacted]")
        return output.strip()

    def reset(self) -> None:
        """Each run starts from an empty Deployment namespace, so counts and queues are exact."""
        keys = self.server.cli(
            "KEYS", f"{{gimme:{DEPLOYMENT}}}:*", authenticated=True
        ).splitlines()
        if keys:
            self.server.cli("DEL", *keys, authenticated=True)

    def run(self, username: str, password: str) -> tuple[bool, str]:
        self.reset()
        result = subprocess.run(
            [executable("php"), str(FIXTURE / "contract.php")], cwd=FIXTURE,
            env=self.environment(username, password), check=False, capture_output=True,
            text=True, timeout=120,
        )
        output = self.scrub(result.stderr + result.stdout)
        return result.returncode == 0 and output.endswith("GIMME_LARAVEL_VALKEY|ready"), output

    def passes(self, username: str, password: str, why: str) -> None:
        ok, output = self.run(username, password)
        if not ok:
            detail = " | ".join(output.splitlines()[-12:]) if output else "no output"
            raise RuntimeError(f"{why}: real Laravel contract failed: {detail}")

    def is_refused(self, username: str, password: str, why: str, *markers: str) -> None:
        ok, output = self.run(username, password)
        if ok or not any(marker in output.upper() for marker in markers):
            wanted = "/".join(markers)
            raise RuntimeError(f"{why}: expected a refusal ({wanted}), got: {output[-300:]}")


def new_user(server: Server, username: str, password: str, access: list[str]) -> None:
    server.cli("ACL", "SETUSER", username, "on", f">{password}", *access)


def check_namespaces(server: Server, uses: list[str], *, leaves_keys: bool = True) -> None:
    keys = server.cli("KEYS", "*", authenticated=True).splitlines()
    prefixes = tuple(namespace_prefixes(DEPLOYMENT, uses).values())
    if (leaves_keys and not keys) or any(not key.startswith(prefixes) for key in keys):
        raise RuntimeError(f"Laravel traffic for {'+'.join(uses)} escaped its derived namespaces")
    if server.cli("EXISTS", "{gimme:other}:cache:escape", authenticated=True) != "0":
        raise RuntimeError("cross-Deployment write escaped the ACL")


def failed_candidates(server: Server, contract: Contract, held: Held) -> None:
    """A candidate that fails its probe leaves the live credential working, and rolling it back
    removes its user."""
    access = laravel_access_string(DEPLOYMENT).split()
    candidates = {
        # The secret holds a password the user does not have.
        # (Predis reports a refused AUTH in cluster mode as an empty connection pool.)
        "wrong password": (
            binding_username(DEPLOYMENT, 2), "gimme-test-wrong",
            ("WRONGPASS", "NO CONNECTIONS AVAILABLE"), [],
        ),
        # The user exists but its ACL lacks the scripting the Laravel queue and locks need.
        "missing permission": (
            binding_username(DEPLOYMENT, 3), "gimme-test-" + secrets.token_hex(24), ("NOPERM",),
            [item for item in access if item not in {"+eval", "+evalsha"}],
        ),
    }
    for why, (username, password, markers, acl) in candidates.items():
        real = password if acl else "gimme-test-" + secrets.token_hex(24)
        new_user(server, username, real, acl or access)
        contract.is_refused(username, password, f"candidate with {why}", *markers)
        contract.passes(USERNAME, PASSWORD, f"live credential after a candidate with {why}")
        if not held.alive():
            raise RuntimeError(f"a failed candidate with {why} disturbed a live connection")
        server.cli("ACL", "DELUSER", username)
        if username in server.users():
            raise RuntimeError(f"rollback left the candidate with {why} in place")
    contract.passes(USERNAME, PASSWORD, "live credential after every rollback")


def rotation(server: Server, contract: Contract, held: Held) -> tuple[str, str]:
    """The next generation is probed while the current one still works; only after that is the
    previous user deleted, which closes its connections and ends its credential."""
    username = binding_username(DEPLOYMENT, 2)
    password = "gimme-test-" + secrets.token_hex(24)
    new_user(server, username, password, laravel_access_string(DEPLOYMENT).split())
    contract.passes(username, password, "candidate generation")
    contract.passes(USERNAME, PASSWORD, "previous generation while the candidate is probed")
    if not held.alive():
        raise RuntimeError("probing the candidate closed the live connection")
    server.cli("ACL", "DELUSER", USERNAME)
    if held.alive():
        raise RuntimeError("the deleted user's connection stayed open")
    contract.is_refused(
        USERNAME, PASSWORD, "deleted generation",
        "WRONGPASS", "INVALID USERNAME", "NO CONNECTIONS AVAILABLE",
    )
    contract.passes(username, password, "new generation after the previous one is gone")
    server.cli("ACL", "SAVE")
    return username, password


def restart(server: Server, contract: Contract, username: str, password: str) -> None:
    """A worker holding a client meets a server restart: an operation during the outage fails
    within the contract's bounds, the worker recovers once it reconnects (Laravel's connection
    purge, or a supervisor restarting it), and a fresh process passes the whole contract."""
    process = subprocess.Popen(
        [executable("php"), str(FIXTURE / "reconnect.php")], cwd=FIXTURE,
        env=contract.environment(username, password), stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert process.stdin is not None and process.stdout is not None
    watchdog = threading.Timer(120, process.kill)
    watchdog.start()
    try:
        def expect(line: str) -> None:
            got = process.stdout.readline().strip()  # type: ignore[union-attr]
            if got != line:
                process.stdin.close()  # type: ignore[union-attr]
                rest = process.stdout.read() + (process.stderr.read() if process.stderr else "")  # type: ignore[union-attr]
                detail = contract.scrub(rest)
                raise RuntimeError(f"restart: expected {line!r}, got {got!r}: {detail[-300:]}")

        def send(command: str) -> None:
            process.stdin.write(command + "\n")  # type: ignore[union-attr]
            process.stdin.flush()  # type: ignore[union-attr]

        expect("ready")
        server.stop()
        send("down")
        expect("outage|bounded")
        server.start()
        server.wait_ready()
        if username not in server.users():
            raise RuntimeError("the saved ACL did not survive the restart")
        send("up")
        # Observed, not required: whether the client instance that lost its connection heals.
        observed = process.stdout.readline().strip()
        if not observed.startswith("same-client|"):
            raise RuntimeError(f"restart: expected a same-client report, got {observed!r}")
        print(f"restart: {observed}")
        expect("GIMME_LARAVEL_VALKEY|recovered")
    finally:
        watchdog.cancel()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
    contract.passes(username, password, "a new process after the restart")


def run() -> None:
    cleanup()
    binary = executable("valkey-server", "redis-server")
    client = executable("valkey-cli", "redis-cli")
    executable("php")
    with tempfile.TemporaryDirectory(prefix="gimme-laravel-valkey-") as temporary:
        directory = Path(temporary)
        server = Server(binary, client, directory, certificates(directory))
        server.start()
        try:
            server.wait_ready(first=True)
            new_user(
                server, USERNAME, PASSWORD, laravel_access_string(DEPLOYMENT).split()
            )
            server.cli(
                "ACL", "SETUSER", "inspector", "on", ">inspect", "allkeys", "+@all",
            )
            server.cli("ACL", "SETUSER", "default", "off")
            server.locked = True
            server.cli("ACL", "SAVE")
            contract = Contract(server)

            # Each selected use on its own, then all together, through real Laravel: each stays
            # inside its own derived namespace, and the binding ACL denies what it must.
            for uses in (["cache"], ["session"], ["queue"]):
                Contract(server, uses).passes(USERNAME, PASSWORD, f"{uses[0]} alone")
                check_namespaces(server, uses, leaves_keys=uses != ["session"])
            contract.passes(USERNAME, PASSWORD, "every use together")
            check_namespaces(server, contract.uses)

            held = Held(server.ca, server.port, USERNAME, PASSWORD)
            try:
                failed_candidates(server, contract, held)
                username, password = rotation(server, contract, held)
            finally:
                held.close()
            restart(server, contract, username, password)
            check_namespaces(server, contract.uses)
            print("GIMME_LARAVEL_VALKEY|ready")
        finally:
            server.stop()
            if server.running():
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
