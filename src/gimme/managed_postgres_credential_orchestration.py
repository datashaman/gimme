from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable, cast

from gimme import resources_postgres
from gimme.control import (
    AWSRDSPostgresResource,
    AWSSecretsManagerStore,
    StateStore,
    legacy_server,
)
from gimme.control_plans import postgres_rotation_plan
from gimme.resources_postgres import ResourceError
from gimme.secrets import protected_secret_file


@dataclass(frozen=True)
class ManagedPostgresCredentialOrchestrator:
    """Own one transactional managed PostgreSQL workload credential rotation."""

    store: Any
    rds_postgres: Any
    context: Callable[..., Any]
    deployment_resource_lock: Callable[..., Any]
    assert_plan: Callable[..., Any]
    apply_resources: Callable[..., Any]
    resource_plan: Callable[..., Any]
    runner: Any
    rotating_ok: Any

    def _context(self, name: str, deployment: str) -> tuple[Any, ...]:
        state = self.store.load()
        resource = state.resources.get(name)
        configured = state.deployments.get(deployment)
        if not isinstance(resource, AWSRDSPostgresResource):
            raise ResourceError("aws_rds_rotate_resource_invalid")
        if configured is None or configured.resources.database != name:
            raise ResourceError("aws_rds_rotate_binding_missing")
        observed = resources_postgres.load_observed(self.store.root, name)
        allocations = (
            {} if observed is None
            else cast(dict[str, dict[str, object]], observed["allocations"])
        )
        allocation = allocations.get(deployment)
        if observed is None or observed["phase"] != "ready" or allocation is None:
            raise ResourceError("aws_rds_rotate_binding_missing")
        network = state.aws_networks[resource.aws_network]
        secret_store = state.secret_stores[resource.workload_secret_store]
        if not isinstance(secret_store, AWSSecretsManagerStore):
            raise ResourceError("aws_rds_workload_secret_store_invalid")
        return state, resource, configured, observed, allocation, network, secret_store

    def rotation_plan(self, name: str, deployment: str) -> dict[str, object]:
        marker = resources_postgres.load_rotation(self.store.root, name)
        if marker is not None:
            if marker["deployment"] != deployment:
                raise ResourceError("aws_rds_rotate_in_progress")
            return cast(dict[str, object], marker["plan"])
        _state, _resource, _configured, observed, allocation, _network, _store = (
            self._context(name, deployment)
        )
        fingerprint = "sha256:" + hashlib.sha256(
            str(observed["identity"]).encode()
        ).hexdigest()
        return postgres_rotation_plan(
            name,
            deployment,
            fingerprint,
            StateStore.digest(_resource.model_dump(mode="json")),
            int(allocation["generation"]),
        )

    def apply_rotation(
        self, name: str, deployment: str, plan_id: str
    ) -> dict[str, object]:
        with self.deployment_resource_lock(deployment):
            expected = self.rotation_plan(name, deployment)
            self.assert_plan(expected, plan_id)
            state, resource, _configured, observed, current, network, secret_store = (
                self._context(name, deployment)
            )
            if expected["resource_fingerprint"] != StateStore.digest(
                resource.model_dump(mode="json")
            ):
                raise ValueError("plan_id is invalid or stale; request a fresh plan")
            account = state.provider_accounts[network.provider_account]
            live = self.rds_postgres.describe_instance(
                account, network, resources_postgres.derive_instance_identifier(name)
            )
            if (
                live is None
                or resources_postgres.readiness_issues(resource, live)
                or live.identity != observed["identity"]
            ):
                raise ResourceError("aws_rds_rotate_resource_not_ready")
            marker = resources_postgres.load_rotation(self.store.root, name)
            if marker is None:
                previous = dict(current)
                generation = int(previous["generation"]) + 1
                candidate = resources_postgres.login_role(
                    str(previous["database_identifier"]), generation
                )
                marker = {
                    "schema_version": 1,
                    "resource": name,
                    "deployment": deployment,
                    "phase": "prepared",
                    "plan": expected,
                    "previous": previous,
                    "candidate_generation": generation,
                    "candidate_login": candidate,
                    "candidate": None,
                }
                resources_postgres.save_rotation(self.store.root, name, marker)
            else:
                previous = cast(dict[str, object], marker["previous"])
                generation = int(marker["candidate_generation"])
                candidate = str(marker["candidate_login"])
            database = str(previous["database_identifier"])
            owner = str(previous["owner_role"])
            secret_arn = observed["master_secret_arn"]
            if not isinstance(secret_arn, str):
                raise ResourceError("aws_rds_master_secret_missing")
            if self.rds_postgres.master_secret_version_fingerprint(
                account, network.region, secret_arn
            ) != observed["master_secret_version_fingerprint"]:
                raise ResourceError("aws_rds_rotate_master_secret_stale")
            master_username, master_password = self.rds_postgres.resolve_master_credential(
                account, network.region, secret_arn
            )
            admin_target = state.targets[resource.administration_target]
            if marker["phase"] == "rolling_back":
                candidate_allocation = cast(dict[str, object], marker["candidate"])
                try:
                    self._rollback(
                        name, deployment, account, secret_store, previous,
                        candidate_allocation, admin_target, observed, master_username,
                        master_password, candidate,
                    )
                    resources_postgres.clear_rotation(self.store.root, name)
                except Exception:
                    raise ResourceError("aws_rds_rotation_rollback_failed") from None
                raise ResourceError("aws_rds_rotation_activation_failed")
            if marker["phase"] == "prepared":
                password = resources_postgres.generate_workload_password()
                payload = {
                    "master_username": master_username,
                    "master_password": master_password,
                    "workload_username": candidate,
                    "workload_password": password,
                }
                try:
                    with protected_secret_file(payload) as secret_file:
                        self.runner.run(
                            "gimme:resource:bind-postgres",
                            legacy_server(admin_target),
                            stack=admin_target.stack,
                            resource_endpoint=(
                                str(observed["endpoint"]), int(observed["port"])
                            ),
                            resource_database=database,
                            resource_owner=owner,
                            resource_login=candidate,
                            resource_extensions=cast(
                                dict[str, str], previous["extensions"]
                            ),
                            secret_file=secret_file,
                            resource_trust_bundle_sha256=(
                                resources_postgres.RDS_TRUST_BUNDLE_SHA256
                            ),
                            timeout=120,
                        )
                except Exception:
                    try:
                        self._retire_login(
                            admin_target, observed, master_username, master_password, candidate
                        )
                        resources_postgres.clear_rotation(self.store.root, name)
                    except Exception:
                        raise ResourceError(
                            "aws_rds_rotation_prepare_cleanup_failed"
                        ) from None
                    raise ResourceError("aws_rds_rotation_prepare_failed") from None
                resources_postgres.persist_binding(
                    self.rds_postgres,
                    self.store.root,
                    account,
                    secret_store,
                    resource.workload_secret_store,
                    name,
                    deployment,
                    database,
                    owner,
                    candidate,
                    generation,
                    cast(dict[str, str], previous["extensions"]),
                    password,
                )
                refreshed = resources_postgres.load_observed(self.store.root, name)
                if refreshed is None:
                    raise ResourceError("aws_rds_rotation_state_missing")
                marker = {
                    **marker,
                    "phase": "published",
                    "candidate": cast(
                        dict[str, dict[str, object]], refreshed["allocations"]
                    )[deployment],
                }
                resources_postgres.save_rotation(self.store.root, name, marker)
            candidate_allocation = cast(dict[str, object], marker["candidate"])
            if marker["phase"] == "published":
                try:
                    token = self.rotating_ok.set(True)
                    try:
                        self.apply_resources(deployment, self.resource_plan(deployment))
                    finally:
                        self.rotating_ok.reset(token)
                except Exception:
                    marker = {**marker, "phase": "rolling_back"}
                    resources_postgres.save_rotation(self.store.root, name, marker)
                    try:
                        self._rollback(
                            name, deployment, account, secret_store, previous,
                            candidate_allocation, admin_target, observed,
                            master_username, master_password, candidate,
                        )
                        resources_postgres.clear_rotation(self.store.root, name)
                    except Exception:
                        raise ResourceError("aws_rds_rotation_rollback_failed") from None
                    raise ResourceError("aws_rds_rotation_activation_failed") from None
                marker = {**marker, "phase": "activated"}
                resources_postgres.save_rotation(self.store.root, name, marker)
            self._retire_login(
                admin_target,
                observed,
                master_username,
                master_password,
                str(previous["login_role"]),
            )
            resources_postgres.clear_rotation(self.store.root, name)
            return {
                "resource": name,
                "deployment": deployment,
                "rotated": True,
                "generation": generation,
            }

    def _rollback(
        self, name: str, deployment: str, account: Any,
        secret_store: AWSSecretsManagerStore, previous: dict[str, object],
        candidate_allocation: dict[str, object], admin_target: Any,
        observed: dict[str, object], master_username: str, master_password: str,
        candidate: str,
    ) -> None:
        """Idempotently restore every externally visible part of the prior generation."""
        self.rds_postgres.restore_workload_secret_version(
            account,
            secret_store,
            f"{name}/{deployment}",
            str(previous["secret_version_id"]),
            str(candidate_allocation["secret_version_id"]),
        )
        resources_postgres.restore_allocation(
            self.store.root, name, deployment, previous
        )
        self._retire_login(
            admin_target, observed, master_username, master_password, candidate
        )
        token = self.rotating_ok.set(True)
        try:
            self.apply_resources(deployment, self.resource_plan(deployment))
        finally:
            self.rotating_ok.reset(token)

    def _retire_login(
        self, admin_target: Any, observed: dict[str, object], username: str,
        password: str, login: str,
    ) -> None:
        with protected_secret_file({"username": username, "password": password}) as secret_file:
            self.runner.run(
                "gimme:resource:retire-postgres-login",
                legacy_server(admin_target),
                stack=admin_target.stack,
                resource_endpoint=(str(observed["endpoint"]), int(observed["port"])),
                resource_login=login,
                secret_file=secret_file,
                resource_trust_bundle_sha256=resources_postgres.RDS_TRUST_BUNDLE_SHA256,
                timeout=120,
            )
