from __future__ import annotations

import hashlib
import base64
import json
import os
import re
import secrets as secrets_module
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn, Protocol, cast

from gimme.control import (
    AWSNetwork,
    AWSProviderAccount,
    AWSRDSPostgresResource,
    AWSSecretsManagerStore,
)


RESOURCE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DEPLOYMENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
AWS_INSTANCE_IDENTIFIER = re.compile(r"^gimme-[a-z0-9-]{1,57}$")
DB_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
AWS_ARN = re.compile(r"^arn:aws:[a-z0-9-]+:[a-z0-9-]*:[0-9]{12}:.+$")
AWS_SECRET_VERSION = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
INSTANCE_PHASE = ("pending", "ready", "failed")
ALLOCATION_STATUS = ("active", "detached")
POLL_BUDGET_SECONDS = 30
POLL_INTERVAL_SECONDS = 3
MAX_OBSERVED_BYTES = 32 * 1024
ABSENT_VALUE: None = None
MODIFIABLE_FIELDS = frozenset({
    "EngineVersion", "DBInstanceClass", "AllocatedStorage", "VpcSecurityGroupIds",
    "DBParameterGroupName", "BackupRetentionPeriod", "PreferredBackupWindow",
    "PreferredMaintenanceWindow",
})
# The pinned AWS commercial-region RDS trust bundle; see deploy/aws-rds-global-bundle.md.
RDS_TRUST_BUNDLE_SHA256 = "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3"


class ResourceError(RuntimeError):
    """A bounded error whose text is safe for plans, logs, and journals."""


@dataclass(frozen=True)
class InstanceObservation:
    identity: str
    status: str
    engine_version: str
    endpoint: str | None
    port: int | None
    master_secret_arn: str | None
    # Live-only, secret-free fields used for drift. None means "not observed".
    instance_class: str | None = None
    allocated_storage_gb: int | None = None
    security_group_ids: tuple[str, ...] | None = None
    modification_pending: bool = False
    # Pending values for the fields Gimme converges, and the attached parameter group.
    pending_engine_version: str | None = None
    pending_instance_class: str | None = None
    pending_allocated_storage_gb: int | None = None
    parameter_group_name: str | None = None
    parameter_group_status: str | None = None
    multi_az: bool | None = None
    storage_encrypted: bool | None = None
    deletion_protection: bool | None = None
    publicly_accessible: bool | None = None
    backup_retention_days: int | None = None
    backup_window: str | None = None
    maintenance_window: str | None = None

    @property
    def converging(self) -> bool:
        """True while AWS still has an unapplied change to a field Gimme manages. Other
        pending values (for example a maintenance-window change) do not hold readiness."""
        return any(value is not None for value in (
            self.pending_engine_version, self.pending_instance_class,
            self.pending_allocated_storage_gb,
        ))


class RDSAdapter(Protocol):
    def describe_instance(
        self, account: AWSProviderAccount, network: AWSNetwork, aws_instance_identifier: str
    ) -> InstanceObservation | None: ...

    def create_instance(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource: AWSRDSPostgresResource, resource_name: str, aws_instance_identifier: str,
        security_group_ids: list[str],
    ) -> InstanceObservation: ...

    def modify_instance(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource: AWSRDSPostgresResource, resource_name: str, aws_instance_identifier: str,
        changes: dict[str, object],
    ) -> InstanceObservation: ...

    def reboot_instance(
        self, account: AWSProviderAccount, network: AWSNetwork, aws_instance_identifier: str,
    ) -> InstanceObservation: ...

    def resolve_master_credential(
        self, account: AWSProviderAccount, region: str, secret_arn: str
    ) -> tuple[str, str]: ...

    def master_secret_version_fingerprint(
        self, account: AWSProviderAccount, region: str, secret_arn: str
    ) -> str: ...

    def create_workload_secret(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str,
        tags: dict[str, str], payload: dict[str, str],
    ) -> tuple[str, str]: ...

    def restore_workload_secret_version(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str,
        restore_version: str, remove_version: str,
    ) -> None: ...

    def resolve_workload_credential(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str,
        version_id: str,
    ) -> tuple[str, str]: ...


def _provider_error(
    exc: Exception, operation: str, prefix: str = "aws_rds"
) -> ResourceError:
    code = "unavailable"
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        provider_code = error.get("Code") if isinstance(error, dict) else None
        mapping = {
            "AccessDenied": "access_denied",
            "AccessDeniedException": "access_denied",
            # RDS wire codes are inconsistent: some carry a "Fault" suffix and some do not
            # (DBInstanceNotFound vs DBSubnetGroupNotFoundFault), so both are mapped.
            "DBInstanceNotFound": "missing",
            "DBInstanceNotFoundFault": "missing",
            "ResourceNotFoundException": "missing",
            "DBInstanceAlreadyExists": "already_exists",
            "DBInstanceAlreadyExistsFault": "already_exists",
            "DBSubnetGroupAlreadyExists": "already_exists",
            "DBSubnetGroupAlreadyExistsFault": "already_exists",
            "DBParameterGroupAlreadyExists": "already_exists",
            "DBParameterGroupAlreadyExistsFault": "already_exists",
            "ResourceExistsException": "already_exists",
            # ElastiCache wire codes mostly omit the "Fault" suffix its shapes carry.
            "ReplicationGroupNotFoundFault": "missing",
            "UserNotFound": "missing",
            "UserGroupNotFound": "missing",
            "CacheParameterGroupNotFound": "missing",
            "CacheClusterNotFound": "missing",
            "CacheSubnetGroupNotFoundFault": "missing",
            "SnapshotNotFoundFault": "missing",
            "ReplicationGroupAlreadyExists": "already_exists",
            "UserAlreadyExists": "already_exists",
            "UserGroupAlreadyExists": "already_exists",
            "CacheSubnetGroupAlreadyExists": "already_exists",
            "CacheParameterGroupAlreadyExists": "already_exists",
            "InvalidReplicationGroupState": "invalid_state",
            "InvalidUserGroupState": "invalid_state",
            "InvalidCacheParameterGroupState": "invalid_state",
            "InvalidDBInstanceState": "invalid_state",
            "InvalidDBInstanceStateFault": "invalid_state",
            "SnapshotAlreadyExistsFault": "snapshot_exists",
            "SnapshotAlreadyExists": "snapshot_exists",
            "DecryptionFailure": "revoked",
            "Throttling": "throttled",
            "ThrottlingException": "throttled",
            "TooManyRequestsException": "throttled",
        }
        if isinstance(provider_code, str):
            code = mapping.get(provider_code, "unavailable")
    return ResourceError(f"{prefix}_{operation}_{code}")


