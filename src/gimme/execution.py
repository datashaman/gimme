from __future__ import annotations

import hashlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FINGERPRINT_VERSION = b"gimme-execution-v1\0"
REQUIRED_FILES = (
    "deploy.php",
    "deploy/aws-rds-global-bundle.pem",
    "scripts/gimme-provision-stack",
    "scripts/gimme-provision-processes",
    "scripts/gimme-recovery-maintenance",
    "scripts/gimme-postgres-restore-swap",
    "scripts/gimme-capture-valkey",
    "scripts/gimme-restore-postgres",
    "vendor/deployer/deployer/bin/dep",
    "pyproject.toml",
    "uv.lock",
    "composer.json",
    "composer.lock",
)
SOURCE_GROUPS = (
    "src/gimme/**/*.py",
    "deploy/**/*.php",
    "vendor/deployer/deployer/src/**/*.php",
)


def execution_inputs(root: Path = ROOT) -> tuple[Path, ...]:
    """Return the complete, stable set of files that can change mutation behavior."""
    selected: set[Path] = set()
    for name in REQUIRED_FILES:
        path = root / name
        if not path.is_file():
            raise RuntimeError(f"execution fingerprint input is missing: {name}")
        selected.add(path)
    for pattern in SOURCE_GROUPS:
        matches = {path for path in root.glob(pattern) if path.is_file()}
        if not matches:
            raise RuntimeError(f"execution fingerprint input is missing: {pattern}")
        selected.update(matches)
    return tuple(sorted(selected, key=lambda path: path.relative_to(root).as_posix()))


def execution_fingerprint(root: Path = ROOT) -> str:
    """Hash executable control-plane inputs without operational state or secrets."""
    digest = hashlib.sha256(FINGERPRINT_VERSION)
    for path in execution_inputs(root):
        relative = path.relative_to(root).as_posix().encode()
        digest.update(relative)
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "exec_" + digest.hexdigest()
