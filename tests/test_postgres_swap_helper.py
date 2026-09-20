import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def helper_namespace(tmp_path: Path) -> dict[str, object]:
    source = (ROOT / "scripts/gimme-postgres-restore-swap").read_text()
    source = source.replace('Path("__GIMME_APPS_ROOT__")', f"Path({str(tmp_path)!r})")
    source = source.replace(
        'json.loads("__GIMME_ALLOWED_DATABASES_JSON__")',
        repr({"example-app": "gimme_example_app"}),
    )
    namespace: dict[str, object] = {"__name__": "gimme_postgres_restore_swap"}
    exec(compile(source, "gimme-postgres-restore-swap", "exec"), namespace)
    return namespace


def authority_file(tmp_path: Path, database: str = "gimme_example_app") -> Path:
    path = tmp_path / ".gimme/restores/example-app/restore-1.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema_version": 1,
        "deployment": "example-app",
        "request_id": "restore-1",
        "database": database,
        "role": database,
        "sha256": "a" * 64,
        "bytes": 10,
        "phase": "shadow_verified",
        "live_oid": 101,
        "shadow_oid": 202,
    }))
    path.chmod(0o600)
    return path


def test_authority_is_bound_to_the_bootstrapped_deployment_database(tmp_path) -> None:
    helper = helper_namespace(tmp_path)
    path = authority_file(tmp_path, "gimme_another_app")

    with pytest.raises(RuntimeError, match="authority identity invalid"):
        helper["load_state"]("example-app", "restore-1", path.stat().st_uid)


def test_swap_terminates_only_live_connections_and_renames_verified_oids(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace(tmp_path)
    state_path = authority_file(tmp_path)
    state = helper["load_state"]("example-app", "restore-1", state_path.stat().st_uid)
    database = "gimme_example_app"
    shadow, previous = helper["derived_identities"](database, "restore-1")
    oids = {database: 101, shadow: 202}
    statements = []
    monkeypatch.setitem(helper, "database_oid", lambda name: oids.get(name))

    def query(statement, variables):
        statements.append((statement, variables))
        return ""

    def rename(current, replacement):
        oids[replacement] = oids.pop(current)

    monkeypatch.setitem(helper, "query", query)
    monkeypatch.setitem(helper, "rename_database", rename)

    helper["swap"](state)

    assert len(statements) == 1
    assert "pg_terminate_backend" in statements[0][0]
    assert statements[0][1] == {"database": database}
    assert oids == {previous: 101, database: 202}


def test_failed_shadow_rename_restores_the_original_database_name(
    tmp_path, monkeypatch
) -> None:
    helper = helper_namespace(tmp_path)
    state_path = authority_file(tmp_path)
    state = helper["load_state"]("example-app", "restore-1", state_path.stat().st_uid)
    database = "gimme_example_app"
    shadow, previous = helper["derived_identities"](database, "restore-1")
    oids = {database: 101, shadow: 202}
    monkeypatch.setitem(helper, "database_oid", lambda name: oids.get(name))
    monkeypatch.setitem(helper, "query", lambda *_args: "")

    def rename(current, replacement):
        if current == shadow:
            raise RuntimeError("simulated rename failure")
        oids[replacement] = oids.pop(current)

    monkeypatch.setitem(helper, "rename_database", rename)

    with pytest.raises(RuntimeError, match="simulated rename failure"):
        helper["swap"](state)

    assert oids == {database: 101, shadow: 202}


def test_query_uses_only_fixed_runuser_and_psql_executables(tmp_path, monkeypatch) -> None:
    helper = helper_namespace(tmp_path)
    observed = []

    class Result:
        stdout = b"1\n"

    monkeypatch.setattr(
        helper["subprocess"], "run",
        lambda arguments, **kwargs: observed.append((arguments, kwargs)) or Result(),
    )

    assert helper["query"]("SELECT 1;\n", {}) == "1"
    assert observed[0][0][:5] == [
        "/usr/sbin/runuser", "-u", "postgres", "--", "/usr/bin/psql"
    ]
