# Changelog

All notable changes to this alpha project are recorded here. Until 1.0, minor releases
may contain deliberate schema and MCP API breaks.

## [Unreleased]

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
