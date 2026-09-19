from __future__ import annotations

import hashlib
import json
import re
import secrets as secrets_module
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn, Protocol, cast

from gimme.control import (
    AWSElastiCacheValkeyResource,
    AWSNetwork,
    AWSProviderAccount,
    AWSSecretsManagerStore,
)
from gimme.resources_postgres import (
    MAX_OBSERVED_BYTES,
    POLL_BUDGET_SECONDS,
    POLL_INTERVAL_SECONDS,
    RESOURCE_NAME,
    AWSAdapter,
    ResourceError,
    _observed_path,
    _provider_error,
    _tombstone_path,
    _write_json,
)

ID_LIMIT = 40
GROUP_PHASE = ("pending", "ready", "degraded", "failed")
# Fixed, secret-free reasons a group that AWS reports available is not ready.
ISSUES = (
    "cluster_mode", "topology", "availability_zones", "multi_az", "automatic_failover", "tls",
    "encryption_at_rest", "durability", "authentication", "snapshot_policy",
    "maintenance_policy", "automatic_minor_upgrade",
)
PORT = 6379
# The default user can never authenticate; the administrative identity is limited to
# Gimme-owned key and channel prefixes and a fixed maintenance command set.
DEFAULT_ACCESS_STRING = "off ~* -@all"
ADMIN_ACCESS_STRING = (
    "on ~{gimme:* &{gimme:* -@all +ping +info +get +set +del +exists +ttl +type +scan"
)
ADMIN_USER_NAME = "gimme-admin"


def validate_update(
    current: AWSElastiCacheValkeyResource, proposed: AWSElastiCacheValkeyResource
) -> None:
    """Refuse the updates ADR 0009 says need a new Resource. Local: never calls AWS.
    Same-major engine, node type, windows, and retention are allowed and applied later."""
    def forbid(field: str) -> NoReturn:
        raise ResourceError(f"aws_elasticache_update_forbidden_{field}")

    if proposed.aws_network != current.aws_network:
        forbid("aws_network")
    if proposed.engine_version.split(".")[0] != current.engine_version.split(".")[0]:
        forbid("engine_major")
    if proposed.security_group_id != current.security_group_id:
        forbid("security_group_id")


def derive_group_id(resource_name: str) -> str:
    """ElastiCache identifiers are at most 40 characters of letters, digits, and single
    hyphens, so a long or irregular Resource name gets a hash suffix."""
    if RESOURCE_NAME.fullmatch(resource_name) is None:
        raise ResourceError("resource_name_invalid")
    slug = re.sub(r"-+", "-", resource_name).rstrip("-")
    candidate = f"gimme-{slug}"
    if slug == resource_name and len(candidate) <= ID_LIMIT:
        return candidate
    digest = hashlib.sha256(resource_name.encode()).hexdigest()[:8]
    return f"gimme-{slug[:25].rstrip('-')}-{digest}"


def _derived(group_id: str, suffix: str) -> str:
    candidate = f"{group_id}-{suffix}"
    if len(candidate) <= ID_LIMIT:
        return candidate
    digest = hashlib.sha256(group_id.encode()).hexdigest()[:8]
    return f"{group_id[:ID_LIMIT - len(suffix) - 10].rstrip('-')}-{digest}-{suffix}"


def derive_user_group_id(group_id: str) -> str:
    return _derived(group_id, "users")


def _parameter_group_family(engine_version: str) -> str:
    return f"valkey{engine_version.split('.')[0]}"


@dataclass(frozen=True)
class GroupObservation:
    identity: str
    status: str
    engine_version: str | None
    node_type: str | None
    cluster_enabled: bool
    shards: int
    members: int
    member_zones: tuple[str, ...]
    multi_az: bool
    automatic_failover: bool
    transit_encryption: bool
    at_rest_encryption: bool
    effective_durability: str | None
    user_group_ids: tuple[str, ...]
    snapshot_retention_days: int | None
    snapshot_window: str | None
    maintenance_window: str | None
    automatic_minor_upgrade: bool | None
    endpoint: str | None
    port: int | None