class AWSAdapter:
    """Shared AWS boundary: role assumption pinned to one account, and workload secret
    writes. Subclasses set the prefix of the bounded error codes they raise."""

    error_prefix = "aws_rds"

    @staticmethod
    def _boto3():
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - packaging guard
            raise ResourceError("aws_sdk_unavailable") from exc
        return boto3

    def _session(self, account: AWSProviderAccount, role_arn: str, purpose: str):
        boto3 = self._boto3()
        try:
            response = boto3.client("sts").assume_role(
                RoleArn=role_arn, RoleSessionName=f"gimme-{purpose}", DurationSeconds=900,
            )
            credentials = response["Credentials"]
            session = boto3.session.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
            )
            if session.client("sts").get_caller_identity().get("Account") != account.account_id:
                raise ResourceError(f"{self.error_prefix}_role_account_mismatch")
            return session
        except ResourceError:
            raise
        except Exception as exc:
            raise _provider_error(exc, "identity", self.error_prefix) from None

    def create_workload_secret(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str,
        tags: dict[str, str], payload: dict[str, str],
    ) -> tuple[str, str]:
        session = self._session(
            account, account.inspection_role_arn,
            f"{self.error_prefix.removeprefix('aws_')}-workload-secret",
        )
        client = session.client("secretsmanager", region_name=store.region)
        secret_id = f"{store.prefix}/{name}"
        secret_string = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        kms_kwargs = {"KmsKeyId": store.kms_key_arn} if store.kms_key_arn is not None else {}
        tag_list = [{"Key": key, "Value": value} for key, value in sorted(tags.items())]
        try:
            client.create_secret(
                Name=secret_id, SecretString=secret_string, Tags=tag_list, **kms_kwargs,
            )
        except Exception as exc:
            error = _provider_error(exc, "workload_secret_create", self.error_prefix)
            if "already_exists" not in str(error):
                raise error from None
        try:
            response = client.put_secret_value(SecretId=secret_id, SecretString=secret_string)
        except Exception as exc:
            raise _provider_error(exc, "workload_secret_write", self.error_prefix) from None
        arn = response.get("ARN")
        version_id = response.get("VersionId")
        if not isinstance(arn, str) or not isinstance(version_id, str):
            raise ResourceError(f"{self.error_prefix}_workload_secret_invalid")
        return arn, version_id

    def restore_workload_secret_version(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str,
        restore_version: str, remove_version: str,
    ) -> None:
        session = self._session(
            account, account.resolver_role_arn,
            f"{self.error_prefix.removeprefix('aws_')}-workload-secret-rollback",
        )
        client = session.client("secretsmanager", region_name=store.region)
        try:
            metadata = client.describe_secret(SecretId=f"{store.prefix}/{name}")
            stages = metadata.get("VersionIdsToStages")
            if not isinstance(stages, dict):
                raise ResourceError(f"{self.error_prefix}_workload_secret_invalid")
            restored_stages = stages.get(restore_version, [])
            removed_stages = stages.get(remove_version, [])
            if not isinstance(restored_stages, list) or not isinstance(removed_stages, list):
                raise ResourceError(f"{self.error_prefix}_workload_secret_invalid")
            if "AWSCURRENT" in restored_stages:
                return
            if "AWSCURRENT" not in removed_stages:
                raise ResourceError(f"{self.error_prefix}_workload_secret_rollback_stale")
            client.update_secret_version_stage(
                SecretId=f"{store.prefix}/{name}",
                VersionStage="AWSCURRENT",
                MoveToVersionId=restore_version,
                RemoveFromVersionId=remove_version,
            )
        except ResourceError:
            raise
        except Exception as exc:
            raise _provider_error(
                exc, "workload_secret_rollback", self.error_prefix
            ) from None

    def resolve_workload_credential(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str,
        version_id: str,
    ) -> tuple[str, str]:
        session = self._session(
            account, account.resolver_role_arn,
            f"{self.error_prefix.removeprefix('aws_')}-workload-secret-resolve",
        )
        try:
            response = session.client(
                "secretsmanager", region_name=store.region
            ).get_secret_value(
                SecretId=f"{store.prefix}/{name}", VersionId=version_id
            )
            document = json.loads(response.get("SecretString", ""))
        except Exception as exc:
            if isinstance(exc, (json.JSONDecodeError, UnicodeError)):
                raise ResourceError(
                    f"{self.error_prefix}_workload_secret_invalid"
                ) from None
            raise _provider_error(
                exc, "workload_secret_resolve", self.error_prefix
            ) from None
        if (
            response.get("VersionId") != version_id
            or not isinstance(document, dict)
            or set(document) != {"username", "password"}
            or any(not isinstance(value, str) or not value for value in document.values())
        ):
            raise ResourceError(f"{self.error_prefix}_workload_secret_invalid")
        return document["username"], document["password"]



