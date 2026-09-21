<?php

declare(strict_types=1);

namespace Deployer;

function laravel_candidate_health_script(): string
{
    return <<<'PHP'
$path = getenv('GIMME_HEALTH_PATH');
$host = getenv('GIMME_HEALTH_HOST');
$expected = filter_var(getenv('GIMME_HEALTH_EXPECTED'), FILTER_VALIDATE_INT);
try {
    require getcwd() . '/vendor/autoload.php';
    $app = require getcwd() . '/bootstrap/app.php';
    $kernel = $app->make(\Illuminate\Contracts\Http\Kernel::class);
    $request = \Illuminate\Http\Request::create(
        $path,
        'GET',
        [],
        [],
        [],
        ['HTTP_HOST' => $host, 'HTTPS' => 'on', 'SERVER_PORT' => 443]
    );
    $response = $kernel->handle($request);
    $status = $response->getStatusCode();
    $kernel->terminate($request, $response);
    fwrite(STDOUT, "GIMME_HEALTH_STATUS|{$status}\n");
    exit($status === $expected ? 0 : 1);
} catch (\Throwable) {
    fwrite(STDOUT, "GIMME_HEALTH_STATUS|exception\n");
    exit(1);
}
PHP;
}

function laravel_live_health_script(): string
{
    return <<<'PHP'
$url = getenv('GIMME_HEALTH_URL');
$host = getenv('GIMME_HEALTH_HOST');
$ca = getenv('GIMME_HEALTH_CA');
$expected = filter_var(getenv('GIMME_HEALTH_EXPECTED'), FILTER_VALIDATE_INT);
$timeout = filter_var(getenv('GIMME_HEALTH_TIMEOUT'), FILTER_VALIDATE_INT);
try {
    $handle = curl_init($url);
    if ($handle === false) {
        throw new \RuntimeException('curl initialization failed');
    }
    $options = [
        CURLOPT_CONNECTTIMEOUT => $timeout,
        CURLOPT_FOLLOWLOCATION => false,
        CURLOPT_HTTPHEADER => ["Host: {$host}"],
        CURLOPT_RESOLVE => ["{$host}:443:127.0.0.1"],
        CURLOPT_RETURNTRANSFER => false,
        CURLOPT_TIMEOUT => $timeout,
        CURLOPT_WRITEFUNCTION => static fn ($curl, string $body): int => strlen($body),
    ];
    if (is_string($ca) && $ca !== '' && is_file($ca)) {
        $options[CURLOPT_CAINFO] = $ca;
    }
    curl_setopt_array($handle, $options);
    $ok = curl_exec($handle);
    $status = curl_getinfo($handle, CURLINFO_RESPONSE_CODE);
    curl_close($handle);
    fwrite(STDOUT, "GIMME_HEALTH_STATUS|{$status}\n");
    exit($ok !== false && $status === $expected ? 0 : 1);
} catch (\Throwable) {
    fwrite(STDOUT, "GIMME_HEALTH_STATUS|exception\n");
    exit(1);
}
PHP;
}

