# Use AWS ElastiCache for Valkey as a managed Resource

[ADR 0009](../adr/0009-synchronously-durable-aws-valkey.md) describes managed Valkey on AWS
ElastiCache: one shard with one cross-AZ replica, cluster mode, TLS, encryption at rest, and
synchronous durability. This page describes what is implemented today. The provider is not
finished: see the list below.

## Current scope

Implemented: registering the Resource, provisioning one replication group (with its subnet
group, parameter group, user group, and administrative user), reviewed updates and drift,
live inspection with fixed readiness codes, typed Deployment bindings with a per-Deployment ACL
user, namespace, and credential, and retention-by-default removal.

Not implemented yet (tracked in #14): the Laravel application contract (a bound Deployment does
not yet receive `GIMME_VALKEY_*` values or activation probes), destruction, snapshots and
restore, and credential rotation.

The AWS calls have only been exercised against botocore stubs. Nothing here has run against a
live account, so the IAM statement and the ACL access strings below are unverified.

## Prerequisites

Gimme creates only the replication group, its cache subnet group, cache parameter group, user
group, users, and the administrative user's secret. Everything else must already exist and is
never edited:

- an AWS Network (a VPC and exactly two private subnets in different Availability Zones), a
  Provider Account, and an AWS Secrets Manager Secret Store, exactly as in
  [`use-aws-rds-postgresql.md`](use-aws-rds-postgresql.md);
