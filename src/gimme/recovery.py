from __future__ import annotations

import hashlib
import json
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from gimme.control import (
    ControlState,
    CredentialReferenceBackupAuth,
    S3BackupDestination,
    SSEKMS,
)

RECOVERY_POINT_PREFIX = "gimme/recovery-points"
PREFLIGHT_PREFIX = "gimme/preflight"
RECOVERY_POINT_ID = re.compile(r"^rp_[0-9a-f]{20}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
COMPONENT_KIND = re.compile(r"^postgres$")
MAX_MANIFEST_BYTES = 8 * 1024


class RecoveryError(RuntimeError):
    """A bounded error whose text is safe for plans, logs, and journals."""


Credentials = tuple[str, str] | None


@dataclass(frozen=True)
class ObjectMetadata:
    bytes: int
    sha256: str
    server_side_encryption: str
    version_id: str | None = None


class S3Adapter(Protocol):
    def bucket_versioning(
        self, destination: S3BackupDestination, credentials: Credentials
    ) -> str: ...

    def put_object(
        self, destination: S3BackupDestination, credentials: Credentials,
        key: str, body: bytes, sha256: str,
    ) -> ObjectMetadata: ...

    def head_object(
        self, destination: S3BackupDestination, credentials: Credentials, key: str
    ) -> ObjectMetadata | None: ...

    def get_object(
        self, destination: S3BackupDestination, credentials: Credentials, key: str
    ) -> bytes: ...

    def delete_object(
        self, destination: S3BackupDestination, credentials: Credentials, key: str,
        version_id: str | None = None,
    ) -> None: ...

    def list_keys(
        self, destination: S3BackupDestination, credentials: Credentials, prefix: str
    ) -> list[str]: ...


def _provider_error(exc: Exception, operation: str) -> RecoveryError:
    code = "unavailable"
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        provider_code = error.get("Code") if isinstance(error, dict) else None
        mapping = {
            "AccessDenied": "access_denied",
            "NoSuchBucket": "missing",
            "NoSuchKey": "missing",
            "404": "missing",
            "InvalidBucketState": "versioning_unavailable",
            "Throttling": "throttled",
            "SlowDown": "throttled",
        }
        if isinstance(provider_code, str):
            code = mapping.get(provider_code, "unavailable")
    return RecoveryError(f"backup_destination_{operation}_{code}")


class BotoS3Adapter:
    """Narrow S3 boundary. Every call is bounded to one destination and object key."""

    @staticmethod
    def _boto3():
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise RecoveryError("aws_sdk_unavailable") from exc
        return boto3

    def _client(self, destination: S3BackupDestination, credentials: Credentials):
        boto3 = self._boto3()
        from botocore.config import Config

        addressing_style = "virtual" if destination.addressing == "virtual_hosted" else "path"
        kwargs: dict[str, object] = {
            "region_name": destination.region,
            "config": Config(s3={"addressing_style": addressing_style}),
        }
        if destination.endpoint is not None:
            kwargs["endpoint_url"] = f"https://{destination.endpoint}"
        if credentials is not None:
            kwargs["aws_access_key_id"], kwargs["aws_secret_access_key"] = credentials
        return boto3.client("s3", **kwargs)

    @staticmethod
    def _sse_kwargs(destination: S3BackupDestination) -> dict[str, str]:
        if isinstance(destination.encryption, SSEKMS):
            return {
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": destination.encryption.kms_key_arn,
            }
        return {"ServerSideEncryption": "AES256"}

    def bucket_versioning(
        self, destination: S3BackupDestination, credentials: Credentials
    ) -> str:
        try:
            response = self._client(destination, credentials).get_bucket_versioning(
                Bucket=destination.bucket
            )
        except Exception as exc:
            raise _provider_error(exc, "versioning") from None
        return str(response.get("Status") or "Disabled")

    def put_object(
        self, destination: S3BackupDestination, credentials: Credentials,
        key: str, body: bytes, sha256: str,
    ) -> ObjectMetadata:
        try:
            response = self._client(destination, credentials).put_object(
                Bucket=destination.bucket,
                Key=key,
                Body=body,
                Metadata={"gimme-sha256": sha256},
                **self._sse_kwargs(destination),
            )
        except Exception as exc:
            raise _provider_error(exc, "upload") from None
        encryption = str(response.get("ServerSideEncryption") or "")
        if not encryption:
            raise RecoveryError("backup_destination_upload_not_encrypted")
        return ObjectMetadata(
            bytes=len(body), sha256=sha256, server_side_encryption=encryption,
            version_id=response.get("VersionId"),
        )

    def head_object(
        self, destination: S3BackupDestination, credentials: Credentials, key: str
    ) -> ObjectMetadata | None:
        try:
            response = self._client(destination, credentials).head_object(
                Bucket=destination.bucket, Key=key
            )
        except Exception as exc:
            error = _provider_error(exc, "head")
            if "missing" in str(error):
                return None
            raise error from None
        metadata = response.get("Metadata") or {}
        sha256 = str(metadata.get("gimme-sha256") or "")
        return ObjectMetadata(
            bytes=int(response.get("ContentLength") or 0),
            sha256=sha256,
            server_side_encryption=str(response.get("ServerSideEncryption") or ""),
        )

    def get_object(
        self, destination: S3BackupDestination, credentials: Credentials, key: str
    ) -> bytes:
        try:
            response = self._client(destination, credentials).get_object(
                Bucket=destination.bucket, Key=key
            )
            return response["Body"].read()
        except Exception as exc:
            raise _provider_error(exc, "download") from None

    def delete_object(
        self, destination: S3BackupDestination, credentials: Credentials, key: str,
        version_id: str | None = None,
    ) -> None:
        try:
            kwargs: dict[str, str] = {"VersionId": version_id} if version_id is not None else {}
            self._client(destination, credentials).delete_object(
                Bucket=destination.bucket, Key=key, **kwargs
            )
        except Exception as exc:
            raise _provider_error(exc, "cleanup") from None

    def list_keys(
        self, destination: S3BackupDestination, credentials: Credentials, prefix: str
    ) -> list[str]:
        try:
            client = self._client(destination, credentials)
            keys: list[str] = []
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=destination.bucket, Prefix=prefix):
                keys.extend(item["Key"] for item in page.get("Contents", []))
            return keys
        except Exception as exc:
            raise _provider_error(exc, "list") from None