function laravel_environment_reconcile_script(): string
{
    return <<<'PYTHON'
import base64
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile

path = Path(sys.argv[1])
updates = json.loads(base64.b64decode(sys.argv[2]).decode())
secret_argument = sys.argv[3] if len(sys.argv) > 3 else "-"
if secret_argument != "-":
    secret_path = Path(secret_argument)
    secret_details = secret_path.lstat()
    if secret_path.is_symlink() or not stat.S_ISREG(secret_details.st_mode):
        raise RuntimeError("refusing to read a non-regular secret document")
    secrets = json.loads(secret_path.read_text())
    if not isinstance(secrets, dict):
        raise RuntimeError("secret document must be an object")
    for key, value in secrets.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise RuntimeError("secret document contains an invalid value")
    updates.update(secrets)
manifest = (
    json.loads(base64.b64decode(sys.argv[4]).decode())
    if len(sys.argv) > 4 else []
)
if not isinstance(manifest, list) or len(manifest) > 128:
    raise RuntimeError("secret manifest must be a bounded list")
manifest_path = path.parent / ".gimme-secret-manifest.json"
details = path.lstat()
if path.is_symlink() or not stat.S_ISREG(details.st_mode):
    raise RuntimeError("refusing to reconcile a non-regular environment file")

runtime_keys = {"APP_ENV", "APP_DEBUG"}
seen = set()
rendered = []
runtime_changed = False
original = path.read_text()
for line in original.splitlines():
    key = line.split("=", 1)[0]
    if key not in updates:
        rendered.append(line)
        continue
    if key in seen:
        if key in runtime_keys:
            runtime_changed = True
        continue
    desired = f"{key}={updates[key]}"
    if line != desired and key in runtime_keys:
        runtime_changed = True
    rendered.append(desired)
    seen.add(key)

for key, value in updates.items():
    if key in seen:
        continue
    rendered.append(f"{key}={value}")
    if key in runtime_keys:
        runtime_changed = True

desired_content = "\n".join(rendered) + "\n"
manifest_content = json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
manifest_original = manifest_path.read_text() if manifest_path.is_file() else None
environment_changed = desired_content != original
if not environment_changed and manifest_content == manifest_original:
    print("GIMME_RUNTIME_CHANGED|no")
    print("GIMME_ENVIRONMENT_CHANGED|no")
    raise SystemExit(0)

descriptor, temporary = tempfile.mkstemp(prefix=".env.", dir=path.parent)
temporary_path = Path(temporary)
try:
    with os.fdopen(descriptor, "w") as handle:
        handle.write(desired_content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary_path, 0o600)
    os.chown(temporary_path, details.st_uid, details.st_gid)
    os.replace(temporary_path, path)
except BaseException:
    temporary_path.unlink(missing_ok=True)
    raise

manifest_descriptor, manifest_temporary = tempfile.mkstemp(
    prefix=".gimme-secret-manifest.", dir=path.parent
)
manifest_temporary_path = Path(manifest_temporary)
try:
    with os.fdopen(manifest_descriptor, "w") as handle:
        handle.write(manifest_content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(manifest_temporary_path, 0o600)
    os.chown(manifest_temporary_path, details.st_uid, details.st_gid)
    os.replace(manifest_temporary_path, manifest_path)
except BaseException:
    manifest_temporary_path.unlink(missing_ok=True)
    rollback_descriptor, rollback_temporary = tempfile.mkstemp(prefix=".env.rollback.", dir=path.parent)
    rollback_path = Path(rollback_temporary)
    try:
        with os.fdopen(rollback_descriptor, "w") as handle:
            handle.write(original)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(rollback_path, 0o600)
        os.chown(rollback_path, details.st_uid, details.st_gid)
        os.replace(rollback_path, path)
    except BaseException:
        rollback_path.unlink(missing_ok=True)
    raise

print("GIMME_RUNTIME_CHANGED|" + ("yes" if runtime_changed else "no"))
print("GIMME_ENVIRONMENT_CHANGED|" + ("yes" if environment_changed else "no"))
PYTHON;
}

function valkey_probe_script(): string
{
    return <<<'PYTHON'
import base64
import json
import random
import re
import secrets
import socket
import ssl
import stat
import sys
import time
from pathlib import Path

TIMEOUT = 3.0
MAX_BULK = 1 << 20
MAX_ITEMS = 4096
MAX_REDIRECTS = 2
CONNECT_ATTEMPTS = 3
KEY_TTL = 30
MINIMUMS = {"laravel/framework": (12, 0, 0), "laravel/horizon": (5, 46, 0)}


class ProbeFailure(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class ServerError(Exception):
    """A server error reply. Only its leading token is ever reported."""

    def __init__(self, message):
        super().__init__(message)
        self.message = message
        token = message.split(" ", 1)[0].lower()
        self.code = token if re.fullmatch(r"[a-z]{1,16}", token) else "error"


class Connection:
    def __init__(self, host, port, context):
        for attempt in range(CONNECT_ATTEMPTS):
            try:
                raw = socket.create_connection((host, port), timeout=TIMEOUT)
                break
            except OSError:
                if attempt + 1 == CONNECT_ATTEMPTS:
                    raise ProbeFailure("connect")
                time.sleep(0.1 * 2 ** attempt + random.uniform(0, 0.1))
        try:
            self.socket = context.wrap_socket(raw, server_hostname=host)
        except ssl.SSLCertVerificationError:
            raw.close()
            raise ProbeFailure("tls_verify")
        except (ssl.SSLError, OSError):
            raw.close()
            raise ProbeFailure("tls_handshake")
        self.reader = self.socket.makefile("rb")

    def call(self, *arguments):
        parts = [a if isinstance(a, bytes) else str(a).encode() for a in arguments]
        try:
            self.socket.sendall(
                b"*%d\r\n" % len(parts)
                + b"".join(b"$%d\r\n%s\r\n" % (len(part), part) for part in parts)
            )
            return self._read()
        except (OSError, ValueError):
            raise ProbeFailure("io")

    def _read(self):
        line = self.reader.readline(MAX_BULK)
        if not line.endswith(b"\r\n"):
            raise ProbeFailure("protocol")
        kind, rest = line[:1], line[1:-2]
        if kind == b"+":
            return rest
        if kind == b"-":
            raise ServerError(rest.decode("ascii", "replace"))
        if kind == b":":
            return int(rest)
        if kind == b"$":
            size = int(rest)
            if size < 0:
                return None
            if size > MAX_BULK:
                raise ProbeFailure("protocol")
            return self.reader.read(size + 2)[:-2]
        if kind == b"*":
            size = int(rest)
            if size > MAX_ITEMS:
                raise ProbeFailure("protocol")
            return None if size < 0 else [self._read() for _ in range(size)]
        raise ProbeFailure("protocol")

    def close(self):
        self.socket.close()


class Session:
    """One authenticated connection to the primary, following a bounded number of cluster
    redirects, and only to a host inside the configured endpoint's own domain."""

    def __init__(self, host, port, context, username, password):
        self.host, self.port, self.context = host, port, context
        self.username, self.password = username, password
        self.connection = None

    def open(self, host=None, port=None):
        self.connection = Connection(host or self.host, port or self.port, self.context)

    def login(self):
        try:
            self.connection.call("AUTH", self.username, self.password)
        except ServerError:
            raise ProbeFailure("auth")

    def call(self, *arguments):
        for _ in range(MAX_REDIRECTS + 1):
            try:
                return self.connection.call(*arguments)
            except ServerError as error:
                if error.code != "moved":
                    raise
                self.connection.close()
                self.open(*self._redirect(error.message))
                self.login()
        raise ProbeFailure("redirect_unverified")

    def _redirect(self, message):
        try:
            host, _, port = message.split()[2].rpartition(":")
            port = int(port)
        except (IndexError, ValueError):
            raise ProbeFailure("redirect_unverified")
        domain = self.host.partition(".")[2]
        # A shared domain of one label (".com") would match any host: require at least two.
        if not 0 < port < 65536 or not (
            host == self.host or ("." in domain and host.endswith("." + domain))
        ):
            raise ProbeFailure("redirect_unverified")
        return host, port


def read_environment(path):
    path = Path(path)
    details = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise ProbeFailure("environment")
    values = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def check_environment(config, values):
    expected = {
        "GIMME_VALKEY_CONTRACT": config["contract"],
        "GIMME_VALKEY_HOST": config["host"],
        "GIMME_VALKEY_PORT": str(config["port"]),
        "GIMME_VALKEY_USES": ",".join(config["uses"]),
    }
    if any(values.get(key) != value for key, value in expected.items()):
        raise ProbeFailure("environment_mismatch")
    for key in ("GIMME_VALKEY_USERNAME", "GIMME_VALKEY_PASSWORD"):
        if not values.get(key):
            raise ProbeFailure("environment_mismatch")


def version_tuple(value):
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(?:[.+-].*)?", value or "")
    return tuple(int(part) for part in match.groups()) if match else None


def check_horizon(lock_path):
    try:
        document = json.loads(Path(lock_path).read_text())
        locked = {
            package["name"]: package["version"] for package in document["packages"]
        }
    except (OSError, ValueError, KeyError, TypeError):
        raise ProbeFailure("horizon_lock_unreadable")
    for name, minimum in MINIMUMS.items():
        if name not in locked:
            raise ProbeFailure("horizon_package_missing")
        found = version_tuple(locked[name])
        if found is None or found < minimum:
            raise ProbeFailure("horizon_version")


def check_default_user(host, port, context):
    connection = Connection(host, port, context)
    try:
        connection.call("PING")
    except ServerError:
        return
    finally:
        connection.close()
    raise ProbeFailure("default_user_open")


def check_cluster(session):
    info = session.call("CLUSTER", "INFO")
    if b"cluster_state:ok" not in info.split():
        raise ProbeFailure("cluster_state")
    slots = session.call("CLUSTER", "SLOTS")
    if [(item[0], item[1]) for item in slots] != [(0, 16383)]:
        raise ProbeFailure("cluster_slots")


class Scope:
    """Short-lived probe keys inside one use's namespace, always removed afterwards."""

    def __init__(self, session, token):
        self.session, self.token, self.keys = session, token, []

    def key(self, prefix, name):
        key = f"{prefix}_probe:{self.token}:{name}"
        self.keys.append(key)
        return key

    def expiring(self, key):
        if self.session.call("EXPIRE", key, KEY_TTL * 2) != 1:
            raise ProbeFailure("expire")

    def remove(self):
        try:
            self.session.call("DEL", *self.keys)
        except (ProbeFailure, ServerError):
            pass


def expect(actual, expected, code):
    if actual != expected:
        raise ProbeFailure(code)


def check_read_after_write(scope, prefix):
    key = scope.key(prefix, "raw")
    scope.session.call("SET", key, "value", "EX", KEY_TTL)
    expect(scope.session.call("GET", key), b"value", "read_after_write")


def check_namespace(session, token):
    foreign = "{gimme:_isolation_%s}:probe" % token
    denied = (
        ("SET", foreign, "x", "EX", KEY_TTL),
        ("GET", foreign),
        ("PUBLISH", foreign, "x"),
        # Read-only on purpose: a destructive command is never sent, even to prove it is denied.
        ("CONFIG", "GET", "maxmemory"),
        ("KEYS", "gimme-probe-no-such-key-*"),
        ("SCAN", "0", "COUNT", "1"),
        ("ACL", "LIST"),
        ("CLIENT", "LIST"),
    )
    for command in denied:
        try:
            session.call(*command)
        except ServerError as error:
            if error.code != "noperm":
                raise ProbeFailure("namespace_unverified")
        else:
            try:
                session.call("DEL", foreign)
            except ServerError:
                pass
            raise ProbeFailure("namespace_open")


def use_cache(scope, prefix):
    key, counter, lock = (scope.key(prefix, name) for name in ("value", "counter", "lock"))
    call = scope.session.call
    call("SET", key, "value", "EX", KEY_TTL)
    expect(call("GET", key), b"value", "use_cache")
    expect(call("INCR", counter), 1, "use_cache")
    expect(call("EXPIRE", counter, KEY_TTL), 1, "use_cache")
    expect(call("SET", lock, "1", "NX", "EX", KEY_TTL), b"OK", "use_cache")
    expect(call("SET", lock, "2", "NX", "EX", KEY_TTL), None, "use_cache")
    if not 0 < call("TTL", key) <= KEY_TTL:
        raise ProbeFailure("use_cache")


def use_session(scope, prefix):
    key = scope.key(prefix, "session")
    call = scope.session.call
    call("SET", key, "payload", "EX", KEY_TTL)
    expect(call("GET", key), b"payload", "use_session")
    expect(call("EXPIRE", key, KEY_TTL), 1, "use_session")
    expect(call("DEL", key), 1, "use_session")
    expect(call("GET", key), None, "use_session")


def use_queue(scope, prefix):
    jobs, delayed = scope.key(prefix, "jobs"), scope.key(prefix, "delayed")
    call = scope.session.call
    expect(call("RPUSH", jobs, "job"), 1, "use_queue")
    scope.expiring(jobs)
    pop = "return redis.call('lpop', KEYS[1])"
    expect(call("EVAL", pop, 1, jobs), b"job", "use_queue")
    expect(call("ZADD", delayed, 1, "job"), 1, "use_queue")
    scope.expiring(delayed)
    expect(call("ZRANGEBYSCORE", delayed, "-inf", "+inf"), [b"job"], "use_queue")
    expect(call("ZREM", delayed, "job"), 1, "use_queue")


def use_horizon(scope, prefix):
    record, recent = scope.key(prefix, "job"), scope.key(prefix, "recent")
    call = scope.session.call
    expect(call("HSET", record, "status", "pending"), 1, "use_horizon")
    scope.expiring(record)
    expect(call("HGET", record, "status"), b"pending", "use_horizon")
    expect(call("ZADD", recent, 1, "job"), 1, "use_horizon")
    scope.expiring(recent)
    expect(call("ZRANGE", recent, 0, -1), [b"job"], "use_horizon")


USES = {
    "cache": use_cache, "session": use_session, "queue": use_queue, "horizon": use_horizon,
}


def steps(config, values, lock_path, context, session, scope):
    prefixes = config["prefixes"]
    first = prefixes[config["uses"][0]]
    horizon = config["horizon"] is True
    yield "environment", lambda: check_environment(config, values)
    if horizon:
        yield "horizon-compatibility", lambda: check_horizon(lock_path)
    yield "tls", session.open
    yield "auth", session.login
    yield "default-user", lambda: check_default_user(config["host"], config["port"], context)
    yield "cluster", lambda: check_cluster(session)
    yield "read-after-write", lambda: check_read_after_write(scope, first)
    yield "namespace", lambda: check_namespace(session, scope.token)
    for use in config["uses"] + (["horizon"] if horizon else []):
        yield f"use-{use}", lambda use=use: USES[use](scope, prefixes[use])
    yield "cleanup", scope.remove


def probe(config, values, lock_path, context, emit=print):
    """Run every check in order, stopping at the first failure. Prints only fixed names and
    codes; credentials, values, and server messages are never echoed."""
    session = Session(
        config["host"], config["port"], context,
        values.get("GIMME_VALKEY_USERNAME", ""), values.get("GIMME_VALKEY_PASSWORD", ""),
    )
    scope = Scope(session, secrets.token_hex(6))
    try:
        for name, step in steps(config, values, lock_path, context, session, scope):
            try:
                step()
            except ProbeFailure as failure:
                emit(f"GIMME_VALKEY_PROBE|{name}|failed|{failure.code}")
                return 1
            except ServerError as error:
                emit(f"GIMME_VALKEY_PROBE|{name}|failed|{error.code}")
                return 1
            except Exception:
                emit(f"GIMME_VALKEY_PROBE|{name}|failed|unexpected")
                return 1
            emit(f"GIMME_VALKEY_PROBE|{name}|ready")
        return 0
    finally:
        if session.connection is not None:
            scope.remove()
            session.connection.close()


def main(arguments):
    config = json.loads(base64.b64decode(arguments[3]).decode())
    try:
        values = read_environment(arguments[1])
    except (OSError, ValueError, ProbeFailure):
        print("GIMME_VALKEY_PROBE|environment|failed|environment")
        return 1
    return probe(config, values, arguments[2], ssl.create_default_context())


if __name__ == "__main__":
    sys.exit(main(sys.argv))
PYTHON;
}

function artifact_store_probe_script(): string
{
    return <<<'PYTHON'
import base64
import hashlib
import json
import re
import secrets
import stat
import sys
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

def safe_exception_hook(_kind, _error, _traceback):
    print("GIMME_ARTIFACT_STORE_ERROR|verification_failed", file=sys.stderr)

sys.excepthook = safe_exception_hook

policy = json.loads(base64.b64decode(sys.argv[1], validate=True))
role, version, credential_argument = sys.argv[2:5]
if set(policy) != {"name", "bucket", "region", "endpoint", "addressing", "encryption"}:
    raise SystemExit("artifact store policy has an unexpected shape")
if re.fullmatch(r"[a-z][a-z0-9-]{0,63}", policy["name"]) is None:
    raise SystemExit("artifact store name is invalid")
if role not in {"publisher", "reader"}:
    raise SystemExit("artifact probe role is invalid")

client_options = {
    "region_name": policy["region"],
    "config": Config(
        signature_version="s3v4",
        s3={"addressing_style": (
            "virtual" if policy["addressing"] == "virtual_hosted" else "path"
        )},
        retries={"max_attempts": 3, "mode": "standard"},
    ),
}
if policy["endpoint"] is not None:
    client_options["endpoint_url"] = "https://" + policy["endpoint"]
if credential_argument != "-":
    path = Path(credential_argument)
    details = path.lstat()
    if path.is_symlink() or not stat.S_ISREG(details.st_mode):
        raise SystemExit("refusing to read a non-regular credential document")
    credentials = json.loads(path.read_text())
    allowed = {"access_key_id", "secret_access_key", "session_token"}
    if not isinstance(credentials, dict) or not set(credentials) <= allowed or not {
        "access_key_id", "secret_access_key"
    } <= set(credentials):
        raise SystemExit("credential document has an unexpected shape")
    if any(not isinstance(value, str) or not value or len(value) > 4096
           for value in credentials.values()):
        raise SystemExit("credential document contains an invalid value")
    client_options.update({
        "aws_access_key_id": credentials["access_key_id"],
        "aws_secret_access_key": credentials["secret_access_key"],
    })
    if "session_token" in credentials:
        client_options["aws_session_token"] = credentials["session_token"]

client = boto3.client("s3", **client_options)
bucket = policy["bucket"]
prefix = "gimme/artifact-store-capabilities/" + hashlib.sha256(
    policy["name"].encode()
).hexdigest()[:20]
reader_key = prefix + "/reader-v1.check"
reader_body = b"gimme-artifact-reader-capability-v1\n"
reader_sha256 = hashlib.sha256(reader_body).hexdigest()

def encryption_arguments():
    encryption = policy["encryption"]
    if encryption["method"] == "aes256":
        return {"ServerSideEncryption": "AES256"}, "AES256"
    return {
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": encryption["kms_key_arn"],
    }, "aws:kms"

def encryption_confirmed(response, expected):
    if response.get("ServerSideEncryption") != expected:
        return False
    if expected == "aws:kms":
        return response.get("SSEKMSKeyId") == policy["encryption"]["kms_key_arn"]
    return True

def exact_body(key, object_version):
    response = client.get_object(Bucket=bucket, Key=key, VersionId=object_version)
    try:
        return response["Body"].read()
    finally:
        response["Body"].close()

if role == "reader":
    if re.fullmatch(r"[A-Za-z0-9._+=/-]{1,1024}", version) is None:
        raise SystemExit("reader object version is invalid")
    body = exact_body(reader_key, version)
    if hashlib.sha256(body).hexdigest() != reader_sha256:
        raise SystemExit("reader capability checksum mismatch")
    result = {
        "status": "ready",
        "role": "reader",
        "versioning": "exact-version-read",
        "checksum": reader_sha256,
    }
else:
    versioning = client.get_bucket_versioning(Bucket=bucket).get("Status")
    if versioning != "Enabled":
        raise SystemExit("artifact store bucket versioning is not enabled")
    encryption, expected_encryption = encryption_arguments()
    probe_key = prefix + "/publisher/" + secrets.token_hex(16) + ".check"
    probe_body = secrets.token_bytes(64)
    probe_sha256 = hashlib.sha256(probe_body).hexdigest()
    probe_version = None
    try:
        response = client.put_object(
            Bucket=bucket, Key=probe_key, Body=probe_body,
            Metadata={"gimme-sha256": probe_sha256},
            **encryption,
        )
        probe_version = response.get("VersionId")
        if not isinstance(probe_version, str) or not probe_version:
            raise RuntimeError("artifact store did not return a probe object version")
        if not encryption_confirmed(response, expected_encryption):
            raise RuntimeError("artifact store did not confirm required encryption")
        if hashlib.sha256(exact_body(probe_key, probe_version)).hexdigest() != probe_sha256:
            raise RuntimeError("publisher capability checksum mismatch")
    finally:
        if probe_version is not None:
            client.delete_object(Bucket=bucket, Key=probe_key, VersionId=probe_version)
    try:
        client.head_object(Bucket=bucket, Key=probe_key, VersionId=probe_version)
    except ClientError as error:
        if error.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
            raise
    else:
        raise RuntimeError("publisher probe version still exists after exact-version delete")
    reader_version = None
    try:
        reader_response = client.put_object(
            Bucket=bucket, Key=reader_key, Body=reader_body,
            Metadata={"gimme-sha256": reader_sha256},
            **encryption,
        )
        reader_version = reader_response.get("VersionId")
        if not isinstance(reader_version, str) or not reader_version:
            raise RuntimeError("artifact store did not return a reader object version")
        if not encryption_confirmed(reader_response, expected_encryption):
            raise RuntimeError("artifact store did not encrypt the reader capability object")
    except Exception:
        if reader_version is not None:
            client.delete_object(Bucket=bucket, Key=reader_key, VersionId=reader_version)
        raise
    result = {
        "status": "ready",
        "role": "publisher",
        "versioning": "enabled",
        "encryption": policy["encryption"]["method"],
        "checksum": probe_sha256,
        "probe_deleted": True,
        "reader_version": reader_version,
    }

encoded = base64.b64encode(json.dumps(result, sort_keys=True).encode()).decode()
print("GIMME_ARTIFACT_STORE_RESULT|" + encoded)
PYTHON;
}


function managed_postgres_bind_script(): string
{
    return <<<'PYTHON'
import hashlib
import json
import os
import re
import stat
import subprocess  # nosec B404
import sys
from pathlib import Path

(
    host, port, database, owner, login, extensions_json,
    secret_path_argument, bundle_path_argument, bundle_digest,
) = sys.argv[1:10]
if not (1 <= int(port) <= 65535):
    raise SystemExit("unsafe port")
if re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database) is None:
    raise SystemExit("unsafe database identifier")
if any(re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) is None for value in (owner, login)):
    raise SystemExit("unsafe role identifier")
extensions = json.loads(extensions_json)
if (
    not isinstance(extensions, dict)
    or any(name not in {"pgcrypto", "uuid-ossp", "citext"} for name in extensions)
    or any(
        not isinstance(version, str)
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", version) is None
        for version in extensions.values()
    )
):
    raise SystemExit("unsafe PostgreSQL extension policy")
if re.fullmatch(r"[0-9a-f]{64}", bundle_digest) is None:
    raise SystemExit("unsafe trust bundle digest")

secret_path = Path(secret_path_argument)
details = secret_path.lstat()
if secret_path.is_symlink() or not stat.S_ISREG(details.st_mode):
    raise SystemExit("refusing to read a non-regular secret document")
bundle_path = Path(bundle_path_argument)
if bundle_path.is_symlink() or not stat.S_ISREG(bundle_path.lstat().st_mode):
    raise SystemExit("refusing to read a non-regular trust bundle")
if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != bundle_digest:
    raise SystemExit("trust bundle digest mismatch")
secrets = json.loads(secret_path.read_text())
if not isinstance(secrets, dict) or set(secrets) != {
    "master_username", "master_password", "workload_username", "workload_password",
}:
    raise SystemExit("secret document has an unexpected shape")
for value in secrets.values():
    if not isinstance(value, str) or not value:
        raise SystemExit("secret document contains an invalid value")
# Only these characters can be embedded in a psql \set line without any quoting concern.
if re.fullmatch(r"[A-Za-z0-9_-]{8,128}", secrets["workload_password"]) is None:
    raise SystemExit("workload password has an unexpected format")
if secrets["workload_username"] != login:
    raise SystemExit("workload login does not match the reviewed identity")

# The role name matches the database identifier, exactly like target-local PostgreSQL.
env = {
    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
    "PGPASSWORD": secrets["master_password"],
    "PGSSLMODE": "verify-full",
    "PGSSLROOTCERT": str(bundle_path),
}


def redacted(text: str) -> str:
    for value in (secrets["master_password"], secrets["workload_password"]):
        text = text.replace(value, "[redacted]")
    return text


TLS_FAILURE = re.compile(r"certificate verify failed|does not match host name", re.IGNORECASE)


def fail(result: subprocess.CompletedProcess[str], message: str) -> None:
    # A verification failure never echoes psql output: it names the endpoint and certificate.
    if TLS_FAILURE.search(result.stdout):
        raise SystemExit("managed PostgreSQL TLS certificate verification failed")
    print(redacted(result.stdout))
    raise SystemExit(message)


def psql(statements: str, selected_database: str = "postgres") -> subprocess.CompletedProcess[str]:
    # Statements go over stdin so nothing secret ever appears in argv. psql's :'var'
    # substitution is applied client-side (and quotes each value as a SQL string literal)
    # only for stdin/-f input, never for -c, and never inside a dollar-quoted body.
    return subprocess.run(  # nosec B603
        [
            "psql", "-h", host, "-p", port, "-U", secrets["master_username"],
            "-d", selected_database,
            "--no-psqlrc", "-v", "ON_ERROR_STOP=1",
        ],
        input=statements, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, timeout=60, check=False,
    )


# CREATE ROLE and CREATE DATABASE are generated by format(%I/%L) and run with \gexec only
# when their guard yields a row: idempotent, injection-safe, and CREATE DATABASE stays
# outside any transaction block.
role_result = psql(
    f"\\set owner '{owner}'\n"
    f"\\set login '{login}'\n"
    f"\\set owner_marker 'gimme:managed-postgres:{database}:owner'\n"
    f"\\set login_marker 'gimme:managed-postgres:{database}:login'\n"
    f"\\set workload_password '{secrets['workload_password']}'\n"
    "SELECT (NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner') "
    "OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner' AND "
    "shobj_description(oid, 'pg_authid') = :'owner_marker')) AS owner_valid \\gset\n"
    "\\if :owner_valid\n\\else\n\\quit 1\n\\endif\n"
    "SELECT (NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'login') "
    "OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'login' AND "
    "shobj_description(oid, 'pg_authid') = :'login_marker')) AS login_valid \\gset\n"
    "\\if :login_valid\n\\else\n\\quit 1\n\\endif\n"
    "SELECT format('CREATE ROLE %I NOLOGIN', :'owner') "
    "WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner') \\gexec\n"
    "SELECT format('CREATE ROLE %I LOGIN NOCREATEDB NOCREATEROLE NOREPLICATION', :'login') "
    "WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'login') \\gexec\n"
    "SELECT format('COMMENT ON ROLE %I IS %L', :'owner', :'owner_marker') \\gexec\n"
    "SELECT format('COMMENT ON ROLE %I IS %L', :'login', :'login_marker') \\gexec\n"
    "SELECT format('ALTER ROLE %I NOLOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE "
    "NOREPLICATION NOBYPASSRLS', :'owner') \\gexec\n"
    "SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER INHERIT NOCREATEDB NOCREATEROLE "
    "NOREPLICATION NOBYPASSRLS PASSWORD %L', :'login', :'workload_password') \\gexec\n"
    "SELECT format('REVOKE %I FROM %I', parent.rolname, :'owner') "
    "FROM pg_auth_members membership JOIN pg_roles member ON member.oid = membership.member "
    "JOIN pg_roles parent ON parent.oid = membership.roleid "
    "WHERE member.rolname = :'owner' \\gexec\n"
    "SELECT format('REVOKE %I FROM %I', parent.rolname, :'login') "
    "FROM pg_auth_members membership JOIN pg_roles member ON member.oid = membership.member "
    "JOIN pg_roles parent ON parent.oid = membership.roleid "
    "WHERE member.rolname = :'login' \\gexec\n"
    "SELECT format('GRANT %I TO %I', :'owner', :'login') \\gexec\n"
)
if role_result.returncode != 0:
    fail(role_result, "managed PostgreSQL role reconciliation failed")

database_result = psql(
    f"\\set owner '{owner}'\n"
    f"\\set login '{login}'\n"
    f"\\set database '{database}'\n"
    f"\\set database_marker 'gimme:managed-postgres:{database}:database'\n"
    "SELECT (NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database') "
    "OR EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database' "
    "AND pg_get_userbyid(datdba) = :'owner' "
    "AND shobj_description(oid, 'pg_database') = :'database_marker') "
    ") AS database_valid \\gset\n"
    "\\if :database_valid\n\\else\n\\quit 1\n\\endif\n"
    "SELECT format('CREATE DATABASE %I OWNER %I', :'database', :'owner') "
    "WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database') \\gexec\n"
    "SELECT format('COMMENT ON DATABASE %I IS %L', :'database', :'database_marker') \\gexec\n"
    "SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'database') \\gexec\n"
    "SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'database', :'login') \\gexec\n"
)
if database_result.returncode != 0:
    fail(database_result, "managed PostgreSQL database reconciliation failed")

extension_sql = (
    f"\\set owner '{owner}'\n"
    "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n"
    "SELECT format('ALTER SCHEMA public OWNER TO %I', :'owner') \\gexec\n"
)
for extension, version in sorted(extensions.items()):
    extension_sql += (
        f"\\set extension '{extension}'\n"
        f"\\set extension_version '{version}'\n"
        "SELECT format('CREATE EXTENSION IF NOT EXISTS %I VERSION %L', "
        ":'extension', :'extension_version') \\gexec\n"
        "SELECT format('ALTER EXTENSION %I UPDATE TO %L', :'extension', :'extension_version') "
        "WHERE EXISTS (SELECT 1 FROM pg_extension WHERE extname = :'extension' "
        "AND extversion <> :'extension_version') \\gexec\n"
        "SELECT EXISTS (SELECT 1 FROM pg_extension "
        "WHERE extname = :'extension' AND extversion = :'extension_version') "
        "AS extension_valid \\gset\n"
        "\\if :extension_valid\n\\else\n\\quit 1\n\\endif\n"
    )
extension_result = psql(extension_sql, database)
if extension_result.returncode != 0:
    fail(extension_result, "managed PostgreSQL extension reconciliation failed")

print("GIMME_RESOURCE_BOUND|" + database)
PYTHON;
}


