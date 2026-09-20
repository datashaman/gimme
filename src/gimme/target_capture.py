from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess  # nosec B404 -- fixed executable argv only
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


POINT = re.compile(r"^rp_[a-f0-9]{20}$")
REQUEST = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DATABASE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,63}$")
VERSION_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,1024}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
FORMAT = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
MAX_COMPONENT_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024
MINIMUM_FREE_BYTES = 64 * 1024 * 1024
PG_DUMP = "/usr/bin/pg_dump"


class CaptureFailure(RuntimeError):
    """Fixed safe failure from the shared Target capture boundary."""


@dataclass(frozen=True)
class ObjectMetadata:
    bytes: int
    sha256: str
    encryption: str
    version_id: str | None


@dataclass(frozen=True)
class Component:
    kind: str
    path: Path
    bytes: int
    sha256: str
    resource_version: str
    format: str
    records: int | None = None
    captured_at: str | None = None


class ObjectStore(Protocol):
    def put(self, key: str, body: bytes, sha256: str) -> ObjectMetadata: ...
    def head(self, key: str, version_id: str | None = None) -> ObjectMetadata | None: ...
    def get(self, key: str, version_id: str | None = None) -> bytes: ...
    def delete(self, key: str, version_id: str) -> None: ...


def recovery_point_id(deployment: str, destination: str, request_id: str) -> str:
    if (
        NAME.fullmatch(deployment) is None
        or NAME.fullmatch(destination) is None
        or REQUEST.fullmatch(request_id) is None
    ):
        raise CaptureFailure("recovery_request_identity_invalid")
    digest = hashlib.sha256(
        f"gimme-recovery-point-v1\0{deployment}\0{destination}\0{request_id}".encode()
    ).hexdigest()[:20]
    return f"rp_{digest}"


def component_key(deployment: str, point_id: str, kind: str) -> str:
    if NAME.fullmatch(deployment) is None or POINT.fullmatch(point_id) is None:
        raise CaptureFailure("recovery_request_identity_invalid")
    if kind not in {"postgres", "valkey"}:
        raise CaptureFailure("recovery_component_kind_invalid")
    return f"gimme/recovery-points/{deployment}/{point_id}/{kind}.dump"


def manifest_key(deployment: str, point_id: str) -> str:
    if NAME.fullmatch(deployment) is None or POINT.fullmatch(point_id) is None:
        raise CaptureFailure("recovery_request_identity_invalid")
    return f"gimme/recovery-points/{deployment}/{point_id}/manifest.json"


