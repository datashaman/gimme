from __future__ import annotations

import base64
import json
from contextlib import nullcontext
from pathlib import Path

import pytest

from gimme.artifact_deployment_orchestration import release_contract
from gimme.control import ControlState, StateStore
from gimme.deployer import CommandResult
from gimme.fleet_orchestration import fleet_state
from gimme.rollout_orchestration import RolloutOrchestrator


EXAMPLE = Path(__file__).parents[1] / "config" / "state.example.json"


def rollout_state(*, slots: int = 2, stage: str = "staging") -> ControlState:
    document = json.loads(EXAMPLE.read_text())
    deployment = document["deployments"]["example-local"]
    deployment["release_mode"] = "artifact"
    deployment["stage"] = stage
    deployment["app_debug"] = False
    deployment["app_env"] = "production"
    deployment["source"] = {"kind": "commit", "ref": "b" * 40}
    deployment["runtimes"] = {
        "php": {"provider": "system", "version": "8.4.1"},
        "composer": {"provider": "system", "version": "2.8.4"},
    }
    document["applications"]["example"]["frontend"] = None
    document["targets"]["devbox"]["deployment_slots"] = slots
    return ControlState.model_validate(document)


class MemoryStore:
    def __init__(self, state: ControlState):
        self.state = state

    def load(self) -> ControlState:
        return self.state

    def update(self, operation):
        self.state = operation(self.state)
        return self.state


class ArtifactSupport:
    def __init__(self, store: MemoryStore):
        self.store = store

    @staticmethod
    def identity(commit: str, extensions: list[str]) -> dict[str, object]:
        return {
            "commit": commit,
            "capability": {
                "php": "8.4.1",
                "composer": "2.8.4",
                "php_extensions": extensions,
                "system": "linux",
                "machine": "x86_64",
                "frontend": None,
            },
        }

    def live_release(self, name: str) -> dict[str, object]:
        state = self.store.load()
        deployment = state.deployments[name]
        application = state.applications[deployment.application]
        return {
            "application": "example",
            "commit": "a" * 40,
            "build_id": "build_v1_" + "1" * 64,
            "artifact_digest": "2" * 64,
            "tree_digest": "3" * 64,
            "package_version": "private-package-v1",
            "manifest_version": "private-manifest-v1",
            "release_contract": release_contract(deployment, application),
        }

    def expected_from_release(self, name: str, metadata: dict[str, object]):
        return {"build_id": metadata["build_id"]}

    def context(self, name: str) -> dict[str, object]:
        state = self.store.load()
        deployment = state.deployments[name]
        application = state.applications[deployment.application]
        return {
            "state": state,
            "deployment": deployment,
            "application": application,
            "identity": self.identity("b" * 40, application.php_extensions),
            "reader_credentials": {},
            "artifact": {
                "status": "ready",
                "application": "example",
                "commit": "b" * 40,
                "build_id": "build_v1_" + "4" * 64,
                "artifact_digest": "5" * 64,
                "tree_digest": "6" * 64,
                "package_version": "private-package-v2",
                "manifest_version": "private-manifest-v2",
            },
        }

    def apply_arguments(self, context: dict[str, object]):
        return {"operation": "materialize", "artifact": context["artifact"]}, nullcontext(None)


def operations(state: ControlState, calls: list, *, fail_prepare: bool = False):
    store = MemoryStore(state)
    artifacts = ArtifactSupport(store)

    def run(task: str, name: str, **kwargs):
        current = store.load().rollouts.get(name)
        calls.append((task, name, kwargs, None if current is None else current.phase))
        if task == "gimme:rollout:prepare" and fail_prepare:
            raise RuntimeError("private target output")
        output = "\n".join([
            "GIMME_RUNTIME|php|8.4.1",
            "GIMME_PLATFORM|linux|x86_64",
            *(
                f"GIMME_PHP_EXTENSION|{extension}|ready"
                for extension in store.load().applications["example"].php_extensions
            ),
        ])
        return CommandResult([], 0, output)

    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("stale")

    return store, RolloutOrchestrator(store, artifacts, run, assert_plan)


def target_output(value: dict[str, object]) -> str:
    encoded = base64.b64encode(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).decode()
    return f"GIMME_ROLLOUT_STATE|{encoded}"


