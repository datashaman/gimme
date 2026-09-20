import hashlib
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def program_namespace(tmp_path: Path) -> dict[str, object]:
    source = (ROOT / "scripts" / "gimme-restore-postgres").read_text()
    source = source.replace('Path("__GIMME_APPS_ROOT__")', f"Path({str(tmp_path)!r})")
    namespace: dict[str, object] = {"__name__": "gimme_restore_postgres"}
    exec(compile(source, "gimme-restore-postgres", "exec"), namespace)
    return namespace


def operation_files(tmp_path: Path, *, phase: str = "pending") -> tuple[Path, Path]:
    directory = tmp_path / ".gimme" / "restores" / "example-app"
    directory.mkdir(parents=True)
    artifact = directory / "restore-1.dump"
    artifact.write_bytes(b"verified-dump")
    artifact.chmod(0o600)
    state = directory / "restore-1.json"
    state.write_text(json.dumps({
        "schema_version": 1,
        "deployment": "example-app",
        "request_id": "restore-1",
        "database": "gimme_example_app",
        "role": "gimme_example_app",
        "sha256": hashlib.sha256(b"verified-dump").hexdigest(),
        "bytes": len(b"verified-dump"),
        "phase": phase,
        "live_oid": 101 if phase != "pending" else None,
        "shadow_oid": 202 if phase in {"shadow_verified", "data_replaced"} else None,
    }))
    state.chmod(0o600)
    return state, artifact


def test_restore_identities_are_deterministic_bounded_and_request_specific(tmp_path) -> None:
    program = program_namespace(tmp_path)

    first = program["derived_identities"]("gimme_example_app", "restore-1")
    repeated = program["derived_identities"]("gimme_example_app", "restore-1")
    another = program["derived_identities"]("gimme_example_app", "restore-2")

    assert first == repeated
    assert first != another
    assert all(len(value) <= 63 and value.startswith("gimme_") for value in first)


def test_public_failure_codes_are_fixed_and_do_not_include_request_data(tmp_path) -> None:
    program = program_namespace(tmp_path)

    assert program["PUBLIC_FAILURE_CODES"] == {
        "restore privileged swap failed": "privileged_swap_failed",
        "restore invocation does not match state": "invocation_mismatch",
    }


def test_prepare_verifies_artifact_restores_only_the_derived_shadow_and_records_oids(
    tmp_path, monkeypatch
) -> None:
    program = program_namespace(tmp_path)
    state_path, artifact = operation_files(tmp_path)
    shadow, _previous = program["derived_identities"]("gimme_example_app", "restore-1")
    oids = {"gimme_example_app": 101}
    commands: list[list[str]] = []

    monkeypatch.setitem(program, "database_oid", lambda name: oids.get(name))

    def run(arguments):
        commands.append(arguments)
        if arguments[0] == "createdb":
            oids[arguments[-1]] = 202

    monkeypatch.setitem(program, "run", run)
    monkeypatch.setitem(
        program, "query", lambda database, statement, variables: "1"
    )

    program["prepare"](state_path, artifact)

    state = json.loads(state_path.read_text())
    assert state["phase"] == "shadow_verified"
    assert state["live_oid"] == 101 and state["shadow_oid"] == 202
    assert commands == [
        ["pg_restore", "--list", str(artifact)],
        ["createdb", "--owner", "gimme_example_app", shadow],
        [
            "pg_restore", "--exit-on-error", "--no-owner", "--no-privileges",
            "--role", "gimme_example_app", "--dbname", shadow, str(artifact),
        ],
    ]


def test_prepare_rejects_tampered_artifact_before_postgres_mutation(
    tmp_path, monkeypatch
) -> None:
    program = program_namespace(tmp_path)
    state_path, artifact = operation_files(tmp_path)
    artifact.write_bytes(b"tampered")
    commands = []
    monkeypatch.setitem(program, "run", lambda arguments: commands.append(arguments))

    with pytest.raises(program["RestoreFailure"], match="artifact invalid"):
        program["prepare"](state_path, artifact)

    assert commands == []


def test_prepare_rejects_artifact_outside_request_directory_before_mutation(
    tmp_path, monkeypatch
) -> None:
    program = program_namespace(tmp_path)
    state_path, _artifact = operation_files(tmp_path)
    artifact = tmp_path / "restore-1.dump"
    artifact.write_bytes(b"verified-dump")
    artifact.chmod(0o600)
    commands = []
    monkeypatch.setitem(program, "run", lambda arguments: commands.append(arguments))

    with pytest.raises(program["RestoreFailure"], match="artifact boundary invalid"):
        program["prepare"](state_path, artifact)

    assert commands == []