def destination_credentials(destination: S3BackupDestination) -> dict[str, object] | None:
    """Return the two SecretReferences a credential-referenced destination needs, or None."""
    if not isinstance(destination.auth, CredentialReferenceBackupAuth):
        return None
    return {
        "access_key_id": destination.auth.access_key_id,
        "secret_access_key": destination.auth.secret_access_key,
    }


def plan_destination_credentials(
    state: ControlState, secrets_path: Path, destination: S3BackupDestination
) -> list[dict[str, str]] | None:
    references = destination_credentials(destination)
    if references is None:
        return None
    from gimme.secrets import plan_secret_references

    return plan_secret_references(state, secrets_path, references)  # type: ignore[arg-type]


def resolve_destination_credentials(
    state: ControlState, secrets_path: Path, destination: S3BackupDestination,
    planned: list[dict[str, str]] | None,
) -> Credentials:
    references = destination_credentials(destination)
    if references is None:
        return None
    from gimme.secrets import resolve_planned_secret_references

    resolved = resolve_planned_secret_references(
        state, secrets_path, references, planned or []  # type: ignore[arg-type]
    )
    return resolved["access_key_id"], resolved["secret_access_key"]


def preflight_backup_destination(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
) -> dict[str, object]:
    """Reject unversioned buckets, then round-trip a probe object leaving nothing behind."""
    status = adapter.bucket_versioning(destination, credentials)
    if status != "Enabled":
        raise RecoveryError("backup_destination_versioning_disabled")
    probe_key = f"{PREFLIGHT_PREFIX}/{uuid4().hex}.check"
    probe_body = f"gimme-preflight-{uuid4().hex}".encode()
    probe_sha256 = hashlib.sha256(probe_body).hexdigest()
    written: ObjectMetadata | None = None
    try:
        written = adapter.put_object(destination, credentials, probe_key, probe_body, probe_sha256)
        if written.server_side_encryption == "":
            raise RecoveryError("backup_destination_upload_not_encrypted")
        fetched = adapter.get_object(destination, credentials, probe_key)
        if fetched != probe_body:
            raise RecoveryError("backup_destination_round_trip_mismatch")
    except Exception:
        # A cleanup failure here must never replace the real probe failure above.
        with suppress(Exception):
            adapter.delete_object(
                destination, credentials, probe_key,
                version_id=written.version_id if written is not None else None,
            )
        raise
    # Delete the exact version this probe wrote: on a versioned bucket, a version-less
    # delete only adds a delete marker and leaves the probe's bytes behind as a prior
    # version, which is exactly the residue a preflight round trip must leave none of.
    adapter.delete_object(destination, credentials, probe_key, version_id=written.version_id)
    return {
        "versioning": "enabled",
        "write_read_delete": "verified",
        "tls": True,
        "encryption": destination.encryption.model_dump(mode="json"),
    }


