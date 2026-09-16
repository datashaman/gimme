from __future__ import annotations

from pathlib import Path

from gimme.config import FrontendBuildConfig


LOCKFILES = {
    "npm": ("package-lock.json",),
    "pnpm": ("pnpm-lock.yaml",),
    "yarn": ("yarn.lock",),
    "bun": ("bun.lock", "bun.lockb"),
}


def install_command(manager: str, version: str) -> list[str]:
    if manager == "npm":
        return ["npm", "ci", "--no-audit", "--no-fund"]
    if manager == "pnpm":
        return ["pnpm", "install", "--frozen-lockfile"]
    if manager == "yarn":
        try:
            major = int(version.split(".", 1)[0])
        except ValueError as exc:
            raise ValueError("Yarn version must begin with a numeric major") from exc
        return (
            ["yarn", "install", "--frozen-lockfile", "--non-interactive"]
            if major == 1
            else ["yarn", "install", "--immutable"]
        )
    if manager == "bun":
        return ["bun", "install", "--frozen-lockfile"]
    raise ValueError(f"unsupported frontend package manager: {manager}")


def build_command(frontend: FrontendBuildConfig) -> list[str]:
    return [frontend.package_manager, "run", frontend.build_script]


def validate_lockfile(root: Path, manager: str) -> str:
    supported = {name for names in LOCKFILES.values() for name in names}
    present = sorted(name for name in supported if (root / name).is_file())
    allowed = [name for name in LOCKFILES[manager] if name in present]
    if len(allowed) != 1 or len(present) != 1:
        raise ValueError(
            f"{manager} requires exactly one matching lockfile and no conflicting lockfiles"
        )
    return allowed[0]
