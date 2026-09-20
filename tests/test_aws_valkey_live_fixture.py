import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIVE_SPEC = importlib.util.spec_from_file_location(
    "aws_valkey_recovery_live_smoke",
    ROOT / "tests/integration/aws_valkey_recovery_live_smoke.py",
)
assert LIVE_SPEC is not None and LIVE_SPEC.loader is not None
live_smoke = importlib.util.module_from_spec(LIVE_SPEC)
sys.modules[LIVE_SPEC.name] = live_smoke
LIVE_SPEC.loader.exec_module(live_smoke)
configuration = live_smoke.configuration


def live_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "GIMME_AWS_VALKEY_LIVE_CREATE": "1",
        "GIMME_AWS_VALKEY_LIVE_DESTROY": "1",
        "GIMME_AWS_VALKEY_RESOURCE": "live-valkey",
        "GIMME_AWS_VALKEY_DEPLOYMENT": "live-app",
        "GIMME_AWS_VALKEY_RUN_ID": "run-20260920-a",
        "GIMME_STATE_DIR": str(tmp_path.resolve()),
    }


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("GIMME_AWS_VALKEY_LIVE_CREATE", "authorize live AWS creation"),
        ("GIMME_AWS_VALKEY_LIVE_DESTROY", "authorize exact live AWS deletion"),
    ],
)
def test_live_recovery_requires_separate_creation_and_destruction_authority(
    tmp_path: Path, missing: str, message: str,
) -> None:
    environ = live_environment(tmp_path)
    del environ[missing]

    with pytest.raises(SystemExit, match=message):
        configuration(environ)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("GIMME_AWS_VALKEY_RESOURCE", "bad/value", "VALKEY_RESOURCE"),
        ("GIMME_AWS_VALKEY_DEPLOYMENT", "UPPER", "VALKEY_DEPLOYMENT"),
        ("GIMME_AWS_VALKEY_RUN_ID", "", "run id"),
        ("GIMME_STATE_DIR", "relative-state", "absolute isolated"),
    ],
)
def test_live_recovery_rejects_unbounded_context(
    tmp_path: Path, field: str, value: str, message: str,
) -> None:
    environ = live_environment(tmp_path)
    environ[field] = value

    with pytest.raises(SystemExit, match=message):
        configuration(environ)


def test_live_recovery_accepts_only_complete_bounded_context(tmp_path: Path) -> None:
    selected = configuration(live_environment(tmp_path))

    assert selected.resource == "live-valkey"
    assert selected.deployment == "live-app"
    assert selected.run_id == "run-20260920-a"
    assert selected.state_directory == tmp_path.resolve()


def test_live_recovery_refuses_the_repository_state_directory(tmp_path: Path) -> None:
    environ = live_environment(tmp_path)
    environ["GIMME_STATE_DIR"] = str(ROOT / "config")

    with pytest.raises(SystemExit, match="refuses the repository"):
        configuration(environ)


