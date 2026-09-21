from __future__ import annotations

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
