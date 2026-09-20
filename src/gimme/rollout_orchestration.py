from __future__ import annotations

import base64
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


def _target_result(output: str) -> dict[str, object]:
    prefix = "GIMME_ROLLOUT_STATE|"
    lines = [line.split("] ", 1)[-1].strip() for line in output.splitlines()]
    values = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
    if len(values) != 1 or len(values[0]) > 8192:
        raise RuntimeError("rollout_target_state_invalid")
    try:
        value = json.loads(base64.b64decode(values[0], validate=True))
    except (ValueError, json.JSONDecodeError):
        raise RuntimeError("rollout_target_state_invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError("rollout_target_state_invalid")
    return value


@dataclass(frozen=True)
class RolloutOrchestrator:
    store: Any
    artifact_deployment: Any
    run_deployment: Callable[..., Any]
    assert_plan: Callable[[dict[str, object], str], None]

    @staticmethod
    def _policy(state: ControlState, deployment: Any, application: Any) -> dict[str, object]:
        bindings = deployment.resources.model_dump(mode="json")
        resource_names = {
            name
            for name in (
                deployment.resources.database,
                None if deployment.resources.valkey is None
                else deployment.resources.valkey.resource,
            )
            if name is not None
        }
        return {
            "deployment": deployment.model_dump(mode="json"),
            "application": application.model_dump(mode="json"),
            "target": state.targets[deployment.target].model_dump(mode="json"),
            "resource_bindings": bindings,
            "resources": {
                resource_name: state.resources[resource_name].model_dump(mode="json")
                for resource_name in sorted(resource_names)
            },
        }

    def inspect(self, name: str) -> dict[str, object]:
        state = self.store.load()
        try:
            rollout = state.rollouts[name]
        except KeyError as exc:
            raise KeyError(f"deployment '{name}' has no rollout") from exc
        value = public_rollout(rollout)
        try:
            observed = self._observed(name, rollout)
        except RuntimeError:
            return {
                **value,
                "drift": "target_unavailable",
                "stable_health": "unknown",
                "candidate_health": "unknown",
            }
        if observed["configured"]:
            value.update({
                "stable_eligible": observed["stable_eligible"],
                "candidate_eligible": observed["candidate_eligible"],
                "stable_health": observed["stable_health"],
                "candidate_health": observed["candidate_health"],
                "drift": (
                    "none"
                    if observed["route_fingerprint"] == rollout.route_fingerprint
                    and observed["stable_weight"] == rollout.stable_weight
                    and observed["candidate_weight"] == rollout.candidate_weight
                    else "backend_unavailable"
                ),
            })
        return value

    def _observed(self, name: str, rollout: Rollout) -> dict[str, object]:
        try:
            result = self.run_deployment("gimme:rollout:inspect", name, timeout=60)
        except Exception:
            raise RuntimeError("rollout_target_unavailable") from None
        return self._validate_observed(rollout, _target_result(result.output))

    @staticmethod
    def _validate_observed(
        rollout: Rollout, observed: dict[str, object]
    ) -> dict[str, object]:
        if observed == {"configured": False}:
            return observed
        expected = {
            "configured", "generation", "affinity_generation", "stable_weight",
            "candidate_weight", "stable_eligible", "candidate_eligible",
            "stable_health", "candidate_health", "stable_identity",
            "candidate_identity", "route_fingerprint", "phase", "outcome",
        }
        if (
            set(observed) != expected
            or observed.get("configured") is not True
            or observed.get("generation") != rollout.generation
            or not isinstance(observed.get("affinity_generation"), int)
            or isinstance(observed.get("affinity_generation"), bool)
            or not 1 <= observed["affinity_generation"] <= 2_147_483_647
            or any(
                not isinstance(observed.get(field), int)
                or isinstance(observed.get(field), bool)
                or not 0 <= observed[field] <= 100
                for field in ("stable_weight", "candidate_weight")
            )
            or observed["stable_weight"] + observed["candidate_weight"] != 100
            or any(
                not isinstance(observed.get(field), bool)
                for field in ("stable_eligible", "candidate_eligible")
            )
            or observed.get("stable_health") not in {"ready", "unavailable", "unknown"}
            or observed.get("candidate_health") not in {"ready", "unavailable", "unknown"}
            or observed.get("phase") != "active"
            or any(
                not isinstance(observed.get(field), str)
                or re.fullmatch(r"rollout_[0-9a-f]{64}", observed[field]) is None
                for field in ("stable_identity", "candidate_identity")
            )
            or observed.get("stable_identity")
            != _fingerprint(rollout.stable.model_dump(mode="json"))
            or observed.get("candidate_identity")
            != _fingerprint(rollout.candidate.model_dump(mode="json"))
            or not isinstance(observed.get("route_fingerprint"), str)
            or re.fullmatch(r"rollout_[0-9a-f]{64}", observed["route_fingerprint"]) is None
            or observed.get("outcome") not in {"ready", "route_restored"}
        ):
            raise RuntimeError("rollout_target_state_invalid")
        return observed

    @staticmethod
    def _health(deployment: Any, application: Any) -> list[dict[str, object]]:
        primary = (
            application.default_health
            if deployment.health == "inherit"
            else deployment.health
        )
        probes = [
            *([primary] if primary is not None else []),
            *application.health_probes,
            *deployment.health_probes,
        ]
        selected = [
            {
                "path": probe.path,
                "expected_status": probe.expected_status,
                "timeout_seconds": probe.timeout_seconds,
                "attempts": probe.attempts,
                "delay_seconds": probe.delay_seconds,
            }
            for probe in probes
            if "candidate" in probe.phases or "live" in probe.phases
        ]
        return selected or [{
            "path": "/", "expected_status": 200, "timeout_seconds": 5,
            "attempts": 3, "delay_seconds": 1,
        }]

    def _routing_policy(
        self, context: dict[str, object], stable_weight: int, candidate_weight: int,
        route_fingerprint: str,
    ) -> dict[str, object]:
        deployment = context["deployment"]
        application = context["state"].applications[deployment.application]
        target = context["state"].targets[deployment.target]
        php = deployment.runtimes.get("php")
        if php is None:
            raise ValueError("rollout routing requires a PHP runtime")
        version = ".".join(php.version.split(".")[:2])
        return {
            "generation": context["desired"].generation,
            "affinity_generation": context["desired"].affinity_generation,
            "stable_weight": stable_weight,
            "candidate_weight": candidate_weight,
            "route_fingerprint": route_fingerprint,
            "framework": application.framework,
            "site_host": deployment.placement.site_host,
            "network_mode": target.network.mode,
            "deploy_path": f"{target.apps_root}/{deployment.placement.relative_path}",
            "php_version": version,
            "health": self._health(deployment, application),
            "stable_identity": _fingerprint(context["desired"].stable.model_dump(mode="json")),
            "candidate_identity": _fingerprint(
                context["desired"].candidate.model_dump(mode="json")
            ),
        }

    def _weights_context(
        self, name: str, stable_weight: int, candidate_weight: int
    ) -> dict[str, object]:
        if (
            isinstance(stable_weight, bool)
            or isinstance(candidate_weight, bool)
            or not 0 <= stable_weight <= 100
            or not 0 <= candidate_weight <= 100
            or stable_weight + candidate_weight != 100
        ):
            raise ValueError("rollout weights must be integers from 0 to 100 totaling 100")
        context = self._context(name)
        current = context["state"].rollouts.get(name)
        if current is None or current.phase != "active":
            raise ValueError("rollout weight changes require one active generation")
        if candidate_weight > 0 and (
            not current.backend_ready or current.candidate_health != "ready"
        ):
            raise ValueError("candidate traffic requires a ready candidate backend")
        affinity_generation = current.affinity_generation or current.generation
        route_fingerprint = _fingerprint({
            "generation": current.generation,
            "affinity_generation": affinity_generation,
            "stable": current.stable.model_dump(mode="json"),
            "candidate": current.candidate.model_dump(mode="json"),
            "weights": [stable_weight, candidate_weight],
            "contract": current.contract_fingerprint,
        })
        observed = self._observed(name, current)
        target_already_applied = False
        if observed["configured"]:
            stable_identity = _fingerprint(current.stable.model_dump(mode="json"))
            candidate_identity = _fingerprint(current.candidate.model_dump(mode="json"))
            matches_current = (
                observed["route_fingerprint"] == current.route_fingerprint
                and observed["stable_weight"] == current.stable_weight
                and observed["candidate_weight"] == current.candidate_weight
                and observed["affinity_generation"]
                == (current.affinity_generation or affinity_generation)
                and observed["stable_identity"] == stable_identity
                and observed["candidate_identity"] == candidate_identity
            )
            target_already_applied = (
                observed["route_fingerprint"] == route_fingerprint
                and observed["stable_weight"] == stable_weight
                and observed["candidate_weight"] == candidate_weight
                and observed["affinity_generation"] == affinity_generation
                and observed["stable_identity"] == stable_identity
                and observed["candidate_identity"] == candidate_identity
            )
            if not matches_current and not target_already_applied:
                raise ValueError("rollout route is drifted")
        elif current.affinity_generation != 0:
            raise ValueError("rollout route is missing")
        desired = current.model_copy(update={"affinity_generation": affinity_generation})
        context.update({
            "current": current,
            "desired": desired,
            "observed": observed,
            "stable_weight": stable_weight,
            "candidate_weight": candidate_weight,
            "route_fingerprint": route_fingerprint,
            "target_already_applied": target_already_applied,
        })
        return context

    @staticmethod
    def _weights_plan(name: str, context: dict[str, object]) -> dict[str, object]:
        current: Rollout = context["current"]
        return exact_plan({
            "kind": "rollout_weights",
            "deployment": name,
            "generation": current.generation,
            "affinity_generation": context["desired"].affinity_generation,
            "current_weights": {
                "stable": current.stable_weight,
                "candidate": current.candidate_weight,
            },
            "proposed_weights": {
                "stable": context["stable_weight"],
                "candidate": context["candidate_weight"],
            },
            "current_route_fingerprint": current.route_fingerprint,
            "proposed_route_fingerprint": context["route_fingerprint"],
            "retry": context["target_already_applied"],
            "policy_fingerprint": current.policy_fingerprint,
            "contract_fingerprint": current.contract_fingerprint,
            "evidence_fingerprint": current.evidence_fingerprint,
            "effects": [
                "preflight stable and candidate backends directly",
                "atomically install signed sticky weighted routing",
                "verify direct backends and the public live route",
                "restore and verify the exact prior route on failure",
                "persist desired weights only after target verification",
            ],
        })

    def plan_weights(
        self, name: str, stable_weight: int, candidate_weight: int
    ) -> dict[str, object]:
        return self._weights_plan(
            name, self._weights_context(name, stable_weight, candidate_weight)
        )

    def apply_weights(
        self, name: str, stable_weight: int, candidate_weight: int, plan_id: str
    ) -> dict[str, object]:
        context = self._weights_context(name, stable_weight, candidate_weight)
        expected = self._weights_plan(name, context)
        self.assert_plan(expected, plan_id)
        current: Rollout = context["current"]
        policy = self._routing_policy(
            context, stable_weight, candidate_weight, context["route_fingerprint"]
        )
        try:
            result = self.run_deployment(
                "gimme:rollout:weights", name, rollout_policy=policy, timeout=1800
            )
            observed = self._validate_observed(current, _target_result(result.output))
        except Exception:
            raise RuntimeError("rollout_weight_transition_failed") from None
        if (
            observed.get("outcome") != "ready"
            or observed.get("affinity_generation")
            != context["desired"].affinity_generation
            or observed.get("route_fingerprint") != context["route_fingerprint"]
            or observed.get("stable_weight") != stable_weight
            or observed.get("candidate_weight") != candidate_weight
            or observed.get("stable_eligible") is not (stable_weight > 0)
            or observed.get("candidate_eligible") is not (candidate_weight > 0)
            or observed.get("stable_health") != "ready"
            or observed.get("candidate_health") != "ready"
        ):
            raise RuntimeError("rollout_weight_transition_failed")

        def persist(state: ControlState) -> ControlState:
            rollout = state.rollouts.get(name)
            if rollout != current:
                raise ValueError("rollout changed during route installation")
            updated = rollout.model_copy(update={
                "stable_weight": stable_weight,
                "candidate_weight": candidate_weight,
                "affinity_generation": context["desired"].affinity_generation,
                "route_fingerprint": context["route_fingerprint"],
                "stable_eligible": observed.get("stable_eligible") is True,
                "candidate_eligible": observed.get("candidate_eligible") is True,
                "stable_health": observed.get("stable_health", "unknown"),
                "candidate_health": observed.get("candidate_health", "unknown"),
                "drift": "none",
                "outcome": "ready",
            })
            return state.model_copy(update={"rollouts": {**state.rollouts, name: updated}})

        return public_rollout(self.store.update(persist).rollouts[name])

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
        elif fleet_state(state)["targets"][deployment.target]["overcommitted"]:
            raise ValueError("rollout Target capacity is overcommitted")

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
        policy = self._policy(state, deployment, application)
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
            route_fingerprint=_fingerprint({
                "generation": generation,
                "stable": stable.model_dump(mode="json"),
                "weights": [100, 0],
                "affinity_generation": 0,
            }),
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
            if application is None:
                raise ValueError("rollout application changed; request a fresh plan")
            try:
                policy = self._policy(state, deployment, application)
            except KeyError:
                raise ValueError(
                    "rollout dependencies changed; request a fresh plan"
                ) from None
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
                "candidate_eligible": True, "candidate_health": "ready",
            })
            return state.model_copy(update={"rollouts": {**state.rollouts, name: updated}})

        return public_rollout(self.store.update(activate).rollouts[name])
