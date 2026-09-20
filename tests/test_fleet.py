from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from pydantic import ValidationError

from gimme.config import FrontendBuildConfig, StackConfig
from gimme.control import (
    ApplicationConfig,
    ControlState,
    DeploymentRegistration,
    DeploymentSource,
    PlacementPolicy,
    StateStore,
    TargetConfig,
    TargetNetwork,
    TargetRuntimePolicy,
    RuntimePin,
)
from gimme.fleet_orchestration import FleetPlacementOrchestrator, fleet_state


def target(name: str, slots: int) -> TargetConfig:
    return TargetConfig(
        host_alias=name,
        bootstrap_hostname="192.0.2.10",
        hostname=f"{name}.local",
        system_hostname=name,
        remote_user="deployer",
        apps_root="/srv/gimme/apps",
        deployment_slots=slots,
        network=TargetNetwork(mode="local_mdns", mdns_name=name),
        stack=StackConfig(
            package_manager="apt",
            packages=["git", "mise", "software-properties-common"],
            services=[],
        ),
        runtimes=TargetRuntimePolicy(mise_version="2026.9.9"),
    )


def registration(*candidates: str, explicit: str | None = None) -> DeploymentRegistration:
    return DeploymentRegistration(
        application="site",
        target=explicit,
        placement_policy=(
            None if explicit is not None else PlacementPolicy(candidates=list(candidates))
        ),
        stage="preview",
        release_mode="source",
        source=DeploymentSource(kind="branch", ref="main"),
        app_env="preview",
        runtimes={"bun": RuntimePin(provider="mise", version="1.1.38")},
    )


def state(slots: dict[str, int]) -> ControlState:
    return ControlState(
        targets={name: target(name, capacity) for name, capacity in slots.items()},
        applications={"site": ApplicationConfig(
            repository="https://github.com/example/site.git",
            framework="static",
            frontend=FrontendBuildConfig(
                package_manager="bun", build_script="build", output_dir="dist"
            ),
        )},
    )


def orchestrator(
    tmp_path: Path,
    selected: ControlState,
    observations: dict[str, dict[str, object] | Exception] | None = None,
) -> tuple[StateStore, FleetPlacementOrchestrator]:
    desired = StateStore(tmp_path)
    desired.save(selected)
    observed = observations or {}

    def observe(name, _definition):
        value = observed.get(name, {"status": "ready", "runtimes": {}})
        if isinstance(value, Exception):
            raise value
        return value

    def assert_plan(expected, actual):
        if expected["plan_id"] != actual:
            raise ValueError("plan_id is invalid or stale")

    def replace(current, collection, name, value):
        document = current.model_dump(mode="json")
        document[collection][name] = value.model_dump(mode="json")
        return ControlState.model_validate(document)

    return desired, FleetPlacementOrchestrator(
        store=desired,
        observe_target=observe,
        assert_plan=assert_plan,
        replace=replace,
    )


def test_policy_candidates_are_unique_bounded_and_normalized() -> None:
    assert PlacementPolicy(candidates=["target-b", "target-a"]).candidates == [
        "target-a", "target-b",
    ]
    with pytest.raises(ValidationError, match="unique"):
        PlacementPolicy(candidates=["target-a", "target-a"])
    with pytest.raises(ValidationError, match="exactly one"):
        value = registration("target-a").model_dump()
        value["target"] = "target-a"
        DeploymentRegistration.model_validate(value)


def test_policy_selection_uses_ratio_then_free_slots_then_name(tmp_path: Path) -> None:
    desired, operations = orchestrator(
        tmp_path, state({"target-a": 2, "target-b": 4, "target-c": 4})
    )
    plan = operations.registration_plan(
        "site-preview", registration("target-c", "target-a", "target-b")
    )

    assert plan["ready"] is True
    assert [item["target"] for item in plan["candidates"]] == [
        "target-a", "target-b", "target-c",
    ]
    assert plan["selected_target"] == "target-b"
    result = operations.register_deployment(
        "site-preview",
        registration("target-c", "target-a", "target-b"),
        plan["plan_id"],
    )
    assert result["target"] == "target-b"
    assert desired.deployment("site-preview").placement_decision.mode == "policy"


def test_partial_reachability_and_no_eligibility_are_explained(tmp_path: Path) -> None:
    _, operations = orchestrator(
        tmp_path,
        state({"target-a": 1, "target-b": 1}),
        {"target-a": RuntimeError("private provider detail")},
    )
    plan = operations.registration_plan(
        "site-preview", registration("target-a", "target-b")
    )
    assert plan["selected_target"] == "target-b"
    assert plan["candidates"][0]["reasons"] == ["target_unavailable"]
    assert "private provider detail" not in json.dumps(plan)

    _, unavailable = orchestrator(
        tmp_path / "none",
        state({"target-a": 0, "target-b": 0}),
    )
    rejected = unavailable.registration_plan(
        "site-preview", registration("target-a", "target-b")
    )
    assert rejected["ready"] is False
    assert rejected["selected_target"] is None
    assert all(item["reasons"] == ["target_full"] for item in rejected["candidates"])

    with pytest.raises(ValueError, match="unregistered Target"):
        operations.registration_plan(
            "unknown-preview", registration("target-a", "target-unknown")
        )


