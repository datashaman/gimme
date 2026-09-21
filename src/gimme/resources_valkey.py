from __future__ import annotations

import hashlib
import json
import re
import secrets as secrets_module
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable, NoReturn, Protocol, cast

from gimme.control import (
    AWSElastiCacheValkeyResource,
    AWSNetwork,
    AWSProviderAccount,
    AWSSecretsManagerStore,
    DEPLOYMENT_NAME,
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
    _version_tuple,
    _write_json,
)

ID_LIMIT = 40
GROUP_PHASE = ("pending", "restoring", "ready", "degraded", "failed")
# Fixed, secret-free reasons a group that AWS reports available is not ready.
ISSUES = (
    "cluster_mode", "topology", "availability_zones", "multi_az", "automatic_failover", "tls",
    "encryption_at_rest", "durability", "authentication", "snapshot_policy",
    "maintenance_policy", "automatic_minor_upgrade", "service_update_overdue", "security_group",
)
# The only ISSUES an apply can repair; every other live difference fails apply closed.
REPAIRABLE_ISSUES = ("snapshot_policy", "maintenance_policy", "service_update_overdue")
# Gimme never edits the security group: anything else that can reach the group is unsafe drift.
MODIFIABLE_FIELDS = frozenset({
    "EngineVersion", "CacheNodeType", "SnapshotRetentionLimit", "SnapshotWindow",
    "PreferredMaintenanceWindow",
})
# AWS documents Durability for these instance families only (ElastiCache User Guide,
# Durability > Limitations, read 2026-09-21) and has no per-node-type describe call, so this
# list is the compatibility gate. Widen it only when AWS's page does.
DURABLE_NODE_FAMILIES = frozenset({"r8g", "r7g", "r6g", "m8g", "m7g", "m6g", "c8gn", "c7gn"})
UPDATE_ACTIONS_DONE = ("complete", "not-applicable")
# Bounded, read-only CloudWatch inspection (AWS/ElastiCache, per member node): stable output
# key, metric name, and the limit above which a fixed warning code is reported. Metrics only
# warn: Gimme does no sizing, admission control, or scaling. DurabilityLag and the buffer count
# are always 0 for synchronous durability, so any non-zero value is worth a warning.
METRICS = (
    ("memory_usage_percent", "DatabaseMemoryUsagePercentage", 80, "metric_memory_high"),
    ("connections", "CurrConnections", None, None),
    ("evictions", "Evictions", 0, "metric_evictions"),
    ("replica_lag_seconds", "ReplicationLag", 5, "metric_replica_lag"),
    ("durability_lag_ms", "DurabilityLag", 0, "metric_durability_lag"),
    ("durability_rejections", "DurabilityBufferExceededErrorCount", 0,
     "metric_durability_rejections"),
    ("traffic_management_active", "TrafficManagementActive", 0, "metric_traffic_management"),
)
METRICS_WINDOW = timedelta(minutes=15)
METRICS_PERIOD_SECONDS = 300
METRICS_MAX_MEMBERS = 8
ENGINE_VERSION_FLOOR = 9
PORT = 6379
# The default user can never authenticate; the administrative identity is limited to
# Gimme-owned key and channel prefixes and a fixed maintenance command set.
DEFAULT_ACCESS_STRING = "off ~* -@all"
ADMIN_ACCESS_STRING = (
    "on ~{gimme:* &{gimme:* -@all +ping +info +get +set +del +exists +ttl +type +scan "
    "+dump +pexpiretime +eval"
)
ADMIN_USER_NAME = "gimme-admin"
# The Gimme-owned `laravel` ACL profile, version 1: only these commands, only on the
# Deployment's own key and channel namespace. Administrative, configuration, ACL, persistence,
# replication, flush, and keyspace-scanning commands are absent, so cross-prefix discovery is
# denied. Callers can never supply an access string.
LARAVEL_PROFILE = "laravel-v1"
LARAVEL_COMMANDS = (
    "+get", "+set", "+del", "+unlink", "+exists", "+type", "+ttl", "+pttl", "+expire",
    "+pexpire", "+expireat", "+pexpireat", "+persist", "+incr", "+decr", "+incrby", "+decrby",
    "+incrbyfloat", "+mget", "+mset", "+setex", "+psetex", "+setnx", "+getset", "+getdel",
    "+append", "+strlen", "+touch", "+hget", "+hset", "+hsetnx", "+hdel", "+hgetall",
    "+hincrby", "+hmget", "+hmset", "+hexists", "+hkeys", "+hvals", "+hlen", "+lpush", "+rpush",
    "+lpop", "+rpop", "+llen", "+lrange", "+lrem", "+lindex", "+ltrim", "+lset", "+blpop",
    "+brpop", "+sadd", "+srem", "+smembers", "+sismember", "+scard", "+spop", "+zadd", "+zrem",
    "+zrange", "+zrangebyscore", "+zrevrange", "+zrevrangebyscore", "+zcard", "+zscore",
    "+zcount", "+zincrby", "+zrank", "+zrevrank", "+zremrangebyscore", "+zremrangebyrank",
    "+eval", "+evalsha", "+script|load", "+script|exists", "+multi", "+exec", "+discard",
    "+watch", "+unwatch", "+publish", "+subscribe", "+unsubscribe", "+psubscribe",
    "+punsubscribe", "+ping", "+echo", "+auth", "+hello", "+time", "+client|setinfo",
    "+client|setname", "+cluster|slots", "+cluster|shards", "+cluster|nodes", "+cluster|info",
)
BINDING_STATUS = ("active",)


