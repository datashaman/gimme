from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from gimme.artifact_deployment_orchestration import release_contract
from gimme.control import ControlState, Rollout, RolloutArtifact
from gimme.control_plans import exact_plan
from gimme.fleet_orchestration import fleet_state


def _fingerprint(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return "rollout_" + hashlib.sha256(encoded).hexdigest()


def _artifact(value: dict[str, object]) -> RolloutArtifact:
    return RolloutArtifact.model_validate({
        key: value[key]
        for key in ("application", "build_id", "commit", "artifact_digest", "tree_digest")
    })


def public_rollout(rollout: Rollout) -> dict[str, object]:
    """Return the complete allowlisted rollout projection and no target internals."""
    return rollout.model_dump(mode="json")


@dataclass(frozen=True)
class RolloutOrchestrator:
    store: Any
    artifact_deployment: Any
    run_deployment: Callable[..., Any]
    assert_plan: Callable[[dict[str, object], str], None]

    def inspect(self, name: str) -> dict[str, object]:
        state = self.store.load()
        try:
            return public_rollout(state.rollouts[name])
        except KeyError as exc:
            raise KeyError(f"deployment '{name}' has no rollout") from exc

    def _runtime_fingerprint(
        self, name: str, deployment: Any, application: Any, identity: dict[str, object]
    ) -> str:
        try:
            result = self.run_deployment(
                "gimme:preflight:artifact-runtimes", name, timeout=60
            )
        except Exception:
            raise RuntimeError("rollout_target_unavailable") from None
        observed: dict[str, object] = {"php_extensions": []}
        for raw in result.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_RUNTIME|php|"):
                observed["php"] = line.rsplit("|", 1)[-1]
            elif line.startswith("GIMME_PLATFORM|") and line.count("|") == 2:
                _, system, machine = line.split("|", 2)
                if all(re.fullmatch(r"[a-z0-9_.+-]{1,64}", part) for part in (system, machine)):
                    observed["system"], observed["machine"] = system, machine
            elif line.startswith("GIMME_PHP_EXTENSION|") and line.endswith("|ready"):
                observed["php_extensions"].append(line.split("|", 2)[1])
        observed["php_extensions"] = sorted(observed["php_extensions"])
        capability = identity.get("capability")
        if not isinstance(capability, dict) or observed != {
            "php": deployment.runtimes["php"].version,
            "php_extensions": application.php_extensions,
            "system": capability.get("system"),
            "machine": capability.get("machine"),
        }:
            raise RuntimeError("rollout_runtime_incompatible")
        return _fingerprint(observed)

    def _context(self, name: str) -> dict[str, object]:
        state = self.store.load()
        deployment = state.deployments[name]
        application = state.applications[deployment.application]
        if deployment.release_mode != "artifact":
            raise ValueError("rollout requires artifact release mode")
        if deployment.stage not in {"staging", "production"}:
            raise ValueError("rollout requires staging or production")
        existing = state.rollouts.get(name)
        if existing is None:
            capacity = fleet_state(state)["targets"][deployment.target]
            if capacity["overcommitted"] or capacity["free_slots"] < 1:
                raise ValueError("rollout requires one free Target slot")

        stable_metadata = self.artifact_deployment.live_release(name)
        stable_expected = self.artifact_deployment.expected_from_release(
            name, stable_metadata
        )
        if stable_expected["build_id"] != stable_metadata.get("build_id"):
            raise RuntimeError("rollout_stable_incompatible")
        stable = _artifact(stable_metadata)

        candidate_context = self.artifact_deployment.context(name)
        candidate_raw = candidate_context["artifact"]
        if candidate_raw.get("status") != "ready":
            raise ValueError("rollout candidate artifact is unavailable")
        candidate = _artifact(candidate_raw)
        if stable.build_id == candidate.build_id:
            raise ValueError("rollout candidate must differ from stable")
        contract = release_contract(deployment, application)
        if stable_metadata.get("release_contract") != contract:
            raise RuntimeError("rollout_contract_incompatible")
        runtime_fingerprint = self._runtime_fingerprint(
            name, deployment, application, candidate_context["identity"]
        )
        policy = {
            "deployment": deployment.model_dump(mode="json"),
            "application": application.model_dump(mode="json"),
            "target": deployment.target,
            "resource_bindings": deployment.resources.model_dump(mode="json"),
        }
        policy_fingerprint = _fingerprint(policy)
        contract_fingerprint = _fingerprint({
            "release_contract": contract,
            "runtime": runtime_fingerprint,
        })
        evidence_fingerprint = _fingerprint({
            "stable_release": stable_metadata,
            "candidate_publication": candidate_raw,
            "reader_credential_versions": candidate_context.get(
                "reader_credential_versions", []
            ),
        })
        generation = (
            existing.generation
            if existing is not None
            else 1 + int(hashlib.sha256(
                f"{name}\0{stable.build_id}\0{candidate.build_id}".encode()
            ).hexdigest()[:7], 16)
        )
        desired = Rollout(
            deployment=name,
            target=deployment.target,
            generation=generation,
            phase="preparing",
            stable=stable,
            candidate=candidate,
            policy_fingerprint=policy_fingerprint,
            contract_fingerprint=contract_fingerprint,
            evidence_fingerprint=evidence_fingerprint,
        )
        if existing is not None and (
            existing.stable != desired.stable
            or existing.candidate != desired.candidate
            or existing.policy_fingerprint != desired.policy_fingerprint
            or existing.contract_fingerprint != desired.contract_fingerprint
            or existing.evidence_fingerprint != desired.evidence_fingerprint
        ):
            raise ValueError("rollout generation conflicts with current policy or artifacts")
        return {
            "state": state,
            "deployment": deployment,
            "candidate_context": candidate_context,
            "desired": desired,
            "retry": existing is not None,
        }

    @staticmethod
    def _plan(name: str, context: dict[str, object]) -> dict[str, object]:
        rollout: Rollout = context["desired"]
        return exact_plan({
            "kind": "rollout_start",
            "deployment": name,
            "generation": rollout.generation,
            "stable": rollout.stable.model_dump(mode="json"),
            "candidate": rollout.candidate.model_dump(mode="json"),
            "weights": {"stable": 100, "candidate": 0},
            "temporary_slots": 1,
            "policy_fingerprint": rollout.policy_fingerprint,
            "contract_fingerprint": rollout.contract_fingerprint,
            "evidence_fingerprint": rollout.evidence_fingerprint,
            "retry": context["retry"],
            "ready": True,
            "effects": [
                "persist preparing before target mutation",
                "materialize one isolated candidate backend",
                "run direct loopback candidate health probes",
                "leave public traffic and stable background processes unchanged",
            ],
        })

    def plan_start(self, name: str) -> dict[str, object]:
        return self._plan(name, self._context(name))

    def start(self, name: str, plan_id: str) -> dict[str, object]:
        context = self._context(name)
        expected = self._plan(name, context)
        self.assert_plan(expected, plan_id)
        desired: Rollout = context["desired"]

        def reserve(state: ControlState) -> ControlState:
            current = state.rollouts.get(name)
            if current is not None:
                if current.generation != desired.generation:
                    raise ValueError("rollout generation conflicts with current state")
                return state
            deployment = state.deployments.get(name)
            if deployment is None:
                raise ValueError("rollout deployment changed; request a fresh plan")
            application = state.applications.get(deployment.application)
            policy = {
                "deployment": deployment.model_dump(mode="json"),
                "application": (
                    None if application is None else application.model_dump(mode="json")
                ),
                "target": deployment.target,
                "resource_bindings": deployment.resources.model_dump(mode="json"),
            }
            if _fingerprint(policy) != desired.policy_fingerprint:
                raise ValueError("rollout policy changed; request a fresh plan")
            capacity = fleet_state(state)["targets"][desired.target]
            if capacity["overcommitted"] or capacity["free_slots"] < 1:
                raise ValueError("rollout capacity changed; request a fresh plan")
            return state.model_copy(update={"rollouts": {**state.rollouts, name: desired}})

        self.store.update(reserve)
        request, secret_context = self.artifact_deployment.apply_arguments(
            context["candidate_context"]
        )
        try:
            with secret_context as credential_file:
                self.run_deployment(
                    "gimme:rollout:prepare",
                    name,
                    revision=desired.candidate.commit,
                    artifact_request=request,
                    artifact_secret_file=credential_file,
                    rollout_generation=desired.generation,
                    timeout=1800,
                )
        except Exception:
            def degraded(state: ControlState) -> ControlState:
                rollout = state.rollouts.get(name)
                if rollout is None or rollout.generation != desired.generation:
                    return state
                updated = rollout.model_copy(update={
                    "phase": "degraded", "outcome": "prepare_failed",
                })
                return state.model_copy(update={"rollouts": {**state.rollouts, name: updated}})
            self.store.update(degraded)
            raise RuntimeError("rollout_candidate_prepare_failed") from None

        def activate(state: ControlState) -> ControlState:
            rollout = state.rollouts.get(name)
            if rollout is None or rollout.generation != desired.generation:
                raise ValueError("rollout generation changed during preparation")
            updated = rollout.model_copy(update={
                "phase": "active", "backend_ready": True, "outcome": "ready",
            })
            return state.model_copy(update={"rollouts": {**state.rollouts, name: updated}})

        return public_rollout(self.store.update(activate).rollouts[name])
