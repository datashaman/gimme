# Changelog

All notable changes to this alpha project are recorded here. Until 1.0, minor releases
may contain deliberate schema and MCP API breaks.

## [Unreleased]

- Completed the build-once/deploy-many artifact tracer bullet with an exact-publication resource,
  secret- and storage-identity-safe public projections, a disposable two-Target lifecycle proof,
  and operator/acceptance documentation covering migration, IAM separation, deployment,
  promotion, verified rollback, failure handling, costs, and the no-garbage-collection boundary.
- Replaced confirmation-only rollback with a content-addressed plan/apply flow for source and
  artifact releases. Planning selects and verifies the exact retained predecessor, runtime and
  platform capability, health, and process effects. Apply rechecks the complete inventory on the
  Target immediately before cache regeneration and candidate/live gates, switches atomically,
  restores the prior release on failure, and returns only bounded release identity.
- Added build-free artifact promotion. Planning validates the source Deployment's live readonly
  release metadata and immutable tree, recomputes the destination-context build identity locally,
  verifies the exact private publication with destination reader authority, and binds runtime,
  architecture, health, and process compatibility. Apply rechecks all evidence, activates through
  the artifact deployment path, and pins destination source only after successful live health.
- Added the first artifact-mode Deployment path. Planning recomputes the expected build,
  resolves and verifies one exact private publication from the destination Target, checks its
  execution-time PHP capability, and binds object versions plus archive/tree digests. Apply
  downloads those exact versions directly on the Target, safely extracts and re-hashes the
  immutable tree, records readonly secret-free release metadata, and then uses the existing
  environment, Laravel, health-gate, activation, process-refresh, rollback, and retention graph
  without Git, Composer, or frontend build tooling on the destination.
- Extended Application Artifact publication to npm, pnpm, Yarn 1/2+, and Bun frontends with
  exact lock/runtime identity, fixed frozen commands, protected build-only SOPS secrets,
  exact-value leak scanning, and secret-independent reproducibility enforcement.
- Added the first end-to-end Application Artifact publication path for backend-only Laravel
  Applications. Reviewed source, lockfile, runtime, extension, platform, policy, and execution
  inputs form a versioned build identity; the Build Target performs frozen Composer installation,
  deterministic safe packaging, encrypted upload/readback, manifest-last first-writer publication,
  reproducibility enforcement, cleanup, and bounded secret-safe integrity inventory.
- Added schema-v6 immutable Artifact foundations. Desired state now has bounded versioned Artifact
  Stores, separate publisher/reader authority, explicit Deployment release modes, Application
  build policy, and a hard operator-led migration. Target-side capability probes keep object bytes
  and credentials off MCP while verifying encrypted publisher round trips and exact reader access.
- Completed the common Recovery Policy execution path: reviewed on-demand requests now invoke
  the same content-bound Target runner, Deployment lock, capture, verification, idempotency, and
  retention implementation as timers. Manual Valkey policies retain capture authority while
  schedule cleanup remains endpoint- and credential-free. Stored Backup Destination credentials
  now optionally carry bounded session tokens and report fixed expiry outcomes. Operator
  documentation and the disposable-host matrix cover cadence reconciliation, stored credentials,
  latest-slot catch-up, bounded status, ambient/manual cleanup, and secret redaction.
- Wired Recovery Schedule reconciliation into Deployment Resource apply and removal. Apply now
  rotates protected S3 and managed-Valkey credentials only after application-secret activation;
  manual policy and removal disable units without requiring a live managed endpoint or decrypted
  credential. The scheduled runner now preserves request-owned Valkey maintenance through capture
  verification, restores runtime before manifest publication, and keeps failed-exit authority for
  safe retry. Bounded runner status is exposed through one strictly parsed fixed marker to the MCP
  schedule projection.
- Completed the standalone runner's scheduled activation path: it validates the exact
  fingerprint and stable delay, consumes only named systemd credentials, records atomic bounded
  attempt status, waits at most five minutes for the Deployment lock, executes the shared capture
  core, and enforces verified retention oldest-first. The shared Target core now owns bounded S3
  inventory, strict restore-event protection, completed-Safety eligibility, and a shared
  manifest-authorized exact-version deletion path, stopping at the first failed candidate while
  preserving a successfully published replacement.
