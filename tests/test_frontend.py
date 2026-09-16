from pathlib import Path

import pytest

from gimme.config import FrontendBuildConfig
from gimme.frontend import build_command, install_command, validate_lockfile


@pytest.mark.parametrize(
    ("manager", "version", "expected"),
    [
        ("npm", "10.9.0", ["npm", "ci", "--no-audit", "--no-fund"]),
        ("pnpm", "9.15.0", ["pnpm", "install", "--frozen-lockfile"]),
        ("yarn", "1.22.22", ["yarn", "install", "--frozen-lockfile", "--non-interactive"]),
        ("yarn", "4.5.3", ["yarn", "install", "--immutable"]),
        ("bun", "1.1.38", ["bun", "install", "--frozen-lockfile"]),
    ],
)
def test_frozen_install_commands(manager: str, version: str, expected: list[str]) -> None:
    assert install_command(manager, version) == expected


def test_build_command_is_not_free_form() -> None:
    frontend = FrontendBuildConfig(package_manager="pnpm", build_script="build:prod")
    assert build_command(frontend) == ["pnpm", "run", "build:prod"]


def test_lockfile_must_match_exclusively(tmp_path: Path) -> None:
    (tmp_path / "pnpm-lock.yaml").touch()
    assert validate_lockfile(tmp_path, "pnpm") == "pnpm-lock.yaml"
    (tmp_path / "package-lock.json").touch()
    with pytest.raises(ValueError, match="exactly one"):
        validate_lockfile(tmp_path, "pnpm")
