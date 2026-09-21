from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import tempfile
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gimme.config import (
    APP_NAME,
    LARAVEL_APP_ENV,
    ABSOLUTE_DIRECTORY,
    ArtisanConfig,
    ConfigStore,
    FrontendBuildConfig,
    HealthCheckConfig,
    SchedulerConfig,
    StackConfig,
    WorkerConfig,
    _valid_endpoint,
    _valid_git_branch,
)


TARGET_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DEPLOYMENT_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
DOMAIN_NAME = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])$"
)
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
SECRET_REF = re.compile(r"^[a-z][a-z0-9-]{0,63}(?:/[A-Z][A-Z0-9_]{0,63})+$")
AWS_ACCOUNT_ID = re.compile(r"^[0-9]{12}$")
AWS_ROLE_ARN = re.compile(r"^arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]{1,512})$")
AWS_REGION = re.compile(r"^(?:[a-z]{2}(?:-gov)?|us-gov)-[a-z]+-[1-9][0-9]?$")
SECRET_STORE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
SECRET_IDENTITY = re.compile(r"^[A-Za-z0-9_+=.@-]+(?:/[A-Za-z0-9_+=.@-]+)*$")
SECRET_FIELD = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
BACKUP_DESTINATION_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
ARTIFACT_STORE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
S3_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
S3_KMS_KEY_ARN = re.compile(r"^arn:aws:kms:([a-z0-9-]+):([0-9]{12}):key/([0-9a-f-]{36})$")
AWS_NETWORK_NAME = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
AWS_VPC_ID = re.compile(r"^vpc-[0-9a-f]{8,17}$")
AWS_SUBNET_ID = re.compile(r"^subnet-[0-9a-f]{8,17}$")
AWS_SECURITY_GROUP_ID = re.compile(r"^sg-[0-9a-f]{8,17}$")
AWS_DB_INSTANCE_CLASS = re.compile(r"^db\.[a-z0-9]+\.[a-z0-9]+$")
AWS_CACHE_NODE_TYPE = re.compile(r"^cache\.[a-z0-9]+\.[a-z0-9]+$")
AWS_VALKEY_VERSION = re.compile(r"^(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){1,2}$")
CLOCK = r"(?:[01][0-9]|2[0-3]):[0-5][0-9]"
SNAPSHOT_WINDOW = re.compile(rf"^({CLOCK})-({CLOCK})$")
MAINTENANCE_WINDOW = re.compile(
    rf"^(mon|tue|wed|thu|fri|sat|sun):({CLOCK})-(mon|tue|wed|thu|fri|sat|sun):({CLOCK})$"
)
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MINUTES_PER_DAY = 24 * 60
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}(?:[-+][a-zA-Z0-9.-]+)?$")
COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
RELATIVE_PATH = re.compile(r"^[a-zA-Z0-9._-]+(?:/[a-zA-Z0-9._-]+)*$")
RESERVED_ENV_KEYS = {
    "APP_ENV",
    "APP_DEBUG",
    "APP_URL",
    "DB_CONNECTION",
    "DB_HOST",
    "DB_PORT",
    "DB_DATABASE",
    "DB_USERNAME",
    "DB_PASSWORD",
    "CACHE_STORE",
    "REDIS_HOST",
    "REDIS_PORT",
    "REDIS_PREFIX",
    "QUEUE_CONNECTION",
    "HORIZON_PREFIX",
}


# Values the laravel-cluster-v1 contract injects for a managed Valkey binding. The prefix is
# always protected; the adapter keys are protected for a Deployment bound to a managed Resource.
VALKEY_ENV_PREFIX = "GIMME_VALKEY_"
MANAGED_VALKEY_ENV_KEYS = frozenset(
    {"CACHE_STORE", "SESSION_DRIVER", "QUEUE_CONNECTION", "HORIZON_PREFIX"}
)
MANAGED_POSTGRES_ENV_KEYS = frozenset(
    {
        "DB_CONNECTION",
        "DB_HOST",
        "DB_PORT",
        "DB_DATABASE",
        "DB_USERNAME",
        "DB_PASSWORD",
        "DB_SSLMODE",
        "DB_SSLROOTCERT",
    }
)
POSTGRES_EXTENSIONS = frozenset({"pgcrypto", "uuid-ossp", "citext"})


class TargetNetwork(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["local_mdns", "public_dns"]
    mdns_name: str | None = None
    expected_addresses: list[str] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def coherent(self) -> "TargetNetwork":
        if self.mode == "local_mdns":
            if self.mdns_name is None or TARGET_NAME.fullmatch(self.mdns_name) is None:
                raise ValueError("local_mdns targets require a safe mdns_name")
            if self.expected_addresses:
                raise ValueError("local_mdns targets do not accept expected_addresses")
        else:
            if self.mdns_name is not None:
                raise ValueError("public_dns targets do not accept mdns_name")
            if not self.expected_addresses:
                raise ValueError("public_dns targets require expected_addresses")
            for address in self.expected_addresses:
                try:
                    ip_address(address)
                except ValueError as exc:
                    raise ValueError(
                        "public_dns expected_addresses must contain literal IP addresses"
                    ) from exc
        return self


RuntimeName = Literal[
    "php", "composer", "node", "npm", "pnpm", "yarn", "bun",
    "python", "ruby", "go", "java",
]


class RuntimePin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["system", "mise", "bundled"]
    version: str

    @field_validator("version")
    @classmethod
    def exact_version(cls, value: str) -> str:
        if VERSION.fullmatch(value) is None:
            raise ValueError("runtime versions must be exact bounded version strings")
        return value


class TargetRuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mise_version: str | None = None

    @field_validator("mise_version")
    @classmethod
    def exact_mise_version(cls, value: str | None) -> str | None:
        if value is not None and VERSION.fullmatch(value) is None:
            raise ValueError("mise_version must be an exact version")
        return value


class ResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern=TARGET_NAME.pattern)
    kind: Literal["postgres", "valkey"]
    provider: Literal["target_local"] = "target_local"
    version: str

    @field_validator("version")
    @classmethod
    def exact_version(cls, value: str) -> str:
        if VERSION.fullmatch(value) is None:
            raise ValueError("resource versions must be exact bounded version strings")
        return value


class AWSNetwork(BaseModel):
    """A validated, narrow subset of one pre-existing AWS VPC's prerequisites: only the
    two private data subnets a managed RDS Resource's DB subnet group may use. Gimme
    creates no VPC, subnet, route, or security group here; it only records identity."""

    model_config = ConfigDict(extra="forbid")

    provider_account: str = Field(pattern=AWS_NETWORK_NAME.pattern)
    region: str = Field(pattern=AWS_REGION.pattern)
    vpc_id: str = Field(pattern=AWS_VPC_ID.pattern)
    private_subnet_ids: list[str] = Field(min_length=2, max_length=2)

    @field_validator("private_subnet_ids")
    @classmethod
    def exact_distinct_subnets(cls, value: list[str]) -> list[str]:
        if len(set(value)) != 2 or any(AWS_SUBNET_ID.fullmatch(item) is None for item in value):
            raise ValueError("an AWS Network requires exactly two distinct data subnet ids")
        return value


def _valid_deployment_security_groups(value: dict[str, str]) -> dict[str, str]:
    for target_name, security_group_id in value.items():
        if (
            TARGET_NAME.fullmatch(target_name) is None
            or AWS_SECURITY_GROUP_ID.fullmatch(security_group_id) is None
        ):
            raise ValueError(
                "deployment_security_group_ids must map exact target names to "
                "exact security group ids"
            )
    return value


