import base64
import json

import pytest

from gimme.valkey_archive import ValkeyArchiveError, archive_records, replace_archive


PREFIX = b"{gimme:checkout}:"
OTHER = b"{gimme:billing}:"


def archive(records):
    lines = [json.dumps({"format": "gimme-valkey-v1"}, separators=(",", ":"))]
    for key, payload, expiry in sorted(records):
        lines.append(json.dumps({
            "dump": base64.b64encode(payload).decode(),
            "expires_at_ms": expiry,
            "key": base64.b64encode(key).decode(),
        }, sort_keys=True, separators=(",", ":")))
    return ("\n".join(lines) + "\n").encode()


class FakeRestore:
    def __init__(self, values=None, now=1_000):
        self.values = dict(values or {})
        self.now = now
        self.unlinked = []
        self.restored = []

    def scan(self, cursor):
        assert cursor == 0
        return 0, sorted(key for key in self.values if key.startswith(PREFIX))

    def capture(self, key):
        value = self.values.get(key)
        if value is None:
            return None, None
        payload, expiry = value
        if expiry is not None and expiry <= self.now:
            self.values.pop(key)
            return None, None
        return payload, expiry

    def unlink(self, keys):
        self.unlinked.append(list(keys))
        for key in keys:
            self.values.pop(key, None)

    def restore(self, key, payload, expires_at_ms):
        self.restored.append((key, expires_at_ms))
        self.values[key] = (payload, expires_at_ms)

    def server_time_ms(self):
        return self.now


def test_archive_records_reject_foreign_prefix_before_mutation() -> None:
    body = archive([(OTHER + b"key", b"payload", None)])

    with pytest.raises(ValkeyArchiveError, match="^valkey_archive_invalid$"):
        archive_records(body, PREFIX, expected_records=1)


def test_replace_is_prefix_scoped_binary_safe_and_preserves_absolute_expiry() -> None:
    persistent = PREFIX + b"persistent\x00key"
    expiring = PREFIX + b"expiring"
    unrelated = OTHER + b"untouched"
    adapter = FakeRestore({
        PREFIX + b"old": (b"old", None),
        unrelated: (b"foreign", None),
    })
    body = archive([
        (persistent, b"\x00dump\xff", None),
        (expiring, b"expiring-dump", 2_000),
    ])

    result = replace_archive(adapter, PREFIX, body, expected_records=2)

    assert result.restored == 2 and result.expired == 0
    assert adapter.values[unrelated] == (b"foreign", None)
    assert adapter.values[persistent] == (b"\x00dump\xff", None)
    assert adapter.values[expiring] == (b"expiring-dump", 2_000)
    assert adapter.restored == [(expiring, 2_000), (persistent, None)]


def test_replace_skips_records_expired_by_server_time() -> None:
    expired = PREFIX + b"expired"
    live = PREFIX + b"live"
    adapter = FakeRestore(now=2_000)
    body = archive([
        (expired, b"old", 1_999),
        (live, b"live", 2_001),
    ])

    result = replace_archive(adapter, PREFIX, body, expected_records=2)

    assert result.restored == 1 and result.expired == 1
    assert expired not in adapter.values
    assert adapter.values[live] == (b"live", 2_001)


def test_verification_rejects_an_unexpected_prefix_key() -> None:
    expected = PREFIX + b"expected"
    adapter = FakeRestore()
    body = archive([(expected, b"dump", None)])
    original_restore = adapter.restore

    def restore_with_intruder(key, payload, expiry):
        original_restore(key, payload, expiry)
        adapter.values[PREFIX + b"intruder"] = (b"unexpected", None)

    adapter.restore = restore_with_intruder

    with pytest.raises(ValkeyArchiveError, match="valkey_restore_verification_failed"):
        replace_archive(adapter, PREFIX, body, expected_records=1)
