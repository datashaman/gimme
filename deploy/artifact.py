from __future__ import annotations

import base64
import gzip
import hashlib
import json
import os
import platform
import re
import shutil
import signal
import stat
# Commands below use fixed executable vectors and validated values.
import subprocess  # nosec B404
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import ClientError
except ImportError:
    print("GIMME_ARTIFACT_ERROR|artifact_runtime_dependency_missing", file=sys.stderr)
    raise SystemExit(1) from None


BUILD_ID = re.compile(r"^build_v1_[0-9a-f]{64}$")
COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][A-Za-z0-9.-]+)?$")
VERSION_ID = re.compile(r"^[A-Za-z0-9._+=/-]{1,1024}$")
PUBLISHED_AT = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\+00:00$")
MAX_LOCK_BYTES = 4 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TREE_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_FILES = 100_000
MAX_MANIFEST_BYTES = 64 * 1024
MIN_FREE_BYTES = 1024 * 1024 * 1024
LOCKFILES = {
    "npm": ("package-lock.json",),
    "pnpm": ("pnpm-lock.yaml",),
    "yarn": ("yarn.lock",),
    "bun": ("bun.lock", "bun.lockb"),
}
active_process: subprocess.Popen[bytes] | None = None


class ArtifactFailure(RuntimeError):
    pass


def fail(code: str) -> None:
    raise ArtifactFailure(code)


def safe_exception_hook(_kind, error, _traceback) -> None:
    code = str(error) if isinstance(error, ArtifactFailure) else "artifact_operation_failed"
    if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code) is None:
        code = "artifact_operation_failed"
    print(f"GIMME_ARTIFACT_ERROR|{code}", file=sys.stderr)


sys.excepthook = safe_exception_hook


def interrupted(_signal, _frame) -> None:
    if active_process is not None:
        with suppress_os_error():
            os.killpg(active_process.pid, signal.SIGTERM)
    raise KeyboardInterrupt


class suppress_os_error:
    def __enter__(self):
        return self

    def __exit__(self, kind, _error, _traceback):
        return kind is not None and issubclass(kind, OSError)


def emit(value: dict[str, object]) -> None:
    encoded = base64.b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    print("GIMME_ARTIFACT_RESULT|" + encoded)


def safe_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    selected = {
        name: os.environ[name]
        for name in ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "SSH_AUTH_SOCK")
        if name in os.environ
    }
    selected.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    selected.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=accept-new",
    })
    if extra:
        selected.update(extra)
    return selected


def command(
    arguments: list[str],
    *,
    cwd: Path | None = None,
    environment: dict[str, str] | None = None,
    timeout: int = 300,
) -> bytes:
    global active_process
    try:
        active_process = subprocess.Popen(  # nosec B603
            arguments,
            cwd=cwd,
            env=safe_environment(environment),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            output, _ = active_process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(active_process.pid, signal.SIGKILL)
            active_process.communicate()
            fail("build_command_timeout")
        if active_process.returncode != 0:
            fail("build_command_failed")
        return output
    finally:
        if active_process is not None:
            with suppress_os_error():
                os.killpg(active_process.pid, signal.SIGTERM)
        active_process = None


def validate_request(value: object, expected: set[str]) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != expected:
        fail("artifact_request_invalid")
    return value


def parse_request(argument: str) -> dict[str, object]:
    try:
        decoded = base64.b64decode(argument, validate=True)
        value = json.loads(decoded)
    except (ValueError, json.JSONDecodeError):
        fail("artifact_request_invalid")
    if not isinstance(value, dict):
        fail("artifact_request_invalid")
    return value


def checked_root(argument: str) -> Path:
    root = Path(argument)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or not root.is_dir()
        or root.is_symlink()
        or root.stat().st_uid != os.getuid()
    ):
        fail("artifact_workspace_root_invalid")
    workspace_root = root / ".gimme" / "artifact-builds"
    workspace_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(workspace_root, 0o700)
    if workspace_root.stat().st_uid != os.getuid():
        fail("artifact_workspace_root_invalid")
    return workspace_root


def repository_environment(workspace_root: Path) -> dict[str, str]:
    return {"GIT_CEILING_DIRECTORIES": str(workspace_root.parent)}


def clone_exact(request: dict[str, object], workspace_root: Path) -> tuple[Path, list[str]]:
    repository = request.get("repository")
    commit = request.get("commit")
    if (
        not isinstance(repository, str)
        or not isinstance(commit, str)
        or COMMIT.fullmatch(commit) is None
    ):
        fail("source_policy_invalid")
    if shutil.disk_usage(workspace_root).free < MIN_FREE_BYTES:
        fail("build_space_insufficient")
    workspace = Path(tempfile.mkdtemp(prefix="build-", dir=workspace_root))
    os.chmod(workspace, 0o700)
    try:
        tracked = checkout_exact(request, workspace_root, workspace)
    except BaseException:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return workspace, tracked


def checkout_exact(
    request: dict[str, object], workspace_root: Path, workspace: Path
) -> list[str]:
    repository = request["repository"]
    commit = request["commit"]
    source = workspace / "source"
    source.mkdir(mode=0o700)
    environment = repository_environment(workspace_root)
    command(["git", "init", "--quiet", str(source)], environment=environment, timeout=60)
    command(["git", "-C", str(source), "remote", "add", "origin", repository],
            environment=environment, timeout=60)
    command([
        "git", "-C", str(source), "fetch", "--quiet", "--depth=1", "--no-tags",
        "origin", commit,
    ], environment=environment, timeout=300)
    command(["git", "-C", str(source), "checkout", "--quiet", "--detach", "FETCH_HEAD"],
            environment=environment, timeout=60)
    actual = command(["git", "-C", str(source), "rev-parse", "HEAD"],
                     environment=environment, timeout=30).decode().strip()
    remote = command(["git", "-C", str(source), "remote", "get-url", "origin"],
                     environment=environment, timeout=30).decode().strip()
    if actual != commit or remote != repository:
        fail("repository_substitution_detected")
    if (source / ".git" / "objects" / "info" / "alternates").exists():
        fail("alternate_object_database_forbidden")
    raw_paths = command(["git", "-C", str(source), "ls-files", "-z"],
                        environment=environment, timeout=60)
    try:
        tracked = [item.decode() for item in raw_paths.split(b"\0") if item]
    except UnicodeDecodeError:
        fail("tracked_path_invalid")
    if not tracked or len(tracked) > MAX_FILES or len(tracked) != len(set(tracked)):
        fail("tracked_tree_invalid")
    for relative in tracked:
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or ".git" in path.parts:
            fail("tracked_path_invalid")
        full = source / relative
        details = full.lstat()
        if details.st_uid != os.getuid() or (
            not stat.S_ISLNK(details.st_mode) and details.st_mode & 0o022
        ):
            fail("unsafe_source_ownership_or_mode")
        if stat.S_ISREG(details.st_mode):
            if details.st_size > MAX_FILE_BYTES:
                fail("source_file_too_large")
            with full.open("rb") as handle:
                if handle.read(128).startswith(b"version https://git-lfs.github.com/spec/v1"):
                    fail("git_lfs_forbidden")
        elif stat.S_ISLNK(details.st_mode):
            safe_link(relative, os.readlink(full))
        else:
            fail("source_special_file_forbidden")
    if ".gitmodules" in tracked:
        fail("git_submodules_forbidden")
    return tracked