function managed_postgres_verify_script(): string
{
    return <<<'PYTHON'
import base64
import hashlib
import json
import os
import re
import stat
import subprocess  # nosec B404
import sys
from pathlib import Path

host, port, secret_argument, bundle_argument, expected_digest = sys.argv[1:6]
if not 1 <= int(port) <= 65535 or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
    raise SystemExit("unsafe managed PostgreSQL verification input")
secret_path, bundle_path = Path(secret_argument), Path(bundle_argument)
for path in (secret_path, bundle_path):
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise SystemExit("unsafe managed PostgreSQL verification file")
if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != expected_digest:
    raise SystemExit("trust bundle digest mismatch")
secret = json.loads(secret_path.read_text())
if not isinstance(secret, dict) or set(secret) != {"username", "password"}:
    raise SystemExit("secret document has an unexpected shape")
if any(not isinstance(value, str) or not value for value in secret.values()):
    raise SystemExit("secret document contains an invalid value")
result = subprocess.run(  # nosec B603
    ["psql", "-h", host, "-p", port, "-U", secret["username"], "-d", "postgres",
     "--no-psqlrc", "-v", "ON_ERROR_STOP=1", "-At"],
    input=("SHOW ssl; SELECT 1; "
           "SELECT name || '=' || default_version FROM pg_available_extensions "
           "WHERE name IN ('pgcrypto','uuid-ossp','citext') ORDER BY name;\n"),
    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PGPASSWORD": secret["password"],
         "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(bundle_path)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60, check=False,
)
lines = result.stdout.splitlines()
if result.returncode != 0 or lines[:2] != ["on", "1"]:
    raise SystemExit("managed PostgreSQL TLS administration verification failed")
extensions = {}
for line in lines[2:]:
    name, separator, version = line.partition("=")
    if (
        separator != "="
        or name not in {"pgcrypto", "uuid-ossp", "citext"}
        or re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", version) is None
        or name in extensions
    ):
        raise SystemExit("managed PostgreSQL extension inventory invalid")
    extensions[name] = version
encoded = base64.b64encode(json.dumps(extensions, sort_keys=True).encode()).decode()
print("GIMME_RESOURCE_VERIFIED|postgres|" + encoded)
PYTHON;
}


