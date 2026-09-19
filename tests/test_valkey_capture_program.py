import base64
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def program_namespace() -> dict[str, object]:
    source = (ROOT / "scripts" / "gimme-capture-valkey").read_text()
    namespace: dict[str, object] = {"__name__": "gimme_capture_valkey"}
    exec(compile(source, "gimme-capture-valkey", "exec"), namespace)
    return namespace


def test_target_capture_preserves_binary_values_and_deduplicates_scan(
    tmp_path, monkeypatch
) -> None:
    program = program_namespace()
    prefix = b"gimme:checkout:"
    binary_key = prefix + b"\x00key"

    class FakeSession:
        def __init__(self, *_args):
            self.connection = SimpleNamespace(close=lambda: None)
            self.scans = 0

        def call(self, command, *args):
            if command == "SCAN":
                self.scans += 1
                return [b"1", [binary_key]] if self.scans == 1 else [b"0", [binary_key]]
            assert command == "EVAL"
            return [b"\x00\xffdump", -1]

    monkeypatch.setitem(program, "Session", FakeSession)
    monkeypatch.setattr(
        program["shutil"], "disk_usage",
        lambda _path: SimpleNamespace(free=10 * 1024 * 1024 * 1024),
    )
    output = tmp_path / "archive"

    _digest, _size, count, _captured = program["capture"](
        output, prefix, "localhost", 6379, False, None, None
    )

    assert count == 1
    lines = output.read_bytes().splitlines()
    assert json.loads(lines[0]) == {"format": "gimme-valkey-v1"}
    assert len(lines) == 2
    record = json.loads(lines[1])
    assert base64.b64decode(record["key"]) == binary_key
    assert base64.b64decode(record["dump"]) == b"\x00\xffdump"
    assert record["expires_at_ms"] is None


def test_target_capture_omits_expired_keys_and_preserves_absolute_expiry(
    tmp_path, monkeypatch
) -> None:
    program = program_namespace()
    prefix = b"gimme:checkout:"
    expiring = prefix + b"future"
    expired = prefix + b"expired"

    class FakeSession:
        def __init__(self, *_args):
            self.connection = SimpleNamespace(close=lambda: None)

        def call(self, command, *args):
            if command == "SCAN":
                return [b"0", [expired, expiring]]
            return [b"dump", 1 if args[-1] == expired else 4_102_444_800_000]

    monkeypatch.setitem(program, "Session", FakeSession)
    monkeypatch.setattr(
        program["shutil"], "disk_usage",
        lambda _path: SimpleNamespace(free=10 * 1024 * 1024 * 1024),
    )
    output = tmp_path / "archive"

    _digest, _size, count, _captured = program["capture"](
        output, prefix, "localhost", 6379, False, None, None
    )

    assert count == 1
    record = json.loads(output.read_bytes().splitlines()[1])
    assert base64.b64decode(record["key"]) == expiring
    assert record["expires_at_ms"] == 4_102_444_800_000


def test_target_capture_accepts_an_empty_prefix_result(tmp_path, monkeypatch) -> None:
    program = program_namespace()

    class FakeSession:
        def __init__(self, *_args):
            self.connection = SimpleNamespace(close=lambda: None)

        def call(self, command, *_args):
            assert command == "SCAN"
            return [b"0", []]

    monkeypatch.setitem(program, "Session", FakeSession)
    monkeypatch.setattr(
        program["shutil"], "disk_usage",
        lambda _path: SimpleNamespace(free=10 * 1024 * 1024 * 1024),
    )
    output = tmp_path / "archive"

    _digest, _size, count, _captured = program["capture"](
        output, b"gimme:checkout:", "localhost", 6379, False, None, None
    )

    assert count == 0
    assert output.read_bytes() == b'{"format":"gimme-valkey-v1"}\n'


def test_protocol_reader_accepts_a_value_larger_than_one_io_buffer() -> None:
    program = program_namespace()
    payload = b"x" * 70_000
    connection = program["Connection"].__new__(program["Connection"])
    connection.reader = io.BytesIO(
        f"${len(payload)}\r\n".encode() + payload + b"\r\n"
    )

    assert connection.read() == payload


def test_target_capture_rejects_a_cross_prefix_scan_result(tmp_path, monkeypatch) -> None:
    program = program_namespace()

    class FakeSession:
        def __init__(self, *_args):
            self.connection = SimpleNamespace(close=lambda: None)

        def call(self, command, *_args):
            assert command == "SCAN"
            return [b"0", [b"gimme:billing:key"]]

    monkeypatch.setitem(program, "Session", FakeSession)

    with pytest.raises(program["CaptureFailure"], match="isolation"):
        program["capture"](
            tmp_path / "archive", b"gimme:checkout:", "localhost", 6379,
            False, None, None,
        )