def safe_link(relative: str, target: str) -> None:
    if not target or target.startswith("/"):
        fail("unsafe_symlink")
    resolved = PurePosixPath(relative).parent.joinpath(target)
    depth = 0
    for part in resolved.parts:
        if part == "..":
            depth -= 1
        elif part not in {"", "."}:
            depth += 1
        if depth < 0:
            fail("unsafe_symlink")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def frontend_policy(request: dict[str, object], source: Path) -> dict[str, object] | None:
    frontend = request.get("frontend")
    if frontend is None:
        return None
    if (
        not isinstance(frontend, dict)
        or set(frontend) != {"package_manager", "build_script", "output_dir"}
        or frontend.get("package_manager") not in LOCKFILES
        or not isinstance(frontend.get("build_script"), str)
        or re.fullmatch(r"[A-Za-z0-9:_-]{1,64}", frontend["build_script"]) is None
        or frontend.get("output_dir") != "public/build"
    ):
        fail("frontend_policy_invalid")
    supported = {name for names in LOCKFILES.values() for name in names}
    present = sorted(name for name in supported if (source / name).is_file())
    matching = [name for name in LOCKFILES[str(frontend["package_manager"])] if name in present]
    if len(present) != 1 or len(matching) != 1:
        fail("frontend_lockfile_invalid")
    lock = source / matching[0]
    if lock.is_symlink() or lock.stat().st_size > MAX_LOCK_BYTES:
        fail("frontend_lockfile_invalid")
    return {
        "package_manager": frontend["package_manager"],
        "build_script": frontend["build_script"],
        "output_dir": frontend["output_dir"],
        "lockfile": matching[0],
        "lockfile_sha256": file_sha256(lock),
        "lockfile_bytes": lock.stat().st_size,
    }


def runtime_vector(
    runtimes: dict[str, object], names: list[str], arguments: list[str], workspace_root: Path
) -> tuple[list[str], dict[str, str]]:
    selected = []
    for name in names:
        pin = runtimes[name]
        if isinstance(pin, dict) and pin["provider"] == "mise":
            selected.append(f"{name}@{pin['version']}")
    if not selected:
        return arguments, {}
    return ["mise", "exec", *selected, "--", *arguments], {
        "MISE_DATA_DIR": str(workspace_root.parent / "mise")
    }


def frontend_capability(
    request: dict[str, object], policy: dict[str, object] | None, workspace_root: Path
) -> dict[str, object] | None:
    if policy is None:
        return None
    runtimes = request["runtimes"]
    manager = str(policy["package_manager"])
    names = [manager] if manager == "bun" else ["node", manager]
    if not isinstance(runtimes, dict) or any(name not in runtimes for name in names):
        fail("build_runtime_policy_invalid")
    manager_command, manager_environment = runtime_vector(
        runtimes, names, [manager, "--version"], workspace_root
    )
    manager_version = command(
        manager_command, environment=manager_environment, timeout=30
    ).decode().strip().removeprefix("v")
    node_version = None
    if manager != "bun":
        node_command, node_environment = runtime_vector(
            runtimes, names, ["node", "--version"], workspace_root
        )
        node_version = command(
            node_command, environment=node_environment, timeout=30
        ).decode().strip().removeprefix("v")
    if (
        manager_version != runtimes[manager]["version"]
        or manager != "bun" and node_version != runtimes["node"]["version"]
    ):
        fail("build_runtime_mismatch")
    return {
        "manager": manager,
        "manager_version": manager_version,
        "node_version": node_version,
    }


def runtime_capability(
    request: dict[str, object], policy: dict[str, object] | None, workspace_root: Path
) -> dict[str, object]:
    runtimes = request.get("runtimes")
    extensions = request.get("php_extensions")
    if not isinstance(runtimes, dict) or set(runtimes) < {"php", "composer"}:
        fail("build_runtime_policy_invalid")
    for name, pin in runtimes.items():
        if (
            not isinstance(name, str)
            or not isinstance(pin, dict)
            or set(pin) != {"provider", "version"}
            or pin["provider"] not in {"system", "mise", "bundled"}
            or not isinstance(pin["version"], str)
            or VERSION.fullmatch(pin["version"]) is None
            or name in {"php", "composer"} and pin["provider"] != "system"
            or pin["provider"] == "bundled" and name != "npm"
        ):
            fail("build_runtime_policy_invalid")
    if not isinstance(extensions, list) or any(
        not isinstance(item, str) or re.fullmatch(r"[a-z][a-z0-9_]{0,47}", item) is None
        for item in extensions
    ):
        fail("build_extension_policy_invalid")
    php = command(["php", "-r", "echo PHP_VERSION;"], timeout=30).decode().strip()
    composer_line = command(["composer", "--version", "--no-ansi"], timeout=30).decode().strip()
    match = re.search(r"Composer version ([0-9][A-Za-z0-9.+-]*)", composer_line)
    composer = "" if match is None else match.group(1)
    available = sorted(
        line.strip().lower().replace("pdo_pgsql", "pdo_pgsql")
        for line in command(["php", "-m"], timeout=30).decode().splitlines()
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", line.strip())
    )
    if php != runtimes["php"]["version"] or composer != runtimes["composer"]["version"]:
        fail("build_runtime_mismatch")
    missing = sorted(set(extensions) - set(available))
    if missing:
        fail("build_extension_missing")
    return {
        "php": php,
        "composer": composer,
        "php_extensions": available,
        "system": platform.system().lower(),
        "machine": platform.machine().lower(),
        "frontend": frontend_capability(request, policy, workspace_root),
    }