function managed_postgres_retire_login_script(): string
{
    return <<<'PYTHON'
import hashlib
import json
import os
import re
import stat
import subprocess  # nosec B404
import sys
from pathlib import Path

host, port, login, secret_argument, bundle_argument, expected_digest = sys.argv[1:7]
if (
    not 1 <= int(port) <= 65535
    or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", login) is None
    or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
):
    raise SystemExit("unsafe managed PostgreSQL retirement input")
secret_path, bundle_path = Path(secret_argument), Path(bundle_argument)
for path in (secret_path, bundle_path):
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise SystemExit("unsafe managed PostgreSQL retirement file")
if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != expected_digest:
    raise SystemExit("trust bundle digest mismatch")
secret = json.loads(secret_path.read_text())
if not isinstance(secret, dict) or set(secret) != {"username", "password"}:
    raise SystemExit("secret document has an unexpected shape")
result = subprocess.run(  # nosec B603
    ["psql", "-h", host, "-p", port, "-U", secret["username"], "-d", "postgres",
     "--no-psqlrc", "-v", "ON_ERROR_STOP=1"],
    input=(f"\\set login '{login}'\n"
           "SELECT format('REVOKE CONNECT ON DATABASE %I FROM %I', datname, :'login') "
           "FROM pg_database WHERE datallowconn AND NOT datistemplate \\gexec\n"
           "SELECT format('DROP ROLE IF EXISTS %I', :'login') \\gexec\n"),
    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PGPASSWORD": secret["password"],
         "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(bundle_path)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=60, check=False,
)
if result.returncode != 0:
    raise SystemExit("managed PostgreSQL login retirement failed")
print("GIMME_RESOURCE_LOGIN_RETIRED|postgres")
PYTHON;
}