class RoutingTarget:
    def __init__(self, store: MemoryStore):
        self.store = store
        self.observed: dict[str, object] = {"configured": False}
        self.fail_weights = False
        self.calls: list[tuple[str, dict[str, object]]] = []

    def __call__(self, task: str, name: str, **kwargs) -> CommandResult:
        self.calls.append((task, kwargs))
        if task == "gimme:preflight:artifact-runtimes":
            extensions = self.store.load().applications["example"].php_extensions
            return CommandResult([], 0, "\n".join([
                "GIMME_RUNTIME|php|8.4.1",
                "GIMME_PLATFORM|linux|x86_64",
                *(f"GIMME_PHP_EXTENSION|{item}|ready" for item in extensions),
            ]))
        if task == "gimme:rollout:inspect":
            return CommandResult([], 0, target_output(self.observed))
        if task == "gimme:rollout:weights":
            if self.fail_weights:
                raise RuntimeError("private target failure with 192.0.2.8")
            policy = kwargs["rollout_policy"]
            self.observed = {
                "configured": True,
                "generation": policy["generation"],
                "phase": "active",
                "affinity_generation": policy["affinity_generation"],
                "stable_weight": policy["stable_weight"],
                "candidate_weight": policy["candidate_weight"],
                "stable_eligible": policy["stable_weight"] > 0,
                "candidate_eligible": policy["candidate_weight"] > 0,
                "stable_health": "ready",
                "candidate_health": "ready",
                "stable_identity": policy["stable_identity"],
                "candidate_identity": policy["candidate_identity"],
                "route_fingerprint": policy["route_fingerprint"],
                "outcome": "ready",
            }
            return CommandResult([], 0, target_output(self.observed))
        raise AssertionError(task)


def active_rollout() -> tuple[MemoryStore, RolloutOrchestrator, RoutingTarget]:
    store, starter = operations(rollout_state(), [])
    plan = starter.plan_start("example-local")
    starter.start("example-local", plan["plan_id"])
    target = RoutingTarget(store)

    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("stale")

    return store, RolloutOrchestrator(
        store, ArtifactSupport(store), target, assert_plan
    ), target


def test_start_persists_one_zero_traffic_reservation_and_prepares_backend() -> None:
    calls: list = []
    store, rollout = operations(rollout_state(), calls)

    plan = rollout.plan_start("example-local")
    assert "private-package" not in json.dumps(plan)
    assert "private-manifest" not in json.dumps(plan)
    result = rollout.start("example-local", plan["plan_id"])

    assert result["phase"] == "active"
    assert result["stable_weight"] == 100
    assert result["candidate_weight"] == 0
    assert result["background_owner"] == "stable"
    assert result["backend_ready"] is True
    assert fleet_state(store.load())["targets"]["devbox"]["occupied_slots"] == 2
    prepare = [call for call in calls if call[0] == "gimme:rollout:prepare"]
    assert len(prepare) == 1
    assert prepare[0][2]["rollout_generation"] == plan["generation"]
    assert prepare[0][3] == "preparing"


def test_interruption_is_inspectable_and_same_generation_retry_does_not_duplicate_slot() -> None:
    calls: list = []
    store, failing = operations(rollout_state(), calls, fail_prepare=True)
    plan = failing.plan_start("example-local")
    with pytest.raises(RuntimeError, match="rollout_candidate_prepare_failed"):
        failing.start("example-local", plan["plan_id"])

    degraded = failing.inspect("example-local")
    assert degraded["phase"] == "degraded"
    assert degraded["backend_ready"] is False
    assert fleet_state(store.load())["targets"]["devbox"]["occupied_slots"] == 2

    retry_calls: list = []
    retry_store, retry = operations(store.load(), retry_calls)
    retry_plan = retry.plan_start("example-local")
    assert retry_plan["generation"] == plan["generation"]
    assert retry_plan["retry"] is True
    assert retry.start("example-local", retry_plan["plan_id"])["phase"] == "active"
    assert fleet_state(retry_store.load())["targets"]["devbox"]["occupied_slots"] == 2


@pytest.mark.parametrize(
    ("state", "message"),
    [
        (rollout_state(stage="preview"), "staging or production"),
        (rollout_state(slots=1), "free Target slot"),
    ],
)
def test_rollout_admission_is_bounded(state: ControlState, message: str) -> None:
    _, rollout = operations(state, [])
    with pytest.raises(ValueError, match=message):
        rollout.plan_start("example-local")


