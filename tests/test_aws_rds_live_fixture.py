from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "aws_rds_postgres_live_smoke",
    ROOT / "tests/integration/aws_rds_postgres_live_smoke.py",
)
assert SPEC is not None and SPEC.loader is not None
LIVE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LIVE
SPEC.loader.exec_module(LIVE)


def environment(tmp_path: Path) -> dict[str, str]:
    return {
        "GIMME_AWS_RDS_LIVE_CREATE": "1",
        "GIMME_AWS_RDS_RESOURCE": "live-postgres",
        "GIMME_AWS_RDS_DEPLOYMENT": "live-app",
        "GIMME_AWS_RDS_RUN_ID": "run-20260921-a",
        "GIMME_STATE_DIR": str(tmp_path.resolve()),
    }


def test_live_rds_requires_explicit_creation_authority(tmp_path: Path) -> None:
    configured = environment(tmp_path)
    del configured["GIMME_AWS_RDS_LIVE_CREATE"]

    with pytest.raises(SystemExit, match="authorize billable RDS creation"):
        LIVE.configuration(configured)


def test_live_rds_retains_by_default_and_destroy_is_separate(tmp_path: Path) -> None:
    retained = LIVE.configuration(environment(tmp_path))
    destructive_environment = environment(tmp_path)
    destructive_environment["GIMME_AWS_RDS_LIVE_DESTROY"] = "1"
    destructive = LIVE.configuration(destructive_environment)

    assert retained.destroy is False
    assert destructive.destroy is True


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("GIMME_AWS_RDS_RESOURCE", "bad/value", "RDS_RESOURCE"),
        ("GIMME_AWS_RDS_DEPLOYMENT", "UPPER", "RDS_DEPLOYMENT"),
        ("GIMME_AWS_RDS_RUN_ID", "", "run id"),
        ("GIMME_STATE_DIR", "relative", "absolute isolated"),
        ("GIMME_AWS_RDS_LIVE_DESTROY", "yes", "absent or exactly 1"),
    ],
)
def test_live_rds_rejects_unbounded_context(
    tmp_path: Path, field: str, value: str, message: str,
) -> None:
    configured = environment(tmp_path)
    configured[field] = value

    with pytest.raises(SystemExit, match=message):
        LIVE.configuration(configured)


def test_live_rds_refuses_repository_state(tmp_path: Path) -> None:
    configured = environment(tmp_path)
    configured["GIMME_STATE_DIR"] = str(ROOT / "config")

    with pytest.raises(SystemExit, match="refuses the repository"):
        LIVE.configuration(configured)


def test_live_rds_default_run_reports_bounded_retention(
    tmp_path: Path, monkeypatch,
) -> None:
    selected = LIVE.configuration(environment(tmp_path))
    monkeypatch.setattr(
        LIVE,
        "require_live_context",
        lambda configured: SimpleNamespace(recovery=None),
    )
    monkeypatch.setattr(
        LIVE,
        "_converge_resource",
        lambda resource: {"phase": "ready"},
    )
    monkeypatch.setattr(
        LIVE.gimme,
        "inspect_resource",
        lambda resource: {
            "phase": "ready",
            "readiness_issues": [],
            "multi_az": True,
            "storage_encrypted": True,
            "publicly_accessible": False,
        },
    )
    monkeypatch.setattr(
        LIVE,
        "_bind_activate_rotate",
        lambda resource, deployment: {"rotated": True, "generation": 2},
    )

    report = LIVE.run_live(selected)

    assert report["cleanup"] == "retained_by_default"
    assert report["final_snapshot"] is None
    assert report["topology"] == {
        "multi_az": True,
        "storage_encrypted": True,
        "publicly_accessible": False,
    }
    assert "password" not in str(report).lower()
    assert "endpoint" not in str(report).lower()
