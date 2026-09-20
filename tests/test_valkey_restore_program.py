import base64
import hashlib
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def program_namespace() -> dict[str, object]:
    source = (ROOT / "scripts" / "gimme-restore-valkey").read_text()
    namespace: dict[str, object] = {"__name__": "gimme_restore_valkey"}
    exec(compile(source, "gimme-restore-valkey", "exec"), namespace)
    return namespace


def encoded_archive(records: list[tuple[bytes, bytes, int | None]]) -> bytes:
    lines = [b'{"format":"gimme-valkey-v1"}']
    for key, payload, expiry in sorted(records):
        lines.append(json.dumps({
            "dump": base64.b64encode(payload).decode(),
            "expires_at_ms": expiry,
            "key": base64.b64encode(key).decode(),
        }, sort_keys=True, separators=(",", ":")).encode())
    return b"\n".join(lines) + b"\n"


class FakeSession:
    def __init__(self, values: dict[bytes, tuple[bytes, int | None]], now: int = 1_000):
        self.values = values
        self.now = now
        self.commands: list[str] = []

    def call(self, command, *arguments):
        self.commands.append(command)
        if command == "SCAN":
            prefix = arguments[2][:-1]
            return [b"0", sorted(key for key in self.values if key.startswith(prefix))]
        if command == "UNLINK":
            for key in arguments:
                self.values.pop(key, None)
            return len(arguments)
        if command == "TIME":
            return [str(self.now // 1000).encode(), str(self.now % 1000 * 1000).encode()]
        if command == "RESTORE":
            key, ttl, payload = arguments[:3]
            self.values[key] = (payload, None if ttl == 0 else ttl)
            return b"OK"
        if command == "DUMP":
            return self.values.get(arguments[0], (None, None))[0]
        if command == "PEXPIRETIME":
            return -1 if self.values[arguments[0]][1] is None else self.values[arguments[0]][1]
        raise AssertionError(command)


def test_target_restore_replaces_only_prefix_and_preserves_absolute_expiry(
    tmp_path: Path,
) -> None:
    program = program_namespace()
    prefix = b"gimme:checkout:"
    body = encoded_archive([
        (prefix + b"persistent", b"dump-a", None),
        (prefix + b"expiring", b"dump-b", 9_000),
        (prefix + b"expired", b"dump-c", 500),
    ])
    path = tmp_path / "archive"
    path.write_bytes(body)
    os.chmod(path, 0o600)
    records = program["archive"](
        path, prefix, hashlib.sha256(body).hexdigest(), len(body), 3
    )
    foreign = b"gimme:billing:foreign"
    session = FakeSession({prefix + b"old": (b"old", None), foreign: (b"safe", None)})

    restored, expired = program["replace"](session, prefix, records)

    assert (restored, expired) == (2, 1)
    assert session.values[foreign] == (b"safe", None)
    assert session.values[prefix + b"persistent"] == (b"dump-a", None)
    assert session.values[prefix + b"expiring"] == (b"dump-b", 9_000)
    assert prefix + b"expired" not in session.values
    assert "UNLINK" in session.commands
    assert all(command not in session.commands for command in ("KEYS", "FLUSHDB", "FLUSHALL"))


def test_target_restore_rejects_foreign_prefix_before_mutation(tmp_path: Path) -> None:
    program = program_namespace()
    prefix = b"gimme:checkout:"
    body = encoded_archive([(b"gimme:billing:key", b"dump", None)])
    path = tmp_path / "archive"
    path.write_bytes(body)
    os.chmod(path, 0o600)

    with pytest.raises(program["RestoreFailure"], match="archive invalid"):
        program["archive"](
            path, prefix, hashlib.sha256(body).hexdigest(), len(body), 1
        )


def test_target_restore_accepts_natural_expiry_during_verification() -> None:
    program = program_namespace()
    prefix = b"gimme:checkout:"
    key = prefix + b"soon"

    class ExpiringSession(FakeSession):
        def call(self, command, *arguments):
            if command == "TIME" and "RESTORE" in self.commands:
                self.now = 2_000
            return super().call(command, *arguments)

    session = ExpiringSession({}, now=1_000)

    assert program["replace"](session, prefix, [(key, b"dump", 1_500)]) == (1, 0)


def test_target_restore_rejects_integrity_or_mode_mismatch(tmp_path: Path) -> None:
    program = program_namespace()
    prefix = b"gimme:checkout:"
    body = encoded_archive([])
    path = tmp_path / "archive"
    path.write_bytes(body)

    with pytest.raises(program["RestoreFailure"], match="archive invalid"):
        program["archive"](path, prefix, "0" * 64, len(body), 0)

    os.chmod(path, 0o600)
    with pytest.raises(program["RestoreFailure"], match="archive invalid"):
        program["archive"](path, prefix, "0" * 64, len(body), 0)


def test_target_restore_program_has_no_global_or_persistence_commands() -> None:
    source = (ROOT / "scripts" / "gimme-restore-valkey").read_text()

    assert 'session.call("SCAN", cursor, "MATCH", prefix + b"*", "COUNT", 1000)' in source
    assert 'session.call("UNLINK", *keys[offset:offset + UNLINK_BATCH])' in source
    assert '"RESTORE", key' in source
    assert '"ABSTTL"' in source
    assert all(token not in source for token in ('"KEYS"', '"FLUSHDB"', '"FLUSHALL"', '"SAVE"'))
