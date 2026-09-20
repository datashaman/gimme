import importlib.util
import os
from pathlib import Path
import subprocess
import sys

from gimme.control import StateStore


ROOT = Path(__file__).resolve().parents[1]
SMOKE_SPEC = importlib.util.spec_from_file_location(
    "disposable_vm_smoke", ROOT / "tests/integration/disposable_vm_smoke.py"
)
assert SMOKE_SPEC is not None and SMOKE_SPEC.loader is not None
disposable_vm_smoke = importlib.util.module_from_spec(SMOKE_SPEC)
SMOKE_SPEC.loader.exec_module(disposable_vm_smoke)


def test_deployment_route_status_does_not_depend_on_runner_dns(monkeypatch) -> None:
    observed: list[str] = []

    def fake_ssh(*arguments: str) -> str:
        observed.extend(arguments)
        return "200"

    monkeypatch.setattr(disposable_vm_smoke, "ssh", fake_ssh)

    assert disposable_vm_smoke.deployment_route_status() == "200"
    assert "smoke-default.gimme-ci.local:80:127.0.0.1" in observed


def test_disposable_vm_fixture_writes_current_isolated_state(tmp_path: Path) -> None:
    environment = {
        **os.environ,
        "CI": "true",
        "GIMME_INTEGRATION_DISPOSABLE": "1",
        "GIMME_STATE_DIR": str(tmp_path),
    }

    subprocess.run(
        [sys.executable, "tests/integration/disposable_vm_smoke.py", "setup"],
        cwd=ROOT,
        env=environment,
        check=True,
        text=True,
        capture_output=True,
    )

    state = StateStore(tmp_path).load()
    assert state.schema_version == 7
    assert sorted(state.targets) == ["integration"]
    assert sorted(state.deployments) == ["smoke-default", "smoke-preview"]
    assert state.deployments["smoke-default"].placement != (
        state.deployments["smoke-preview"].placement
    )
    assert state.applications["smoke"].default_health is not None
    assert state.applications["smoke"].default_health.path == "/up"
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
