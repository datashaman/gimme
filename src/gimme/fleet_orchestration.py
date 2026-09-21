from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Callable

from gimme.control import (
    ControlState,
    DeploymentRegistration,
    PlacementCandidateDecision,
    PlacementDecision,
    explicit_placement_decision,
    new_placement,
)
from gimme.control_plans import exact_plan


def fleet_state(state: ControlState) -> dict[str, object]:
    deployment_reservations = {
        name: sorted(
            deployment_name
            for deployment_name, deployment in state.deployments.items()
            if deployment.target == name
        )
        for name in state.targets
    }
    rollout_reservations = {
        name: sorted(
            rollout.deployment
            for rollout in state.rollouts.values()
            if rollout.target == name and rollout.phase not in {"completed", "reversed"}
        )
        for name in state.targets
    }
    return {
        "targets": {
            name: {
                "deployment_slots": target.deployment_slots,
                "occupied_slots": (
                    len(deployment_reservations[name])
                    + len(rollout_reservations[name])
                ),
                "free_slots": max(
                    target.deployment_slots
                    - len(deployment_reservations[name])
                    - len(rollout_reservations[name]),
                    0,
                ),
                "overcommitted": (
                    len(deployment_reservations[name]) + len(rollout_reservations[name])
                    > target.deployment_slots
                ),
                "reservations": deployment_reservations[name],
                "temporary_rollout_reservations": rollout_reservations[name],
            }
            for name, target in sorted(state.targets.items())
        }
    }


