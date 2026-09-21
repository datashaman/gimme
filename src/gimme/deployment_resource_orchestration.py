from __future__ import annotations

import socket
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any, Callable, cast

from gimme import recovery_schedule as recovery_schedule_module
from gimme import postgres_contract
from gimme import resources_postgres as resources_postgres_module
from gimme import resources_valkey as resources_valkey_module
from gimme import valkey_contract
from gimme.control import (
    AWSElastiCacheValkeyResource,
    AWSRDSPostgresResource,
    ControlState,
    DeploymentConfig,
    SecretReference,
    StateStore,
    TargetConfig,
    legacy_server,
    runs_horizon,
    target_sites,
)
from gimme.control_plans import deployment_resource_plan
from gimme.resources_postgres import ResourceError
from gimme.secrets import (
    SecretError,
    load_applied_secret_manifest,
    plan_secret_references,
    protected_secret_file,
    resolve_planned_secret_references,
    save_applied_secret_manifest,
)


@dataclass(frozen=True)
class DeploymentResourceOrchestrator:
    """Own secret-safe Deployment Resource readiness and activation."""

    store: Any
    runner: Any
    aws_secrets: Any
    context: Callable[..., Any]
    run_deployment: Callable[..., Any]
    deployment_resource_lock: Callable[..., Any]
    assert_plan: Callable[..., Any]
    recovery_schedule_runtime_issues: Callable[..., Any]
    recovery_schedule_authority: Callable[..., Any]
    backup_destination_credentials: Callable[..., Any]
    valkey_capture_credential: Callable[..., Any]
    restoring_ok: Callable[[], bool]
    postgres_rotating_ok: Callable[[], bool]

    def dns_issues(
        self, deployment: DeploymentConfig, target: TargetConfig
    ) -> list[str]:
        if target.network.mode != "public_dns":
            return []
        try:
            actual = {
                item[4][0]
                for item in socket.getaddrinfo(
                    deployment.placement.site_host,
                    443,
                    type=socket.SOCK_STREAM,
                )
            }
        except socket.gaierror:
            return ["domain does not resolve"]
        return (
            []
            if actual & set(target.network.expected_addresses)
            else ["domain does not resolve to a declared target address"]
        )

    def valkey_runtime(
        self, name: str, state: ControlState, deployment: DeploymentConfig
    ) -> tuple[
        dict[str, str],
        dict[str, SecretReference],
        dict[str, object] | None,
        list[str],
    ]:
        """Return the managed Valkey contract values, references, probe, and issues."""
        binding = deployment.resources.valkey
        resource = None if binding is None else state.resources.get(binding.resource)
        if binding is None or not isinstance(resource, AWSElastiCacheValkeyResource):
            return {}, {}, None, []
        try:
            observed = resources_valkey_module.load_observed(
                self.store.root, binding.resource
            )
        except ResourceError:
            observed = None
        if observed is None or observed["phase"] not in (
            ("ready", "restoring") if self.restoring_ok() else ("ready",)
        ):
            return {}, {}, None, ["valkey_resource_not_ready"]
        if name not in cast(dict[str, object], observed["allocations"]):
            return {}, {}, None, ["valkey_binding_missing"]
        host, port = observed["endpoint"], observed["port"]
        if not isinstance(host, str) or not isinstance(port, int):
            return {}, {}, None, ["valkey_endpoint_missing"]
        return (
            valkey_contract.contract_variables(name, binding.uses, host, port),
            valkey_contract.credential_references(
                resource.workload_secret_store, binding.resource, name
            ),
            valkey_contract.probe_config(
                name,
                binding.uses,
                host,
                port,
                runs_horizon(deployment.workers),
            ),
            [],
        )

    def postgres_runtime(
        self, name: str, state: ControlState, deployment: DeploymentConfig
    ) -> tuple[dict[str, str], dict[str, SecretReference], list[str]]:
        binding = deployment.resources.database
        resource = None if binding is None else state.resources.get(binding)
        if binding is None or not isinstance(resource, AWSRDSPostgresResource):
            return {}, {}, []
        marker = resources_postgres_module.load_rotation(self.store.root, binding)
        if (
            marker is not None
            and marker["deployment"] == name
            and not self.postgres_rotating_ok()
        ):
            return {}, {}, ["postgres_credential_rotation_in_progress"]
        try:
            observed = resources_postgres_module.load_observed(self.store.root, binding)
        except ResourceError:
            observed = None
        if observed is None or observed["phase"] != "ready":
            return {}, {}, ["postgres_resource_not_ready"]
        allocations = cast(dict[str, dict[str, object]], observed["allocations"])
        allocation = allocations.get(name)
        if allocation is None or allocation["status"] != "active":
            return {}, {}, ["postgres_binding_missing"]
        host, port = observed["endpoint"], observed["port"]
        if not isinstance(host, str) or not isinstance(port, int):
            return {}, {}, ["postgres_endpoint_missing"]
        target = state.targets[deployment.target]
        return (
            postgres_contract.contract_variables(
                host,
                port,
                str(allocation["database_identifier"]),
                f"{target.apps_root}/{postgres_contract.TRUST_BUNDLE_PATH}",
            ),
            postgres_contract.credential_references(
                resource.workload_secret_store, binding, name
            ),
            [],
        )

    def secret_plan(
        self, name: str, state: ControlState, deployment: DeploymentConfig
    ) -> tuple[list[dict[str, str]], list[str]]:
        try:
            planned = plan_secret_references(
                state,
                self.store.secrets_path,
                {
                    **deployment.secrets,
                    **self.postgres_runtime(name, state, deployment)[1],
                    **self.valkey_runtime(name, state, deployment)[1],
                },
                self.aws_secrets,
                load_applied_secret_manifest(self.store.root, name),
            )
            issues = [
                "secret_reference_missing"
                for item in planned
                if item["status"] == "missing"
            ]
            return planned, issues
        except SecretError as exc:
            return [], [str(exc)]

    def _contract_summary(
        self, name: str, state: ControlState, deployment: DeploymentConfig
    ) -> dict[str, object] | None:
        binding = deployment.resources.valkey
        resource = None if binding is None else state.resources.get(binding.resource)
        if binding is None or not isinstance(resource, AWSElastiCacheValkeyResource):
            return None
        return {
            "contract": valkey_contract.CONTRACT,
            "resource": binding.resource,
            "uses": list(binding.uses),
            "variables_digest": StateStore.digest(
                self.valkey_runtime(name, state, deployment)[0]
            ),
            "namespaces": resources_valkey_module.namespace_prefixes(
                name, list(binding.uses)
            ),
            "adapters": {
                key: (
                    "redis"
                    if use in binding.uses
                    else valkey_contract.LOCAL_DRIVERS[key]
                )
                for use, key in valkey_contract.ADAPTER_KEYS.items()
            },
            "credential_keys": [
                valkey_contract.USERNAME_KEY,
                valkey_contract.PASSWORD_KEY,
            ],
            "probes": valkey_contract.probe_names(
                list(binding.uses), runs_horizon(deployment.workers)
            ),
        }

    def managed_database_issues(
        self, state: ControlState, deployment: DeploymentConfig
    ) -> list[str]:
        name = next(
            (
                candidate
                for candidate, configured in state.deployments.items()
                if configured == deployment
            ),
            None,
        )
        return (
            [] if name is None else self.postgres_runtime(name, state, deployment)[2]
        )

    def resource_plan(self, name: str) -> dict[str, Any]:
        state, deployment, target, application = self.context(name)
        secret_versions, secret_issues = self.secret_plan(name, state, deployment)
        issues = (
            secret_issues
            + self.dns_issues(deployment, target)
            + self.postgres_runtime(name, state, deployment)[2]
            + self.recovery_schedule_runtime_issues(deployment, target)
            + self.valkey_runtime(name, state, deployment)[3]
        )
        if (
            deployment.recovery is not None
            and deployment.recovery.cadence.kind != "manual"
            and deployment.resources.database is not None
            and isinstance(
                state.resources.get(deployment.resources.database),
                AWSRDSPostgresResource,
            )
        ):
            issues.append("managed_postgres_recovery_schedule_unsupported")
        schedule = None
        authority = self.recovery_schedule_authority(name, state, deployment)
        if authority is not None:
            schedule = recovery_schedule_module.schedule_plan(authority)
        plan = deployment_resource_plan(
            name,
            deployment,
            target,
            application,
            missing_secrets=issues,
            secret_versions=secret_versions,
            valkey_contract=self._contract_summary(name, state, deployment),
            recovery_schedule=schedule,
        )
        if issues:
            plan["readiness_issues"] = issues
            plan["plan_id"] = StateStore.digest(
                {key: value for key, value in plan.items() if key != "plan_id"}
            )
        return plan

    def plan_deployment_resources(self, name: str) -> dict[str, object]:
        return self.resource_plan(name)

    def apply_deployment_resources(
        self, name: str, plan_id: str
    ) -> dict[str, object]:
        with self.deployment_resource_lock(name):
            expected = self.resource_plan(name)
            self.assert_plan(expected, plan_id)
            return self.apply_resources(name, expected)

    def apply_resources(
        self, name: str, expected: dict[str, Any]
    ) -> dict[str, object]:
        if not expected["ready"]:
            raise ValueError(
                "deployment resources are not ready; inspect readiness_issues"
            )
        state, deployment, target, _ = self.context(name)
        resolved = resolve_planned_secret_references(
            state,
            self.store.secrets_path,
            {
                **deployment.secrets,
                **self.postgres_runtime(name, state, deployment)[1],
                **self.valkey_runtime(name, state, deployment)[1],
            },
            cast(list[dict[str, str]], expected["secret_versions"]),
            self.aws_secrets,
        )
        try:
            with self.deployment_resource_lock(name):
                self.runner.run(
                    "gimme:reconcile:sites",
                    legacy_server(target),
                    stack=target.stack,
                    sites=target_sites(state, deployment.target),
                    network_mode=target.network.mode,
                    mise_version=target.runtimes.mise_version,
                    timeout=1800,
                )
                with protected_secret_file(resolved) as secret_file:
                    self.run_deployment(
                        "gimme:provision:app",
                        name,
                        secret_file=secret_file,
                        secret_manifest=cast(
                            list[dict[str, str]], expected["secret_versions"]
                        ),
                        timeout=1800,
                    )
                save_applied_secret_manifest(
                    self.store.root,
                    name,
                    cast(list[dict[str, str]], expected["secret_versions"]),
                )
        except Exception:
            raise SecretError("deployment_secret_activation_failed") from None
        self._apply_recovery_schedule(name, state, deployment)
        return {"changed": True, "deployment": name}

    def _apply_recovery_schedule(
        self, name: str, state: ControlState, deployment: DeploymentConfig
    ) -> None:
        authority = self.recovery_schedule_authority(
            name,
            state,
            deployment,
            cleanup=(
                deployment.recovery is not None
                and deployment.recovery.cadence.kind == "manual"
            ),
        )
        if authority is None:
            return
        recovery = deployment.recovery
        if recovery is None:
            raise RuntimeError("Recovery Schedule authority requires a Recovery Policy")
        destination = state.backup_destinations[recovery.destination]
        try:
            destination_credentials = None
            if authority["calendar"] is not None:
                _planned, destination_credentials = self.backup_destination_credentials(
                    state, destination
                )
            aws_values = (
                {}
                if destination_credentials is None
                else {
                    "access_key_id": destination_credentials[0],
                    "secret_access_key": destination_credentials[1],
                }
            )
            if destination_credentials is not None and len(destination_credentials) == 3:
                aws_values["session_token"] = destination_credentials[2]
            valkey_values: dict[str, str] = {}
            binding = deployment.resources.valkey
            if (
                authority["calendar"] is not None
                and recovery.valkey
                and binding is not None
            ):
                resource = state.resources[binding.resource]
                valkey_values = self.valkey_capture_credential(
                    state, binding.resource, resource
                )
            with ExitStack() as protected:
                aws_file = protected.enter_context(protected_secret_file(aws_values))
                valkey_file = protected.enter_context(
                    protected_secret_file(valkey_values)
                )
                self.run_deployment(
                    "gimme:recovery:schedule-reconcile",
                    name,
                    secret_file=aws_file,
                    recovery_schedule_authority=authority,
                    recovery_schedule_valkey_file=valkey_file,
                    timeout=1800,
                )
        except Exception:
            raise SecretError("recovery_schedule_activation_failed") from None
