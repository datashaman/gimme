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
from typing import Callable, Protocol


POINT = re.compile(r"^rp_[a-f0-9]{20}$")
REQUEST = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DATABASE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
PREFIX = re.compile(r"^(?:[a-zA-Z0-9:_-]{1,160}|\{gimme:[a-z][a-z0-9-]{0,63}\}:)$")
HOST = re.compile(r"^[a-zA-Z0-9.-]{1,255}$")
VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,63}$")
VERSION_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,1024}$")
SHA256 = re.compile(r"^[a-f0-9]{64}$")
FORMAT = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
RESTORE_EVENT = re.compile(
    r"^gimme/restores/([a-z][a-z0-9-]{0,63})/"
    r"([a-z0-9][a-z0-9-]{0,63})/([0-9]{6})\.json$"
)
MAX_COMPONENT_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024
MINIMUM_FREE_BYTES = 64 * 1024 * 1024
PG_DUMP = "/usr/bin/pg_dump"
VALKEY_CAPTURE = "/usr/local/libexec/gimme-capture-valkey"


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
    def get(
        self, key: str, version_id: str | None = None,
        max_bytes: int = MAX_COMPONENT_BYTES,
    ) -> bytes: ...
    def delete(self, key: str, version_id: str) -> None: ...
    def list(self, prefix: str) -> list[str]: ...


def _provider_failure(error: Exception, operation: str) -> CaptureFailure:
    response = getattr(error, "response", None)
    provider_code = None
    if isinstance(response, dict) and isinstance(response.get("Error"), dict):
        provider_code = response["Error"].get("Code")
    codes = {
        "AccessDenied": "access_denied", "NoSuchBucket": "missing",
        "NoSuchKey": "missing", "404": "missing", "SlowDown": "throttled",
        "Throttling": "throttled",
    }
    return CaptureFailure(
        f"backup_destination_{operation}_{codes.get(provider_code, 'unavailable')}"
    )


