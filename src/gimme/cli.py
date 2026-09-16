from __future__ import annotations

import argparse
from pathlib import Path

from gimme.control import StateStore, legacy_server, target_sites
from gimme.deployer import DeployerRunner


ROOT = Path(__file__).resolve().parents[2]


def bootstrap_target() -> None:
    parser = argparse.ArgumentParser(
        description="Perform Gimme's one-time interactive privileged-helper bootstrap."
    )
    parser.add_argument("target", help="registered target name")
    arguments = parser.parse_args()
    store = StateStore.from_environment(ROOT)
    state = store.load()
    target = state.targets[arguments.target]
    result = DeployerRunner(ROOT).run(
        "gimme:provision:stack",
        legacy_server(target),
        stack=target.stack,
        sites=target_sites(state, arguments.target),
        network_mode=target.network.mode,
        toolchains=target.toolchains.model_dump(),
        timeout=1800,
        bootstrap=True,
        interactive_sudo=True,
    )
    print(result.output, end="")


def bootstrap_database() -> None:
    parser = argparse.ArgumentParser(
        description="Grant Gimme's deployment user local PostgreSQL administration roles."
    )
    parser.add_argument("target", help="registered target name")
    arguments = parser.parse_args()
    store = StateStore.from_environment(ROOT)
    target = store.target(arguments.target)
    result = DeployerRunner(ROOT).run(
        "gimme:bootstrap:database-admin",
        legacy_server(target),
        stack=target.stack,
        timeout=300,
        bootstrap=True,
        interactive_sudo=True,
    )
    print(result.output, end="")
