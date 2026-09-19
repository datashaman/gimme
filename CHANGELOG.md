# Changelog

All notable changes to this alpha project are recorded here. Until 1.0, minor releases
may contain deliberate schema and MCP API breaks.

## [Unreleased]

- Replaced the Deployment's `resources.cache` string with a typed `resources.valkey` binding
  (`resource` plus `uses` of `cache`, `session`, and `queue`) and bumped desired state to
  **schema v5** (slice 4 of #14). `plan_state_migration` migrates v2, v3, and v4 state one way:
  the old string becomes a `cache` use, plus `queue` for a running Horizon, `session` is never
  inferred, and any ambiguous shape fails the migration without writing; no reader for the old
  shape remains, so a v4 file must be migrated before it loads. Uses must be unique and
  non-empty, and Horizon requires `queue`. `plan_bind_resource` and `bind_resource` now also
  bind a managed `aws_elasticache_valkey` Resource: a per-Deployment ElastiCache ACL user
  limited to the derived namespace `{gimme:<deployment>}:<use>:` and the fixed Gimme-owned
  `laravel-v1` command profile, and a Resource Credential secret with exactly `username` and a
  48-character `password`, never returned, stored in state, or logged. A Resource that a fresh
  live read does not report ready takes no new binding. The Valkey Resource gains
  `administration_security_group_id` and `deployment_security_group_ids` (required for a
  managed Resource), and inbound rules on its security group are read and reported as the
  `security_group` issue, degrading the Resource; Gimme never edits the group. Privilege impact:
  the inspection role needs `elasticache:ModifyUser`, `ModifyUserGroup`, `DescribeUserGroups`
  and `ec2:DescribeSecurityGroupRules`. The state schema is a breaking change. Not yet
  verified against a live account; the ACL access string in particular is unverified.
- `apply_resource` now converges an existing `aws_elasticache_valkey` replication group (slice 3
  of #14) with one immediate `ModifyReplicationGroup` carrying only the fields that differ:
  same-major `engine_version`, `node_type`, snapshot retention and window, and maintenance
  window. A major mismatch, version downgrade, node type outside AWS's allowed modifications
  (`ListAllowedNodeTypeModifications`), or, when something needs changing, any other live
  difference from the contract is refused before any call with
  `aws_elasticache_modify_forbidden_<reason>`. Values AWS has pending count as applied and a
  group that is still modifying is never sent a second change. `inspect_resource` adds `drift`,
  and an unfinished service update past its apply-by date makes the Resource `degraded`
  (`service_update_overdue`). New read-only MCP resource
  `gimme://aws-networks/{name}/valkey-options` lists the exact Valkey versions and node types
  the account offers, and registering or updating to a `node_type` it does not offer is refused
  with `aws_elasticache_node_type_unavailable`, so Valkey registration now reads from AWS.
  Privilege impact: the inspection role needs `elasticache:ModifyReplicationGroup` and
  `ListAllowedNodeTypeModifications` on the group, plus `DescribeUpdateActions`,
  `DescribeCacheEngineVersions`, and `DescribeReservedCacheNodesOfferings` on `*`; describing an
  available group now also reads its update actions. Not yet verified against a live account.
- `apply_resource` now provisions an `aws_elasticache_valkey` Resource (slice 2 of #14): a cache
  subnet group, a parameter group (`cluster-enabled yes`, `maxmemory-policy noeviction`), a user
  group with a default user that cannot authenticate, an administrative user whose generated
  password is written to the workload Secret Store, and one replication group with one shard,
  one cross-AZ replica, Multi-AZ automatic failover, TLS, encryption at rest, synchronous
  durability, and no automatic minor upgrades. Apply polls for 30 seconds, resumes idempotently,
  and never modifies an existing group. `inspect_resource` reports secret-free `phase`
  (`pending`, `ready`, `degraded`, `failed`) and fixed `issues` codes, and `degraded` when the
  effective durability is not `sync`; removal now writes a Retained Resource tombstone.
  Privilege impact: the inspection role needs the new `ElastiCacheCreateAndDescribe` statement
  (create and describe actions on `gimme-*` ElastiCache objects, no delete or modify-group
  actions) and, once per account, the ElastiCache service-linked role; existing deployments of
  the policy must add them (see the ElastiCache how-to). Not yet verified against a live account.
- Added the `aws_elasticache_valkey` Resource for ADR 0009, registration only (slice 1 of #14):
  an exact Valkey 9 or later `engine_version`, `node_type`, one Valkey security group, a daily
  UTC snapshot window, `snapshot_retention_days` (1-35, default 7), and a non-overlapping
  60-minute weekly maintenance window, with the administration Target and AWS Secrets Manager
  store checked like a managed RDS Resource. Nothing is provisioned, and `inspect_resource`
  reports `phase: absent`. A Deployment binding to it
  is refused when state is validated until typed Valkey bindings exist. Updates that need a
  new Resource are refused locally with `aws_elasticache_update_forbidden_<field>`. No privilege
  change.
- Managed AWS RDS PostgreSQL Resources in `cn-*` regions are now refused with
  `aws_rds_tls_region_unsupported`, exactly like `us-gov-*`: the pinned trust bundle has no China
  roots, so an instance created there could never be bound.
- `apply_resource` now converges an existing managed AWS RDS PostgreSQL instance onto desired
  state with one immediate `ModifyDBInstance` (`ApplyImmediately`, never a major-version
  upgrade) carrying only the fields that differ: same-major `engine_version`,
  `instance_class`, an increased `allocated_storage_gb`, the security-group set, and the
  Resource-owned parameter group. Apply describes the live instance first and refuses a
  storage decrease, minor-version downgrade, or major mismatch (`aws_rds_modify_forbidden_*`)
  before any change; values already pending are not re-sent, so a resumed apply is safe. It
  reboots once, without forced failover, when the parameter group is `pending-reboot`, as after
  re-attaching it to an older instance. A pending change to a managed field now keeps the
  phase `pending`. `plan_apply_resource` remains local, names the disruption, and binds the
  security-group set. Privilege impact: the inspection role gains `rds:ModifyDBInstance` and
  `rds:RebootDBInstance` on `db:gimme-*` and `pg:gimme-*`; existing deployments of the policy
  must add them (see the RDS how-to).
- Managed AWS RDS PostgreSQL Resource updates are now validated locally against the ADR 0008
  allowlist: `plan_update_resource` and `update_resource` refuse a changed `aws_network`, engine
  major version, decreased `allocated_storage_gb`, changed `workload_secret_store` while
  allocations exist, a removed deployment Target security group still used by a bound
  Deployment, and a change between managed and target-local providers, each with a fixed
  `aws_rds_update_forbidden_<field>` error. `inspect_resource` now reports secret-free `drift`
  (engine version, instance class, storage, security groups, and whether a modification is
  pending) after a successful live read.
- Managed AWS RDS PostgreSQL binds now verify the server certificate: `psql` runs with
  `sslmode=verify-full` against a pinned AWS commercial-region root bundle
  (`deploy/aws-rds-global-bundle.pem`) uploaded per bind beside the secret file, checked by
  sha256 before connecting, and removed afterwards. Certificate and digest failures return
  distinct fixed errors without `psql` output; `us-gov-*` regions are refused with
  `aws_rds_tls_region_unsupported` when a managed Resource is registered, planned, applied, or
  bound, so an instance that could never be bound is not created. The bundle is part of the
  execution fingerprint, so earlier plans go stale.
- Fixed managed AWS RDS PostgreSQL instances not forcing TLS below PostgreSQL 15: creation
  now also creates a Resource-owned `gimme-<name>-params` DB parameter group
  (`postgres<major>`, `rds.force_ssl=1`, applied `pending-reboot`) before the instance and
  attaches it, re-applying the setting when the group already exists and refusing (with
  `aws_rds_parameter_group_ownership_mismatch`) an existing group that lacks this Resource's
  `gimme:resource` tag or the expected family. Privilege impact: the inspection role gains
  `rds:CreateDBParameterGroup`, `rds:ModifyDBParameterGroup`, `rds:DescribeDBParameterGroups`,
  `rds:ListTagsForResource`, and `rds:AddTagsToResource` on `pg:gimme-*`, and
  `rds:CreateDBInstance` on `pg:gimme-*`. Parameter groups are retained, never deleted, on
  Resource removal, and an already-created instance is not modified.
- Added schema-v4 S3-compatible Backup Destinations, Deployment Recovery Policies, and
  on-demand, content-addressed PostgreSQL Recovery Point creation and inventory.
  Destinations are preflight-verified (bucket versioning, encryption, and a
  leave-nothing-behind write/read/delete round trip); `pg_dump` excludes roles,
  ownership, and ACLs; the checksummed component and its immutable Recovery Manifest
  are verified before a Recovery Point becomes visible; and duplicate apply with the
  same request identity is a deterministic no-op. See
  [`docs/how-to/use-backup-destinations.md`](docs/how-to/use-backup-destinations.md).
- Added schema-v4 AWS Provider Accounts, bounded AWS Secrets Manager stores, structured
  Secret References, metadata-only planning, exact-version apply-time resolution, and
  secret-safe Applied Secret Manifests while retaining the fixed local SOPS store.
- Bound every schema-v3 mutation plan to a fingerprint of executable Gimme, Deployer,
  privileged-helper, and dependency-lock inputs so code changes invalidate stale plans.
- Fixed empty deployment environment objects being misclassified as JSON lists by PHP.
- Fixed the bootstrap CLI to inherit all terminal streams so Deployer's hidden sudo
  prompt is displayed live instead of being buffered with captured task output.
- Blocked deployments with managed processes before activation when the target-bound
  process helper is stale, and made target inspection use the same policy-hash check.
- Added a private append-only, secret-safe operation journal with correlated plan/apply
  outcomes, classified failures, an MCP resource, trace template, and filtered read tool.
- Added fixed official Ubuntu PPA bootstrap for an exact mise version.
- Added a complete MCP primitive reference and manifest validation documentation.
- Consolidated local and CI verification into one command that covers every Deployer
  module, and migrated the scheduled disposable-host smoke test to schema v3.

## [0.6.0] - 2026-09-16

### Added

- Schema-v3 named PostgreSQL and Valkey resources with deployment bindings.
- Deployment-scoped exact runtime pins for PHP, Composer, Node.js, npm, pnpm, Yarn,
  Bun, Python, Ruby, Go, and Java.
- mise runtime installation and execution without shell activation.
- Version-specific PHP CLI, FPM, queue, Horizon, scheduler, and health commands.
- Runtime plan/apply tools and observed schema-v2 migration.

### Changed

- Replaced target-global frontend toolchains with deployment runtime pins.
- Made PHP, Composer, PostgreSQL, and Valkey versions explicit.
- This release is a hard desired-state and MCP API break.

## [0.5.0] - 2026-09-15

- Introduced explicit target, application, and deployment control-plane objects.
- Added stages, promotion, worktree-oriented deployments, health gates, SOPS secret
  references, Horizon, scheduler, and public/local DNS policies.

[Unreleased]: https://github.com/datashaman/gimme/compare/main...HEAD
[0.6.0]: https://github.com/datashaman/gimme/compare/4fb0e97...a9ed1a3
[0.5.0]: https://github.com/datashaman/gimme/commits/4fb0e97