class BotoRDSAdapter(AWSAdapter):
    """Narrow AWS boundary for one managed RDS PostgreSQL instance and its workload
    secrets. Every call is bounded to one instance, one network, and one account."""

    @staticmethod
    def _observation(
        response: dict[str, object], aws_instance_identifier: str
    ) -> InstanceObservation:
        arn = response.get("DBInstanceArn")
        if not isinstance(arn, str):
            raise ResourceError("aws_rds_instance_identity_invalid")
        tags = response.get("TagList") or []
        ownership = {
            item.get("Key"): item.get("Value") for item in tags if isinstance(item, dict)
        } if isinstance(tags, list) else {}
        owner = ownership.get("gimme:resource")
        if (
            not isinstance(owner, str)
            or RESOURCE_NAME.fullmatch(owner) is None
            or derive_instance_identifier(owner) != aws_instance_identifier
        ):
            raise ResourceError("aws_rds_instance_ownership_mismatch")
        endpoint = response.get("Endpoint")
        address = endpoint.get("Address") if isinstance(endpoint, dict) else None
        port = endpoint.get("Port") if isinstance(endpoint, dict) else None
        master_secret = response.get("MasterUserSecret")
        secret_arn = master_secret.get("SecretArn") if isinstance(master_secret, dict) else None
        engine_version = response.get("EngineVersion")
        status = response.get("DBInstanceStatus")
        if not isinstance(engine_version, str) or not isinstance(status, str):
            raise ResourceError("aws_rds_instance_identity_invalid")
        instance_class = response.get("DBInstanceClass")
        storage = response.get("AllocatedStorage")
        groups = response.get("VpcSecurityGroups")
        pending = response.get("PendingModifiedValues")
        pending_values = pending if isinstance(pending, dict) else {}
        pending_version = pending_values.get("EngineVersion")
        pending_class = pending_values.get("DBInstanceClass")
        pending_storage = pending_values.get("AllocatedStorage")
        parameter_groups = response.get("DBParameterGroups")
        group = (
            parameter_groups[0]
            if isinstance(parameter_groups, list) and len(parameter_groups) == 1
            and isinstance(parameter_groups[0], dict) else {}
        )
        group_name = group.get("DBParameterGroupName")
        group_status = group.get("ParameterApplyStatus")
        return InstanceObservation(
            identity=arn, status=status, engine_version=engine_version,
            endpoint=address if isinstance(address, str) else None,
            port=port if isinstance(port, int) else None,
            master_secret_arn=secret_arn if isinstance(secret_arn, str) else None,
            instance_class=instance_class if isinstance(instance_class, str) else None,
            allocated_storage_gb=storage if isinstance(storage, int) else None,
            security_group_ids=tuple(sorted(
                item["VpcSecurityGroupId"] for item in groups
                if isinstance(item, dict) and isinstance(item.get("VpcSecurityGroupId"), str)
            )) if isinstance(groups, list) else None,
            modification_pending=bool(pending),
            pending_engine_version=pending_version if isinstance(pending_version, str) else None,
            pending_instance_class=pending_class if isinstance(pending_class, str) else None,
            pending_allocated_storage_gb=(
                pending_storage if isinstance(pending_storage, int) else None
            ),
            parameter_group_name=group_name if isinstance(group_name, str) else None,
            parameter_group_status=group_status if isinstance(group_status, str) else None,
            multi_az=response.get("MultiAZ") if isinstance(response.get("MultiAZ"), bool) else None,
            storage_encrypted=(
                response.get("StorageEncrypted")
                if isinstance(response.get("StorageEncrypted"), bool) else None
            ),
            deletion_protection=(
                response.get("DeletionProtection")
                if isinstance(response.get("DeletionProtection"), bool) else None
            ),
            publicly_accessible=(
                response.get("PubliclyAccessible")
                if isinstance(response.get("PubliclyAccessible"), bool) else None
            ),
            backup_retention_days=(
                response.get("BackupRetentionPeriod")
                if isinstance(response.get("BackupRetentionPeriod"), int) else None
            ),
            backup_window=(
                response.get("PreferredBackupWindow")
                if isinstance(response.get("PreferredBackupWindow"), str) else None
            ),
            maintenance_window=(
                response.get("PreferredMaintenanceWindow")
                if isinstance(response.get("PreferredMaintenanceWindow"), str) else None
            ),
        )

    def describe_instance(
        self, account: AWSProviderAccount, network: AWSNetwork, aws_instance_identifier: str
    ) -> InstanceObservation | None:
        session = self._session(account, account.inspection_role_arn, "rds-inspect")
        try:
            response = session.client("rds", region_name=network.region).describe_db_instances(
                DBInstanceIdentifier=aws_instance_identifier
            )
        except Exception as exc:
            error = _provider_error(exc, "describe")
            if "missing" in str(error):
                return None
            raise error from None
        instances = response.get("DBInstances") or []
        if not isinstance(instances, list) or len(instances) != 1:
            raise ResourceError("aws_rds_instance_identity_invalid")
        return self._observation(instances[0], aws_instance_identifier)

    @staticmethod
    def _verify_parameter_group_ownership(
        client, parameter_group_name: str, family: str, resource_name: str
    ) -> None:
        """An already-existing group is reused only when it is this Resource's own: a
        same-named pre-created, foreign, or wrong-family group is never modified."""
        try:
            groups = client.describe_db_parameter_groups(
                DBParameterGroupName=parameter_group_name
            ).get("DBParameterGroups") or []
            arn = groups[0].get("DBParameterGroupArn") if len(groups) == 1 else None
            tags = (
                client.list_tags_for_resource(ResourceName=arn).get("TagList") or []
                if isinstance(arn, str) else []
            )
        except Exception as exc:
            raise _provider_error(exc, "parameter_group_verify") from None
        owner = {
            item.get("Key"): item.get("Value") for item in tags if isinstance(item, dict)
        }.get("gimme:resource")
        if (
            len(groups) != 1
            or groups[0].get("DBParameterGroupFamily") != family
            or owner != resource_name
        ):
            raise ResourceError("aws_rds_parameter_group_ownership_mismatch")

    def _ensure_parameter_group(
        self, client, parameter_group_name: str, family: str, resource_name: str
    ) -> None:
        """Create this Resource's parameter group (or verify it owns an existing one) and
        converge it onto rds.force_ssl=1."""
        try:
            client.create_db_parameter_group(
                DBParameterGroupName=parameter_group_name,
                DBParameterGroupFamily=family,
                Description=f"Gimme-managed parameter group for {resource_name}",
                Tags=[{"Key": "gimme:resource", "Value": resource_name}],
            )
        except Exception as exc:
            error = _provider_error(exc, "parameter_group")
            if "already_exists" not in str(error):
                raise error from None
            self._verify_parameter_group_ownership(
                client, parameter_group_name, family, resource_name
            )
        try:
            # Applied even when the group already existed so a stale group converges.
            client.modify_db_parameter_group(
                DBParameterGroupName=parameter_group_name,
                Parameters=[{
                    "ParameterName": "rds.force_ssl",
                    "ParameterValue": "1",
                    "ApplyMethod": "pending-reboot",
                }],
            )
        except Exception as exc:
            raise _provider_error(exc, "parameter_group_modify") from None

    def create_instance(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource: AWSRDSPostgresResource, resource_name: str, aws_instance_identifier: str,
        security_group_ids: list[str],
    ) -> InstanceObservation:
        parameter_group_family = _parameter_group_family(resource.engine_version)
        session = self._session(account, account.inspection_role_arn, "rds-create")
        client = session.client("rds", region_name=network.region)
        subnet_group_name = f"{aws_instance_identifier}-subnets"
        parameter_group_name = derive_parameter_group_name(aws_instance_identifier)
        try:
            client.create_db_subnet_group(
                DBSubnetGroupName=subnet_group_name,
                DBSubnetGroupDescription=f"Gimme-managed subnet group for {resource_name}",
                SubnetIds=network.private_subnet_ids,
                Tags=[{"Key": "gimme:resource", "Value": resource_name}],
            )
        except Exception as exc:
            error = _provider_error(exc, "subnet_group")
            if "already_exists" not in str(error):
                raise error from None
        self._ensure_parameter_group(
            client, parameter_group_name, parameter_group_family, resource_name
        )
        try:
            client.create_db_instance(
                DBInstanceIdentifier=aws_instance_identifier,
                Engine="postgres",
                EngineVersion=resource.engine_version,
                DBInstanceClass=resource.instance_class,
                AllocatedStorage=resource.allocated_storage_gb,
                StorageType="gp3",
                StorageEncrypted=True,
                MultiAZ=True,
                PubliclyAccessible=False,
                DBSubnetGroupName=subnet_group_name,
                DBParameterGroupName=parameter_group_name,
                VpcSecurityGroupIds=security_group_ids,
                ManageMasterUserPassword=True,
                MasterUsername="gimme_admin",
                BackupRetentionPeriod=resource.backup_retention_days,
                PreferredBackupWindow=resource.backup_window,
                PreferredMaintenanceWindow=resource.maintenance_window,
                AutoMinorVersionUpgrade=False,
                DeletionProtection=True,
                Tags=[{"Key": "gimme:resource", "Value": resource_name}],
            )
        except Exception as exc:
            error = _provider_error(exc, "create")
            if "already_exists" not in str(error):
                raise error from None
        observed = self.describe_instance(account, network, aws_instance_identifier)
        if observed is None:
            raise ResourceError("aws_rds_instance_missing_after_create")
        return observed

    def modify_instance(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource: AWSRDSPostgresResource, resource_name: str, aws_instance_identifier: str,
        changes: dict[str, object],
    ) -> InstanceObservation:
        """One immediate modification of exactly the given fields, never a major upgrade."""
        if not changes or not set(changes) <= MODIFIABLE_FIELDS:
            raise ResourceError("aws_rds_modify_field_forbidden")
        family = _parameter_group_family(resource.engine_version)
        session = self._session(account, account.inspection_role_arn, "rds-modify")
        client = session.client("rds", region_name=network.region)
        if "DBParameterGroupName" in changes:
            self._ensure_parameter_group(
                client, derive_parameter_group_name(aws_instance_identifier), family,
                resource_name,
            )
        try:
            client.modify_db_instance(
                DBInstanceIdentifier=aws_instance_identifier, ApplyImmediately=True, **changes
            )
        except Exception as exc:
            raise _provider_error(exc, "modify") from None
        observed = self.describe_instance(account, network, aws_instance_identifier)
        if observed is None:
            raise ResourceError("aws_rds_instance_disappeared")
        return observed

    def reboot_instance(
        self, account: AWSProviderAccount, network: AWSNetwork, aws_instance_identifier: str,
    ) -> InstanceObservation:
        session = self._session(account, account.inspection_role_arn, "rds-reboot")
        try:
            session.client("rds", region_name=network.region).reboot_db_instance(
                DBInstanceIdentifier=aws_instance_identifier, ForceFailover=False
            )
        except Exception as exc:
            raise _provider_error(exc, "reboot") from None
        observed = self.describe_instance(account, network, aws_instance_identifier)
        if observed is None:
            raise ResourceError("aws_rds_instance_disappeared")
        return observed

    def resolve_master_credential(
        self, account: AWSProviderAccount, region: str, secret_arn: str
    ) -> tuple[str, str]:
        session = self._session(account, account.resolver_role_arn, "rds-master-resolve")
        try:
            response = session.client(
                "secretsmanager", region_name=region
            ).get_secret_value(SecretId=secret_arn)
        except Exception as exc:
            raise _provider_error(exc, "master_secret") from None
        value = response.get("SecretString")
        if not isinstance(value, str):
            raise ResourceError("aws_rds_master_secret_invalid")
        try:
            document = json.loads(value)
        except (json.JSONDecodeError, UnicodeError):
            raise ResourceError("aws_rds_master_secret_invalid") from None
        username = document.get("username") if isinstance(document, dict) else None
        password = document.get("password") if isinstance(document, dict) else None
        if not isinstance(username, str) or not isinstance(password, str) or not password:
            raise ResourceError("aws_rds_master_secret_invalid")
        return username, password

    def master_secret_version_fingerprint(
        self, account: AWSProviderAccount, region: str, secret_arn: str
    ) -> str:
        session = self._session(account, account.inspection_role_arn, "rds-master-inspect")
        try:
            response = session.client(
                "secretsmanager", region_name=region
            ).describe_secret(SecretId=secret_arn)
        except Exception as exc:
            raise _provider_error(exc, "master_secret_metadata") from None
        versions = response.get("VersionIdsToStages")
        current = [
            version for version, stages in versions.items()
            if isinstance(version, str) and isinstance(stages, list) and "AWSCURRENT" in stages
        ] if isinstance(versions, dict) else []
        if len(current) != 1:
            raise ResourceError("aws_rds_master_secret_version_invalid")
        return "sha256:" + hashlib.sha256(
            f"{secret_arn}\0{current[0]}".encode()
        ).hexdigest()