class ElastiCacheAdapter(Protocol):
    def describe_group(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str
    ) -> GroupObservation | None: ...

    def create_group(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource: AWSElastiCacheValkeyResource, resource_name: str, group_id: str,
        store: AWSSecretsManagerStore, store_name: str,
    ) -> GroupObservation: ...


def _tags(response: dict[str, object]) -> dict[object, object]:
    tags = response.get("TagList") or []
    return {
        item.get("Key"): item.get("Value") for item in tags if isinstance(item, dict)
    } if isinstance(tags, list) else {}


def _flag(value: object) -> bool:
    return value is True or value == "enabled"


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


class BotoElastiCacheAdapter(AWSAdapter):
    """Narrow AWS boundary for one managed ElastiCache Valkey replication group, its
    subnet group, parameter group, user group, and administrative identity."""

    error_prefix = "aws_elasticache"

    def _client(self, account: AWSProviderAccount, network: AWSNetwork, purpose: str):
        session = self._session(account, account.inspection_role_arn, purpose)
        return session.client("elasticache", region_name=network.region)

    def _observation(
        self, client, response: dict[str, object], group_id: str
    ) -> GroupObservation:
        arn = response.get("ARN")
        status = response.get("Status")
        if not isinstance(arn, str) or not isinstance(status, str):
            raise ResourceError("aws_elasticache_group_identity_invalid")
        try:
            tags = client.list_tags_for_resource(ResourceName=arn)
        except Exception as exc:
            raise _provider_error(exc, "tags", self.error_prefix) from None
        owner = _tags(tags).get("gimme:resource")
        if (
            not isinstance(owner, str)
            or RESOURCE_NAME.fullmatch(owner) is None
            or derive_group_id(owner) != group_id
        ):
            raise ResourceError("aws_elasticache_group_ownership_mismatch")
        member_ids = response.get("MemberClusters")
        node_groups = response.get("NodeGroups")
        if not isinstance(member_ids, list) or not isinstance(node_groups, list):
            raise ResourceError("aws_elasticache_group_identity_invalid")
        zones = tuple(sorted(
            member["PreferredAvailabilityZone"]
            for node_group in node_groups if isinstance(node_group, dict)
            for member in node_group.get("NodeGroupMembers") or []
            if isinstance(member, dict) and isinstance(member.get("PreferredAvailabilityZone"), str)
        ))
        cluster: dict[str, object] = {}
        if member_ids:
            try:
                clusters = client.describe_cache_clusters(CacheClusterId=member_ids[0])
            except Exception as exc:
                error = _provider_error(exc, "describe_cluster", self.error_prefix)
                if "missing" not in str(error):
                    raise error from None
            else:
                found = clusters.get("CacheClusters") or []
                cluster = found[0] if isinstance(found, list) and found else {}
        endpoint = response.get("ConfigurationEndpoint")
        address = endpoint.get("Address") if isinstance(endpoint, dict) else None
        port = endpoint.get("Port") if isinstance(endpoint, dict) else None
        retention = response.get("SnapshotRetentionLimit")
        groups = response.get("UserGroupIds")
        version = cluster.get("EngineVersion")
        window = cluster.get("PreferredMaintenanceWindow")
        minor = cluster.get("AutoMinorVersionUpgrade")
        return GroupObservation(
            identity=arn, status=status,
            engine_version=version if isinstance(version, str) else None,
            node_type=_text(response.get("CacheNodeType")),
            cluster_enabled=response.get("ClusterEnabled") is True,
            shards=len(node_groups), members=len(member_ids), member_zones=zones,
            multi_az=_flag(response.get("MultiAZ")),
            automatic_failover=_flag(response.get("AutomaticFailover")),
            transit_encryption=response.get("TransitEncryptionEnabled") is True,
            at_rest_encryption=response.get("AtRestEncryptionEnabled") is True,
            effective_durability=_text(response.get("EffectiveDurability")),
            user_group_ids=tuple(sorted(
                item for item in groups if isinstance(item, str)
            )) if isinstance(groups, list) else (),
            snapshot_retention_days=retention if isinstance(retention, int) else None,
            snapshot_window=_text(response.get("SnapshotWindow")),
            maintenance_window=window if isinstance(window, str) else None,
            automatic_minor_upgrade=minor if isinstance(minor, bool) else None,
            endpoint=address if isinstance(address, str) else None,
            port=port if isinstance(port, int) else None,
        )

    def describe_group(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str
    ) -> GroupObservation | None:
        client = self._client(account, network, "elasticache-inspect")
        try:
            response = client.describe_replication_groups(ReplicationGroupId=group_id)
        except Exception as exc:
            error = _provider_error(exc, "describe", self.error_prefix)
            if "missing" in str(error):
                return None
            raise error from None
        groups = response.get("ReplicationGroups") or []
        if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
            raise ResourceError("aws_elasticache_group_identity_invalid")
        return self._observation(client, groups[0], group_id)

    def _tolerate_existing(self, operation: str, call: Callable[[], object]) -> None:
        try:
            call()
        except Exception as exc:
            error = _provider_error(exc, operation, self.error_prefix)
            if "already_exists" not in str(error):
                raise error from None

    def _verify_parameter_group_ownership(
        self, client, name: str, family: str, resource_name: str
    ) -> None:
        try:
            groups = client.describe_cache_parameter_groups(CacheParameterGroupName=name)
            found = groups.get("CacheParameterGroups") or []
            if len(found) != 1:
                raise ResourceError("aws_elasticache_parameter_group_ownership_mismatch")
            arn = found[0].get("ARN")
            tags = client.list_tags_for_resource(ResourceName=arn)
        except ResourceError:
            raise
        except Exception as exc:
            raise _provider_error(exc, "parameter_group_verify", self.error_prefix) from None
        if (
            found[0].get("CacheParameterGroupFamily") != family
            or _tags(tags).get("gimme:resource") != resource_name
        ):
            raise ResourceError("aws_elasticache_parameter_group_ownership_mismatch")

    def _ensure_parameter_group(
        self, client, name: str, family: str, resource_name: str
    ) -> None:
        tag = [{"Key": "gimme:resource", "Value": resource_name}]
        try:
            client.create_cache_parameter_group(
                CacheParameterGroupName=name, CacheParameterGroupFamily=family,
                Description=f"Gimme-managed parameter group for {resource_name}", Tags=tag,
            )
        except Exception as exc:
            error = _provider_error(exc, "parameter_group", self.error_prefix)
            if "already_exists" not in str(error):
                raise error from None
            self._verify_parameter_group_ownership(client, name, family, resource_name)
        try:
            # Applied even when the group already existed so a stale group converges.
            client.modify_cache_parameter_group(
                CacheParameterGroupName=name,
                ParameterNameValues=[
                    {"ParameterName": "cluster-enabled", "ParameterValue": "yes"},
                    {"ParameterName": "maxmemory-policy", "ParameterValue": "noeviction"},
                ],
            )
        except Exception as exc:
            raise _provider_error(exc, "parameter_group_modify", self.error_prefix) from None

    def _user_exists(self, client, user_id: str) -> bool:
        try:
            client.describe_users(UserId=user_id)
        except Exception as exc:
            error = _provider_error(exc, "user_describe", self.error_prefix)
            if "missing" in str(error):
                return False
            raise error from None
        return True

    def _ensure_authentication(
        self, account: AWSProviderAccount, client, store: AWSSecretsManagerStore,
        store_name: str, resource_name: str, group_id: str,
    ) -> str:
        tag = [{"Key": "gimme:resource", "Value": resource_name}]
        default_id, admin_id = _derived(group_id, "default"), _derived(group_id, "admin")
        self._tolerate_existing("user_create", lambda: client.create_user(
            UserId=default_id, UserName="default", Engine="valkey",
            AccessString=DEFAULT_ACCESS_STRING, NoPasswordRequired=True, Tags=tag,
        ))
        # The password only ever exists here: it is written to the Secret Store and sent to
        # ElastiCache, never returned. An existing user keeps the credential already stored.
        if not self._user_exists(client, admin_id):
            password = secrets_module.token_urlsafe(36)
            self.create_workload_secret(
                account, store, f"{resource_name}/admin",
                {"gimme:secret-store": store_name, "gimme:resource": resource_name},
                {"username": ADMIN_USER_NAME, "password": password},
            )
            try:
                client.create_user(
                    UserId=admin_id, UserName=ADMIN_USER_NAME, Engine="valkey",
                    AccessString=ADMIN_ACCESS_STRING, Passwords=[password], Tags=tag,
                )
            except Exception as exc:
                raise _provider_error(exc, "user_create", self.error_prefix) from None
        user_group = derive_user_group_id(group_id)
        self._tolerate_existing("user_group_create", lambda: client.create_user_group(
            UserGroupId=user_group, Engine="valkey", UserIds=[default_id, admin_id], Tags=tag,
        ))
        return user_group

    def create_group(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource: AWSElastiCacheValkeyResource, resource_name: str, group_id: str,
        store: AWSSecretsManagerStore, store_name: str,
    ) -> GroupObservation:
        client = self._client(account, network, "elasticache-create")
        tag = [{"Key": "gimme:resource", "Value": resource_name}]
        subnet_group, parameter_group = f"{group_id}-subnets", f"{group_id}-params"
        self._tolerate_existing("subnet_group", lambda: client.create_cache_subnet_group(
            CacheSubnetGroupName=subnet_group,
            CacheSubnetGroupDescription=f"Gimme-managed subnet group for {resource_name}",
            SubnetIds=network.private_subnet_ids, Tags=tag,
        ))
        self._ensure_parameter_group(
            client, parameter_group, _parameter_group_family(resource.engine_version),
            resource_name,
        )
        user_group = self._ensure_authentication(
            account, client, store, store_name, resource_name, group_id
        )
        try:
            client.create_replication_group(
                ReplicationGroupId=group_id,
                ReplicationGroupDescription=f"Gimme-managed Valkey for {resource_name}",
                Engine="valkey", EngineVersion=resource.engine_version,
                CacheNodeType=resource.node_type, CacheParameterGroupName=parameter_group,
                CacheSubnetGroupName=subnet_group, SecurityGroupIds=[resource.security_group_id],
                ClusterMode="enabled", NumNodeGroups=1, ReplicasPerNodeGroup=1,
                AutomaticFailoverEnabled=True, MultiAZEnabled=True,
                TransitEncryptionEnabled=True, TransitEncryptionMode="required",
                AtRestEncryptionEnabled=True, UserGroupIds=[user_group], Durability="sync",
                AutoMinorVersionUpgrade=False,
                SnapshotRetentionLimit=resource.snapshot_retention_days,
                SnapshotWindow=resource.snapshot_window,
                PreferredMaintenanceWindow=resource.maintenance_window,
                Port=PORT, Tags=tag,
            )
        except Exception as exc:
            raise _provider_error(exc, "create", self.error_prefix) from None
        observed = self.describe_group(account, network, group_id)
        if observed is None:
            raise ResourceError("aws_elasticache_group_missing_after_create")
        return observed