- Added the standalone runner's bounded execution core over the shared Target capture module. It
  independently checks the fingerprinted authority shape and credential-mode agreement, creates
  the deterministic Recovery Point identity, captures PostgreSQL and optional Valkey through the
  sole fixed implementations, publishes through the narrow S3 adapter, removes protected local
  dumps, and collapses unexpected provider or secret-bearing failures to fixed safe outcomes.
- Installed the standalone Recovery Schedule runner, shared Target capture module, and fixed
  Valkey capture program as root-owned Target bootstrap assets. Their sources now participate in
  the Target-bound helper policy; stack inspection fails back to bootstrap when an asset is
  missing, and the privileged schedule reconciler verifies the exact post-substitution runner
  SHA-256 before it mutates or enables any unit.
- Added an independent protected transfer and systemd credential channel for scheduled managed
  Valkey capture. The Deployer boundary accepts only a regular local credential file, cleans both
  remote transfers unconditionally, and the privileged helper independently validates the exact
  username/password document before atomically installing it as root-owned mode 0600 and exposing
  it only as `LoadCredential=valkey`; local/manual policy removes stale Valkey material.
- Extended the private Recovery Schedule authority with the bounded, secret-free Valkey
  execution contract needed by the shared Target capture core: derived namespace, observed or
  loopback endpoint, fixed TLS posture, and credential mode. The privileged helper validates the
  complete schema independently, while unavailable managed Resources remain ordinary readiness
  issues instead of making resource planning fail.
- Extended the shared Target capture core to Valkey without adding another protocol
  implementation. It invokes only the fixed installed `gimme-capture-valkey` program with
  validated derived prefix, endpoint, TLS, version, and protected credential identity; then
  independently verifies the bounded marker, archive size, record count, timestamp, and SHA-256
  before returning the same `gimme-valkey-v1` component used by existing manifests.
- Added the shared Target capture core's narrow boto3 adapter. It builds one client from the
  validated registered destination and ambient-or-stored credential mode, forces registered
  addressing and encryption, and exposes only exact-key/exact-version put, head, read, and cleanup
  operations with fixed provider failures. The review also corrected the merged privileged
  helper's endpoint validator to accept the state model's raw `host[:port]` representation rather
  than incorrectly requiring an `https://` URL.
- Added the dependency-light shared Target capture core that will be invoked by both scheduled
  and on-demand execution. It derives the existing Recovery Point identity, captures PostgreSQL
  through one fixed argv vector, hashes protected local output, uploads and re-reads exact object
  versions, publishes the existing immutable schema-v3 manifest last, converges matching retries,
  and removes only exact versions after pre-publication failure. It is not installed or activated
  until its S3 adapter and Valkey path are complete.
- Added a fixed Target runtime probe before non-manual Recovery Schedule reconciliation. It
  imports only boto3 through the fixed `python3` executable, emits one strictly parsed bounded
  version marker, and runs before desired-state transfer or privileged mutation. Manual cleanup
  does not depend on boto3, so a broken runtime can never prevent disabling a schedule.
- Made the standalone runner's S3 runtime an explicit Target policy prerequisite. A non-manual
  Recovery Policy is not resource-apply-ready unless the registered exact APT stack includes
  `python3-boto3`; plans return the fixed `recovery_schedule_runtime_missing` issue otherwise.
  The example state and disposable-host policy declare the package. Manual and existing
  controller-side on-demand capture remain unaffected while the common Target capture moves.
- Added, but deliberately did not install, the standalone Recovery Schedule runner core. It
  duplicates no capture or S3 behavior: this slice only fixes logical-slot derivation, the exact
  controller-compatible scheduled request identity, atomic bounded attempt status, and a
  Deployment-scoped lock with a hard five-minute timeout. Its CLI remains non-operational until
  the following common capture executable is ready, so merged resource apply still cannot enable
  a timer that would silently skip a backup.
- Added the fixed Deployer transfer boundary for Recovery Schedule reconciliation. The runner
  accepts canonical secret-free authority JSON through a dedicated environment field; the remote
  task writes a caller-owned mode-0600 desired-state document below the configured applications
  root, optionally uploads one protected credential transfer, invokes only the policy-bound
  helper with the derived Deployment identity, and unconditionally removes transfer residue.
  The task is not yet called by resource apply, so this slice cannot activate a schedule.
