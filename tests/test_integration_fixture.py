import os
from pathlib import Path
import subprocess
import sys

from gimme.control import StateStore


ROOT = Path(__file__).resolve().parents[1]


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
    assert state.schema_version == 5
    assert sorted(state.targets) == ["integration"]
    assert sorted(state.deployments) == ["smoke-default", "smoke-preview"]
    assert state.deployments["smoke-default"].placement != (
        state.deployments["smoke-preview"].placement
    )
    assert (tmp_path / "state.json").stat().st_mode & 0o777 == 0o600