def recovery_point_id(deployment: str, destination: str, request_id: str) -> str:
    digest = hashlib.sha256(
        f"gimme-recovery-point-v1\0{deployment}\0{destination}\0{request_id}".encode()
    ).hexdigest()[:20]
    return f"rp_{digest}"


def component_key(deployment: str, point_id: str, component: str) -> str:
    return f"{RECOVERY_POINT_PREFIX}/{deployment}/{point_id}/{component}.dump"


def manifest_key(deployment: str, point_id: str) -> str:
    return f"{RECOVERY_POINT_PREFIX}/{deployment}/{point_id}/manifest.json"


@dataclass(frozen=True)
class ComponentDump:
    kind: str
    local_path: Path
    sha256: str
    bytes: int


def find_recovery_point(
    destination_name: str, destination: S3BackupDestination, credentials: Credentials,
    adapter: S3Adapter, deployment: str, point_id: str,
) -> dict[str, object] | None:
    """Return the already-published manifest for one deterministic recovery point id, if any."""
    if RECOVERY_POINT_ID.fullmatch(point_id) is None:
        raise RecoveryError("recovery_point_identity_invalid")
    key = manifest_key(deployment, point_id)
    if adapter.head_object(destination, credentials, key) is None:
        return None
    return _load_manifest(destination, credentials, adapter, deployment, destination_name, point_id)


