from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Protocol
from uuid import uuid4

from gimme.control import (
    ControlState,
    CredentialReferenceBackupAuth,
    S3BackupDestination,
    SSEKMS,
)

RECOVERY_POINT_PREFIX = "gimme/recovery-points"
RESTORE_PREFIX = "gimme/restores"
PREFLIGHT_PREFIX = "gimme/preflight"
RECOVERY_POINT_ID = re.compile(r"^rp_[0-9a-f]{20}$")
SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
COMPONENT_KIND = re.compile(r"^(?:postgres|valkey)$")
VERSION_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,1024}$")
FORMAT_ID = re.compile(r"^[a-z0-9][a-z0-9.-]{0,63}$")
RESOURCE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+:~_-]{0,63}$")
MAX_MANIFEST_BYTES = 8 * 1024
MAX_COMPONENT_BYTES = 512 * 1024 * 1024
REQUEST_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
RESTORE_STATES = (
    "started", "maintenance_entered", "safety_verified", "safety_not_required",
    "safety_failed",
    "artifact_verified", "shadow_verified", "data_replaced", "verification_failed",
    "verification_succeeded", "cleanup_completed", "completed",
)
RESTORE_TRANSITIONS = {
    None: {"started"},
    "started": {"maintenance_entered"},
    "maintenance_entered": {"safety_verified", "safety_not_required", "safety_failed"},
    "safety_failed": {"maintenance_entered"},
    "safety_verified": {"artifact_verified"},
    "safety_not_required": {"artifact_verified"},
    "artifact_verified": {"shadow_verified"},
    "shadow_verified": {"data_replaced"},
    "data_replaced": {"verification_failed", "verification_succeeded"},
    "verification_failed": {"verification_failed", "verification_succeeded"},
    "verification_succeeded": {"verification_failed", "cleanup_completed"},
    "cleanup_completed": {"verification_failed", "completed"},
    "completed": set(),
}


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
        self, destination: S3BackupDestination, credentials: Credentials, key: str,
        version_id: str | None = None,
    ) -> ObjectMetadata | None: ...

    def get_object(
        self, destination: S3BackupDestination, credentials: Credentials, key: str,
        version_id: str | None = None,
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
        self, destination: S3BackupDestination, credentials: Credentials, key: str,
        version_id: str | None = None,
    ) -> ObjectMetadata | None:
        try:
            kwargs: dict[str, str] = {"VersionId": version_id} if version_id is not None else {}
            response = self._client(destination, credentials).head_object(
                Bucket=destination.bucket, Key=key, **kwargs
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
            version_id=response.get("VersionId"),
        )

    def get_object(
        self, destination: S3BackupDestination, credentials: Credentials, key: str,
        version_id: str | None = None,
    ) -> bytes:
        try:
            kwargs: dict[str, str] = {"VersionId": version_id} if version_id is not None else {}
            response = self._client(destination, credentials).get_object(
                Bucket=destination.bucket, Key=key, **kwargs
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


def safety_recovery_point_id(
    deployment: str, destination: str, restore_request_id: str
) -> str:
    digest = hashlib.sha256(
        f"gimme-safety-recovery-point-v1\0{deployment}\0{destination}\0"
        f"{restore_request_id}".encode()
    ).hexdigest()[:20]
    return f"rp_{digest}"


def component_key(deployment: str, point_id: str, component: str) -> str:
    return f"{RECOVERY_POINT_PREFIX}/{deployment}/{point_id}/{component}.dump"


def manifest_key(deployment: str, point_id: str) -> str:
    return f"{RECOVERY_POINT_PREFIX}/{deployment}/{point_id}/manifest.json"


def restore_event_key(deployment: str, request_id: str, sequence: int) -> str:
    return f"{RESTORE_PREFIX}/{deployment}/{request_id}/{sequence:06d}.json"


def _validate_restore_event(document: object) -> dict[str, object]:
    common_keys = {
        "schema_version", "deployment", "request_id", "sequence", "state", "created_at",
        "source_recovery_point_id", "destination", "safety_recovery_point_id",
    }
    if not isinstance(document, dict) or (
        document.get("schema_version") == 2 and set(document) != common_keys
    ) or (
        document.get("schema_version") == 3 and set(document) != common_keys | {
            "selected_components", "untouched_components", "partial",
        }
    ) or (
        document.get("schema_version") == 4 and set(document) != common_keys | {
            "selected_components", "untouched_components", "partial",
            "destinations", "request_fingerprint",
        }
    ) or (
        document.get("schema_version") == 5 and set(document) != common_keys | {
            "selected_components", "untouched_components", "partial",
            "destinations", "request_fingerprint", "safety_components",
        }
    ) or document.get("schema_version") not in {2, 3, 4, 5}:
        raise RecoveryError("restore_record_invalid")
    destination = document.get("destination")
    if (
        not isinstance(document.get("deployment"), str)
        or not isinstance(document.get("request_id"), str)
        or REQUEST_ID.fullmatch(str(document["request_id"])) is None
        or not isinstance(document.get("sequence"), int)
        or isinstance(document.get("sequence"), bool)
        or not 0 <= int(document["sequence"]) <= 999999
        or document.get("state") not in RESTORE_STATES
        or RECOVERY_POINT_ID.fullmatch(str(document.get("source_recovery_point_id"))) is None
        or not isinstance(destination, dict)
        or set(destination) != {"resource", "provider", "kind", "version"}
        or not isinstance(destination.get("resource"), str)
        or re.fullmatch(r"[a-z][a-z0-9-]{0,63}", str(destination.get("resource"))) is None
        or destination.get("kind") not in {"postgres", "valkey"}
        or (
            destination.get("kind") == "postgres"
            and destination.get("provider") != "target_local"
        )
        or (
            destination.get("kind") == "valkey"
            and destination.get("provider") not in {
                "target_local", "aws_elasticache_valkey"
            }
        )
        or RESOURCE_VERSION.fullmatch(str(destination.get("version"))) is None
        or (
            document.get("safety_recovery_point_id") is not None
            and RECOVERY_POINT_ID.fullmatch(
                str(document.get("safety_recovery_point_id"))
            ) is None
        )
    ):
        raise RecoveryError("restore_record_invalid")
    try:
        created_at = datetime.fromisoformat(str(document.get("created_at")))
    except ValueError:
        raise RecoveryError("restore_record_invalid") from None
    if created_at.tzinfo is None:
        raise RecoveryError("restore_record_invalid")
    if document["schema_version"] == 2:
        document = {
            **document,
            "selected_components": ["postgres"],
            "untouched_components": [],
            "partial": False,
        }
    if document["schema_version"] in {2, 3}:
        document = {
            **document,
            "destinations": [destination],
            "request_fingerprint": None,
        }
    if document["schema_version"] in {2, 3, 4}:
        document = {
            **document,
            "safety_components": (
                [] if document["safety_recovery_point_id"] is None
                else list(document["selected_components"])
            ),
        }
    selected = document.get("selected_components")
    untouched = document.get("untouched_components")
    destinations = document.get("destinations")
    safety_components = document.get("safety_components")
    if (
        not isinstance(selected, list)
        or not 1 <= len(selected) <= 2
        or len(selected) != len(set(selected))
        or not set(selected) <= {"postgres", "valkey"}
        or not isinstance(untouched, list)
        or len(untouched) > 1
        or len(untouched) != len(set(untouched))
        or not set(untouched) <= {"postgres", "valkey"}
        or set(selected) & set(untouched)
        or set(selected) | set(untouched) not in (
            {"postgres"}, {"valkey"}, {"postgres", "valkey"}
        )
        or destination["kind"] not in selected
        or not isinstance(destinations, list)
        or any(
            not isinstance(item, dict)
            or set(item) != {"resource", "provider", "kind", "version"}
            or not isinstance(item.get("resource"), str)
            or re.fullmatch(
                r"[a-z][a-z0-9-]{0,63}", str(item.get("resource"))
            ) is None
            or item.get("kind") not in {"postgres", "valkey"}
            or item.get("kind") == "postgres" and item.get("provider") != "target_local"
            or item.get("kind") == "valkey" and item.get("provider") not in {
                "target_local", "aws_elasticache_valkey"
            }
            or RESOURCE_VERSION.fullmatch(str(item.get("version"))) is None
            for item in destinations
        )
        or (
            document["schema_version"] in {4, 5}
            and document.get("request_fingerprint") is not None
            and (
                len(destinations) != len(selected)
                or [item["kind"] for item in destinations] != selected
                or destinations[0] != destination
            )
        )
        or (
            (
                document["schema_version"] in {2, 3}
                or document.get("request_fingerprint") is None
            )
            and destinations != [destination]
        )
        or (
            document.get("request_fingerprint") is not None
            and re.fullmatch(
                r"plan_[0-9a-f]{20}", str(document.get("request_fingerprint"))
            ) is None
        )
        or document.get("partial") != bool(untouched)
        or not isinstance(safety_components, list)
        or len(safety_components) != len(set(safety_components))
        or not set(safety_components) <= set(selected)
        or (document.get("safety_recovery_point_id") is None) != (
            safety_components == []
        )
    ):
        raise RecoveryError("restore_record_invalid")
    return document


def _restore_events(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
    deployment: str, request_id: str,
) -> list[dict[str, object]]:
    if REQUEST_ID.fullmatch(request_id) is None:
        raise RecoveryError("restore_request_identity_invalid")
    prefix = f"{RESTORE_PREFIX}/{deployment}/{request_id}/"
    keys = sorted(adapter.list_keys(destination, credentials, prefix))
    if len(keys) > 100:
        raise RecoveryError("restore_record_limit")
    events: list[dict[str, object]] = []
    for sequence, key in enumerate(keys):
        if key != restore_event_key(deployment, request_id, sequence):
            raise RecoveryError("restore_record_invalid")
        metadata = adapter.head_object(destination, credentials, key)
        if (
            metadata is None or metadata.bytes > MAX_MANIFEST_BYTES
            or metadata.version_id is None
            or VERSION_ID.fullmatch(metadata.version_id) is None
            or metadata.server_side_encryption == ""
        ):
            raise RecoveryError("restore_record_invalid")
        raw = adapter.get_object(destination, credentials, key, metadata.version_id)
        if (
            len(raw) > MAX_MANIFEST_BYTES
            or hashlib.sha256(raw).hexdigest() != metadata.sha256
        ):
            raise RecoveryError("restore_record_invalid")
        try:
            event = _validate_restore_event(json.loads(raw))
        except (json.JSONDecodeError, UnicodeError):
            raise RecoveryError("restore_record_invalid") from None
        if (
            event["deployment"] != deployment
            or event["request_id"] != request_id
            or event["sequence"] != sequence
        ):
            raise RecoveryError("restore_record_invalid")
        events.append(event)
    return events


def append_restore_event(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
    deployment: str, request_id: str, state: str, *,
    source_recovery_point_id: str, destination_provider: str,
    destination_resource: str, destination_kind: str, destination_version: str,
    safety_recovery_point_id: str | None = None,
    selected_components: list[str] | None = None,
    untouched_components: list[str] | None = None,
    partial: bool = False,
    destinations: list[dict[str, object]] | None = None,
    request_fingerprint: str | None = None,
    safety_components: list[str] | None = None,
) -> dict[str, object]:
    """Append and round-trip one immutable, secret-safe Restore transition."""
    events = _restore_events(destination, credentials, adapter, deployment, request_id)
    previous = events[-1] if events else None
    normalized_selected = (
        ["postgres"] if selected_components is None else selected_components
    )
    identity = {
        "source_recovery_point_id": source_recovery_point_id,
        "destination": {
            "resource": destination_resource,
            "provider": destination_provider,
            "kind": destination_kind,
            "version": destination_version,
        },
        "safety_recovery_point_id": safety_recovery_point_id,
        "selected_components": normalized_selected,
        "untouched_components": (
            [] if untouched_components is None else untouched_components
        ),
        "partial": partial,
        "destinations": (
            [{
                "resource": destination_resource,
                "provider": destination_provider,
                "kind": destination_kind,
                "version": destination_version,
            }] if destinations is None else destinations
        ),
        "request_fingerprint": request_fingerprint,
        "safety_components": (
            normalized_selected
            if safety_components is None and safety_recovery_point_id is not None
            else [] if safety_components is None else safety_components
        ),
    }
    if previous is not None and any(previous[key] != value for key, value in identity.items()):
        raise RecoveryError("restore_request_conflict")
    previous_state = None if previous is None else str(previous["state"])
    if state not in RESTORE_TRANSITIONS.get(previous_state, set()):
        raise RecoveryError("restore_transition_invalid")
    sequence = len(events)
    event = _validate_restore_event({
        "schema_version": 5, "deployment": deployment, "request_id": request_id,
        "sequence": sequence, "state": state, "created_at": datetime.now(UTC).isoformat(),
        **identity,
    })
    encoded = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    key = restore_event_key(deployment, request_id, sequence)
    if adapter.head_object(destination, credentials, key) is not None:
        raise RecoveryError("restore_record_conflict")
    written = adapter.put_object(
        destination, credentials, key, encoded, hashlib.sha256(encoded).hexdigest()
    )
    if (
        written.version_id is None
        or VERSION_ID.fullmatch(written.version_id) is None
        or written.server_side_encryption == ""
        or adapter.get_object(destination, credentials, key, written.version_id) != encoded
    ):
        raise RecoveryError("restore_record_verification_failed")
    return event


def load_restore_record(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
    deployment: str, request_id: str,
) -> dict[str, object]:
    events = _restore_events(destination, credentials, adapter, deployment, request_id)
    if not events:
        raise RecoveryError("restore_record_missing")
    latest = events[-1]
    return {
        "deployment": latest["deployment"], "request_id": latest["request_id"],
        "source_recovery_point_id": latest["source_recovery_point_id"],
        "destination": latest["destination"], "state": latest["state"],
        "updated_at": latest["created_at"], "events": len(events),
        "safety_recovery_point_id": next(
            (event["safety_recovery_point_id"] for event in reversed(events)
             if event["safety_recovery_point_id"] is not None),
            None,
        ),
        "selected_components": latest["selected_components"],
        "untouched_components": latest["untouched_components"],
        "partial": latest["partial"],
        "destinations": latest["destinations"],
        "request_fingerprint": latest["request_fingerprint"],
        "safety_components": latest["safety_components"],
    }


def list_restore_records(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
    deployment: str,
) -> list[dict[str, object]]:
    prefix = f"{RESTORE_PREFIX}/{deployment}/"
    keys = adapter.list_keys(destination, credentials, prefix)
    if len(keys) > 10_000:
        raise RecoveryError("restore_record_limit")
    request_ids: set[str] = set()
    for key in keys:
        remainder = key[len(prefix):]
        request_id, separator, event_name = remainder.partition("/")
        if (
            separator != "/" or REQUEST_ID.fullmatch(request_id) is None
            or re.fullmatch(r"[0-9]{6}\.json", event_name) is None
        ):
            raise RecoveryError("restore_record_invalid")
        request_ids.add(request_id)
    if len(request_ids) > 100:
        raise RecoveryError("restore_record_limit")
    records = [
        load_restore_record(destination, credentials, adapter, deployment, request_id)
        for request_id in request_ids
    ]
    records.sort(key=lambda record: str(record["updated_at"]), reverse=True)
    return records


def recovery_point_source_protected(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
    deployment: str, point_id: str,
) -> bool:
    """Protect a source point while any schema-v2 Restore using it is incomplete."""
    prefix = f"{RESTORE_PREFIX}/{deployment}/"
    keys = adapter.list_keys(destination, credentials, prefix)
    if len(keys) > 10_000:
        raise RecoveryError("restore_record_limit")
    request_ids: set[str] = set()
    for key in keys:
        remainder = key[len(prefix):]
        request_id, separator, event_name = remainder.partition("/")
        if (
            separator != "/" or REQUEST_ID.fullmatch(request_id) is None
            or re.fullmatch(r"[0-9]{6}\.json", event_name) is None
        ):
            raise RecoveryError("restore_record_invalid")
        request_ids.add(request_id)
    for request_id in request_ids:
        try:
            record = load_restore_record(
                destination, credentials, adapter, deployment, request_id
            )
        except RecoveryError as exc:
            if str(exc) != "restore_record_invalid":
                raise
            request_keys = sorted(
                key for key in keys
                if key.startswith(f"{prefix}{request_id}/")
            )
            legacy = True
            for sequence, key in enumerate(request_keys):
                metadata = adapter.head_object(destination, credentials, key)
                if (
                    metadata is None
                    or metadata.bytes > MAX_MANIFEST_BYTES
                    or metadata.version_id is None
                    or VERSION_ID.fullmatch(metadata.version_id) is None
                    or metadata.server_side_encryption == ""
                ):
                    legacy = False
                    break
                try:
                    raw = adapter.get_object(
                        destination, credentials, key, metadata.version_id
                    )
                    if (
                        len(raw) > MAX_MANIFEST_BYTES
                        or hashlib.sha256(raw).hexdigest() != metadata.sha256
                    ):
                        legacy = False
                        break
                    event = json.loads(raw)
                except (json.JSONDecodeError, UnicodeError):
                    legacy = False
                    break
                if (
                    not isinstance(event, dict)
                    or set(event) != {
                        "schema_version", "deployment", "request_id", "sequence",
                        "state", "safety_recovery_point_id",
                    }
                    or event.get("schema_version") != 1
                    or event.get("deployment") != deployment
                    or event.get("request_id") != request_id
                    or event.get("sequence") != sequence
                    or event.get("state") not in RESTORE_STATES
                    or (
                        event.get("safety_recovery_point_id") is not None
                        and RECOVERY_POINT_ID.fullmatch(
                            str(event.get("safety_recovery_point_id"))
                        ) is None
                    )
                    or key != restore_event_key(deployment, request_id, sequence)
                ):
                    legacy = False
                    break
            if legacy:
                continue
            raise
        if (
            record["source_recovery_point_id"] == point_id
            and record["state"] != "completed"
        ):
            return True
    return False


def public_recovery_point(manifest: dict[str, object]) -> dict[str, object]:
    """Project a private manifest without storage identities or checksums."""
    return {
        "recovery_point_id": manifest["recovery_point_id"],
        "deployment": manifest["deployment"],
        "destination": manifest["destination"],
        "created_at": manifest["created_at"],
        "safety": manifest["safety"],
        "restore_request_id": manifest["restore_request_id"],
        "components": [
            {
                "kind": item["kind"],
                "bytes": item["bytes"],
                "format": item["format"],
                "records": item["records"],
                "captured_at": item["captured_at"],
                "resource_kind": item["resource_kind"],
                "resource_version": item["resource_version"],
            }
            for item in manifest["components"]  # type: ignore[union-attr]
        ],
        **{
            key: manifest[key]
            for key in ("state", "deleted_components", "remaining_components")
            if key in manifest
        },
    }


def safety_recovery_point_protected(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
    deployment: str, manifest: dict[str, object],
) -> bool:
    """Fail closed unless the Safety point's authoritative Restore stream completed."""
    if manifest.get("safety") is not True:
        return False
    request_id = manifest.get("restore_request_id")
    if not isinstance(request_id, str) or REQUEST_ID.fullmatch(request_id) is None:
        return True
    prefix = f"{RESTORE_PREFIX}/{deployment}/{request_id}/"
    keys = sorted(adapter.list_keys(destination, credentials, prefix))
    if not keys or len(keys) > 100:
        return True
    try:
        events = _restore_events(destination, credentials, adapter, deployment, request_id)
        latest = events[-1]
        safety_ids = {
            event["safety_recovery_point_id"]
            for event in events if event["safety_recovery_point_id"] is not None
        }
        return not (
            latest["state"] == "completed"
            and safety_ids == {manifest["recovery_point_id"]}
        )
    except RecoveryError:
        # Schema 1 records were emitted by the recovery-point deletion foundation.
        # Continue with its strict parser so already-published safety points retain
        # their original protection semantics.
        pass
    latest_sequence = -1
    latest_state = ""
    allowed_states = {
        "started", "maintenance_entered", "safety_verified", "safety_not_required",
        "artifact_verified", "shadow_verified", "data_replaced", "verification_failed",
        "completed",
    }
    try:
        for key in keys:
            raw = adapter.get_object(destination, credentials, key)
            if len(raw) > MAX_MANIFEST_BYTES:
                return True
            event = json.loads(raw)
            if (
                not isinstance(event, dict)
                or set(event) != {
                    "schema_version", "deployment", "request_id", "sequence", "state",
                    "safety_recovery_point_id",
                }
                or event["schema_version"] != 1
                or event["deployment"] != deployment
                or event["request_id"] != request_id
                or event["safety_recovery_point_id"] != manifest["recovery_point_id"]
                or not isinstance(event["sequence"], int)
                or not 0 <= event["sequence"] <= 999999
                or event["state"] not in allowed_states
                or key != restore_event_key(deployment, request_id, event["sequence"])
            ):
                return True
            if event["sequence"] > latest_sequence:
                latest_sequence = event["sequence"]
                latest_state = event["state"]
    except Exception:
        return True
    return latest_state != "completed"


@dataclass(frozen=True)
class ComponentDump:
    kind: str
    local_path: Path
    sha256: str
    bytes: int
    resource_version: str
    format: str = "pg-custom-v1"
    records: int | None = None
    captured_at: str | None = None


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


def materialize_recovery_component(
    destination_name: str, destination: S3BackupDestination, credentials: Credentials,
    adapter: S3Adapter, deployment: str, point_id: str, kind: str, path: Path,
) -> dict[str, object]:
    """Write one exact manifest-owned component to a protected local file."""
    if path.exists() or path.is_symlink() or not path.is_absolute():
        raise RecoveryError("restore_artifact_path_invalid")
    manifest = _load_manifest(
        destination, credentials, adapter, deployment, destination_name, point_id
    )
    component = next(
        (item for item in manifest["components"] if item["kind"] == kind),  # type: ignore[union-attr]
        None,
    )
    if component is None:
        raise RecoveryError("restore_component_missing")
    if int(component["bytes"]) > MAX_COMPONENT_BYTES:
        raise RecoveryError("restore_component_too_large")
    body = adapter.get_object(
        destination, credentials, str(component["key"]), str(component["version_id"])
    )
    if (
        len(body) != component["bytes"]
        or len(body) > MAX_COMPONENT_BYTES
        or hashlib.sha256(body).hexdigest() != component["sha256"]
    ):
        raise RecoveryError("restore_component_verification_failed")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return component


def create_recovery_point(
    destination_name: str, destination: S3BackupDestination, credentials: Credentials,
    adapter: S3Adapter, deployment: str, point_id: str,
    dump: ComponentDump | list[ComponentDump], *,
    safety_restore_request_id: str | None = None,
    before_publish: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Upload every verified component and publish one immutable manifest last."""
    existing = find_recovery_point(
        destination_name, destination, credentials, adapter, deployment, point_id
    )
    if existing is not None:
        return existing
    if (
        safety_restore_request_id is not None
        and REQUEST_ID.fullmatch(safety_restore_request_id) is None
    ):
        raise RecoveryError("restore_request_identity_invalid")
    dumps = [dump] if isinstance(dump, ComponentDump) else list(dump)
    if (
        not dumps or len(dumps) > 8
        or len({item.kind for item in dumps}) != len(dumps)
        or any(COMPONENT_KIND.fullmatch(item.kind) is None for item in dumps)
    ):
        raise RecoveryError("recovery_component_kind_invalid")
    uploaded: list[tuple[str, ObjectMetadata]] = []
    components: list[dict[str, object]] = []

    def cleanup_uploaded() -> None:
        for uploaded_key, metadata in reversed(uploaded):
            if metadata.version_id is not None:
                with suppress(Exception):
                    adapter.delete_object(
                        destination, credentials, uploaded_key,
                        version_id=metadata.version_id,
                    )

    try:
        for item in sorted(dumps, key=lambda value: value.kind):
            key = component_key(deployment, point_id, item.kind)
            body = item.local_path.read_bytes()
            if len(body) != item.bytes or hashlib.sha256(body).hexdigest() != item.sha256:
                raise RecoveryError("recovery_component_checksum_mismatch")
            written = adapter.put_object(
                destination, credentials, key, body, item.sha256
            )
            uploaded.append((key, written))
            if written.version_id is None or VERSION_ID.fullmatch(written.version_id) is None:
                raise RecoveryError("recovery_component_version_missing")
            confirmed = adapter.head_object(
                destination, credentials, key, written.version_id
            )
            if confirmed is None or confirmed.bytes != written.bytes:
                raise RecoveryError("recovery_component_verification_failed")
            # Metadata is untrusted: hash the exact uploaded version's bytes as well.
            if hashlib.sha256(
                adapter.get_object(destination, credentials, key, written.version_id)
            ).hexdigest() != item.sha256:
                raise RecoveryError("recovery_component_verification_failed")
            components.append({
                "kind": item.kind,
                "key": key,
                "bytes": item.bytes,
                "sha256": item.sha256,
                "version_id": written.version_id,
                "format": item.format,
                "records": item.records,
                "captured_at": item.captured_at or datetime.now(UTC).isoformat(),
                "resource_kind": item.kind,
                "resource_version": item.resource_version,
            })
    except Exception:
        cleanup_uploaded()
        raise
    manifest = {
        "schema_version": 3,
        "recovery_point_id": point_id,
        "deployment": deployment,
        "destination": destination_name,
        "created_at": datetime.now(UTC).isoformat(),
        "safety": safety_restore_request_id is not None,
        "restore_request_id": safety_restore_request_id,
        "components": components,
    }
    try:
        _validate_manifest(manifest)
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        if before_publish is not None:
            before_publish()
    except Exception:
        cleanup_uploaded()
        raise
    if len(encoded) > MAX_MANIFEST_BYTES:
        for key, metadata in reversed(uploaded):
            adapter.delete_object(
                destination, credentials, key, version_id=metadata.version_id
            )
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
            cleanup_uploaded()
        raise
    return manifest


def _validate_manifest(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "recovery_point_id", "deployment", "destination",
        "created_at", "safety", "restore_request_id", "components",
    }:
        raise RecoveryError("recovery_manifest_invalid")
    if document.get("schema_version") != 3:
        raise RecoveryError("recovery_manifest_invalid")
    if RECOVERY_POINT_ID.fullmatch(str(document.get("recovery_point_id"))) is None:
        raise RecoveryError("recovery_manifest_invalid")
    try:
        created_at = datetime.fromisoformat(str(document.get("created_at")))
    except ValueError:
        raise RecoveryError("recovery_manifest_invalid") from None
    if created_at.tzinfo is None:
        raise RecoveryError("recovery_manifest_invalid")
    components = document.get("components")
    safety = document.get("safety")
    restore_request_id = document.get("restore_request_id")
    if (
        not isinstance(safety, bool)
        or (safety and (
            not isinstance(restore_request_id, str)
            or REQUEST_ID.fullmatch(restore_request_id) is None
        ))
        or (not safety and restore_request_id is not None)
    ):
        raise RecoveryError("recovery_manifest_invalid")
    if not isinstance(components, list) or not components or len(components) > 8:
        raise RecoveryError("recovery_manifest_invalid")
    seen_kinds: set[str] = set()
    for component in components:
        if (
            not isinstance(component, dict)
            or set(component) != {
                "kind", "key", "bytes", "sha256", "version_id", "format", "records",
                "captured_at", "resource_kind", "resource_version",
            }
            or COMPONENT_KIND.fullmatch(str(component.get("kind"))) is None
            or not isinstance(component.get("key"), str)
            or not isinstance(component.get("bytes"), int)
            or isinstance(component.get("bytes"), bool)
            or component["bytes"] < 0
            or SHA256_HEX.fullmatch(str(component.get("sha256"))) is None
            or VERSION_ID.fullmatch(str(component.get("version_id"))) is None
            or FORMAT_ID.fullmatch(str(component.get("format"))) is None
            or (
                component.get("records") is not None
                and (
                    not isinstance(component["records"], int)
                    or isinstance(component["records"], bool)
                    or component["records"] < 0
                    or component["records"] > 100_000
                )
            )
            or not isinstance(component.get("captured_at"), str)
            or not 1 <= len(component["captured_at"]) <= 64
            or component.get("resource_kind") not in {"postgres", "valkey"}
            or RESOURCE_VERSION.fullmatch(str(component.get("resource_version"))) is None
        ):
            raise RecoveryError("recovery_manifest_invalid")
        kind = str(component["kind"])
        if kind in seen_kinds:
            raise RecoveryError("recovery_manifest_invalid")
        seen_kinds.add(kind)
        if (
            component["kind"] == "postgres"
            and (
                component["format"] != "pg-custom-v1"
                or component["records"] is not None
                or component["resource_kind"] != "postgres"
            )
        ) or (
            component["kind"] == "valkey"
            and (
                component["format"] != "gimme-valkey-v1"
                or not isinstance(component["records"], int)
                or component["resource_kind"] != "valkey"
            )
        ):
            raise RecoveryError("recovery_manifest_invalid")
        try:
            captured_at = datetime.fromisoformat(component["captured_at"])
        except ValueError:
            raise RecoveryError("recovery_manifest_invalid") from None
        if captured_at.tzinfo is None:
            raise RecoveryError("recovery_manifest_invalid")
    return document


def _load_manifest_record(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
    deployment: str, destination_name: str, point_id: str, *, allow_missing: bool = False,
) -> tuple[dict[str, object], ObjectMetadata, int]:
    key = manifest_key(deployment, point_id)
    # Check size via head_object before ever reading the body: an object planted at a
    # manifest key with an oversized body must not be pulled fully into memory to reject it.
    probe = adapter.head_object(destination, credentials, key)
    if probe is None:
        raise RecoveryError("recovery_manifest_missing")
    if probe.bytes > MAX_MANIFEST_BYTES:
        raise RecoveryError("recovery_manifest_too_large")
    if probe.version_id is None or VERSION_ID.fullmatch(probe.version_id) is None:
        raise RecoveryError("recovery_manifest_version_missing")
    raw = adapter.get_object(destination, credentials, key, probe.version_id)
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
    missing = 0
    seen: set[tuple[str, str]] = set()
    for component in manifest["components"]:  # type: ignore[union-attr]
        component_key_value = str(component["key"])
        version_id = str(component["version_id"])
        if component_key_value != component_key(
            deployment, point_id, str(component["kind"])
        ):
            raise RecoveryError("recovery_manifest_invalid")
        identity = component_key_value, version_id
        if identity in seen:
            raise RecoveryError("recovery_manifest_invalid")
        seen.add(identity)
        confirmed = adapter.head_object(
            destination, credentials, component_key_value, version_id
        )
        if confirmed is None:
            missing += 1
            continue
        if (
            confirmed.bytes != component["bytes"]
            or confirmed.sha256 != component["sha256"]
        ):
            raise RecoveryError("recovery_manifest_tampered")
    if missing and not allow_missing:
        raise RecoveryError("recovery_manifest_tampered")
    return manifest, probe, missing


def _load_manifest(
    destination: S3BackupDestination, credentials: Credentials, adapter: S3Adapter,
    deployment: str, destination_name: str, point_id: str,
) -> dict[str, object]:
    return _load_manifest_record(
        destination, credentials, adapter, deployment, destination_name, point_id
    )[0]


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
            manifest, _metadata, missing = _load_manifest_record(
                destination, credentials, adapter, deployment, destination_name, point_id,
                allow_missing=True,
            )
            manifests.append({
                **manifest,
                "state": "deletion_failed" if missing else "verified",
                "deleted_components": missing,
                "remaining_components": len(manifest["components"]) - missing,
            })
        except RecoveryError:
            rejected.append(point_id if RECOVERY_POINT_ID.fullmatch(point_id) else "unknown")
    manifests.sort(key=lambda item: str(item["created_at"]), reverse=True)
    return {
        "destination": destination_name,
        "deployment": deployment,
        "recovery_points": manifests,
        "rejected": sorted(set(rejected)),
    }


def recovery_point_deletion_targets(
    destination_name: str, destination: S3BackupDestination, credentials: Credentials,
    adapter: S3Adapter, deployment: str, point_id: str,
) -> dict[str, object]:
    """Resolve one immutable manifest into the only objects a future delete may touch."""
    manifest, metadata, missing = _load_manifest_record(
        destination, credentials, adapter, deployment, destination_name, point_id,
        allow_missing=True,
    )
    components = manifest["components"]
    return {
        "recovery_point_id": point_id,
        "components": len(components),
        "bytes": sum(int(component["bytes"]) for component in components),
        "missing_components": missing,
        "manifest_version_id": metadata.version_id,
        "manifest": manifest,
    }


def delete_recovery_point_versions(
    destination_name: str, destination: S3BackupDestination, credentials: Credentials,
    adapter: S3Adapter, deployment: str, point_id: str,
) -> dict[str, object]:
    """Delete only exact versions authorized by a validated manifest, manifest last."""
    manifest, manifest_metadata, _missing = _load_manifest_record(
        destination, credentials, adapter, deployment, destination_name, point_id,
        allow_missing=True,
    )
    components = sorted(
        manifest["components"], key=lambda item: (str(item["kind"]), str(item["key"]))
    )
    deleted = 0
    def deletion_error(exc: Exception) -> RecoveryError:
        if isinstance(exc, RecoveryError) and "access_denied" in str(exc):
            return RecoveryError("recovery_point_deletion_denied")
        return RecoveryError("recovery_point_deletion_failed")

    try:
        for component in components:
            key = str(component["key"])
            version_id = str(component["version_id"])
            if adapter.head_object(destination, credentials, key, version_id) is None:
                deleted += 1
                continue
            adapter.delete_object(destination, credentials, key, version_id)
            if adapter.head_object(destination, credentials, key, version_id) is not None:
                raise RecoveryError("recovery_point_deletion_failed")
            deleted += 1
    except Exception as exc:
        raise deletion_error(exc) from None
    manifest_version = manifest_metadata.version_id
    if manifest_version is None:  # guarded by _load_manifest_record; keeps typing explicit
        raise RecoveryError("recovery_manifest_version_missing")
    manifest_object_key = manifest_key(deployment, point_id)
    try:
        adapter.delete_object(
            destination, credentials, manifest_object_key, manifest_version
        )
        if adapter.head_object(
            destination, credentials, manifest_object_key, manifest_version
        ) is not None:
            raise RecoveryError("recovery_point_deletion_failed")
    except Exception as exc:
        raise deletion_error(exc) from None
    return {
        "recovery_point_id": point_id,
        "state": "deleted",
        "deleted_components": deleted,
    }