def _parameter_group_family(engine_version: str) -> str:
    major = re.match(r"[0-9]+", engine_version)
    if major is None:
        raise ResourceError("aws_rds_engine_version_invalid")
    return f"postgres{major.group()}"


def derive_parameter_group_name(aws_instance_identifier: str) -> str:
    return f"{aws_instance_identifier}-params"


def derive_instance_identifier(resource_name: str) -> str:
    if RESOURCE_NAME.fullmatch(resource_name) is None:
        raise ResourceError("resource_name_invalid")
    candidate = f"gimme-{resource_name}"
    if len(candidate) <= 63:
        return candidate
    digest = hashlib.sha256(resource_name.encode()).hexdigest()[:8]
    return f"gimme-{resource_name[:48]}-{digest}"


def identity_fingerprint(identity: str) -> str:
    return "sha256:" + hashlib.sha256(identity.encode()).hexdigest()


def generate_workload_password() -> str:
    return secrets_module.token_urlsafe(32)


def _derived_database_role(database_identifier: str, suffix: str) -> str:
    candidate = f"{database_identifier}_{suffix}"
    if len(candidate) <= 63:
        return candidate
    digest = hashlib.sha256(candidate.encode()).hexdigest()[:8]
    return f"{database_identifier[: 54 - len(suffix)]}_{suffix}_{digest}"


