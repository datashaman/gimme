# Use AWS ElastiCache for Valkey as a managed Resource

[ADR 0009](../adr/0009-synchronously-durable-aws-valkey.md) describes managed Valkey on AWS
ElastiCache: one shard with one cross-AZ replica, cluster mode, TLS, encryption at rest, and
synchronous durability. This page describes what is implemented today. The provider is not
finished: see the list below.

## Current scope

Implemented: registering the Resource, local validation, provisioning one replication group
(with its subnet group, parameter group, user group, and administrative user), live inspection
with fixed readiness codes, and retention-by-default removal.

Not implemented yet (tracked in #14): applying updates to an existing group and drift
reconciliation, Deployment bindings and workload credentials, the application contract,
destruction, snapshots and restore. A Deployment whose `resources.cache` names this Resource is
refused when state is validated.

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
  allow TCP 6379 from the administration Target's security group yourself;
- an administration Target (`"role": "administration"`);
- the ElastiCache service-linked role. AWS creates `AWSServiceRoleForElastiCache` the first time
  a group is created if the caller may `iam:CreateServiceLinkedRole` for
  `elasticache.amazonaws.com`; otherwise create it once yourself.

## IAM

The inspection role gains one statement (replace `<region>` and `<account>`). It has no delete
or modify-group actions. This is a privilege increase for roles already deployed:

```json
{
  "Sid": "ElastiCacheCreateAndDescribe",
  "Effect": "Allow",
  "Action": [
    "elasticache:CreateReplicationGroup",
    "elasticache:CreateCacheSubnetGroup",
    "elasticache:CreateCacheParameterGroup",
    "elasticache:ModifyCacheParameterGroup",
    "elasticache:CreateUser",
    "elasticache:CreateUserGroup",
    "elasticache:AddTagsToResource",
    "elasticache:ListTagsForResource",
    "elasticache:DescribeReplicationGroups",
    "elasticache:DescribeCacheClusters",
    "elasticache:DescribeCacheParameterGroups",
    "elasticache:DescribeUsers"
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

The administrative user's secret is written with the existing `WorkloadSecrets` statement from
the RDS page, so the Secret Store prefix must match it.

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
  "snapshot_window": "03:00-04:00",
  "snapshot_retention_days": 7,
  "maintenance_window": "sun:05:00-sun:06:00",
  "workload_secret_store": "workload-secrets",
  "retain_on_removal": true
}
```

- `engine_version` is an exact Valkey version, 9.0 or later, in its canonical spelling.
  `node_type` is `cache.<family>.<size>`; whether AWS offers it with synchronous durability is
  not checked until creation, when AWS rejects an unsupported one with a bounded error.
- `snapshot_window` is a daily UTC `HH:MM-HH:MM` window of at least 60 minutes.
  `snapshot_retention_days` is 1 to 35. `maintenance_window` is a weekly UTC
  `ddd:HH:MM-ddd:HH:MM` window of exactly 60 minutes and must not overlap the snapshot window.
- Topology, cluster mode, TLS, encryption, and durability are fixed by Gimme and are not fields.

Use `register_resource`, or `plan_update_resource` and `update_resource` to change it. Updates
are checked locally and refused with `aws_elasticache_update_forbidden_<field>` for a changed
`aws_network`, an engine major version, or `security_group_id`, and
`aws_elasticache_update_forbidden_provider` for a change between this provider and another.
Same-major engine, `node_type`, windows, and retention register, but are not yet applied to an
existing group.

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
a second one. An existing group is never modified.

Phases are `pending`, `ready`, `degraded`, and `failed`. A `failed` group (AWS reports
`create-failed`) is never deleted or recreated by Gimme: delete it yourself, then apply again. An `available` group that does not
meet the contract is `degraded` with fixed reason codes: `cluster_mode`, `topology`,
`availability_zones`, `multi_az`, `automatic_failover`, `tls`, `encryption_at_rest`,
`durability` (the effective durability is not `sync`), `authentication`, `snapshot_policy`,
`maintenance_policy`, `automatic_minor_upgrade`.

If creating the administrative user fails after its secret was written, the next apply
generates and stores a fresh password before trying again. If the user already exists, its
stored credential is left alone.

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
`user_describe`, `tags`, `describe`, `describe_cluster`, and `create`.
`aws_elasticache_group_ownership_mismatch` and
`aws_elasticache_parameter_group_ownership_mismatch` mean a same-named object exists that this
Resource does not own; rename or delete it yourself. `unavailable` covers anything unclassified,
such as an unsupported node type or a quota; call the same API with the same role in your own
terminal to read the error.