def test_loss_simulation_uses_exact_adapter_delete_then_public_restore(
    tmp_path: Path, monkeypatch,
) -> None:
    selected = live_smoke.LiveConfiguration(
        "live-valkey", "live-app", "run-20260920-a", tmp_path
    )
    events: list[str] = []

    class Adapter:
        live = SimpleNamespace(status="available")
        snapshot = False

        def describe_group(self, account, network, group_id):
            return self.live

        def delete_group(self, account, network, group_id, snapshot):
            assert group_id == "gimme-live-valkey"
            assert snapshot == live_smoke.loss_snapshot_name(selected)
            events.append("exact-loss")
            self.live = None
            self.snapshot = True

        def delete_final_snapshot(self, account, network, snapshot):
            events.append("exact-snapshot-cleanup")
            self.snapshot = False
            return True

    adapter = Adapter()
    marker: dict[str, object] = {}
    monkeypatch.setattr(live_smoke.gimme, "elasticache_valkey", adapter)
    monkeypatch.setattr(
        live_smoke, "require_live_context",
        lambda selected: (SimpleNamespace(), SimpleNamespace(), SimpleNamespace()),
    )
    monkeypatch.setattr(
        live_smoke, "snapshot_status",
        lambda resource, snapshot: "available" if adapter.snapshot else None,
    )
    monkeypatch.setattr(live_smoke, "live_marker", lambda selected: marker or None)
    monkeypatch.setattr(
        live_smoke, "save_live_marker",
        lambda selected, phase: marker.update({"phase": phase}),
    )
    monkeypatch.setattr(live_smoke, "clear_live_marker", lambda selected: marker.clear())
    monkeypatch.setattr(
        live_smoke.gimme, "plan_apply_resource", lambda resource: {"plan_id": "normal"},
    )

    def refuse_normal_apply(resource, plan_id):
        events.append("ordinary-apply-refused")
        raise live_smoke.ResourceError("aws_elasticache_group_missing_replace_explicitly")

    monkeypatch.setattr(live_smoke.gimme, "apply_resource", refuse_normal_apply)
    monkeypatch.setattr(
        live_smoke.gimme, "plan_restore_resource",
        lambda resource, snapshot: {"plan_id": "restore"},
    )

    def restore(resource, snapshot, plan_id):
        events.append("public-restore")
        adapter.live = SimpleNamespace(status="available")
        return {"phase": "ready", "verified": ["live-app"]}

    monkeypatch.setattr(live_smoke.gimme, "apply_restore_resource", restore)
    monkeypatch.setattr(
        live_smoke.gimme, "plan_deployment_resources",
        lambda deployment: {
            "ready": True, "secret_versions": [{"status": "current"}],
        },
    )

    result = live_smoke.simulate_loss_and_restore(selected)

    assert result == {"phase": "ready", "verified": ["live-app"]}
    assert events == [
        "exact-loss", "ordinary-apply-refused", "public-restore",
        "exact-snapshot-cleanup",
    ]
    assert marker == {}


def test_live_recovery_resumes_after_snapshot_cleanup_without_deleting_again(
    tmp_path: Path, monkeypatch,
) -> None:
    selected = live_smoke.LiveConfiguration(
        "live-valkey", "live-app", "run-20260920-a", tmp_path
    )
    adapter = SimpleNamespace(
        describe_group=lambda account, network, group: SimpleNamespace(status="available")
    )
    cleared: list[bool] = []
    monkeypatch.setattr(live_smoke.gimme, "elasticache_valkey", adapter)
    monkeypatch.setattr(
        live_smoke, "require_live_context",
        lambda selected: (SimpleNamespace(), SimpleNamespace(), SimpleNamespace()),
    )
    monkeypatch.setattr(live_smoke, "snapshot_status", lambda resource, snapshot: None)
    monkeypatch.setattr(live_smoke, "live_marker", lambda selected: {"phase": "cleanup"})
    monkeypatch.setattr(
        live_smoke, "clear_live_marker", lambda selected: cleared.append(True)
    )

    result = live_smoke.simulate_loss_and_restore(selected)

    assert result == {"phase": "ready", "verified": ["live-app"], "resumed": True}
    assert cleared == [True]


def test_live_recovery_marker_is_private_exact_and_atomic(tmp_path: Path, monkeypatch) -> None:
    selected = live_smoke.LiveConfiguration(
        "live-valkey", "live-app", "run-20260920-a", tmp_path
    )
    monkeypatch.setattr(live_smoke.gimme, "store", SimpleNamespace(root=tmp_path))

    live_smoke.save_live_marker(selected, "deleting")
    path = live_smoke.live_marker_path(selected)

    assert path.stat().st_mode & 0o777 == 0o600
    assert live_smoke.live_marker(selected) == {
        "schema_version": 1,
        "resource": "live-valkey",
        "run_id": "run-20260920-a",
        "snapshot": live_smoke.loss_snapshot_name(selected),
        "phase": "deleting",
    }
    assert list(path.parent.glob(".valkey-recovery-*")) == []
    live_smoke.clear_live_marker(selected)
    assert not path.exists()