def test_state_path_must_match_embedded_deployment_and_request(tmp_path) -> None:
    program = program_namespace(tmp_path)
    state_path, _artifact = operation_files(tmp_path)
    misplaced = state_path.with_name("another-request.json")
    state_path.rename(misplaced)

    with pytest.raises(program["RestoreFailure"], match="state identity invalid"):
        program["load_state"](misplaced)


def test_state_phase_requires_the_corresponding_database_identities(tmp_path) -> None:
    program = program_namespace(tmp_path)
    state_path, _artifact = operation_files(tmp_path)
    state = json.loads(state_path.read_text())
    state["live_oid"] = 101
    state_path.write_text(json.dumps(state))
    state_path.chmod(0o600)

    with pytest.raises(program["RestoreFailure"], match="state phase invalid"):
        program["load_state"](state_path)


def test_invocation_must_match_the_protected_request_state(tmp_path, monkeypatch) -> None:
    program = program_namespace(tmp_path)
    state_path, artifact = operation_files(tmp_path)
    monkeypatch.setattr(program["sys"], "argv", [
        "gimme-restore-postgres", "prepare", str(state_path), str(artifact),
        "gimme_another_app", hashlib.sha256(b"verified-dump").hexdigest(),
        str(len(b"verified-dump")),
    ])

    with pytest.raises(program["RestoreFailure"], match="does not match state"):
        program["main"]()


def test_failed_privileged_swap_keeps_the_verified_shadow_state(
    tmp_path, monkeypatch
) -> None:
    program = program_namespace(tmp_path)
    state_path, _artifact = operation_files(tmp_path, phase="shadow_verified")
    database = "gimme_example_app"
    shadow, previous = program["derived_identities"](database, "restore-1")
    oids = {database: 101, shadow: 202}
    monkeypatch.setitem(program, "database_oid", lambda name: oids.get(name))
    monkeypatch.setitem(
        program, "privileged_swap",
        lambda _state: (_ for _ in ()).throw(
            program["RestoreFailure"]("restore privileged swap failed")
        ),
    )

    with pytest.raises(program["RestoreFailure"], match="privileged swap failed"):
        program["swap"](state_path)

    assert json.loads(state_path.read_text())["phase"] == "shadow_verified"


def test_swap_retries_after_the_live_database_was_already_renamed(
    tmp_path, monkeypatch
) -> None:
    program = program_namespace(tmp_path)
    state_path, _artifact = operation_files(tmp_path, phase="shadow_verified")
    database = "gimme_example_app"
    shadow, previous = program["derived_identities"](database, "restore-1")
    oids = {previous: 101, shadow: 202}
    calls = []
    monkeypatch.setitem(program, "database_oid", lambda name: oids.get(name))

    def privileged_swap(state):
        calls.append((state["deployment"], state["request_id"]))
        oids[database] = oids.pop(shadow)

    monkeypatch.setitem(program, "privileged_swap", privileged_swap)

    program["swap"](state_path)

    assert calls == [("example-app", "restore-1")]
    assert oids == {previous: 101, database: 202}
    assert json.loads(state_path.read_text())["phase"] == "data_replaced"


def test_successful_swap_delegates_only_the_protected_request(
    tmp_path, monkeypatch
) -> None:
    program = program_namespace(tmp_path)
    state_path, _artifact = operation_files(tmp_path, phase="shadow_verified")
    database = "gimme_example_app"
    shadow, previous = program["derived_identities"](database, "restore-1")
    oids = {database: 101, shadow: 202}
    calls = []
    monkeypatch.setitem(program, "database_oid", lambda name: oids.get(name))

    def privileged_swap(state):
        calls.append((state["deployment"], state["request_id"], state["database"]))
        oids[previous] = oids.pop(database)
        oids[database] = oids.pop(shadow)

    monkeypatch.setitem(program, "privileged_swap", privileged_swap)

    program["swap"](state_path)

    assert calls == [("example-app", "restore-1", database)]
    assert oids == {previous: 101, database: 202}
    assert json.loads(state_path.read_text())["phase"] == "data_replaced"


def test_cleanup_drops_only_verified_previous_database_and_removes_artifact(
    tmp_path, monkeypatch
) -> None:
    program = program_namespace(tmp_path)
    state_path, artifact = operation_files(tmp_path, phase="data_replaced")
    database = "gimme_example_app"
    _shadow, previous = program["derived_identities"](database, "restore-1")
    oids = {database: 202, previous: 101}
    commands = []
    monkeypatch.setitem(program, "database_oid", lambda name: oids.get(name))
    monkeypatch.setitem(program, "run", lambda arguments: commands.append(arguments))

    program["cleanup"](state_path, artifact)

    assert commands == [["dropdb", previous]]
    assert not artifact.exists()
    assert json.loads(state_path.read_text())["phase"] == "completed"