- Added the Recovery Schedule helper's stored-credential boundary. A scheduled stored-auth
  authority now requires one fixed caller-owned mode-0600 transfer containing exactly the two
  resolved S3 credential values; the helper validates and removes the transfer before mutation,
  installs or rotates one root-owned mode-0600 credential atomically, and exposes it to the fixed
  service only through `LoadCredential`. Ambient and manual policies persist no credential and
  remove superseded material. No secret value or reference enters units, plans, or output.
- Added the Target-bound privileged Recovery Schedule reconciler and bound its exact source to
  the bootstrap policy and execution fingerprint. It accepts only a caller-owned mode-0600
  authority document, independently validates every policy-derived field, renders one fixed
  hardened service/timer pair, installs root-owned authority atomically, and disables the timer
  before a changed multi-file set is replaced. Manual cadence removes exact units, authority,
  stored credential material, and bounded status. Timer activation remains gated on the separate
  content-addressed runner layer, so this slice cannot enable a non-existent runner.
- Added a content-addressed, secret-reference-free Recovery Schedule authority model to
  Deployment Resource plans. It binds the normalized policy, registered Deployment/Target,
  immutable placement, selected Resource provenance, destination execution policy, fixed unit
  identities, stable delay, and status identity while exposing only ambient/stored auth mode.
- Added a bounded Recovery Schedule Status tool/resource projection. Manual cadence reports
  disabled locally; scheduled cadence observes only the exact Deployment-derived timer and
  collapses missing, malformed, or unavailable target observations to fixed secret-safe fields.
  Latest-attempt persistence will arrive with the standalone runner layer.
- Enforced `retain_last` after every newly published or idempotently verified on-demand
  Recovery Point. Automatic retention selects verified, unprotected points oldest-first,
  uses the sole exact-version deletion primitive, never deletes the successful replacement,
  and stops on the first failure with a bounded `backup_succeeded_retention_failed` result.
- Began scheduled Recovery Policy support with strict manual/hourly/daily/weekly UTC cadence
  unions, `retain_last` from 1 through 365 (default 7), exact systemd-calendar normalization,
  latest-slot catch-up calculations, stable 0–300 second Deployment jitter, and deterministic
  policy-and-slot-scoped scheduled request identities. This slice adds policy and pure scheduling
  semantics only; target runner activation and persisted attempt status remain.
  Existing state without these fields loads as manual cadence with the documented defaults.
- Added an executable opt-in ElastiCache recovery/rotation live matrix for an isolated registered
  Resource and Deployment. Separate `GIMME_AWS_VALKEY_LIVE_CREATE=1` and
  `GIMME_AWS_VALKEY_LIVE_DESTROY=1` flags authorize creation/rotation and exact deletion. The
  harness rotates through Gimme, simulates exact out-of-band group loss, proves ordinary apply
  fails closed, restores and verifies the Deployment through Gimme, removes its exact test
  snapshot, and leaves the supplied Resource ready. A private exact-run marker makes interrupted
  delete/restore/cleanup phases resumable. No MCP, schema, or privilege change.
- Completed the Recovery Point deletion verification matrix. Provider protection and access
  failures now map to fixed secret-safe outcomes, deterministic tests cover the complete S3
  failure taxonomy and concurrent cross-Deployment isolation, and the disposable MinIO workflow
  proves ordinary and final-point deletion, unresolved and completed Safety behavior, partial
  retry, idempotency, exact-version removal without delete markers, and Object Lock rejection.
  MinIO's test bucket now enables Object Lock at creation. No schema or privilege change.