class BotoObjectStore:
    """Exact-key/version S3 boundary for the shared Target capture core."""

    def __init__(self, destination: dict[str, object], credentials: dict[str, str] | None,
                 *, boto_module=None) -> None:
        if set(destination) != {
            "name", "provider", "bucket", "region", "endpoint", "addressing", "encryption",
            "auth_mode",
        } or destination.get("provider") != "s3_compatible":
            raise CaptureFailure("backup_destination_policy_invalid")
        if destination.get("addressing") not in {"virtual_hosted", "path"}:
            raise CaptureFailure("backup_destination_policy_invalid")
        encryption = destination.get("encryption")
        if not isinstance(encryption, dict) or encryption.get("method") not in {"AES256", "kms"}:
            raise CaptureFailure("backup_destination_policy_invalid")
        if (
            (encryption["method"] == "AES256" and set(encryption) != {"method"})
            or (
                encryption["method"] == "kms"
                and (
                    set(encryption) != {"method", "kms_key_arn"}
                    or not isinstance(encryption.get("kms_key_arn"), str)
                )
            )
            or not all(isinstance(destination.get(key), str) for key in (
                "name", "bucket", "region",
            ))
            or (
                destination.get("endpoint") is not None
                and not isinstance(destination.get("endpoint"), str)
            )
        ):
            raise CaptureFailure("backup_destination_policy_invalid")
        auth_mode = destination.get("auth_mode")
        if auth_mode == "ambient" and credentials is not None:
            raise CaptureFailure("credentials_unavailable")
        if auth_mode == "stored" and (
            not isinstance(credentials, dict)
            or set(credentials) != {"access_key_id", "secret_access_key"}
            or not all(
                isinstance(value, str) and value and "\n" not in value and "\0" not in value
                for value in credentials.values()
            )
        ):
            raise CaptureFailure("credentials_unavailable")
        if boto_module is None:
            try:
                import boto3 as boto_module
            except ImportError:
                raise CaptureFailure("credentials_unavailable") from None
        try:
            from botocore.config import Config

            kwargs: dict[str, object] = {
                "region_name": destination["region"],
                "config": Config(s3={
                    "addressing_style": (
                        "virtual" if destination["addressing"] == "virtual_hosted" else "path"
                    )
                }),
            }
            if destination["endpoint"] is not None:
                kwargs["endpoint_url"] = f"https://{destination['endpoint']}"
            if credentials is not None:
                kwargs["aws_access_key_id"] = credentials["access_key_id"]
                kwargs["aws_secret_access_key"] = credentials["secret_access_key"]
            self.client = boto_module.client("s3", **kwargs)
        except CaptureFailure:
            raise
        except Exception:
            raise CaptureFailure("destination_unavailable") from None
        self.destination = destination

    def _encryption(self) -> dict[str, str]:
        encryption = self.destination["encryption"]
        if encryption["method"] == "kms":
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": str(encryption["kms_key_arn"]),
            }
        return {"ServerSideEncryption": "AES256"}

    def put(self, key: str, body: bytes, sha256: str) -> ObjectMetadata:
        try:
            response = self.client.put_object(
                Bucket=self.destination["bucket"], Key=key, Body=body,
                Metadata={"gimme-sha256": sha256}, **self._encryption(),
            )
        except Exception as error:
            raise _provider_failure(error, "upload") from None
        return ObjectMetadata(
            len(body), sha256, str(response.get("ServerSideEncryption") or ""),
            response.get("VersionId"),
        )

    def head(self, key: str, version_id: str | None = None) -> ObjectMetadata | None:
        try:
            response = self.client.head_object(
                Bucket=self.destination["bucket"], Key=key,
                **({"VersionId": version_id} if version_id is not None else {}),
            )
        except Exception as error:
            failure = _provider_failure(error, "head")
            if str(failure).endswith("_missing"):
                return None
            raise failure from None
        metadata = response.get("Metadata") or {}
        return ObjectMetadata(
            int(response.get("ContentLength", -1)), str(metadata.get("gimme-sha256") or ""),
            str(response.get("ServerSideEncryption") or ""), response.get("VersionId"),
        )

    def get(
        self, key: str, version_id: str | None = None,
        max_bytes: int = MAX_COMPONENT_BYTES,
    ) -> bytes:
        if not 1 <= max_bytes <= MAX_COMPONENT_BYTES:
            raise CaptureFailure("recovery_component_too_large")
        try:
            response = self.client.get_object(
                Bucket=self.destination["bucket"], Key=key,
                **({"VersionId": version_id} if version_id is not None else {}),
            )
            body = response["Body"].read(max_bytes + 1)
            if not isinstance(body, bytes) or len(body) > max_bytes:
                raise CaptureFailure("recovery_component_too_large")
            return body
        except CaptureFailure:
            raise
        except Exception as error:
            raise _provider_failure(error, "read") from None

    def delete(self, key: str, version_id: str) -> None:
        try:
            self.client.delete_object(
                Bucket=self.destination["bucket"], Key=key, VersionId=version_id,
            )
        except Exception as error:
            raise _provider_failure(error, "cleanup") from None

    def list(self, prefix: str) -> list[str]:
        try:
            paginator = self.client.get_paginator("list_objects_v2")
            keys = [
                str(item["Key"])
                for page in paginator.paginate(
                    Bucket=self.destination["bucket"], Prefix=prefix,
                )
                for item in page.get("Contents", [])
            ]
            if len(keys) > 10_000:
                raise CaptureFailure("recovery_inventory_limit")
            return keys
        except CaptureFailure:
            raise
        except Exception as error:
            raise _provider_failure(error, "list") from None


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


