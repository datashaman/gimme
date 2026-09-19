import base64
import json

import pytest

from gimme.valkey_archive import (
    ValkeyArchiveError,
    capture_archive,
    verify_archive,
)


class FakeCapture:
    def __init__(self, pages, values):
        self.pages = iter(pages)
        self.values = values
        self.captured = []

    def scan(self, cursor):
        return next(self.pages)

    def capture(self, key):
        self.captured.append(key)
        return self.values[key]


def records(body: bytes) -> list[dict]:
    return [json.loads(line) for line in body.splitlines()[1:]]


def test_archive_is_binary_safe_deduplicated_sorted_and_preserves_absolute_expiry() -> None:
    prefix = b"gimme:checkout:"
    binary_key = prefix + b"\x00z"
    persistent = prefix + b"a"
    large = b"\x00\xff" * 100_000
    adapter = FakeCapture(
        [(7, [binary_key, persistent]), (0, [binary_key])],
        {binary_key: (large, 2_000), persistent: (b"persistent\x00value", None)},
    )

    archive = capture_archive(adapter, prefix, now_ms=1_000)

    assert archive.keys == 2
    assert verify_archive(archive.body) == 2
    decoded = records(archive.body)
    assert [base64.b64decode(item["key"]) for item in decoded] == [binary_key, persistent]
    assert decoded[1]["expires_at_ms"] is None
    assert base64.b64decode(decoded[0]["dump"]) == large
    assert adapter.captured.count(binary_key) == 1


def test_missing_and_expired_keys_are_omitted() -> None:
    prefix = b"gimme:checkout:"
    adapter = FakeCapture(
        [(0, [prefix + b"gone", prefix + b"expired"])],
        {prefix + b"gone": (None, None), prefix + b"expired": (b"dump", 999)},
    )

    archive = capture_archive(adapter, prefix, now_ms=1_000)

    assert archive.keys == 0
    assert verify_archive(archive.body) == 0


def test_capture_fails_closed_if_scan_returns_another_deployments_key() -> None:
    adapter = FakeCapture([(0, [b"gimme:billing:key"])], {})

    with pytest.raises(ValkeyArchiveError, match="^valkey_capture_isolation_failed$"):
        capture_archive(adapter, b"gimme:checkout:", now_ms=1_000)

    assert adapter.captured == []


def test_archive_verifier_rejects_duplicate_or_unsorted_keys() -> None:
    line = json.dumps({
        "dump": base64.b64encode(b"value").decode(),
        "expires_at_ms": None,
        "key": base64.b64encode(b"key").decode(),
    }, sort_keys=True, separators=(",", ":")).encode()
    body = b'{"format":"gimme-valkey-v1"}\n' + line + b"\n" + line + b"\n"

    with pytest.raises(ValkeyArchiveError, match="^valkey_archive_invalid$"):
        verify_archive(body)
