from __future__ import annotations

import hashlib
import json
import os
import re
import secrets as secrets_module
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol, cast

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


class RDSAdapter(Protocol):
    def describe_instance(
        self, account: AWSProviderAccount, network: AWSNetwork, aws_instance_identifier: str
    ) -> InstanceObservation | None: ...

    def create_instance(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource: AWSRDSPostgresResource, resource_name: str, aws_instance_identifier: str,
        security_group_ids: list[str],
    ) -> InstanceObservation: ...

    def resolve_master_credential(
        self, account: AWSProviderAccount, region: str, secret_arn: str
    ) -> tuple[str, str]: ...

    def create_workload_secret(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str,
        tags: dict[str, str], payload: dict[str, str],
    ) -> tuple[str, str]: ...


def _provider_error(exc: Exception, operation: str) -> ResourceError:
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
            "InvalidDBInstanceState": "invalid_state",
            "InvalidDBInstanceStateFault": "invalid_state",
            "DecryptionFailure": "revoked",
            "Throttling": "throttled",
            "ThrottlingException": "throttled",
            "TooManyRequestsException": "throttled",
        }
        if isinstance(provider_code, str):
            code = mapping.get(provider_code, "unavailable")
    return ResourceError(f"aws_rds_{operation}_{code}")