def create_recovery_point(
    destination_name: str, destination: S3BackupDestination, credentials: Credentials,
    adapter: S3Adapter, deployment: str, point_id: str, dump: ComponentDump,
) -> dict[str, object]:
    """Upload one verified PostgreSQL component and publish its immutable manifest."""
    existing = find_recovery_point(
        destination_name, destination, credentials, adapter, deployment, point_id
    )
    if existing is not None:
        return existing
    if COMPONENT_KIND.fullmatch(dump.kind) is None:
        raise RecoveryError("recovery_component_kind_invalid")
    key = component_key(deployment, point_id, dump.kind)
    body = dump.local_path.read_bytes()
    if len(body) != dump.bytes or hashlib.sha256(body).hexdigest() != dump.sha256:
        raise RecoveryError("recovery_component_checksum_mismatch")
    written: ObjectMetadata | None = None
    try:
        written = adapter.put_object(destination, credentials, key, body, dump.sha256)
        confirmed = adapter.head_object(destination, credentials, key)
        if confirmed is None or confirmed.bytes != written.bytes:
            raise RecoveryError("recovery_component_verification_failed")
        # head_object's sha256 only echoes the metadata tag put_object wrote, which S3
        # never validates against the stored bytes; download and hash to catch a
        # same-length corruption a provider-side metadata check would miss.
        if hashlib.sha256(adapter.get_object(destination, credentials, key)).hexdigest() != (
            dump.sha256
        ):
            raise RecoveryError("recovery_component_verification_failed")
    except Exception:
        adapter.delete_object(
            destination, credentials, key,
            version_id=written.version_id if written is not None else None,
        )
        raise
    manifest = {
        "schema_version": 1,
        "recovery_point_id": point_id,
        "deployment": deployment,
        "destination": destination_name,
        "created_at": datetime.now(UTC).isoformat(),
        "components": [
            {
                "kind": dump.kind,
                "key": key,
                "bytes": dump.bytes,
                "sha256": dump.sha256,
            }
        ],
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_MANIFEST_BYTES:
        adapter.delete_object(destination, credentials, key, version_id=written.version_id)
        raise RecoveryError("recovery_manifest_too_large")
    try:
        adapter.put_object(
            destination, credentials, manifest_key(deployment, point_id),
            encoded, hashlib.sha256(encoded).hexdigest(),
        )
    except Exception:
        # The manifest PUT may have actually landed even though the response was lost
        # (timeout, dropped connection): if so it is now the published record, and
        # deleting the component out from under it would break every future read with
        # recovery_manifest_tampered. Leave the component in place and let a retry of
        # this same deterministic point_id find the published manifest as a no-op.
        published = adapter.head_object(
            destination, credentials, manifest_key(deployment, point_id)
        )
        if published is None:
            adapter.delete_object(destination, credentials, key, version_id=written.version_id)
        raise
    return manifest


def _validate_manifest(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "recovery_point_id", "deployment", "destination",
        "created_at", "components",
    }:
        raise RecoveryError("recovery_manifest_invalid")
    if document.get("schema_version") != 1:
        raise RecoveryError("recovery_manifest_invalid")
    if RECOVERY_POINT_ID.fullmatch(str(document.get("recovery_point_id"))) is None:
        raise RecoveryError("recovery_manifest_invalid")
    components = document.get("components")
    if not isinstance(components, list) or not components or len(components) > 8:
        raise RecoveryError("recovery_manifest_invalid")
    for component in components:
        if (
            not isinstance(component, dict)
            or set(component) != {"kind", "key", "bytes", "sha256"}
            or COMPONENT_KIND.fullmatch(str(component.get("kind"))) is None
            or not isinstance(component.get("key"), str)
            or not isinstance(component.get("bytes"), int)
            or component["bytes"] < 0
            or SHA256_HEX.fullmatch(str(component.get("sha256"))) is None
        ):
            raise RecoveryError("recovery_manifest_invalid")
    return document


def _load_manifest(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
    deployment: str, destination_name: str, point_id: str,
) -> dict[str, object]:
    key = manifest_key(deployment, point_id)
    # Check size via head_object before ever reading the body: an object planted at a
    # manifest key with an oversized body must not be pulled fully into memory to reject it.
    probe = adapter.head_object(destination, credentials, key)
    if probe is None:
        raise RecoveryError("recovery_manifest_invalid")
    if probe.bytes > MAX_MANIFEST_BYTES:
        raise RecoveryError("recovery_manifest_too_large")
    raw = adapter.get_object(destination, credentials, key)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RecoveryError("recovery_manifest_too_large")
    try:
        document = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError):
        raise RecoveryError("recovery_manifest_invalid") from None
    manifest = _validate_manifest(document)
    if (
        manifest["deployment"] != deployment
        or manifest["recovery_point_id"] != point_id
        or manifest["destination"] != destination_name
    ):
        raise RecoveryError("recovery_manifest_invalid")
    expected_prefix = f"{RECOVERY_POINT_PREFIX}/{deployment}/{point_id}/"
    for component in manifest["components"]:  # type: ignore[union-attr]
        component_key_value = str(component["key"])
        if not component_key_value.startswith(expected_prefix):
            raise RecoveryError("recovery_manifest_invalid")
        confirmed = adapter.head_object(destination, credentials, component_key_value)
        if (
            confirmed is None
            or confirmed.bytes != component["bytes"]
            or confirmed.sha256 != component["sha256"]
        ):
            raise RecoveryError("recovery_manifest_tampered")
    return manifest


def list_recovery_points(
    destination_name: str, destination: S3BackupDestination, credentials: Credentials,
    adapter: S3Adapter, deployment: str,
) -> dict[str, object]:
    """Read-only, destination-authoritative inventory of one deployment's Recovery Points."""
    prefix = f"{RECOVERY_POINT_PREFIX}/{deployment}/"
    manifests: list[dict[str, object]] = []
    rejected: list[str] = []
    for key in sorted(adapter.list_keys(destination, credentials, prefix)):
        if not key.endswith("/manifest.json"):
            continue
        point_id = key[len(prefix):].split("/", 1)[0]
        try:
            manifests.append(
                _load_manifest(
                    destination, credentials, adapter, deployment, destination_name, point_id
                )
            )
        except RecoveryError:
            rejected.append(point_id if RECOVERY_POINT_ID.fullmatch(point_id) else "unknown")
    manifests.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return {
        "destination": destination_name,
        "deployment": deployment,
        "recovery_points": manifests,
        "rejected": sorted(set(rejected)),
    }
