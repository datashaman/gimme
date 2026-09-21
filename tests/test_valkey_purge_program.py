import json
import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PREFIX = b"{gimme:checkout}:"


def program_namespace() -> dict[str, object]:
    source = (ROOT / "scripts" / "gimme-purge-valkey").read_text()
    namespace: dict[str, object] = {"__name__": "gimme_purge_valkey"}
    exec(compile(source, "gimme-purge-valkey", "exec"), namespace)
    return namespace


class FakeSession:
    """SCAN pages and DEL results, recorded. `pages` are returned in order."""

    def __init__(self, pages: list[list[bytes]]) -> None:
        self.pages = pages
        self.commands: list[tuple] = []
        self.closed = False
        self.connection = self

    def close(self) -> None:
        self.closed = True

    def call(self, command, *args):
        self.commands.append((command, *args))
        if command == "SCAN":
            page = self.pages.pop(0)
            return [str(len(self.pages)).encode(), page]
        assert command == "DEL"
        return len(args)


def run(monkeypatch, pages, **overrides):
    program = program_namespace()
    session = FakeSession(pages)
    monkeypatch.setitem(program, "Session", lambda *_args: session)
    for key, value in overrides.items():
        monkeypatch.setitem(program, key, value)
    outcome = program["purge"](PREFIX, "cfg.example.internal", 6379, "admin", "secret")
    return outcome, session


def test_a_complete_pass_deletes_only_the_prefix_in_batches_and_reports_no_more(
    monkeypatch,
) -> None:
    keys = [PREFIX + b"cache:%d" % index for index in range(5)]

    (deleted, more), session = run(monkeypatch, [keys[:3], keys[3:]], BATCH=2)

    assert (deleted, more) == (5, False) and session.closed
    assert [command[0] for command in session.commands] == [
        "SCAN", "DEL", "DEL", "SCAN", "DEL"
    ]
    assert session.commands[0] == ("SCAN", 0, "MATCH", PREFIX + b"*", "COUNT", 1000)
    assert session.commands[1] == ("DEL", *keys[:2])


def test_an_empty_pass_deletes_nothing(monkeypatch) -> None:
    (deleted, more), session = run(monkeypatch, [[]])

    assert (deleted, more) == (0, False)
    assert [command[0] for command in session.commands] == ["SCAN"]


def test_a_foreign_key_stops_the_purge_before_anything_is_deleted(monkeypatch) -> None:
    program = program_namespace()
    session = FakeSession([[PREFIX + b"ok", b"{gimme:other}:cache:x"]])
    monkeypatch.setitem(program, "Session", lambda *_args: session)

    with pytest.raises(program["PurgeFailure"], match="isolation failed"):
        program["purge"](PREFIX, "cfg.example.internal", 6379, "admin", "secret")

    assert all(command[0] != "DEL" for command in session.commands) and session.closed


def test_a_run_stops_at_its_key_budget_and_asks_to_be_resumed(monkeypatch) -> None:
    keys = [PREFIX + b"%d" % index for index in range(4)]

    (deleted, more), session = run(monkeypatch, [keys[:2], keys[2:], []], MAX_DELETED=2)

    assert (deleted, more) == (2, True) and session.pages != []


def test_a_run_stops_at_its_time_budget_and_asks_to_be_resumed(monkeypatch) -> None:
    (deleted, more), _session = run(
        monkeypatch, [[PREFIX + b"a"], []], MAX_SECONDS=-1
    )

    assert (deleted, more) == (1, True)


@pytest.mark.parametrize(
    "arguments",
    [
        [], ["{gimme:a}:", "h.example", "6379"],
        ["{gimme:a}:cache:", "h.example", "6379", "/x"],
        ["gimme:a:", "h.example", "6379", "/x"],
        ["{gimme:A}:", "h.example", "6379", "/x"],
        ["{gimme:a}:*", "h.example", "6379", "/x"],
        ["{gimme:a}:", "h.example;x", "6379", "/x"],
        ["{gimme:a}:", "h.example", "0", "/x"],
        ["{gimme:a}:", "h.example", "65536", "/x"],
        ["{gimme:a}:", "h.example", "x", "/x"],
    ],
)
def test_the_program_accepts_only_its_fixed_inputs(monkeypatch, arguments) -> None:
    program = program_namespace()
    monkeypatch.setattr(program["sys"], "argv", ["gimme-purge-valkey", *arguments])
    monkeypatch.setitem(program, "credential", lambda _path: ("admin", "secret"))
    monkeypatch.setitem(program, "purge", lambda *_args: pytest.fail("must not connect"))

    with pytest.raises(program["PurgeFailure"]):
        program["main"]()


def test_a_valid_invocation_prints_one_fixed_line(monkeypatch, capsys) -> None:
    program = program_namespace()
    monkeypatch.setattr(
        program["sys"], "argv", ["gimme-purge-valkey", "{gimme:a}:", "h.example", "6379", "/x"]
    )
    monkeypatch.setitem(program, "credential", lambda _path: ("admin", "secret"))
    monkeypatch.setitem(program, "purge", lambda *args: (4, True))

    program["main"]()

    assert capsys.readouterr().out == "GIMME_VALKEY_PURGE|4|yes\n"


def test_the_credential_file_must_be_a_private_regular_file_with_exactly_two_fields(
    tmp_path,
) -> None:
    program = program_namespace()
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"username": "admin", "password": "secret"}))
    good.chmod(0o600)
    assert program["credential"](str(good)) == ("admin", "secret")

    for name, content, mode in (
        ("open.json", {"username": "a", "password": "b"}, 0o644),
        ("extra.json", {"username": "a", "password": "b", "x": "c"}, 0o600),
        ("empty.json", {"username": "", "password": "b"}, 0o600),
    ):
        path = tmp_path / name
        path.write_text(json.dumps(content))
        path.chmod(mode)
        with pytest.raises(program["PurgeFailure"], match="credential invalid"):
            program["credential"](str(path))
    link = tmp_path / "link.json"
    link.symlink_to(good)
    with pytest.raises(program["PurgeFailure"], match="credential invalid"):
        program["credential"](str(link))


def test_the_program_names_no_global_or_administrative_command() -> None:
    source = (ROOT / "scripts" / "gimme-purge-valkey").read_text()

    assert '"SCAN"' in source and '"DEL"' in source and "key.startswith(prefix)" in source
    prohibited = ("KEYS", "FLUSHALL", "FLUSHDB", "CONFIG", "SCRIPT", "EVAL", "SHUTDOWN")
    for command in prohibited:
        assert re.search(rf"\.call\(\s*['\"]{command}['\"]", source) is None
    # TLS is not optional: there is no plaintext connection to fall back to.
    assert "ssl.create_default_context().wrap_socket(raw, server_hostname=host)" in source
    assert "socket.create_connection" in source and source.count("create_connection") == 1