function managed_postgres_purge_allocation_script(): string
{
    return <<<'PYTHON'
import hashlib
import json
import os
import re
import stat
import subprocess  # nosec B404
import sys
from pathlib import Path

(
    host, port, database, owner, login, secret_argument, bundle_argument, expected_digest,
) = sys.argv[1:9]
if (
    not 1 <= int(port) <= 65535
    or any(
        re.fullmatch(r"[a-z][a-z0-9_]{0,62}", value) is None
        for value in (database, owner, login)
    )
    or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
):
    raise SystemExit("unsafe managed PostgreSQL allocation purge input")
secret_path, bundle_path = Path(secret_argument), Path(bundle_argument)
for path in (secret_path, bundle_path):
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise SystemExit("unsafe managed PostgreSQL allocation purge file")
if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != expected_digest:
    raise SystemExit("trust bundle digest mismatch")
secret = json.loads(secret_path.read_text())
if not isinstance(secret, dict) or set(secret) != {"username", "password"}:
    raise SystemExit("secret document has an unexpected shape")
if any(not isinstance(value, str) or not value for value in secret.values()):
    raise SystemExit("secret document contains an invalid value")
statements = (
    f"\\set database '{database}'\n"
    f"\\set owner '{owner}'\n"
    f"\\set login '{login}'\n"
    f"\\set database_marker 'gimme:managed-postgres:{database}:database'\n"
    f"\\set owner_marker 'gimme:managed-postgres:{database}:owner'\n"
    f"\\set login_marker 'gimme:managed-postgres:{database}:login'\n"
    "SELECT (NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database') "
    "OR EXISTS (SELECT 1 FROM pg_database WHERE datname = :'database' "
    "AND pg_get_userbyid(datdba) = :'owner' "
    "AND shobj_description(oid, 'pg_database') = :'database_marker') "
    ") AS database_valid \\gset\n"
    "\\if :database_valid\n\\else\n\\quit 1\n\\endif\n"
    "SELECT (NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner') "
    "OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'owner' AND "
    "shobj_description(oid, 'pg_authid') = :'owner_marker')) AS owner_valid \\gset\n"
    "\\if :owner_valid\n\\else\n\\quit 1\n\\endif\n"
    "SELECT (NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'login') "
    "OR EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'login' AND "
    "shobj_description(oid, 'pg_authid') = :'login_marker')) AS login_valid \\gset\n"
    "\\if :login_valid\n\\else\n\\quit 1\n\\endif\n"
    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
    "WHERE datname = :'database' AND pid <> pg_backend_pid();\n"
    "SELECT format('DROP DATABASE IF EXISTS %I', :'database') \\gexec\n"
    "SELECT format('REVOKE %I FROM %I', :'owner', :'login') "
    "WHERE EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'login') \\gexec\n"
    "SELECT format('DROP ROLE IF EXISTS %I', :'login') \\gexec\n"
    "SELECT format('DROP ROLE IF EXISTS %I', :'owner') \\gexec\n"
)
result = subprocess.run(  # nosec B603
    ["psql", "-h", host, "-p", port, "-U", secret["username"], "-d", "postgres",
     "--no-psqlrc", "-v", "ON_ERROR_STOP=1"],
    input=statements,
    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PGPASSWORD": secret["password"],
         "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(bundle_path)},
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120, check=False,
)
if result.returncode != 0:
    raise SystemExit("managed PostgreSQL allocation purge failed")
print("GIMME_RESOURCE_ALLOCATION_PURGED|postgres|" + database)
PYTHON;
}


