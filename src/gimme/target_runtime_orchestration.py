from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from gimme.control import legacy_server, target_sites
from gimme.control_plans import exact_plan, target_stack_plan


@dataclass(frozen=True)
class TargetRuntimeOrchestrator:
    """Own Target stack and Deployment runtime inspection and reconciliation."""

    store: Any
    runner: Any
    context: Callable[..., Any]
    run_deployment: Callable[..., Any]
    deployment_resource_lock: Callable[..., Any]
    assert_plan: Callable[..., Any]
    result: Callable[..., dict[str, object]]

    def resolved_stack_plan(self, name: str) -> dict[str, Any]:
        state = self.store.load()
        target = state.targets[name]
        result = self.runner.run(
            "gimme:preflight:stack",
            legacy_server(target),
            stack=target.stack,
            sites=target_sites(state, name),
            network_mode=target.network.mode,
            mise_version=target.runtimes.mise_version,
            timeout=60,
            bootstrap=True,
        )
        resolution: dict[str, dict[str, str]] = {}
        busy: list[int] = []
        helper = "unknown"
        for raw in result.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_PACKAGE|"):
                _, package, installed, candidate = line.split("|", 3)
                resolution[package] = {
                    "installed": installed,
                    "candidate": candidate,
                }
            elif line.startswith("GIMME_APT_BUSY|") and not line.endswith("|no"):
                busy = [int(value) for value in line.split("|", 1)[1].split(",")]
            elif line.startswith("GIMME_HELPER|"):
                helper = line.split("|", 1)[1]
        missing = sorted(set(target.stack.packages) - set(resolution))
        if missing:
            raise RuntimeError(
                "preflight omitted configured packages: " + ", ".join(missing)
            )
        return target_stack_plan(
            name,
            target,
            resolution,
            package_manager_processes=busy,
            privileged_helper=helper,
            sites=target_sites(state, name),
        )

    def inspect_target(self, name: str) -> dict[str, object]:
        target = self.store.target(name)
        return self.result(
            self.runner.run(
                "gimme:inspect",
                legacy_server(target),
                stack=target.stack,
                sites=target_sites(self.store.load(), name),
                network_mode=target.network.mode,
                mise_version=target.runtimes.mise_version,
                timeout=60,
            )
        )

    def plan_target_stack(self, name: str) -> dict[str, object]:
        return self.resolved_stack_plan(name)

    def apply_target_stack(self, name: str, plan_id: str) -> dict[str, object]:
        expected = self.resolved_stack_plan(name)
        self.assert_plan(expected, plan_id)
        if not expected["mcp_apply_ready"]:
            raise ValueError(
                "target is not ready for MCP apply; run gimme-bootstrap-target"
            )
        state = self.store.load()
        target = state.targets[name]
        return self.result(
            self.runner.run(
                "gimme:provision:stack",
                legacy_server(target),
                stack=target.stack,
                sites=target_sites(state, name),
                network_mode=target.network.mode,
                mise_version=target.runtimes.mise_version,
                timeout=1800,
            )
        )

    def plan_deployment_runtimes(self, name: str) -> dict[str, object]:
        _state, deployment, target, application = self.context(name)
        return exact_plan(
            {
                "kind": "deployment_runtimes",
                "deployment": name,
                "target": deployment.target,
                "mise_version": target.runtimes.mise_version,
                "runtimes": {
                    key: value.model_dump(mode="json")
                    for key, value in deployment.runtimes.items()
                },
                "php_extensions": application.php_extensions,
                "effects": [
                    "install only declared mise-managed runtime versions",
                    "verify exact system and bundled runtime versions",
                    "leave every other installed runtime version available",
                ],
            }
        )

    def apply_deployment_runtimes(
        self, name: str, plan_id: str
    ) -> dict[str, object]:
        with self.deployment_resource_lock(name):
            expected = self.plan_deployment_runtimes(name)
            self.assert_plan(expected, plan_id)
            result = self.run_deployment(
                "gimme:provision:runtimes", name, timeout=1800
            )
            self.run_deployment("gimme:preflight:runtimes", name, timeout=120)
            return self.result(result)
