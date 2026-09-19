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


class ValkeyArchiveError(RuntimeError):
    """A bounded error which never includes key or value material."""


class CaptureAdapter(Protocol):
    def scan(self, cursor: int) -> tuple[int, list[bytes]]: ...

    def capture(self, key: bytes) -> tuple[bytes | None, int | None]:
        """Atomically return DUMP bytes and absolute expiry milliseconds."""


@dataclass(frozen=True)
class ValkeyArchive:
    body: bytes
    sha256: str
    keys: int
    bytes: int


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