def capture_valkey(
    prefix: str,
    host: str,
    port: int,
    tls: bool,
    resource_version: str,
    directory: Path,
    credential_path: Path | None = None,
    *,
    execute=subprocess.run,
) -> Component:
    if (
        PREFIX.fullmatch(prefix) is None
        or HOST.fullmatch(host) is None
        or isinstance(port, bool)
        or not isinstance(port, int)
        or not 1 <= port <= 65535
        or not isinstance(tls, bool)
        or VERSION.fullmatch(resource_version) is None
    ):
        raise CaptureFailure("recovery_valkey_provenance_invalid")
    if credential_path is not None and (
        not credential_path.is_file()
        or credential_path.is_symlink()
        or credential_path.stat().st_mode & 0o077
    ):
        raise CaptureFailure("credentials_unavailable")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    if shutil.disk_usage(directory).free < MINIMUM_FREE_BYTES:
        raise CaptureFailure("recovery_capacity_insufficient")
    descriptor, name = tempfile.mkstemp(prefix=".valkey-", suffix=".archive", dir=directory)
    os.close(descriptor)
    path = Path(name)
    os.chmod(path, 0o600)
    try:
        try:
            result = execute(  # nosec B603
                [
                    VALKEY_CAPTURE, str(path), prefix, host, str(port),
                    "yes" if tls else "no",
                    "-" if credential_path is None else str(credential_path),
                ],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=1800, check=False,
            )
        except Exception:
            raise CaptureFailure("recovery_capture_failed") from None
        marker = result.stdout.strip().split("|") if result.returncode == 0 else []
        if (
            len(marker) != 5
            or marker[0] != "GIMME_VALKEY_BACKUP"
            or SHA256.fullmatch(marker[1]) is None
            or not marker[2].isdigit()
            or not marker[3].isdigit()
        ):
            raise CaptureFailure("recovery_dump_metadata_invalid")
        size, records = int(marker[2]), int(marker[3])
        try:
            captured = datetime.fromisoformat(marker[4])
        except ValueError:
            raise CaptureFailure("recovery_dump_metadata_invalid") from None
        if (
            captured.tzinfo is None
            or not 0 <= size <= MAX_COMPONENT_BYTES
            or not 0 <= records <= 100_000
            or path.stat().st_size != size
        ):
            raise CaptureFailure("recovery_dump_metadata_invalid")
        with path.open("rb") as source:
            sha256 = hashlib.file_digest(source, "sha256").hexdigest()
        if sha256 != marker[1]:
            raise CaptureFailure("recovery_dump_metadata_invalid")
        return Component(
            kind="valkey", path=path, bytes=size, sha256=sha256,
            resource_version=resource_version, format="gimme-valkey-v1", records=records,
            captured_at=captured.astimezone(UTC).isoformat(),
        )
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _existing_manifest_record(
    store: ObjectStore, deployment: str, destination: str, point_id: str,
) -> tuple[dict[str, object], ObjectMetadata] | None:
    key = manifest_key(deployment, point_id)
    metadata = store.head(key)
    if metadata is None:
        return None
    body = store.get(key, metadata.version_id, MAX_MANIFEST_BYTES)
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
        or not isinstance(document.get("safety"), bool)
        or (
            document.get("safety") is True
            and (
                not isinstance(document.get("restore_request_id"), str)
                or REQUEST.fullmatch(document["restore_request_id"]) is None
            )
        )
        or (
            document.get("safety") is False
            and document.get("restore_request_id") is not None
        )
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
    return document, metadata


def _existing_manifest(
    store: ObjectStore, deployment: str, destination: str, point_id: str,
) -> dict[str, object] | None:
    record = _existing_manifest_record(store, deployment, destination, point_id)
    return None if record is None else record[0]


def publish(
    store: ObjectStore,
    deployment: str,
    destination: str,
    point_id: str,
    components: list[Component],
    *,
    observed_at: datetime | None = None,
    before_publish: Callable[[], None] | None = None,
) -> dict[str, object]:
    existing = _existing_manifest(store, deployment, destination, point_id)
    if existing is not None:
        if existing["safety"] is not False:
            raise CaptureFailure("recovery_manifest_conflict")
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
        if before_publish is not None:
            before_publish()
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


def find_recovery_point(
    store: ObjectStore, deployment: str, destination: str, point_id: str,
) -> dict[str, object] | None:
    """Return one fully verified immutable point, or None when it is unpublished."""
    return _existing_manifest(store, deployment, destination, point_id)


def verified_inventory(
    store: ObjectStore, deployment: str, destination: str,
) -> list[dict[str, object]]:
    """Load only fully verified manifests from authoritative object inventory."""
    prefix = f"gimme/recovery-points/{deployment}/"
    manifests: list[dict[str, object]] = []
    keys = store.list(prefix)
    if len(keys) > 10_000:
        raise CaptureFailure("recovery_inventory_limit")
    for key in sorted(keys):
        if not key.endswith("/manifest.json"):
            continue
        point_id = key[len(prefix):].split("/", 1)[0]
        if POINT.fullmatch(point_id) is None or key != manifest_key(deployment, point_id):
            continue
        try:
            manifest = _existing_manifest(store, deployment, destination, point_id)
        except CaptureFailure:
            continue
        if manifest is not None:
            manifests.append(manifest)
    return manifests


