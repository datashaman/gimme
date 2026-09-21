"""The activation probe runs against a real local TLS, cluster-mode, ACL-enforcing server
(skipped without redis-server and openssl), using the access string a binding really creates."""

import json
import shutil
import socket
import ssl
import subprocess
import time
from pathlib import Path

import pytest

from gimme.resources_valkey import laravel_access_string
from gimme.valkey_contract import contract_variables, probe_config, probe_names

ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT = "shop-production"
PASSWORD = "p" * 48
REDIS = shutil.which("redis-server")
CLI = shutil.which("redis-cli")
OPENSSL = shutil.which("openssl")
needs_server = pytest.mark.skipif(
    not (REDIS and CLI and OPENSSL), reason="redis-server, redis-cli, and openssl required"
)


def probe_module() -> dict[str, object]:
    source = (ROOT / "deploy" / "programs.php").read_text()
    program = source.split("function valkey_probe_script", 1)[1].split(
        "return <<<'PYTHON'\n", 1
    )[1].split("\nPYTHON;", 1)[0]
    namespace: dict[str, object] = {"__name__": "valkey_probe"}
    exec(compile(program, "valkey_probe", "exec"), namespace)
    return namespace


PROBE = probe_module()


def make_certificates(directory: Path) -> Path:
    def openssl(*arguments: str) -> None:
        subprocess.run([OPENSSL, *arguments], cwd=directory, check=True, capture_output=True)

    (directory / "ext.cnf").write_text(
        "subjectAltName=DNS:localhost\nauthorityKeyIdentifier=keyid\n"
        "subjectKeyIdentifier=hash\n"
    )
    # Python's default context is strict: the CA needs basic constraints and key usage.
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key",
            "-out", "ca.crt", "-subj", "/CN=test-ca", "-days", "2",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-keyout", "srv.key",
            "-out", "srv.csr", "-subj", "/CN=localhost")
    openssl("x509", "-req", "-in", "srv.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-CAcreateserial", "-out", "srv.crt", "-days", "2", "-extfile", "ext.cnf")
    return directory


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class Server:
    """A single-node, cluster-mode, TLS server whose only usable account is the binding's."""

    def __init__(
        self, directory: Path, certificates: Path, slots: tuple[str, str] | None = ("0", "16383"),
        full_coverage: bool = True,
    ) -> None:
        self.ca = certificates / "ca.crt"
        self.inspecting = False
        self.port, bus = free_port(), free_port()
        self.process = subprocess.Popen(
            [
                REDIS, "--port", "0", "--tls-port", str(self.port), "--bind", "127.0.0.1",
                "--cluster-port", str(bus),
                "--tls-cert-file", str(certificates / "srv.crt"),
                "--tls-key-file", str(certificates / "srv.key"),
                "--tls-ca-cert-file", str(self.ca), "--tls-auth-clients", "no",
                "--cluster-enabled", "yes", "--cluster-config-file", "nodes.conf",
                "--dir", str(directory), "--save", "", "--appendonly", "no",
                "--cluster-require-full-coverage", "yes" if full_coverage else "no",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 10
        while self.cli("PING", check=False) != "PONG":
            assert time.monotonic() < deadline, "redis-server did not start"
            time.sleep(0.05)
        if slots:
            self.cli("CLUSTER", "ADDSLOTSRANGE", *slots)
            while "cluster_state:ok" not in self.cli("CLUSTER", "INFO"):
                time.sleep(0.05)
        self.cli(
            "ACL", "SETUSER", "app", "on", f">{PASSWORD}",
            *laravel_access_string(DEPLOYMENT).split(),
        )
        self.cli("ACL", "SETUSER", "inspector", "on", ">inspect", "allkeys", "+@all")
        self.cli("ACL", "SETUSER", "default", "off")
        self.inspecting = True

    def cli(self, *arguments: str, check: bool = True) -> str:
        credentials = ["--user", "inspector", "--pass", "inspect"] if self.inspecting else []
        result = subprocess.run(
            [CLI, "--tls", "--cacert", str(self.ca), "-h", "localhost", "-p", str(self.port),
             *credentials, *arguments],
            capture_output=True, text=True, check=False,
        )
        assert not check or result.returncode == 0, result.stdout + result.stderr
        return result.stdout.strip()

    def context(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.ca))

    def stop(self) -> None:
        self.process.terminate()
        self.process.wait(timeout=10)


@pytest.fixture(scope="session")
def certificates(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_certificates(tmp_path_factory.mktemp("certificates"))


@pytest.fixture
def server(tmp_path: Path, certificates: Path):
    started = Server(tmp_path, certificates)
    yield started
    started.stop()


@pytest.fixture(scope="module")
def shared(tmp_path_factory: pytest.TempPathFactory, certificates: Path):
    """For tests that only read: one server for the module instead of one per test."""
    started = Server(tmp_path_factory.mktemp("shared"), certificates)
    yield started
    started.stop()


def config(
    server: Server, uses: list[str], host: str = "localhost", horizon: bool | None = None
) -> dict[str, object]:
    horizon = "queue" in uses if horizon is None else horizon
    return probe_config(DEPLOYMENT, uses, host, server.port, horizon)  # type: ignore[arg-type]


def environment(server: Server, uses: list[str], host: str = "localhost") -> dict[str, str]:
    return {
        **contract_variables(DEPLOYMENT, uses, host, server.port),  # type: ignore[arg-type]
        "GIMME_VALKEY_USERNAME": "app",
        "GIMME_VALKEY_PASSWORD": PASSWORD,
    }


def lockfile(tmp_path: Path, framework: str = "v12.0.0", horizon: str = "v5.46.0") -> Path:
    path = tmp_path / "composer.lock"
    path.write_text(json.dumps({"packages": [
        {"name": "laravel/framework", "version": framework},
        {"name": "laravel/horizon", "version": horizon},
    ]}))
    return path


def run_probe(
    server: Server, tmp_path: Path, uses: list[str], *, host: str = "localhost",
    context: ssl.SSLContext | None = None, values: dict[str, str] | None = None,
    lock: Path | None = None, horizon: bool | None = None,
) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = PROBE["probe"](  # type: ignore[operator]
        config(server, uses, host, horizon), values or environment(server, uses, host),
        str(lock or lockfile(tmp_path)), context or server.context(), lines.append,
    )
    return code, lines


def outcome(lines: list[str]) -> str:
    return lines[-1]


@needs_server
def test_probe_passes_every_declared_use_and_leaves_no_keys(tmp_path: Path, shared: Server) -> None:
    code, lines = run_probe(shared, tmp_path, ["cache", "session", "queue"])

    assert code == 0
    expected = probe_names(["cache", "session", "queue"], True)  # type: ignore[arg-type]
    assert "use-horizon" in expected and len(expected) == 13
    assert lines == [f"GIMME_VALKEY_PROBE|{name}|ready" for name in expected]
    assert shared.cli("DBSIZE") == "0"


@needs_server
def test_a_queue_without_horizon_needs_no_horizon_package_or_check(
    tmp_path: Path, shared: Server
) -> None:
    empty = tmp_path / "empty.lock"
    empty.write_text(json.dumps({"packages": []}))

    code, lines = run_probe(shared, tmp_path, ["queue"], lock=empty, horizon=False)

    assert code == 0
    assert lines == [
        f"GIMME_VALKEY_PROBE|{name}|ready"
        for name in probe_names(["queue"], False)  # type: ignore[arg-type]
    ]
    assert not any("horizon" in line for line in lines)


@needs_server
def test_probe_checks_only_the_declared_uses(tmp_path: Path, shared: Server) -> None:
    code, lines = run_probe(shared, tmp_path, ["cache"])

    assert code == 0
    assert "GIMME_VALKEY_PROBE|use-cache|ready" in lines
    assert not any("use-session" in line or "horizon" in line for line in lines)


@needs_server
def test_probe_rejects_a_wrong_password_without_echoing_it(tmp_path: Path, shared: Server) -> None:
    values = {**environment(shared, ["cache"]), "GIMME_VALKEY_PASSWORD": "wrong-" + PASSWORD}

    code, lines = run_probe(shared, tmp_path, ["cache"], values=values)

    assert code == 1
    assert outcome(lines) == "GIMME_VALKEY_PROBE|auth|failed|auth"
    assert not any(PASSWORD in line for line in lines)


@needs_server
def test_probe_fails_when_the_default_user_can_be_used_without_a_password(
    tmp_path: Path, server: Server
) -> None:
    server.cli("ACL", "SETUSER", "default", "on", "nopass", "allkeys", "+@all")

    code, lines = run_probe(server, tmp_path, ["cache"])

    assert code == 1
    assert outcome(lines) == "GIMME_VALKEY_PROBE|default-user|failed|default_user_open"


@needs_server
def test_probe_fails_closed_on_an_untrusted_certificate(tmp_path: Path, shared: Server) -> None:
    code, lines = run_probe(shared, tmp_path, ["cache"], context=ssl.create_default_context())

    assert code == 1
    assert outcome(lines) == "GIMME_VALKEY_PROBE|tls|failed|tls_verify"


@needs_server
def test_probe_fails_closed_when_the_certificate_does_not_match_the_host(
    tmp_path: Path, shared: Server
) -> None:
    code, lines = run_probe(shared, tmp_path, ["cache"], host="127.0.0.1")

    assert code == 1
    assert outcome(lines) == "GIMME_VALKEY_PROBE|tls|failed|tls_verify"


@needs_server
def test_probe_fails_when_the_cluster_serves_no_slots(
    tmp_path: Path, certificates: Path
) -> None:
    started = Server(tmp_path, certificates, slots=None)
    try:
        code, lines = run_probe(started, tmp_path, ["cache"])
    finally:
        started.stop()

    assert code == 1
    assert outcome(lines) == "GIMME_VALKEY_PROBE|cluster|failed|cluster_state"


@needs_server
def test_probe_fails_when_the_cluster_does_not_serve_every_slot_from_one_shard(
    tmp_path: Path, certificates: Path
) -> None:
    started = Server(tmp_path, certificates, slots=("0", "8191"), full_coverage=False)
    try:
        code, lines = run_probe(started, tmp_path, ["cache"])
    finally:
        started.stop()

    assert code == 1
    assert outcome(lines) == "GIMME_VALKEY_PROBE|cluster|failed|cluster_slots"


@needs_server
@pytest.mark.parametrize("grant", [
    ("allkeys", "allchannels"),
    ("+config|get",),
    ("+keys",),
    ("+acl|list",),
])
def test_probe_fails_when_the_user_reaches_beyond_its_namespace(
    tmp_path: Path, server: Server, grant: tuple[str, ...]
) -> None:
    server.cli("ACL", "SETUSER", "app", *grant)

    code, lines = run_probe(server, tmp_path, ["cache"])

    assert code == 1
    assert outcome(lines) == "GIMME_VALKEY_PROBE|namespace|failed|namespace_open"
    assert server.cli("DBSIZE") == "0"


@needs_server
def test_probe_reports_a_use_the_acl_does_not_permit(tmp_path: Path, server: Server) -> None:
    server.cli("ACL", "SETUSER", "app", "-zadd")

    code, lines = run_probe(server, tmp_path, ["queue"])

    assert code == 1
    assert outcome(lines) == "GIMME_VALKEY_PROBE|use-queue|failed|noperm"
    assert server.cli("DBSIZE") == "0"


@needs_server
@pytest.mark.parametrize(("framework", "horizon"), [
    ("v11.99.9", "v5.46.0"), ("v12.0.0", "v5.45.9"), ("dev-main", "v5.46.0"),
])
def test_probe_blocks_an_incompatible_horizon_before_touching_valkey(
    tmp_path: Path, shared: Server, framework: str, horizon: str
) -> None:
    lock = lockfile(tmp_path, framework, horizon)

    code, lines = run_probe(shared, tmp_path, ["cache", "queue"], lock=lock)

    assert code == 1
    assert outcome(lines) == "GIMME_VALKEY_PROBE|horizon-compatibility|failed|horizon_version"
    assert not any("|tls|" in line for line in lines)


@needs_server
def test_probe_accepts_newer_versions_and_requires_locked_packages(
    tmp_path: Path, shared: Server
) -> None:
    newer = lockfile(tmp_path, "v12.69.2", "v5.49.0")
    assert run_probe(shared, tmp_path, ["cache", "queue"], lock=newer)[0] == 0

    empty = tmp_path / "empty.lock"
    empty.write_text(json.dumps({"packages": []}))
    code, lines = run_probe(shared, tmp_path, ["cache", "queue"], lock=empty)
    assert (code, outcome(lines)) == (
        1, "GIMME_VALKEY_PROBE|horizon-compatibility|failed|horizon_package_missing"
    )

    broken = tmp_path / "broken.lock"
    broken.write_text("not json")
    assert outcome(run_probe(shared, tmp_path, ["cache", "queue"], lock=broken)[1]).endswith(
        "horizon_lock_unreadable"
    )


def test_locked_real_laravel_fixture_meets_the_horizon_probe_floor() -> None:
    lock = ROOT / "tests" / "integration" / "laravel-valkey" / "composer.lock"

    PROBE["check_horizon"](lock)  # type: ignore[operator]


@needs_server
@pytest.mark.parametrize("key", [
    "GIMME_VALKEY_HOST", "GIMME_VALKEY_PORT", "GIMME_VALKEY_USES", "GIMME_VALKEY_CONTRACT",
])
def test_probe_requires_the_environment_file_to_match_the_planned_contract(
    tmp_path: Path, shared: Server, key: str
) -> None:
    values = {**environment(shared, ["cache"]), key: "other"}

    code, lines = run_probe(shared, tmp_path, ["cache"], values=values)

    assert code == 1
    assert lines == ["GIMME_VALKEY_PROBE|environment|failed|environment_mismatch"]


def test_probe_follows_redirects_only_inside_the_endpoint_domain() -> None:
    session = PROBE["Session"]("clustercfg.shop.abc.cache.example.com", 6379, None, "u", "p")
    failure = PROBE["ProbeFailure"]
    two_labels = PROBE["Session"]("cache.com", 6379, None, "u", "p")
    with pytest.raises(failure):
        two_labels._redirect("MOVED 1 attacker.com:6379")

    assert session._redirect("MOVED 3999 node-0001.shop.abc.cache.example.com:6380") == (
        "node-0001.shop.abc.cache.example.com", 6380,
    )
    for message in (
        "MOVED 1 attacker.example.net:6379",
        "MOVED 1 10.0.0.5:6379",
        "MOVED 1 attacker.com:6379",
        "MOVED 1 evilshop.abc.cache.example.com.attacker.net:6379",
        "MOVED 1 node.shop.abc.cache.example.com:0",
        "MOVED 1 node.shop.abc.cache.example.com:notaport",
        "MOVED",
    ):
        with pytest.raises(failure) as caught:
            session._redirect(message)
        assert caught.value.code == "redirect_unverified"


def test_server_error_codes_are_reduced_to_a_fixed_token() -> None:
    error = PROBE["ServerError"]

    assert error("WRONGPASS invalid username-password pair").code == "wrongpass"
    assert error("secret-looking text: hunter2").code == "error"
    assert error("").code == "error"


class ScriptedSession:
    def __init__(self, reply: object) -> None:
        self.reply = reply

    def call(self, *arguments: object) -> object:
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_a_stale_read_after_write_fails() -> None:
    scope = PROBE["Scope"](ScriptedSession(b"stale"), "token")

    with pytest.raises(PROBE["ProbeFailure"]) as caught:  # type: ignore[call-overload]
        PROBE["check_read_after_write"](scope, "{gimme:x}:cache:")  # type: ignore[operator]

    assert caught.value.code == "read_after_write"


class PerCommandSession:
    def __init__(self, replies: dict[str, str], default: str) -> None:
        self.replies, self.default = replies, default

    def call(self, *arguments: object) -> object:
        raise PROBE["ServerError"](self.replies.get(str(arguments[0]), self.default))  # type: ignore[operator]


def test_a_provider_that_removes_config_still_proves_namespace_enforcement() -> None:
    # Live ElastiCache: CONFIG is answered "unknown command"; every other denial is NOPERM.
    noperm = "NOPERM User u has no permissions to run the command"
    removed = "ERR unknown command 'CONFIG', with args beginning with: 'GET'"
    session = PerCommandSession({"CONFIG": removed}, noperm)

    PROBE["check_namespace"](session, "token")  # type: ignore[operator]


def test_unknown_command_proves_nothing_for_any_command_other_than_config() -> None:
    session = PerCommandSession({}, "ERR unknown command 'GET'")

    with pytest.raises(PROBE["ProbeFailure"]) as caught:  # type: ignore[call-overload]
        PROBE["check_namespace"](session, "token")  # type: ignore[operator]

    assert caught.value.code == "namespace_unverified"


def test_a_denial_of_any_other_kind_does_not_prove_namespace_enforcement() -> None:
    session = ScriptedSession(PROBE["ServerError"]("WRONGTYPE Operation against a key"))  # type: ignore[operator]

    with pytest.raises(PROBE["ProbeFailure"]) as caught:  # type: ignore[call-overload]
        PROBE["check_namespace"](session, "token")  # type: ignore[operator]

    assert caught.value.code == "namespace_unverified"