class AWSRDSPostgresResource(BaseModel):
    """One managed AWS RDS for PostgreSQL instance (ADR 0008). Bindable by many
    Deployments placed on any Target listed in deployment_security_group_ids; each owns
    an isolated database, role, and Resource Credential created at bind time."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["postgres"] = "postgres"
    provider: Literal["aws_rds_postgres"] = "aws_rds_postgres"
    aws_network: str = Field(pattern=AWS_NETWORK_NAME.pattern)
    administration_target: str = Field(pattern=TARGET_NAME.pattern)
    engine_version: str
    instance_class: str = Field(pattern=AWS_DB_INSTANCE_CLASS.pattern)
    allocated_storage_gb: int = Field(ge=20, le=65536)
    backup_window: str = "03:00-04:00"
    backup_retention_days: int = Field(default=7, ge=7, le=35)
    maintenance_window: str = "sun:05:00-sun:06:00"
    administration_security_group_id: str = Field(pattern=AWS_SECURITY_GROUP_ID.pattern)
    deployment_security_group_ids: dict[str, str] = Field(default_factory=dict, max_length=32)
    workload_secret_store: str = Field(pattern=SECRET_STORE_NAME.pattern)
    retain_on_removal: bool = Field(
        default=True,
        description="Lifecycle policy: ordinary Resource removal only deletes desired "
        "registration and leaves the RDS instance and its data intact (Retained Resource).",
    )

    @field_validator("engine_version")
    @classmethod
    def exact_engine_version(cls, value: str) -> str:
        if VERSION.fullmatch(value) is None:
            raise ValueError("engine_version must be an exact bounded PostgreSQL version")
        return value

    @field_validator("deployment_security_group_ids")
    @classmethod
    def valid_deployment_security_groups(cls, value: dict[str, str]) -> dict[str, str]:
        return _valid_deployment_security_groups(value)

    @field_validator("backup_window")
    @classmethod
    def valid_backup_window(cls, value: str) -> str:
        _snapshot_window_minutes(value)
        return value

    @field_validator("maintenance_window")
    @classmethod
    def valid_maintenance_window(cls, value: str) -> str:
        _maintenance_window_minutes(value)
        return value

    @model_validator(mode="after")
    def windows_do_not_overlap(self) -> "AWSRDSPostgresResource":
        if set(_snapshot_window_minutes(self.backup_window)) & set(
            _maintenance_window_minutes(self.maintenance_window)
        ):
            raise ValueError("backup_window and maintenance_window must not overlap")
        return self


def _clock_minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def _snapshot_window_minutes(window: str) -> list[int]:
    """Minutes of the week a daily UTC window covers, repeated on every day."""
    match = SNAPSHOT_WINDOW.fullmatch(window)
    if match is None:
        raise ValueError("snapshot_window must be a UTC HH:MM-HH:MM window")
    start, end = (_clock_minutes(part) for part in match.groups())
    length = (end - start) % MINUTES_PER_DAY
    if length < 60:
        raise ValueError("snapshot_window must span at least 60 minutes")
    return [
        (day * MINUTES_PER_DAY + start + offset) % MINUTES_PER_WEEK
        for day in range(7) for offset in range(length)
    ]


def _maintenance_window_minutes(window: str) -> list[int]:
    """Minutes of the week a weekly UTC window covers."""
    match = MAINTENANCE_WINDOW.fullmatch(window)
    if match is None:
        raise ValueError("maintenance_window must be a UTC ddd:HH:MM-ddd:HH:MM window")
    first_day, first_clock, last_day, last_clock = match.groups()
    start = WEEKDAYS.index(first_day) * MINUTES_PER_DAY + _clock_minutes(first_clock)
    end = WEEKDAYS.index(last_day) * MINUTES_PER_DAY + _clock_minutes(last_clock)
    if (end - start) % MINUTES_PER_WEEK != 60:
        raise ValueError("maintenance_window must span exactly 60 minutes")
    return [(start + offset) % MINUTES_PER_WEEK for offset in range(60)]


class AWSElastiCacheValkeyResource(BaseModel):
    """One managed AWS ElastiCache for Valkey replication group (ADR 0009): one shard, one
    cross-AZ replica, cluster mode, TLS, and synchronous durability are fixed by Gimme and
    are not fields. Bindable by many Deployments placed on any Target listed in
    deployment_security_group_ids; each owns an ACL user, namespace, and Resource Credential.
    Only the administration Target's security group and those may reach it (ADR 0009)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["valkey"] = "valkey"
    provider: Literal["aws_elasticache_valkey"] = "aws_elasticache_valkey"
    aws_network: str = Field(pattern=AWS_NETWORK_NAME.pattern)
    administration_target: str = Field(pattern=TARGET_NAME.pattern)
    engine_version: str
    node_type: str = Field(pattern=AWS_CACHE_NODE_TYPE.pattern)
    security_group_id: str = Field(pattern=AWS_SECURITY_GROUP_ID.pattern)
    administration_security_group_id: str = Field(pattern=AWS_SECURITY_GROUP_ID.pattern)
    deployment_security_group_ids: dict[str, str] = Field(default_factory=dict, max_length=32)
    snapshot_window: str
    snapshot_retention_days: int = Field(default=7, ge=1, le=35)
    maintenance_window: str
    workload_secret_store: str = Field(pattern=SECRET_STORE_NAME.pattern)
    retain_on_removal: bool = Field(
        default=True,
        description="Lifecycle policy: ordinary Resource removal only deletes desired "
        "registration and leaves the replication group and its data intact.",
    )

    @field_validator("deployment_security_group_ids")
    @classmethod
    def valid_deployment_security_groups(cls, value: dict[str, str]) -> dict[str, str]:
        return _valid_deployment_security_groups(value)

    @field_validator("engine_version")
    @classmethod
    def exact_valkey_version(cls, value: str) -> str:
        if AWS_VALKEY_VERSION.fullmatch(value) is None or int(value.split(".")[0]) < 9:
            raise ValueError("engine_version must be an exact Valkey version, 9.0 or later")
        return value

    @field_validator("snapshot_window")
    @classmethod
    def valid_snapshot_window(cls, value: str) -> str:
        _snapshot_window_minutes(value)
        return value

    @field_validator("maintenance_window")
    @classmethod
    def valid_maintenance_window(cls, value: str) -> str:
        _maintenance_window_minutes(value)
        return value

    @model_validator(mode="after")
    def windows_do_not_overlap(self) -> "AWSElastiCacheValkeyResource":
        if set(_snapshot_window_minutes(self.snapshot_window)) & set(
            _maintenance_window_minutes(self.maintenance_window)
        ):
            raise ValueError("snapshot_window and maintenance_window must not overlap")
        return self


Resource = Annotated[
    ResourceConfig | AWSRDSPostgresResource | AWSElastiCacheValkeyResource,
    Field(discriminator="provider"),
]


ValkeyUse = Literal["cache", "session", "queue"]


class ValkeyBinding(BaseModel):
    """Which Laravel uses of a Valkey Resource a Deployment relies on. The namespace and
    credential of each use are derived by Gimme and can never be supplied."""

    model_config = ConfigDict(extra="forbid")

    resource: str = Field(pattern=DEPLOYMENT_NAME.pattern)
    uses: list[ValkeyUse] = Field(min_length=1, max_length=3)

    @field_validator("uses")
    @classmethod
    def unique_uses(cls, value: list[ValkeyUse]) -> list[ValkeyUse]:
        if len(set(value)) != len(value):
            raise ValueError("each Valkey use may appear at most once")
        return value


class ResourceBindings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    database: str | None = Field(default=None, pattern=DEPLOYMENT_NAME.pattern)
    valkey: ValkeyBinding | None = None


def runs_horizon(workers: object) -> bool:
    """True for a Horizon worker that is enabled, as a model or a raw state document."""
    if isinstance(workers, dict):
        return workers.get("driver") == "horizon" and workers.get("enabled", True) is True
    return getattr(workers, "driver", None) == "horizon" and getattr(workers, "enabled", False)