class BotoRDSAdapter:
    """Narrow AWS boundary for one managed RDS PostgreSQL instance and its workload
    secrets. Every call is bounded to one instance, one network, and one account."""

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
                raise ResourceError("aws_rds_role_account_mismatch")
            return session
        except ResourceError:
            raise
        except Exception as exc:
            raise _provider_error(exc, "identity") from None

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
        try:
            client.create_db_parameter_group(
                DBParameterGroupName=parameter_group_name,
                DBParameterGroupFamily=parameter_group_family,
                Description=f"Gimme-managed parameter group for {resource_name}",
                Tags=[{"Key": "gimme:resource", "Value": resource_name}],
            )
        except Exception as exc:
            error = _provider_error(exc, "parameter_group")
            if "already_exists" not in str(error):
                raise error from None
            self._verify_parameter_group_ownership(
                client, parameter_group_name, parameter_group_family, resource_name
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
                BackupRetentionPeriod=7,
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

    def create_workload_secret(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str,
        tags: dict[str, str], payload: dict[str, str],
    ) -> tuple[str, str]:
        session = self._session(account, account.inspection_role_arn, "rds-workload-secret")
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
            error = _provider_error(exc, "workload_secret_create")
            if "already_exists" not in str(error):
                raise error from None
        try:
            response = client.put_secret_value(SecretId=secret_id, SecretString=secret_string)
        except Exception as exc:
            raise _provider_error(exc, "workload_secret_write") from None
        arn = response.get("ARN")
        version_id = response.get("VersionId")
        if not isinstance(arn, str) or not isinstance(version_id, str):
            raise ResourceError("aws_rds_workload_secret_invalid")
        return arn, version_id


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


def generate_workload_password() -> str:
    return secrets_module.token_urlsafe(32)


def _observed_path(root: Path, resource_name: str) -> Path:
    if RESOURCE_NAME.fullmatch(resource_name) is None:
        raise ResourceError("resource_name_invalid")
    return root / "observed-resources" / f"{resource_name}.json"


def _validate_allocation(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != {
        "database_identifier", "secret_arn", "secret_version_id", "status",
    }:
        raise ResourceError("observed_resource_invalid")
    if (
        DB_IDENTIFIER.fullmatch(str(value.get("database_identifier"))) is None
        or AWS_ARN.fullmatch(str(value.get("secret_arn"))) is None
        or AWS_SECRET_VERSION.fullmatch(str(value.get("secret_version_id"))) is None
        or value.get("status") not in ALLOCATION_STATUS
    ):
        raise ResourceError("observed_resource_invalid")
    return value


def _validate_observed(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "resource", "aws_instance_identifier", "identity", "status",
        "phase", "engine_version", "endpoint", "port", "master_secret_arn", "allocations",
    }:
        raise ResourceError("observed_resource_invalid")
    if document.get("schema_version") != 1:
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
    for deployment_name, allocation in allocations.items():
        if DEPLOYMENT_NAME.fullmatch(deployment_name) is None:
            raise ResourceError("observed_resource_invalid")
        _validate_allocation(allocation)
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


def _save_observed(root: Path, resource_name: str, document: dict[str, object]) -> None:
    _validate_observed(document)
    path = _observed_path(root, resource_name)
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


def _instance_document(
    resource_name: str, aws_instance_identifier: str, observation: InstanceObservation,
    previous_allocations: dict[str, object],
) -> dict[str, object]:
    phase = "ready" if observation.status == "available" else "pending"
    return {
        "schema_version": 1,
        "resource": resource_name,
        "aws_instance_identifier": aws_instance_identifier,
        "identity": observation.identity,
        "status": observation.status,
        "phase": phase,
        "engine_version": observation.engine_version,
        "endpoint": observation.endpoint,
        "port": observation.port,
        "master_secret_arn": observation.master_secret_arn,
        "allocations": previous_allocations,
    }


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
    def forbid(field: str) -> None:
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


def instance_drift(
    resource: AWSRDSPostgresResource, live: InstanceObservation
) -> dict[str, object]:
    """Desired-versus-live differences for a successful live read; unobserved fields are
    skipped, and nothing here is persisted."""
    pairs = {
        "engine_version": (resource.engine_version, live.engine_version),
        "instance_class": (resource.instance_class, live.instance_class),
        "allocated_storage_gb": (resource.allocated_storage_gb, live.allocated_storage_gb),
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
    observed = adapter.describe_instance(account, network, aws_instance_identifier)
    if observed is None:
        observed = adapter.create_instance(
            account, network, resource, resource_name, aws_instance_identifier,
            security_group_ids,
        )
    deadline = now() + POLL_BUDGET_SECONDS
    while observed.status not in ("available", "failed") and now() < deadline:
        sleep(POLL_INTERVAL_SECONDS)
        refreshed = adapter.describe_instance(account, network, aws_instance_identifier)
        if refreshed is None:
            raise ResourceError("aws_rds_instance_disappeared")
        observed = refreshed
    document = _instance_document(
        resource_name, aws_instance_identifier, observed, previous_allocations
    )
    _save_observed(root, resource_name, document)
    return {
        "resource": resource_name,
        "aws_instance_identifier": aws_instance_identifier,
        "identity": observed.identity,
        "status": observed.status,
        "phase": document["phase"],
        "engine_version": observed.engine_version,
        "endpoint": observed.endpoint,
        "port": observed.port,
    }


def persist_binding(
    adapter: RDSAdapter, root: Path, account: AWSProviderAccount,
    store: AWSSecretsManagerStore, store_name: str, resource_name: str, deployment_name: str,
    database_identifier: str, workload_username: str, workload_password: str,
    endpoint: str, port: int,
) -> dict[str, object]:
    """Create/refresh the tagged Secrets Manager workload secret and record a
    secret-free allocation. Never returns workload_password or workload_username."""
    if DEPLOYMENT_NAME.fullmatch(deployment_name) is None:
        raise ResourceError("deployment_name_invalid")
    secret_name = f"{resource_name}/{deployment_name}"
    payload = {
        "username": workload_username,
        "password": workload_password,
        "host": endpoint,
        "port": str(port),
        "dbname": database_identifier,
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
        "secret_arn": secret_arn,
        "secret_version_id": version_id,
        "status": "active",
    }
    document = {**document, "allocations": allocations}
    _save_observed(root, resource_name, document)
    return {
        "deployment": deployment_name,
        "database": database_identifier,
        "secret_reference": {"store": store_name, "secret": secret_name},
    }


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
    path = _tombstone_path(root, resource_name)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{resource_name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w") as handle:
            json.dump(tombstone, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    return tombstone


def load_retained(root: Path, resource_name: str) -> dict[str, object] | None:
    path = _tombstone_path(root, resource_name)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ResourceError("retained_resource_invalid") from None
