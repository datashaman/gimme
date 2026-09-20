from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, cast

from gimme import resources_postgres as resources_postgres_module
from gimme import resources_valkey as resources_valkey_module
from gimme.control import (
    AWSElastiCacheValkeyResource,
    AWSRDSPostgresResource,
    AWSSecretsManagerStore,
    ControlState,
)
from gimme.control_plans import resource_provision_plan, valkey_provision_plan
from gimme.resources_postgres import ResourceError

Name = str
PlanId = str


@dataclass(frozen=True)
class ManagedResourceOrchestrator:
    """Own non-destructive managed Resource provision and inspection."""

    store: Any
    rds_postgres: Any
    elasticache_valkey: Any
    deployment_resource_locks: Callable[..., Any]
    assert_plan: Callable[..., Any]

    def _refuse_unverifiable_tls_region(
        self, state: ControlState, resource: AWSRDSPostgresResource
    ) -> None:
        network = state.aws_networks.get(resource.aws_network)
        if network is not None and network.region.startswith(("us-gov-", "cn-")):
            raise ResourceError("aws_rds_tls_region_unsupported")

    def _refuse_unavailable_node_type(
        self, state: ControlState, resource: AWSElastiCacheValkeyResource
    ) -> None:
        network = state.aws_networks[resource.aws_network]
        options = self.elasticache_valkey.live_options(
            state.provider_accounts[network.provider_account], network
        )
        if resource.node_type not in options.node_types:
            raise ResourceError("aws_elasticache_node_type_unavailable")

    def _managed_resource(self, name: str) -> tuple[ControlState, AWSRDSPostgresResource]:
        state = self.store.load()
        resource = state.resources.get(name)
        if resource is None:
            raise KeyError(f"resource '{name}' is not registered")
        if not isinstance(resource, AWSRDSPostgresResource):
            raise ValueError(f"resource '{name}' is not a managed AWS RDS PostgreSQL resource")
        self._refuse_unverifiable_tls_region(state, resource)
        return (state, resource)

    def _managed_valkey(
        self, name: str
    ) -> tuple[ControlState, AWSElastiCacheValkeyResource] | None:
        state = self.store.load()
        resource = state.resources.get(name)
        return (state, resource) if isinstance(resource, AWSElastiCacheValkeyResource) else None

    def _resource_provision_plan(self, name: str) -> dict[str, object]:
        if (valkey := self._managed_valkey(name)) is not None:
            observed = resources_valkey_module.load_observed(self.store.root, name)
            return valkey_provision_plan(name, valkey[1], observed)
        _state, resource = self._managed_resource(name)
        observed = resources_postgres_module.load_observed(self.store.root, name)
        return resource_provision_plan(name, resource, observed)

    def _resource_deployment_names(self, name: str) -> list[str]:
        return sorted(
            (
                deployment_name
                for deployment_name, deployment in self.store.load().deployments.items()
                if deployment.resources.database == name
                or (
                    deployment.resources.valkey is not None
                    and deployment.resources.valkey.resource == name
                )
            )
        )

    def plan_apply_resource(self, name: Name) -> dict[str, object]:
        """Plan provisioning or reconciling one managed AWS RDS PostgreSQL instance or
        ElastiCache Valkey replication group."""
        return self._resource_provision_plan(name)

    def apply_resource(self, name: Name, plan_id: PlanId) -> dict[str, object]:
        """Create the RDS instance, or converge an existing one onto desired state with one
        immediate modification, polling at most 30 seconds before returning a bounded pending
        phase. Never returns a decrypted credential."""
        with self.deployment_resource_locks(*self._resource_deployment_names(name)):
            expected = self._resource_provision_plan(name)
            self.assert_plan(expected, plan_id)
            if (valkey := self._managed_valkey(name)) is not None:
                state, cache = valkey
                network = state.aws_networks[cache.aws_network]
                workload_store = cast(
                    AWSSecretsManagerStore, state.secret_stores[cache.workload_secret_store]
                )
                return {
                    "changed": True,
                    **resources_valkey_module.apply_provision(
                        self.elasticache_valkey,
                        self.store.root,
                        state.provider_accounts[network.provider_account],
                        network,
                        cache,
                        name,
                        workload_store,
                        cache.workload_secret_store,
                    ),
                }
            state, resource = self._managed_resource(name)
            network = state.aws_networks[resource.aws_network]
            account = state.provider_accounts[network.provider_account]
            result = resources_postgres_module.apply_provision(
                self.rds_postgres, self.store.root, account, network, resource, name
            )
            return {"changed": True, **result}

    def inspect_resource(self, name: Name) -> dict[str, object]:
        """Read-only, secret-free provider identity, health, and version for one resource."""
        state = self.store.load()
        resource = state.resources.get(name)
        if resource is None:
            raise KeyError(f"resource '{name}' is not registered")
        if isinstance(resource, AWSElastiCacheValkeyResource):
            return self._inspect_valkey(state, name, resource)
        if not isinstance(resource, AWSRDSPostgresResource):
            return {
                "resource": name,
                "provider": resource.provider,
                "target": resource.target,
                "kind": resource.kind,
                "version": resource.version,
            }
        observed = resources_postgres_module.load_observed(self.store.root, name)
        network = state.aws_networks[resource.aws_network]
        account = state.provider_accounts[network.provider_account]
        live: resources_postgres_module.InstanceObservation | None = None
        refresh_error: str | None = None
        try:
            live = self.rds_postgres.describe_instance(
                account, network, resources_postgres_module.derive_instance_identifier(name)
            )
        except ResourceError as exc:
            refresh_error = str(exc)
        result: dict[str, object] = {
            "resource": name,
            "provider": "aws_rds_postgres",
            "phase": "absent" if observed is None else observed["phase"],
            "source": "cache" if live is None else "live",
        }
        if refresh_error is not None:
            result["refresh_error"] = refresh_error
        if live is not None:
            result.update(
                phase="ready"
                if live.status == "available" and (not live.converging)
                else "pending",
                status=live.status,
                engine_version=live.engine_version,
                identity=live.identity,
                endpoint=live.endpoint,
                port=live.port,
                drift=resources_postgres_module.instance_drift(resource, live),
            )
        elif observed is not None:
            result.update(
                status=observed["status"],
                engine_version=observed["engine_version"],
                identity=observed["identity"],
                endpoint=observed["endpoint"],
                port=observed["port"],
            )
        if observed is not None:
            result["allocations"] = {
                deployment_name: {
                    "database": allocation["database_identifier"],
                    "status": allocation["status"],
                }
                for deployment_name, allocation in cast(
                    dict[str, dict[str, object]], observed["allocations"]
                ).items()
            }
        return result

    def _inspect_valkey(
        self, state: ControlState, name: str, resource: AWSElastiCacheValkeyResource
    ) -> dict[str, object]:
        """Bounded and secret-free: no endpoint, address, ARN, user, or secret identifier."""
        observed = resources_valkey_module.load_observed(self.store.root, name)
        network = state.aws_networks[resource.aws_network]
        group_id = resources_valkey_module.derive_group_id(name)
        live: resources_valkey_module.GroupObservation | None = None
        refresh_error: str | None = None
        try:
            live = self.elasticache_valkey.describe_group(
                state.provider_accounts[network.provider_account], network, group_id
            )
        except ResourceError as exc:
            refresh_error = str(exc)
        result: dict[str, object] = {
            "resource": name,
            "provider": resource.provider,
            "kind": resource.kind,
            "phase": "absent" if observed is None else observed["phase"],
            "source": "cache" if live is None else "live",
        }
        if refresh_error is not None:
            result["refresh_error"] = refresh_error
        operation = resources_valkey_module.busy_operation(self.store.root, name)
        if operation is not None:
            result["operation"] = operation
            progress = resources_valkey_module.operation_progress(self.store.root, name, operation)
            if progress:
                result["progress"] = progress
        if live is not None:
            issues = resources_valkey_module.structural_issues(resource, live, group_id)
            result.update(
                phase="restoring"
                if operation == "restoring"
                else progress.get("phase", "destroying")
                if operation == "destroying"
                else "pending"
                if operation == "provisioning"
                else resources_valkey_module.group_phase(live, issues),
                status=live.status,
                engine_version=live.engine_version,
                effective_durability=live.effective_durability,
                issues=issues,
                drift=resources_valkey_module.group_drift(resource, live),
            )
        elif observed is not None:
            result.update(
                phase=progress.get("phase", "destroying")
                if operation == "destroying"
                else "pending"
                if operation == "provisioning"
                else result["phase"],
                status=observed["status"],
                engine_version=observed["engine_version"],
                effective_durability=observed["effective_durability"],
                issues=observed["issues"],
            )
        elif operation == "provisioning":
            result["phase"] = "pending"
        if observed is not None:
            result["allocations"] = {
                deployment_name: {"status": allocation["status"]}
                for deployment_name, allocation in cast(
                    dict[str, dict[str, object]], observed["allocations"]
                ).items()
            }
        return result