- one security group in that VPC for the Valkey nodes. Gimme attaches it and never edits it, so
  allow TCP 6379 yourself from the administration Target's security group and from the security
  group of each Deployment Target that will bind the Resource, and from nothing else (see
  [Security group](#security-group));
- an administration Target (`"role": "administration"`);
- the ElastiCache service-linked role. AWS creates `AWSServiceRoleForElastiCache` the first time
  a group is created if the caller may `iam:CreateServiceLinkedRole` for
  `elasticache.amazonaws.com`; otherwise create it once yourself.

## IAM

The inspection role gains two statements (replace `<region>` and `<account>`). Neither has a
delete action. This is a privilege increase for roles already deployed, including the
`ModifyReplicationGroup` and read-only statement added for updates:

```json
{
  "Sid": "ElastiCacheCreateAndDescribe",
  "Effect": "Allow",
  "Action": [
    "elasticache:CreateReplicationGroup",
    "elasticache:ModifyReplicationGroup",
    "elasticache:ListAllowedNodeTypeModifications",
    "elasticache:CreateCacheSubnetGroup",
    "elasticache:CreateCacheParameterGroup",
    "elasticache:ModifyCacheParameterGroup",
    "elasticache:CreateUser",
    "elasticache:ModifyUser",
    "elasticache:CreateUserGroup",
    "elasticache:ModifyUserGroup",
    "elasticache:AddTagsToResource",
    "elasticache:ListTagsForResource",
    "elasticache:DescribeReplicationGroups",
    "elasticache:DescribeCacheClusters",
    "elasticache:DescribeCacheParameterGroups",
    "elasticache:DescribeUsers",
    "elasticache:DescribeUserGroups"
  ],
  "Resource": [
    "arn:aws:elasticache:<region>:<account>:replicationgroup:gimme-*",
    "arn:aws:elasticache:<region>:<account>:cluster:gimme-*",
    "arn:aws:elasticache:<region>:<account>:subnetgroup:gimme-*",
    "arn:aws:elasticache:<region>:<account>:parametergroup:gimme-*",
    "arn:aws:elasticache:<region>:<account>:user:gimme-*",
    "arn:aws:elasticache:<region>:<account>:usergroup:gimme-*"
  ]
}
```

Four read-only calls do not support resource-level permissions, so they need `"Resource": "*"`:

```json
{
  "Sid": "ElastiCacheReadOptions",
  "Effect": "Allow",
  "Action": [
    "elasticache:DescribeUpdateActions",
    "elasticache:DescribeCacheEngineVersions",
    "elasticache:DescribeReservedCacheNodesOfferings",
    "ec2:DescribeSecurityGroupRules"
  ],
  "Resource": "*"
}
```

The administrative user's secret is written with the existing `WorkloadSecrets` statement from
the RDS page, so the Secret Store prefix must match it. The IAM statements are not yet verified
against a live account.

## Register the Resource

`config/state.example.json` has a complete example:

```json
{
  "kind": "valkey",
  "provider": "aws_elasticache_valkey",
  "aws_network": "primary",
  "administration_target": "adminbox",
  "engine_version": "9.0",
  "node_type": "cache.m7g.large",
  "security_group_id": "sg-0123456789abcdef2",
  "administration_security_group_id": "sg-0123456789abcdef0",
  "deployment_security_group_ids": {"devbox": "sg-0123456789abcdef1"},
  "snapshot_window": "03:00-04:00",
  "snapshot_retention_days": 7,
  "maintenance_window": "sun:05:00-sun:06:00",
  "workload_secret_store": "workload-secrets",
  "retain_on_removal": true
}
```

- `engine_version` is an exact Valkey version, 9.0 or later, in its canonical spelling.
  `node_type` is `cache.<family>.<size>`. Registering, or updating to, a `node_type` the
  account does not offer in the network's region is refused with
  `aws_elasticache_node_type_unavailable`, so registration now reads from AWS through the
  inspection role. AWS does not say which node types support synchronous durability, so one
  that does not is only rejected at creation, with a bounded error. Read
  `gimme://aws-networks/<network>/valkey-options` for the exact Valkey versions and node types
  the account offers.
- `administration_security_group_id` is the security group of the administration Target and
  `deployment_security_group_ids` maps each Deployment Target that may bind the Resource to its
  security group, exactly as for RDS. They are the only sources allowed to reach the Valkey
  security group, and a Deployment on an unlisted Target cannot bind the Resource.
- `snapshot_window` is a daily UTC `HH:MM-HH:MM` window of at least 60 minutes.
  `snapshot_retention_days` is 1 to 35. `maintenance_window` is a weekly UTC
  `ddd:HH:MM-ddd:HH:MM` window of exactly 60 minutes and must not overlap the snapshot window.
- Topology, cluster mode, TLS, encryption, and durability are fixed by Gimme and are not fields.

Use `register_resource`, or `plan_update_resource` and `update_resource` to change it. Updates
are checked locally and refused with `aws_elasticache_update_forbidden_<field>` for a changed
`aws_network`, an engine major version, or `security_group_id`, and
`aws_elasticache_update_forbidden_provider` for a change between this provider and another.
Same-major engine, `node_type`, windows, and retention are accepted and applied to an
existing group by `apply_resource`.

## Provision

`plan_apply_resource` then `apply_resource` creates, in order: the cache subnet group, a
parameter group `gimme-<name>-params` (`cluster-enabled yes`, `maxmemory-policy noeviction`),
the default user with an access string that can never authenticate, an administrative user
whose generated 48-character password is written to the Secret Store at
`<prefix>/<resource>/_admin` as `username` and `password`, a user group holding both, and the
replication group. Every object is tagged `gimme:resource=<name>`. Gimme refuses a group or
parameter group of the same name that it does not own, and never adopts one.

Identifiers are derived from the Resource name (`gimme-<name>`). ElastiCache allows only 40
characters, so a long or irregular name gets a hash suffix.

Creation takes several minutes. `apply_resource` polls for at most 30 seconds and returns
`phase: pending`; plan and apply again to resume, which describes the group and never creates
a second one.

## Update an existing group

`apply_resource` on an existing `available` group makes one immediate
`ModifyReplicationGroup` carrying only the fields that differ from the Resource: `engine_version`,
`node_type`, `snapshot_retention_days`, `snapshot_window`, and `maintenance_window`. Before any
change it refuses, with no modify call:

- `aws_elasticache_modify_forbidden_engine_major` and
  `aws_elasticache_modify_forbidden_engine_downgrade`: only newer versions of the same major
  are applied. A desired `9.0` is satisfied by a running `9.0.3`;
- `aws_elasticache_modify_forbidden_node_type`: AWS does not list the desired type among the
  allowed modifications of this group;
- `aws_elasticache_modify_forbidden_<reason>`, using the codes below, when there is something
  to change but the group is also outside the contract (say TLS is off). Gimme never touches a
  group it would have to repair beyond those five fields. With nothing to change, apply
  returns the `degraded` phase instead.

An engine version or node type change replaces nodes one at a time and may fail over the
primary, briefly interrupting connections. Values AWS has accepted but not finished applying
count as applied, and a group that is not yet `available` is polled and never sent a second
modification, so re-running apply mid-change is safe. `inspect_resource` reports `drift`
(`engine_version`, `node_type`, retention, and windows, desired against live) and whether a
modification is pending. A pending engine version or node type keeps the phase `pending`.

Phases are `pending`, `ready`, `degraded`, and `failed`. A `failed` group (AWS reports
`create-failed`) is never deleted or recreated by Gimme: delete it yourself, then apply again. An `available` group that does not
meet the contract is `degraded` with fixed reason codes: `cluster_mode`, `topology`,
`availability_zones`, `multi_az`, `automatic_failover`, `tls`, `encryption_at_rest`,
`durability` (the effective durability is not `sync`), `authentication`, `snapshot_policy`,
`maintenance_policy`, `automatic_minor_upgrade`, `security_group`, `service_update_overdue` (AWS reports a
service update that missed its recommended apply-by date and is not finished). Apply the update
yourself; Gimme does not.

If creating the administrative user fails after its secret was written, the next apply
generates and stores a fresh password before trying again. If the user already exists, its
stored credential is left alone.

## Bind a Deployment

A Deployment binds the Resource with a typed binding, replacing the old `resources.cache`
string:

```json
"resources": {
  "database": "orders-postgres",
  "valkey": {"resource": "shared-valkey", "uses": ["cache", "session", "queue"]}
}
```

`uses` is a non-empty list of `cache`, `session`, and `queue`, each at most once, and a
Deployment that runs Horizon must include `queue`. The namespaces are derived, never supplied:
`{gimme:<deployment>}:cache:`, `:session:`, `:queue:`, and `:horizon:` (with `queue`). They share
one hash tag so multi-key Laravel and Horizon operations stay in one cluster slot.

`plan_bind_resource` then `bind_resource` gives the Deployment its own ACL user, restricted to
its own key and channel namespace and to the fixed, Gimme-owned `laravel-v1` command profile, and
adds it to the Resource's user group. Its Resource Credential is a secret at
`<prefix>/<resource>/<deployment>` holding exactly `username` and a 48-character URL-safe
`password`. Neither is ever returned, stored in state, plans, or observations, or logged. The
profile denies administrative, configuration, ACL, persistence, replication, flush, and
keyspace-scanning commands, so a Deployment cannot list or touch another Deployment's keys;
`cache:clear` (which flushes the database) is therefore not permitted. Callers cannot supply an
access string.

Binding needs a `ready` Resource by a fresh live read, so a `degraded` one, including unsafe
security group drift, takes no new binding and existing Deployments keep running. Binding again
keeps the recorded credential and only re-applies the profile. A Deployment name that has no
recorded allocation but already has a user gets a fresh credential. Removing a Deployment Target
from `deployment_security_group_ids` while a Deployment on it is bound, and moving
`workload_secret_store` once credentials exist, are refused by `plan_update_resource`, and a
bound Resource cannot be removed.

## Security group

`inspect_resource` and every describe read the inbound rules of the group's security group
through `ec2:DescribeSecurityGroupRules`. Any rule reaching TCP 6379 whose source is not the
administration security group or a listed Deployment Target security group, including a CIDR,
prefix list, or any unrelated group, and any attached group other than `security_group_id`,
makes the Resource `degraded` with the `security_group` code. Gimme never edits the group, so
fix it yourself. The allowed set is the listed Deployment Targets rather than only the bound
ones.

## Inspect and remove

`inspect_resource` describes the group live and returns `phase`, `status`, `engine_version`,
`effective_durability`, and `issues`. It never returns an endpoint, address, ARN, user, group
identifier, or secret identifier. If AWS cannot be reached it returns the last cached state
with a bounded `refresh_error`.

`plan_cleanup_resource` and `apply_cleanup_resource` require `RETAIN <name>` and remove only the
local registration, writing a Retained Resource tombstone. The replication group, its data,
snapshots, users, secrets, and the subnet and parameter groups remain in AWS and keep costing
money until you delete them yourself. Destruction is a later slice.

## Failure codes

Provider failures become fixed `aws_elasticache_<operation>_<reason>` codes, for example
`aws_elasticache_create_access_denied`. Reasons are `access_denied`, `missing`, `already_exists`,
`invalid_state`, `throttled`, `revoked`, and `unavailable`, and AWS messages, ARNs, and values
are never included. Operations include `subnet_group`, `parameter_group`,
`parameter_group_verify`, `parameter_group_modify`, `user_create`, `user_group_create`,
`user_describe`, `user_bind`, `user_group_bind`, `security_group`, `tags`, `describe`, `describe_cluster`, `update_actions`, `node_types`,
`options`, `modify`, and `create`.
`aws_elasticache_group_ownership_mismatch` and
`aws_elasticache_parameter_group_ownership_mismatch` mean a same-named object exists that this
Resource does not own; rename or delete it yourself. `unavailable` covers anything unclassified,
such as an unsupported node type or a quota; call the same API with the same role in your own
terminal to read the error.
