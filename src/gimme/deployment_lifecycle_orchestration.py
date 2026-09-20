from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from gimme.control import (
    DeploymentConfig,
    DeploymentRegistration,
    ManualRecoveryCadence,
    legacy_server,
    new_placement,
    target_sites,
)
from gimme.control_plans import deployment_removal_plan, registration_update_plan


@dataclass(frozen=True)
class DeploymentLifecycleOrchestrator:
    """Own Deployment registration, reviewed updates, and ordered removal."""

    store: Any
    runner: Any
    context: Callable[..., Any]
    run_deployment: Callable[..., Any]
    deployment_resource_lock: Callable[..., Any]
    assert_plan: Callable[..., Any]
    replace: Callable[..., Any]
    delete: Callable[..., Any]
    recovery_schedule_authority: Callable[..., Any]
    result: Callable[..., dict[str, object]]

    def register_deployment(
        self, name: str, definition: DeploymentRegistration
    ) -> dict[str, object]:
        state = self.store.load()
        if name in state.deployments:
            raise ValueError("deployment already exists; use plan_update_deployment")
        target = state.targets[definition.target]
        deployment = definition.materialize(
            new_placement(name, target, domain=definition.domain)
        )
        self.store.save(self.replace(state, "deployments", name, deployment))
        return {
            "changed": True,
            "deployment": name,
            "placement": deployment.placement.model_dump(mode="json"),
        }

    def plan_update_deployment(
        self, name: str, definition: DeploymentRegistration
    ) -> dict[str, object]:
        state = self.store.load()
        current = state.deployments[name]
        placement = current.placement
        if definition.domain is not None and definition.domain != placement.site_host:
            placement = placement.model_copy(update={"site_host": definition.domain})
        proposed = definition.materialize(placement)
        self.replace(state, "deployments", name, proposed)
        return registration_update_plan(
            "deployment_update", name, current, proposed
        )

    def update_deployment(
        self, name: str, definition: DeploymentRegistration, plan_id: str
    ) -> dict[str, object]:
        with self.deployment_resource_lock(name):
            expected = self.plan_update_deployment(name, definition)
            self.assert_plan(expected, plan_id)
            proposed = DeploymentConfig.model_validate(expected["proposed"])
            self.store.save(
                self.replace(self.store.load(), "deployments", name, proposed)
            )
            return {"changed": True, "deployment": name}

    def plan_remove_deployment(self, name: str) -> dict[str, object]:
        _state, deployment, target, _application = self.context(name)
        return deployment_removal_plan(name, deployment, target)

    def remove_deployment(
        self, name: str, plan_id: str, confirmation: str
    ) -> dict[str, object]:
        with self.deployment_resource_lock(name):
            expected = self.plan_remove_deployment(name)
            self.assert_plan(expected, plan_id)
            if confirmation != expected["confirmation"]:
                raise ValueError(
                    f"confirmation must exactly equal '{expected['confirmation']}'"
                )
            state, deployment, _target, _application = self.context(name)
            if deployment.recovery is not None:
                cleanup_deployment = deployment.model_copy(
                    update={
                        "recovery": deployment.recovery.model_copy(
                            update={
                                "cadence": ManualRecoveryCadence(),
                                "valkey": False,
                            }
                        )
                    }
                )
                authority = self.recovery_schedule_authority(
                    name, state, cleanup_deployment
                )
                if authority is None:
                    raise RuntimeError(
                        "Recovery Schedule cleanup authority is unavailable"
                    )
                try:
                    self.run_deployment(
                        "gimme:recovery:schedule-reconcile",
                        name,
                        recovery_schedule_authority=authority,
                        timeout=1800,
                    )
                except Exception:
                    raise RuntimeError("recovery_schedule_cleanup_failed") from None
            result = self.run_deployment(
                "gimme:remove:deployment", name, timeout=1800
            )
            state = self.delete(self.store.load(), "deployments", name)
            self.store.save(state)
            (self.store.root / "applied-secrets" / f"{name}.json").unlink(
                missing_ok=True
            )
            target = state.targets[expected["target"]]
            self.runner.run(
                "gimme:reconcile:sites",
                legacy_server(target),
                stack=target.stack,
                sites=target_sites(state, str(expected["target"])),
                network_mode=target.network.mode,
                mise_version=target.runtimes.mise_version,
                timeout=1800,
            )
            return self.result(result)