def inspect_source(request: dict[str, object], workspace_root: Path) -> dict[str, object]:
    workspace = None
    try:
        workspace, tracked = clone_exact(request, workspace_root)
        source = workspace / "source"
        if "composer.lock" not in tracked:
            fail("composer_lock_missing")
        lock = source / "composer.lock"
        if not lock.is_file() or lock.is_symlink() or lock.stat().st_size > MAX_LOCK_BYTES:
            fail("composer_lock_invalid")
        frontend = frontend_policy(request, source)
        capability = runtime_capability(request, frontend, workspace_root)
        return {
            "status": "ready",
            "commit": request["commit"],
            "repository_fingerprint": "repo_" + hashlib.sha256(
                str(request["repository"]).encode()
            ).hexdigest(),
            "composer_lock_sha256": file_sha256(lock),
            "composer_lock_bytes": lock.stat().st_size,
            "frontend_lock": (
                None if frontend is None else {
                    "filename": frontend["lockfile"],
                    "sha256": frontend["lockfile_sha256"],
                    "bytes": frontend["lockfile_bytes"],
                }
            ),
            "capability": capability,
        }
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)


def credentials(argument: str) -> tuple[dict[str, str], dict[str, str]]:
    if argument == "-":
        return {}, {}
    path = Path(argument)
    try:
        details = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(details.st_mode):
            fail("credential_document_invalid")
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        fail("credential_document_invalid")
    finally:
        path.unlink(missing_ok=True)
    if not isinstance(value, dict) or set(value) != {"store", "build"}:
        fail("credential_document_invalid")
    store, build_values = value["store"], value["build"]
    allowed = {"access_key_id", "secret_access_key", "session_token"}
    if not isinstance(store, dict) or (
        store and not {"access_key_id", "secret_access_key"} <= set(store) <= allowed
    ) or any(
        not isinstance(item, str) or not item or len(item) > 4096
        for item in store.values()
    ):
        fail("credential_document_invalid")
    if (
        not isinstance(build_values, dict)
        or len(build_values) > 32
        or any(
            not isinstance(name, str)
            or re.fullmatch(r"^[A-Z][A-Z0-9_]{0,63}$", name) is None
            or not isinstance(item, str)
            or len(item) > 4096
            for name, item in build_values.items()
        )
    ):
        fail("credential_document_invalid")
    return store, build_values


def s3_client(store: dict[str, object], values: dict[str, str]):
    options: dict[str, object] = {
        "region_name": store["region"],
        "config": Config(
            signature_version="s3v4",
            s3={"addressing_style": (
                "virtual" if store["addressing"] == "virtual_hosted" else "path"
            )},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    }
    if store["endpoint"] is not None:
        options["endpoint_url"] = "https://" + str(store["endpoint"])
    if values:
        options["aws_access_key_id"] = values["access_key_id"]
        options["aws_secret_access_key"] = values["secret_access_key"]
        if "session_token" in values:
            options["aws_session_token"] = values["session_token"]
    return boto3.client("s3", **options)


def store_request(request: dict[str, object]):
    store = request.get("store")
    if not isinstance(store, dict) or set(store) != {
        "bucket", "region", "endpoint", "addressing", "encryption"
    }:
        fail("artifact_store_policy_invalid")
    return store


def encryption(store: dict[str, object]) -> tuple[dict[str, str], str]:
    policy = store["encryption"]
    if not isinstance(policy, dict) or policy.get("method") not in {"aes256", "kms"}:
        fail("artifact_store_policy_invalid")
    if policy["method"] == "aes256" and set(policy) == {"method"}:
        return {"ServerSideEncryption": "AES256"}, "AES256"
    if (
        policy["method"] == "kms"
        and set(policy) == {"method", "kms_key_arn"}
        and isinstance(policy["kms_key_arn"], str)
    ):
        return {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": policy["kms_key_arn"],
        }, "aws:kms"
    fail("artifact_store_policy_invalid")


def encryption_confirmed(
    response: dict[str, object], store: dict[str, object], expected: str
) -> bool:
    if response.get("ServerSideEncryption") != expected:
        return False
    policy = store["encryption"]
    return expected != "aws:kms" or response.get("SSEKMSKeyId") == policy["kms_key_arn"]


def object_keys(application: str, build_id: str) -> tuple[str, str, str]:
    scope = hashlib.sha256(application.encode()).hexdigest()[:20]
    prefix = f"gimme/artifacts/{scope}/{build_id}"
    return prefix + "/package.tar.gz", prefix + "/manifest.json", f"gimme/artifacts/{scope}/"


def read_bounded_body(body, limit: int) -> bytes:
    try:
        value = body.read(limit + 1)
    finally:
        body.close()
    if len(value) > limit:
        fail("artifact_object_too_large")
    return value


def client_error_missing(error: ClientError) -> bool:
    response = getattr(error, "response", {})
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 404 or code in {"NoSuchKey", "NoSuchVersion", "NotFound"}


def read_manifest_record(client, bucket: str, key: str, version: str | None = None):
    try:
        arguments = {"Bucket": bucket, "Key": key}
        if version is not None:
            arguments["VersionId"] = version
        response = client.get_object(**arguments)
    except ClientError as error:
        if client_error_missing(error):
            return None, None
        raise
    raw = read_bounded_body(response["Body"], MAX_MANIFEST_BYTES)
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        fail("artifact_manifest_malformed")
    if not isinstance(value, dict):
        fail("artifact_manifest_malformed")
    return value, response


def read_manifest(client, bucket: str, key: str) -> dict[str, object] | None:
    value, _ = read_manifest_record(client, bucket, key)
    return value


def valid_manifest(value: dict[str, object], application: str, build_id: str) -> bool:
    provenance = {"build_secrets_used", "build_secret_count"}
    return (
        set(value) == {
            "schema_version", "application", "build_id", "commit", "format",
            "artifact_digest", "tree_digest", "bytes", "package_version", "published_at",
        } | provenance
        and value.get("schema_version") == 2
        and value.get("application") == application
        and value.get("build_id") == build_id
        and value.get("format") == "laravel_v1"
        and isinstance(value.get("commit"), str)
        and COMMIT.fullmatch(value["commit"]) is not None
        and isinstance(value.get("artifact_digest"), str)
        and SHA256.fullmatch(value["artifact_digest"]) is not None
        and isinstance(value.get("tree_digest"), str)
        and SHA256.fullmatch(value["tree_digest"]) is not None
        and isinstance(value.get("bytes"), int)
        and 0 < value["bytes"] <= MAX_ARCHIVE_BYTES
        and isinstance(value.get("package_version"), str)
        and VERSION_ID.fullmatch(value["package_version"]) is not None
        and isinstance(value.get("published_at"), str)
        and PUBLISHED_AT.fullmatch(value["published_at"]) is not None
        and isinstance(value.get("build_secrets_used"), bool)
        and isinstance(value.get("build_secret_count"), int)
        and 0 <= value["build_secret_count"] <= 32
        and value["build_secrets_used"] == (value["build_secret_count"] > 0)
    )


def manifest_integrity(client, bucket: str, application: str, manifest: dict[str, object]) -> str:
    package_key, _, _ = object_keys(application, str(manifest["build_id"]))
    try:
        digest, size = stream_digest(
            client, bucket, package_key, str(manifest["package_version"])
        )
    except ClientError as error:
        return "missing" if client_error_missing(error) else "malformed"
    return (
        "ready" if digest == manifest["artifact_digest"] and size == manifest["bytes"]
        else "checksum_invalid"
    )


def publication_status(request: dict[str, object], credential_argument: str) -> dict[str, object]:
    application, build_id = request.get("application"), request.get("build_id")
    if not isinstance(application, str) or NAME.fullmatch(application) is None:
        fail("artifact_identity_invalid")
    if not isinstance(build_id, str) or BUILD_ID.fullmatch(build_id) is None:
        fail("artifact_identity_invalid")
    store = store_request(request)
    store_credentials, build_values = credentials(credential_argument)
    if build_values:
        fail("credential_document_invalid")
    client = s3_client(store, store_credentials)
    _, manifest_key, _ = object_keys(application, build_id)
    try:
        manifest = read_manifest(client, str(store["bucket"]), manifest_key)
    except ArtifactFailure:
        return {"status": "malformed", "build_id": build_id}
    if manifest is None:
        return {"status": "absent", "build_id": build_id}
    if not valid_manifest(manifest, application, build_id):
        return {"status": "malformed", "build_id": build_id}
    integrity = manifest_integrity(client, str(store["bucket"]), application, manifest)
    if integrity != "ready":
        return {"status": integrity, "build_id": build_id}
    return {
        "status": "published",
        "build_id": build_id,
        "artifact_digest": manifest["artifact_digest"],
        "tree_digest": manifest["tree_digest"],
        "bytes": manifest["bytes"],
    }


def included_source(relative: str) -> bool:
    path = PurePosixPath(relative)
    if relative == ".env" or relative.startswith(".env.") and relative != ".env.example":
        return False
    if path.parts and path.parts[0] in {
        ".bun", ".git", ".npm", ".pnpm-store", ".yarn", "node_modules", "storage",
    }:
        return False
    if len(path.parts) >= 2 and path.parts[:2] == ("bootstrap", "cache"):
        return False
    return True


def source_record(path: Path) -> tuple[str, str, int]:
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode):
        return "symlink", os.readlink(path), normalized_mode(details)
    if stat.S_ISREG(details.st_mode):
        return "file", file_sha256(path), normalized_mode(details)
    return "directory", "", normalized_mode(details)


