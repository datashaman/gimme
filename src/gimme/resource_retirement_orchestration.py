from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, cast

from gimme import resources_postgres as resources_postgres_module
from gimme import resources_valkey as resources_valkey_module
from gimme.control import (
    AWSElastiCacheValkeyResource,
    AWSRDSPostgresResource,
    AWSSecretsManagerStore,
    legacy_server,
)
from gimme.control_plans import (
    exact_plan,
    postgres_allocation_purge_plan,
    postgres_destroy_plan,
    resource_cleanup_plan,
    resource_forget_plan,
    valkey_destroy_plan,
)
from gimme.resources_postgres import ResourceError
from gimme.secrets import protected_secret_file


@dataclass(frozen=True)
class ResourceRetirementOrchestrator:
    """Own retained Resource removal and separately authorized destruction."""

    store: Any
    elasticache_valkey: Any
    rds_postgres: Any
    runner: Any
    postgres_recovery_evidence: Callable[..., Any]
    deployment_resource_lock: Callable[..., Any]
    assert_plan: Callable[..., Any]
    delete: Callable[..., Any]

    def detach_postgres_allocation(
        self, deployment_name: str, resource_name: str
    ) -> dict[str, object]:
        state = self.store.load()
        resource = state.resources.get(resource_name)
        if not isinstance(resource, AWSRDSPostgresResource):
            return {"detached": False}
        if resources_postgres_module.load_rotation(self.store.root, resource_name) is not None:
            raise ResourceError("aws_rds_rotate_in_progress")
        observed = resources_postgres_module.load_observed(self.store.root, resource_name)
        if observed is None:
            raise ResourceError("observed_resource_missing")
        allocation = cast(
            dict[str, dict[str, object]], observed["allocations"]
        ).get(deployment_name)
        if allocation is None:
            return {"detached": False}
        if allocation["status"] == "detached":
            return {"detached": True, "already_detached": True}
        network = state.aws_networks[resource.aws_network]
        account = state.provider_accounts[network.provider_account]
        live = self.rds_postgres.describe_instance(
            account, network,
            resources_postgres_module.derive_instance_identifier(resource_name),
        )
        if (
            live is None
            or resources_postgres_module.readiness_issues(resource, live)
            or live.identity != observed["identity"]
        ):
            raise ResourceError("aws_rds_detach_resource_not_ready")
        secret_arn = observed["master_secret_arn"]
        if not isinstance(secret_arn, str):
            raise ResourceError("aws_rds_master_secret_missing")
        if self.rds_postgres.master_secret_version_fingerprint(
            account, network.region, secret_arn
        ) != observed["master_secret_version_fingerprint"]:
            raise ResourceError("aws_rds_detach_master_secret_stale")
        evidence = self.postgres_recovery_evidence(deployment_name, allocation)
        username, password = self.rds_postgres.resolve_master_credential(
            account, network.region, secret_arn
        )
        admin_target = state.targets[resource.administration_target]
        with protected_secret_file(
            {"username": username, "password": password}
        ) as secret_file:
            self.runner.run(
                "gimme:resource:retire-postgres-login",
                legacy_server(admin_target),
                stack=admin_target.stack,
                resource_endpoint=(str(observed["endpoint"]), int(observed["port"])),
                resource_login=str(allocation["login_role"]),
                secret_file=secret_file,
                resource_trust_bundle_sha256=(
                    resources_postgres_module.RDS_TRUST_BUNDLE_SHA256
                ),
                timeout=120,
            )
        detached = resources_postgres_module.detach_allocation(
            self.store.root, resource_name, deployment_name, evidence
        )
        return {
            "detached": True,
            "generation": detached["generation"],
            "recovery_evidence": detached["recovery_evidence"] is not None,
        }

    def allocation_purge_plan(
        self, name: str, deployment: str
    ) -> dict[str, object]:
        marker = resources_postgres_module.load_allocation_purge(
            self.store.root, name, deployment
        )
        if marker is not None:
            return cast(dict[str, object], marker["plan"])
        state = self.store.load()
        resource = state.resources.get(name)
        if not isinstance(resource, AWSRDSPostgresResource):
            raise ValueError(f"resource {name} is not a managed RDS PostgreSQL resource")
        if any(
            deployment_name == deployment and configured.resources.database == name
            for deployment_name, configured in state.deployments.items()
        ):
            raise ResourceError("aws_rds_allocation_still_bound")
        observed = resources_postgres_module.load_observed(self.store.root, name)
        allocation = (
            None if observed is None
            else cast(dict[str, dict[str, object]], observed["allocations"]).get(deployment)
        )
        if allocation is None or allocation["status"] != "detached":
            raise ResourceError("aws_rds_detached_allocation_missing")
        if not resources_postgres_module.recovery_evidence_is_fresh(allocation):
            raise ResourceError("aws_rds_allocation_recovery_evidence_missing")
        evidence = self.postgres_recovery_evidence(deployment, allocation)
        if evidence != allocation["recovery_evidence"]:
            raise ResourceError("aws_rds_allocation_recovery_evidence_stale")
        network = state.aws_networks[resource.aws_network]
        account = state.provider_accounts[network.provider_account]
        if account.destructive_role_arn is None:
            raise ResourceError("aws_rds_destroy_role_missing")
        return postgres_allocation_purge_plan(
            name,
            deployment,
            resources_postgres_module.identity_fingerprint(str(observed["identity"])),
            self.store.digest(allocation),
            cast(dict[str, object], evidence),
        )

    def plan_purge_resource_allocation(
        self, name: str, deployment: str
    ) -> dict[str, object]:
        return self.allocation_purge_plan(name, deployment)

    def apply_purge_resource_allocation(
        self, name: str, deployment: str, plan_id: str, confirmation: str
    ) -> dict[str, object]:
        with self.deployment_resource_lock(deployment):
            expected = self.allocation_purge_plan(name, deployment)
            self.assert_plan(expected, plan_id)
            if confirmation != expected["confirmation"]:
                raise ValueError(
                    f"confirmation must exactly equal '{expected['confirmation']}'"
                )
            state = self.store.load()
            resource = state.resources.get(name)
            if not isinstance(resource, AWSRDSPostgresResource):
                raise ResourceError("aws_rds_resource_missing")
            if any(
                deployment_name == deployment and configured.resources.database == name
                for deployment_name, configured in state.deployments.items()
            ):
                raise ResourceError("aws_rds_allocation_still_bound")
            observed = resources_postgres_module.load_observed(self.store.root, name)
            if observed is None:
                raise ResourceError("observed_resource_missing")
            marker = resources_postgres_module.load_allocation_purge(
                self.store.root, name, deployment
            )
            allocation = cast(
                dict[str, dict[str, object]], observed["allocations"]
            ).get(deployment)
            if allocation is None:
                if marker is not None and marker["phase"] == "secret_scheduled":
                    resources_postgres_module.clear_allocation_purge(
                        self.store.root, name, deployment
                    )
                    return {
                        "changed": False,
                        "resource": name,
                        "deployment": deployment,
                        "purged": True,
                        "secret_recovery_window_days": (
                            resources_postgres_module.DELETION_RECOVERY_DAYS
                        ),
                    }
                raise ResourceError("aws_rds_detached_allocation_missing")
            if allocation["status"] != "detached":
                raise ResourceError("aws_rds_detached_allocation_missing")
            if self.store.digest(allocation) != expected["allocation_fingerprint"]:
                raise ValueError("plan_id is invalid or stale; request a fresh plan")
            if marker is None:
                if self.postgres_recovery_evidence(deployment, allocation) != allocation[
                    "recovery_evidence"
                ]:
                    raise ResourceError("aws_rds_allocation_recovery_evidence_stale")
                marker = {
                    "schema_version": 1,
                    "resource": name,
                    "deployment": deployment,
                    "phase": "prepared",
                    "plan": expected,
                }
                resources_postgres_module.save_allocation_purge(
                    self.store.root, name, deployment, marker
                )
            network = state.aws_networks[resource.aws_network]
            account = state.provider_accounts[network.provider_account]
            live = self.rds_postgres.describe_instance(
                account, network,
                resources_postgres_module.derive_instance_identifier(name),
            )
            if (
                live is None
                or live.identity != observed["identity"]
                or resources_postgres_module.identity_fingerprint(live.identity)
                != expected["identity_fingerprint"]
            ):
                raise ResourceError("aws_rds_allocation_purge_identity_changed")
            secret_arn = observed["master_secret_arn"]
            if not isinstance(secret_arn, str):
                raise ResourceError("aws_rds_master_secret_missing")
            if marker["phase"] == "prepared":
                username, password = self.rds_postgres.resolve_master_credential(
                    account, network.region, secret_arn
                )
                admin_target = state.targets[resource.administration_target]
                with protected_secret_file(
                    {"username": username, "password": password}
                ) as secret_file:
                    self.runner.run(
                        "gimme:resource:purge-postgres-allocation",
                        legacy_server(admin_target),
                        stack=admin_target.stack,
                        resource_endpoint=(str(observed["endpoint"]), int(observed["port"])),
                        resource_database=str(allocation["database_identifier"]),
                        resource_owner=str(allocation["owner_role"]),
                        resource_login=str(allocation["login_role"]),
                        secret_file=secret_file,
                        resource_trust_bundle_sha256=(
                            resources_postgres_module.RDS_TRUST_BUNDLE_SHA256
                        ),
                        timeout=180,
                    )
                marker = {**marker, "phase": "database_deleted"}
                resources_postgres_module.save_allocation_purge(
                    self.store.root, name, deployment, marker
                )
            secret_store = cast(
                AWSSecretsManagerStore,
                state.secret_stores[resource.workload_secret_store],
            )
            if marker["phase"] == "database_deleted":
                self.rds_postgres.schedule_workload_secret_deletion(
                    account,
                    secret_store,
                    f"{name}/{deployment}",
                    {
                        "gimme:secret-store": resource.workload_secret_store,
                        "gimme:resource": name,
                        "gimme:deployment": deployment,
                    },
                )
                marker = {**marker, "phase": "secret_scheduled"}
                resources_postgres_module.save_allocation_purge(
                    self.store.root, name, deployment, marker
                )
            resources_postgres_module.remove_allocation(
                self.store.root, name, deployment
            )
            resources_postgres_module.clear_allocation_purge(
                self.store.root, name, deployment
            )
            return {
                "changed": True,
                "resource": name,
                "deployment": deployment,
                "purged": True,
                "secret_recovery_window_days": (
                    resources_postgres_module.DELETION_RECOVERY_DAYS
                ),
            }

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
        if isinstance(resource, AWSRDSPostgresResource):
            observed = resources_postgres_module.load_observed(self.store.root, name)
            if observed is not None and any(
                allocation["status"] == "active"
                for allocation in cast(
                    dict[str, dict[str, object]], observed["allocations"]
                ).values()
            ):
                raise ResourceError("aws_rds_cleanup_active_allocations")
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
            network = state.aws_networks[resource.aws_network]
            resources_postgres_module.retain_resource(
                self.store.root,
                name,
                network.provider_account,
                resource.aws_network,
                resource.workload_secret_store,
            )
        self.store.save(self.delete(state, "resources", name))
        return {"changed": True, "resource": name, "retained": retained}

    def resource_destroy_plan(self, name: str) -> dict[str, object]:
        state = self.store.load()
        resource = state.resources.get(name)
        if isinstance(resource, AWSRDSPostgresResource):
            if any(
                deployment.resources.database == name
                for deployment in state.deployments.values()
            ):
                raise ValueError(f"resource {name} is still referenced by a deployment")
            observed = resources_postgres_module.load_observed(self.store.root, name)
            if observed is None:
                raise ResourceError("aws_rds_destroy_not_observed")
            allocations = cast(
                dict[str, dict[str, object]], observed["allocations"]
            )
            if any(item["status"] != "detached" for item in allocations.values()):
                raise ResourceError("aws_rds_destroy_bindings_remain")
            in_progress = resources_postgres_module.load_destroy_marker(
                self.store.root, name
            ) is not None
            safe_allocations: list[dict[str, object]] = []
            for deployment, allocation in sorted(allocations.items()):
                if not resources_postgres_module.recovery_evidence_is_fresh(allocation):
                    raise ResourceError("aws_rds_destroy_recovery_evidence_missing")
                if (
                    not in_progress
                    and self.postgres_recovery_evidence(deployment, allocation)
                    != allocation["recovery_evidence"]
                ):
                    raise ResourceError("aws_rds_destroy_recovery_evidence_stale")
                evidence = cast(dict[str, object], allocation["recovery_evidence"])
                safe_allocations.append({
                    "deployment": deployment,
                    "generation": allocation["generation"],
                    "recovery_point_id": evidence["recovery_point_id"],
                    "captured_at": evidence["captured_at"],
                })
            network = state.aws_networks[resource.aws_network]
            account = state.provider_accounts[network.provider_account]
            if account.destructive_role_arn is None:
                raise ResourceError("aws_rds_destroy_role_missing")
            fingerprint = resources_postgres_module.identity_fingerprint(
                str(observed["identity"])
            )
            return postgres_destroy_plan(
                name,
                fingerprint,
                resources_postgres_module.final_snapshot_id(name, fingerprint),
                safe_allocations,
            )
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
        resource = state.resources[name]
        if isinstance(resource, AWSRDSPostgresResource):
            network = state.aws_networks[resource.aws_network]
            result = resources_postgres_module.apply_destroy(
                self.rds_postgres,
                self.store.root,
                state.provider_accounts[network.provider_account],
                network,
                name,
                str(expected["identity_fingerprint"]),
                str(expected["final_snapshot"]),
                int(expected["generation"]),
            )
            if result["destroyed"]:
                self.store.save(self.delete(self.store.load(), "resources", name))
            return {"changed": True, **result}
        resource = cast(AWSElastiCacheValkeyResource, resource)
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
            # Forget is deliberately local-only and remains the escape hatch for a
            # legacy or corrupt tombstone. The resource name fixes the sole path.
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