- Added recovery of a managed `aws_elasticache_valkey` Resource: snapshot inventory, restore, and
  credential rotation (slice 7 of #14, the last). `list_resource_snapshots` lists a group's
  snapshots by name and status. `plan_restore_resource` / `apply_restore_resource` create a lost
  group from one, and `plan_recreate_empty_resource` / `apply_recreate_empty_resource`
  (`RECREATE EMPTY RESOURCE <name>`) create an empty one; both recreate the user group and any
  missing ACL user from the credential already stored, so no credential is rotated, and the
  Resource stays `restoring`, blocking every other operation, until each recorded Deployment has
  its environment refreshed to the new endpoint, its current release probed, and its workers
  restarted; a failed verification or a still-creating group is resumed by repeating the call.
  `apply_resource` now refuses (`aws_elasticache_group_missing_replace_explicitly`) when a group
  that was observed has vanished, instead of silently creating an empty one.
  `plan_rotate_resource_credential` / `apply_rotate_resource_credential` create the next-generation
  ACL user and secret version, prove them with the same refresh, probe, and restart, then delete
  the previous user, and roll back on failure; an unfinished rotation or restore is a local marker
  that makes every other operation refuse. Deployer gains `gimme:probe:valkey:current`. Privilege
  impact: the inspection role needs `DescribeSnapshots` and `CreateReplicationGroup` on snapshots,
  and the destructive role's `DeleteUser` is now also used by rotation, so a rotation needs a
  destructive role; both are in the ElastiCache how-to. Not verified against a live account:
  restoring with `SnapshotName` in cluster mode, snapshots of a destroyed group, deleting a user
  that is still in a user group, and whether running PHP processes pick up a rotated credential.
- Added destruction of a managed `aws_elasticache_valkey` Resource with separate authority (slice 6
  of #14). `AWSProviderAccount` gains an optional `destructive_role_arn` (same account, distinct
  from the inspection and resolver roles), assumed only while applying a destruction and never
  while planning, registering, or inspecting. `plan_destroy_resource` (local, secret-free) and
  `apply_destroy_resource` (exact confirmation `DESTROY RESOURCE <name>`) delete the replication
  group with a final snapshot, then the user group, users, parameter group, and subnet group Gimme
  created, then the local registration. It requires no referencing Deployment or live allocation,
  a prior observation, and a live group with the planned identity and ownership tag; each
  dependent's ownership tag is checked before it is deleted, an already-missing object counts as
  done, and a group still deleting after 30 seconds returns `phase: deleting` so the same call
  resumes. A destruction in progress blocks provisioning and binding. The final snapshot, manual
  snapshots, and workload secrets are retained. New `plan_forget_resource` and
  `apply_forget_resource` (`FORGET <name>`) delete a Retained Resource tombstone, RDS or Valkey,
  locally only. Privilege impact: a new optional role needs the delete statement documented in the
  ElastiCache how-to; no existing role changes. Not verified against a live account.
- Added the `laravel-cluster-v1` application contract and pre-switchover probes for a Deployment
  bound to a managed `aws_elasticache_valkey` Resource (slice 5 of #14). Gimme injects protected
  `GIMME_VALKEY_*` values (TLS cluster endpoint, fixed client settings with bounded retry and
  jitter, no replica reads, derived namespaces) and selects the Redis adapter for exactly the
  declared uses, pinning undeclared uses to `file`/`file`/`sync`; the Resource Credential is
  referenced like any secret and resolved at apply time. State validation refuses any
  `GIMME_VALKEY_*` key, and `SESSION_DRIVER` when bound to a managed Resource, in a Deployment's
  `variables` or `secrets`. A Horizon prefix set by the contract is no longer overwritten when
  processes are provisioned. `plan_deployment_resources` shows the secret-free contract and
  `plan_deployment` refuses until the Resource is ready and the Deployment bound. `deploy` now
  runs a fixed stdlib probe on the Target before the symlink switch, ahead of the candidate
  health check (cluster mode, TLS with hostname verification, auth, primary read-after-write,
  namespace enforcement, each declared use, and Horizon compatibility from `composer.lock`:
  laravel/framework 12.0.0 and laravel/horizon 5.46.0 or later); a failed probe fails the deploy
  and leaves the current release live, and probe keys are namespaced, short-lived, and removed.
  The disposable workflow now independently drives real Laravel cache, session, and queue APIs
  plus Horizon's Redis repository through a locked Laravel 12/Horizon 5 fixture and observes
  namespace and ACL denials. This also corrected the former impossible Laravel 13/Horizon 5
  compatibility pair. No privilege change. ElastiCache behavior is not yet exercised.
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
