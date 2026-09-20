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


def test_replay_interruption_is_recovered_by_full_clear_and_replay() -> None:
    first = PREFIX + b"first"
    second = PREFIX + b"second"
    body = archive([(first, b"one", None), (second, b"two", None)])

    class InterruptedReplay(FakeRestore):
        interrupted = False

        def restore(self, key, payload, expires_at_ms):
            super().restore(key, payload, expires_at_ms)
            if not self.interrupted:
                self.interrupted = True
                raise RuntimeError("transport interrupted")

    adapter = InterruptedReplay({PREFIX + b"old": (b"old", None)})

    with pytest.raises(RuntimeError, match="transport interrupted"):
        replace_archive(adapter, PREFIX, body, expected_records=2)
    assert first in adapter.values or second in adapter.values

    result = replace_archive(adapter, PREFIX, body, expected_records=2)

    assert result.restored == 2
    assert adapter.values == {
        first: (b"one", None),
        second: (b"two", None),
    }


def test_clear_interruption_mutates_no_foreign_prefix_and_retry_restarts_clear() -> None:
    source = PREFIX + b"source"
    foreign = OTHER + b"foreign"
    body = archive([(source, b"restored", None)])

    class InterruptedClear(FakeRestore):
        interrupted = False

        def unlink(self, keys):
            super().unlink(keys[:1])
            if not self.interrupted:
                self.interrupted = True
                raise RuntimeError("clear interrupted")
            super().unlink(keys[1:])

    adapter = InterruptedClear({
        PREFIX + b"old-a": (b"a", None),
        PREFIX + b"old-b": (b"b", None),
        foreign: (b"untouched", None),
    })

    with pytest.raises(RuntimeError, match="clear interrupted"):
        replace_archive(adapter, PREFIX, body, expected_records=1)
    assert adapter.values[foreign] == (b"untouched", None)

    replace_archive(adapter, PREFIX, body, expected_records=1)

    assert adapter.values == {
        source: (b"restored", None),
        foreign: (b"untouched", None),
    }


def test_record_expiring_between_replay_turns_is_never_resurrected() -> None:
    early = PREFIX + b"a-early"
    expired_before_turn = PREFIX + b"b-expired"
    persistent = PREFIX + b"persistent"
    body = archive([
        (early, b"early", 5_000),
        (expired_before_turn, b"expired", 1_001),
        (persistent, b"keep", None),
    ])

    class AdvancingTime(FakeRestore):
        calls = 0

        def server_time_ms(self):
            self.calls += 1
            return 1_000 if self.calls == 1 else 1_002

    adapter = AdvancingTime()

    result = replace_archive(adapter, PREFIX, body, expected_records=3)

    assert result.restored == 2 and result.expired == 1
    assert expired_before_turn not in adapter.values
    assert adapter.values[early] == (b"early", 5_000)
    assert adapter.values[persistent] == (b"keep", None)
