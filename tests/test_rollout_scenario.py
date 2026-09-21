from __future__ import annotations

import importlib.util
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys


PATH = Path(__file__).parent / "integration" / "rollout_local_scenario.py"
LOADER = SourceFileLoader("rollout_local_scenario", str(PATH))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
SCENARIO = importlib.util.module_from_spec(SPEC)
sys.modules[LOADER.name] = SCENARIO
LOADER.exec_module(SCENARIO)


def test_zero_cost_rollout_operator_scenario() -> None:
    report = SCENARIO.run_scenario()

    assert [stage["weights"] for stage in report["stages"]] == [
        [90, 10], [50, 50], [0, 100],
    ]
    assert report["direct_health"] == [
        "stable", "candidate", "stable", "candidate", "stable", "candidate",
    ]
    assert report["public_health_checks"] == 3
    assert report["completion"]["background_owner"] == "candidate"
    assert report["reversal"]["background_owner"] == "stable"
