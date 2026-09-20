"""Deterministic, binary-safe archives for one registered Deployment Valkey prefix."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Protocol


FORMAT = "gimme-valkey-v1"
MAX_KEYS = 100_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_SCAN_ITERATIONS = 100_000
MAX_CLEAR_PASSES = 100
UNLINK_BATCH = 1_000


class ValkeyArchiveError(RuntimeError):
    """A bounded error which never includes key or value material."""


class CaptureAdapter(Protocol):
    def scan(self, cursor: int) -> tuple[int, list[bytes]]: ...

    def capture(self, key: bytes) -> tuple[bytes | None, int | None]:
        """Atomically return DUMP bytes and absolute expiry milliseconds."""


class RestoreAdapter(CaptureAdapter, Protocol):
    def unlink(self, keys: list[bytes]) -> None: ...

    def restore(
        self, key: bytes, payload: bytes, expires_at_ms: int | None
    ) -> None: ...

    def server_time_ms(self) -> int: ...


@dataclass(frozen=True)
class ValkeyArchive:
    body: bytes
    sha256: str
    keys: int
    bytes: int


@dataclass(frozen=True)
class ValkeyRecord:
    key: bytes
    payload: bytes
    expires_at_ms: int | None


@dataclass(frozen=True)
class ValkeyRestore:
    restored: int
    expired: int


def _encoded_record(key: bytes, payload: bytes, expires_at_ms: int | None) -> bytes:
    return json.dumps(
        {
            "dump": base64.b64encode(payload).decode("ascii"),
            "expires_at_ms": expires_at_ms,
            "key": base64.b64encode(key).decode("ascii"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def capture_archive(
    adapter: CaptureAdapter, registered_prefix: bytes, *, now_ms: int
) -> ValkeyArchive:
    """Scan only the registered prefix and serialize each key's atomic DUMP snapshot."""
    if not registered_prefix or len(registered_prefix) > 160:
        raise ValkeyArchiveError("valkey_capture_policy_invalid")
    cursor = 0
    iterations = 0
    seen: set[bytes] = set()
    records: list[tuple[bytes, bytes]] = []
    while True:
        iterations += 1
        if iterations > MAX_SCAN_ITERATIONS:
            raise ValkeyArchiveError("valkey_capture_scan_unbounded")
        cursor, keys = adapter.scan(cursor)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValkeyArchiveError("valkey_capture_protocol_invalid")
        for key in keys:
            if not isinstance(key, bytes) or not key.startswith(registered_prefix):
                raise ValkeyArchiveError("valkey_capture_isolation_failed")
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > MAX_KEYS:
                raise ValkeyArchiveError("valkey_capture_too_large")
            payload, expires_at_ms = adapter.capture(key)
            if payload is None:
                continue
            if not isinstance(payload, bytes) or (
                expires_at_ms is not None
                and (
                    not isinstance(expires_at_ms, int)
                    or isinstance(expires_at_ms, bool)
                    or expires_at_ms < 0
                )
            ):
                raise ValkeyArchiveError("valkey_capture_protocol_invalid")
            if expires_at_ms is not None and expires_at_ms <= now_ms:
                continue
            records.append((key, _encoded_record(key, payload, expires_at_ms)))
        if cursor == 0:
            break
    header = json.dumps(
        {"format": FORMAT}, sort_keys=True, separators=(",", ":")
    ).encode()
    body = b"\n".join([header, *(record for _key, record in sorted(records))]) + b"\n"
    if len(body) > MAX_ARCHIVE_BYTES:
        raise ValkeyArchiveError("valkey_capture_too_large")
    verify_archive(body)
    return ValkeyArchive(
        body=body, sha256=hashlib.sha256(body).hexdigest(),
        keys=len(records), bytes=len(body),
    )


def verify_archive(body: bytes) -> int:
    """Verify the bounded deterministic archive structure without exposing its records."""
    if not body or len(body) > MAX_ARCHIVE_BYTES or not body.endswith(b"\n"):
        raise ValkeyArchiveError("valkey_archive_invalid")
    lines = body.splitlines()
    try:
        header = json.loads(lines[0])
        if header != {"format": FORMAT} or len(lines) - 1 > MAX_KEYS:
            raise ValkeyArchiveError("valkey_archive_invalid")
        previous: bytes | None = None
        for line in lines[1:]:
            record = json.loads(line)
            if not isinstance(record, dict) or set(record) != {
                "dump", "expires_at_ms", "key"
            }:
                raise ValkeyArchiveError("valkey_archive_invalid")
            key = base64.b64decode(record["key"], validate=True)
            base64.b64decode(record["dump"], validate=True)
            expiry = record["expires_at_ms"]
            if expiry is not None and (
                not isinstance(expiry, int) or isinstance(expiry, bool) or expiry < 0
            ):
                raise ValkeyArchiveError("valkey_archive_invalid")
            if previous is not None and key <= previous:
                raise ValkeyArchiveError("valkey_archive_invalid")
            previous = key
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ValkeyArchiveError("valkey_archive_invalid") from None
    return len(lines) - 1