def test_source_mode_is_rejected_before_remote_reads() -> None:
    document = json.loads(EXAMPLE.read_text())
    document["targets"]["devbox"]["deployment_slots"] = 2
    state = ControlState.model_validate(document)
    _, rollout = operations(state, [])

    with pytest.raises(ValueError, match="artifact release mode"):
        rollout.plan_start("example-local")


def test_changed_stable_release_contract_is_rejected() -> None:
    store = MemoryStore(rollout_state())

    class IncompatibleArtifacts(ArtifactSupport):
        def live_release(self, name: str) -> dict[str, object]:
            value = super().live_release(name)
            value["release_contract"] = {
                "schema": "laravel_release_v1",
                "health_sha256": "7" * 64,
                "processes_sha256": "8" * 64,
            }
            return value

    rollout = RolloutOrchestrator(
        store,
        IncompatibleArtifacts(store),
        lambda *_args, **_kwargs: CommandResult([], 0, ""),
        lambda *_args: None,
    )
    with pytest.raises(RuntimeError, match="rollout_contract_incompatible"):
        rollout.plan_start("example-local")


def test_interruption_after_target_prepare_leaves_preparing_generation_retryable() -> None:
    class InterruptingStore(MemoryStore):
        updates = 0

        def update(self, operation):
            self.updates += 1
            if self.updates == 2:
                raise KeyboardInterrupt
            return super().update(operation)

    calls: list = []
    store = InterruptingStore(rollout_state())
    artifacts = ArtifactSupport(store)

    def run(task: str, name: str, **kwargs):
        calls.append((task, name, kwargs))
        return CommandResult([], 0, "\n".join([
            "GIMME_RUNTIME|php|8.4.1",
            "GIMME_PLATFORM|linux|x86_64",
            *(
                f"GIMME_PHP_EXTENSION|{extension}|ready"
                for extension in store.load().applications["example"].php_extensions
            ),
        ]))

    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("stale")

    rollout = RolloutOrchestrator(
        store, artifacts, run, assert_plan,
    )
    plan = rollout.plan_start("example-local")
    with pytest.raises(KeyboardInterrupt):
        rollout.start("example-local", plan["plan_id"])
    assert store.load().rollouts["example-local"].phase == "preparing"

    retry_store, retry = operations(store.load(), [])
    retry_plan = retry.plan_start("example-local")
    assert retry_plan["generation"] == plan["generation"]
    assert retry.start("example-local", retry_plan["plan_id"])["phase"] == "active"
    assert len(retry_store.load().rollouts) == 1


def test_schema_v7_migration_adds_empty_rollout_collection(tmp_path: Path) -> None:
    document = rollout_state().model_dump(mode="json")
    document["schema_version"] = 7
    document.pop("rollouts")
    (tmp_path / "state.json").write_text(json.dumps(document))

    migrated = StateStore(tmp_path).state_migration({}, {})

    assert migrated.schema_version == 8
    assert migrated.rollouts == {}


def test_target_loss_is_a_fixed_error_without_raw_output() -> None:
    store = MemoryStore(rollout_state())
    artifacts = ArtifactSupport(store)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("ssh 192.0.2.99 leaked-private-output")

    rollout = RolloutOrchestrator(store, artifacts, unavailable, lambda *_args: None)
    with pytest.raises(RuntimeError, match="^rollout_target_unavailable$"):
        rollout.plan_start("example-local")


def test_recoverable_generation_rejects_changed_deployment_policy() -> None:
    store, rollout = operations(rollout_state(), [], fail_prepare=True)
    plan = rollout.plan_start("example-local")
    with pytest.raises(RuntimeError, match="rollout_candidate_prepare_failed"):
        rollout.start("example-local", plan["plan_id"])
    deployment = store.state.deployments["example-local"]
    store.state = store.state.model_copy(update={
        "deployments": {
            **store.state.deployments,
            "example-local": deployment.model_copy(update={"app_env": "staging"}),
        }
    })

    with pytest.raises(ValueError, match="generation conflicts"):
        rollout.plan_start("example-local")


