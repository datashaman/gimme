from __future__ import annotations

import argparse
from pathlib import Path
import time

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
    started = time.monotonic()
    print(f"Bootstrap target={arguments.target} host={target.bootstrap_hostname}", flush=True)
    result = DeployerRunner(ROOT).run(
        "gimme:provision:stack",
        legacy_server(target),
        stack=target.stack,
        sites=target_sites(state, arguments.target),
        network_mode=target.network.mode,
        mise_version=target.runtimes.mise_version,
        timeout=1800,
        bootstrap=True,
        interactive_sudo=True,
    )
    print(result.output, end="")
    print(f"Bootstrap completed in {time.monotonic() - started:.1f}s", flush=True)


def bootstrap_database() -> None:
    parser = argparse.ArgumentParser(
        description="Grant Gimme's deployment user local PostgreSQL administration roles."
    )
    parser.add_argument("target", help="registered target name")
    arguments = parser.parse_args()
    store = StateStore.from_environment(ROOT)
    target = store.target(arguments.target)
    started = time.monotonic()
    print(
        f"Database bootstrap target={arguments.target} host={target.bootstrap_hostname}",
        flush=True,
    )
    result = DeployerRunner(ROOT).run(
        "gimme:bootstrap:database-admin",
        legacy_server(target),
        stack=target.stack,
        timeout=300,
        bootstrap=True,
        interactive_sudo=True,
    )
    print(result.output, end="")
    print(f"Database bootstrap completed in {time.monotonic() - started:.1f}s", flush=True)
