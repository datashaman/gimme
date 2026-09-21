from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import tempfile
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, cast

from gimme import recovery as recovery_module
from gimme import recovery_schedule as recovery_schedule_module
from gimme.control import (
    AWSElastiCacheValkeyResource,
    AWSRDSPostgresResource,
    AWSSecretsManagerStore,
    ControlState,
    DeploymentConfig,
    ManualRecoveryCadence,
    ResourceConfig,
    S3BackupDestination,
    StateStore,
    TargetConfig,
)
from gimme.control_plans import (
    deployment_restore_plan,
    recovery_point_creation_plan,
    recovery_point_deletion_plan,
    restore_verification_plan,
)
from gimme.execution import execution_fingerprint
from gimme.recovery import ComponentDump, RecoveryError
from gimme.secrets import protected_secret_file

Name = str
PlanId = str
RequestId = str
RecoveryPointId = str
RestoreComponents = list[str]


@dataclass(frozen=True)
class RecoveryOrchestrator:
    """Own the deployment-scoped Recovery Point and Restore lifecycle."""

    backup_s3: Any
    elasticache_valkey: Any
    context: Callable[..., Any]
    managed_database_issues: Callable[..., Any]
    backup_destination_credentials: Callable[..., Any]
    deployment_resource_lock: Callable[..., Any]
    assert_plan: Callable[..., Any]
    run_deployment: Callable[..., Any]
    valkey_runtime: Callable[..., Any]
    postgres_capture_credential: Callable[..., Any]
    bounded_marker_values: Callable[..., Any]
    journal: Callable[..., Any]

    @contextmanager
    def _recovery_maintenance_window(
        self, name: str, request_id: str, wait_seconds: int, *, enabled: bool
    ) -> Iterator[Callable[[], None]]:
        """Keep request-owned maintenance active until verified uploads are ready to publish."""
        pending = enabled

        def restore() -> None:
            nonlocal pending
            if not pending:
                return
            try:
                self.run_deployment(
                    "gimme:recovery:maintenance",
                    name,
                    recovery_action="exit",
                    recovery_request_id=request_id,
                    recovery_quiesce_wait=wait_seconds,
                    timeout=900,
                )
            except Exception:
                raise RecoveryError("recovery_runtime_restore_failed") from None
            pending = False

        if enabled:
            try:
                self.run_deployment(
                    "gimme:recovery:maintenance",
                    name,
                    recovery_action="enter",
                    recovery_request_id=request_id,
                    recovery_quiesce_wait=wait_seconds,
                    timeout=900,
                )
            except Exception:
                restore()
                raise RecoveryError("recovery_maintenance_failed") from None
        try:
            yield restore
        finally:
            restore()

    def _capture_postgres_dump(
        self, name: str, local_path: Path, resource_version: str,
        secret_file: Path | None = None,
    ) -> ComponentDump:
        try:
            result = self.run_deployment(
                "gimme:backup:dump-postgres", name, backup_local_path=local_path,
                secret_file=secret_file, timeout=1800,
            )
        except Exception:
            raise RecoveryError("recovery_capture_failed") from None
        sha256 = ""
        size = -1
        for raw in result.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_BACKUP|"):
                parts = line.split("|", 2)
                if len(parts) == 3 and parts[2].isdigit():
                    sha256, size = (parts[1], int(parts[2]))
        if (
            re.fullmatch("[0-9a-f]{64}", sha256) is None
            or not 0 <= size <= recovery_module.MAX_COMPONENT_BYTES
            or (not local_path.is_file())
            or local_path.is_symlink()
            or (local_path.stat().st_size != size)
        ):
            raise RecoveryError("recovery_dump_metadata_invalid")
        with local_path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
                raise RecoveryError("recovery_dump_metadata_invalid")
        return ComponentDump(
            kind="postgres",
            local_path=local_path,
            sha256=sha256,
            bytes=size,
            resource_version=resource_version,
        )

    def _valkey_resource_version(
        self, resource: ResourceConfig | AWSElastiCacheValkeyResource
    ) -> str:
        if isinstance(resource, ResourceConfig):
            if resource.kind != "valkey":
                raise RecoveryError("recovery_valkey_provenance_invalid")
            return resource.version
        return resource.engine_version

    def _valkey_capture_credential(
        self,
        state: ControlState,
        resource_name: str,
        resource: ResourceConfig | AWSElastiCacheValkeyResource,
    ) -> dict[str, str]:
        if isinstance(resource, ResourceConfig):
            if resource.kind != "valkey":
                raise RecoveryError("recovery_valkey_provenance_invalid")
            return {}
        network = state.aws_networks[resource.aws_network]
        account = state.provider_accounts[network.provider_account]
        workload_store = cast(
            AWSSecretsManagerStore, state.secret_stores[resource.workload_secret_store]
        )
        self.elasticache_valkey.ensure_admin_capture_access(account, network, resource_name)
        return self.elasticache_valkey.resolve_admin_credential(
            account, workload_store, resource_name
        )

    def _capture_valkey_dump(
        self, name: str, local_path: Path, resource_version: str, secret_file: Path | None
    ) -> ComponentDump:
        try:
            result = self.run_deployment(
                "gimme:backup:capture-valkey",
                name,
                backup_local_path=local_path,
                secret_file=secret_file,
                timeout=1800,
            )
        except Exception:
            raise RecoveryError("recovery_capture_failed") from None
        sha256 = ""
        size = -1
        records = -1
        captured_at = ""
        for raw in result.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            if line.startswith("GIMME_VALKEY_BACKUP|"):
                parts = line.split("|", 4)
                if len(parts) == 5 and parts[2].isdigit() and parts[3].isdigit():
                    sha256 = parts[1]
                    size = int(parts[2])
                    records = int(parts[3])
                    captured_at = parts[4]
        try:
            capture_time = datetime.fromisoformat(captured_at)
        except ValueError:
            capture_time = None
        if (
            re.fullmatch("[0-9a-f]{64}", sha256) is None
            or not 0 <= size <= recovery_module.MAX_COMPONENT_BYTES
            or (not 0 <= records <= 100000)
            or (not local_path.is_file())
            or local_path.is_symlink()
            or (local_path.stat().st_size != size)
            or (capture_time is None)
            or (capture_time.tzinfo is None)
        ):
            raise RecoveryError("recovery_valkey_metadata_invalid")
        with local_path.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() != sha256:
                raise RecoveryError("recovery_valkey_metadata_invalid")
        return ComponentDump(
            kind="valkey",
            local_path=local_path,
            sha256=sha256,
            bytes=size,
            resource_version=resource_version,
            format="gimme-valkey-v1",
            records=records,
            captured_at=captured_at,
        )

    def _capture_restore_safety(
        self,
        name: str,
        request_id: str,
        safety_id: str,
        safety_components: list[str],
        state: ControlState,
        deployment: DeploymentConfig,
        destination_name: str,
        destination: S3BackupDestination,
        credentials: tuple[str, str] | tuple[str, str, str] | None,
    ) -> dict[str, object]:
        """Capture and verify exactly the protected destination components."""
        dumps: list[ComponentDump] = []
        expected_versions: dict[str, str] = {}
        with tempfile.TemporaryDirectory(prefix="gimme-restore-safety-") as directory:
            root = Path(directory)
            if "postgres" in safety_components:
                resource_name = deployment.resources.database
                resource = state.resources.get(resource_name) if resource_name else None
                if not isinstance(resource, ResourceConfig) or resource.kind != "postgres":
                    raise RecoveryError("restore_destination_incompatible")
                expected_versions["postgres"] = resource.version
                dumps.append(
                    self._capture_postgres_dump(name, root / "postgres.dump", resource.version)
                )
            if "valkey" in safety_components:
                binding = deployment.resources.valkey
                resource_name = binding.resource if binding is not None else None
                resource = state.resources.get(resource_name) if resource_name else None
                if (
                    not isinstance(resource, (ResourceConfig, AWSElastiCacheValkeyResource))
                    or resource_name is None
                ):
                    raise RecoveryError("restore_destination_incompatible")
                version = self._valkey_resource_version(resource)
                expected_versions["valkey"] = version
                credential = self._valkey_capture_credential(state, resource_name, resource)
                with protected_secret_file(credential) as secret_file:
                    dumps.append(
                        self._capture_valkey_dump(
                            name, root / "valkey.archive", version, secret_file
                        )
                    )
            safety = recovery_module.create_recovery_point(
                destination_name,
                destination,
                credentials,
                self.backup_s3,
                name,
                safety_id,
                dumps,
                safety_restore_request_id=request_id,
            )
        components = cast(list[dict[str, object]], safety["components"])
        observed = {
            str(component["kind"]): str(component["resource_version"]) for component in components
        }
        if (
            safety["safety"] is not True
            or safety["restore_request_id"] != request_id
            or observed != expected_versions
            or (len(components) != len(safety_components))
        ):
            raise RecoveryError("restore_safety_conflict")
        return safety

    def _restore_valkey_component(
        self,
        name: str,
        request_id: str,
        component: dict[str, object],
        local_source: Path,
        state: ControlState,
        deployment: DeploymentConfig,
    ) -> None:
        binding = deployment.resources.valkey
        resource_name = binding.resource if binding is not None else None
        resource = state.resources.get(resource_name) if resource_name else None
        if (
            not isinstance(resource, (ResourceConfig, AWSElastiCacheValkeyResource))
            or resource_name is None
        ):
            raise RecoveryError("restore_destination_incompatible")
        credential = self._valkey_capture_credential(state, resource_name, resource)
        try:
            with protected_secret_file(credential) as secret_file:
                result = self.run_deployment(
                    "gimme:recovery:valkey",
                    name,
                    backup_local_path=local_source,
                    secret_file=secret_file,
                    valkey_restore_request_id=request_id,
                    valkey_restore_sha256=str(component["sha256"]),
                    valkey_restore_bytes=int(component["bytes"]),
                    valkey_restore_records=int(component["records"]),
                    timeout=3600,
                )
        except Exception:
            raise RecoveryError("valkey_restore_failed") from None
        markers = []
        for raw in result.output.splitlines():
            line = raw.split("] ", 1)[-1].strip()
            match = re.fullmatch("GIMME_VALKEY_RESTORE\\|([0-9]{1,6})\\|([0-9]{1,6})", line)
            if match is not None:
                markers.append((int(match[1]), int(match[2])))
        if len(markers) != 1 or sum(markers[0]) != int(component["records"]):
            raise RecoveryError("valkey_verification_failed")

    def _recovery_schedule_runtime_issues(
        self, deployment: DeploymentConfig, target: TargetConfig
    ) -> list[str]:
        if (
            deployment.recovery is not None
            and deployment.recovery.cadence.kind != "manual"
            and ("python3-boto3" not in target.stack.packages)
        ):
            return ["recovery_schedule_runtime_missing"]
        return []

    def _recovery_valkey_execution(
        self, name: str, state: ControlState, deployment: DeploymentConfig
    ) -> dict[str, object] | None:
        policy = deployment.recovery
        binding = deployment.resources.valkey
        if policy is None or not policy.valkey or binding is None:
            return None
        resource = state.resources[binding.resource]
        if isinstance(resource, ResourceConfig):
            return {
                "prefix": deployment.placement.cache_prefix,
                "host": "127.0.0.1",
                "port": 6379,
                "tls": False,
                "auth_mode": "none",
            }
        values, credentials, _probe, issues = self.valkey_runtime(name, state, deployment)
        if issues or not credentials:
            return None
        return {
            "prefix": f"{{gimme:{name}}}:",
            "host": values["GIMME_VALKEY_HOST"],
            "port": int(values["GIMME_VALKEY_PORT"]),
            "tls": True,
            "auth_mode": "stored",
        }

    def _recovery_schedule_authority(
        self, name: str, state: ControlState, deployment: DeploymentConfig, *, cleanup: bool = False
    ) -> dict[str, object] | None:
        if deployment.recovery is None:
            return None
        database_name = deployment.resources.database
        if (
            not cleanup
            and deployment.recovery.cadence.kind != "manual"
            and database_name is not None
            and isinstance(state.resources.get(database_name), AWSRDSPostgresResource)
        ):
            return None
        destination_name = deployment.recovery.destination
        resource_provenance: dict[str, dict[str, str]] = {}
        for component, resource_name in (
            ("postgres", deployment.resources.database),
            (
                "valkey",
                None
                if deployment.resources.valkey is None
                else deployment.resources.valkey.resource,
            ),
        ):
            if resource_name is None or (
                component == "valkey" and (not deployment.recovery.valkey)
            ):
                continue
            resource = state.resources[resource_name]
            version = (
                resource.version
                if isinstance(resource, ResourceConfig)
                else resource.engine_version
            )
            resource_provenance[component] = {
                "name": resource_name,
                "provider": resource.provider,
                "kind": resource.kind,
                "version": version,
            }
        valkey_execution = (
            None if cleanup else self._recovery_valkey_execution(name, state, deployment)
        )
        if deployment.recovery.valkey and (not cleanup) and (valkey_execution is None):
            return None
        selected = deployment
        if cleanup:
            selected = deployment.model_copy(
                update={
                    "recovery": deployment.recovery.model_copy(
                        update={"cadence": ManualRecoveryCadence(), "valkey": False}
                    )
                }
            )
            resource_provenance.pop("valkey", None)
        return recovery_schedule_module.runner_authority(
            name,
            selected,
            destination_name,
            state.backup_destinations[destination_name],
            resource_provenance,
            valkey_execution,
        )

    def _valid_recovery_attempt_status(self, status: object, name: str) -> bool:
        fields = {
            "schema_version",
            "deployment",
            "last_logical_slot",
            "started_at",
            "finished_at",
            "outcome",
            "error_code",
            "recovery_point_id",
            "last_verified_recovery_point_id",
            "retention_outcome",
            "retention_deleted",
            "retention_remaining",
        }
        outcomes = {
            "succeeded",
            "backup_succeeded_retention_failed",
            "deployment_busy",
            "policy_stale",
            "credentials_unavailable",
            "credentials_expired",
            "destination_unavailable",
            "capture_failed",
            "verification_failed",
            "retention_failed",
            "status_unavailable",
        }
        error_codes = outcomes | {
            "maintenance_failed",
            "maintenance_route_validation_failed",
            "maintenance_route_reload_failed",
            "maintenance_quiesce_wait_failed",
            "maintenance_process_control_failed",
            "postgres_capture_failed",
            "valkey_capture_failed",
            "publish_failed",
            "provider_runtime_access_denied",
            "provider_runtime_unavailable",
            "provider_configuration_invalid",
            "provider_client_unavailable",
        }
        if (
            not isinstance(status, dict)
            or set(status) != fields
            or status.get("schema_version") != 1
            or (status.get("deployment") != name)
            or (status.get("outcome") not in outcomes | {None})
            or (status.get("error_code") not in error_codes | {None})
            or (
                status.get("retention_outcome")
                not in {None, "succeeded", "backup_succeeded_retention_failed"}
            )
        ):
            return False
        for key in ("last_logical_slot", "started_at", "finished_at"):
            value = status.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or len(value) > 64:
                return False
            try:
                if datetime.fromisoformat(value).tzinfo is None:
                    return False
            except ValueError:
                return False
        for key in ("recovery_point_id", "last_verified_recovery_point_id"):
            value = status.get(key)
            if value is not None and (
                not isinstance(value, str) or re.fullmatch("rp_[a-f0-9]{20}", value) is None
            ):
                return False
        return all(
            (
                isinstance(status.get(key), int)
                and (not isinstance(status.get(key), bool))
                and (0 <= status[key] <= 10000)
                for key in ("retention_deleted", "retention_remaining")
            )
        )

    def _recovery_schedule_status(
        self, name: str, observed_at: datetime | None = None
    ) -> dict[str, object]:
        _state, deployment, _target, _application = self.context(name)
        if deployment.recovery is None:
            raise ValueError(f"deployment {name} has no Recovery Policy bound")
        cadence = deployment.recovery.cadence
        observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
        logical_next = recovery_schedule_module.next_logical_slot(cadence, observed)
        effective_next = (
            None
            if logical_next is None
            else recovery_schedule_module.effective_execution(logical_next, name)
        )
        result: dict[str, object] = {
            "deployment": name,
            "cadence": cadence.model_dump(mode="json"),
            "logical_next_utc": None if logical_next is None else logical_next.isoformat(),
            "effective_next_utc": None if effective_next is None else effective_next.isoformat(),
            "timer_state": "disabled" if logical_next is None else "unavailable",
            "timer_enabled": False if logical_next is None else None,
            "timer_active": False if logical_next is None else None,
            "last_logical_slot": None,
            "started_at": None,
            "finished_at": None,
            "outcome": None if logical_next is None else "status_unavailable",
            "error_code": None if logical_next is None else "status_unavailable",
            "recovery_point_id": None,
            "last_verified_recovery_point_id": None,
            "retention_outcome": None,
            "retention_deleted": 0,
            "retention_remaining": 0,
        }
        if logical_next is None:
            return result
        try:
            observation = self.run_deployment("gimme:recovery:schedule-status", name, timeout=60)
            markers = []
            status_markers = []
            for raw in observation.output.splitlines():
                line = raw.split("] ", 1)[-1].strip()
                if line.startswith("GIMME_RECOVERY_TIMER|"):
                    markers.append(line.split("|"))
                elif line.startswith("GIMME_RECOVERY_STATUS|"):
                    status_markers.append(line.split("|", 1)[1])
            if (
                len(markers) != 1
                or len(markers[0]) != 3
                or markers[0][1] not in {"missing", "enabled", "disabled"}
                or (markers[0][2] not in {"active", "inactive"})
            ):
                return result
            configured, activity = markers[0][1:]
            result.update(
                {
                    "timer_state": "missing" if configured == "missing" else activity,
                    "timer_enabled": configured == "enabled",
                    "timer_active": activity == "active",
                    "outcome": None,
                    "error_code": None,
                }
            )
            if len(status_markers) == 1:
                encoded = status_markers[0]
                if len(encoded) <= 24576:
                    raw_status = base64.b64decode(encoded, validate=True)
                    if len(raw_status) <= 16 * 1024:
                        status = json.loads(raw_status)
                        if self._valid_recovery_attempt_status(status, name):
                            result.update(
                                {
                                    key: status[key]
                                    for key in status
                                    if key not in {"schema_version", "deployment"}
                                }
                            )
        except Exception:
            return result
        return result

    def _recovery_context(
        self, name: str
    ) -> tuple[ControlState, DeploymentConfig, str, S3BackupDestination]:
        state, deployment, _target, _application = self.context(name)
        if deployment.recovery is None:
            raise ValueError(f"deployment {name} has no Recovery Policy bound")
        destination_name = deployment.recovery.destination
        destination = state.backup_destinations[destination_name]
        return (state, deployment, destination_name, destination)

    def plan_create_recovery_point(self, name: Name, request_id: RequestId) -> dict[str, object]:
        """Plan one on-demand PostgreSQL Recovery Point for a recovery-bound deployment."""
        _state, deployment, destination_name, destination = self._recovery_context(name)
        point_id = recovery_module.recovery_point_id(name, destination_name, request_id)
        return recovery_point_creation_plan(
            name, deployment, destination_name, destination, request_id, point_id
        )

    def create_recovery_point(
        self, name: Name, request_id: RequestId, plan_id: PlanId
    ) -> dict[str, object]:
        """Apply a reviewed on-demand Recovery Point: dump, upload, verify, and publish."""
        state, deployment, destination_name, destination = self._recovery_context(name)
        point_id = recovery_module.recovery_point_id(name, destination_name, request_id)
        expected = recovery_point_creation_plan(
            name, deployment, destination_name, destination, request_id, point_id
        )
        self.assert_plan(expected, plan_id)
        _, credentials = self.backup_destination_credentials(state, destination)
        database_name = deployment.resources.database
        database_resource = (
            state.resources.get(database_name) if database_name is not None else None
        )
        if isinstance(database_resource, AWSRDSPostgresResource):
            if deployment.recovery.valkey:
                raise RecoveryError("managed_postgres_recovery_valkey_unsupported")
            with self.deployment_resource_lock(name):
                manifest = recovery_module.find_recovery_point(
                    destination_name, destination, credentials, self.backup_s3,
                    name, point_id,
                )
                changed = manifest is None
                if manifest is None:
                    with tempfile.TemporaryDirectory(
                        prefix="gimme-managed-postgres-recovery-"
                    ) as directory:
                        path = Path(directory) / "postgres.dump"
                        credential = self.postgres_capture_credential(
                            state, name, database_resource
                        )
                        with protected_secret_file(credential) as postgres_file:
                            dump = self._capture_postgres_dump(
                                name, path, database_resource.engine_version,
                                postgres_file,
                            )
                        manifest = recovery_module.create_recovery_point(
                            destination_name, destination, credentials, self.backup_s3,
                            name, point_id, [dump],
                        )
                verified = recovery_module.find_recovery_point(
                    destination_name, destination, credentials, self.backup_s3,
                    name, point_id,
                )
                if verified is None:
                    raise RecoveryError("recovery_verification_failed")
                retention = recovery_module.enforce_recovery_retention(
                    destination_name, destination, credentials, self.backup_s3,
                    name, deployment.recovery.retain_last, point_id,
                )
                return {
                    "changed": changed,
                    "recovery_point": recovery_module.public_recovery_point(verified),
                    "retention": retention,
                }
        with self.deployment_resource_lock(name):
            authority = self._recovery_schedule_authority(name, state, deployment)
            if authority is None:
                raise RecoveryError("recovery_authority_unavailable")
            aws_values = (
                {}
                if credentials is None
                else {"access_key_id": credentials[0], "secret_access_key": credentials[1]}
            )
            if credentials is not None and len(credentials) == 3:
                aws_values["session_token"] = credentials[2]
            valkey_values: dict[str, str] = {}
            binding = deployment.resources.valkey
            if deployment.recovery.valkey and binding is not None:
                valkey_values = self._valkey_capture_credential(
                    state, binding.resource, state.resources[binding.resource]
                )
            try:
                with ExitStack() as protected:
                    aws_file = protected.enter_context(protected_secret_file(aws_values))
                    valkey_file = protected.enter_context(protected_secret_file(valkey_values))
                    executed = self.run_deployment(
                        "gimme:recovery:on-demand",
                        name,
                        secret_file=aws_file,
                        recovery_schedule_authority=authority,
                        recovery_schedule_valkey_file=valkey_file,
                        recovery_on_demand_request_id=request_id,
                        timeout=7200,
                    )
            except Exception:
                raise RecoveryError("recovery_capture_failed") from None
            markers = [
                line.split("|", 1)[1]
                for raw in executed.output.splitlines()
                if (line := raw.split("] ", 1)[-1].strip()).startswith("GIMME_RECOVERY_RESULT|")
            ]
            try:
                if len(markers) != 1 or len(markers[0]) > 24576:
                    raise ValueError
                raw_result = base64.b64decode(markers[0], validate=True)
                if len(raw_result) > 16 * 1024:
                    raise ValueError
                result = json.loads(raw_result)
                if not isinstance(result, dict):
                    raise ValueError
                retention = result["retention"]
                if (
                    set(result) != {"changed", "recovery_point_id", "retention"}
                    or not isinstance(result["changed"], bool)
                    or result["recovery_point_id"] != point_id
                    or (not isinstance(retention, dict))
                    or (set(retention) != {"outcome", "error_code", "deleted", "remaining"})
                    or (
                        retention["outcome"]
                        not in {"succeeded", "backup_succeeded_retention_failed"}
                    )
                    or (retention["error_code"] not in {None, "retention_failed"})
                    or any(
                        (
                            isinstance(retention[key], bool)
                            or not isinstance(retention[key], int)
                            or (not 0 <= retention[key] <= 10000)
                            for key in ("deleted", "remaining")
                        )
                    )
                ):
                    raise ValueError
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                raise RecoveryError("recovery_result_invalid") from None
            manifest = recovery_module.find_recovery_point(
                destination_name, destination, credentials, self.backup_s3, name, point_id
            )
            if manifest is None:
                raise RecoveryError("recovery_verification_failed")
            return {
                "changed": result["changed"],
                "recovery_point": recovery_module.public_recovery_point(manifest),
                "retention": retention,
            }

    def list_recovery_points(self, name: Name) -> dict[str, object]:
        """List one deployment's Recovery Points from destination-authoritative inventory."""
        state, _deployment, destination_name, destination = self._recovery_context(name)
        _, credentials = self.backup_destination_credentials(state, destination)
        inventory = recovery_module.list_recovery_points(
            destination_name, destination, credentials, self.backup_s3, name
        )
        return {
            **inventory,
            "recovery_points": [
                recovery_module.public_recovery_point(item) for item in inventory["recovery_points"]
            ],
        }

    def _restore_records(self, name: str) -> list[dict[str, object]]:
        state, _deployment, _destination_name, destination = self._recovery_context(name)
        _, credentials = self.backup_destination_credentials(state, destination)
        return recovery_module.list_restore_records(destination, credentials, self.backup_s3, name)

    def list_restores(self, name: Name) -> dict[str, object]:
        """List destination-authoritative, secret-safe Restore records newest first."""
        return {"deployment": name, "restores": self._restore_records(name)}

    def restore_record_resource(self, name: str, request_id: str) -> dict[str, object]:
        """Read the latest public Restore state for one request identity."""
        state, _deployment, _destination_name, destination = self._recovery_context(name)
        _, credentials = self.backup_destination_credentials(state, destination)
        return recovery_module.load_restore_record(
            destination, credentials, self.backup_s3, name, request_id
        )

    def _normalize_restore_components(
        self, manifest_components: list[dict[str, object]], requested: list[str] | None
    ) -> list[str]:
        available = [str(component.get("kind")) for component in manifest_components]
        if (
            not available
            or len(available) != len(set(available))
            or (not set(available) <= {"postgres", "valkey"})
        ):
            raise RecoveryError("restore_component_manifest_invalid")
        if requested is None:
            return available
        if (
            not 1 <= len(requested) <= 2
            or len(requested) != len(set(requested))
            or (not set(requested) <= {"postgres", "valkey"})
        ):
            raise RecoveryError("restore_component_selection_invalid")
        if not set(requested) <= set(available):
            raise RecoveryError("restore_component_missing")
        return [component for component in available if component in requested]

    def _deployment_restore_plan(
        self,
        name: str,
        recovery_point_id: str,
        request_id: str,
        components: list[str] | None = None,
    ) -> dict[str, object]:
        state, deployment, destination_name, destination = self._recovery_context(name)
        _, credentials = self.backup_destination_credentials(state, destination)
        manifest = recovery_module.find_recovery_point(
            destination_name, destination, credentials, self.backup_s3, name, recovery_point_id
        )
        if manifest is None:
            raise RecoveryError("restore_source_missing")
        manifest_components = cast(list[dict[str, object]], manifest["components"])
        selected_components = self._normalize_restore_components(manifest_components, components)
        available_components = [str(item["kind"]) for item in manifest_components]
        untouched_components = [
            component for component in available_components if component not in selected_components
        ]
        valkey_destination: dict[str, str] | None = None
        if "valkey" in selected_components:
            binding = deployment.resources.valkey
            valkey_resource_name = binding.resource if binding is not None else None
            valkey_resource = (
                state.resources.get(valkey_resource_name)
                if valkey_resource_name is not None
                else None
            )
            if isinstance(valkey_resource, ResourceConfig) and valkey_resource.kind == "valkey":
                valkey_destination = {
                    "resource": str(valkey_resource_name),
                    "provider": "target_local",
                    "kind": "valkey",
                    "version": valkey_resource.version,
                }
            elif isinstance(valkey_resource, AWSElastiCacheValkeyResource):
                valkey_destination = {
                    "resource": str(valkey_resource_name),
                    "provider": "aws_elasticache_valkey",
                    "kind": "valkey",
                    "version": valkey_resource.engine_version,
                }
        resource_name = deployment.resources.database
        resource = state.resources[resource_name] if resource_name is not None else None
        if not isinstance(resource, ResourceConfig) or resource.kind != "postgres":
            raise RecoveryError("restore_destination_incompatible")
        states: set[str] = set()
        capacity_ready = True
        if "postgres" in selected_components:
            postgres_source = next(
                (item for item in manifest_components if item["kind"] == "postgres")
            )
            source_bytes = int(postgres_source["bytes"])
            observation = self.run_deployment(
                "gimme:recovery:inspect-postgres",
                name,
                restore_source_bytes=source_bytes,
                timeout=60,
            )
            states = self.bounded_marker_values(
                observation.output, "GIMME_POSTGRES_RESTORE_PREFLIGHT|", {"empty", "nonempty"}
            )
            if len(states) != 1 or not states <= {"empty", "nonempty"}:
                raise RecoveryError("restore_destination_inspection_failed")
            capacity = self.bounded_marker_values(
                observation.output, "GIMME_POSTGRES_RESTORE_CAPACITY|", {"ready", "insufficient"}
            )
            if len(capacity) != 1:
                raise RecoveryError("restore_destination_inspection_failed")
            required_bytes = source_bytes * 2 + 64 * 1024 * 1024
            capacity_ready = (
                capacity == {"ready"}
                and shutil.disk_usage(tempfile.gettempdir()).free >= required_bytes
            )
        try:
            existing_restore = recovery_module.load_restore_record(
                destination, credentials, self.backup_s3, name, request_id
            )
        except RecoveryError as exc:
            if str(exc) != "restore_record_missing":
                raise
            existing_restore = None
        expected_destination = {
            "resource": resource_name,
            "provider": "target_local",
            "kind": "postgres",
            "version": resource.version,
        }
        if selected_components == ["valkey"] and valkey_destination is not None:
            expected_destination = valkey_destination
        request_conflict = existing_restore is not None and (
            existing_restore["source_recovery_point_id"] != recovery_point_id
            or existing_restore["destination"] != expected_destination
            or existing_restore["selected_components"] != selected_components
            or (existing_restore["untouched_components"] != untouched_components)
            or (existing_restore["partial"] != bool(untouched_components))
            or (
                existing_restore["safety_recovery_point_id"]
                not in {
                    None,
                    recovery_module.safety_recovery_point_id(name, destination_name, request_id),
                }
            )
        )
        observed_empty = "postgres" in selected_components and states == {"empty"}
        observed_safety_components = [
            component
            for component in selected_components
            if component == "valkey" or (component == "postgres" and (not observed_empty))
        ]
        safety_components = (
            observed_safety_components
            if existing_restore is None
            else cast(list[str], existing_restore["safety_components"])
        )
        original_empty = "postgres" in selected_components and "postgres" not in safety_components
        destination_changed = (
            existing_restore is not None
            and existing_restore["state"]
            in {
                "started",
                "maintenance_entered",
                "safety_failed",
                "artifact_failed",
                "shadow_failed",
            }
            and (original_empty != observed_empty)
        )
        selected_destinations = [
            *(
                [
                    {
                        "resource": resource_name,
                        "provider": "target_local",
                        "kind": "postgres",
                        "version": resource.version,
                        "empty": original_empty,
                    }
                ]
                if "postgres" in selected_components
                else []
            ),
            *(
                [valkey_destination]
                if "valkey" in selected_components and valkey_destination is not None
                else []
            ),
        ]
        request_fingerprint = StateStore.digest(
            {
                "kind": "deployment_restore_request",
                "deployment": name,
                "source_manifest": manifest,
                "selected_components": selected_components,
                "untouched_components": untouched_components,
                "destinations": selected_destinations,
                "recovery_policy": deployment.recovery.model_dump(mode="json"),
                "placement": deployment.placement.model_dump(mode="json"),
                "execution_fingerprint": execution_fingerprint(),
            }
        )
        record_destinations = [
            {key: value for key, value in item.items() if key != "empty"}
            for item in selected_destinations
        ]
        if existing_restore is not None and (
            existing_restore.get("request_fingerprint") not in {None, request_fingerprint}
            or (
                existing_restore.get("request_fingerprint") is not None
                and existing_restore.get("destinations") != record_destinations
            )
        ):
            request_conflict = True
        return deployment_restore_plan(
            name,
            recovery_point_id,
            request_id,
            manifest_components,
            resource_name,
            resource.version,
            original_empty,
            selected_components,
            safety_components=safety_components,
            capacity_ready=capacity_ready,
            valkey_destination=valkey_destination,
            request_fingerprint=request_fingerprint,
            restore_state=None if existing_restore is None else str(existing_restore["state"]),
            request_conflict=request_conflict,
            destination_changed=destination_changed,
        )

    def plan_restore_deployment(
        self,
        name: Name,
        recovery_point_id: RecoveryPointId,
        request_id: RequestId,
        components: RestoreComponents | None = None,
    ) -> dict[str, object]:
        """Plan full Restore by default or an explicit bounded component subset."""
        return self._deployment_restore_plan(name, recovery_point_id, request_id, components)

    def apply_restore_deployment(
        self,
        name: Name,
        recovery_point_id: RecoveryPointId,
        request_id: RequestId,
        plan_id: PlanId,
        confirmation: str,
        components: RestoreComponents | None = None,
    ) -> dict[str, object]:
        """Prepare and activate one reviewed full or partial Deployment Restore.

        The deployment remains in request-owned maintenance for a separate verified
        completion step.
        """
        with self.deployment_resource_lock(name):
            expected = self._deployment_restore_plan(
                name, recovery_point_id, request_id, components
            )
            self.assert_plan(expected, plan_id)
            if not expected["ready"]:
                raise RecoveryError("restore_not_ready")
            if confirmation != expected["confirmation"]:
                raise ValueError("restore confirmation is invalid")
            state, deployment, destination_name, destination = self._recovery_context(name)
            _, credentials = self.backup_destination_credentials(state, destination)
            manifest = recovery_module.find_recovery_point(
                destination_name, destination, credentials, self.backup_s3, name, recovery_point_id
            )
            if manifest is None:
                raise RecoveryError("restore_source_missing")
            selected_components = cast(list[str], expected["selected_components"])
            safety_components = cast(list[str], expected["safety_components"])
            untouched_components = cast(list[str], expected["untouched_components"])
            source_components = {
                str(item["kind"]): item
                for item in manifest["components"]
                if item["kind"] in selected_components
            }
            if set(source_components) != set(selected_components):
                raise RecoveryError("restore_component_missing")
            resource_name = deployment.resources.database
            resource = state.resources[resource_name] if resource_name is not None else None
            if not isinstance(resource, ResourceConfig) or resource.kind != "postgres":
                raise RecoveryError("restore_destination_incompatible")
            existing = (
                recovery_module.load_restore_record(
                    destination, credentials, self.backup_s3, name, request_id
                )
                if expected["restore_state"] is not None
                else None
            )
            safety_id = (
                recovery_module.safety_recovery_point_id(name, destination_name, request_id)
                if safety_components
                else None
            )
            if existing is not None:
                safety_id = cast(str | None, existing["safety_recovery_point_id"])
            destinations = cast(list[dict[str, object]], expected["destinations"])
            if len(destinations) != len(selected_components):
                raise RecoveryError("restore_destination_incompatible")
            primary_destination = destinations[0]
            record_destinations = [
                {key: value for key, value in item.items() if key != "empty"}
                for item in destinations
            ]
            identity = {
                "source_recovery_point_id": recovery_point_id,
                "destination_provider": str(primary_destination["provider"]),
                "destination_resource": str(primary_destination["resource"]),
                "destination_kind": str(primary_destination["kind"]),
                "destination_version": str(primary_destination["version"]),
                "safety_recovery_point_id": safety_id,
                "selected_components": selected_components,
                "untouched_components": untouched_components,
                "partial": bool(expected["partial"]),
                "destinations": record_destinations
                if existing is None
                else cast(list[dict[str, object]], existing["destinations"]),
                "request_fingerprint": str(expected["request_fingerprint"])
                if existing is None
                else cast(str | None, existing["request_fingerprint"]),
                "safety_components": safety_components,
            }
            current = None if existing is None else str(existing["state"])
            changed = False

            def advance(next_state: str) -> None:
                nonlocal changed, current
                recovery_module.append_restore_event(
                    destination,
                    credentials,
                    self.backup_s3,
                    name,
                    request_id,
                    next_state,
                    **identity,
                )
                current = next_state
                changed = True

            if current is None:
                advance("started")
            resume_failure = current if current in {"artifact_failed", "shadow_failed"} else None
            if current in {"started", "safety_failed", "artifact_failed", "shadow_failed"}:
                try:
                    self.run_deployment(
                        "gimme:recovery:maintenance",
                        name,
                        recovery_action="enter",
                        recovery_request_id=request_id,
                        recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                        timeout=900,
                    )
                except Exception:
                    raise RecoveryError("restore_maintenance_failed") from None
                if resume_failure == "artifact_failed":
                    advance("safety_not_required" if safety_id is None else "safety_verified")
                elif resume_failure == "shadow_failed":
                    advance("artifact_verified")
                else:
                    advance("maintenance_entered")
            if current == "maintenance_entered":
                if safety_id is None:
                    advance("safety_not_required")
                else:
                    try:
                        self._capture_restore_safety(
                            name,
                            request_id,
                            safety_id,
                            safety_components,
                            state,
                            deployment,
                            destination_name,
                            destination,
                            credentials,
                        )
                    except Exception:
                        runtime_restored = True
                        try:
                            self.run_deployment(
                                "gimme:recovery:maintenance",
                                name,
                                recovery_action="exit",
                                recovery_request_id=request_id,
                                recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                                timeout=900,
                            )
                        except Exception:
                            runtime_restored = False
                        advance("safety_failed")
                        raise RecoveryError(
                            "safety_failed"
                            if runtime_restored
                            else "recovery_runtime_restore_failed"
                        ) from None
                    advance("safety_verified")
            resume_valkey_from_shadow = (
                current == "shadow_verified" and "valkey" in selected_components
            )
            with tempfile.TemporaryDirectory(prefix="gimme-restore-source-") as directory:
                local_sources = {
                    "postgres": Path(directory) / "postgres.dump",
                    "valkey": Path(directory) / "valkey.archive",
                }
                try:
                    if current in {"safety_verified", "safety_not_required", "artifact_verified"}:
                        for selected_kind in selected_components:
                            source_components[selected_kind] = (
                                recovery_module.materialize_recovery_component(
                                    destination_name,
                                    destination,
                                    credentials,
                                    self.backup_s3,
                                    name,
                                    recovery_point_id,
                                    selected_kind,
                                    local_sources[selected_kind],
                                )
                            )
                    elif resume_valkey_from_shadow:
                        source_components["valkey"] = (
                            recovery_module.materialize_recovery_component(
                                destination_name,
                                destination,
                                credentials,
                                self.backup_s3,
                                name,
                                recovery_point_id,
                                "valkey",
                                local_sources["valkey"],
                            )
                        )
                except Exception as exc:
                    if resume_valkey_from_shadow:
                        if isinstance(exc, RecoveryError):
                            raise RecoveryError(str(exc)) from None
                        raise RecoveryError("restore_artifact_failed") from None
                    advance("artifact_failed")
                    try:
                        self.run_deployment(
                            "gimme:recovery:maintenance",
                            name,
                            recovery_action="exit",
                            recovery_request_id=request_id,
                            recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                            timeout=900,
                        )
                    except Exception:
                        raise RecoveryError("recovery_runtime_restore_failed") from None
                    if isinstance(exc, RecoveryError):
                        raise RecoveryError(str(exc)) from None
                    raise RecoveryError("restore_artifact_failed") from None
                if current in {"safety_verified", "safety_not_required"}:
                    advance("artifact_verified")
                if current == "artifact_verified":
                    if "postgres" in selected_components:
                        postgres_component = source_components["postgres"]
                        try:
                            self.run_deployment(
                                "gimme:recovery:postgres",
                                name,
                                backup_local_path=local_sources["postgres"],
                                postgres_restore_action="prepare",
                                postgres_restore_request_id=request_id,
                                postgres_restore_sha256=str(postgres_component["sha256"]),
                                postgres_restore_bytes=int(postgres_component["bytes"]),
                                timeout=3600,
                            )
                        except Exception:
                            advance("shadow_failed")
                            try:
                                self.run_deployment(
                                    "gimme:recovery:maintenance",
                                    name,
                                    recovery_action="exit",
                                    recovery_request_id=request_id,
                                    recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                                    timeout=900,
                                )
                            except Exception:
                                raise RecoveryError("recovery_runtime_restore_failed") from None
                            raise RecoveryError("restore_shadow_prepare_failed") from None
                    if "valkey" in selected_components:
                        self._restore_valkey_component(
                            name,
                            request_id,
                            source_components["valkey"],
                            local_sources["valkey"],
                            state,
                            deployment,
                        )
                    advance("shadow_verified")
                elif resume_valkey_from_shadow:
                    self._restore_valkey_component(
                        name,
                        request_id,
                        source_components["valkey"],
                        local_sources["valkey"],
                        state,
                        deployment,
                    )
            if current == "shadow_verified":
                if "postgres" in selected_components:
                    postgres_component = source_components["postgres"]
                    try:
                        self.run_deployment(
                            "gimme:recovery:postgres",
                            name,
                            postgres_restore_action="swap",
                            postgres_restore_request_id=request_id,
                            postgres_restore_sha256=str(postgres_component["sha256"]),
                            postgres_restore_bytes=int(postgres_component["bytes"]),
                            timeout=300,
                        )
                    except Exception:
                        raise RecoveryError("restore_swap_failed") from None
                advance("data_replaced")
            return {
                "changed": changed,
                "deployment": name,
                "request_id": request_id,
                "state": current,
                "recovery_required": current != "completed",
            }

    def _restore_verification_plan(self, name: str, request_id: str) -> dict[str, object]:
        state, deployment, _destination_name, destination = self._recovery_context(name)
        _, credentials = self.backup_destination_credentials(state, destination)
        restore = recovery_module.load_restore_record(
            destination, credentials, self.backup_s3, name, request_id
        )
        observed: list[dict[str, object]] = []
        for kind in cast(list[str], restore["selected_components"]):
            resource_name = (
                deployment.resources.database
                if kind == "postgres"
                else deployment.resources.valkey.resource
                if deployment.resources.valkey is not None
                else None
            )
            resource = state.resources.get(resource_name) if resource_name else None
            if (
                kind == "postgres"
                and isinstance(resource, ResourceConfig)
                and (resource.kind == "postgres")
            ):
                observed.append(
                    {
                        "resource": resource_name,
                        "provider": "target_local",
                        "kind": "postgres",
                        "version": resource.version,
                    }
                )
            elif (
                kind == "valkey"
                and isinstance(resource, ResourceConfig)
                and (resource.kind == "valkey")
            ):
                observed.append(
                    {
                        "resource": resource_name,
                        "provider": "target_local",
                        "kind": "valkey",
                        "version": resource.version,
                    }
                )
            elif kind == "valkey" and isinstance(resource, AWSElastiCacheValkeyResource):
                observed.append(
                    {
                        "resource": resource_name,
                        "provider": "aws_elasticache_valkey",
                        "kind": "valkey",
                        "version": resource.engine_version,
                    }
                )
        recorded = cast(list[dict[str, object]], restore["destinations"])
        if restore["request_fingerprint"] is None:
            observed = observed[: len(recorded)]
        return restore_verification_plan(
            name, request_id, restore, identity_conflict=observed != recorded
        )

    def plan_verify_restore(self, name: Name, request_id: RequestId) -> dict[str, object]:
        """Plan private application verification and return from Restore maintenance."""
        return self._restore_verification_plan(name, request_id)

    def apply_verify_restore(
        self, name: Name, request_id: RequestId, plan_id: PlanId
    ) -> dict[str, object]:
        """Verify restored data privately, clean up, and restore normal routing."""
        with self.deployment_resource_lock(name):
            expected = self._restore_verification_plan(name, request_id)
            self.assert_plan(expected, plan_id)
            if not expected["ready"]:
                raise RecoveryError("restore_verification_not_ready")
            state, deployment, destination_name, destination = self._recovery_context(name)
            _, credentials = self.backup_destination_credentials(state, destination)
            restore = recovery_module.load_restore_record(
                destination, credentials, self.backup_s3, name, request_id
            )
            restore_destination = cast(dict[str, object], restore["destination"])
            identity = {
                "source_recovery_point_id": str(restore["source_recovery_point_id"]),
                "destination_provider": str(restore_destination["provider"]),
                "destination_resource": str(restore_destination["resource"]),
                "destination_kind": str(restore_destination["kind"]),
                "destination_version": str(restore_destination["version"]),
                "safety_recovery_point_id": cast(str | None, restore["safety_recovery_point_id"]),
                "selected_components": cast(list[str], restore["selected_components"]),
                "untouched_components": cast(list[str], restore["untouched_components"]),
                "partial": bool(restore["partial"]),
                "destinations": cast(list[dict[str, object]], restore["destinations"]),
                "request_fingerprint": cast(str | None, restore["request_fingerprint"]),
                "safety_components": cast(list[str], restore["safety_components"]),
            }
            current = str(restore["state"])
            changed = False

            def advance(next_state: str) -> None:
                nonlocal changed, current
                recovery_module.append_restore_event(
                    destination,
                    credentials,
                    self.backup_s3,
                    name,
                    request_id,
                    next_state,
                    **identity,
                )
                current = next_state
                changed = True

            def verify_runtime() -> None:
                try:
                    self.run_deployment(
                        "gimme:recovery:maintenance",
                        name,
                        recovery_action="resume",
                        recovery_request_id=request_id,
                        recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                        timeout=900,
                    )
                    result = self.run_deployment(
                        "gimme:recovery:verify-application", name, timeout=900
                    )
                    markers = self.bounded_marker_values(
                        result.output, "GIMME_RESTORE_VERIFY|", {"ready"}
                    )
                    if markers != {"ready"}:
                        raise RecoveryError("restore_verification_failed")
                except Exception:
                    quiesce_failed = False
                    try:
                        self.run_deployment(
                            "gimme:recovery:maintenance",
                            name,
                            recovery_action="quiesce",
                            recovery_request_id=request_id,
                            recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                            timeout=900,
                        )
                    except Exception:
                        quiesce_failed = True
                    advance("verification_failed")
                    raise RecoveryError(
                        "restore_quiesce_failed"
                        if quiesce_failed
                        else "restore_verification_failed"
                    ) from None

            if current in {"data_replaced", "verification_failed"}:
                verify_runtime()
                advance("verification_succeeded")
            elif current == "verification_succeeded":
                verify_runtime()
            if current == "verification_succeeded":
                if "postgres" in cast(list[str], restore["selected_components"]):
                    manifest = recovery_module.find_recovery_point(
                        destination_name,
                        destination,
                        credentials,
                        self.backup_s3,
                        name,
                        str(restore["source_recovery_point_id"]),
                    )
                    if manifest is None:
                        raise RecoveryError("restore_source_missing")
                    component = next(
                        (item for item in manifest["components"] if item["kind"] == "postgres"),
                        None,
                    )
                    if component is None:
                        raise RecoveryError("restore_component_missing")
                    try:
                        self.run_deployment(
                            "gimme:recovery:postgres",
                            name,
                            postgres_restore_action="cleanup",
                            postgres_restore_request_id=request_id,
                            postgres_restore_sha256=str(component["sha256"]),
                            postgres_restore_bytes=int(component["bytes"]),
                            timeout=300,
                        )
                    except Exception:
                        raise RecoveryError("restore_cleanup_failed") from None
                advance("cleanup_completed")
            if current == "cleanup_completed":
                verify_runtime()
                try:
                    self.run_deployment(
                        "gimme:recovery:maintenance",
                        name,
                        recovery_action="exit",
                        recovery_request_id=request_id,
                        recovery_quiesce_wait=deployment.recovery.quiesce_wait_seconds,
                        timeout=900,
                    )
                except Exception:
                    raise RecoveryError("restore_maintenance_exit_failed") from None
                advance("completed")
            return {
                "changed": changed,
                "deployment": name,
                "request_id": request_id,
                "state": current,
                "recovery_required": current != "completed",
            }

    def _recovery_point_deletion_plan(
        self, name: str, point_id: str, *, allow_partial: bool = False
    ) -> dict[str, object]:
        state, _deployment, destination_name, destination = self._recovery_context(name)
        _, credentials = self.backup_destination_credentials(state, destination)
        inventory = recovery_module.list_recovery_points(
            destination_name, destination, credentials, self.backup_s3, name
        )
        selected = next(
            (
                item
                for item in inventory["recovery_points"]
                if item["recovery_point_id"] == point_id
            ),
            None,
        )
        if selected is None:
            raise RecoveryError("recovery_manifest_missing")
        if selected["state"] == "deletion_failed" and (not allow_partial):
            raise RecoveryError("recovery_point_deletion_failed")
        targets = recovery_module.recovery_point_deletion_targets(
            destination_name, destination, credentials, self.backup_s3, name, point_id
        )
        effective_verified = sum(
            (
                item["state"] == "verified"
                and (
                    not recovery_module.safety_recovery_point_protected(
                        destination, credentials, self.backup_s3, name, item
                    )
                )
                for item in inventory["recovery_points"]
            )
        ) + (1 if selected["state"] == "deletion_failed" else 0)
        safety_protected = recovery_module.safety_recovery_point_protected(
            destination, credentials, self.backup_s3, name, targets["manifest"]
        )
        restore_protected = recovery_module.recovery_point_source_protected(
            destination, credentials, self.backup_s3, name, point_id
        )
        normalized_inventory = sorted(
            (
                (
                    str(item["recovery_point_id"]),
                    "verified"
                    if item is selected and item["state"] == "deletion_failed"
                    else str(item["state"]),
                )
                for item in inventory["recovery_points"]
            )
        )
        inventory_fingerprint = hashlib.sha256(
            json.dumps(normalized_inventory, separators=(",", ":")).encode()
        ).hexdigest()
        manifest_fingerprint = hashlib.sha256(
            json.dumps(
                {"manifest": targets["manifest"], "version": targets["manifest_version_id"]},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return recovery_point_deletion_plan(
            name,
            destination_name,
            point_id,
            components=int(targets["components"]),
            bytes=int(targets["bytes"]),
            final_verified_point=effective_verified == 1,
            safety_protected=safety_protected,
            restore_protected=restore_protected,
            inventory_fingerprint=inventory_fingerprint,
            manifest_fingerprint=manifest_fingerprint,
            state="verified",
        )

    def plan_delete_recovery_point(
        self, name: Name, recovery_point_id: RecoveryPointId
    ) -> dict[str, object]:
        """Plan deletion of one manifest-owned Recovery Point without exposing S3 identities."""
        return self._recovery_point_deletion_plan(name, recovery_point_id)

    def _matching_delete_retry(self, name: str, plan_id: str) -> bool:
        events = self.journal().list(limit=200, operation="delete_recovery_point", subject=name)
        outcomes = [
            event
            for event in events
            if event.phase == "outcome"
            and event.plan_id == plan_id
            and (event.status in {"failed", "succeeded"})
        ]
        return (
            bool(outcomes)
            and self.journal().plan_correlation(plan_id, "delete_recovery_point") is not None
        )

    def delete_recovery_point(
        self,
        name: Name,
        recovery_point_id: RecoveryPointId,
        plan_id: PlanId,
        confirmation: str,
        last_recovery_point_confirmation: str | None = None,
    ) -> dict[str, object]:
        """Delete only exact manifest-owned versions, with the immutable manifest last."""
        retry = self._matching_delete_retry(name, plan_id)
        with self.deployment_resource_lock(name):
            try:
                expected = self._recovery_point_deletion_plan(
                    name, recovery_point_id, allow_partial=retry
                )
            except RecoveryError as exc:
                if retry and str(exc) == "recovery_manifest_missing":
                    return {
                        "changed": False,
                        "recovery_point_id": recovery_point_id,
                        "state": "deleted",
                    }
                raise
            self.assert_plan(expected, plan_id)
            if expected["safety_protected"]:
                raise RecoveryError("recovery_point_safety_protected")
            if expected["restore_protected"]:
                raise RecoveryError("recovery_point_restore_protected")
            if confirmation != expected["confirmation"]:
                raise ValueError("recovery point deletion confirmation is invalid")
            required_last = expected["last_recovery_point_confirmation"]
            if required_last is not None and last_recovery_point_confirmation != required_last:
                raise ValueError("last Recovery Point deletion confirmation is invalid")
            state, _deployment, destination_name, destination = self._recovery_context(name)
            _, credentials = self.backup_destination_credentials(state, destination)
            result = recovery_module.delete_recovery_point_versions(
                destination_name, destination, credentials, self.backup_s3, name, recovery_point_id
            )
        return {"changed": True, **result}