def structural_issues(
    resource: AWSElastiCacheValkeyResource, observed: GroupObservation, group_id: str
) -> list[str]:
    """Fixed codes for every part of the ADR 0009 contract an available group does not meet."""
    checks = {
        "cluster_mode": observed.cluster_enabled,
        "topology": observed.shards == 1 and observed.members == 2,
        "availability_zones": len(set(observed.member_zones)) == 2,
        "multi_az": observed.multi_az,
        "automatic_failover": observed.automatic_failover,
        "tls": observed.transit_encryption,
        "encryption_at_rest": observed.at_rest_encryption,
        "durability": observed.effective_durability == "sync",
        "authentication": observed.user_group_ids == (derive_user_group_id(group_id),),
        "snapshot_policy": (
            observed.snapshot_retention_days == resource.snapshot_retention_days
            and observed.snapshot_window == resource.snapshot_window
        ),
        "maintenance_policy": observed.maintenance_window == resource.maintenance_window,
        "automatic_minor_upgrade": observed.automatic_minor_upgrade is False,
    }
    return [code for code in ISSUES if not checks[code]]


def group_phase(observed: GroupObservation, issues: list[str]) -> str:
    if observed.status == "create-failed":
        return "failed"
    if observed.status != "available":
        return "pending"
    return "degraded" if issues else "ready"