def test_apply_rejects_changed_capacity_and_prevents_last_slot_overbooking(
    tmp_path: Path,
) -> None:
    desired, operations = orchestrator(tmp_path, state({"target-a": 1}))
    first = registration(explicit="target-a")
    first_plan = operations.registration_plan("first", first)
    second_plan = operations.registration_plan("second", first)
    original_update = desired.update
    barrier = threading.Barrier(2)

    def competing_update(operation):
        barrier.wait(timeout=5)
        return original_update(operation)

    desired.update = competing_update
    outcomes: list[str] = []

    def claim(name: str, plan: dict[str, object]) -> None:
        try:
            operations.register_deployment(name, first, str(plan["plan_id"]))
        except ValueError as error:
            assert "invalid or stale" in str(error)
            outcomes.append("stale")
        else:
            outcomes.append("succeeded")

    threads = [
        threading.Thread(target=claim, args=("first", first_plan)),
        threading.Thread(target=claim, args=("second", second_plan)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    assert sorted(outcomes) == ["stale", "succeeded"]

    capacity = fleet_state(desired.load())["targets"]["target-a"]
    assert capacity == {
        "deployment_slots": 1,
        "occupied_slots": 1,
        "free_slots": 0,
        "overcommitted": False,
        "reservations": [next(iter(desired.load().deployments))],
    }


def test_capacity_reduction_reports_overcommit_without_eviction(tmp_path: Path) -> None:
    desired, operations = orchestrator(tmp_path, state({"target-a": 2}))
    definition = registration(explicit="target-a")
    for name in ("first", "second"):
        plan = operations.registration_plan(name, definition)
        operations.register_deployment(name, definition, plan["plan_id"])

    current = desired.load()
    reduced = current.model_copy(update={
        "targets": {
            "target-a": current.targets["target-a"].model_copy(
                update={"deployment_slots": 1}
            )
        }
    })
    desired.save(reduced)

    capacity = fleet_state(desired.load())["targets"]["target-a"]
    assert capacity["overcommitted"] is True
    assert capacity["reservations"] == ["first", "second"]
    rejected = operations.registration_plan("third", definition)
    assert rejected["ready"] is False
    assert rejected["candidates"][0]["reasons"] == ["target_overcommitted"]


def test_policy_apply_rechecks_observation_and_explicit_placement_does_not(
    tmp_path: Path,
) -> None:
    observations: dict[str, dict[str, object] | Exception] = {
        "target-a": {"status": "ready", "runtimes": {}}
    }
    desired, operations = orchestrator(
        tmp_path, state({"target-a": 2}), observations
    )
    policy = registration("target-a")
    plan = operations.registration_plan("policy", policy)
    observations["target-a"] = RuntimeError("lost")
    with pytest.raises(ValueError, match="invalid or stale"):
        operations.register_deployment("policy", policy, plan["plan_id"])
    assert "policy" not in desired.load().deployments

    explicit = registration(explicit="target-a")
    direct = operations.registration_plan("explicit", explicit)
    assert direct["ready"] is True


def test_transient_target_loss_can_retry_the_same_reviewed_plan(tmp_path: Path) -> None:
    ready = {"status": "ready", "runtimes": {}}
    observations: dict[str, dict[str, object] | Exception] = {"target-a": ready}
    desired, operations = orchestrator(
        tmp_path, state({"target-a": 1}), observations
    )
    definition = registration("target-a")
    plan = operations.registration_plan("site-preview", definition)
    observations["target-a"] = RuntimeError("temporarily down")
    with pytest.raises(ValueError, match="invalid or stale"):
        operations.register_deployment("site-preview", definition, plan["plan_id"])

    observations["target-a"] = ready
    assert operations.register_deployment(
        "site-preview", definition, plan["plan_id"]
    )["changed"] is True
    assert desired.deployment("site-preview").target == "target-a"


def test_runtime_eligibility_requires_exact_system_pins_but_allows_mise_install() -> None:
    definition = registration("target-a").model_copy(update={
        "runtimes": {
            "php": RuntimePin(provider="system", version="8.4.1"),
            "node": RuntimePin(provider="mise", version="22.12.0"),
        }
    })
    assert FleetPlacementOrchestrator._runtime_issues(
        definition, {"status": "ready", "runtimes": {"php": "8.4.1"}}
    ) == []
    assert FleetPlacementOrchestrator._runtime_issues(
        definition, {"status": "ready", "runtimes": {"php": "8.3.0"}}
    ) == ["runtime_incompatible"]


def test_target_loss_keeps_immutable_reservation_and_reports_degraded(
    tmp_path: Path,
) -> None:
    observations = {"target-a": {"status": "ready", "runtimes": {}}}
    desired, operations = orchestrator(
        tmp_path, state({"target-a": 1}), observations
    )
    definition = registration("target-a")
    plan = operations.registration_plan("site-preview", definition)
    operations.register_deployment("site-preview", definition, plan["plan_id"])
    observations["target-a"] = RuntimeError("host detail")

    inspected = operations.inspect_fleet()
    assert inspected["observations"]["target-a"]["status"] == "unavailable"
    assert inspected["targets"]["target-a"]["reservations"] == ["site-preview"]
    assert desired.deployment("site-preview").target == "target-a"


def test_schema_v6_migration_preserves_placements_without_spare_capacity(
    tmp_path: Path,
) -> None:
    current = state({"target-a": 9}).model_dump(mode="json")
    current["schema_version"] = 6
    current["targets"]["target-a"].pop("deployment_slots")
    desired = StateStore(tmp_path)
    desired.root.mkdir(parents=True, exist_ok=True)
    desired.state_path.write_text(json.dumps(current))

    migrated = desired.state_migration({}, {})

    assert migrated.schema_version == 7
    assert migrated.targets["target-a"].deployment_slots == 1