function managed_postgres_dump_script(): string
{
    return <<<'PYTHON'
import hashlib
import json
import os
import re
import stat
import subprocess  # nosec B404
import sys
from pathlib import Path

(
    host, port, database, secret_argument, bundle_argument, expected_digest, output_argument,
) = sys.argv[1:8]
if (
    not 1 <= int(port) <= 65535
    or re.fullmatch(r"[a-z][a-z0-9_]{0,62}", database) is None
    or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
):
    raise SystemExit("unsafe managed PostgreSQL backup identity")
secret_path, bundle_path, output_path = map(
    Path, (secret_argument, bundle_argument, output_argument)
)
for path in (secret_path, bundle_path):
    if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
        raise SystemExit("unsafe managed PostgreSQL backup input")
if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != expected_digest:
    raise SystemExit("trust bundle digest mismatch")
secret = json.loads(secret_path.read_text())
if not isinstance(secret, dict) or set(secret) != {"username", "password"}:
    raise SystemExit("secret document has an unexpected shape")
if any(not isinstance(value, str) or not value for value in secret.values()):
    raise SystemExit("secret document contains an invalid value")
result = subprocess.run(  # nosec B603
    ["pg_dump", "--format=custom", "--no-owner", "--no-privileges", "--no-acl",
     "-h", host, "-p", port, "-U", secret["username"], "-d", database,
     "-f", str(output_path)],
    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PGPASSWORD": secret["password"],
         "PGSSLMODE": "verify-full", "PGSSLROOTCERT": str(bundle_path)},
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    text=True, timeout=1800, check=False,
)
if result.returncode != 0:
    output_path.unlink(missing_ok=True)
    raise SystemExit("managed PostgreSQL backup capture failed")