def frontend_commands(manager: str, version: str, script: str) -> tuple[list[str], list[str]]:
    if manager == "npm":
        install = ["npm", "ci", "--no-audit", "--no-fund"]
    elif manager == "pnpm":
        install = ["pnpm", "install", "--frozen-lockfile"]
    elif manager == "yarn":
        install = (
            ["yarn", "install", "--frozen-lockfile", "--non-interactive"]
            if int(version.split(".", 1)[0]) == 1
            else ["yarn", "install", "--immutable"]
        )
    elif manager == "bun":
        install = ["bun", "install", "--frozen-lockfile"]
    else:
        fail("frontend_policy_invalid")
    return install, [manager, "run", script]


def run_frontend(
    request: dict[str, object], policy: dict[str, object], source: Path,
    workspace_root: Path, tracked: list[str], build_secrets: dict[str, str]
) -> None:
    output = str(policy["output_dir"])
    before = {
        relative: source_record(source / relative)
        for relative in tracked
        if relative != output and not relative.startswith(output + "/")
    }
    manager = str(policy["package_manager"])
    capability = request["capability"]
    frontend = capability["frontend"]
    install, build_command = frontend_commands(
        manager, str(frontend["manager_version"]), str(policy["build_script"])
    )
    names = [manager] if manager == "bun" else ["node", manager]
    install_vector, runtime_environment = runtime_vector(
        request["runtimes"], names, install, workspace_root
    )
    command(
        install_vector,
        cwd=source,
        environment={**runtime_environment, **build_secrets},
        timeout=1800,
    )
    build_vector, runtime_environment = runtime_vector(
        request["runtimes"], names, build_command, workspace_root
    )
    command(
        build_vector,
        cwd=source,
        environment={**runtime_environment, **build_secrets},
        timeout=1800,
    )
    try:
        mutated = any(
            source_record(source / relative) != record
            for relative, record in before.items()
        )
    except OSError:
        mutated = True
    if mutated:
        fail("frontend_source_mutation_detected")
    output_path = source / output
    if not output_path.is_dir() or output_path.is_symlink():
        fail("frontend_output_missing")


def contains_secret(handle, needles: list[bytes]) -> bool:
    overlap = max(len(needle) for needle in needles) - 1
    previous = b""
    while chunk := handle.read(1024 * 1024):
        combined = previous + chunk
        if any(needle in combined for needle in needles):
            return True
        previous = combined[-overlap:] if overlap else b""
    return False


def scan_secret_values(entries: list[tuple[str, Path]], values: dict[str, str]) -> None:
    needles = [value.encode() for value in values.values() if value]
    if not needles:
        return
    for relative, path in entries:
        if any(needle in relative.encode() for needle in needles):
            fail("secret_leak_detected")
        if path.is_symlink():
            if any(needle in os.readlink(path).encode() for needle in needles):
                fail("secret_leak_detected")
        elif path.is_file():
            with path.open("rb") as handle:
                if contains_secret(handle, needles):
                    fail("secret_leak_detected")


def scan_workspace_secrets(root: Path, values: dict[str, str]) -> None:
    needles = [value.encode() for value in values.values() if value]
    if not needles:
        return
    count, total = 0, 0
    for path in root.rglob("*"):
        details = path.lstat()
        if any(needle in path.relative_to(root).as_posix().encode() for needle in needles):
            fail("secret_leak_detected")
        if stat.S_ISLNK(details.st_mode) and any(
            needle in os.readlink(path).encode() for needle in needles
        ):
            fail("secret_leak_detected")
        if not stat.S_ISREG(details.st_mode):
            continue
        count += 1
        total += details.st_size
        if count > MAX_FILES or total > MAX_TREE_BYTES:
            fail("secret_scan_bounds_exceeded")
        with path.open("rb") as handle:
            if contains_secret(handle, needles):
                fail("secret_leak_detected")


