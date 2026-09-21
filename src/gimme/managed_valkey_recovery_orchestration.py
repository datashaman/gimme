from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, cast

from gimme import resources_valkey as resources_valkey_module
from gimme import valkey_recovery
from gimme.control_plans import valkey_restore_plan, valkey_rotation_plan
from gimme.resources_postgres import ResourceError


@dataclass(frozen=True)
class ManagedValkeyRecoveryOrchestrator:
    """Own managed Valkey restore, empty recreation, and credential rotation."""

    store: Any
    elasticache_valkey: Any
    valkey_context: Callable[..., Any]
    deployment_resource_locks: Callable[..., Any]
    deployment_resource_lock: Callable[..., Any]
    resource_deployment_names: Callable[..., Any]
    assert_plan: Callable[..., Any]
    apply_resources: Callable[..., Any]
    resource_plan: Callable[..., Any]
    run_deployment: Callable[..., Any]
    restoring_ok: Any

    @staticmethod
    def binds(state: Any, deployment: str, name: str) -> bool:
        bound = state.deployments.get(deployment)
        return (
            bound is not None
            and getattr(bound.resources.valkey, "resource", None) == name
        )

    def restore_plan(self, name: str, snapshot: str | None) -> dict[str, object]:
        state, resource, _network, _account, _store = self.valkey_context(name)
        observed = resources_valkey_module.load_observed(self.store.root, name)
        if snapshot is None and observed is None:
            raise ResourceError("aws_elasticache_recreate_not_needed")
        return valkey_restore_plan(
            name,
            snapshot,
            sorted(
                deployment
                for deployment in valkey_recovery.restore_targets(
                    self.store.root, name
                )
                if self.binds(state, deployment, name)
            ),
            resource.engine_version,
        )

    def restore_valkey(self, name: str, snapshot: str | None) -> dict[str, object]:
        state, resource, network, account, workload_store = self.valkey_context(name)

        def verify(deployment: str) -> None:
            if not self.binds(self.store.load(), deployment, name):
                return
            token = self.restoring_ok.set(True)
            try:
                self.apply_resources(deployment, self.resource_plan(deployment))
                self.run_deployment(
                    "gimme:probe:valkey:current", deployment, timeout=300
                )
                self.run_deployment(
                    "gimme:restart:workers", deployment, timeout=300
                )
            finally:
                self.restoring_ok.reset(token)

        return {
            "changed": True,
            **valkey_recovery.apply_restore(
                self.elasticache_valkey,
                self.store.root,
                account,
                network,
                resource,
                name,
                workload_store,
                resource.workload_secret_store,
                snapshot,
                verify,
            ),
        }

    def plan_restore_resource(
        self, name: str, snapshot: str
    ) -> dict[str, object]:
        return self.restore_plan(name, snapshot)

    def apply_restore_resource(
        self, name: str, snapshot: str, plan_id: str
    ) -> dict[str, object]:
        with self.deployment_resource_locks(*self.resource_deployment_names(name)):
            self.assert_plan(self.restore_plan(name, snapshot), plan_id)
            return self.restore_valkey(name, snapshot)

    def plan_recreate_empty_resource(self, name: str) -> dict[str, object]:
        return self.restore_plan(name, None)

    def apply_recreate_empty_resource(
        self, name: str, plan_id: str, confirmation: str
    ) -> dict[str, object]:
        with self.deployment_resource_locks(*self.resource_deployment_names(name)):
            expected = self.restore_plan(name, None)
            self.assert_plan(expected, plan_id)
            if confirmation != expected["confirmation"]:
                raise ValueError(
                    f"confirmation must exactly equal '{expected['confirmation']}'"
                )
            return self.restore_valkey(name, None)

    def rotation_plan(self, name: str, deployment: str) -> dict[str, object]:
        state, _resource, _network, account, _store = self.valkey_context(name)
        if account.destructive_role_arn is None:
            raise ResourceError("aws_elasticache_destroy_role_missing")
        observed = resources_valkey_module.load_observed(self.store.root, name)
        if (
            observed is None
            or cast(dict[str, dict[str, object]], observed["allocations"]).get(
                deployment, {}
            ).get("status") != "active"
            or not self.binds(state, deployment, name)
        ):
            raise ResourceError("aws_elasticache_rotate_binding_missing")
        return valkey_rotation_plan(
            name,
            deployment,
            resources_valkey_module.identity_fingerprint(str(observed["identity"])),
        )

    def plan_rotate_resource_credential(
        self, name: str, deployment: str
    ) -> dict[str, object]:
        return self.rotation_plan(name, deployment)

    def apply_rotate_resource_credential(
        self, name: str, deployment: str, plan_id: str
    ) -> dict[str, object]:
        with self.deployment_resource_lock(deployment):
            self.assert_plan(self.rotation_plan(name, deployment), plan_id)
            _state, resource, network, account, workload_store = (
                self.valkey_context(name)
            )

            def switch(target: str) -> None:
                self.apply_resources(target, self.resource_plan(target))
                self.run_deployment(
                    "gimme:probe:valkey:current", target, timeout=300
                )
                self.run_deployment("gimme:restart:workers", target, timeout=300)

            return {
                "changed": True,
                **valkey_recovery.apply_rotation(
                    self.elasticache_valkey,
                    self.store.root,
                    account,
                    network,
                    resource,
                    name,
                    workload_store,
                    resource.workload_secret_store,
                    deployment,
                    switch,
                ),
            }