def owner_role(database_identifier: str) -> str:
    return _derived_database_role(database_identifier, "owner")


def login_role(database_identifier: str, generation: int) -> str:
    if not 1 <= generation <= 999_999_999:
        raise ResourceError("aws_rds_login_generation_invalid")
    return _derived_database_role(database_identifier, f"g{generation}")


def _observed_path(root: Path, resource_name: str) -> Path:
    if RESOURCE_NAME.fullmatch(resource_name) is None:
        raise ResourceError("resource_name_invalid")
    return root / "observed-resources" / f"{resource_name}.json"


def _validate_allocation(value: object) -> dict[str, object]:
    if isinstance(value, dict) and set(value) == {
        "database_identifier", "secret_arn", "secret_version_id", "status",
    }:
        database = str(value.get("database_identifier"))
        value = {
            **value,
            "owner_role": owner_role(database),
            "login_role": login_role(database, 1),
            "generation": 1,
            "extensions": {},
        }
    if not isinstance(value, dict) or set(value) != {
        "database_identifier", "owner_role", "login_role", "generation",
        "secret_arn", "secret_version_id", "status", "extensions",
    }:
        raise ResourceError("observed_resource_invalid")
    if (
        DB_IDENTIFIER.fullmatch(str(value.get("database_identifier"))) is None
        or AWS_ARN.fullmatch(str(value.get("secret_arn"))) is None
        or AWS_SECRET_VERSION.fullmatch(str(value.get("secret_version_id"))) is None
        or value.get("status") not in ALLOCATION_STATUS
        or DB_IDENTIFIER.fullmatch(str(value.get("owner_role"))) is None
        or DB_IDENTIFIER.fullmatch(str(value.get("login_role"))) is None
        or not isinstance(value.get("generation"), int)
        or not 1 <= int(cast(int, value.get("generation"))) <= 999_999_999
        or not isinstance(value.get("extensions"), dict)
        or set(cast(dict[str, object], value["extensions"]))
        - {"pgcrypto", "uuid-ossp", "citext"}
        or any(
            not isinstance(version, str)
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", version) is None
            for version in cast(dict[str, object], value["extensions"]).values()
        )
    ):
        raise ResourceError("observed_resource_invalid")
    return value


def _validate_observed(document: object) -> dict[str, object]:
    base_fields = {
        "schema_version", "resource", "aws_instance_identifier", "identity", "status",
        "phase", "engine_version", "endpoint", "port", "master_secret_arn", "allocations",
    }
    added_fields = {
        "readiness_issues", "administration_verified",
        "master_secret_version_fingerprint", "extension_versions",
    }
    if not isinstance(document, dict) or frozenset(document) not in {
        frozenset(base_fields), frozenset(base_fields | added_fields)
    }:
        raise ResourceError("observed_resource_invalid")
    if document.get("schema_version") not in {1, 2}:
        raise ResourceError("observed_resource_invalid")
    if RESOURCE_NAME.fullmatch(str(document.get("resource"))) is None:
        raise ResourceError("observed_resource_invalid")
    if AWS_INSTANCE_IDENTIFIER.fullmatch(str(document.get("aws_instance_identifier"))) is None:
        raise ResourceError("observed_resource_invalid")
    if document.get("phase") not in INSTANCE_PHASE:
        raise ResourceError("observed_resource_invalid")
    port = document.get("port")
    if port is not None and not isinstance(port, int):
        raise ResourceError("observed_resource_invalid")
    allocations = document.get("allocations")
    if not isinstance(allocations, dict) or len(allocations) > 256:
        raise ResourceError("observed_resource_invalid")
    normalized_allocations: dict[str, object] = {}
    for deployment_name, allocation in allocations.items():
        if DEPLOYMENT_NAME.fullmatch(deployment_name) is None:
            raise ResourceError("observed_resource_invalid")
        normalized_allocations[deployment_name] = _validate_allocation(allocation)
    document = {**document, "allocations": normalized_allocations}
    if document["schema_version"] == 1:
        return {
            **document,
            "schema_version": 2,
            "readiness_issues": ["aws_rds_not_ready_administration"],
            "administration_verified": False,
            "master_secret_version_fingerprint": ABSENT_VALUE,
            "extension_versions": {},
            "phase": "pending" if document["phase"] == "ready" else document["phase"],
        }
    issues = document.get("readiness_issues")
    if (
        not isinstance(issues, list)
        or len(issues) > 16
        or any(not isinstance(issue, str) or len(issue) > 80 for issue in issues)
        or not isinstance(document.get("administration_verified"), bool)
    ):
        raise ResourceError("observed_resource_invalid")
    fingerprint = document.get("master_secret_version_fingerprint")
    if fingerprint is not None and re.fullmatch(r"sha256:[0-9a-f]{64}", str(fingerprint)) is None:
        raise ResourceError("observed_resource_invalid")
    extension_versions = document.get("extension_versions")
    if (
        not isinstance(extension_versions, dict)
        or set(extension_versions) - {"pgcrypto", "uuid-ossp", "citext"}
        or any(
            not isinstance(version, str)
            or re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,3}", version) is None
            for version in extension_versions.values()
        )
    ):
        raise ResourceError("observed_resource_invalid")
    return document