@dataclass(frozen=True)
class FleetPlacementOrchestrator:
    store: Any
    observe_target: Callable[
        [str, DeploymentRegistration | None], dict[str, object]
    ]
    assert_plan: Callable[[dict[str, object], str], None]
    replace: Callable[..., ControlState]

    @staticmethod
    def _state_fingerprint(state: ControlState) -> str:
        return FleetPlacementOrchestrator._fingerprint(state.model_dump(mode="json"))

    @staticmethod
    def _observation_fingerprint(value: dict[str, object]) -> str:
        return FleetPlacementOrchestrator._fingerprint(value)

    @staticmethod
    def _fingerprint(value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return "fleet_" + hashlib.sha256(encoded).hexdigest()

    def inspect_fleet(self) -> dict[str, object]:
        state = self.store.load()
        desired = fleet_state(state)["targets"]
        observations: dict[str, object] = {}
        for name, target in sorted(state.targets.items()):
            if target.role != "deployment":
                observations[name] = {"status": "not_deployment_capable"}
                continue
            try:
                value = self.observe_target(name, None)
            except Exception:
                value = {"status": "unavailable", "runtimes": {}}
            observations[name] = {
                "status": value.get("status", "unavailable"),
                "observation_fingerprint": self._observation_fingerprint(value),
            }
        return {"targets": desired, "observations": observations}

    def _configured_issues(
        self,
        state: ControlState,
        name: str,
        definition: DeploymentRegistration,
        target_name: str,
        occupied: int,
    ) -> list[str]:
        target = state.targets.get(target_name)
        if target is None:
            return ["target_unregistered"]
        if target.role != "deployment":
            return ["target_not_deployment_capable"]
        if occupied > target.deployment_slots:
            return ["target_overcommitted"]
        if occupied == target.deployment_slots:
            return ["target_full"]
        decision = explicit_placement_decision(
            target_name, target, occupied_slots=occupied
        )
        try:
            deployment = definition.materialize(
                target_name,
                new_placement(name, target, domain=definition.domain),
                decision,
            )
            self.replace(state, "deployments", name, deployment)
        except (KeyError, ValueError) as error:
            message = str(error)
            if "resource" in message or "binding" in message:
                return ["resource_incompatible"]
            if "runtime" in message or "mise" in message:
                return ["runtime_policy_incompatible"]
            if "domain" in message or "public_dns" in message or "local_mdns" in message:
                return ["network_incompatible"]
            return ["target_policy_incompatible"]
        return []

    @staticmethod
    def _runtime_issues(
        definition: DeploymentRegistration, observation: dict[str, object]
    ) -> list[str]:
        if observation.get("status") != "ready":
            status = observation.get("status")
            return [status if isinstance(status, str) else "target_unavailable"]
        observed = observation.get("runtimes")
        if not isinstance(observed, dict):
            return ["target_observation_invalid"]
        for name, pin in definition.runtimes.items():
            if pin.provider in {"system", "bundled"} and observed.get(name) != pin.version:
                return ["runtime_incompatible"]
        return []

    def registration_plan(
        self, name: str, definition: DeploymentRegistration
    ) -> dict[str, object]:
        state = self.store.load()
        if name in state.deployments:
            raise ValueError("deployment already exists; use plan_update_deployment")
        candidates = (
            [definition.target]
            if definition.target is not None
            else definition.placement_policy.candidates
        )
        unknown = sorted(set(candidates) - set(state.targets))
        if unknown:
            raise ValueError("placement policy references an unregistered Target")
        desired = fleet_state(state)["targets"]
        results: list[dict[str, object]] = []
        policy_mode = definition.placement_policy is not None
        for target_name in candidates:
            capacity = desired[target_name]
            occupied = capacity["occupied_slots"]
            issues = self._configured_issues(
                state, name, definition, target_name, int(occupied)
            )
            observation = None
            if not issues and policy_mode:
                try:
                    observation = self.observe_target(target_name, definition)
                except Exception:
                    observation = {"status": "target_unavailable", "runtimes": {}}
                issues.extend(self._runtime_issues(definition, observation))
            results.append({
                "target": target_name,
                "capacity": capacity,
                "eligible": not issues,
                "reasons": sorted(set(issues)),
                "observation_fingerprint": (
                    self._observation_fingerprint(observation)
                    if observation is not None else None
                ),
            })
        eligible = [item for item in results if item["eligible"]]
        selected = min(
            eligible,
            key=lambda item: (
                Fraction(
                    item["capacity"]["occupied_slots"],
                    item["capacity"]["deployment_slots"],
                ),
                -item["capacity"]["free_slots"],
                item["target"],
            ),
            default=None,
        )
        target_name = None if selected is None else selected["target"]
        policy_fingerprint = self._fingerprint(definition.model_dump(mode="json"))
        placement = None
        if target_name is not None:
            placement = new_placement(
                name, state.targets[target_name], domain=definition.domain
            ).model_dump(mode="json")
        return exact_plan({
            "kind": "deployment_registration",
            "deployment": name,
            "mode": "policy" if policy_mode else "explicit",
            "selection_rule": "occupied_ratio_free_slots_name_v1",
            "policy_fingerprint": policy_fingerprint,
            "state_fingerprint": self._state_fingerprint(state),
            "application": definition.application,
            "stage": definition.stage,
            "release_mode": definition.release_mode,
            "runtime_policy": {
                key: value.model_dump(mode="json")
                for key, value in sorted(definition.runtimes.items())
            },
            "resource_bindings": definition.resources.model_dump(mode="json"),
            "candidates": results,
            "selected_target": target_name,
            "placement": placement,
            "ready": selected is not None,
            "effects": [
                "reserve one Deployment slot on the selected registered Target",
                "allocate immutable placement identities",
                "write local desired state without remote mutation",
                "never relocate the Deployment during reconciliation or Target loss",
            ],
        })

    def register_deployment(
        self, name: str, definition: DeploymentRegistration, plan_id: str
    ) -> dict[str, object]:
        expected = self.registration_plan(name, definition)
        self.assert_plan(expected, plan_id)
        if not expected["ready"]:
            raise ValueError("deployment placement has no eligible target")
        selected = str(expected["selected_target"])
        chosen = next(
            item for item in expected["candidates"] if item["target"] == selected
        )

        def reserve(state: ControlState) -> ControlState:
            if self._state_fingerprint(state) != expected["state_fingerprint"]:
                raise ValueError("deployment placement plan is invalid or stale")
            target = state.targets[selected]
            occupied = sum(
                deployment.target == selected for deployment in state.deployments.values()
            )
            if occupied >= target.deployment_slots:
                raise ValueError("deployment placement plan is invalid or stale")
            decision = PlacementDecision(
                mode=expected["mode"],
                candidates=[item["target"] for item in expected["candidates"]],
                selected_target=selected,
                selection_rule=expected["selection_rule"],
                candidate_results=[PlacementCandidateDecision(
                    target=item["target"],
                    deployment_slots=item["capacity"]["deployment_slots"],
                    occupied_slots=item["capacity"]["occupied_slots"],
                    free_slots=item["capacity"]["free_slots"],
                    eligible=item["eligible"],
                    reasons=item["reasons"],
                ) for item in expected["candidates"]],
                policy_fingerprint=expected["policy_fingerprint"],
                deployment_slots=target.deployment_slots,
                occupied_slots=occupied,
                observation_fingerprint=chosen["observation_fingerprint"],
            )
            deployment = definition.materialize(
                selected,
                new_placement(name, target, domain=definition.domain),
                decision,
            )
            return self.replace(state, "deployments", name, deployment)

        updated = self.store.update(reserve)
        deployment = updated.deployments[name]
        return {
            "changed": True,
            "deployment": name,
            "target": selected,
            "placement": deployment.placement.model_dump(mode="json"),
            "placement_decision": deployment.placement_decision.model_dump(mode="json"),
        }