class AWSProviderAccount(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["aws"] = "aws"
    account_id: str = Field(pattern=AWS_ACCOUNT_ID.pattern)
    inspection_role_arn: str = Field(min_length=20, max_length=600)
    resolver_role_arn: str = Field(min_length=20, max_length=600)
    # Optional. Assumed only while applying a confirmed destruction, never while planning,
    # registering, or inspecting, so the day-to-day roles cannot delete anything.
    destructive_role_arn: str | None = Field(default=None, min_length=20, max_length=600)

    @model_validator(mode="after")
    def exact_roles(self) -> "AWSProviderAccount":
        roles = [self.inspection_role_arn, self.resolver_role_arn]
        if self.destructive_role_arn is not None:
            roles.append(self.destructive_role_arn)
        accounts: list[str] = []
        for role in roles:
            match = AWS_ROLE_ARN.fullmatch(role)
            if match is None:
                raise ValueError("AWS roles must be exact commercial-partition IAM role ARNs")
            accounts.append(match.group(1))
        if any(account != self.account_id for account in accounts):
            raise ValueError("AWS roles must belong to the expected account")
        if len(set(roles)) != len(roles):
            raise ValueError("AWS inspection, resolver, and destructive roles must be distinct")
        return self


ProviderAccount = Annotated[AWSProviderAccount, Field(discriminator="provider")]


class SopsSecretStore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["sops"] = "sops"


class AWSSecretsManagerStore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["aws_secrets_manager"] = "aws_secrets_manager"
    provider_account: str = Field(pattern=SECRET_STORE_NAME.pattern)
    region: str = Field(pattern=AWS_REGION.pattern)
    prefix: str = Field(min_length=1, max_length=400)
    kms_key_arn: str | None = Field(default=None, min_length=20, max_length=600)

    @field_validator("prefix")
    @classmethod
    def bounded_prefix(cls, value: str) -> str:
        if (
            value.startswith(("/", "-"))
            or value.endswith("/")
            or SECRET_IDENTITY.fullmatch(value) is None
            or len(value.encode()) > 400
        ):
            raise ValueError("AWS secret prefix must be a bounded relative name")
        return value

    @model_validator(mode="after")
    def bounded_kms_policy(self) -> "AWSSecretsManagerStore":
        if self.kms_key_arn is not None:
            match = re.fullmatch(
                r"arn:aws:kms:([a-z0-9-]+):([0-9]{12}):key/([0-9a-f-]{36})",
                self.kms_key_arn,
            )
            if match is None or match.group(1) != self.region:
                raise ValueError("customer KMS key must be one exact same-region key ARN")
        return self


SecretStore = Annotated[
    SopsSecretStore | AWSSecretsManagerStore,
    Field(discriminator="provider"),
]


class SecretReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    store: str = Field(pattern=SECRET_STORE_NAME.pattern)
    secret: str = Field(min_length=1, max_length=400)
    field: str = Field(pattern=SECRET_FIELD.pattern)

    @field_validator("secret")
    @classmethod
    def bounded_secret(cls, value: str) -> str:
        if (
            value.startswith(("/", "-"))
            or value.endswith("/")
            or SECRET_IDENTITY.fullmatch(value) is None
            or any(part in {"", ".", ".."} for part in value.split("/"))
        ):
            raise ValueError("secret must be a bounded relative identity")
        return value


class SSEAES256(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["aes256"] = "aes256"


class SSEKMS(BaseModel):
    model_config = ConfigDict(extra="forbid")

    method: Literal["kms"] = "kms"
    kms_key_arn: str = Field(min_length=20, max_length=600)

    @field_validator("kms_key_arn")
    @classmethod
    def exact_kms_key(cls, value: str) -> str:
        if S3_KMS_KEY_ARN.fullmatch(value) is None:
            raise ValueError("kms_key_arn must be one exact customer-managed KMS key ARN")
        return value


BackupEncryption = Annotated[SSEAES256 | SSEKMS, Field(discriminator="method")]


class AmbientBackupAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["ambient"] = "ambient"


class CredentialReferenceBackupAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["credential_reference"] = "credential_reference"
    access_key_id: SecretReference
    secret_access_key: SecretReference
    session_token: SecretReference | None = None


BackupDestinationAuth = Annotated[
    AmbientBackupAuth | CredentialReferenceBackupAuth,
    Field(discriminator="mode"),
]


class S3BackupDestination(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["s3_compatible"] = "s3_compatible"
    bucket: str = Field(pattern=S3_BUCKET.pattern)
    region: str = Field(pattern=AWS_REGION.pattern)
    endpoint: str | None = Field(default=None, min_length=1, max_length=261)
    addressing: Literal["virtual_hosted", "path"] = "virtual_hosted"
    encryption: BackupEncryption
    auth: BackupDestinationAuth = AmbientBackupAuth()

    @field_validator("bucket")
    @classmethod
    def safe_bucket(cls, value: str) -> str:
        if ".." in value or _looks_like_ip(value):
            raise ValueError("bucket must be a safe, non-IP-literal bounded S3 bucket name")
        return value

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if value.startswith("["):
            # A bracketed IPv6 literal, optionally with a port: [::1] or [::1]:9000.
            close = value.find("]")
            if close == -1:
                raise ValueError("endpoint host is not a safe IP address or DNS name")
            host, remainder = value[1:close], value[close + 1:]
            _valid_endpoint(host)
            if remainder == "":
                return value
            port = remainder.removeprefix(":")
            if remainder == port or not port.isdigit() or not 1 <= int(port) <= 65535:
                raise ValueError("endpoint port must be a safe port number")
            return value
        host, sep, port = value.rpartition(":")
        if sep != "" and ":" in host:
            # A bare (unbracketed) IPv6 literal: rpartition would otherwise mistake its
            # last hextet for a port, and https://<host> is not a valid URL without
            # brackets around an IPv6 host either way — require [::1] / [::1]:port.
            raise ValueError("an IPv6 endpoint must be bracketed, e.g. [::1] or [::1]:9000")
        if sep == "":
            _valid_endpoint(value)
            return value
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            raise ValueError("endpoint port must be a safe port number")
        _valid_endpoint(host)
        return value

    @model_validator(mode="after")
    def bounded_kms_region(self) -> "S3BackupDestination":
        if isinstance(self.encryption, SSEKMS):
            match = S3_KMS_KEY_ARN.fullmatch(self.encryption.kms_key_arn)
            if match is not None and match.group(1) != self.region:
                raise ValueError("customer KMS key must be in the destination's own region")
        return self


class AmbientArtifactAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["ambient"] = "ambient"


class SopsArtifactAuth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["sops_reference"] = "sops_reference"
    access_key_id: SecretReference
    secret_access_key: SecretReference
    session_token: SecretReference | None = None


ArtifactStoreAuth = Annotated[
    AmbientArtifactAuth | SopsArtifactAuth,
    Field(discriminator="mode"),
]


class S3ArtifactStore(BaseModel):
    """A versioned S3-compatible store whose object names are always derived by Gimme."""

    model_config = ConfigDict(extra="forbid")

    provider: Literal["s3_compatible"] = "s3_compatible"
    bucket: str = Field(pattern=S3_BUCKET.pattern)
    region: str = Field(pattern=AWS_REGION.pattern)
    endpoint: str | None = Field(default=None, min_length=1, max_length=261)
    addressing: Literal["virtual_hosted", "path"] = "virtual_hosted"
    encryption: BackupEncryption
    publisher_auth: ArtifactStoreAuth = Field(default_factory=AmbientArtifactAuth)
    reader_auth: ArtifactStoreAuth = Field(default_factory=AmbientArtifactAuth)

    @field_validator("bucket")
    @classmethod
    def safe_bucket(cls, value: str) -> str:
        return S3BackupDestination.safe_bucket(value)

    @field_validator("endpoint")
    @classmethod
    def safe_endpoint(cls, value: str | None) -> str | None:
        return S3BackupDestination.safe_endpoint(value)

    @model_validator(mode="after")
    def bounded_kms_region(self) -> "S3ArtifactStore":
        if isinstance(self.encryption, SSEKMS):
            match = S3_KMS_KEY_ARN.fullmatch(self.encryption.kms_key_arn)
            if match is not None and match.group(1) != self.region:
                raise ValueError("customer KMS key must be in the artifact store's own region")
        return self


class ManualRecoveryCadence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["manual"] = "manual"


class HourlyRecoveryCadence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["hourly"] = "hourly"
    minute: int = Field(default=0, ge=0, le=59)

    @field_validator("minute", mode="before")
    @classmethod
    def strict_minute(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("minute must be an integer")
        return value


class DailyRecoveryCadence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["daily"] = "daily"
    hour: int = Field(default=2, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)

    @field_validator("hour", "minute", mode="before")
    @classmethod
    def strict_clock(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("schedule clock fields must be integers")
        return value


class WeeklyRecoveryCadence(DailyRecoveryCadence):
    kind: Literal["weekly"] = "weekly"
    weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "sun"


RecoveryCadence = Annotated[
    ManualRecoveryCadence | HourlyRecoveryCadence | DailyRecoveryCadence
    | WeeklyRecoveryCadence,
    Field(discriminator="kind"),
]


class RecoveryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    destination: str = Field(pattern=BACKUP_DESTINATION_NAME.pattern)
    valkey: bool = False
    quiesce_wait_seconds: int = Field(default=30, ge=1, le=300)
    cadence: RecoveryCadence = Field(default_factory=ManualRecoveryCadence)
    retain_last: int = Field(default=7, ge=1, le=365)

    @field_validator("quiesce_wait_seconds", mode="before")
    @classmethod
    def strict_quiesce_wait(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("quiesce_wait_seconds must be an integer")
        return value

    @field_validator("retain_last", mode="before")
    @classmethod
    def strict_retain_last(cls, value: object) -> object:
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("retain_last must be an integer")
        return value

    @field_validator("valkey", mode="before")
    @classmethod
    def strict_valkey_selection(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("valkey must be a boolean")
        return value


def _looks_like_ip(value: str) -> bool:
    try:
        ip_address(value)
    except ValueError:
        return False
    return True


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_alias: str = Field(pattern=TARGET_NAME.pattern)
    bootstrap_hostname: str
    hostname: str
    system_hostname: str = Field(pattern=TARGET_NAME.pattern)
    remote_user: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_-]{0,31}$")
    apps_root: str
    keep_releases: int = Field(default=5, ge=2, le=20)
    deployment_slots: int = Field(ge=0, le=1024)
    network: TargetNetwork
    stack: StackConfig
    runtimes: TargetRuntimePolicy = Field(default_factory=TargetRuntimePolicy)
    role: Literal["deployment", "administration"] = Field(
        default="deployment",
        description="An administration Target is a private-network execution boundary "
        "for one managed Resource's provider administration; it never hosts a Deployment.",
    )

    @field_validator("bootstrap_hostname", "hostname")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return _valid_endpoint(value)

    @field_validator("apps_root")
    @classmethod
    def safe_apps_root(cls, value: str) -> str:
        if ABSOLUTE_DIRECTORY.fullmatch(value.rstrip("/")) is None or ".." in Path(value).parts:
            raise ValueError("apps_root must be a safe absolute directory")
        return value.rstrip("/")

    @model_validator(mode="after")
    def coherent_network_identity(self) -> "TargetConfig":
        if self.network.mode == "local_mdns":
            expected = f"{self.network.mdns_name}.local"
            if self.hostname != expected or self.system_hostname != self.network.mdns_name:
                raise ValueError(
                    "local_mdns hostname/system_hostname must match the advertised name"
                )
        required_mise_packages = {"mise", "software-properties-common"}
        declared = set(self.stack.packages)
        if self.runtimes.mise_version is not None and not required_mise_packages <= declared:
            raise ValueError(
                "mise targets require mise and software-properties-common packages"
            )
        if "mise" in declared and self.runtimes.mise_version is None:
            raise ValueError("the mise package requires an exact mise_version")
        return self


class ApplicationBuildPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern=TARGET_NAME.pattern)
    artifact_store: str = Field(pattern=ARTIFACT_STORE_NAME.pattern)
    packaging: Literal["laravel_v1"]
    secrets: dict[str, SecretReference] = Field(default_factory=dict, max_length=32)

    @field_validator("secrets")
    @classmethod
    def safe_secret_names(
        cls, value: dict[str, SecretReference]
    ) -> dict[str, SecretReference]:
        if any(ENV_KEY.fullmatch(name) is None for name in value):
            raise ValueError("build secret names must be bounded environment names")
        return value


class ApplicationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=1, max_length=500)
    framework: Literal["common", "laravel", "symfony", "wordpress", "static"] = "common"
    build: ApplicationBuildPolicy | None = None
    frontend: FrontendBuildConfig | None = None
    artisan: ArtisanConfig | None = None
    default_health: HealthCheckConfig | None = None
    health_probes: list[HealthCheckConfig] = Field(default_factory=list, max_length=7)
    php_extensions: list[str] = Field(default_factory=list, max_length=64)
    postgres_extensions: list[Literal["pgcrypto", "uuid-ossp", "citext"]] = Field(
        default_factory=list, max_length=3
    )

    @field_validator("php_extensions")
    @classmethod
    def safe_php_extensions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            re.fullmatch(r"[a-z][a-z0-9_]{0,47}", extension) is None
            for extension in value
        ):
            raise ValueError("php_extensions must be unique safe extension names")
        return sorted(value)

    @field_validator("postgres_extensions")
    @classmethod
    def safe_postgres_extensions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or not set(value) <= POSTGRES_EXTENSIONS:
            raise ValueError("postgres_extensions must be unique allowlisted names")
        return sorted(value)

    @model_validator(mode="after")
    def coherent(self) -> "ApplicationConfig":
        # Reuse the hardened repository/framework validation in the legacy model.
        from gimme.config import AppConfig, EnvironmentConfig

        validated = AppConfig(
            repository=self.repository,
            framework=self.framework,
            frontend=self.frontend,
            artisan=self.artisan,
            health=self.default_health,
            health_probes=self.health_probes,
            environments={"default": EnvironmentConfig()},
        )
        self.artisan = validated.artisan
        if self.build is not None and self.framework != "laravel":
            raise ValueError("artifact build policy currently supports Laravel only")
        return self

class DeploymentSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["branch", "tag", "commit"]
    ref: str = Field(min_length=1, max_length=120)

    @model_validator(mode="after")
    def valid_ref(self) -> "DeploymentSource":
        if self.kind == "commit":
            if COMMIT.fullmatch(self.ref) is None:
                raise ValueError("commit sources require a 40- or 64-character lowercase hash")
        elif not _valid_git_branch(self.ref):
            raise ValueError("branch and tag sources require a safe Git ref")
        return self


class Placement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str = Field(pattern=r"^[a-z][a-z0-9-]{0,93}$")
    relative_path: str
    database_identifier: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    cache_prefix: str = Field(pattern=r"^[a-zA-Z0-9:_-]{1,160}$")
    site_host: str

    @field_validator("relative_path")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        parts = Path(value).parts
        if (
            RELATIVE_PATH.fullmatch(value) is None
            or ".." in parts
            or parts[0].startswith(".")
            or parts == ("deployments",)
        ):
            raise ValueError("relative_path must be safe and relative")
        return value

    @field_validator("site_host")
    @classmethod
    def safe_site_host(cls, value: str) -> str:
        return _valid_endpoint(value)


class PlacementPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[str] = Field(min_length=1, max_length=64)

    @field_validator("candidates")
    @classmethod
    def bounded_candidates(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)) or any(
            TARGET_NAME.fullmatch(name) is None for name in value
        ):
            raise ValueError("placement candidates must be unique Target names")
        return sorted(value)


PlacementReason = Annotated[
    str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", max_length=64)
]


class PlacementCandidateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str = Field(pattern=TARGET_NAME.pattern)
    deployment_slots: int = Field(ge=0, le=1024)
    occupied_slots: int = Field(ge=0)
    free_slots: int = Field(ge=0, le=1024)
    eligible: bool
    reasons: list[PlacementReason] = Field(max_length=16)


class PlacementDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["explicit", "policy"]
    candidates: list[str] = Field(min_length=1, max_length=64)
    selected_target: str = Field(pattern=TARGET_NAME.pattern)
    selection_rule: Literal["occupied_ratio_free_slots_name_v1"]
    candidate_results: list[PlacementCandidateDecision] = Field(
        min_length=1, max_length=64
    )
    policy_fingerprint: str = Field(pattern=r"^fleet_[0-9a-f]{64}$")
    deployment_slots: int = Field(ge=0, le=1024)
    occupied_slots: int = Field(ge=0)
    observation_fingerprint: str | None = Field(
        default=None, pattern=r"^fleet_[0-9a-f]{64}$"
    )

    @model_validator(mode="after")
    def coherent(self) -> "PlacementDecision":
        if self.candidates != sorted(set(self.candidates)):
            raise ValueError("placement decision candidates must be normalized and unique")
        if self.selected_target not in self.candidates:
            raise ValueError("selected target must be a placement candidate")
        if [result.target for result in self.candidate_results] != self.candidates:
            raise ValueError("placement candidate results must match normalized candidates")
        selected = next(
            result for result in self.candidate_results
            if result.target == self.selected_target
        )
        if not selected.eligible or selected.reasons:
            raise ValueError("selected placement candidate must be eligible")
        if self.mode == "explicit" and (
            len(self.candidates) != 1 or self.observation_fingerprint is not None
        ):
            raise ValueError("explicit placement must name one unobserved candidate")
        if self.mode == "policy" and self.observation_fingerprint is None:
            raise ValueError("policy placement requires an observation fingerprint")
        return self


class DeploymentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application: str = Field(pattern=APP_NAME.pattern)
    target: str = Field(pattern=TARGET_NAME.pattern)
    stage: Literal["local", "preview", "staging", "production"]
    release_mode: Literal["source", "artifact"]
    source: DeploymentSource
    app_env: str = "production"
    app_debug: bool = Field(default=False, strict=True)
    domain: str | None = None
    health: Literal["inherit"] | HealthCheckConfig | None = "inherit"
    health_probes: list[HealthCheckConfig] = Field(default_factory=list, max_length=7)
    workers: WorkerConfig | None = None
    scheduler: SchedulerConfig | None = None
    variables: dict[str, str] = Field(default_factory=dict, max_length=128)
    secrets: dict[str, SecretReference] = Field(default_factory=dict, max_length=128)
    runtimes: dict[RuntimeName, RuntimePin]
    resources: ResourceBindings = Field(default_factory=ResourceBindings)
    recovery: RecoveryPolicy | None = None
    placement: Placement
    placement_decision: PlacementDecision

    @model_validator(mode="after")
    def unique_own_health_probes(self) -> "DeploymentConfig":
        probes = [*([self.health] if isinstance(self.health, HealthCheckConfig) else []),
                  *self.health_probes]
        names = [probe.name for probe in probes]
        if len(names) != len(set(names)):
            raise ValueError("deployment health probe names must be unique")
        return self

    @field_validator("app_env")
    @classmethod
    def safe_app_env(cls, value: str) -> str:
        if LARAVEL_APP_ENV.fullmatch(value) is None:
            raise ValueError("app_env is unsafe")
        return value

    @field_validator("domain")
    @classmethod
    def safe_domain(cls, value: str | None) -> str | None:
        if value is not None and DOMAIN_NAME.fullmatch(value) is None:
            raise ValueError("domain must be a lowercase fully qualified DNS name")
        return value

    @field_validator("variables")
    @classmethod
    def safe_variables(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if ENV_KEY.fullmatch(key) is None or key in RESERVED_ENV_KEYS:
                raise ValueError(f"environment key is reserved or unsafe: {key}")
            if len(item) > 4096 or "\x00" in item or "\n" in item or "\r" in item:
                raise ValueError(f"environment value is unsafe: {key}")
        return value

    @field_validator("secrets")
    @classmethod
    def safe_secrets(cls, value: dict[str, SecretReference]) -> dict[str, SecretReference]:
        for key in value:
            if ENV_KEY.fullmatch(key) is None or key in RESERVED_ENV_KEYS:
                raise ValueError(f"secret environment key is reserved or unsafe: {key}")
        return value


class DeploymentRegistration(BaseModel):
    """User-owned deployment fields; placement is allocated once by Gimme."""

    model_config = ConfigDict(extra="forbid")

    application: str = Field(pattern=APP_NAME.pattern)
    target: str | None = Field(default=None, pattern=TARGET_NAME.pattern)
    placement_policy: PlacementPolicy | None = None
    stage: Literal["local", "preview", "staging", "production"]
    release_mode: Literal["source", "artifact"]
    source: DeploymentSource
    app_env: str = "production"
    app_debug: bool = Field(default=False, strict=True)
    domain: str | None = None
    health: Literal["inherit"] | HealthCheckConfig | None = "inherit"
    health_probes: list[HealthCheckConfig] = Field(default_factory=list, max_length=7)
    workers: WorkerConfig | None = None
    scheduler: SchedulerConfig | None = None
    variables: dict[str, str] = Field(default_factory=dict, max_length=128)
    secrets: dict[str, SecretReference] = Field(default_factory=dict, max_length=128)
    runtimes: dict[RuntimeName, RuntimePin]
    resources: ResourceBindings = Field(default_factory=ResourceBindings)
    recovery: RecoveryPolicy | None = None

    @model_validator(mode="after")
    def exactly_one_placement_selector(self) -> "DeploymentRegistration":
        if (self.target is None) == (self.placement_policy is None):
            raise ValueError("choose exactly one explicit target or placement_policy")
        return self

    def materialize(
        self, target: str, placement: Placement, decision: PlacementDecision
    ) -> DeploymentConfig:
        value = self.model_dump(exclude={"target", "placement_policy"})
        return DeploymentConfig(
            **value, target=target, placement=placement, placement_decision=decision
        )

    @classmethod
    def from_deployment(cls, deployment: DeploymentConfig) -> "DeploymentRegistration":
        value = deployment.model_dump(exclude={"placement", "placement_decision"})
        return cls.model_validate(value)

    @field_validator("domain")
    @classmethod
    def safe_domain(cls, value: str | None) -> str | None:
        if value is not None and DOMAIN_NAME.fullmatch(value) is None:
            raise ValueError("domain must be a lowercase fully qualified DNS name")
        return value

    @field_validator("variables")
    @classmethod
    def safe_variables(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if ENV_KEY.fullmatch(key) is None or key in RESERVED_ENV_KEYS:
                raise ValueError(f"environment key is reserved or unsafe: {key}")
            if len(item) > 4096 or "\x00" in item or "\n" in item or "\r" in item:
                raise ValueError(f"environment value is unsafe: {key}")
        return value

    @field_validator("secrets")
    @classmethod
    def safe_secrets(cls, value: dict[str, SecretReference]) -> dict[str, SecretReference]:
        for key in value:
            if ENV_KEY.fullmatch(key) is None or key in RESERVED_ENV_KEYS:
                raise ValueError(f"secret environment key is reserved or unsafe: {key}")
        return value


class RolloutArtifact(BaseModel):
    """Bounded public identity of one immutable rollout artifact."""

    model_config = ConfigDict(extra="forbid")

    application: str = Field(pattern=APP_NAME.pattern)
    build_id: str = Field(pattern=r"^build_v1_[0-9a-f]{64}$")
    commit: str = Field(pattern=COMMIT.pattern)
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    tree_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class Rollout(BaseModel):
    """Resumable desired state for one isolated sticky-traffic generation."""

    model_config = ConfigDict(extra="forbid")

    deployment: str = Field(pattern=DEPLOYMENT_NAME.pattern)
    target: str = Field(pattern=TARGET_NAME.pattern)
    generation: int = Field(ge=1, le=2_147_483_647)
    phase: Literal[
        "preparing", "active", "completing", "reversing",
        "completed", "reversed", "degraded",
    ]
    stable: RolloutArtifact
    candidate: RolloutArtifact
    stable_weight: int = Field(default=100, ge=0, le=100)
    candidate_weight: int = Field(default=0, ge=0, le=100)
    temporary_slots: Literal[1] = 1
    backend_ready: bool = False
    background_owner: Literal["stable", "candidate"] = "stable"
    drift: Literal["none", "target_unavailable", "backend_unavailable"] = "none"
    outcome: Literal[
        "preparing", "ready", "prepare_failed", "completing", "reversing",
        "completed", "reversed", "complete_failed", "reverse_failed",
    ] = "preparing"
    policy_fingerprint: str = Field(pattern=r"^rollout_[0-9a-f]{64}$")
    contract_fingerprint: str = Field(pattern=r"^rollout_[0-9a-f]{64}$")
    evidence_fingerprint: str = Field(pattern=r"^rollout_[0-9a-f]{64}$")
    route_fingerprint: str = Field(pattern=r"^rollout_[0-9a-f]{64}$")
    affinity_generation: int = Field(default=0, ge=0, le=2_147_483_647)
    stable_eligible: bool = True
    candidate_eligible: bool = False
    stable_health: Literal["ready", "unavailable", "unknown"] = "ready"
    candidate_health: Literal["ready", "unavailable", "unknown"] = "unknown"

    @model_validator(mode="after")
    def distinct_artifacts(self) -> "Rollout":
        if self.stable.application != self.candidate.application:
            raise ValueError("rollout artifacts must belong to the same application")
        if self.stable.build_id == self.candidate.build_id:
            raise ValueError("rollout candidate must differ from stable")
        if self.stable_weight + self.candidate_weight != 100:
            raise ValueError("rollout weights must total 100")
        if self.phase == "active" and not self.backend_ready:
            raise ValueError("active rollout requires a ready candidate backend")
        if self.phase == "active" and self.outcome != "ready":
            raise ValueError("active rollout requires a ready outcome")
        if self.phase in {"completed", "reversed"} and self.outcome != self.phase:
            raise ValueError("terminal rollout phase and outcome must match")
        return self


class ControlState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[8] = 8
    provider_accounts: dict[str, ProviderAccount] = Field(default_factory=dict)
    secret_stores: dict[str, SecretStore] = Field(
        default_factory=lambda: {"local-sops": SopsSecretStore()}
    )
    backup_destinations: dict[str, S3BackupDestination] = Field(default_factory=dict)
    artifact_stores: dict[str, S3ArtifactStore] = Field(default_factory=dict)
    targets: dict[str, TargetConfig] = Field(default_factory=dict)
    applications: dict[str, ApplicationConfig] = Field(default_factory=dict)
    aws_networks: dict[str, AWSNetwork] = Field(default_factory=dict)
    resources: dict[str, Resource] = Field(default_factory=dict)
    deployments: dict[str, DeploymentConfig] = Field(default_factory=dict)
    rollouts: dict[str, Rollout] = Field(default_factory=dict)

    @model_validator(mode="after")
    def references_exist(self) -> "ControlState":
        for name in self.provider_accounts:
            if SECRET_STORE_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid provider account name: {name}")
        if self.secret_stores.get("local-sops") != SopsSecretStore():
            raise ValueError("local-sops must be the fixed built-in SOPS store")
        for name, secret_store in self.secret_stores.items():
            if SECRET_STORE_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid secret store name: {name}")
            if isinstance(secret_store, AWSSecretsManagerStore):
                account = self.provider_accounts.get(secret_store.provider_account)
                if account is None:
                    raise ValueError(f"secret store {name} references an unknown provider account")
                if secret_store.kms_key_arn is not None:
                    key_account = secret_store.kms_key_arn.split(":", 5)[4]
                    if key_account != account.account_id:
                        raise ValueError(f"secret store {name} KMS key is in another account")
        for name, destination in self.backup_destinations.items():
            if BACKUP_DESTINATION_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid backup destination name: {name}")
            if isinstance(destination.auth, CredentialReferenceBackupAuth):
                for reference in (
                    destination.auth.access_key_id,
                    destination.auth.secret_access_key,
                    destination.auth.session_token,
                ):
                    if reference is None:
                        continue
                    if reference.store not in self.secret_stores:
                        raise ValueError(
                            f"backup destination {name} references an unknown secret store"
                        )
        for name, artifact_store in self.artifact_stores.items():
            if ARTIFACT_STORE_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid artifact store name: {name}")
            for auth in (artifact_store.publisher_auth, artifact_store.reader_auth):
                if not isinstance(auth, SopsArtifactAuth):
                    continue
                for reference in (
                    auth.access_key_id,
                    auth.secret_access_key,
                    auth.session_token,
                ):
                    if reference is not None and reference.store != "local-sops":
                        raise ValueError(
                            f"artifact store {name} credentials must use local-sops references"
                        )
        for name in self.targets:
            if TARGET_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid target name: {name}")
        for name, application in self.applications.items():
            if APP_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid application name: {name}")
            if application.build is not None:
                build_target = self.targets.get(application.build.target)
                if build_target is None or build_target.role != "deployment":
                    raise ValueError(
                        f"application {name} build target must be a registered "
                        "Deployment-capable Target"
                    )
                if application.build.artifact_store not in self.artifact_stores:
                    raise ValueError(
                        f"application {name} references an unknown artifact store"
                    )
                if any(
                    reference.store != "local-sops"
                    for reference in application.build.secrets.values()
                ):
                    raise ValueError(
                        f"application {name} build secrets must use local-sops references"
                    )
        for name, network in self.aws_networks.items():
            if AWS_NETWORK_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid AWS Network name: {name}")
            if network.provider_account not in self.provider_accounts:
                raise ValueError(f"AWS Network {name} references an unknown provider account")
        target_resource_versions: dict[tuple[str, str], str] = {}
        for name, resource in self.resources.items():
            if DEPLOYMENT_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid resource name: {name}")
            if isinstance(resource, ResourceConfig):
                if resource.target not in self.targets:
                    raise ValueError(f"resource {name} references an unknown target")
                if self.targets[resource.target].role != "deployment":
                    raise ValueError(f"resource {name} target must be a Deployment Target")
                key = (resource.target, resource.kind)
                previous = target_resource_versions.setdefault(key, resource.version)
                if previous != resource.version:
                    raise ValueError(
                        f"target-local {resource.kind} resources on {resource.target} "
                        "must use one version"
                    )
            else:
                network = self.aws_networks.get(resource.aws_network)
                if network is None:
                    raise ValueError(f"resource {name} references an unknown AWS Network")
                administration_target = self.targets.get(resource.administration_target)
                if administration_target is None or administration_target.role != "administration":
                    raise ValueError(
                        f"resource {name} administration_target must be a registered "
                        "administration Target"
                    )
                secret_store = self.secret_stores.get(resource.workload_secret_store)
                if not isinstance(secret_store, AWSSecretsManagerStore):
                    raise ValueError(
                        f"resource {name} workload_secret_store must be a registered "
                        "AWS Secrets Manager store"
                    )
                if isinstance(resource, (AWSRDSPostgresResource, AWSElastiCacheValkeyResource)):
                    for target_name in resource.deployment_security_group_ids:
                        deployment_target = self.targets.get(target_name)
                        if deployment_target is None or deployment_target.role != "deployment":
                            raise ValueError(
                                f"resource {name} deployment_security_group_ids references "
                                "an invalid target"
                            )
        for name, deployment in self.deployments.items():
            if DEPLOYMENT_NAME.fullmatch(name) is None:
                raise ValueError(f"invalid deployment name: {name}")
            if deployment.application not in self.applications:
                raise ValueError(f"deployment {name} references an unknown application")
            if deployment.target not in self.targets:
                raise ValueError(f"deployment {name} references an unknown target")
            if deployment.placement_decision.selected_target != deployment.target:
                raise ValueError(f"deployment {name} placement decision target is inconsistent")
            if any(
                candidate not in self.targets
                for candidate in deployment.placement_decision.candidates
            ):
                raise ValueError(f"deployment {name} placement decision candidate is unknown")
            if self.targets[deployment.target].role != "deployment":
                raise ValueError(f"deployment {name} target must be a Deployment Target")
            for reference in deployment.secrets.values():
                if reference.store not in self.secret_stores:
                    raise ValueError(
                        f"deployment {name} references unknown secret store {reference.store}"
                    )
            validate_stage_policy(
                deployment,
                self.targets[deployment.target],
                self.applications[deployment.application],
            )
            application = self.applications[deployment.application]
            if deployment.release_mode == "source":
                if deployment.stage not in {"local", "preview"}:
                    raise ValueError(
                        f"deployment {name} source release mode is limited to local and preview"
                    )
            elif application.build is None:
                raise ValueError(
                    f"deployment {name} artifact release mode requires application build policy"
                )
            validate_runtime_policy(
                deployment,
                self.targets[deployment.target],
                self.applications[deployment.application],
            )
            valkey = deployment.resources.valkey
            managed = valkey is not None and isinstance(
                self.resources.get(valkey.resource), AWSElastiCacheValkeyResource
            )
            for key in (*deployment.variables, *deployment.secrets):
                if key.startswith(VALKEY_ENV_PREFIX) or (
                    managed and key in MANAGED_VALKEY_ENV_KEYS
                ):
                    raise ValueError(
                        f"deployment {name} environment key is managed by the Valkey "
                        f"contract: {key}"
                    )
            database = deployment.resources.database
            managed_database = database is not None and isinstance(
                self.resources.get(database), AWSRDSPostgresResource
            )
            if managed_database:
                if application.framework != "laravel":
                    raise ValueError(
                        f"deployment {name} managed PostgreSQL binding supports Laravel only"
                    )
                for key in (*deployment.variables, *deployment.secrets):
                    if key in MANAGED_POSTGRES_ENV_KEYS:
                        raise ValueError(
                            f"deployment {name} environment key is managed by the PostgreSQL "
                            f"contract: {key}"
                        )
            for binding, kind in (
                (deployment.resources.database, "postgres"),
                (None if valkey is None else valkey.resource, "valkey"),
            ):
                if binding is None:
                    continue
                resource = self.resources.get(binding)
                if resource is None:
                    raise ValueError(f"deployment {name} references unknown resource {binding}")
                if resource.kind != kind:
                    raise ValueError(f"deployment {name} has an incompatible {kind} binding")
                if isinstance(resource, ResourceConfig):
                    if resource.target != deployment.target:
                        raise ValueError(f"deployment {name} has an incompatible {kind} binding")
                elif deployment.target not in resource.deployment_security_group_ids:
                    raise ValueError(
                        f"deployment {name} target is not an eligible Deployment Target "
                        f"for resource {binding}"
                    )
            is_static = self.applications[deployment.application].framework == "static"
            has_database = deployment.resources.database is not None
            has_valkey = valkey is not None
            if is_static and (has_database or has_valkey):
                raise ValueError(f"static deployment {name} cannot bind database or Valkey")
            if is_static and deployment.secrets:
                raise ValueError(f"static deployment {name} cannot receive runtime secrets")
            if deployment.recovery is not None:
                if deployment.recovery.destination not in self.backup_destinations:
                    raise ValueError(
                        f"deployment {name} references an unknown backup destination"
                    )
                if not has_database:
                    raise ValueError(
                        f"deployment {name} requires a bound database to enable recovery"
                    )
                if deployment.recovery.valkey and not has_valkey:
                    raise ValueError(
                        f"deployment {name} requires a bound Valkey resource for Valkey recovery"
                    )
            if not is_static and (
                not has_database or not has_valkey
            ):
                raise ValueError(f"deployment {name} requires database and Valkey bindings")
            if valkey is not None and runs_horizon(deployment.workers) and (
                "queue" not in valkey.uses
            ):
                raise ValueError(f"deployment {name} runs Horizon and requires the queue use")
        for name, rollout in self.rollouts.items():
            deployment = self.deployments.get(name)
            if name != rollout.deployment or deployment is None:
                raise ValueError(f"rollout {name} references an unknown deployment")
            if rollout.target != deployment.target:
                raise ValueError(f"rollout {name} target is inconsistent")
            if rollout.stable.application != deployment.application:
                raise ValueError(f"rollout {name} application is inconsistent")
        return self


def validate_runtime_policy(
    deployment: DeploymentConfig,
    target: TargetConfig,
    application: ApplicationConfig,
) -> None:
    pins = deployment.runtimes
    if application.framework != "static":
        if "php" not in pins or "composer" not in pins:
            raise ValueError("non-static deployments require php and composer runtime pins")
        if pins["php"].provider != "system":
            raise ValueError("PHP-FPM deployments currently require the system PHP provider")
        if pins["composer"].provider != "system":
            raise ValueError("Composer currently requires the system provider")
    frontend = application.frontend
    if frontend is not None:
        manager = frontend.package_manager
        if manager in {"npm", "pnpm", "yarn"} and "node" not in pins:
            raise ValueError(f"{manager} deployments require a node runtime pin")
        if manager not in pins:
            raise ValueError(f"frontend package manager {manager} requires a runtime pin")
        if manager == "npm" and pins["npm"].provider != "bundled":
            raise ValueError("npm must use the bundled provider from the selected Node runtime")
    uses_mise = any(pin.provider == "mise" for pin in pins.values())
    if uses_mise and target.runtimes.mise_version is None:
        raise ValueError("mise runtime pins require target.runtimes.mise_version")
    for name, pin in pins.items():
        if pin.provider == "bundled" and name != "npm":
            raise ValueError("only npm supports the bundled runtime provider")


def validate_stage_policy(
    deployment: DeploymentConfig,
    target: TargetConfig,
    application: ApplicationConfig,
) -> None:
    health = application.default_health if deployment.health == "inherit" else deployment.health
    probes = [*([health] if health is not None else []), *application.health_probes,
              *deployment.health_probes]
    names = [probe.name for probe in probes]
    if len(names) != len(set(names)):
        raise ValueError("effective health probe names must be unique")
    if target.network.mode == "local_mdns":
        if deployment.domain is not None:
            raise ValueError("local_mdns deployments derive their domain")
    elif deployment.domain is None:
        raise ValueError("public_dns deployments require an explicit domain")
    if deployment.stage in {"staging", "production"}:
        if deployment.app_debug:
            raise ValueError(f"{deployment.stage} deployments cannot enable APP_DEBUG")
        phases = {phase for probe in probes for phase in probe.phases}
        if not {"candidate", "live"} <= phases:
            raise ValueError(
                f"{deployment.stage} deployments require candidate and live health gates"
            )
    if deployment.stage == "production":
        if target.network.mode != "public_dns":
            raise ValueError("production deployments require a public_dns target")
        if deployment.app_env != "production":
            raise ValueError("production deployments require APP_ENV=production")
        if deployment.source.kind != "commit":
            raise ValueError("production deployments require an exact commit source")


def new_placement(
    name: str, target: TargetConfig, *, domain: str | None = None
) -> Placement:
    digest = hashlib.sha256(name.encode()).hexdigest()[:10]
    normalized = name.replace("-", "_")
    database = f"gimme_{normalized}"
    if len(database) > 63:
        database = f"{database[:52]}_{digest}"
    site_label = name if len(name) <= 63 else f"{name[:52]}-{digest}"
    site_host = domain or f"{site_label}.{target.network.mdns_name}.local"
    return Placement(
        instance=name,
        relative_path=f"deployments/{name}",
        database_identifier=database,
        cache_prefix=f"gimme:{name}:",
        site_host=site_host,
    )


def explicit_placement_decision(
    target_name: str, target: TargetConfig, *, occupied_slots: int = 0
) -> PlacementDecision:
    return PlacementDecision(
        mode="explicit",
        candidates=[target_name],
        selected_target=target_name,
        selection_rule="occupied_ratio_free_slots_name_v1",
        candidate_results=[PlacementCandidateDecision(
            target=target_name,
            deployment_slots=target.deployment_slots,
            occupied_slots=occupied_slots,
            free_slots=max(target.deployment_slots - occupied_slots, 0),
            eligible=True,
            reasons=[],
        )],
        policy_fingerprint="fleet_" + hashlib.sha256(json.dumps({
            "mode": "explicit", "candidates": [target_name],
        }, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        deployment_slots=target.deployment_slots,
        occupied_slots=occupied_slots,
    )


class StateStore:
    def __init__(self, root: Path, legacy_root: Path | None = None) -> None:
        self.root = root.expanduser().resolve()
        self.state_path = self.root / "state.json"
        self.secrets_path = self.root / "secrets.enc.json"
        self.lock_path = self.root / ".gimme.lock"
        self.legacy_root = (legacy_root or Path.cwd()).resolve()

    @classmethod
    def from_environment(cls, legacy_root: Path) -> "StateStore":
        configured = os.environ.get("GIMME_STATE_DIR")
        root = Path(configured) if configured else legacy_root / "config"
        return cls(root, legacy_root)

    def exists(self) -> bool:
        return self.state_path.is_file()

    def load(self) -> ControlState:
        if not self.exists():
            raise RuntimeError("state migration required; call plan_state_migration")
        document = self.raw_state()
        if document.get("schema_version") != 8:
            raise RuntimeError("state migration required; call plan_state_migration")
        return ControlState.model_validate(document)

    def raw_state(self) -> dict[str, object]:
        if not self.exists():
            raise RuntimeError("desired state does not exist")
        value = json.loads(self.state_path.read_text())
        if not isinstance(value, dict):
            raise RuntimeError("desired state must be a JSON object")
        return value

    def save(self, state: ControlState) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._atomic_json_write(self.state_path, state.model_dump(mode="json"))

    def update(self, operation: Callable[[ControlState], ControlState]) -> ControlState:
        """Reload, validate, and atomically persist one state transition under one lock."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            current = self.load()
            updated = operation(current)
            self._atomic_json_write(self.state_path, updated.model_dump(mode="json"))
            return updated

    def target(self, name: str) -> TargetConfig:
        try:
            return self.load().targets[name]
        except KeyError as exc:
            raise KeyError(f"target '{name}' is not registered") from exc

    def application(self, name: str) -> ApplicationConfig:
        try:
            return self.load().applications[name]
        except KeyError as exc:
            raise KeyError(f"application '{name}' is not registered") from exc

    def deployment(self, name: str) -> DeploymentConfig:
        try:
            return self.load().deployments[name]
        except KeyError as exc:
            raise KeyError(f"deployment '{name}' is not registered") from exc

    def legacy_migration(
        self,
        observations: dict[str, dict[str, str]],
        release_modes: dict[str, Literal["source", "artifact"]],
        artifact_stores: dict[str, S3ArtifactStore],
        application_builds: dict[str, ApplicationBuildPolicy],
    ) -> ControlState:
        legacy = ConfigStore(self.legacy_root)
        server = legacy.server()
        stack = legacy.stack()
        registry = legacy.registry()
        target_name = server.host_alias
        target = TargetConfig(
            host_alias=server.host_alias,
            bootstrap_hostname=server.bootstrap_hostname,
            hostname=server.hostname,
            system_hostname=server.mdns_name,
            remote_user=server.remote_user,
            apps_root=server.apps_root,
            keep_releases=server.keep_releases,
            deployment_slots=max(
                sum(len(app.environments) for app in registry.apps.values()), 1
            ),
            network=TargetNetwork(mode="local_mdns", mdns_name=server.mdns_name),
            stack=stack,
        )
        applications: dict[str, ApplicationConfig] = {}
        resources: dict[str, Resource] = {}
        deployments: dict[str, DeploymentConfig] = {}
        from gimme.plans import (
            environment_database_identifier,
            environment_deploy_path,
            environment_instance,
            environment_site_url,
        )

        for app_name, app in registry.apps.items():
            applications[app_name] = ApplicationConfig(
                repository=app.repository,
                framework=app.framework,
                build=application_builds.get(app_name),
                frontend=app.frontend,
                artisan=app.artisan,
                default_health=app.health,
            )
            for environment, definition in app.environments.items():
                deployment_name = (
                    app_name if environment == "default" else f"{app_name}-{environment}"
                )
                if len(deployment_name) > 64:
                    digest = hashlib.sha256(
                        f"{app_name}\0{environment}".encode()
                    ).hexdigest()[:8]
                    deployment_name = f"{deployment_name[:55]}-{digest}"
                if deployment_name in deployments:
                    digest = hashlib.sha256(f"{app_name}\0{environment}".encode()).hexdigest()[:8]
                    deployment_name = f"{deployment_name[:54]}-{digest}"
                if deployment_name not in release_modes:
                    raise ValueError(
                        "release_modes must name every migrated deployment exactly"
                    )
                deploy_path = environment_deploy_path(server, app_name, environment)
                relative_path = str(Path(deploy_path).relative_to(server.apps_root))
                deployments[deployment_name] = DeploymentConfig(
                    application=app_name,
                    target=target_name,
                    stage="local" if environment == "default" else "preview",
                    release_mode=release_modes[deployment_name],
                    source=DeploymentSource(kind="branch", ref=definition.branch),
                    app_env=definition.app_env,
                    app_debug=definition.app_debug,
                    health=definition.health,
                    workers=definition.workers,
                    scheduler=definition.scheduler,
                    runtimes=self._observed_runtime_pins(
                        observations[target_name], app.frontend.package_manager
                        if app.frontend is not None else None,
                        backend=app.framework != "static",
                    ),
                    resources=ResourceBindings(
                        database=f"{target_name}-postgres",
                        valkey=ValkeyBinding(
                            resource=f"{target_name}-valkey",
                            uses=["cache", "queue"] if runs_horizon(definition.workers)
                            else ["cache"],
                        ),
                    ) if app.framework != "static" else ResourceBindings(),
                    placement=Placement(
                        instance=environment_instance(app_name, environment),
                        relative_path=relative_path,
                        database_identifier=environment_database_identifier(app_name, environment),
                        cache_prefix=(
                            f"gimme:{app_name}:"
                            if environment == "default"
                            else f"gimme:{app_name}:{environment}:"
                        ),
                        site_host=environment_site_url(server, app_name, environment).removeprefix(
                            "https://"
                        ),
                    ),
                    placement_decision=explicit_placement_decision(
                        target_name, target, occupied_slots=len(deployments)
                    ),
                )
                if app.framework != "static":
                    resources[f"{target_name}-postgres"] = ResourceConfig(
                        target=target_name,
                        kind="postgres",
                        version=self._observed(observations[target_name], "postgres"),
                    )
                    resources[f"{target_name}-valkey"] = ResourceConfig(
                        target=target_name,
                        kind="valkey",
                        version=self._observed(observations[target_name], "valkey"),
                    )
        if set(release_modes) != set(deployments):
            raise ValueError("release_modes must name every migrated deployment exactly")
        if not set(application_builds) <= set(applications):
            raise ValueError("application_builds contains an unknown application")
        return ControlState(
            artifact_stores=artifact_stores,
            targets={target_name: target},
            applications=applications,
            resources=resources,
            deployments=deployments,
        )

    def state_migration(
        self,
        observations: dict[str, dict[str, str]],
        release_modes: dict[str, Literal["source", "artifact"]],
        artifact_stores: dict[str, S3ArtifactStore] | None = None,
        application_builds: dict[str, ApplicationBuildPolicy] | None = None,
    ) -> ControlState:
        stores = artifact_stores or {}
        builds = application_builds or {}
        if not self.exists():
            return self.legacy_migration(observations, release_modes, stores, builds)
        document = self.raw_state()
        if document.get("schema_version") == 8:
            raise ValueError("schema-v8 state already exists")
        if document.get("schema_version") == 7:
            migrated = json.loads(json.dumps(document))
            self._migrate_rollout_policy(migrated)
            return ControlState.model_validate(migrated)
        if document.get("schema_version") == 6:
            migrated = json.loads(json.dumps(document))
            self._migrate_fleet_policy(migrated)
            self._migrate_rollout_policy(migrated)
            return ControlState.model_validate(migrated)
        if document.get("schema_version") == 5:
            migrated = json.loads(json.dumps(document))
            self._migrate_artifact_policy(migrated, release_modes, stores, builds)
            self._migrate_fleet_policy(migrated)
            self._migrate_rollout_policy(migrated)
            return ControlState.model_validate(migrated)
        if document.get("schema_version") == 4:
            migrated = json.loads(json.dumps(document))
            self._migrate_valkey_bindings(migrated)
            self._migrate_artifact_policy(migrated, release_modes, stores, builds)
            self._migrate_fleet_policy(migrated)
            self._migrate_rollout_policy(migrated)
            return ControlState.model_validate(migrated)
        if document.get("schema_version") == 3:
            migrated = json.loads(json.dumps(document))
            migrated["schema_version"] = 5
            migrated["provider_accounts"] = {}
            migrated["secret_stores"] = {"local-sops": {"provider": "sops"}}
            deployments = migrated.get("deployments")
            if not isinstance(deployments, dict):
                raise ValueError("schema-v3 deployments are invalid")
            for deployment_name, deployment in deployments.items():
                if not isinstance(deployment, dict):
                    raise ValueError(f"deployment {deployment_name} is invalid")
                references = deployment.get("secrets", {})
                if not isinstance(references, dict):
                    raise ValueError(f"deployment {deployment_name} secrets are invalid")
                converted: dict[str, object] = {}
                for key, reference in references.items():
                    if not isinstance(key, str) or not isinstance(reference, str):
                        raise ValueError(f"deployment {deployment_name} secret is invalid")
                    parts = reference.split("/")
                    if len(parts) < 2:
                        raise ValueError(f"deployment {deployment_name} secret is invalid")
                    converted[key] = {
                        "store": "local-sops",
                        "secret": "/".join(parts[:-1]),
                        "field": parts[-1],
                    }
                deployment["secrets"] = converted
            self._migrate_valkey_bindings(migrated)
            self._migrate_artifact_policy(migrated, release_modes, stores, builds)
            self._migrate_fleet_policy(migrated)
            self._migrate_rollout_policy(migrated)
            return ControlState.model_validate(migrated)
        if document.get("schema_version") != 2:
            raise ValueError(
                "only schema-v2 through schema-v7 state can be migrated"
            )
        targets = document.get("targets")
        applications = document.get("applications")
        deployments = document.get("deployments")
        if not all(isinstance(value, dict) for value in (targets, applications, deployments)):
            raise ValueError("schema-v2 state collections are invalid")
        migrated = json.loads(json.dumps(document))
        migrated["schema_version"] = 5
        migrated["provider_accounts"] = {}
        migrated["secret_stores"] = {"local-sops": {"provider": "sops"}}
        migrated["resources"] = {}
        for target_name, target in migrated["targets"].items():
            if not isinstance(target, dict):
                raise ValueError(f"target {target_name} is invalid")
            old_toolchains = target.pop("toolchains", {})
            observed = observations.get(target_name)
            if observed is None:
                raise ValueError(f"runtime observations are missing for target {target_name}")
            target["runtimes"] = {
                "mise_version": observed.get("mise") or None,
            }
            target["_gimme_old_toolchains"] = old_toolchains
        for deployment_name, deployment in migrated["deployments"].items():
            if not isinstance(deployment, dict):
                raise ValueError(f"deployment {deployment_name} is invalid")
            target_name = deployment["target"]
            application_name = deployment["application"]
            target = migrated["targets"][target_name]
            old_toolchains = target.pop("_gimme_old_toolchains", {})
            target["_gimme_old_toolchains"] = old_toolchains
            application = migrated["applications"][application_name]
            manager = None
            if isinstance(application, dict) and isinstance(application.get("frontend"), dict):
                manager = application["frontend"].get("package_manager")
            deployment["runtimes"] = {
                name: pin.model_dump(mode="json")
                for name, pin in self._observed_runtime_pins(
                    observations[target_name], manager, old_toolchains,
                    backend=application.get("framework") != "static"
                ).items()
            }
            references = deployment.get("secrets", {})
            if not isinstance(references, dict):
                raise ValueError(f"deployment {deployment_name} secrets are invalid")
            converted: dict[str, object] = {}
            for key, reference in references.items():
                if not isinstance(key, str) or not isinstance(reference, str):
                    raise ValueError(f"deployment {deployment_name} secret is invalid")
                parts = reference.split("/")
                if len(parts) < 2:
                    raise ValueError(f"deployment {deployment_name} secret is invalid")
                converted[key] = {
                    "store": "local-sops",
                    "secret": "/".join(parts[:-1]),
                    "field": parts[-1],
                }
            deployment["secrets"] = converted
            if isinstance(application, dict) and application.get("framework") != "static":
                database_resource = f"{target_name}-postgres"
                cache_resource = f"{target_name}-valkey"
                deployment["resources"] = {
                    "database": database_resource,
                    "cache": cache_resource,
                }
                migrated["resources"][database_resource] = {
                    "target": target_name,
                    "kind": "postgres",
                    "provider": "target_local",
                    "version": self._observed(observations[target_name], "postgres"),
                }
                migrated["resources"][cache_resource] = {
                    "target": target_name,
                    "kind": "valkey",
                    "provider": "target_local",
                    "version": self._observed(observations[target_name], "valkey"),
                }
            else:
                deployment["resources"] = {"database": None, "cache": None}
        for target in migrated["targets"].values():
            target.pop("_gimme_old_toolchains", None)
        self._migrate_valkey_bindings(migrated)
        self._migrate_artifact_policy(migrated, release_modes, stores, builds)
        self._migrate_fleet_policy(migrated)
        self._migrate_rollout_policy(migrated)
        return ControlState.model_validate(migrated)

    @staticmethod
    def _migrate_rollout_policy(document: dict[str, object]) -> None:
        document["rollouts"] = {}
        document["schema_version"] = 8

    @staticmethod
    def _migrate_fleet_policy(document: dict[str, object]) -> None:
        targets = document.get("targets")
        deployments = document.get("deployments")
        if not isinstance(targets, dict) or not isinstance(deployments, dict):
            raise ValueError("migrated targets and deployments are invalid")
        occupied = {
            name: sum(
                isinstance(deployment, dict) and deployment.get("target") == name
                for deployment in deployments.values()
            )
            for name in targets
        }
        for name, target in targets.items():
            if not isinstance(target, dict):
                raise ValueError(f"target {name} is invalid")
            target["deployment_slots"] = max(occupied[name], 1)
        seen = {name: 0 for name in targets}
        for name in sorted(deployments):
            deployment = deployments[name]
            if not isinstance(deployment, dict):
                raise ValueError(f"deployment {name} is invalid")
            target_name = deployment.get("target")
            if not isinstance(target_name, str) or target_name not in targets:
                raise ValueError(f"deployment {name} target is invalid")
            deployment["placement_decision"] = {
                "mode": "explicit",
                "candidates": [target_name],
                "selected_target": target_name,
                "selection_rule": "occupied_ratio_free_slots_name_v1",
                "candidate_results": [{
                    "target": target_name,
                    "deployment_slots": targets[target_name]["deployment_slots"],
                    "occupied_slots": seen[target_name],
                    "free_slots": max(
                        targets[target_name]["deployment_slots"] - seen[target_name], 0
                    ),
                    "eligible": True,
                    "reasons": [],
                }],
                "policy_fingerprint": "fleet_" + hashlib.sha256(json.dumps({
                    "mode": "explicit", "candidates": [target_name],
                }, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                "deployment_slots": targets[target_name]["deployment_slots"],
                "occupied_slots": seen[target_name],
                "observation_fingerprint": None,
            }
            seen[target_name] += 1
        document["schema_version"] = 7

    @staticmethod
    def _migrate_artifact_policy(
        document: dict[str, object],
        release_modes: dict[str, Literal["source", "artifact"]],
        artifact_stores: dict[str, S3ArtifactStore],
        application_builds: dict[str, ApplicationBuildPolicy],
    ) -> None:
        deployments = document.get("deployments")
        applications = document.get("applications")
        if not isinstance(deployments, dict) or not isinstance(applications, dict):
            raise ValueError("migrated applications and deployments are invalid")
        if set(release_modes) != set(deployments):
            raise ValueError("release_modes must name every migrated deployment exactly")
        if not set(application_builds) <= set(applications):
            raise ValueError("application_builds contains an unknown application")
        for name, deployment in deployments.items():
            if not isinstance(deployment, dict):
                raise ValueError(f"deployment {name} is invalid")
            deployment["release_mode"] = release_modes[name]
        for name, build in application_builds.items():
            application = applications[name]
            if not isinstance(application, dict):
                raise ValueError(f"application {name} is invalid")
            application["build"] = build.model_dump(mode="json")
        document["artifact_stores"] = {
            name: store.model_dump(mode="json") for name, store in artifact_stores.items()
        }
        document["schema_version"] = 6

    @staticmethod
    def _migrate_valkey_bindings(document: dict[str, object]) -> None:
        """Rewrite the schema-v4 resources.cache string as a typed resources.valkey binding
        of the cache use, plus queue for a running Horizon; session is never inferred. Any
        shape that cannot be read unambiguously fails the migration, and no reader for the old
        shape remains."""
        document["schema_version"] = 5
        deployments = document.get("deployments")
        if not isinstance(deployments, dict):
            raise ValueError("deployments are invalid")
        for name, deployment in deployments.items():
            resources = deployment.get("resources", {}) if isinstance(deployment, dict) else None
            if not isinstance(resources, dict) or not set(resources) <= {"database", "cache"}:
                raise ValueError(f"deployment {name} resource bindings are ambiguous")
            cache = resources.pop("cache", None)
            workers = deployment.get("workers")
            if cache is not None and not isinstance(cache, str):
                raise ValueError(f"deployment {name} cache binding is ambiguous")
            if workers is not None and not (
                isinstance(workers, dict) and workers.get("driver") in ("queue", "horizon")
            ):
                raise ValueError(f"deployment {name} workers are ambiguous")
            resources["valkey"] = None if cache is None else {
                "resource": cache,
                "uses": ["cache", "queue"] if runs_horizon(workers) else ["cache"],
            }
            deployment["resources"] = resources

    @staticmethod
    def _observed(observations: dict[str, str], name: str) -> str:
        version = observations.get(name, "")
        if VERSION.fullmatch(version) is None:
            raise ValueError(f"an exact observed {name} version is required for migration")
        return version

    @classmethod
    def _observed_runtime_pins(
        cls,
        observations: dict[str, str],
        package_manager: str | None,
        old_toolchains: object = None,
        *,
        backend: bool = True,
    ) -> dict[RuntimeName, RuntimePin]:
        pins: dict[RuntimeName, RuntimePin] = {}
        if backend:
            pins.update({
                "php": RuntimePin(
                    provider="system", version=cls._observed(observations, "php")
                ),
                "composer": RuntimePin(
                    provider="system", version=cls._observed(observations, "composer")
                ),
            })
        if package_manager is None:
            return pins
        previous = old_toolchains if isinstance(old_toolchains, dict) else {}
        if package_manager in {"npm", "pnpm", "yarn"}:
            node_version = str(previous.get("node") or observations.get("node") or "")
            pins["node"] = RuntimePin(provider="system", version=node_version)
        manager_version = str(
            previous.get(package_manager) or observations.get(package_manager) or ""
        )
        pins[package_manager] = RuntimePin(
            provider="bundled" if package_manager == "npm" else "system",
            version=manager_version,
        )
        return pins

    @staticmethod
    def digest(value: object) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return "plan_" + hashlib.sha256(encoded).hexdigest()[:20]

    @staticmethod
    def _atomic_json_write(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, path)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def legacy_server(target: TargetConfig):
    from gimme.config import ServerConfig

    return ServerConfig(
        host_alias=target.host_alias,
        bootstrap_hostname=target.bootstrap_hostname,
        hostname=target.hostname,
        mdns_name=target.network.mdns_name or target.system_hostname,
        remote_user=target.remote_user,
        apps_root=target.apps_root,
        keep_releases=target.keep_releases,
    )


def legacy_app(application: ApplicationConfig, deployment: DeploymentConfig):
    from gimme.config import AppConfig, EnvironmentConfig

    health = application.default_health if deployment.health == "inherit" else deployment.health
    return AppConfig(
        repository=application.repository,
        framework=application.framework,
        frontend=application.frontend,
        artisan=application.artisan,
        health=application.default_health,
        health_probes=application.health_probes,
        environments={
            "default": EnvironmentConfig(
                branch=deployment.source.ref,
                app_env=deployment.app_env,
                app_debug=deployment.app_debug,
                health=health,
                health_probes=deployment.health_probes,
                workers=deployment.workers,
                scheduler=deployment.scheduler,
            )
        },
    )


def target_sites(state: ControlState, target_name: str) -> list[dict[str, str]]:
    sites: list[dict[str, str]] = []
    for deployment_name, deployment in sorted(state.deployments.items()):
        if deployment.target != target_name:
            continue
        application = state.applications[deployment.application]
        relative_root = {
            "laravel": "public",
            "symfony": "public",
            "static": (
                application.frontend.output_dir if application.frontend is not None else "dist"
            ),
        }.get(application.framework, "")
        document_root = (
            f"{state.targets[target_name].apps_root}/"
            f"{deployment.placement.relative_path}/current"
        )
        if relative_root:
            document_root += f"/{relative_root}"
        php_socket = ""
        if application.framework != "static":
            php = deployment.runtimes.get("php")
            if php is None:
                raise ValueError(f"deployment {deployment_name} has no PHP runtime")
            parts = php.version.split(".")
            if len(parts) < 2:
                raise ValueError("PHP runtime versions must include major and minor")
            php_socket = f"/run/php/php{parts[0]}.{parts[1]}-fpm.sock"
        sites.append(
            {
                "deployment": deployment_name,
                "instance": deployment.placement.instance,
                "framework": application.framework,
                "site_host": deployment.placement.site_host,
                "document_root": document_root,
                "php_fpm_socket": php_socket,
                "database_identifier": deployment.placement.database_identifier,
            }
        )
    return sites