def _validate_observed(document: object) -> dict[str, object]:
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "resource", "replication_group_id", "identity", "status", "phase",
        "engine_version", "effective_durability", "issues", "endpoint", "port",
    }:
        raise ResourceError("observed_resource_invalid")
    issues = document.get("issues")
    port = document.get("port")
    if (
        document.get("schema_version") != 1
        or RESOURCE_NAME.fullmatch(str(document.get("resource"))) is None
        or document.get("phase") not in GROUP_PHASE
        or not isinstance(issues, list) or any(item not in ISSUES for item in issues)
        or (port is not None and not isinstance(port, int))
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


def _group_document(
    resource_name: str, group_id: str, observed: GroupObservation, issues: list[str]
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "resource": resource_name,
        "replication_group_id": group_id,
        "identity": observed.identity,
        "status": observed.status,
        "phase": group_phase(observed, issues),
        "engine_version": observed.engine_version,
        "effective_durability": observed.effective_durability,
        "issues": issues,
        "endpoint": observed.endpoint,
        "port": observed.port,
    }


def apply_provision(
    adapter: ElastiCacheAdapter, root: Path, account: AWSProviderAccount, network: AWSNetwork,
    resource: AWSElastiCacheValkeyResource, resource_name: str,
    store: AWSSecretsManagerStore, store_name: str,
    *, sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Create the replication group if absent, then poll up to a bounded 30 seconds. A
    still-provisioning group is recorded as phase 'pending'; a later call resumes by
    describing rather than re-creating. An existing group is never modified here."""
    group_id = derive_group_id(resource_name)
    observed = adapter.describe_group(account, network, group_id)
    if observed is None:
        observed = adapter.create_group(
            account, network, resource, resource_name, group_id, store, store_name
        )
    deadline = now() + POLL_BUDGET_SECONDS
    while observed.status not in ("available", "create-failed") and now() < deadline:
        sleep(POLL_INTERVAL_SECONDS)
        refreshed = adapter.describe_group(account, network, group_id)
        if refreshed is None:
            raise ResourceError("aws_elasticache_group_disappeared")
        observed = refreshed
    issues = structural_issues(resource, observed, group_id)
    document = _group_document(resource_name, group_id, observed, issues)
    _write_json(_observed_path(root, resource_name), _validate_observed(document), resource_name)
    return {
        "resource": resource_name, "status": observed.status, "phase": document["phase"],
        "engine_version": observed.engine_version,
        "effective_durability": observed.effective_durability, "issues": issues,
    }


def retain_group(root: Path, resource_name: str, aws_network: str) -> dict[str, object]:
    """Record a secret-free Retained Resource tombstone. Ordinary removal never deletes the
    replication group or its data; this is inventory only."""
    observed = load_observed(root, resource_name)
    tombstone = {
        "schema_version": 1,
        "resource": resource_name,
        "aws_network": aws_network,
        "replication_group_id": (
            observed["replication_group_id"] if observed is not None else None
        ),
        "identity": observed["identity"] if observed is not None else None,
    }
    _write_json(_tombstone_path(root, resource_name), tombstone, resource_name)
    return cast(dict[str, object], tombstone)