def test_weight_transition_is_reviewed_and_persists_only_after_target_success() -> None:
    store, rollout, target = active_rollout()

    plan = rollout.plan_weights("example-local", 90, 10)
    assert plan["current_weights"] == {"stable": 100, "candidate": 0}
    assert plan["proposed_weights"] == {"stable": 90, "candidate": 10}
    assert store.load().rollouts["example-local"].candidate_weight == 0

    result = rollout.apply_weights("example-local", 90, 10, plan["plan_id"])

    assert result["stable_weight"] == 90
    assert result["candidate_weight"] == 10
    assert result["affinity_generation"] == result["generation"]
    assert result["candidate_eligible"] is True
    policy = next(
        kwargs["rollout_policy"]
        for task, kwargs in target.calls
        if task == "gimme:rollout:weights"
    )
    assert "signing" not in json.dumps(policy).lower()
    assert "cookie" not in json.dumps(policy).lower()


@pytest.mark.parametrize(
    ("stable", "candidate"),
    [(-1, 101), (101, -1), (50, 49), (True, 99)],
)
def test_invalid_rollout_weights_are_rejected(stable, candidate) -> None:
    _, rollout, _ = active_rollout()

    with pytest.raises(ValueError, match="totaling 100"):
        rollout.plan_weights("example-local", stable, candidate)


def test_failed_weight_transition_keeps_desired_weights_and_redacts_target_error() -> None:
    store, rollout, target = active_rollout()
    plan = rollout.plan_weights("example-local", 75, 25)
    target.fail_weights = True

    with pytest.raises(RuntimeError, match="^rollout_weight_transition_failed$"):
        rollout.apply_weights("example-local", 75, 25, plan["plan_id"])

    current = store.load().rollouts["example-local"]
    assert (current.stable_weight, current.candidate_weight) == (100, 0)


def test_candidate_traffic_requires_ready_direct_health() -> None:
    store, rollout, _ = active_rollout()
    current = store.load().rollouts["example-local"]
    store.state = store.state.model_copy(update={
        "rollouts": {
            **store.state.rollouts,
            "example-local": current.model_copy(update={"candidate_health": "unavailable"}),
        }
    })

    with pytest.raises(ValueError, match="ready candidate backend"):
        rollout.plan_weights("example-local", 90, 10)


def test_rollout_inspection_reports_target_loss_without_private_output() -> None:
    store, _, _ = active_rollout()

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("ssh output included 192.0.2.9 and a private cookie")

    rollout = RolloutOrchestrator(
        store, ArtifactSupport(store), unavailable, lambda *_args: None
    )
    result = rollout.inspect("example-local")

    assert result["drift"] == "target_unavailable"
    assert result["stable_health"] == "unknown"
    assert result["candidate_health"] == "unknown"
    assert "192.0.2.9" not in json.dumps(result)


def test_weight_retry_recovers_after_target_success_before_local_persist() -> None:
    class InterruptingStore(MemoryStore):
        interrupt = True

        def update(self, operation):
            if self.interrupt:
                self.interrupt = False
                raise KeyboardInterrupt
            return super().update(operation)

    initial, _, _ = active_rollout()
    store = InterruptingStore(initial.load())
    target = RoutingTarget(store)

    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("stale")

    rollout = RolloutOrchestrator(
        store, ArtifactSupport(store), target, assert_plan,
    )
    plan = rollout.plan_weights("example-local", 80, 20)
    with pytest.raises(KeyboardInterrupt):
        rollout.apply_weights("example-local", 80, 20, plan["plan_id"])

    assert store.load().rollouts["example-local"].candidate_weight == 0
    retry = rollout.plan_weights("example-local", 80, 20)
    assert retry["retry"] is True
    assert rollout.apply_weights(
        "example-local", 80, 20, retry["plan_id"]
    )["candidate_weight"] == 20


def test_weight_transition_rejects_route_drift_and_target_loss() -> None:
    _, rollout, target = active_rollout()
    plan = rollout.plan_weights("example-local", 50, 50)
    rollout.apply_weights("example-local", 50, 50, plan["plan_id"])
    target.observed["route_fingerprint"] = "rollout_" + "3" * 64
    with pytest.raises(ValueError, match="route is drifted"):
        rollout.plan_weights("example-local", 90, 10)

    target.observed = {"not": "safe"}
    with pytest.raises(RuntimeError, match="rollout_target_state_invalid"):
        rollout.plan_weights("example-local", 90, 10)
