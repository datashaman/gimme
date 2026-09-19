# Register an AWS ElastiCache Valkey Resource

[ADR 0009](../adr/0009-synchronously-durable-aws-valkey.md) describes managed Valkey on AWS
ElastiCache: one shard with one cross-AZ replica, cluster mode, TLS, encryption at rest, and
synchronous durability. This page describes what exists today, which is **registration only**.

## Current scope

Implemented: registering the Resource in desired state, validating it locally, and refusing
the updates ADR 0009 says need a new Resource.

Not implemented yet (tracked in #14): creating the replication group, live inspection,
applying updates, Deployment bindings and credentials, cleanup and destruction, snapshots
and restore. Registration makes no AWS call, `inspect_resource` reports only
`phase: registered`, and a Deployment whose `resources.cache` names this Resource is refused
when state is validated.

## Register

The Resource reuses an AWS Network, an administration Target (`"role": "administration"`), and
an AWS Secrets Manager store, exactly as in
[`use-aws-rds-postgresql.md`](use-aws-rds-postgresql.md). `config/state.example.json` has a
complete example:

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

- `engine_version` is an exact Valkey version, 9.0 or later. `node_type` is `cache.<family>.<size>`;
  whether AWS offers it with synchronous durability is not checked until provisioning exists.
- `security_group_id` is a pre-existing group Gimme will validate but never edit.
- `snapshot_window` is a daily UTC `HH:MM-HH:MM` window of at least 60 minutes.
  `snapshot_retention_days` is 1 to 35.
- `maintenance_window` is a weekly UTC `ddd:HH:MM-ddd:HH:MM` window of exactly 60 minutes
  and must not overlap the snapshot window on any day.
- Topology, cluster mode, TLS, encryption, and durability are fixed by Gimme and are not fields.

Use `register_resource`, or `plan_update_resource` and `update_resource` to change it.

## Updates

Updates are checked locally and refused with a fixed `aws_elasticache_update_forbidden_<field>`
code for a changed `aws_network`, an engine major version, or `security_group_id`, and
`aws_elasticache_update_forbidden_provider` for any change between this provider and another. A
refused update leaves desired state unchanged. Same-major engine versions, `node_type`, the
windows, `snapshot_retention_days`, `administration_target`, `workload_secret_store`, and
`retain_on_removal` register as before; applying them to AWS arrives with the provisioning work.

## Removal

`plan_cleanup_resource` and `apply_cleanup_resource` remove only the local registration, with
the usual `RETAIN <name>` confirmation. Nothing exists in AWS yet, so nothing is left behind.