def scan_archive_secrets(archive_path: Path, values: dict[str, str]) -> None:
    needles = [value.encode() for value in values.values() if value]
    if not needles:
        return
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            if any(needle in member.name.encode() for needle in needles) or (
                member.issym()
                and any(needle in member.linkname.encode() for needle in needles)
            ):
                fail("secret_leak_detected")
            if not member.isfile():
                continue
            source = archive.extractfile(member)
            if source is not None and contains_secret(source, needles):
                fail("secret_leak_detected")


def collect_tree(
    source: Path, tracked: list[str], frontend: dict[str, object] | None = None
) -> list[tuple[str, Path]]:
    selected = [(relative, source / relative) for relative in tracked if included_source(relative)]
    vendor = source / "vendor"
    if not vendor.is_dir() or vendor.is_symlink():
        fail("composer_vendor_missing")
    for path in vendor.rglob("*"):
        relative = path.relative_to(source).as_posix()
        if "/.git/" in f"/{relative}/" or "/node_modules/" in f"/{relative}/":
            continue
        selected.append((relative, path))
    if frontend is not None:
        output = source / str(frontend["output_dir"])
        selected.append((str(frontend["output_dir"]), output))
        for path in output.rglob("*"):
            relative_parts = path.relative_to(output).parts
            if any(
                part == "node_modules"
                or part in {".bun", ".git", ".npm", ".pnpm-store", ".yarn"}
                or part == ".env"
                or part.startswith(".env.") and part != ".env.example"
                for part in relative_parts
            ):
                fail("frontend_output_invalid")
            selected.append((path.relative_to(source).as_posix(), path))
    unique = {name: path for name, path in selected}
    if len(unique) > MAX_FILES:
        fail("artifact_tree_too_many_files")
    return sorted(unique.items())


def validate_tree(entries: list[tuple[str, Path]], root: Path) -> None:
    total = 0
    for relative, path in entries:
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or not path.is_relative_to(root):
            fail("artifact_path_invalid")
        details = path.lstat()
        if details.st_uid != os.getuid() or (
            not stat.S_ISLNK(details.st_mode) and details.st_mode & 0o022
        ):
            fail("unsafe_artifact_ownership_or_mode")
        if stat.S_ISREG(details.st_mode):
            if details.st_size > MAX_FILE_BYTES:
                fail("artifact_file_too_large")
            total += details.st_size
        elif stat.S_ISLNK(details.st_mode):
            safe_link(relative, os.readlink(path))
        elif not stat.S_ISDIR(details.st_mode):
            fail("artifact_special_file_forbidden")
        if total > MAX_TREE_BYTES:
            fail("artifact_tree_too_large")


def normalized_mode(details: os.stat_result) -> int:
    if stat.S_ISDIR(details.st_mode):
        return 0o755
    if stat.S_ISLNK(details.st_mode):
        return 0o777
    return 0o755 if details.st_mode & 0o111 else 0o644


def tree_digest(entries: list[tuple[str, Path]]) -> str:
    digest = hashlib.sha256(b"gimme-laravel-tree-v1\0")
    for relative, path in entries:
        details = path.lstat()
        if stat.S_ISDIR(details.st_mode):
            kind, content = "directory", ""
        elif stat.S_ISLNK(details.st_mode):
            kind, content = "symlink", os.readlink(path)
        else:
            kind, content = "file", file_sha256(path)
        record = [relative, kind, normalized_mode(details), content]
        digest.update(json.dumps(record, separators=(",", ":")).encode())
        digest.update(b"\0")
    return digest.hexdigest()


def create_archive(entries: list[tuple[str, Path]], destination: Path) -> None:
    with destination.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for relative, path in entries:
                    details = path.lstat()
                    info = tarfile.TarInfo(relative)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    info.mode = normalized_mode(details)
                    if stat.S_ISDIR(details.st_mode):
                        info.type = tarfile.DIRTYPE
                        archive.addfile(info)
                    elif stat.S_ISLNK(details.st_mode):
                        info.type = tarfile.SYMTYPE
                        info.linkname = os.readlink(path)
                        archive.addfile(info)
                    else:
                        info.size = details.st_size
                        with path.open("rb") as handle:
                            archive.addfile(info, handle)
    if destination.stat().st_size > MAX_ARCHIVE_BYTES:
        fail("artifact_archive_too_large")


def verify_archive(archive_path: Path, expected_digest: str, root: Path) -> None:
    extracted = root / "verified"
    extracted.mkdir(mode=0o700)
    entries: list[tuple[str, Path]] = []
    total = 0
    with tarfile.open(archive_path, "r:gz") as archive:
        members = archive.getmembers()
        if len(members) > MAX_FILES:
            fail("artifact_archive_too_many_files")
        names = set()
        for member in members:
            pure = PurePosixPath(member.name)
            if (
                pure.is_absolute()
                or ".." in pure.parts
                or member.name in names
                or not (member.isfile() or member.isdir() or member.issym())
            ):
                fail("artifact_archive_unsafe")
            names.add(member.name)
            if member.issym():
                safe_link(member.name, member.linkname)
            total += member.size
            if member.size > MAX_FILE_BYTES or total > MAX_TREE_BYTES:
                fail("artifact_archive_too_large")
        ordered = sorted(
            members, key=lambda item: (len(PurePosixPath(item.name).parts), item.name)
        )
        for member in ordered:
            destination = extracted / member.name
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
            if member.isdir():
                destination.mkdir(exist_ok=True, mode=member.mode)
            elif member.issym():
                destination.symlink_to(member.linkname)
            else:
                source = archive.extractfile(member)
                if source is None:
                    fail("artifact_archive_unsafe")
                with destination.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
                os.chmod(destination, member.mode)
            entries.append((member.name, destination))
    if tree_digest(sorted(entries)) != expected_digest:
        fail("artifact_tree_digest_mismatch")


def stream_response_digest(response) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    body = response["Body"]
    try:
        for chunk in iter(lambda: body.read(1024 * 1024), b""):
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                fail("artifact_archive_too_large")
            digest.update(chunk)
    finally:
        body.close()
    return digest.hexdigest(), size


def stream_digest(client, bucket: str, key: str, version: str) -> tuple[str, int]:
    return stream_response_digest(
        client.get_object(Bucket=bucket, Key=key, VersionId=version)
    )


