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
    DeploymentConfig,
    ValkeyBinding,
    legacy_server,
)
from gimme.control_plans import (
    resource_binding_plan,
    resource_provision_plan,
    valkey_binding_plan,
    valkey_provision_plan,
)
from gimme.resources_postgres import ResourceError
from gimme.secrets import protected_secret_file

Name = str
PlanId = str


@dataclass(frozen=True)
class ManagedResourceOrchestrator:
    """Own non-destructive managed Resource provision, inspection, and binding."""

    store: Any
    rds_postgres: Any
    elasticache_valkey: Any
    deployment_resource_locks: Callable[..., Any]
    assert_plan: Callable[..., Any]
    runner: Any
    context: Callable[..., Any]

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

    def _managed_valkey_binding(
        self, name: str
    ) -> tuple[
        ControlState, DeploymentConfig, str, AWSElastiCacheValkeyResource
    ] | None:
        state, deployment, _target, _application = self.context(name)
        binding = deployment.resources.valkey
        resource = None if binding is None else state.resources.get(binding.resource)
        if binding is None or not isinstance(resource, AWSElastiCacheValkeyResource):
            return None
        return state, deployment, binding.resource, resource

    def _database_binding_plan(self, name: str) -> dict[str, object]:
        _state, deployment, _target, application = self.context(name)
        resource_name = deployment.resources.database
        if resource_name is None:
            raise ValueError(f"deployment {name} has no bound database resource")
        self._managed_resource(resource_name)
        conflicts = resources_postgres_module.find_allocation_resources(
            self.store.root, name, exclude=resource_name
        )
        if conflicts:
            raise ResourceError("aws_rds_detached_allocation_conflict")
        observed = resources_postgres_module.load_observed(self.store.root, resource_name)
        return resource_binding_plan(
            name, deployment, resource_name, observed, application.postgres_extensions
        )

    def _resource_binding_plan(self, name: str) -> dict[str, object]:
        valkey = self._managed_valkey_binding(name)
        if valkey is None:
            return self._database_binding_plan(name)
        state, deployment, resource_name, _resource = valkey
        binding = cast(ValkeyBinding, deployment.resources.valkey)
        uses = cast(list[str], binding.uses)
        database = deployment.resources.database
        return valkey_binding_plan(
            name,
            resource_name,
            uses,
            resources_valkey_module.namespace_prefixes(name, uses),
            resources_valkey_module.LARAVEL_PROFILE,
            resources_valkey_module.load_observed(self.store.root, resource_name),
            self._database_binding_plan(name)
            if isinstance(
                state.resources.get(database or ""), AWSRDSPostgresResource
            )
            else None,
        )

    def plan_bind_resource(self, name: Name) -> dict[str, object]:
        """Plan the Deployment's isolated managed PostgreSQL and Valkey allocations."""
        return self._resource_binding_plan(name)

    def bind_resource(self, name: Name, plan_id: PlanId) -> dict[str, object]:
        """Create or reconcile the Deployment's managed Resource allocations without
        returning either Resource Credential."""
        with self.deployment_resource_locks(name):
            expected = self._resource_binding_plan(name)
            self.assert_plan(expected, plan_id)
            valkey = self._managed_valkey_binding(name)
            if valkey is None:
                return {"changed": True, **self._bind_database(name, expected)}
            state, deployment, resource_name, resource = valkey
            ready = cast(dict[str, object], expected["valkey"])["resource_ready"]
            database = cast(dict[str, object] | None, expected["database"])
            if not ready or (database is not None and not database["resource_ready"]):
                raise ValueError("managed resource is not ready; run apply_resource first")
            result: dict[str, object] = {"changed": True}
            if database is not None:
                result.update(self._bind_database(name, database))
            network = state.aws_networks[resource.aws_network]
            workload_store = cast(
                AWSSecretsManagerStore,
                state.secret_stores[resource.workload_secret_store],
            )
            result["valkey"] = resources_valkey_module.apply_binding(
                self.elasticache_valkey,
                self.store.root,
                state.provider_accounts[network.provider_account],
                network,
                resource,
                resource_name,
                workload_store,
                resource.workload_secret_store,
                name,
                cast(list[str], cast(ValkeyBinding, deployment.resources.valkey).uses),
            )
            return result

    def _bind_database(
        self, name: str, expected: dict[str, object]
    ) -> dict[str, object]:
        if not expected["resource_ready"]:
            raise ValueError("managed resource is not ready; run apply_resource first")
        state, deployment, _target, _application = self.context(name)
        resource_name = str(expected["resource"])
        _state, resource = self._managed_resource(resource_name)
        network = state.aws_networks[resource.aws_network]
        account = state.provider_accounts[network.provider_account]
        admin_target = state.targets[resource.administration_target]
        store_name = resource.workload_secret_store
        workload_store = state.secret_stores[store_name]
        if not isinstance(workload_store, AWSSecretsManagerStore):
            raise ValueError("workload_secret_store must be an AWS Secrets Manager store")
        observed = resources_postgres_module.load_observed(self.store.root, resource_name)
        if observed is None or observed["master_secret_arn"] is None:
            raise ResourceError("aws_rds_master_secret_missing")
        if resources_postgres_module.load_rotation(self.store.root, resource_name) is not None:
            raise ResourceError("aws_rds_rotate_in_progress")
        live = self.rds_postgres.describe_instance(
            account,
            network,
            resources_postgres_module.derive_instance_identifier(resource_name),
        )
        if (
            live is None
            or resources_postgres_module.readiness_issues(resource, live)
            or live.identity != observed["identity"]
        ):
            raise ResourceError("aws_rds_binding_resource_not_ready")
        current_master_fingerprint = self.rds_postgres.master_secret_version_fingerprint(
            account, network.region, str(observed["master_secret_arn"])
        )
        if current_master_fingerprint != observed["master_secret_version_fingerprint"]:
            raise ResourceError("aws_rds_binding_master_secret_stale")
        master_username, master_password = self.rds_postgres.resolve_master_credential(
            account, network.region, str(observed["master_secret_arn"])
        )
        database_identifier = deployment.placement.database_identifier
        allocations = cast(dict[str, dict[str, object]], observed["allocations"])
        existing = allocations.get(name)
        if existing is None or existing["status"] == "detached":
            generation = 1 if existing is None else int(existing["generation"]) + 1
            owner = (
                resources_postgres_module.owner_role(database_identifier)
                if existing is None else str(existing["owner_role"])
            )
            if existing is not None and existing["database_identifier"] != database_identifier:
                raise ResourceError("aws_rds_detached_allocation_identity_mismatch")
            login = resources_postgres_module.login_role(database_identifier, generation)
            workload_password = resources_postgres_module.generate_workload_password()
        else:
            generation = int(existing["generation"])
            owner = str(existing["owner_role"])
            login, workload_password = self.rds_postgres.resolve_workload_credential(
                account,
                workload_store,
                f"{resource_name}/{name}",
                str(existing["secret_version_id"]),
            )
            if login != existing["login_role"]:
                raise ResourceError("aws_rds_workload_secret_identity_mismatch")
        payload = {
            "master_username": master_username,
            "master_password": master_password,
            "workload_username": login,
            "workload_password": workload_password,
        }
        with self.deployment_resource_locks(name), protected_secret_file(
            payload
        ) as secret_file:
            self.runner.run(
                "gimme:resource:bind-postgres",
                legacy_server(admin_target),
                stack=admin_target.stack,
                resource_endpoint=(
                    str(observed["endpoint"]),
                    int(cast(int, observed["port"])),
                ),
                resource_database=database_identifier,
                resource_owner=owner,
                resource_login=login,
                resource_extensions=cast(dict[str, str], expected["postgres_extensions"]),
                secret_file=secret_file,
                resource_trust_bundle_sha256=(
                    resources_postgres_module.RDS_TRUST_BUNDLE_SHA256
                ),
                timeout=120,
            )
            extensions = cast(dict[str, str], expected["postgres_extensions"])
            if existing is not None and existing["status"] == "active":
                resources_postgres_module.update_allocation_extensions(
                    self.store.root, resource_name, name, extensions
                )
                return {
                    "deployment": name,
                    "database": database_identifier,
                    "secret_reference": {
                        "store": store_name,
                        "secret": f"{resource_name}/{name}",
                    },
                }
            return resources_postgres_module.persist_binding(
                self.rds_postgres,
                self.store.root,
                account,
                workload_store,
                store_name,
                resource_name,
                name,
                database_identifier,
                owner,
                login,
                generation,
                extensions,
                workload_password,
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
            if resources_postgres_module.load_rotation(self.store.root, name) is not None:
                raise ResourceError("aws_rds_rotate_in_progress")
            network = state.aws_networks[resource.aws_network]
            account = state.provider_accounts[network.provider_account]
            prior_observed = resources_postgres_module.load_observed(
                self.store.root, name
            )
            reconstruction: list[dict[str, object]] = []
            if prior_observed is None:
                workload_store = cast(
                    AWSSecretsManagerStore,
                    state.secret_stores[resource.workload_secret_store],
                )
                reconstruction = self.rds_postgres.list_workload_secret_metadata(
                    account, workload_store, name
                )
                desired_bindings = {
                    deployment_name
                    for deployment_name, deployment in state.deployments.items()
                    if deployment.resources.database == name
                }
                if any(
                    str(item["deployment"]) not in desired_bindings
                    for item in reconstruction
                ):
                    raise ResourceError("aws_rds_reconstruction_ambiguous")
                if reconstruction and self.rds_postgres.describe_instance(
                    account, network,
                    resources_postgres_module.derive_instance_identifier(name),
                ) is None:
                    raise ResourceError("aws_rds_reconstruction_instance_missing")
            result = resources_postgres_module.apply_provision(
                self.rds_postgres, self.store.root, account, network, resource, name
            )
            if result["readiness_issues"] == ["aws_rds_not_ready_administration"]:
                observed = resources_postgres_module.load_observed(self.store.root, name)
                if (
                    observed is None
                    or observed["master_secret_arn"] is None
                    or observed["endpoint"] is None
                    or observed["port"] is None
                ):
                    raise ResourceError("aws_rds_administration_context_missing")
                secret_arn = str(observed["master_secret_arn"])
                fingerprint = self.rds_postgres.master_secret_version_fingerprint(
                    account, network.region, secret_arn
                )
                username, password = self.rds_postgres.resolve_master_credential(
                    account, network.region, secret_arn
                )
                with protected_secret_file(
                    {"username": username, "password": password}
                ) as secret_file:
                    verification = self.runner.run(
                        "gimme:resource:verify-postgres",
                        legacy_server(state.targets[resource.administration_target]),
                        stack=state.targets[resource.administration_target].stack,
                        resource_endpoint=(str(observed["endpoint"]), int(observed["port"])),
                        secret_file=secret_file,
                        resource_trust_bundle_sha256=(
                            resources_postgres_module.RDS_TRUST_BUNDLE_SHA256
                        ),
                        timeout=120,
                    )
                resources_postgres_module.mark_administration_verified(
                    self.store.root,
                    name,
                    fingerprint,
                    resources_postgres_module.parse_administration_verification(
                        verification.output
                    ),
                )
                result.update(
                    phase="ready",
                    readiness_issues=[],
                    administration_verified=True,
                )
                if reconstruction:
                    current = resources_postgres_module.load_observed(
                        self.store.root, name
                    )
                    if current is None:
                        raise ResourceError("observed_resource_missing")
                    extensions_available = cast(
                        dict[str, str], current["extension_versions"]
                    )
                    workload_store = cast(
                        AWSSecretsManagerStore,
                        state.secret_stores[resource.workload_secret_store],
                    )
                    admin_target = state.targets[resource.administration_target]
                    for metadata in reconstruction:
                        deployment_name = str(metadata["deployment"])
                        configured = state.deployments[deployment_name]
                        application = state.applications[configured.application]
                        extensions = {
                            extension: extensions_available[extension]
                            for extension in application.postgres_extensions
                            if extension in extensions_available
                        }
                        if len(extensions) != len(application.postgres_extensions):
                            raise ResourceError("aws_rds_reconstruction_extension_missing")
                        database = configured.placement.database_identifier
                        generation = int(metadata["generation"])
                        owner = resources_postgres_module.owner_role(database)
                        login = resources_postgres_module.login_role(database, generation)
                        actual_login, workload_password = (
                            self.rds_postgres.resolve_workload_credential(
                                account, workload_store, f"{name}/{deployment_name}",
                                str(metadata["secret_version_id"]),
                            )
                        )
                        if actual_login != login:
                            raise ResourceError("aws_rds_reconstruction_identity_mismatch")
                        with protected_secret_file({
                            "master_username": username,
                            "master_password": password,
                            "workload_username": login,
                            "workload_password": workload_password,
                        }) as secret_file:
                            self.runner.run(
                                "gimme:resource:bind-postgres",
                                legacy_server(admin_target),
                                stack=admin_target.stack,
                                resource_endpoint=(
                                    str(current["endpoint"]), int(current["port"])
                                ),
                                resource_database=database,
                                resource_owner=owner,
                                resource_login=login,
                                resource_extensions=extensions,
                                secret_file=secret_file,
                                resource_trust_bundle_sha256=(
                                    resources_postgres_module.RDS_TRUST_BUNDLE_SHA256
                                ),
                                timeout=120,
                            )
                        resources_postgres_module.record_binding_metadata(
                            self.store.root, name, deployment_name, database, owner,
                            login, generation, extensions, str(metadata["secret_arn"]),
                            str(metadata["secret_version_id"]),
                        )
                    result["reconstructed_allocations"] = len(reconstruction)
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
            issues = resources_postgres_module.readiness_issues(resource, live)
            administration_verified = bool(
                observed is not None
                and observed["administration_verified"]
                and observed["identity"] == live.identity
            )
            if not administration_verified:
                issues.append("aws_rds_not_ready_administration")
            try:
                master_version_current = bool(
                    observed is not None
                    and observed["master_secret_arn"] is not None
                    and self.rds_postgres.master_secret_version_fingerprint(
                        account, network.region, str(observed["master_secret_arn"])
                    ) == observed["master_secret_version_fingerprint"]
                )
            except ResourceError:
                master_version_current = False
            if not master_version_current:
                issues.append("aws_rds_not_ready_master_secret_version")
            result.update(
                phase="ready" if not issues else "pending",
                status=live.status,
                engine_version=live.engine_version,
                identity_fingerprint=resources_postgres_module.identity_fingerprint(
                    live.identity
                ),
                multi_az=live.multi_az,
                storage_encrypted=live.storage_encrypted,
                deletion_protection=live.deletion_protection,
                publicly_accessible=live.publicly_accessible,
                backup_retention_days=live.backup_retention_days,
                backup_window=live.backup_window,
                maintenance_window=live.maintenance_window,
                readiness_issues=issues,
                drift=resources_postgres_module.instance_drift(resource, live),
            )
        elif observed is not None:
            result.update(
                status=observed["status"],
                engine_version=observed["engine_version"],
                identity_fingerprint=resources_postgres_module.identity_fingerprint(
                    str(observed["identity"])
                ),
                multi_az=observed["multi_az"],
                storage_encrypted=observed["storage_encrypted"],
                deletion_protection=observed["deletion_protection"],
                publicly_accessible=observed["publicly_accessible"],
                backup_retention_days=observed["backup_retention_days"],
                backup_window=observed["backup_window"],
                maintenance_window=observed["maintenance_window"],
                readiness_issues=observed["readiness_issues"],
            )
        if observed is not None:
            result["allocations"] = {
                deployment_name: {
                    "database": allocation["database_identifier"],
                    "status": allocation["status"],
                    "generation": allocation["generation"],
                    "detached_at": allocation["detached_at"],
                    "recovery_ready": (
                        allocation["status"] == "detached"
                        and resources_postgres_module.recovery_evidence_is_fresh(
                            allocation
                        )
                    ),
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