def load_observed(root: Path, resource_name: str) -> dict[str, object] | None:
    path = _observed_path(root, resource_name)
    if not path.is_file() or path.is_symlink():
        return None
    if path.stat().st_size > MAX_OBSERVED_BYTES:
        raise ResourceError("observed_resource_too_large")
    try:
        document = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ResourceError("observed_resource_invalid") from None
    return _validate_observed(document)


def _write_json(path: Path, document: dict[str, object], resource_name: str) -> None:
    """Atomically write one secret-free JSON document into a 0700 directory as 0600."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{resource_name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(document, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _save_observed(root: Path, resource_name: str, document: dict[str, object]) -> None:
    _validate_observed(document)
    _write_json(_observed_path(root, resource_name), document, resource_name)


def _instance_document(
    resource_name: str, resource: AWSRDSPostgresResource,
    aws_instance_identifier: str, observation: InstanceObservation,
    previous_allocations: dict[str, object],
) -> dict[str, object]:
    issues = readiness_issues(resource, observation)
    return {
        "schema_version": 2,
        "resource": resource_name,
        "aws_instance_identifier": aws_instance_identifier,
        "identity": observation.identity,
        "status": observation.status,
        "phase": "failed" if observation.status == "failed" else "pending",
        "engine_version": observation.engine_version,
        "endpoint": observation.endpoint,
        "port": observation.port,
        "master_secret_arn": observation.master_secret_arn,
        "master_secret_version_fingerprint": ABSENT_VALUE,
        "extension_versions": {},
        "administration_verified": False,
        "readiness_issues": issues or ["aws_rds_not_ready_administration"],
        "allocations": previous_allocations,
    }


def mark_administration_verified(
    root: Path, resource_name: str, master_secret_version_fingerprint: str,
    extension_versions: dict[str, str],
) -> dict[str, object]:
    document = load_observed(root, resource_name)
    if document is None:
        raise ResourceError("observed_resource_missing")
    if document["readiness_issues"] not in ([], ["aws_rds_not_ready_administration"]):
        raise ResourceError("aws_rds_resource_not_ready")
    updated = {
        **document,
        "phase": "ready",
        "administration_verified": True,
        "readiness_issues": [],
        "master_secret_version_fingerprint": master_secret_version_fingerprint,
        "extension_versions": extension_versions,
    }
    _save_observed(root, resource_name, updated)
    return updated


def parse_administration_verification(output: str) -> dict[str, str]:
    prefix = "GIMME_RESOURCE_VERIFIED|postgres|"
    lines = [line.split("] ", 1)[-1].strip() for line in output.splitlines()]
    payloads = [line.removeprefix(prefix) for line in lines if line.startswith(prefix)]
    if len(payloads) != 1:
        raise ResourceError("aws_rds_administration_verification_invalid")
    try:
        document = json.loads(base64.b64decode(payloads[0], validate=True))
    except (ValueError, UnicodeError, json.JSONDecodeError):
        raise ResourceError("aws_rds_administration_verification_invalid") from None
    candidate = {"extension_versions": document}
    checked = _validate_observed({
        "schema_version": 2, "resource": "verification", "aws_instance_identifier": "gimme-v",
        "identity": None, "status": "available", "phase": "pending", "engine_version": "0",
        "endpoint": None, "port": None,
        "master_secret_arn": ABSENT_VALUE,
        "allocations": {},
        "readiness_issues": [], "administration_verified": False,
        "master_secret_version_fingerprint": ABSENT_VALUE,
        **candidate,
    })
    return cast(dict[str, str], checked["extension_versions"])


def desired_security_group_ids(resource: AWSRDSPostgresResource) -> tuple[str, ...]:
    return tuple(sorted({
        resource.administration_security_group_id,
        *resource.deployment_security_group_ids.values(),
    }))


def validate_update(
    current: AWSRDSPostgresResource, proposed: AWSRDSPostgresResource,
    observed: dict[str, object] | None, bound_targets: set[str],
) -> None:
    """Refuse the updates ADR 0008 says need a new Resource. Local: never calls AWS."""
    def forbid(field: str) -> NoReturn:
        raise ResourceError(f"aws_rds_update_forbidden_{field}")

    if proposed.aws_network != current.aws_network:
        forbid("aws_network")
    if proposed.engine_version.split(".")[0] != current.engine_version.split(".")[0]:
        forbid("engine_major")
    if proposed.allocated_storage_gb < current.allocated_storage_gb:
        forbid("allocated_storage_gb")
    if (
        proposed.workload_secret_store != current.workload_secret_store
        and observed is not None and observed["allocations"]
    ):
        forbid("workload_secret_store")
    removed = set(current.deployment_security_group_ids) - set(
        proposed.deployment_security_group_ids
    )
    if removed & bound_targets:
        forbid("deployment_security_group_ids")


def _version_tuple(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in re.findall(r"[0-9]+", version))


def modification_for(
    resource: AWSRDSPostgresResource, live: InstanceObservation, aws_instance_identifier: str
) -> dict[str, object]:
    """The exact modify_db_instance fields that bring an available instance onto desired
    state, or {} when nothing differs. Values AWS already has pending count as applied, so a
    resumed apply never re-sends them. Refuses, before any call, what ADR 0008 forbids."""
    version = live.pending_engine_version or live.engine_version
    storage = (
        live.pending_allocated_storage_gb
        if live.pending_allocated_storage_gb is not None else live.allocated_storage_gb
    )
    instance_class = live.pending_instance_class or live.instance_class
    if version.split(".")[0] != resource.engine_version.split(".")[0]:
        raise ResourceError("aws_rds_modify_forbidden_engine_major")
    if _version_tuple(resource.engine_version) < _version_tuple(version):
        raise ResourceError("aws_rds_modify_forbidden_engine_downgrade")
    if storage is not None and resource.allocated_storage_gb < storage:
        raise ResourceError("aws_rds_modify_forbidden_allocated_storage_gb")
    changes: dict[str, object] = {}
    if resource.engine_version != version:
        changes["EngineVersion"] = resource.engine_version
    if instance_class is not None and resource.instance_class != instance_class:
        changes["DBInstanceClass"] = resource.instance_class
    if storage is not None and resource.allocated_storage_gb > storage:
        changes["AllocatedStorage"] = resource.allocated_storage_gb
    if (
        live.backup_retention_days is not None
        and live.backup_retention_days != resource.backup_retention_days
    ):
        changes["BackupRetentionPeriod"] = resource.backup_retention_days
    if live.backup_window is not None and live.backup_window != resource.backup_window:
        changes["PreferredBackupWindow"] = resource.backup_window
    if (
        live.maintenance_window is not None
        and live.maintenance_window != resource.maintenance_window
    ):
        changes["PreferredMaintenanceWindow"] = resource.maintenance_window
    desired_groups = desired_security_group_ids(resource)
    if live.security_group_ids is not None and live.security_group_ids != desired_groups:
        changes["VpcSecurityGroupIds"] = list(desired_groups)
    parameter_group = derive_parameter_group_name(aws_instance_identifier)
    if live.parameter_group_name is not None and live.parameter_group_name != parameter_group:
        changes["DBParameterGroupName"] = parameter_group
    return changes


def instance_drift(
    resource: AWSRDSPostgresResource, live: InstanceObservation
) -> dict[str, object]:
    """Desired-versus-live differences for a successful live read; unobserved fields are
    skipped, and nothing here is persisted."""
    pairs = {
        "engine_version": (resource.engine_version, live.engine_version),
        "instance_class": (resource.instance_class, live.instance_class),
        "allocated_storage_gb": (resource.allocated_storage_gb, live.allocated_storage_gb),
        "backup_retention_days": (
            resource.backup_retention_days, live.backup_retention_days
        ),
        "backup_window": (resource.backup_window, live.backup_window),
        "maintenance_window": (resource.maintenance_window, live.maintenance_window),
        "security_group_ids": (
            list(desired_security_group_ids(resource)),
            None if live.security_group_ids is None else list(live.security_group_ids),
        ),
    }
    return {
        "fields": {
            field: {"desired": desired, "live": actual}
            for field, (desired, actual) in pairs.items()
            if actual is not None and actual != desired
        },
        "modification_pending": live.modification_pending,
    }


def readiness_issues(
    resource: AWSRDSPostgresResource, live: InstanceObservation
) -> list[str]:
    """Fixed, bounded reasons an RDS instance cannot accept a Deployment binding."""
    checks = {
        "status": live.status == "available" and not live.converging,
        "multi_az": live.multi_az is True,
        "storage_encrypted": live.storage_encrypted is True,
        "deletion_protection": live.deletion_protection is True,
        "private": live.publicly_accessible is False,
        "parameter_group": live.parameter_group_status in {"in-sync", "applied"},
        "master_secret": live.master_secret_arn is not None,
        "backup_retention": live.backup_retention_days == resource.backup_retention_days,
        "backup_window": live.backup_window == resource.backup_window,
        "maintenance_window": live.maintenance_window == resource.maintenance_window,
    }
    return [f"aws_rds_not_ready_{name}" for name, ready in checks.items() if not ready]


def apply_provision(
    adapter: RDSAdapter, root: Path, account: AWSProviderAccount, network: AWSNetwork,
    resource: AWSRDSPostgresResource, resource_name: str,
    *, sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Create the RDS instance if absent, then poll up to a bounded 30 seconds. A
    still-provisioning instance is recorded as phase 'pending'; a later call to this
    same function resumes by describing rather than re-creating (idempotent resume)."""
    aws_instance_identifier = derive_instance_identifier(resource_name)
    previous = load_observed(root, resource_name)
    previous_allocations: dict[str, object] = (
        dict(cast(dict[str, object], previous["allocations"])) if previous is not None else {}
    )
    security_group_ids = list(desired_security_group_ids(resource))
    modified_fields: list[str] = []
    rebooted = False
    deadline = now() + POLL_BUDGET_SECONDS

    def settle(observed: InstanceObservation) -> InstanceObservation:
        while (
            observed.status not in ("available", "failed") or observed.converging
        ) and now() < deadline:
            sleep(POLL_INTERVAL_SECONDS)
            refreshed = adapter.describe_instance(account, network, aws_instance_identifier)
            if refreshed is None:
                raise ResourceError("aws_rds_instance_disappeared")
            observed = refreshed
        return observed

    observed = adapter.describe_instance(account, network, aws_instance_identifier)
    if observed is None:
        observed = adapter.create_instance(
            account, network, resource, resource_name, aws_instance_identifier,
            security_group_ids,
        )
    else:
        if observed.status not in ("available", "failed"):
            # Diff against the settled instance so drift is not reported as ready.
            observed = settle(observed)
        if observed.status == "available":
            changes = modification_for(resource, observed, aws_instance_identifier)
            if changes:
                observed = adapter.modify_instance(
                    account, network, resource, resource_name, aws_instance_identifier, changes
                )
                modified_fields = sorted(changes)
    observed = settle(observed)
    # Reboot at most once per apply, based on the live status. A second apply racing RDS's
    # status flip could reboot again; add a journal entry if that ever matters.
    if (
        observed.status == "available" and not observed.converging
        and observed.parameter_group_status == "pending-reboot"
    ):
        observed = settle(adapter.reboot_instance(account, network, aws_instance_identifier))
        rebooted = True
    document = _instance_document(
        resource_name, resource, aws_instance_identifier, observed, previous_allocations
    )
    _save_observed(root, resource_name, document)
    return {
        "resource": resource_name,
        "status": observed.status,
        "phase": document["phase"],
        "readiness_issues": document["readiness_issues"],
        "engine_version": observed.engine_version,
        "modified_fields": modified_fields,
        "rebooted": rebooted,
    }