def capture_postgres(
    database: str,
    resource_version: str,
    directory: Path,
    *,
    execute=subprocess.run,
) -> Component:
    if DATABASE.fullmatch(database) is None or VERSION.fullmatch(resource_version) is None:
        raise CaptureFailure("recovery_database_provenance_invalid")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    if shutil.disk_usage(directory).free < MINIMUM_FREE_BYTES:
        raise CaptureFailure("recovery_capacity_insufficient")
    descriptor, name = tempfile.mkstemp(prefix=".postgres-", suffix=".dump", dir=directory)
    os.close(descriptor)
    path = Path(name)
    os.chmod(path, 0o600)
    try:
        try:
            result = execute(  # nosec B603
                [
                    PG_DUMP,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--no-acl",
                    f"--role={database}",
                    "-d",
                    database,
                    "-f",
                    str(path),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1800,
                check=False,
            )
        except Exception:
            raise CaptureFailure("recovery_capture_failed") from None
        if result.returncode != 0:
            raise CaptureFailure("recovery_capture_failed")
        size = path.stat().st_size
        if not 0 <= size <= MAX_COMPONENT_BYTES:
            raise CaptureFailure("recovery_dump_metadata_invalid")
        with path.open("rb") as source:
            sha256 = hashlib.file_digest(source, "sha256").hexdigest()
        return Component(
            kind="postgres", path=path, bytes=size, sha256=sha256,
            resource_version=resource_version, format="pg-custom-v1",
        )
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _existing_manifest(
    store: ObjectStore, deployment: str, destination: str, point_id: str,
) -> dict[str, object] | None:
    key = manifest_key(deployment, point_id)
    metadata = store.head(key)
    if metadata is None:
        return None
    body = store.get(key, metadata.version_id)
    if len(body) > MAX_MANIFEST_BYTES or hashlib.sha256(body).hexdigest() != metadata.sha256:
        raise CaptureFailure("recovery_manifest_tampered")
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        raise CaptureFailure("recovery_manifest_invalid") from None
    if (
        not isinstance(document, dict)
        or set(document) != {
            "schema_version", "recovery_point_id", "deployment", "destination",
            "created_at", "safety", "restore_request_id", "components",
        }
        or document.get("schema_version") != 3
        or document.get("deployment") != deployment
        or document.get("destination") != destination
        or document.get("recovery_point_id") != point_id
        or document.get("safety") is not False
        or document.get("restore_request_id") is not None
        or not isinstance(document.get("components"), list)
        or not document["components"]
    ):
        raise CaptureFailure("recovery_manifest_invalid")
    for component in document["components"]:
        if (
            not isinstance(component, dict)
            or set(component) != {
                "kind", "key", "bytes", "sha256", "version_id", "format", "records",
                "captured_at", "resource_kind", "resource_version",
            }
            or component.get("kind") not in {"postgres", "valkey"}
            or component.get("resource_kind") != component.get("kind")
            or component.get("key")
            != component_key(deployment, point_id, str(component.get("kind")))
            or not isinstance(component.get("version_id"), str)
            or VERSION_ID.fullmatch(component["version_id"]) is None
            or isinstance(component.get("bytes"), bool)
            or not isinstance(component.get("bytes"), int)
            or not 0 <= component["bytes"] <= MAX_COMPONENT_BYTES
            or SHA256.fullmatch(str(component.get("sha256"))) is None
            or FORMAT.fullmatch(str(component.get("format"))) is None
            or VERSION.fullmatch(str(component.get("resource_version"))) is None
        ):
            raise CaptureFailure("recovery_manifest_invalid")
        component_metadata = store.head(component["key"], component["version_id"])
        if (
            component_metadata is None
            or component_metadata.bytes != component["bytes"]
            or component_metadata.sha256 != component["sha256"]
            or hashlib.sha256(
                store.get(component["key"], component["version_id"])
            ).hexdigest() != component["sha256"]
        ):
            raise CaptureFailure("recovery_component_verification_failed")
    return document


def publish(
    store: ObjectStore,
    deployment: str,
    destination: str,
    point_id: str,
    components: list[Component],
    *,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    existing = _existing_manifest(store, deployment, destination, point_id)
    if existing is not None:
        return existing
    if (
        not components
        or len(components) > 2
        or len({item.kind for item in components}) != len(components)
    ):
        raise CaptureFailure("recovery_component_kind_invalid")
    uploaded: list[tuple[str, str]] = []
    manifest_components: list[dict[str, object]] = []
    try:
        for component in sorted(components, key=lambda item: item.kind):
            key = component_key(deployment, point_id, component.kind)
            body = component.path.read_bytes()
            if (
                len(body) != component.bytes
                or hashlib.sha256(body).hexdigest() != component.sha256
            ):
                raise CaptureFailure("recovery_component_checksum_mismatch")
            written = store.put(key, body, component.sha256)
            if written.version_id is None or VERSION_ID.fullmatch(written.version_id) is None:
                raise CaptureFailure("recovery_component_version_missing")
            if not written.encryption:
                raise CaptureFailure("backup_destination_upload_not_encrypted")
            uploaded.append((key, written.version_id))
            confirmed = store.head(key, written.version_id)
            if (
                confirmed is None
                or confirmed.bytes != component.bytes
                or confirmed.sha256 != component.sha256
                or not confirmed.encryption
                or hashlib.sha256(store.get(key, written.version_id)).hexdigest()
                != component.sha256
            ):
                raise CaptureFailure("recovery_component_verification_failed")
            manifest_components.append({
                "kind": component.kind, "key": key, "bytes": component.bytes,
                "sha256": component.sha256, "version_id": written.version_id,
                "format": component.format, "records": component.records,
                "captured_at": component.captured_at
                or (observed_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
                "resource_kind": component.kind,
                "resource_version": component.resource_version,
            })
        manifest: dict[str, object] = {
            "schema_version": 3, "recovery_point_id": point_id,
            "deployment": deployment, "destination": destination,
            "created_at": (observed_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
            "safety": False, "restore_request_id": None,
            "components": manifest_components,
        }
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise CaptureFailure("recovery_manifest_too_large")
        key = manifest_key(deployment, point_id)
        try:
            store.put(key, encoded, hashlib.sha256(encoded).hexdigest())
        except Exception:
            # A lost response may hide a successfully published manifest. Never delete
            # components from beneath one; the deterministic retry will verify it.
            if store.head(key) is not None:
                uploaded.clear()
            raise
        return manifest
    except Exception:
        for key, version_id in reversed(uploaded):
            with suppress(Exception):
                store.delete(key, version_id)
        raise