def resolve_artifact(request: dict[str, object], credential_argument: str) -> dict[str, object]:
    application, build_id = request.get("application"), request.get("build_id")
    if not isinstance(application, str) or NAME.fullmatch(application) is None:
        fail("artifact_identity_invalid")
    if not isinstance(build_id, str) or BUILD_ID.fullmatch(build_id) is None:
        fail("artifact_identity_invalid")
    store = store_request(request)
    store_credentials, build_values = credentials(credential_argument)
    if build_values:
        fail("credential_document_invalid")
    client = s3_client(store, store_credentials)
    bucket = str(store["bucket"])
    package_key, manifest_key, _ = object_keys(application, build_id)
    manifest, response = read_manifest_record(client, bucket, manifest_key)
    if manifest is None:
        return {"status": "missing", "application": application, "build_id": build_id}
    if not valid_manifest(manifest, application, build_id):
        fail("artifact_manifest_malformed")
    manifest_version = response.get("VersionId")
    _, expected_encryption = encryption(store)
    if (
        not isinstance(manifest_version, str)
        or VERSION_ID.fullmatch(manifest_version) is None
        or not encryption_confirmed(response, store, expected_encryption)
    ):
        fail("artifact_manifest_unverified")
    package_response = client.get_object(
        Bucket=bucket, Key=package_key, VersionId=manifest["package_version"]
    )
    if not encryption_confirmed(package_response, store, expected_encryption):
        fail("artifact_package_unverified")
    digest, size = stream_response_digest(package_response)
    if digest != manifest["artifact_digest"] or size != manifest["bytes"]:
        fail("artifact_checksum_invalid")
    return {
        "status": "ready",
        "application": application,
        "build_id": build_id,
        "commit": manifest["commit"],
        "schema_version": manifest["schema_version"],
        "format": manifest["format"],
        "artifact_digest": manifest["artifact_digest"],
        "tree_digest": manifest["tree_digest"],
        "bytes": manifest["bytes"],
        "package_version": manifest["package_version"],
        "manifest_version": manifest_version,
        "build_secrets_used": manifest["build_secrets_used"],
        "build_secret_count": manifest["build_secret_count"],
    }


def checked_release(apps_root: Path, argument: str) -> Path:
    release = Path(argument)
    try:
        resolved = release.resolve(strict=True)
        details = release.lstat()
    except OSError:
        fail("artifact_release_invalid")
    if (
        not release.is_absolute()
        or release != resolved
        or not resolved.is_relative_to(apps_root)
        or "releases" not in resolved.relative_to(apps_root).parts
        or release.is_symlink()
        or not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.getuid()
        or any(release.iterdir())
    ):
        fail("artifact_release_invalid")
    return resolved


def extract_release(archive_path: Path, release: Path, expected_digest: str) -> None:
    entries: list[tuple[str, Path]] = []
    total, names, folded = 0, set(), set()
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_FILES:
                fail("artifact_archive_too_many_files")
            for member in members:
                pure = PurePosixPath(member.name)
                normalized = pure.as_posix()
                canonical = normalized.casefold()
                if (
                    pure.is_absolute()
                    or not pure.parts
                    or ".." in pure.parts
                    or member.name != normalized
                    or normalized in names
                    or canonical in folded
                    or canonical == ".gimme-artifact.json"
                    or not (member.isfile() or member.isdir() or member.issym())
                ):
                    fail("artifact_archive_unsafe")
                names.add(normalized)
                folded.add(canonical)
                if member.issym():
                    safe_link(member.name, member.linkname)
                    if member.mode != 0o777:
                        fail("artifact_archive_unsafe")
                elif member.isdir() and member.mode != 0o755:
                    fail("artifact_archive_unsafe")
                elif member.isfile() and member.mode not in {0o644, 0o755}:
                    fail("artifact_archive_unsafe")
                total += member.size
                if member.size > MAX_FILE_BYTES or total > MAX_TREE_BYTES:
                    fail("artifact_archive_too_large")
            ordered = sorted(
                members,
                key=lambda item: (
                    2 if item.issym() else 0 if item.isdir() else 1,
                    len(PurePosixPath(item.name).parts),
                    item.name,
                ),
            )
            for member in ordered:
                destination = release / member.name
                if not destination.is_relative_to(release):
                    fail("artifact_archive_unsafe")
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                if member.isdir():
                    destination.mkdir(exist_ok=True, mode=0o755)
                elif member.issym():
                    destination.symlink_to(member.linkname)
                else:
                    source = archive.extractfile(member)
                    if source is None:
                        fail("artifact_archive_unsafe")
                    with destination.open("xb") as output:
                        shutil.copyfileobj(source, output, 1024 * 1024)
                    os.chmod(destination, member.mode)
                entries.append((member.name, destination))
        if tree_digest(sorted(entries)) != expected_digest:
            fail("artifact_tree_digest_mismatch")
    except BaseException:
        shutil.rmtree(release, ignore_errors=True)
        raise


