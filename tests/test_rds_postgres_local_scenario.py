from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


PATH = Path(__file__).parent / "integration" / "rds_postgres_local_scenario.py"
SPEC = importlib.util.spec_from_file_location("rds_postgres_local_scenario", PATH)
assert SPEC is not None and SPEC.loader is not None
SCENARIO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SCENARIO
SPEC.loader.exec_module(SCENARIO)


@pytest.mark.skipif(not SCENARIO.available(), reason="PostgreSQL server binaries unavailable")
def test_zero_cost_managed_postgres_lifecycle() -> None:
    report = SCENARIO.run_scenario()

    assert report == {
        "state": "passed",
        "tls": "verify-full",
        "isolated_databases": 2,
        "extension": {"name": "pgcrypto", "version": report["extension"]["version"]},
        "least_privilege": True,
        "activation": True,
        "rotation_rollback": True,
        "detached_rebound_generation": 3,
        "purged_allocations": 2,
    }
