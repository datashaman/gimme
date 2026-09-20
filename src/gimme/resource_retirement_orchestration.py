from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, cast

from gimme import resources_postgres as resources_postgres_module
from gimme import resources_valkey as resources_valkey_module
from gimme.control import (
    AWSElastiCacheValkeyResource,
    AWSRDSPostgresResource,
    AWSSecretsManagerStore,
)
from gimme.control_plans import (
    exact_plan,
    resource_cleanup_plan,
    resource_forget_plan,
    valkey_destroy_plan,
)
from gimme.resources_postgres import ResourceError


@dataclass(frozen=True)
class ResourceRetirementOrchestrator:
    """Own retained Resource removal and separately authorized destruction."""

    store: Any
    elasticache_valkey: Any
    assert_plan: Callable[..., Any]
    delete: Callable[..., Any]

    def resource_cleanup_plan(self, name: str) -> dict[str, object]:
        state = self.store.load()
        resource = state.resources.get(name)
        if resource is None:
            raise KeyError(f"resource '{name}' is not registered")
        if any(
            deployment.resources.database == name
            or getattr(deployment.resources.valkey, "resource", None) == name
            for deployment in state.deployments.values()
        ):
            raise ValueError(f"resource {name} is still referenced by a deployment")
        if isinstance(resource, AWSElastiCacheValkeyResource):
            return resource_cleanup_plan(
                name, managed=True, subject="ElastiCache replication group"
            )
        return resource_cleanup_plan(
            name, managed=isinstance(resource, AWSRDSPostgresResource)
        )

    def plan_cleanup_resource(self, name: str) -> dict[str, object]:
        return self.resource_cleanup_plan(name)

    def apply_cleanup_resource(
        self, name: str, plan_id: str, confirmation: str
    ) -> dict[str, object]:
        expected = self.resource_cleanup_plan(name)
        self.assert_plan(expected, plan_id)
        if confirmation != expected["confirmation"]:
            raise ValueError(
                f"confirmation must exactly equal '{expected['confirmation']}'"
            )
        state = self.store.load()
        resource = state.resources[name]
        retained = isinstance(
            resource, (AWSRDSPostgresResource, AWSElastiCacheValkeyResource)
        )
        if isinstance(resource, AWSElastiCacheValkeyResource):
            resources_valkey_module.retain_group(
                self.store.root, name, resource.aws_network
            )
        elif isinstance(resource, AWSRDSPostgresResource):
            resources_postgres_module.retain_resource(
                self.store.root, name, resource.aws_network
            )
        self.store.save(self.delete(state, "resources", name))
        return {"changed": True, "resource": name, "retained": retained}

    def resource_destroy_plan(self, name: str) -> dict[str, object]:
        state = self.store.load()
        resource = state.resources.get(name)
        if not isinstance(resource, AWSElastiCacheValkeyResource):
            raise ValueError(
                f"resource {name} is not a managed ElastiCache Valkey resource"
            )
        if any(
            getattr(deployment.resources.valkey, "resource", None) == name
            for deployment in state.deployments.values()
        ):
            raise ValueError(f"resource {name} is still referenced by a deployment")
        observed = resources_valkey_module.load_observed(self.store.root, name)
        if observed is not None and set(
            cast(dict[str, object], observed["allocations"])
        ) & set(state.deployments):
            raise ResourceError("aws_elasticache_destroy_bindings_remain")
        account = state.provider_accounts[
            state.aws_networks[resource.aws_network].provider_account
        ]
        if account.destructive_role_arn is None:
            raise ResourceError("aws_elasticache_destroy_role_missing")
        fingerprint, users = resources_valkey_module.destruction_targets(
            self.store.root, name
        )
        return valkey_destroy_plan(
            name,
            fingerprint,
            resources_valkey_module.final_snapshot_id(
                resources_valkey_module.derive_group_id(name), fingerprint
            ),
            len(users),
        )

    def plan_destroy_resource(self, name: str) -> dict[str, object]:
        return self.resource_destroy_plan(name)

    def apply_destroy_resource(
        self, name: str, plan_id: str, confirmation: str
    ) -> dict[str, object]:
        expected = self.resource_destroy_plan(name)
        self.assert_plan(expected, plan_id)
        if confirmation != expected["confirmation"]:
            raise ValueError(
                f"confirmation must exactly equal '{expected['confirmation']}'"
            )
        state = self.store.load()
        resource = cast(AWSElastiCacheValkeyResource, state.resources[name])
        network = state.aws_networks[resource.aws_network]
        observed = resources_valkey_module.load_observed(self.store.root, name)
        secret_names = (
            ["_admin", *sorted(cast(dict[str, object], observed["allocations"]))]
            if observed
            else ["_admin"]
        )
        result = resources_valkey_module.apply_destroy(
            self.elasticache_valkey,
            self.store.root,
            state.provider_accounts[network.provider_account],
            network,
            name,
            str(expected["identity_fingerprint"]),
        )
        if result["destroyed"]:
            self.store.save(self.delete(self.store.load(), "resources", name))
            resources_valkey_module.record_destroyed_group(
                self.store.root,
                name,
                resource.aws_network,
                str(expected["identity_fingerprint"]),
            )
            resources_valkey_module.record_destroyed_secrets(
                self.store.root,
                name,
                resource.workload_secret_store,
                secret_names,
            )
        return {"changed": True, **result}

    def final_snapshot_purge_plan(self, name: str) -> dict[str, object]:
        state = self.store.load()
        if name in state.resources:
            raise ValueError(f"resource {name} is still registered")
        receipt = resources_valkey_module.load_destroyed_receipt(
            self.store.root, name
        )
        if receipt is None:
            raise KeyError(f"no destroyed Valkey resource named '{name}'")
        network = state.aws_networks.get(receipt["aws_network"])
        if network is None:
            raise ResourceError("aws_elasticache_destroy_receipt_invalid")
        account = state.provider_accounts[network.provider_account]
        if account.destructive_role_arn is None:
            raise ResourceError("aws_elasticache_destroy_role_missing")
        return exact_plan(
            {
                "kind": "valkey_final_snapshot_purge",
                "resource": name,
                "confirmation": f"PURGE FINAL SNAPSHOT {name}",
                "snapshot": receipt["final_snapshot"],
                "destroys": [
                    "the final snapshot retained after this Resource was destroyed"
                ],
                "retains": ["manual snapshots and Secrets Manager credentials"],
                "authority": (
                    "the Provider Account's destructive role, assumed only during apply"
                ),
                "irreversible": True,
            }
        )

    def plan_purge_final_snapshot(self, name: str) -> dict[str, object]:
        return self.final_snapshot_purge_plan(name)

    def apply_purge_final_snapshot(
        self, name: str, plan_id: str, confirmation: str
    ) -> dict[str, object]:
        expected = self.final_snapshot_purge_plan(name)
        self.assert_plan(expected, plan_id)
        if confirmation != expected["confirmation"]:
            raise ValueError(
                f"confirmation must exactly equal '{expected['confirmation']}'"
            )
        receipt = resources_valkey_module.load_destroyed_receipt(
            self.store.root, name
        )
        if receipt is None:
            raise KeyError(f"no destroyed Valkey resource named '{name}'")
        state = self.store.load()
        network = state.aws_networks[receipt["aws_network"]]
        account = state.provider_accounts[network.provider_account]
        deleted = self.elasticache_valkey.delete_final_snapshot(
            account, network, receipt["final_snapshot"]
        )
        resources_valkey_module.clear_destroyed_receipt(self.store.root, name)
        return {"changed": deleted, "resource": name, "purged": deleted}

    def retained_secret_purge_plan(self, name: str) -> dict[str, object]:
        state = self.store.load()
        if name in state.resources:
            raise ValueError(f"resource {name} is still registered")
        receipt = resources_valkey_module.load_destroyed_secrets(
            self.store.root, name
        )
        if receipt is None:
            raise KeyError(f"no destroyed Valkey credentials named '{name}'")
        store_name, secrets = receipt
        secret_store = state.secret_stores.get(store_name)
        if not isinstance(secret_store, AWSSecretsManagerStore):
            raise ResourceError("aws_elasticache_destroy_receipt_invalid")
        account = state.provider_accounts[secret_store.provider_account]
        if account.destructive_role_arn is None:
            raise ResourceError("aws_elasticache_destroy_role_missing")
        return exact_plan(
            {
                "kind": "valkey_retained_secret_purge",
                "resource": name,
                "confirmation": f"PURGE RETAINED SECRETS {name}",
                "credentials": len(secrets),
                "destroys": [
                    "Gimme-owned Valkey administrative and deployment credentials"
                ],
                "authority": (
                    "the Provider Account's destructive role, assumed only during apply"
                ),
                "irreversible": True,
            }
        )

    def plan_purge_retained_secrets(self, name: str) -> dict[str, object]:
        return self.retained_secret_purge_plan(name)

    def apply_purge_retained_secrets(
        self, name: str, plan_id: str, confirmation: str
    ) -> dict[str, object]:
        expected = self.retained_secret_purge_plan(name)
        self.assert_plan(expected, plan_id)
        if confirmation != expected["confirmation"]:
            raise ValueError(
                f"confirmation must exactly equal '{expected['confirmation']}'"
            )
        store_name, secret_names = cast(
            tuple[str, list[str]],
            resources_valkey_module.load_destroyed_secrets(self.store.root, name),
        )
        state = self.store.load()
        secret_store = cast(AWSSecretsManagerStore, state.secret_stores[store_name])
        account = state.provider_accounts[secret_store.provider_account]
        deleted = self.elasticache_valkey.delete_retained_secrets(
            account, secret_store, store_name, name, secret_names
        )
        resources_valkey_module.clear_destroyed_secrets(self.store.root, name)
        return {"changed": deleted > 0, "resource": name, "purged": deleted}

    def resource_forget_plan(self, name: str) -> dict[str, object]:
        if name in self.store.load().resources:
            raise ValueError(
                f"resource {name} is still registered; only a retained one is forgotten"
            )
        try:
            retained = (
                resources_postgres_module.load_retained(self.store.root, name)
                is not None
            )
        except ResourceError:
            retained = True
        if not retained:
            raise KeyError(f"no retained resource named '{name}'")
        return resource_forget_plan(name)

    def plan_forget_resource(self, name: str) -> dict[str, object]:
        return self.resource_forget_plan(name)

    def apply_forget_resource(
        self, name: str, plan_id: str, confirmation: str
    ) -> dict[str, object]:
        expected = self.resource_forget_plan(name)
        self.assert_plan(expected, plan_id)
        if confirmation != expected["confirmation"]:
            raise ValueError(
                f"confirmation must exactly equal '{expected['confirmation']}'"
            )
        resources_postgres_module.forget_retained(self.store.root, name)
        return {"changed": True, "resource": name}