def validate_update(
    current: AWSElastiCacheValkeyResource, proposed: AWSElastiCacheValkeyResource,
    observed: dict[str, object] | None, bound_targets: set[str],
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


def derive_binding_user_id(group_id: str, deployment_name: str, generation: int = 1) -> str:
    """One opaque ElastiCache user id per (group, Deployment, credential generation): letters,
    digits, and hyphens, at most 40 characters, and never equal to the default or administrative
    user. Generation 1 is the id every binding made before rotation existed already has."""
    seed = f"{group_id}/{_checked_name(deployment_name)}"
    if generation > 1:
        seed += f"/g{generation}"
    return f"gimme-u-{hashlib.sha256(seed.encode()).hexdigest()[:24]}"


def binding_username(deployment_name: str, generation: int = 1) -> str:
    """ACL user names are unique within a user group, so a candidate generation needs its own."""
    suffix = "" if generation == 1 else f"-g{generation}"
    return f"gimme-{_checked_name(deployment_name)}{suffix}"


def _checked_name(deployment_name: str) -> str:
    # The name reaches an ACL string and a key pattern, so it is validated at every entry.
    if DEPLOYMENT_NAME.fullmatch(deployment_name) is None:
        raise ResourceError("deployment_name_invalid")
    return deployment_name


def laravel_access_string(deployment_name: str) -> str:
    _checked_name(deployment_name)
    namespace = f"{{gimme:{deployment_name}}}:*"
    return f"on ~{namespace} &{namespace} -@all {' '.join(LARAVEL_COMMANDS)}"


def namespace_prefixes(deployment_name: str, uses: list[str]) -> dict[str, str]:
    """Immutable, derived key namespaces sharing one hash tag, so multi-key Laravel and
    Horizon operations stay in a single cluster slot. Horizon accompanies queue."""
    tag = f"{{gimme:{_checked_name(deployment_name)}}}"
    prefixes = {use: f"{tag}:{use}:" for use in uses}
    if "queue" in uses:
        prefixes["horizon"] = f"{tag}:horizon:"
    return prefixes


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
    # What reaches the group on TCP 6379: None when not observed (the group is not available).
    security_group_ids: tuple[str, ...] | None = None
    ingress_sources: tuple[str, ...] | None = None
    # Values AWS has accepted but not finished applying count as applied, so a resumed apply
    # never re-sends them.
    pending_engine_version: str | None = None
    pending_node_type: str | None = None
    service_update_overdue: bool = False
    pending_service_updates: int = 0
    member_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValkeyOptions:
    engine_versions: tuple[str, ...]
    node_types: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotInfo:
    name: str
    source: str
    status: str
    created: str | None
    engine_version: str | None
    shards: int | None


class ElastiCacheAdapter(Protocol):
    def describe_group(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str
    ) -> GroupObservation | None: ...

    def create_group(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource: AWSElastiCacheValkeyResource, resource_name: str, group_id: str,
        store: AWSSecretsManagerStore, store_name: str,
        snapshot_name: str | None = None,
        restore_users: dict[str, tuple[str, int]] | None = None,
    ) -> GroupObservation | None: ...

    def allowed_node_types(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str
    ) -> frozenset[str]: ...

    def modify_group(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str,
        changes: dict[str, object],
    ) -> GroupObservation: ...

    def ensure_binding(
        self, account: AWSProviderAccount, network: AWSNetwork, store: AWSSecretsManagerStore,
        store_name: str, resource_name: str, group_id: str, deployment_name: str,
        keep_credential: bool, generation: int = 1,
    ) -> tuple[str, str, str] | None: ...

    def list_snapshots(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str
    ) -> list["SnapshotInfo"]: ...

    def delete_final_snapshot(
        self, account: AWSProviderAccount, network: AWSNetwork, snapshot_name: str
    ) -> bool: ...

    def delete_retained_secrets(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, store_name: str,
        resource_name: str, secret_names: list[str],
    ) -> int: ...

    def recent_metrics(
        self, account: AWSProviderAccount, network: AWSNetwork, member_ids: tuple[str, ...]
    ) -> dict[str, float | None]: ...

    def begin_rotation(
        self, account: AWSProviderAccount, network: AWSNetwork, store: AWSSecretsManagerStore,
        store_name: str, resource_name: str, group_id: str, deployment_name: str, generation: int,
    ) -> tuple[str, str, str]: ...

    def restore_credential(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, resource_name: str,
        deployment_name: str, expected_username: str,
    ) -> tuple[str, str]: ...

    def remove_user(
        self, account: AWSProviderAccount, network: AWSNetwork, resource_name: str, user_id: str,
    ) -> None: ...

    def delete_group(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str,
        final_snapshot: str,
    ) -> None: ...

    def delete_dependents(
        self, account: AWSProviderAccount, network: AWSNetwork, resource_name: str,
        group_id: str, user_ids: list[str],
    ) -> bool: ...


def durable_node_type(node_type: str) -> bool:
    parts = node_type.split(".")
    return len(parts) == 3 and parts[0] == "cache" and parts[1] in DURABLE_NODE_FAMILIES


def metric_warnings(metrics: dict[str, float | None]) -> list[str]:
    """Fixed warning codes for metrics above their limits. Never affects readiness."""
    return [
        code for key, _name, limit, code in METRICS
        if code is not None and limit is not None
        and (value := metrics.get(key)) is not None and value > limit
    ]


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
        self, session, region: str, client, response: dict[str, object], group_id: str
    ) -> GroupObservation:
        arn = response.get("ARN")
        status = response.get("Status")
        if not isinstance(arn, str) or not isinstance(status, str):
            raise ResourceError("aws_elasticache_group_identity_invalid")
        # ElastiCache rejects ListTagsForResource until a new replication group is available.
        # A pending group is only observed and never modified or destroyed, so defer ownership
        # verification until AWS makes that read possible.
        if status == "available":
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
        if not isinstance(member_ids, list):
            if status == "available":
                raise ResourceError("aws_elasticache_group_identity_invalid")
            member_ids = []
        # AWS omits member and node-group topology while a new group is creating and while
        # one is deleting. Topology is verified once the group reaches available; a pending
        # group is never modified or bound.
        if not isinstance(node_groups, list):
            if status == "available":
                raise ResourceError("aws_elasticache_group_identity_invalid")
            node_groups = []
        zones = tuple(sorted(
            member["PreferredAvailabilityZone"]
            for node_group in node_groups if isinstance(node_group, dict)
            for member in node_group.get("NodeGroupMembers") or []
            if isinstance(member, dict) and isinstance(member.get("PreferredAvailabilityZone"), str)
        ))
        cluster: dict[str, object] = {}
        # Only an available group can be degraded, so a polling describe skips this call.
        pending_updates, overdue = (
            self._service_updates(client, group_id) if status == "available" else (0, False)
        )
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
        pending = cluster.get("PendingModifiedValues")
        pending = pending if isinstance(pending, dict) else {}
        attached = cluster.get("SecurityGroups")
        group_ids = tuple(sorted(
            item["SecurityGroupId"] for item in attached or []
            if isinstance(item, dict) and isinstance(item.get("SecurityGroupId"), str)
        )) if isinstance(attached, list) else None
        sources = (
            self._ingress_sources(session, region, group_ids)
            if status == "available" and group_ids else None
        )
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
            security_group_ids=group_ids if sources is not None else None,
            ingress_sources=sources,
            pending_engine_version=_text(pending.get("EngineVersion")),
            pending_node_type=_text(pending.get("CacheNodeType")),
            service_update_overdue=overdue,
            pending_service_updates=pending_updates,
            member_ids=tuple(member for member in member_ids if isinstance(member, str)),
        )

    def _ingress_sources(
        self, session, region: str, group_ids: tuple[str, ...]
    ) -> tuple[str, ...]:
        """Every distinct source of an inbound rule that reaches TCP 6379 on these groups: a
        security group id, a CIDR, or a prefix list. Read only; Gimme never edits the group."""
        try:
            pages = session.client("ec2", region_name=region).get_paginator(
                "describe_security_group_rules"
            ).paginate(Filters=[{"Name": "group-id", "Values": list(group_ids)}])
            rules = [rule for page in pages for rule in page.get("SecurityGroupRules") or []]
        except Exception as exc:
            raise _provider_error(exc, "security_group", self.error_prefix) from None
        sources: set[str] = set()
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("IsEgress") is not False:
                continue
            low, high = rule.get("FromPort"), rule.get("ToPort")
            if rule.get("IpProtocol") != "-1" and not (
                rule.get("IpProtocol") == "tcp" and isinstance(low, int)
                and isinstance(high, int) and low <= PORT <= high
            ):
                continue
            referenced = rule.get("ReferencedGroupInfo")
            source = (
                referenced.get("GroupId") if isinstance(referenced, dict)
                else rule.get("CidrIpv4") or rule.get("CidrIpv6")
                or (f"pl:{rule['PrefixListId']}" if rule.get("PrefixListId") else None)
            )
            sources.add(source if isinstance(source, str) else "unknown")
        return tuple(sorted(sources))

    def _service_updates(self, client, group_id: str) -> tuple[int, bool]:
        """Unfinished service updates for one group, and whether one missed its recommended
        apply-by date. ponytail: one bounded page of 50 actions."""
        try:
            response = client.describe_update_actions(
                ReplicationGroupIds=[group_id], ServiceUpdateStatus=["available"], MaxRecords=50,
            )
        except Exception as exc:
            raise _provider_error(exc, "update_actions", self.error_prefix) from None
        pending = [
            action for action in response.get("UpdateActions") or []
            if isinstance(action, dict)
            and action.get("UpdateActionStatus") not in UPDATE_ACTIONS_DONE
        ]
        return len(pending), any(action.get("SlaMet") == "no" for action in pending)

    def recent_metrics(
        self, account: AWSProviderAccount, network: AWSNetwork, member_ids: tuple[str, ...]
    ) -> dict[str, float | None]:
        """Maximum of each fixed metric over the last 15 minutes across the group's nodes, or
        None without a datapoint. One bounded read through the inspection role; no caller input."""
        session = self._session(account, account.inspection_role_arn, "elasticache-metrics")
        client = session.client("cloudwatch", region_name=network.region)
        members = member_ids[:METRICS_MAX_MEMBERS]
        queries = [
            {
                "Id": f"m{index}n{node}",
                "MetricStat": {
                    "Metric": {
                        "Namespace": "AWS/ElastiCache", "MetricName": name,
                        "Dimensions": [{"Name": "CacheClusterId", "Value": member}],
                    },
                    "Period": METRICS_PERIOD_SECONDS, "Stat": "Maximum",
                },
            }
            for index, (_key, name, _limit, _code) in enumerate(METRICS)
            for node, member in enumerate(members)
        ]
        end = datetime.now(UTC)
        found: dict[str, list[float]] = {key: [] for key, *_ in METRICS}
        try:
            pages = client.get_paginator("get_metric_data").paginate(
                MetricDataQueries=queries, StartTime=end - METRICS_WINDOW, EndTime=end,
            ) if queries else []
            for page in pages:
                for result in page.get("MetricDataResults") or []:
                    identifier = str(result.get("Id"))
                    key = METRICS[int(identifier[1:].split("n")[0])][0]
                    found[key].extend(
                        float(value) for value in result.get("Values") or []
                        if isinstance(value, (int, float))
                    )
        except Exception as exc:
            raise _provider_error(exc, "metrics", self.error_prefix) from None
        return {key: max(values) if values else None for key, values in found.items()}

    def describe_group(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str
    ) -> GroupObservation | None:
        session = self._session(account, account.inspection_role_arn, "elasticache-inspect")
        client = session.client("elasticache", region_name=network.region)
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
        return self._observation(session, network.region, client, groups[0], group_id)

    def allowed_node_types(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str
    ) -> frozenset[str]:
        client = self._client(account, network, "elasticache-inspect")
        try:
            response = client.list_allowed_node_type_modifications(ReplicationGroupId=group_id)
        except Exception as exc:
            raise _provider_error(exc, "node_types", self.error_prefix) from None
        return frozenset(
            item for key in ("ScaleUpModifications", "ScaleDownModifications")
            for item in response.get(key) or [] if isinstance(item, str)
        )

    def modify_group(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str,
        changes: dict[str, object],
    ) -> GroupObservation:
        """One immediate modification of exactly the given fields."""
        if not changes or not set(changes) <= MODIFIABLE_FIELDS:
            raise ResourceError("aws_elasticache_modify_field_forbidden")
        client = self._client(account, network, "elasticache-modify")
        try:
            client.modify_replication_group(
                ReplicationGroupId=group_id, ApplyImmediately=True, **changes
            )
        except Exception as exc:
            raise _provider_error(exc, "modify", self.error_prefix) from None
        observed = self.describe_group(account, network, group_id)
        if observed is None:
            raise ResourceError("aws_elasticache_group_disappeared")
        return observed

    def live_options(self, account: AWSProviderAccount, network: AWSNetwork) -> ValkeyOptions:
        """Exact Valkey versions and cache node types the registered account offers in the
        network's region, limited to the families AWS documents as durable. ponytail: AWS
        exposes no per-network filter, so the node types are the region's reserved-node
        offerings; a listed type that still cannot run Durability=sync fails at create with a
        bounded error."""
        client = self._client(account, network, "elasticache-options")
        try:
            versions = client.get_paginator("describe_cache_engine_versions").paginate(
                Engine="valkey"
            )
            offerings = client.get_paginator("describe_reserved_cache_nodes_offerings").paginate()
            engine_versions = {
                item["EngineVersion"] for page in versions
                for item in page.get("CacheEngineVersions") or []
                if isinstance(item.get("EngineVersion"), str)
            }
            node_types = {
                item["CacheNodeType"] for page in offerings
                for item in page.get("ReservedCacheNodesOfferings") or []
                if isinstance(item.get("CacheNodeType"), str)
            }
        except Exception as exc:
            raise _provider_error(exc, "options", self.error_prefix) from None
        return ValkeyOptions(
            tuple(sorted(
                (v for v in engine_versions if _version_tuple(v)[:1] >= (ENGINE_VERSION_FLOOR,)),
                key=_version_tuple,
            )),
            tuple(sorted(t for t in node_types if durable_node_type(t))),
        )

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

    @staticmethod
    def _disabled_default_password() -> str:
        """Return an unpersisted password required by ElastiCache for its disabled default user.

        The ``default`` user has the ``off`` ACL, so this value cannot authenticate any
        client. It is deliberately generated for the create request only: it is never
        written to a Secret Store, observation, plan, log, or error.
        """
        return secrets_module.token_urlsafe(36)

    def _ensure_authentication(
        self, account: AWSProviderAccount, client, store: AWSSecretsManagerStore,
        store_name: str, resource_name: str, group_id: str,
    ) -> str:
        tag = [{"Key": "gimme:resource", "Value": resource_name}]
        default_id, admin_id = _derived(group_id, "default"), _derived(group_id, "admin")
        self._tolerate_existing("user_create", lambda: client.create_user(
            UserId=default_id, UserName="default", Engine="valkey",
            AccessString=DEFAULT_ACCESS_STRING,
            Passwords=[self._disabled_default_password()], Tags=tag,
        ))
        # The password only ever exists here: it is written to the Secret Store and sent to
        # ElastiCache, never returned. An existing user keeps the credential already stored.
        if not self._user_exists(client, admin_id):
            password = secrets_module.token_urlsafe(36)
            # Deployment names cannot start with an underscore, so a Deployment credential at
            # <resource>/<deployment> can never collide with this one.
            self.create_workload_secret(
                account, store, f"{resource_name}/_admin",
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

    def ensure_binding(
        self, account: AWSProviderAccount, network: AWSNetwork, store: AWSSecretsManagerStore,
        store_name: str, resource_name: str, group_id: str, deployment_name: str,
        keep_credential: bool, generation: int = 1,
    ) -> tuple[str, str, str] | None:
        """Create or converge this Deployment's ACL user and add it to the group's user group.
        Returns (user id, secret ARN, secret version) when a credential was written, and None
        when the recorded credential was kept. The password exists only here: it goes to the
        Secret Store and to ElastiCache, never back to the caller."""
        client = self._client(account, network, "elasticache-bind")
        tag = [
            {"Key": "gimme:resource", "Value": resource_name},
            {"Key": "gimme:deployment", "Value": deployment_name},
        ]
        user_id = derive_binding_user_id(group_id, deployment_name, generation)
        username = binding_username(deployment_name, generation)
        access = laravel_access_string(deployment_name)
        exists = self._user_exists(client, user_id)
        written: tuple[str, str, str] | None = None
        if not (exists and keep_credential):
            password = secrets_module.token_urlsafe(36)
            arn, version = self.create_workload_secret(
                account, store, f"{resource_name}/{deployment_name}",
                {"gimme:secret-store": store_name, "gimme:resource": resource_name,
                 "gimme:deployment": deployment_name},
                {"username": username, "password": password},
            )
            written = (user_id, arn, version)
            try:
                if exists:
                    client.modify_user(UserId=user_id, AccessString=access, Passwords=[password])
                else:
                    client.create_user(
                        UserId=user_id, UserName=username, Engine="valkey",
                        AccessString=access, Passwords=[password], Tags=tag,
                    )
            except Exception as exc:
                raise _provider_error(exc, "user_bind", self.error_prefix) from None
        else:
            try:
                client.modify_user(UserId=user_id, AccessString=access)
            except Exception as exc:
                raise _provider_error(exc, "user_bind", self.error_prefix) from None
        user_group = derive_user_group_id(group_id)
        try:
            members = client.describe_user_groups(UserGroupId=user_group).get("UserGroups") or []
            if not any(user_id in (group.get("UserIds") or []) for group in members):
                client.modify_user_group(UserGroupId=user_group, UserIdsToAdd=[user_id])
        except Exception as exc:
            raise _provider_error(exc, "user_group_bind", self.error_prefix) from None
        return written

    def _destructive_client(self, account: AWSProviderAccount, network: AWSNetwork):
        """The only place the destructive role is assumed, and only while applying."""
        if account.destructive_role_arn is None:
            raise ResourceError(f"{self.error_prefix}_destroy_role_missing")
        session = self._session(account, account.destructive_role_arn, "elasticache-destroy")
        return session.client("elasticache", region_name=network.region)

    def delete_group(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str,
        final_snapshot: str,
    ) -> None:
        client = self._destructive_client(account, network)
        try:
            client.delete_replication_group(
                ReplicationGroupId=group_id, FinalSnapshotIdentifier=final_snapshot,
            )
        except Exception as exc:
            raise _provider_error(exc, "destroy_group", self.error_prefix) from None

    def delete_dependents(
        self, account: AWSProviderAccount, network: AWSNetwork, resource_name: str,
        group_id: str, user_ids: list[str],
    ) -> bool:
        """Delete what Gimme created around a group that is gone, in dependency order. Each
        object's ownership tag is read (with the inspection role) before it is deleted, an
        already-missing object counts as done, and any other doubt stops the sequence. Returns
        False only while AWS is still deleting the user group that this call just removed."""
        client = self._destructive_client(account, network)
        reader = self._client(account, network, "elasticache-destroy-verify")
        self._delete_owned(
            account, network, reader, resource_name, "usergroup", derive_user_group_id(group_id),
            client.delete_user_group, "UserGroupId", "destroy",
        )
        for user_id in user_ids:
            try:
                self._delete_owned(
                    account, network, reader, resource_name, "user", user_id,
                    client.delete_user, "UserId", "destroy",
                )
            except ResourceError as exc:
                # A successful DeleteUserGroup is asynchronous. ElastiCache rejects user
                # deletion until it has finished, so preserve the marker and let the same
                # confirmed call resume after the group is gone.
                if str(exc) == "aws_elasticache_destroy_delete_invalid_state":
                    return False
                raise
        steps = [
            ("parametergroup", f"{group_id}-params", client.delete_cache_parameter_group,
             "CacheParameterGroupName"),
            ("subnetgroup", f"{group_id}-subnets", client.delete_cache_subnet_group,
             "CacheSubnetGroupName"),
        ]
        for kind, object_id, delete, argument in steps:
            self._delete_owned(
                account, network, reader, resource_name, kind, object_id, delete, argument,
                "destroy",
            )
        return True

    def _delete_owned(
        self, account: AWSProviderAccount, network: AWSNetwork, reader, resource_name: str,
        kind: str, object_id: str, delete: Callable[..., object], argument: str, operation: str,
    ) -> None:
        """Read the object's ownership tag with the inspection role, and only then delete it with
        the destructive one. Already gone counts as done; anything else stops."""
        arn = f"arn:aws:elasticache:{network.region}:{account.account_id}:{kind}:{object_id}"
        try:
            tags = reader.list_tags_for_resource(ResourceName=arn)
        except Exception as exc:
            error = _provider_error(exc, f"{operation}_verify", self.error_prefix)
            if "missing" in str(error):
                return
            raise error from None
        if _tags(tags).get("gimme:resource") != resource_name:
            raise ResourceError(f"aws_elasticache_{operation}_ownership_mismatch")
        try:
            delete(**{argument: object_id})
        except Exception as exc:
            error = _provider_error(exc, f"{operation}_delete", self.error_prefix)
            if "missing" not in str(error):
                raise error from None

    def remove_user(
        self, account: AWSProviderAccount, network: AWSNetwork, resource_name: str, user_id: str,
    ) -> None:
        """Delete one ACL user this Resource owns, with the destructive role."""
        client = self._destructive_client(account, network)
        reader = self._client(account, network, "elasticache-user-verify")
        self._delete_owned(
            account, network, reader, resource_name, "user", user_id, client.delete_user,
            "UserId", "rotate",
        )

    def list_snapshots(
        self, account: AWSProviderAccount, network: AWSNetwork, group_id: str
    ) -> list[SnapshotInfo]:
        """Snapshots whose source group is this Resource's, including the final snapshot of a
        destroyed group. At most 200, newest first."""
        client = self._client(account, network, "elasticache-snapshots")
        found: list[SnapshotInfo] = []
        marker: str | None = None
        for _ in range(4):
            try:
                response = client.describe_snapshots(
                    ReplicationGroupId=group_id, MaxRecords=50,
                    **({"Marker": marker} if marker else {}),
                )
            except Exception as exc:
                raise _provider_error(exc, "snapshots", self.error_prefix) from None
            for item in response.get("Snapshots") or []:
                nodes = item.get("NodeSnapshots") or []
                created = nodes[0].get("SnapshotCreateTime") if nodes else None
                name = item.get("SnapshotName")
                if item.get("ReplicationGroupId") != group_id or not isinstance(name, str):
                    continue
                shards = item.get("NumNodeGroups")
                found.append(SnapshotInfo(
                    name=name, source=str(item.get("SnapshotSource") or "unknown"),
                    status=str(item.get("SnapshotStatus") or "unknown"),
                    created=created.isoformat() if hasattr(created, "isoformat") else None,
                    engine_version=_text(item.get("EngineVersion")),
                    shards=shards if isinstance(shards, int) else None,
                ))
            marker = response.get("Marker")
            if not marker:
                break
        return sorted(found, key=lambda info: (info.created or "", info.name), reverse=True)

    def delete_final_snapshot(
        self, account: AWSProviderAccount, network: AWSNetwork, snapshot_name: str
    ) -> bool:
        """Delete one final snapshot named in Gimme's immutable destruction receipt."""
        client = self._destructive_client(account, network)
        try:
            client.delete_snapshot(SnapshotName=snapshot_name)
        except Exception as exc:
            error = _provider_error(exc, "final_snapshot_delete", self.error_prefix)
            if "missing" in str(error):
                return False
            raise error from None
        return True

    def delete_retained_secrets(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, store_name: str,
        resource_name: str, secret_names: list[str],
    ) -> int:
        """Force-delete only exact, receipt-recorded secrets after verifying Gimme ownership."""
        reader = self._session(account, account.inspection_role_arn, "elasticache-secret-verify")
        verify = reader.client("secretsmanager", region_name=store.region)
        destroy = self._session(account, account.destructive_role_arn, "elasticache-secret-destroy")
        killer = destroy.client("secretsmanager", region_name=store.region)
        deleted = 0
        for name in secret_names:
            secret_id = f"{store.prefix}/{resource_name}/{name}"
            try:
                described = verify.describe_secret(SecretId=secret_id)
            except Exception as exc:
                error = _provider_error(exc, "secret_verify", self.error_prefix)
                if "missing" in str(error):
                    continue
                raise error from None
            tags = _tags(described)
            if (
                tags.get("gimme:resource") != resource_name
                or tags.get("gimme:secret-store") != store_name
            ):
                raise ResourceError("aws_elasticache_destroy_secret_ownership_mismatch")
            try:
                killer.delete_secret(SecretId=secret_id, ForceDeleteWithoutRecovery=True)
            except Exception as exc:
                error = _provider_error(exc, "secret_delete", self.error_prefix)
                if "missing" in str(error):
                    continue
                raise error from None
            deleted += 1
        return deleted

    def _read_secret(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str,
        stage: str = "AWSCURRENT",
    ) -> dict[str, str]:
        """A workload credential, read with the resolver role. Held only in memory."""
        session = self._session(account, account.resolver_role_arn, "elasticache-credential")
        client = session.client("secretsmanager", region_name=store.region)
        try:
            response = client.get_secret_value(
                SecretId=f"{store.prefix}/{name}", VersionStage=stage
            )
            payload = json.loads(response["SecretString"])
        except Exception as exc:
            raise _provider_error(exc, "credential_read", self.error_prefix) from None
        if not isinstance(payload, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in payload.items()
        ):
            raise ResourceError("aws_elasticache_credential_read_invalid")
        return payload

    def resolve_admin_credential(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore,
        resource_name: str,
    ) -> dict[str, str]:
        """Resolve the fixed administrative capture identity immediately before use."""
        payload = self._read_secret(account, store, f"{resource_name}/_admin")
        if set(payload) != {"username", "password"} or payload.get("username") != (
            ADMIN_USER_NAME
        ):
            raise ResourceError("aws_elasticache_admin_credential_invalid")
        return payload

    def ensure_admin_capture_access(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource_name: str,
    ) -> None:
        """Converge an existing Resource's fixed admin ACL before bounded capture."""
        client = self._client(account, network, "elasticache-capture-access")
        try:
            client.modify_user(
                UserId=_derived(derive_group_id(resource_name), "admin"),
                AccessString=ADMIN_ACCESS_STRING,
            )
        except Exception as exc:
            raise _provider_error(exc, "capture_access", self.error_prefix) from None

    def begin_rotation(
        self, account: AWSProviderAccount, network: AWSNetwork, store: AWSSecretsManagerStore,
        store_name: str, resource_name: str, group_id: str, deployment_name: str, generation: int,
    ) -> tuple[str, str, str]:
        """Create the candidate generation's ACL user and make its credential the secret's
        current version, keeping the prior version and the prior user. The user exists before
        the secret points at it. Returns (user id, secret ARN, secret version)."""
        client = self._client(account, network, "elasticache-rotate")
        user_id = derive_binding_user_id(group_id, deployment_name, generation)
        if self._user_exists(client, user_id):
            raise ResourceError("aws_elasticache_rotate_candidate_exists")
        password = secrets_module.token_urlsafe(36)
        username = binding_username(deployment_name, generation)
        try:
            client.create_user(
                UserId=user_id, UserName=username, Engine="valkey",
                AccessString=laravel_access_string(deployment_name), Passwords=[password],
                Tags=[
                    {"Key": "gimme:resource", "Value": resource_name},
                    {"Key": "gimme:deployment", "Value": deployment_name},
                ],
            )
            client.modify_user_group(
                UserGroupId=derive_user_group_id(group_id), UserIdsToAdd=[user_id]
            )
        except Exception as exc:
            raise _provider_error(exc, "rotate_user", self.error_prefix) from None
        arn, version = self.create_workload_secret(
            account, store, f"{resource_name}/{deployment_name}",
            {"gimme:secret-store": store_name, "gimme:resource": resource_name,
             "gimme:deployment": deployment_name},
            {"username": username, "password": password},
        )
        return user_id, arn, version

    def restore_credential(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, resource_name: str,
        deployment_name: str, expected_username: str,
    ) -> tuple[str, str]:
        """Make the credential for `expected_username` the secret's current version again,
        idempotently: nothing is written when it already is. Returns (ARN, version)."""
        name = f"{resource_name}/{deployment_name}"
        if self._read_secret(account, store, name).get("username") == expected_username:
            return self._current_secret(account, store, name)
        previous = self._read_secret(account, store, name, "AWSPREVIOUS")
        if previous.get("username") != expected_username:
            raise ResourceError("aws_elasticache_rotate_previous_credential_missing")
        return self.create_workload_secret(
            account, store, name,
            {"gimme:resource": resource_name, "gimme:deployment": deployment_name}, previous,
        )

    def _current_secret(
        self, account: AWSProviderAccount, store: AWSSecretsManagerStore, name: str
    ) -> tuple[str, str]:
        session = self._session(account, account.inspection_role_arn, "elasticache-secret-meta")
        client = session.client("secretsmanager", region_name=store.region)
        try:
            described = client.describe_secret(SecretId=f"{store.prefix}/{name}")
            versions = described.get("VersionIdsToStages") or {}
            version = next(v for v, stages in versions.items() if "AWSCURRENT" in stages)
            return str(described["ARN"]), str(version)
        except Exception as exc:
            raise _provider_error(exc, "credential_read", self.error_prefix) from None

    def restore_authentication(
        self, account: AWSProviderAccount, client, store: AWSSecretsManagerStore,
        resource_name: str, group_id: str, users: dict[str, tuple[str, int]],
    ) -> str:
        """Recreate the user group and every ACL user that is missing, from the credentials
        already in the Secret Store. Existing users keep their passwords, and nothing here
        generates or writes a credential, so a restore never rotates one."""
        tag = [{"Key": "gimme:resource", "Value": resource_name}]
        default_id, admin_id = _derived(group_id, "default"), _derived(group_id, "admin")
        self._tolerate_existing("user_create", lambda: client.create_user(
            UserId=default_id, UserName="default", Engine="valkey",
            AccessString=DEFAULT_ACCESS_STRING,
            Passwords=[self._disabled_default_password()], Tags=tag,
        ))
        if not self._user_exists(client, admin_id):
            password = self._read_secret(account, store, f"{resource_name}/_admin")["password"]
            try:
                client.create_user(
                    UserId=admin_id, UserName=ADMIN_USER_NAME, Engine="valkey",
                    AccessString=ADMIN_ACCESS_STRING, Passwords=[password], Tags=tag,
                )
            except Exception as exc:
                raise _provider_error(exc, "user_create", self.error_prefix) from None
        for deployment_name, (user_id, generation) in sorted(users.items()):
            access = laravel_access_string(deployment_name)
            try:
                if self._user_exists(client, user_id):
                    client.modify_user(UserId=user_id, AccessString=access)
                    continue
                credential = self._read_secret(
                    account, store, f"{resource_name}/{deployment_name}"
                )
                if credential.get("username") != binding_username(deployment_name, generation):
                    raise ResourceError("aws_elasticache_restore_credential_mismatch")
                client.create_user(
                    UserId=user_id, UserName=credential["username"], Engine="valkey",
                    AccessString=access, Passwords=[credential["password"]],
                    Tags=[*tag, {"Key": "gimme:deployment", "Value": deployment_name}],
                )
            except ResourceError:
                raise
            except Exception as exc:
                raise _provider_error(exc, "user_restore", self.error_prefix) from None
        user_group = derive_user_group_id(group_id)
        members = [default_id, admin_id, *(user_id for user_id, _g in users.values())]
        self._tolerate_existing("user_group_create", lambda: client.create_user_group(
            UserGroupId=user_group, Engine="valkey", UserIds=[default_id, admin_id], Tags=tag,
        ))
        try:
            groups = client.describe_user_groups(UserGroupId=user_group).get("UserGroups") or []
            present = {user for group in groups for user in group.get("UserIds") or []}
            missing = [user for user in members if user not in present]
            if missing:
                client.modify_user_group(UserGroupId=user_group, UserIdsToAdd=missing)
        except Exception as exc:
            raise _provider_error(exc, "user_group_restore", self.error_prefix) from None
        return user_group

    def create_group(
        self, account: AWSProviderAccount, network: AWSNetwork,
        resource: AWSElastiCacheValkeyResource, resource_name: str, group_id: str,
        store: AWSSecretsManagerStore, store_name: str,
        snapshot_name: str | None = None,
        restore_users: dict[str, tuple[str, int]] | None = None,
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
        if restore_users is None:
            user_group = self._ensure_authentication(
                account, client, store, store_name, resource_name, group_id
            )
        else:
            user_group = self.restore_authentication(
                account, client, store, resource_name, group_id, restore_users
            )
        # A snapshot fixes the shard count, so it is not sent; everything durable is.
        restored = {"SnapshotName": snapshot_name} if snapshot_name else {}
        try:
            client.create_replication_group(
                ReplicationGroupId=group_id,
                ReplicationGroupDescription=f"Gimme-managed Valkey for {resource_name}",
                Engine="valkey", EngineVersion=resource.engine_version,
                CacheNodeType=resource.node_type, CacheParameterGroupName=parameter_group,
                CacheSubnetGroupName=subnet_group, SecurityGroupIds=[resource.security_group_id],
                ClusterMode="enabled", ReplicasPerNodeGroup=1,
                **({} if snapshot_name else {"NumNodeGroups": 1}), **restored,
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
        # A successful AWS response is the durable creation acknowledgement. Do not read the
        # group here: that read can fail transiently (including before tags are available), and
        # the caller records this acknowledgement before attempting a resumable observation.
        return None


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
        "service_update_overdue": not observed.service_update_overdue,
        "security_group": (
            observed.security_group_ids == (resource.security_group_id,)
            and set(observed.ingress_sources or ()) <= {
                resource.administration_security_group_id,
                *resource.deployment_security_group_ids.values(),
            }
        ),
    }
    return [code for code in ISSUES if not checks[code]]


def group_phase(observed: GroupObservation, issues: list[str]) -> str:
    if observed.status == "create-failed":
        return "failed"
    if (
        observed.status != "available"
        or observed.pending_engine_version or observed.pending_node_type
    ):
        return "pending"
    return "degraded" if issues else "ready"


def _version_order(desired: str, live: str) -> int:
    """1 when desired is newer than live, -1 when older, 0 when live is desired or a patch of
    it: a desired 9.0 is satisfied by a live 9.0.3, so a resumed apply never re-sends it."""
    wanted = _version_tuple(desired)
    running = _version_tuple(live)[:len(wanted)]
    return (wanted > running) - (wanted < running)


def modification_for(
    resource: AWSElastiCacheValkeyResource, live: GroupObservation, group_id: str
) -> dict[str, object]:
    """The exact modify_replication_group fields that bring an available group onto desired
    state, or {} when nothing differs. Values AWS already has pending count as applied.
    Refuses, before any call, a group that is outside the contract or the allowlist; a group
    with nothing to change is left alone and reported degraded instead."""
    changes: dict[str, object] = {}
    version = live.pending_engine_version or live.engine_version
    if version is not None:
        if version.split(".")[0] != resource.engine_version.split(".")[0]:
            raise ResourceError("aws_elasticache_modify_forbidden_engine_major")
        order = _version_order(resource.engine_version, version)
        if order < 0:
            raise ResourceError("aws_elasticache_modify_forbidden_engine_downgrade")
        if order > 0:
            changes["EngineVersion"] = resource.engine_version
    node_type = live.pending_node_type or live.node_type
    if node_type is not None and node_type != resource.node_type:
        changes["CacheNodeType"] = resource.node_type
    for field, desired, actual in (
        ("SnapshotRetentionLimit", resource.snapshot_retention_days, live.snapshot_retention_days),
        ("SnapshotWindow", resource.snapshot_window, live.snapshot_window),
        ("PreferredMaintenanceWindow", resource.maintenance_window, live.maintenance_window),
    ):
        if actual is not None and actual != desired:
            changes[field] = desired
    if changes:
        for code in structural_issues(resource, live, group_id):
            if code not in REPAIRABLE_ISSUES:
                raise ResourceError(f"aws_elasticache_modify_forbidden_{code}")
    return changes


def group_drift(
    resource: AWSElastiCacheValkeyResource, live: GroupObservation
) -> dict[str, object]:
    """Desired-versus-live differences for a successful live read; unobserved fields are
    skipped, and nothing here is persisted."""
    pairs = {
        "engine_version": (
            resource.engine_version, live.engine_version,
            live.engine_version is not None
            and _version_order(resource.engine_version, live.engine_version) != 0,
        ),
        "node_type": (resource.node_type, live.node_type, live.node_type != resource.node_type),
        "snapshot_retention_days": (
            resource.snapshot_retention_days, live.snapshot_retention_days,
            live.snapshot_retention_days != resource.snapshot_retention_days,
        ),
        "snapshot_window": (
            resource.snapshot_window, live.snapshot_window,
            live.snapshot_window != resource.snapshot_window,
        ),
        "maintenance_window": (
            resource.maintenance_window, live.maintenance_window,
            live.maintenance_window != resource.maintenance_window,
        ),
    }
    return {
        "fields": {
            field: {"desired": desired, "live": actual}
            for field, (desired, actual, differs) in pairs.items()
            if actual is not None and differs
        },
        "modification_pending": bool(
            live.status == "modifying" or live.pending_engine_version or live.pending_node_type
        ),
    }


def _validate_observed(document: object) -> dict[str, object]:
    if isinstance(document, dict) and "allocations" not in document:
        # A cache written before bindings existed: it is replaceable, so upgrade it in place.
        document = {**document, "allocations": {}}
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "resource", "replication_group_id", "identity", "status", "phase",
        "engine_version", "effective_durability", "issues", "endpoint", "port", "allocations",
    }:
        raise ResourceError("observed_resource_invalid")
    issues = document.get("issues")
    port = document.get("port")
    allocations = document.get("allocations")
    if (
        document.get("schema_version") != 1
        or RESOURCE_NAME.fullmatch(str(document.get("resource"))) is None
        or document.get("phase") not in GROUP_PHASE
        or not isinstance(issues, list) or any(item not in ISSUES for item in issues)
        or (port is not None and not isinstance(port, int))
        or not isinstance(allocations, dict) or any(
            DEPLOYMENT_NAME.fullmatch(str(name)) is None or not isinstance(item, dict)
            or set(item) - {"generation"} != {
                "user_id", "secret_arn", "secret_version_id", "status"
            }
            or item["status"] not in BINDING_STATUS
            or not isinstance(item.get("generation", 1), int) or item.get("generation", 1) < 1
            for name, item in allocations.items()
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


def _group_document(
    resource_name: str, group_id: str, observed: GroupObservation, issues: list[str],
    allocations: dict[str, object],
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
        "allocations": allocations,
    }


def apply_provision(
    adapter: ElastiCacheAdapter, root: Path, account: AWSProviderAccount, network: AWSNetwork,
    resource: AWSElastiCacheValkeyResource, resource_name: str,
    store: AWSSecretsManagerStore, store_name: str,
    *, sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Create the replication group if absent, otherwise converge an available one with one
    immediate modification of only the fields that differ, then poll up to a bounded 30
    seconds. AWS's creation acknowledgement is recorded before any following observation, so
    a transient read failure cannot make a later call create a second group. A still-
    provisioning or still-modifying group is recorded as phase 'pending'; a later call
    resumes by describing, so it re-creates and re-sends nothing."""
    refuse_while_busy(root, resource_name, allow="provisioning")
    group_id = derive_group_id(resource_name)
    previous = load_observed(root, resource_name)
    provisioning = read_marker(root, "provisioning", resource_name)
    creation_pending = provisioning is not None
    allocations = dict(cast(dict[str, object], previous["allocations"])) if previous else {}
    modified_fields: list[str] = []
    deadline = now() + POLL_BUDGET_SECONDS

    def settle(observed: GroupObservation) -> GroupObservation:
        while observed.status not in ("available", "create-failed") and now() < deadline:
            sleep(POLL_INTERVAL_SECONDS)
            refreshed = adapter.describe_group(account, network, group_id)
            if refreshed is None:
                raise ResourceError("aws_elasticache_group_disappeared")
            observed = refreshed
        return observed

    observed = adapter.describe_group(account, network, group_id)
    if observed is None:
        if provisioning is not None:
            raise ResourceError("aws_elasticache_group_missing_after_create")
        if previous is not None:
            # The group existed and is gone. Provisioning would silently create an empty one.
            raise ResourceError("aws_elasticache_group_missing_replace_explicitly")
        created = adapter.create_group(
            account, network, resource, resource_name, group_id, store, store_name
        )
        write_marker(
            root, "provisioning", resource_name,
            {"schema_version": 1, "resource": resource_name, "phase": "creating"},
        )
        creation_pending = True
        observed = created
        if observed is None:
            observed = adapter.describe_group(account, network, group_id)
            if observed is None:
                raise ResourceError("aws_elasticache_group_missing_after_create")
    else:
        # Diff against the settled group, so a modification already under way is not repeated.
        observed = settle(observed)
        if observed.status == "available":
            changes = modification_for(resource, observed, group_id)
            if "CacheNodeType" in changes and resource.node_type not in adapter.allowed_node_types(
                account, network, group_id
            ):
                raise ResourceError("aws_elasticache_modify_forbidden_node_type")
            if changes:
                observed = adapter.modify_group(account, network, group_id, changes)
                modified_fields = sorted(changes)
    observed = settle(observed)
    issues = structural_issues(resource, observed, group_id)
    document = _group_document(resource_name, group_id, observed, issues, allocations)
    _write_json(_observed_path(root, resource_name), _validate_observed(document), resource_name)
    if creation_pending:
        if document["phase"] == "pending":
            write_marker(
                root, "provisioning", resource_name,
                {"schema_version": 1, "resource": resource_name, "phase": observed.status},
            )
        else:
            clear_marker(root, "provisioning", resource_name)
    return {
        "resource": resource_name, "status": observed.status, "phase": document["phase"],
        "engine_version": observed.engine_version,
        "effective_durability": observed.effective_durability, "issues": issues,
        "modified_fields": modified_fields,
    }


def apply_binding(
    adapter: ElastiCacheAdapter, root: Path, account: AWSProviderAccount, network: AWSNetwork,
    resource: AWSElastiCacheValkeyResource, resource_name: str,
    store: AWSSecretsManagerStore, store_name: str, deployment_name: str, uses: list[str],
) -> dict[str, object]:
    """Give one Deployment its own ACL user, namespace, and Resource Credential. A group that
    is not ready by a fresh live read, degraded included, takes no new binding. An existing
    allocation keeps its credential; a user with no recorded allocation gets a new one.
    Never returns the username or password."""
    _checked_name(deployment_name)
    refuse_while_busy(root, resource_name)
    document = load_observed(root, resource_name)
    group_id = derive_group_id(resource_name)
    live = adapter.describe_group(account, network, group_id)
    if document is None or live is None:
        raise ResourceError("aws_elasticache_binding_resource_not_ready")
    if group_phase(live, structural_issues(resource, live, group_id)) != "ready":
        raise ResourceError("aws_elasticache_binding_resource_not_ready")
    allocations = dict(cast(dict[str, object], document["allocations"]))
    existing = cast(dict[str, dict[str, object]], allocations).get(deployment_name)
    generation = int(cast(int, existing.get("generation", 1))) if existing else 1
    written = adapter.ensure_binding(
        account, network, store, store_name, resource_name, group_id, deployment_name,
        keep_credential=deployment_name in allocations, generation=generation,
    )
    if written is not None:
        user_id, secret_arn, version_id = written
        allocations[deployment_name] = {
            "user_id": user_id, "secret_arn": secret_arn, "secret_version_id": version_id,
            "status": "active", **({"generation": generation} if generation > 1 else {}),
        }
        _write_json(
            _observed_path(root, resource_name),
            _validate_observed({**document, "allocations": allocations}), resource_name,
        )
    return {
        "deployment": deployment_name, "resource": resource_name,
        "namespaces": namespace_prefixes(deployment_name, uses),
        "secret_reference": {"store": store_name, "secret": f"{resource_name}/{deployment_name}"},
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
    # Retaining abandons anything half-finished, so a later Resource of this name is free.
    for kind in MARKERS:
        clear_marker(root, kind, resource_name)
    return cast(dict[str, object], tombstone)


# One in-progress operation per Resource is recorded as a local marker, and every other
# operation that could change the group refuses while any marker exists.
MARKERS = {
    "provisioning": "aws_elasticache_provision_in_progress",
    "destroying": "aws_elasticache_destroy_in_progress",
    "restoring": "aws_elasticache_restore_in_progress",
    "rotating": "aws_elasticache_rotate_in_progress",
}


def _marker_path(root: Path, kind: str, resource_name: str) -> Path:
    if RESOURCE_NAME.fullmatch(resource_name) is None or kind not in MARKERS:
        raise ResourceError("resource_name_invalid")
    return root / f"{kind}-resources" / f"{resource_name}.json"


def read_marker(root: Path, kind: str, resource_name: str) -> dict[str, object] | None:
    path = _marker_path(root, kind, resource_name)
    if not path.is_file() or path.is_symlink():
        return None
    try:
        document = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ResourceError(f"aws_elasticache_{kind}_marker_invalid") from None
    if not isinstance(document, dict):
        raise ResourceError(f"aws_elasticache_{kind}_marker_invalid")
    return document


def write_marker(
    root: Path, kind: str, resource_name: str, document: dict[str, object]
) -> None:
    _write_json(_marker_path(root, kind, resource_name), document, resource_name)


def clear_marker(root: Path, kind: str, resource_name: str) -> None:
    _marker_path(root, kind, resource_name).unlink(missing_ok=True)


def busy_operation(root: Path, resource_name: str) -> str | None:
    """The operation a marker records as in progress on this Resource, if any."""
    return next(
        (kind for kind in MARKERS if _marker_path(root, kind, resource_name).is_file()), None
    )


def operation_progress(root: Path, resource_name: str, kind: str) -> dict[str, object]:
    """Registered names only, for `inspect_resource`: which Deployments have been verified or
    have failed, so a repeat can be aimed."""
    try:
        marker = read_marker(root, kind, resource_name) or {}
    except ResourceError:
        return {}
    return {
        key: marker[key] for key in ("verified", "failed", "deployment", "phase") if key in marker
    }


def refuse_while_busy(root: Path, resource_name: str, *, allow: str | None = None) -> None:
    for kind, code in MARKERS.items():
        if kind != allow and _marker_path(root, kind, resource_name).is_file():
            raise ResourceError(code)


def identity_fingerprint(identity: str) -> str:
    return hashlib.sha256(identity.encode()).hexdigest()[:16]


def final_snapshot_id(group_id: str, fingerprint: str) -> str:
    """Deterministic per group incarnation, so a retried delete never makes a second snapshot
    and a reused name fails closed as `snapshot_exists`."""
    return f"{group_id}-final-{fingerprint[:8]}"


def _destroyed_receipt_path(root: Path, resource_name: str) -> Path:
    if RESOURCE_NAME.fullmatch(resource_name) is None:
        raise ResourceError("resource_name_invalid")
    return root / "destroyed-resources" / f"{resource_name}.json"


def record_destroyed_group(
    root: Path, resource_name: str, aws_network: str, fingerprint: str
) -> dict[str, object]:
    """Keep only the exact final snapshot identity after destructive cleanup completes."""
    group_id = derive_group_id(resource_name)
    receipt = {
        "schema_version": 1, "resource": resource_name, "aws_network": aws_network,
        "replication_group_id": group_id, "identity_fingerprint": fingerprint,
        "final_snapshot": final_snapshot_id(group_id, fingerprint),
    }
    _write_json(_destroyed_receipt_path(root, resource_name), receipt, resource_name)
    return receipt


def load_destroyed_receipt(root: Path, resource_name: str) -> dict[str, str] | None:
    path = _destroyed_receipt_path(root, resource_name)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_OBSERVED_BYTES:
        return None
    try:
        receipt = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ResourceError("aws_elasticache_destroy_receipt_invalid") from None
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version", "resource", "aws_network", "replication_group_id",
        "identity_fingerprint", "final_snapshot",
    } or receipt.get("schema_version") != 1 or any(
        not isinstance(receipt.get(key), str)
        for key in ("resource", "aws_network", "replication_group_id", "identity_fingerprint",
                    "final_snapshot")
    ):
        raise ResourceError("aws_elasticache_destroy_receipt_invalid")
    group_id = derive_group_id(resource_name)
    if receipt["resource"] != resource_name or receipt["replication_group_id"] != group_id or (
        receipt["final_snapshot"] != final_snapshot_id(group_id, receipt["identity_fingerprint"])
    ):
        raise ResourceError("aws_elasticache_destroy_receipt_invalid")
    return cast(dict[str, str], receipt)


def clear_destroyed_receipt(root: Path, resource_name: str) -> None:
    _destroyed_receipt_path(root, resource_name).unlink(missing_ok=True)


def _destroyed_secrets_receipt_path(root: Path, resource_name: str) -> Path:
    if RESOURCE_NAME.fullmatch(resource_name) is None:
        raise ResourceError("resource_name_invalid")
    return root / "destroyed-secret-resources" / f"{resource_name}.json"


def record_destroyed_secrets(
    root: Path, resource_name: str, store_name: str, secret_names: list[str]
) -> None:
    if any(DEPLOYMENT_NAME.fullmatch(name) is None and name != "_admin" for name in secret_names):
        raise ResourceError("aws_elasticache_destroy_receipt_invalid")
    _write_json(_destroyed_secrets_receipt_path(root, resource_name), {
        "schema_version": 1, "resource": resource_name, "store": store_name,
        "secrets": sorted(set(secret_names)),
    }, resource_name)


def load_destroyed_secrets(root: Path, resource_name: str) -> tuple[str, list[str]] | None:
    path = _destroyed_secrets_receipt_path(root, resource_name)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_OBSERVED_BYTES:
        return None
    try:
        receipt = json.loads(path.read_text())
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ResourceError("aws_elasticache_destroy_receipt_invalid") from None
    if not isinstance(receipt, dict) or set(receipt) != {
        "schema_version", "resource", "store", "secrets"
    } or (
        receipt.get("schema_version") != 1 or receipt.get("resource") != resource_name
        or not isinstance(receipt.get("store"), str)
        or not isinstance(receipt.get("secrets"), list)
        or any(
            not isinstance(name, str)
            or (name != "_admin" and DEPLOYMENT_NAME.fullmatch(name) is None)
            for name in receipt["secrets"]
        )
    ):
        raise ResourceError("aws_elasticache_destroy_receipt_invalid")
    return cast(str, receipt["store"]), sorted(set(cast(list[str], receipt["secrets"])))


def clear_destroyed_secrets(root: Path, resource_name: str) -> None:
    _destroyed_secrets_receipt_path(root, resource_name).unlink(missing_ok=True)


def destruction_targets(root: Path, resource_name: str) -> tuple[str, list[str]]:
    """The observed identity fingerprint and every ElastiCache user Gimme created: the default
    and administrative users and one per recorded allocation."""
    observed = load_observed(root, resource_name)
    if observed is None:
        raise ResourceError("aws_elasticache_destroy_not_observed")
    group_id = derive_group_id(resource_name)
    allocations = cast(dict[str, dict[str, str]], observed["allocations"])
    users = [
        _derived(group_id, "default"), _derived(group_id, "admin"),
        *sorted(item["user_id"] for item in allocations.values()),
    ]
    return identity_fingerprint(str(observed["identity"])), users


def apply_destroy(
    adapter: ElastiCacheAdapter, root: Path, account: AWSProviderAccount, network: AWSNetwork,
    resource_name: str, expected_fingerprint: str,
    *, sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic,
) -> dict[str, object]:
    """Delete the replication group with a final snapshot, then what Gimme created around it.
    Resumable: a group that is still deleting after a bounded 30 seconds returns phase
    'deleting', and a later call continues. The group must still be the one that was planned
    (identity and ownership tags); anything else fails closed before any deletion. Workload
    secrets and the final snapshot are retained."""
    refuse_while_busy(root, resource_name, allow="destroying")
    fingerprint, user_ids = destruction_targets(root, resource_name)
    if fingerprint != expected_fingerprint:
        raise ResourceError("aws_elasticache_destroy_identity_changed")
    group_id = derive_group_id(resource_name)
    snapshot = final_snapshot_id(group_id, fingerprint)
    live = adapter.describe_group(account, network, group_id)
    marker = {"schema_version": 1, "resource": resource_name, "phase": "deleting"}
    if live is not None:
        if identity_fingerprint(live.identity) != fingerprint:
            raise ResourceError("aws_elasticache_destroy_identity_changed")
        if live.status not in ("available", "deleting"):
            raise ResourceError("aws_elasticache_destroy_invalid_state")
        write_marker(root, "destroying", resource_name, marker)
        if live.status == "available":
            adapter.delete_group(account, network, group_id, snapshot)
        deadline = now() + POLL_BUDGET_SECONDS
        while live is not None and now() < deadline:
            sleep(POLL_INTERVAL_SECONDS)
            live = adapter.describe_group(account, network, group_id)
        if live is not None:
            return {
                "resource": resource_name, "phase": "deleting", "destroyed": False,
                "final_snapshot": snapshot,
            }
    marker["phase"] = "cleaning"
    write_marker(root, "destroying", resource_name, marker)
    if adapter.delete_dependents(account, network, resource_name, group_id, user_ids) is False:
        marker["phase"] = "waiting_for_user_group"
        write_marker(root, "destroying", resource_name, marker)
        return {
            "resource": resource_name, "phase": "waiting_for_user_group", "destroyed": False,
            "final_snapshot": snapshot,
        }
    _observed_path(root, resource_name).unlink(missing_ok=True)
    clear_marker(root, "destroying", resource_name)
    return {
        "resource": resource_name, "phase": "destroyed", "destroyed": True,
        "final_snapshot": snapshot,
    }