def persist_binding(
    adapter: RDSAdapter, root: Path, account: AWSProviderAccount,
    store: AWSSecretsManagerStore, store_name: str, resource_name: str, deployment_name: str,
    database_identifier: str, owner: str, workload_username: str, generation: int,
    extensions: dict[str, str], workload_password: str,
) -> dict[str, object]:
    """Create/refresh the tagged Secrets Manager workload secret and record a
    secret-free allocation. Never returns workload_password or workload_username."""
    if DEPLOYMENT_NAME.fullmatch(deployment_name) is None:
        raise ResourceError("deployment_name_invalid")
    secret_name = f"{resource_name}/{deployment_name}"
    payload = {
        "username": workload_username,
        "password": workload_password,
    }
    tags = {
        "gimme:secret-store": store_name,
        "gimme:resource": resource_name,
        "gimme:deployment": deployment_name,
    }
    secret_arn, version_id = adapter.create_workload_secret(
        account, store, secret_name, tags, payload
    )
    document = load_observed(root, resource_name)
    if document is None:
        raise ResourceError("observed_resource_missing")
    allocations: dict[str, object] = dict(cast(dict[str, object], document["allocations"]))
    allocations[deployment_name] = {
        "database_identifier": database_identifier,
        "owner_role": owner,
        "login_role": workload_username,
        "generation": generation,
        "secret_arn": secret_arn,
        "secret_version_id": version_id,
        "status": "active",
        "extensions": dict(sorted(extensions.items())),
    }
    document = {**document, "allocations": allocations}
    _save_observed(root, resource_name, document)
    return {
        "deployment": deployment_name,
        "database": database_identifier,
        "secret_reference": {"store": store_name, "secret": secret_name},
    }