def materialize_artifact(
    request: dict[str, object], credential_argument: str, workspace_root: Path,
    apps_root: Path, release_argument: str,
) -> dict[str, object]:
    artifact = request.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("status") != "ready":
        fail("artifact_materialization_invalid")
    application, build_id = artifact.get("application"), artifact.get("build_id")
    if (
        not isinstance(application, str)
        or NAME.fullmatch(application) is None
        or not isinstance(build_id, str)
        or BUILD_ID.fullmatch(build_id) is None
    ):
        fail("artifact_materialization_invalid")
    store = store_request(request)
    store_credentials, build_values = credentials(credential_argument)
    if build_values:
        fail("credential_document_invalid")
    release = checked_release(apps_root, release_argument)
    workspace = None
    try:
        workspace = Path(tempfile.mkdtemp(prefix="materialize-", dir=workspace_root))
        os.chmod(workspace, 0o700)
        client = s3_client(store, store_credentials)
        bucket = str(store["bucket"])
        package_key, manifest_key, _ = object_keys(application, build_id)
        manifest_version = artifact.get("manifest_version")
        if not isinstance(manifest_version, str) or VERSION_ID.fullmatch(manifest_version) is None:
            fail("artifact_materialization_invalid")
        manifest, manifest_response = read_manifest_record(
            client, bucket, manifest_key, manifest_version
        )
        _, expected_encryption = encryption(store)
        if (
            manifest is None
            or not valid_manifest(manifest, application, build_id)
            or not encryption_confirmed(manifest_response, store, expected_encryption)
        ):
            fail("artifact_manifest_unverified")
        expected = {
            "status": "ready", "application": application, "build_id": build_id,
            "commit": manifest["commit"], "schema_version": manifest["schema_version"],
            "format": manifest["format"], "artifact_digest": manifest["artifact_digest"],
            "tree_digest": manifest["tree_digest"], "bytes": manifest["bytes"],
            "package_version": manifest["package_version"],
            "manifest_version": manifest_version,
            "build_secrets_used": manifest["build_secrets_used"],
            "build_secret_count": manifest["build_secret_count"],
        }
        if artifact != expected:
            fail("artifact_plan_stale")
        package_response = client.get_object(
            Bucket=bucket, Key=package_key, VersionId=manifest["package_version"]
        )
        if not encryption_confirmed(package_response, store, expected_encryption):
            fail("artifact_package_unverified")
        archive_path = workspace / "artifact.tar.gz"
        digest, size = hashlib.sha256(), 0
        body = package_response["Body"]
        try:
            with archive_path.open("xb") as output:
                for chunk in iter(lambda: body.read(1024 * 1024), b""):
                    size += len(chunk)
                    if size > MAX_ARCHIVE_BYTES:
                        fail("artifact_archive_too_large")
                    digest.update(chunk)
                    output.write(chunk)
        finally:
            body.close()
        if digest.hexdigest() != manifest["artifact_digest"] or size != manifest["bytes"]:
            fail("artifact_checksum_invalid")
        extract_release(archive_path, release, str(manifest["tree_digest"]))
        metadata = {
            "application": application,
            "commit": manifest["commit"],
            "build_id": build_id,
            "artifact_digest": manifest["artifact_digest"],
            "tree_digest": manifest["tree_digest"],
            "manifest_version": manifest_version,
            "packaging_schema": manifest["format"],
            "release_mode": "artifact",
        }
        metadata_path = release / ".gimme-artifact.json"
        metadata_path.write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")))
        os.chmod(metadata_path, 0o444)
        return {"status": "materialized", "application": application, "build_id": build_id}
    except BaseException:
        shutil.rmtree(release, ignore_errors=True)
        raise
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)


def build(request: dict[str, object], credential_argument: str, workspace_root: Path):
    application, build_id = request.get("application"), request.get("build_id")
    if not isinstance(application, str) or NAME.fullmatch(application) is None:
        fail("artifact_identity_invalid")
    if not isinstance(build_id, str) or BUILD_ID.fullmatch(build_id) is None:
        fail("artifact_identity_invalid")
    expected_lock = request.get("composer_lock_sha256")
    if not isinstance(expected_lock, str) or SHA256.fullmatch(expected_lock) is None:
        fail("artifact_identity_invalid")
    store = store_request(request)
    store_credentials, build_secrets = credentials(credential_argument)
    expected_secret_names = request.get("build_secret_names")
    if (
        not isinstance(expected_secret_names, list)
        or expected_secret_names != sorted(build_secrets)
        or len(expected_secret_names) != len(set(expected_secret_names))
    ):
        fail("build_secret_policy_invalid")
    workspace = None
    try:
        workspace, tracked = clone_exact(request, workspace_root)
        source = workspace / "source"
        lock = source / "composer.lock"
        frontend = frontend_policy(request, source)
        frontend_lock = (
            None if frontend is None else {
                "filename": frontend["lockfile"],
                "sha256": frontend["lockfile_sha256"],
                "bytes": frontend["lockfile_bytes"],
            }
        )
        capability = runtime_capability(request, frontend, workspace_root)
        if (
            file_sha256(lock) != expected_lock
            or frontend_lock != request.get("frontend_lock")
            or capability != request.get("capability")
        ):
            fail("build_plan_stale")
        composer_home = workspace / "composer-home"
        composer_cache = workspace / "composer-cache"
        composer_home.mkdir(mode=0o700)
        composer_cache.mkdir(mode=0o700)
        command([
            "composer", "validate", "--no-check-publish", "--strict", "--no-ansi",
        ], cwd=source, environment={"COMPOSER_HOME": str(composer_home)}, timeout=300)
        if file_sha256(lock) != expected_lock:
            fail("composer_lock_changed")
        command([
            "composer", "install", "--no-dev", "--prefer-dist", "--no-interaction",
            "--no-progress", "--no-ansi", "--no-scripts", "--optimize-autoloader",
            "--classmap-authoritative",
        ], cwd=source, environment={
            "COMPOSER_HOME": str(composer_home),
            "COMPOSER_CACHE_DIR": str(composer_cache),
            "COMPOSER_ALLOW_SUPERUSER": "0",
        }, timeout=1800)
        if file_sha256(lock) != expected_lock:
            fail("composer_lock_changed")
        command(["composer", "check-platform-reqs", "--no-dev", "--no-ansi"],
                cwd=source, environment={"COMPOSER_HOME": str(composer_home)}, timeout=300)
        if not (source / "vendor" / "composer" / "autoload_real.php").is_file():
            fail("composer_runtime_metadata_missing")
        if frontend is not None:
            run_frontend(
                request, frontend, source, workspace_root, tracked, build_secrets
            )
            refreshed = frontend_policy(request, source)
            if refreshed != frontend:
                fail("frontend_lockfile_changed")
        scan_workspace_secrets(source, build_secrets)
        entries = collect_tree(source, tracked, frontend)
        validate_tree(entries, source)
        scan_secret_values(entries, build_secrets)
        immutable_digest = tree_digest(entries)
        archive_path = workspace / "artifact.tar.gz"
        create_archive(entries, archive_path)
        verify_archive(archive_path, immutable_digest, workspace)
        scan_archive_secrets(archive_path, build_secrets)
        artifact_digest = file_sha256(archive_path)
        artifact_bytes = archive_path.stat().st_size
        client = s3_client(store, store_credentials)
        bucket = str(store["bucket"])
        package_key, manifest_key, _ = object_keys(application, build_id)
        existing = read_manifest(client, bucket, manifest_key)
        if existing is not None:
            if not valid_manifest(existing, application, build_id):
                fail("artifact_manifest_malformed")
            if manifest_integrity(client, bucket, application, existing) != "ready":
                fail("artifact_publication_degraded")
            if (
                existing["artifact_digest"] != artifact_digest
                or existing["tree_digest"] != immutable_digest
                or existing["bytes"] != artifact_bytes
            ):
                return {"status": "non_reproducible_build", "application": application,
                        "build_id": build_id}
            return {
                "status": "idempotent", "application": application, "build_id": build_id,
                "artifact_digest": artifact_digest, "tree_digest": immutable_digest,
                "bytes": artifact_bytes,
            }
        encryption_options, expected_encryption = encryption(store)
        with archive_path.open("rb") as body:
            uploaded = client.put_object(
                Bucket=bucket, Key=package_key, Body=body,
                Metadata={"gimme-sha256": artifact_digest}, **encryption_options,
            )
        version = uploaded.get("VersionId")
        if (
            not isinstance(version, str)
            or VERSION_ID.fullmatch(version) is None
            or not encryption_confirmed(uploaded, store, expected_encryption)
        ):
            fail("artifact_upload_unverified")
        downloaded_digest, downloaded_bytes = stream_digest(client, bucket, package_key, version)
        if downloaded_digest != artifact_digest or downloaded_bytes != artifact_bytes:
            fail("artifact_upload_checksum_mismatch")
        manifest = {
            "schema_version": 2,
            "application": application,
            "build_id": build_id,
            "commit": request["commit"],
            "format": "laravel_v1",
            "artifact_digest": artifact_digest,
            "tree_digest": immutable_digest,
            "bytes": artifact_bytes,
            "package_version": version,
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "build_secrets_used": bool(build_secrets),
            "build_secret_count": len(build_secrets),
        }
        manifest_bytes = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode()
        try:
            published = client.put_object(
                Bucket=bucket, Key=manifest_key, Body=manifest_bytes,
                ContentType="application/json", IfNoneMatch="*", **encryption_options,
            )
            if not encryption_confirmed(published, store, expected_encryption):
                fail("artifact_manifest_encryption_failed")
            observed = read_manifest(client, bucket, manifest_key)
            if observed != manifest:
                fail("artifact_manifest_verification_failed")
        except ClientError as error:
            response = getattr(error, "response", {})
            if response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 412:
                raise
            winner = read_manifest(client, bucket, manifest_key)
            if winner is None or not valid_manifest(winner, application, build_id):
                fail("artifact_publication_conflict")
            if (
                winner["artifact_digest"] != artifact_digest
                or winner["tree_digest"] != immutable_digest
                or winner["bytes"] != artifact_bytes
            ):
                return {"status": "non_reproducible_build", "application": application,
                        "build_id": build_id}
            return {
                "status": "idempotent", "application": application, "build_id": build_id,
                "artifact_digest": artifact_digest, "tree_digest": immutable_digest,
                "bytes": artifact_bytes,
            }
        return {
            "status": "published", "application": application, "build_id": build_id,
            "artifact_digest": artifact_digest, "tree_digest": immutable_digest,
            "bytes": artifact_bytes,
        }
    finally:
        if workspace is not None:
            shutil.rmtree(workspace, ignore_errors=True)