PYTHON;
}

function assert_laravel_configuration_health(
    array $health,
    string $siteHost,
    string $appsRoot,
): void {
    foreach ($health as $probe) {
        if (!in_array('live', $probe['phases'], true)) {
            continue;
        }
        $expected = $probe['expected_status'];
        $command =
            'GIMME_HEALTH_URL=' . escapeshellarg("https://{$siteHost}{$probe['path']}") . ' ' .
            'GIMME_HEALTH_HOST=' . escapeshellarg($siteHost) . ' ' .
            'GIMME_HEALTH_CA=' . escapeshellarg("{$appsRoot}/.caddy-local-root.crt") . ' ' .
            'GIMME_HEALTH_EXPECTED=' . escapeshellarg((string) $expected) . ' ' .
            'GIMME_HEALTH_TIMEOUT=' . escapeshellarg((string) $probe['timeout_seconds']) . ' ' .
            '{{bin/php}} -d display_errors=0 -r %health_script% 2>/dev/null || true';
        for ($attempt = 1; $attempt <= $probe['attempts']; $attempt++) {
            $output = run($command, secrets: [
                'health_script' => escapeshellarg(laravel_live_health_script()),
            ]);
            if (trim($output) === "GIMME_HEALTH_STATUS|{$expected}") {
                continue 2;
            }
            if ($attempt < $probe['attempts'] && $probe['delay_seconds'] > 0) {
                run('/usr/bin/sleep ' . escapeshellarg((string) $probe['delay_seconds']));
            }
        }
        throw new \RuntimeException('Configuration health verification failed');
    }
}