def restore_allocation(
    root: Path, resource_name: str, deployment_name: str, allocation: dict[str, object]
) -> None:
    document = load_observed(root, resource_name)
    if document is None:
        raise ResourceError("observed_resource_missing")
    allocations = dict(cast(dict[str, object], document["allocations"]))
    allocations[deployment_name] = _validate_allocation(allocation)
    _save_observed(root, resource_name, {**document, "allocations": allocations})


def update_allocation_extensions(
    root: Path, resource_name: str, deployment_name: str, extensions: dict[str, str]
) -> dict[str, object]:
    document = load_observed(root, resource_name)
    if document is None:
        raise ResourceError("observed_resource_missing")
    allocations = dict(cast(dict[str, dict[str, object]], document["allocations"]))
    allocation = allocations.get(deployment_name)
    if allocation is None:
        raise ResourceError("aws_rds_binding_missing")
    updated = _validate_allocation({**allocation, "extensions": extensions})
    allocations[deployment_name] = updated
    _save_observed(root, resource_name, {**document, "allocations": allocations})
    return updated


def _rotation_path(root: Path, resource_name: str) -> Path:
    if RESOURCE_NAME.fullmatch(resource_name) is None:
        raise ResourceError("resource_name_invalid")
    return root / "rotating-postgres-resources" / f"{resource_name}.json"


def _validate_rotation(
    document: object, resource_name: str
) -> dict[str, object]:
    if (
        not isinstance(document, dict)
        or set(document) != {
            "schema_version", "resource", "deployment", "phase", "plan", "previous",
            "candidate_generation", "candidate_login", "candidate",
        }
        or document.get("schema_version") != 1
        or document.get("resource") != resource_name
        or DEPLOYMENT_NAME.fullmatch(str(document.get("deployment"))) is None
        or document.get("phase") not in {
            "prepared", "published", "activated", "rolling_back",
        }
        or not isinstance(document.get("plan"), dict)
        or not isinstance(document.get("previous"), dict)
        or _validate_allocation(document["previous"]) != document["previous"]
        or not isinstance(document.get("candidate_generation"), int)
        or DB_IDENTIFIER.fullmatch(str(document.get("candidate_login"))) is None
        or (
            document.get("candidate") is not None
            and (
                not isinstance(document["candidate"], dict)
                or _validate_allocation(document["candidate"]) != document["candidate"]
            )
        )
        or (
            document.get("phase") in {"published", "activated", "rolling_back"}
            and document.get("candidate") is None
        )
    ):
        raise ResourceError("aws_rds_rotation_marker_invalid")
    return cast(dict[str, object], document)


def load_rotation(root: Path, resource_name: str) -> dict[str, object] | None:
    path = _rotation_path(root, resource_name)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        document = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ResourceError("aws_rds_rotation_marker_invalid") from None
    return _validate_rotation(document, resource_name)


def save_rotation(root: Path, resource_name: str, document: dict[str, object]) -> None:
    _validate_rotation(document, resource_name)
    _write_json(_rotation_path(root, resource_name), document, resource_name)


def clear_rotation(root: Path, resource_name: str) -> None:
    _rotation_path(root, resource_name).unlink(missing_ok=True)


def _tombstone_path(root: Path, resource_name: str) -> Path:
    if RESOURCE_NAME.fullmatch(resource_name) is None:
        raise ResourceError("resource_name_invalid")
    return root / "retained-resources" / f"{resource_name}.json"


def retain_resource(root: Path, resource_name: str, aws_network: str) -> dict[str, object]:
    """Record a secret-free Retained Resource tombstone. Ordinary removal never
    deletes the underlying RDS instance or its data; this is inventory only."""
    observed = load_observed(root, resource_name)
    tombstone = {
        "schema_version": 1,
        "resource": resource_name,
        "aws_network": aws_network,
        "aws_instance_identifier": (
            observed["aws_instance_identifier"] if observed is not None else None
        ),
        "identity": observed["identity"] if observed is not None else None,
    }
    _write_json(_tombstone_path(root, resource_name), tombstone, resource_name)
    return tombstone


def forget_retained(root: Path, resource_name: str) -> None:
    """Delete only the local tombstone. The infrastructure it named is untouched and stays
    unadoptable."""
    _tombstone_path(root, resource_name).unlink(missing_ok=True)


def load_retained(root: Path, resource_name: str) -> dict[str, object] | None:
    path = _tombstone_path(root, resource_name)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ResourceError("retained_resource_invalid") from None