def inventory(request: dict[str, object], credential_argument: str) -> dict[str, object]:
    application = request.get("application")
    if not isinstance(application, str) or NAME.fullmatch(application) is None:
        fail("artifact_identity_invalid")
    store = store_request(request)
    store_credentials, build_values = credentials(credential_argument)
    if build_values:
        fail("credential_document_invalid")
    client = s3_client(store, store_credentials)
    bucket = str(store["bucket"])
    _, _, prefix = object_keys(application, "build_v1_" + "0" * 64)
    publications: list[dict[str, object]] = []
    token = None
    while len(publications) < 100:
        arguments = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 100}
        if token is not None:
            arguments["ContinuationToken"] = token
        page = client.list_objects_v2(**arguments)
        for item in page.get("Contents", []):
            key = item.get("Key")
            if not isinstance(key, str) or not key.endswith("/manifest.json"):
                continue
            derived = key.removesuffix("/manifest.json").rsplit("/", 1)[-1]
            build_id = derived if BUILD_ID.fullmatch(derived) else "unknown"
            status = "malformed"
            published_at = ""
            artifact_digest = None
            try:
                manifest = read_manifest(client, bucket, key)
                if manifest is None or not isinstance(manifest, dict):
                    status = "malformed"
                elif manifest.get("application") != application:
                    status = "foreign"
                elif manifest.get("schema_version") != 2 or manifest.get("format") != "laravel_v1":
                    status = "unsupported"
                elif not valid_manifest(manifest, application, derived):
                    status = "malformed"
                else:
                    status = manifest_integrity(client, bucket, application, manifest)
                    published_at = str(manifest["published_at"])
                    artifact_digest = str(manifest["artifact_digest"])
            except (ArtifactFailure, ClientError):
                status = "malformed"
            publications.append({
                "build_id": build_id,
                "status": status,
                "published_at": published_at,
                "artifact_digest": artifact_digest,
            })
            if len(publications) >= 100:
                break
        if not page.get("IsTruncated") or len(publications) >= 100:
            break
        token = page.get("NextContinuationToken")
        if not isinstance(token, str):
            break
    publications.sort(key=lambda item: (str(item["published_at"]), str(item["build_id"])),
                      reverse=True)
    return {"application": application, "artifacts": publications}


def main(arguments: list[str]) -> int:
    if len(arguments) not in {4, 5}:
        fail("artifact_invocation_invalid")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    request = parse_request(arguments[1])
    operation = request.get("operation")
    if operation != "materialize" and len(arguments) != 4:
        fail("artifact_invocation_invalid")
    workspace_root = checked_root(arguments[3])
    if operation == "inspect":
        validate_request(request, {
            "operation", "repository", "commit", "runtimes", "php_extensions", "frontend",
        })
        result = inspect_source(request, workspace_root)
    elif operation == "publication":
        validate_request(request, {"operation", "application", "build_id", "store"})
        result = publication_status(request, arguments[2])
    elif operation == "build":
        validate_request(request, {
            "operation", "application", "repository", "commit", "runtimes",
            "php_extensions", "frontend", "composer_lock_sha256", "frontend_lock",
            "capability", "build_secret_names", "build_id", "store",
        })
        result = build(request, arguments[2], workspace_root)
    elif operation == "inventory":
        validate_request(request, {"operation", "application", "store"})
        result = inventory(request, arguments[2])
    elif operation == "resolve":
        validate_request(request, {"operation", "application", "build_id", "store"})
        result = resolve_artifact(request, arguments[2])
    elif operation == "materialize":
        if len(arguments) != 5:
            fail("artifact_invocation_invalid")
        validate_request(request, {"operation", "artifact", "store"})
        result = materialize_artifact(
            request, arguments[2], workspace_root, Path(arguments[3]), arguments[4]
        )
    else:
        fail("artifact_operation_invalid")
    emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