def retention_candidates(
    manifests: list[dict[str, object]], retain_last: int, replacement_point_id: str,
    protected_point_ids: set[str] | None = None,
) -> list[str]:
    """Select verified ordinary points oldest-first, never selecting the replacement."""
    if (
        isinstance(retain_last, bool) or not isinstance(retain_last, int)
        or not 1 <= retain_last <= 365 or POINT.fullmatch(replacement_point_id) is None
    ):
        raise CaptureFailure("recovery_retention_policy_invalid")
    protected = protected_point_ids or set()
    eligible = [
        item for item in manifests
        if str(item.get("recovery_point_id")) not in protected
    ]
    if not any(item.get("recovery_point_id") == replacement_point_id for item in eligible):
        raise CaptureFailure("recovery_retention_replacement_unverified")
    eligible.sort(key=lambda item: (
        str(item.get("created_at")), str(item.get("recovery_point_id")),
    ))
    count = max(0, len(eligible) - retain_last)
    candidates = [
        str(item["recovery_point_id"]) for item in eligible
        if item.get("recovery_point_id") != replacement_point_id
    ]
    if len(candidates) < count:
        raise CaptureFailure("recovery_retention_replacement_required")
    return candidates[:count]


def restore_protected_points(
    store: ObjectStore, deployment: str, manifests: list[dict[str, object]],
) -> set[str]:
    """Derive fail-closed source and Safety protection from strict restore event streams."""
    protected = {
        str(item["recovery_point_id"]) for item in manifests if item.get("safety") is True
    }
    prefix = f"gimme/restores/{deployment}/"
    grouped: dict[str, list[tuple[int, str]]] = {}
    keys = store.list(prefix)
    if len(keys) > 10_000:
        raise CaptureFailure("restore_record_limit")
    for key in keys:
        match = RESTORE_EVENT.fullmatch(key)
        if match is None or match.group(1) != deployment:
            raise CaptureFailure("restore_record_invalid")
        grouped.setdefault(match.group(2), []).append((int(match.group(3)), key))
    if len(grouped) > 100:
        raise CaptureFailure("restore_record_limit")
    common = {
        "schema_version", "deployment", "request_id", "sequence", "state", "created_at",
        "source_recovery_point_id", "destination", "safety_recovery_point_id",
    }
    additions = {
        2: set(),
        3: {"selected_components", "untouched_components", "partial"},
        4: {
            "selected_components", "untouched_components", "partial", "destinations",
            "request_fingerprint",
        },
        5: {
            "selected_components", "untouched_components", "partial", "destinations",
            "request_fingerprint", "safety_components",
        },
    }
    terminal = "completed"
    states = {
        "started", "maintenance_entered", "safety_verified", "safety_not_required",
        "safety_failed", "artifact_failed", "artifact_verified", "shadow_failed",
        "shadow_verified", "data_replaced", "verification_failed",
        "verification_succeeded", "cleanup_completed", terminal,
    }
    transitions = {
        None: {"started"}, "started": {"maintenance_entered"},
        "maintenance_entered": {"safety_verified", "safety_not_required", "safety_failed"},
        "safety_failed": {"maintenance_entered"},
        "safety_verified": {"artifact_failed", "artifact_verified"},
        "safety_not_required": {"artifact_failed", "artifact_verified"},
        "artifact_failed": {"safety_verified", "safety_not_required"},
        "artifact_verified": {"artifact_failed", "shadow_failed", "shadow_verified"},
        "shadow_failed": {"artifact_verified"}, "shadow_verified": {"data_replaced"},
        "data_replaced": {"verification_failed", "verification_succeeded"},
        "verification_failed": {"verification_failed", "verification_succeeded"},
        "verification_succeeded": {"verification_failed", "cleanup_completed"},
        "cleanup_completed": {"verification_failed", terminal}, terminal: set(),
    }
    for request, items in grouped.items():
        items.sort()
        if len(items) > 100 or [item[0] for item in items] != list(range(len(items))):
            raise CaptureFailure("restore_record_invalid")
        events = []
        previous = None
        source_point = None
        for sequence, key in items:
            metadata = store.head(key)
            if (
                metadata is None or metadata.version_id is None
                or metadata.bytes > MAX_MANIFEST_BYTES
            ):
                raise CaptureFailure("restore_record_invalid")
            body = store.get(key, metadata.version_id, MAX_MANIFEST_BYTES)
            if hashlib.sha256(body).hexdigest() != metadata.sha256:
                raise CaptureFailure("restore_record_invalid")
            try:
                event = json.loads(body)
            except (UnicodeError, json.JSONDecodeError):
                raise CaptureFailure("restore_record_invalid") from None
            schema = event.get("schema_version") if isinstance(event, dict) else None
            if (
                schema not in additions or set(event) != common | additions[schema]
                or event.get("deployment") != deployment
                or event.get("request_id") != request or event.get("sequence") != sequence
                or event.get("state") not in states
                or event.get("state") not in transitions[previous]
                or POINT.fullmatch(str(event.get("source_recovery_point_id"))) is None
                or (
                    source_point is not None
                    and event.get("source_recovery_point_id") != source_point
                )
                or (
                    event.get("safety_recovery_point_id") is not None
                    and POINT.fullmatch(str(event["safety_recovery_point_id"])) is None
                )
            ):
                raise CaptureFailure("restore_record_invalid")
            events.append(event)
            previous = event["state"]
            source_point = event["source_recovery_point_id"]
        latest = events[-1]
        safety_ids = {
            str(event["safety_recovery_point_id"])
            for event in events if event["safety_recovery_point_id"] is not None
        }
        if latest["state"] == terminal:
            protected.difference_update(safety_ids)
        else:
            protected.add(str(latest["source_recovery_point_id"]))
            protected.update(safety_ids)
    return protected


