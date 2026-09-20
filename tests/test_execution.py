from pathlib import Path

import pytest

from gimme.execution import execution_fingerprint


def write_execution_tree(root: Path) -> None:
    files = {
        "src/gimme/server.py": "server-v1\n",
        "deploy.php": "recipe-v1\n",
        "deploy/artifact.py": "artifact-v1\n",
        "deploy/aws-rds-global-bundle.pem": "bundle-v1\n",
        "deploy/configuration.php": "configuration-v1\n",
        "scripts/gimme-provision-stack": "stack-helper-v1\n",
        "scripts/gimme-provision-processes": "process-helper-v1\n",
        "scripts/gimme-provision-recovery-schedule": "schedule-helper-v1\n",
        "scripts/gimme-recovery-runner": "schedule-runner-v1\n",
        "scripts/gimme-recovery-maintenance": "recovery-helper-v1\n",
        "scripts/gimme-postgres-restore-swap": "postgres-swap-helper-v1\n",
        "scripts/gimme-capture-valkey": "valkey-capture-v1\n",
        "scripts/gimme-restore-postgres": "postgres-restore-v1\n",
        "scripts/gimme-restore-valkey": "valkey-restore-v1\n",
        "vendor/deployer/deployer/bin/dep": "deployer-bin-v1\n",
        "vendor/deployer/deployer/src/functions.php": "deployer-functions-v1\n",
        "pyproject.toml": "python-project-v1\n",
        "uv.lock": "python-lock-v1\n",
        "composer.json": "composer-project-v1\n",
        "composer.lock": "composer-lock-v1\n",
    }
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def test_execution_fingerprint_changes_with_executable_sources_only(tmp_path: Path) -> None:
    write_execution_tree(tmp_path)
    first = execution_fingerprint(tmp_path)

    (tmp_path / "README.md").write_text("documentation only\n")
    assert execution_fingerprint(tmp_path) == first

    (tmp_path / "deploy/configuration.php").write_text("configuration-v2\n")
    assert execution_fingerprint(tmp_path) != first


def test_execution_fingerprint_changes_with_the_pinned_rds_trust_bundle(tmp_path: Path) -> None:
    write_execution_tree(tmp_path)
    first = execution_fingerprint(tmp_path)

    (tmp_path / "deploy/aws-rds-global-bundle.pem").write_text("bundle-v2\n")
    assert execution_fingerprint(tmp_path) != first


def test_execution_fingerprint_fails_closed_when_required_input_is_missing(
    tmp_path: Path,
) -> None:
    write_execution_tree(tmp_path)
    (tmp_path / "composer.lock").unlink()

    with pytest.raises(RuntimeError, match="execution fingerprint input is missing"):
        execution_fingerprint(tmp_path)