def archive_records(
    body: bytes, registered_prefix: bytes, *, expected_records: int
) -> tuple[ValkeyRecord, ...]:
    """Decode a fully bounded archive and prove every record belongs to one prefix."""
    if (
        not registered_prefix
        or len(registered_prefix) > 160
        or not 0 <= expected_records <= MAX_KEYS
        or verify_archive(body) != expected_records
    ):
        raise ValkeyArchiveError("valkey_archive_invalid")
    records: list[ValkeyRecord] = []
    try:
        for line in body.splitlines()[1:]:
            raw = json.loads(line)
            key = base64.b64decode(raw["key"], validate=True)
            payload = base64.b64decode(raw["dump"], validate=True)
            if not key.startswith(registered_prefix) or not payload:
                raise ValkeyArchiveError("valkey_archive_invalid")
            records.append(ValkeyRecord(key, payload, raw["expires_at_ms"]))
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise ValkeyArchiveError("valkey_archive_invalid") from None
    return tuple(records)


def _scan_prefix(adapter: CaptureAdapter, prefix: bytes) -> list[bytes]:
    cursor = 0
    iterations = 0
    seen: set[bytes] = set()
    while True:
        iterations += 1
        if iterations > MAX_SCAN_ITERATIONS:
            raise ValkeyArchiveError("valkey_restore_scan_unbounded")
        cursor, keys = adapter.scan(cursor)
        if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
            raise ValkeyArchiveError("valkey_restore_protocol_invalid")
        for key in keys:
            if not isinstance(key, bytes) or not key.startswith(prefix):
                raise ValkeyArchiveError("valkey_restore_isolation_failed")
            seen.add(key)
            if len(seen) > MAX_KEYS:
                raise ValkeyArchiveError("valkey_restore_too_large")
        if cursor == 0:
            return sorted(seen)


def replace_archive(
    adapter: RestoreAdapter,
    registered_prefix: bytes,
    body: bytes,
    *,
    expected_records: int,
) -> ValkeyRestore:
    """Clear, replay, and verify exactly one registered prefix from a checked archive."""
    records = archive_records(
        body, registered_prefix, expected_records=expected_records
    )
    for _pass in range(MAX_CLEAR_PASSES):
        existing = _scan_prefix(adapter, registered_prefix)
        if not existing:
            break
        for offset in range(0, len(existing), UNLINK_BATCH):
            adapter.unlink(existing[offset:offset + UNLINK_BATCH])
    else:
        raise ValkeyArchiveError("valkey_restore_clear_failed")
    restored = 0
    expired = 0
    for record in records:
        if (
            record.expires_at_ms is not None
            and record.expires_at_ms <= adapter.server_time_ms()
        ):
            expired += 1
            continue
        adapter.restore(record.key, record.payload, record.expires_at_ms)
        restored += 1
    observed = _scan_prefix(adapter, registered_prefix)
    now_ms = adapter.server_time_ms()
    all_records = {record.key: record for record in records}
    expected = {
        record.key: record for record in records
        if record.expires_at_ms is None or record.expires_at_ms > now_ms
    }
    unexpected = set(observed) - set(all_records)
    live_observed = {
        key for key in observed
        if key in all_records and (
            all_records[key].expires_at_ms is None
            or all_records[key].expires_at_ms > now_ms
        )
    }
    if unexpected or live_observed != set(expected):
        raise ValkeyArchiveError("valkey_restore_verification_failed")
    for key in sorted(live_observed):
        payload, expires_at_ms = adapter.capture(key)
        record = expected[key]
        if (
            payload is None
            and record.expires_at_ms is not None
            and record.expires_at_ms <= adapter.server_time_ms()
        ):
            continue
        if payload != record.payload or expires_at_ms != record.expires_at_ms:
            raise ValkeyArchiveError("valkey_restore_verification_failed")
    return ValkeyRestore(restored=restored, expired=expired)