def delete_recovery_point_versions(
    store: ObjectStore, deployment: str, destination: str, point_id: str,
) -> int:
    """Delete only exact versions from one verified manifest, with the manifest last."""
    record = _existing_manifest_record(store, deployment, destination, point_id)
    if record is None:
        raise CaptureFailure("recovery_manifest_missing")
    manifest, manifest_metadata = record
    deleted = 0
    for component in sorted(
        manifest["components"], key=lambda item: (str(item["kind"]), str(item["key"]))
    ):
        key, version_id = str(component["key"]), str(component["version_id"])
        store.delete(key, version_id)
        if store.head(key, version_id) is not None:
            raise CaptureFailure("recovery_point_deletion_failed")
        deleted += 1
    key = manifest_key(deployment, point_id)
    if manifest_metadata.version_id is None:
        raise CaptureFailure("recovery_manifest_version_missing")
    store.delete(key, manifest_metadata.version_id)
    if store.head(key, manifest_metadata.version_id) is not None:
        raise CaptureFailure("recovery_point_deletion_failed")
    return deleted


def enforce_retention(
    store: ObjectStore, deployment: str, destination: str, retain_last: int,
    replacement_point_id: str,
) -> dict[str, object]:
    """Delete verified ordinary points oldest-first and stop at the first failure."""
    try:
        manifests = verified_inventory(store, deployment, destination)
        protected = restore_protected_points(store, deployment, manifests)
        candidates = retention_candidates(
            manifests, retain_last, replacement_point_id, protected
        )
        eligible_count = sum(
            str(item.get("recovery_point_id")) not in protected for item in manifests
        )
    except Exception:
        return {
            "outcome": "backup_succeeded_retention_failed", "error_code": "retention_failed",
            "deleted": 0, "remaining": 0,
        }
    deleted = 0
    for point_id in candidates:
        try:
            delete_recovery_point_versions(store, deployment, destination, point_id)
        except Exception:
            return {
                "outcome": "backup_succeeded_retention_failed",
                "error_code": "retention_failed", "deleted": deleted,
                "remaining": eligible_count - deleted,
            }
        deleted += 1
    return {
        "outcome": "succeeded", "error_code": None, "deleted": deleted,
        "remaining": eligible_count - deleted,
    }
