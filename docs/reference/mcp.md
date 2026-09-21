# MCP reference

Gimme is a local stdio MCP server built with FastMCP. Its desired state is stored in
schema-v8 JSON; decrypted secrets are never returned by resources or tools.

Runtime schemas returned by MCP discovery are authoritative. This page documents the
stable intent, mutation boundary, and pairing of each primitive.

## Plan and apply rules

- Read-only inspection and planning tools do not mutate local or remote state.
- An `apply_*`, `update_*`, `promote_*`, or `remove_*` tool requiring a `plan_id`
  recomputes the plan and rejects stale or altered IDs.
- Every plan includes an `execution_fingerprint`; executable control-plane or dependency
  changes invalidate its `plan_id` before apply.
- Target, Application, and Resource registration tools create local entries directly. Deployment
  registration is plan/apply because it reserves bounded capacity and may inspect policy
  candidates. Provider Account and Secret Store registration is plan/apply because it verifies
  external identity and policy.
- `rollback_deployment` and `remove_deployment` require exact confirmation text. Rollback uses
  `ROLLBACK <deployment> TO <release>` from its reviewed plan.
- Remote operations are restricted to registered targets and validated fields. There
  is no arbitrary shell, SQL, service, package, or filesystem-path tool.

## Resources

| URI | Contents |
| --- | --- |
| `gimme://state` | Complete desired state with artifact auth and build-secret references replaced by bounded public projections |
| `gimme://fleet` | Desired Target slot capacity, reservations, free slots, and overcommit without live calls |
| `gimme://operations` | The 50 most recent secret-safe operation events, newest first |
| `gimme://targets/{name}` | One target and its network, stack, and runtime policy |
| `gimme://applications/{name}` | One reusable application definition without build-secret names or references |
| `gimme://applications/{name}/artifacts/{build_id}` | One exact bounded publication status without object identities, versions, or raw manifests |
| `gimme://provider-accounts/{name}` | One credential-free provider identity policy |
| `gimme://secret-stores/{name}` | One bounded Secret Store policy and derived ownership tag |
| `gimme://backup-destinations/{name}` | One bounded S3-compatible Backup Destination policy without credentials |
| `gimme://artifact-stores/{name}` | One bounded versioned S3-compatible Artifact Store policy without credential references or values |
| `gimme://resources/{name}` | One named PostgreSQL or Valkey resource |
| `gimme://aws-networks/{name}/valkey-options` | Exact Valkey versions and node types the registered account offers in one AWS Network's region (a live read, nothing stored) |
| `gimme://deployments/{name}` | One deployment, including pins, bindings, and placement |
| `gimme://deployments/{name}/rollout` | One secret-safe Rollout generation with bounded artifact identities, weights, phase, readiness, and background owner |
| `gimme://operations/{correlation_id}` | One operation trace in chronological order |

The parameterized URIs are resource templates. `gimme://state`, `gimme://fleet`, and
`gimme://operations` are concrete resources.

## State and inventory tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_state_migration` | Read | Plan schema-v8 migration, preserving placements, adding empty Rollout state, and assigning no accidental spare Target capacity |
| `apply_state_migration` | Local write | Apply the exact migration plan atomically |
| `list_targets` | Read | List registered targets and provisioning policy |
| `list_applications` | Read | List application source/build definitions without build-secret names or references |
| `list_provider_accounts` | Read | List credential-free external-provider identity policy |
| `list_secret_stores` | Read | List bounded Secret Store policy without secret identities or values |
| `list_resources` | Read | List named resources, optionally filtered by target |
| `list_deployments` | Read | List deployments, optionally filtered by target |
| `list_backup_destinations` | Read | List registered S3-compatible Backup Destinations without credentials |
| `list_artifact_stores` | Read | List bounded Artifact Store policies without credential references or values |
| `list_recovery_points` | Destination read | Read-only, destination-authoritative inventory of one deployment's Recovery Points |
| `list_operations` | Read | List recent journal events with exact operation, subject, and correlation filters |
| `inspect_fleet` | Remote read | Inspect bounded Target readiness without changing placement |
| `inspect_rollout` | Remote read | Inspect desired routing and bounded Target health/drift without paths, ports, output, cookies, or credentials |

## Operation journal

Gimme appends control-plane evidence to `operations.jsonl` beside desired state. Plan
calls record a `plan` event and return its `correlation_id`. Mutation calls record an
`apply` event before work starts and an `outcome` event afterward under a new correlation
ID. When the supplied plan was previously observed, `plan_correlation_id` links the apply
trace to it. Stale plans and policy rejections are classified without copying exception
text into the journal.

Each JSONL record contains only its schema version, event and correlation IDs, UTC
timestamp, fixed operation name, phase, status, bounded registered object names, optional
plan ID/link, and an optional safe error code. Gimme never journals definitions, command
arguments or output, environment values, secret references, or exception messages.
Correlation IDs are generated after a plan is hashed, so they do not alter `plan_id` or
make otherwise identical plans differ. Journal files and locks use mode `0600`; reads fail
closed if a record does not validate. `list_operations` returns newest events first and is
bounded to 200 records per call.

## Registration and update tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_register_provider_account` | Provider read | Verify both exact AWS roles and plan registration |
| `register_provider_account` | Local write | Reverify and register an AWS Provider Account |
| `plan_update_provider_account` | Provider read | Reverify and plan an account policy update |
| `update_provider_account` | Local write | Apply an exact account update plan |
| `plan_remove_provider_account` | Read | Plan local removal when no store references the account |
| `remove_provider_account` | Local write | Remove only the local account registration |
| `plan_register_secret_store` | Provider read | Verify region and inspection identity and plan store registration |
| `register_secret_store` | Local write | Register a bounded AWS Secrets Manager store |
| `plan_update_secret_store` | Provider read | Plan a bounded store policy update |
| `update_secret_store` | Local write | Apply an exact store update plan |
| `plan_remove_secret_store` | Read | Plan local removal when no Deployment references the store |
| `remove_secret_store` | Local write | Remove only the local store registration |
| `plan_register_backup_destination` | Read | Diff a proposed Backup Destination registration; makes no destination calls |
| `register_backup_destination` | Destination write | Preflight-verify and register one Backup Destination without storing credentials |
| `plan_update_backup_destination` | Read | Diff a proposed Backup Destination policy update; makes no destination calls |
| `update_backup_destination` | Destination write | Preflight-verify and apply one reviewed Backup Destination policy update |
| `plan_remove_backup_destination` | Read | Plan local removal when no Deployment references the destination |
| `remove_backup_destination` | Local write | Remove only the local destination registration |
| `plan_register_artifact_store` | Read | Plan local Artifact Store registration without a Target or store call |
| `register_artifact_store` | Local write | Apply the exact local-only registration plan |
| `plan_update_artifact_store` | Read | Plan a local Artifact Store policy replacement |
| `update_artifact_store` | Local write | Apply the exact local-only policy replacement |
| `plan_remove_artifact_store` | Read | Plan removal when no Application build policy references the store |
| `remove_artifact_store` | Local write | Remove only the local Artifact Store registration |
| `register_target` | Local write | Register a target |
| `plan_update_target` | Read | Diff a proposed target update |
| `update_target` | Local write | Apply an exact target update plan |
| `register_application` | Local write | Register reusable source/build metadata |
| `plan_update_application` | Read | Diff a proposed application update |
| `update_application` | Local write | Apply an exact application update plan |
| `register_resource` | Local write | Register a named, exact-version PostgreSQL or Valkey resource |
| `plan_update_resource` | Read | Diff a proposed resource update |
| `update_resource` | Local write | Apply an exact resource update plan |
| `plan_register_deployment` | Remote read for policy placement | Explain capacity, eligibility, deterministic selection, and immutable placement effects |
| `register_deployment` | Local write | Reserve one slot and persist the reviewed immutable placement |
| `plan_update_deployment` | Read | Diff a deployment update while preserving placement |
| `update_deployment` | Local write | Apply an exact deployment update plan |
| `plan_start_rollout` | Remote read | Verify stable and candidate artifacts, compatibility, capacity, runtime, and the exact zero-traffic preparation effects |
| `start_rollout` | Remote write | Reserve one temporary slot, materialize an isolated candidate backend, and directly health-check it at zero traffic |
| `plan_rollout_weights` | Remote read | Review integer weights totaling 100, exact artifacts, affinity generation, route fingerprint, and effects |
| `apply_rollout_weights` | Remote write | Preflight both backends, install and verify signed sticky routing, then persist the reviewed weights |
| `plan_complete_rollout` | Remote read | Review the exact 100%-candidate generation, process handoff, route convergence, cleanup, retention, and capacity effects |
| `complete_rollout` | Remote write | Promote the healthy candidate to an ordinary live release, hand off background ownership, retire temporary routing, and release capacity |
| `plan_reverse_rollout` | Remote read | Review stable restoration, candidate retirement, route/process rollback, retention, and capacity effects |
| `reverse_rollout` | Remote write | Restore verified stable-only service, retire the candidate, rotate affinity, and release capacity |

### Fleet placement

Every Target declares `deployment_slots` from 0 through 1024. Each Deployment reserves one slot
regardless of release or health state. A capacity reduction never evicts or relocates existing
Deployments; `gimme://fleet` reports overcommit and new admission stops.

Deployment registration chooses either one explicit `target` or a `placement_policy` containing
1–64 unique registered candidates. Policy planning freshly checks bounded reachability, bootstrap
and Target policy, network and Resource compatibility, exact system/bundled runtimes, and
capacity. Supported missing mise-managed pins remain eligible for later installation. Candidate
failures are fixed safe reason codes and do not fail the plan while another candidate is eligible.

Selection uses lowest occupied-slot ratio, then greatest free slots, then lexical Target name.
Apply repeats inspection and rejects any changed state or observation before an exclusive
reload–validate–write reservation. The stored `PlacementDecision` is immutable. Target loss keeps
the Deployment and slot on the selected Target; automatic relocation and rebalancing are not
implemented. See [fleet placement](../how-to/place-deployments-on-a-fleet.md).

### Artifact Stores and release mode

Schema v8 retains the schema-v6 requirement that every Deployment declares
`release_mode: source | artifact`.
`source` is valid only for local and preview stages. Staging and production must use
`artifact`, which requires the Application to have a Laravel build policy naming one
registered Deployment-capable Target, one registered Artifact Store, and the bounded
`laravel_v1` packaging policy. Exact build runtime inputs remain Deployment pins. Migration
does not infer any of these choices: callers supply an exact release-mode
map and any required store and build-policy definitions to both migration calls. Schema v5
documents remain unreadable until that reviewed migration is applied.

Artifact Store endpoints are HTTPS-only host-and-optional-port values; URL schemes, paths,
userinfo, and query strings are rejected. Authentication is either the selected Target's
ambient identity or separate publisher/reader references in the built-in `local-sops`
store. There are no profile, credential-file, object-key, or path inputs.

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_verify_artifact_store_publisher` | Read | Content-address a Build Target-side versioning, encryption, write/read/checksum, and exact-version-delete probe |
| `verify_artifact_store_publisher` | Remote write | Run that probe and return bounded evidence plus the exact version of a fixed Gimme reader-capability object |
| `plan_verify_artifact_store_reader` | Read | Content-address an exact read of that fixed object version on a Deployment Target |
| `verify_artifact_store_reader` | Remote read | Prove the reader identity can read and checksum the exact version without any write/delete operation or publisher fallback |
| `plan_build_artifact` | Remote read | Resolve and inspect one exact Deployment source, bind lock/runtime/platform/build inputs into `build_id`, and report authoritative publication status |
| `build_artifact` | Remote write | Build a reviewed backend-only Laravel archive on the Build Target, verify and upload it, and publish provenance last |
| `list_artifacts` | Remote read | List at most 100 newest secret-safe publications with fixed integrity/degradation status |

Probe bytes are generated and consumed on the Target and never transit MCP. Referenced
credentials use protected temporary files on both controller and Target and are removed in
`finally` cleanup. Results contain only fixed status fields, SHA-256 values, and the opaque
reader object version; provider errors and credentials are not returned.

`laravel_v1` publication supports Composer-only Applications and frontends using npm, pnpm,
Yarn 1/2+, or Bun. Planning resolves an exact commit, requires exactly the configured frontend
lockfile with no conflicting lockfile, and binds repository fingerprint, commit, dependency lock
digests and sizes, normalized build policy, Deployment runtime pins, required extensions,
observed platform capability, packaging version, and execution fingerprint into a versioned
`build_id`.

Apply repeats those checks, uses an owner-only derived workspace, and runs fixed frozen production
Composer semantics without scripts, then the selected package manager's fixed frozen install and
bounded configured build script. It rejects submodules, Git LFS pointers, alternate object
databases, repository substitution, unsafe paths/links/modes/ownership, special files, oversized
input, and insufficient space. The deterministic archive contains tracked immutable source and
production `vendor` metadata while excluding Git data, frontend dependencies, environment and
mutable/cache paths, and filesystem ownership. Safe extraction and a canonical immutable-tree
digest are verified before encrypted upload; archive bytes are read back and checksummed on the
Target, and the private provenance manifest is written last with first-writer-wins semantics.

An identical repeat returns `idempotent`; different verified bytes for an existing `build_id`
return `non_reproducible_build` without another authoritative manifest. Inventory never returns
bucket keys, object versions, raw manifests, commands, paths, output, or credentials. It reports
only bounded artifact identity, digest, publication time, and one of `ready`, `malformed`,
`foreign`, `missing`, `unsupported`, or `checksum_invalid`. Inventory does not deploy artifacts or
delete store objects.

For an artifact-mode Deployment, `plan_deployment` recomputes the expected `build_id` and asks the
destination Target's reader identity to resolve the authoritative manifest and exact package
version. The content-addressed plan binds those opaque versions, archive and immutable-tree
digests, source commit, provenance summary, reader credential versions, destination PHP and
extension evidence, and the existing release effects. Absence is an inspectable `ready: false`
plan with `artifact_missing`; it never starts a build.

`apply_deployment` repeats that resolution and runtime preflight, rejects a stale plan, and sends
only the reviewed versions to the Target. The Target verifies store encryption, manifest schema
and ownership, provenance, size, and archive digest; extracts with fixed path, link, type, count,
and size rules; recomputes the canonical tree digest; and writes readonly secret-free release
metadata before mutable release tasks. The normal shared paths, environment activation, Laravel
optimization and migrations, candidate/live health gates, symlink activation, automatic live
rollback, worker refresh, and retained-release cleanup then apply. Artifact destinations execute
no Git checkout, Composer install, Node/package-manager install, or frontend build. Explicit
rollback uses the same content-addressed retained-release verification described below.

Artifact promotion is also content-addressed. `plan_promotion` reads the source Deployment's fixed
`current` release metadata, verifies its readonly shape and canonical immutable-tree digest, and
reconstructs the destination-context build identity without contacting the Build Target or Git.
The plan is ready only when the destination yields the same `build_id`, uses the same Application
and release contract, can read the exact manifest/package versions, and proves matching PHP,
extension, OS, and architecture capability. Health and process declarations are compared through
secret-free canonical digests. An incompatible plan returns only `artifact_incompatible` and the
fixed action `publish_destination_context_artifact_first`.

`promote_deployment` re-reads the live source metadata and all destination evidence under both
Deployment locks. It materializes through the same safe artifact path and records the destination
commit only after activation, live health, and managed-process refresh succeed. Failure leaves the
desired destination source and prior live release unchanged. Promotion never invokes the Build
Target, Git, Composer, Node, or a frontend build.

Optional build-only secrets are safe environment-name mappings to bounded local-SOPS references.
They are resolved only after plan acceptance, transferred through protected temporary files, and
available only to the fixed frontend install and build processes; Deployment secrets and Resource
credentials never enter the build. Gimme removes credential material before packaging and scans
the selected tree plus final archive for every exact non-empty secret value. A match fails with
the fixed `secret_leak_detected` outcome before publication. Secret values and references never enter
`build_id` or provenance; the manifest records only whether build secrets were used and their
bounded count. A secret-induced output difference is therefore rejected by the existing
`non_reproducible_build` rule.

### Artifact Rollout preparation and traffic

`plan_start_rollout` is limited to artifact-mode staging and production Deployments. It reads the
verified live stable release, resolves the different exact artifact implied by current desired
source, verifies release contracts and destination runtime capability, and requires one free
Target slot. The plan exposes only bounded artifact identities and fingerprints. Apply first
persists a `preparing` generation and its temporary reservation, then uses the existing verified
artifact materializer in a generation-derived tree. It links only the Deployment's declared
shared environment/storage and provisions a separate PHP-FPM pool, socket, loopback-only Caddy
backend, runtime directory, and logs. No Git, dependency installation, frontend build, database
migration, public route change, worker, Horizon, or scheduler action occurs.

Candidate probes address that loopback backend directly, bypassing public routing and affinity.
Success records `active` with immutable weights `100/0`, a ready backend, and `stable` as the sole
background-process owner. Failure records `degraded`; the same `start_rollout` generation can be
planned and retried idempotently. `inspect_rollout` and the Rollout resource expose the safe state.
Ordinary deploy, promotion, rollback, Deployment removal, and Deployment update remain blocked
while either an active or recoverable Rollout exists. Later rollout slices own traffic shifting,
completion, reversal, and cleanup; preparation cannot assign candidate traffic.

`plan_rollout_weights` / `apply_rollout_weights` move an active, ready generation between reviewed
integer weights from 0 through 100 that total 100. Apply directly probes stable and candidate,
atomically installs only derived Gimme Caddy policy, and verifies both direct backends and the
public route. Desired weights change only after verification. A validation, reload, or health
failure restores the exact prior route and verifies stable service.

The Target owns a generation-scoped signing key that never enters desired state, MCP responses,
journals, logs, diagnostics, or errors. New cookie-accepting clients receive a Deployment-derived
cookie with `Secure`, `HttpOnly`, `SameSite=Lax`, and `Path=/`. Valid cohorts remain sticky while
their backend has nonzero weight and is healthy; a zero-weight backend is omitted and cannot be
selected by an old cookie. Caddy may temporarily exclude an unhealthy backend, but it never changes
desired weights or promotes a release. Connection retries are restricted to GET and HEAD.

Weights govern new cookie-accepting cohorts, not a guaranteed instantaneous percentage of global
requests. Existing cohorts and clients that reject cookies can make the observed request ratio
differ from configured weights. Inspection reports desired weights, affinity generation,
eligibility, bounded health, route fingerprint, phase, and safe drift without exposing backend
addresses, bodies, cookies, request samples, or secrets.

Completion requires a healthy candidate at `0/100`. `plan_complete_rollout` /
`complete_rollout` copy that exact verified artifact into an ordinary retained release, switch
`current`, reconcile workers/Horizon/scheduler to the promoted release, restore the ordinary public
route, verify public health, rotate affinity, and retire the temporary backend. The temporary slot
is released only after the Target reports `completed`. A failed transaction restores the prior
current release, process owner, route, release inventory, and capacity reservation.

`plan_reverse_rollout` / `reverse_rollout` may restore a `preparing`, `active`, or `degraded`
generation. Reversal requires verified stable service, restores the ordinary stable route and
background ownership, rotates affinity, retires the candidate, and releases the slot only after
the Target reports `reversed`. Both operations persist `completing` or `reversing` before remote
mutation; a matching terminal Target record lets an interrupted caller finish local convergence
without duplicating releases, routes, processes, keys, or reservations. Missing or mismatched
generations fail closed, and Target loss retains placement and capacity.

No Rollout operation runs migrations. While a Rollout remains recoverable, built-in
schema-changing Artisan commands are rejected; other explicitly allowlisted commands run against
the stable `current` release and serialize under the Deployment lock. Ordinary Deployment,
runtime, Resource, secret, and process reconciliation remains blocked unless it can preserve both
revisions transactionally.

Artifact rollback uses `plan_rollback_deployment` and `rollback_deployment` with the exact plan ID
and displayed confirmation. It revalidates retained readonly metadata, the canonical tree,
runtime compatibility, candidate/live health, and process refresh without contacting Git or the
Build Target. Artifact-sensitive public projections omit credential references, build-secret
names/references, raw storage object identities and versions, builder paths, commands, manifests,
and output. See the
[operator guide](../how-to/use-application-artifacts.md) and
[acceptance evidence](application-artifact-evidence.md).

## Managed AWS RDS PostgreSQL resources

See [`use-aws-rds-postgresql.md`](../how-to/use-aws-rds-postgresql.md) for prerequisites,
the IAM roles, and the provisioning and binding workflow.

`register_resource`, `plan_update_resource`, and `update_resource` also accept an AWS
RDS PostgreSQL resource (provider `aws_rds_postgres`): an exact engine version, instance
class, allocated storage, an AWS Network (VPC, exactly two private subnets), an
Administration Target, fixed security groups, and the AWS Secrets Manager store that
holds workload credentials. Registration makes no AWS calls; it is a local desired-state
write like every other Resource.
Updates are validated locally against the ADR 0008 allowlist and refused with a fixed
`aws_rds_update_forbidden_<field>` code for a changed `aws_network`, engine major version,
decreased storage, and the other changes listed in the how-to; a refused update changes nothing.

The same tools accept an AWS ElastiCache Valkey resource (provider `aws_elasticache_valkey`,
[`use-aws-elasticache-valkey.md`](../how-to/use-aws-elasticache-valkey.md)).
`plan_apply_resource` and `apply_resource` create its replication group, and
`inspect_resource` reports secret-free `phase`, `status`, `engine_version`,
`effective_durability`, fixed `issues` codes, and `drift`, never an endpoint or identifier.
`apply_resource` on an existing group makes one modification of only the differing same-major
`engine_version`, `node_type`, snapshot, and maintenance fields, and refuses anything else with
a fixed `aws_elasticache_modify_forbidden_<reason>` code before any change. A bound Deployment
receives the fixed `laravel-cluster-v1` contract and is probed before its release switches
(see the how-to). Updates are refused locally with a fixed
`aws_elasticache_update_forbidden_<field>` code for `aws_network`, an engine major version, or
`security_group_id`, and registering or updating to a `node_type` the account does not offer is
refused with `aws_elasticache_node_type_unavailable`.

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_apply_resource` | Read | Plan creating one managed instance or converging an existing one; makes no AWS call |
| `apply_resource` | Remote write | Create the RDS instance, or converge an existing one with one immediate modification, without returning a credential |
| `inspect_resource` | Remote read | Live secret-free provider identity, health, and version through the inspection role, plus allocations and, after a successful live read, `drift` against desired state; falls back to the last observed state with a bounded `refresh_error` and no drift |
| `plan_bind_resource` | Read | Plan creating a deployment's isolated database, role, and workload secret, and its Valkey ACL user, namespace, and credential |
| `bind_resource` | Remote write | Create or reconcile the binding; never returns the workload credential |
| `plan_cleanup_resource` | Read | Plan local resource removal |
| `apply_cleanup_resource` | Local write | Remove local registration after exact confirmation |
| `plan_destroy_resource` | Read | Plan destroying a managed ElastiCache Valkey Resource and its data; reads only local state and never assumes the destructive role |
| `apply_destroy_resource` | Remote write (destructive) | Delete the replication group with a final snapshot and what Gimme created around it, through the Provider Account's destructive role, after exact confirmation `DESTROY RESOURCE <name>` |
| `plan_purge_final_snapshot` | Read | Plan deleting only the deterministic final snapshot retained after a destroyed Valkey Resource; reads a local receipt only |
| `apply_purge_final_snapshot` | Remote write (destructive) | Delete that exact final snapshot through the destructive role after exact confirmation `PURGE FINAL SNAPSHOT <name>` |
| `plan_purge_retained_secrets` | Read | Plan deleting only Gimme-owned Valkey credentials recorded after a destroyed Resource |
| `apply_purge_retained_secrets` | Remote write (destructive) | Verify and delete those exact credentials through the destructive role after exact confirmation `PURGE RETAINED SECRETS <name>` |
| `list_resource_snapshots` | Remote read | List a managed Valkey Resource's snapshots, including the final snapshot of a destroyed group, by name and status only |
| `plan_restore_resource` | Read | Plan re-creating a lost managed Valkey replication group from one of its snapshots; reads only local state |
| `apply_restore_resource` | Remote write | Create the group from the snapshot only if absent, restore each recorded Deployment credential unchanged, and verify every Deployment before the Resource is `ready`; repeat the same call while `restoring` |
| `plan_recreate_empty_resource` | Read | Plan replacing a lost managed Valkey replication group with an empty one; reads only local state |
| `apply_recreate_empty_resource` | Remote write (destructive) | Create an empty group in place of a lost one after exact confirmation `RECREATE EMPTY RESOURCE <name>`, then verify each Deployment as a restore does |
| `plan_rotate_resource_credential` | Read | Plan replacing one Deployment's Valkey ACL user and Resource Credential; reads only local state |
| `apply_rotate_resource_credential` | Remote write (destructive) | Rotate with a probed switch, rollback on failure, and deletion of the previous user through the destructive role; never returns a credential |
| `plan_forget_resource` | Read | Plan deleting a Retained Resource tombstone |
| `apply_forget_resource` | Local write | Delete the tombstone after exact confirmation `FORGET <name>`; the retained infrastructure is untouched |

`apply_resource` creates the instance with `ManageMasterUserPassword=True` so the master
credential is generated and stored by AWS, never by Gimme, and polls for at most 30
seconds before returning a bounded `pending` phase; a later call resumes by describing
the existing instance rather than recreating it. For an existing `available` instance it sends
one `ModifyDBInstance` with `ApplyImmediately` and only the changed fields (same-major engine
version, instance class, increased storage, security groups, and the Resource-owned parameter
group), never a major upgrade, and reboots once without forced failover when the parameter
group is `pending-reboot`; a storage decrease, version downgrade, or major mismatch is refused
before any change, and the response lists `modified_fields` and `rebooted`. `bind_resource` requires the resource to
already report `phase: ready`; it resolves the master credential through the account's
distinct resolver role only at apply time, creates or reconciles the deployment's isolated
database and role through the Administration Target over `psql` with `verify-full` TLS
against a pinned AWS trust bundle (`us-gov-*` and `cn-*` regions are refused when registering,
planning or applying a managed Resource), and stores a
generation-1 workload credential as a tagged Secrets Manager secret — the response
contains only the `{store, secret}` reference. Workload credential rotation, Detached
Allocation rebind is not implemented yet. A tombstone is deleted only by
`apply_forget_resource`, which never touches AWS.

Runtime wiring of a managed database into a Deployment is not implemented yet, so a
Deployment whose database binding is a managed Resource is fenced off rather than half
working: `plan_deployment_resources` reports a readiness issue (blocking
`apply_deployment_resources`), `plan_deployment` refuses, and Recovery Points reject it,
because those tasks assume a target-local PostgreSQL. Only target-local Resources are sent
to the Deployer recipe.

`apply_cleanup_resource` is non-destructive by default: a managed AWS RDS resource is
left running with its data intact, and Gimme instead writes a secret-free Retained
Resource tombstone recording the resource's AWS Network so it can be re-adopted later;
only the local registration is removed. Destructive RDS instance deletion is a deliberately
separate, not-yet-implemented capability; an ElastiCache Valkey Resource can be destroyed with
`plan_destroy_resource` and `apply_destroy_resource` (see its how-to). Cleanup is refused while any Deployment still
references the resource.

## Target and runtime tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `inspect_target` | Remote read | Inspect OS, services, helpers, TLS, SSH agent, sites, and global tools |
| `plan_target_stack` | Remote read | Resolve APT candidates and helper readiness |
| `apply_target_stack` | Remote write | Reconcile packages, services, sites, and mDNS through the installed helper |
| `plan_deployment_runtimes` | Read | Show exact runtime pins, mise version, and required PHP extensions |
| `apply_deployment_runtimes` | Remote write | Install declared mise pins and verify every runtime/resource version |

The MCP server never accepts a sudo password. Run `uv run gimme-bootstrap-target
<target>` in a terminal when a target plan reports `bootstrap_required`.

## Deployment lifecycle tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_deployment_resources` | Read | Plan routing, database/cache identities, environment, secrets, and processes, and for a managed Valkey binding the secret-free contract |
| `apply_deployment_resources` | Remote write | Reconcile the exact resource plan and return only a bounded outcome after secret resolution |
| `plan_deployment` | Remote read | For source mode, resolve one commit; for artifact mode, resolve exact reviewed object versions and digests or report `artifact_missing`; verify runtime compatibility and render the Deployer graph |
| `apply_deployment` | Remote change | Deploy the exact reviewed source revision or materialize the exact reviewed artifact with health gates |
| `list_releases` | Remote read | List retained releases and the current release |
| `plan_rollback_deployment` | Remote read | Select and verify one exact retained predecessor with runtime, health, and process evidence |
| `rollback_deployment` | Remote change | Apply the exact reviewed rollback after exact confirmation |
| `plan_promotion` | Remote read | Pin the live source commit or verify and pin one compatible live artifact for another Deployment |
| `promote_deployment` | Remote change | Deploy the reviewed commit/artifact and record its exact commit only after success |
| `plan_remove_deployment` | Read | Plan route, process, data, and release cleanup |
| `remove_deployment` | Remote change | Perform exact confirmed cleanup and remove local registration |

Each named health probe explicitly selects `candidate`, `live`, or both phases. Candidate
probes run before the `current` symlink switch. Live HTTPS probes run afterward and
restore the previous release if any live probe fails. Applications and deployments can
add probes to the primary inherited or overridden health definition.
For deployments with Horizon, queue workers, or a scheduler, planning also verifies the
content-bound privileged process helper and required PHP process extensions. Apply is
blocked before deployment when that preflight reports `bootstrap_required` or a missing
extension.

Rollback is content-addressed in both release modes. Planning reads the bounded Deployer
inventory, selects the first valid retained predecessor, and binds current and target release
names and identities, the complete inventory digest, runtime and OS/architecture evidence,
health/process contracts, and the execution fingerprint. Artifact planning additionally validates
both readonly metadata documents and recomputes both immutable-tree digests; source planning binds
exact retained commits. It never repairs, rebuilds, downloads, or checks out a missing or damaged
release.

Apply requires the exact current `plan_id` and `ROLLBACK <deployment> TO <release>`. It recomputes
the plan under the Deployment lock, then the Target independently checks the reviewed inventory
and metadata hashes immediately before activation. The retained release regenerates only fixed
Laravel environment-derived caches, passes candidate health, switches atomically, passes live
health, and refreshes managed processes. A failure after the switch restores the former live
symlink and process state. Results contain bounded release identity and hashes, never paths,
object identities, raw metadata/output, commands, response bodies, or credentials.

### Secret planning and resolution

Deployment secrets are structured `{store, secret, field}` references. The fixed
`local-sops` store accepts no path and always uses `secrets.enc.json` in the state
directory. AWS stores derive a complete name from a registered prefix and require the
derived `gimme:secret-store=<store-name>` ownership tag.

Planning reads SOPS encrypted structure or calls AWS `DescribeSecret` through the
inspection role. Returned plans contain environment keys and reference/version
fingerprints, not ciphertext, AWS version IDs, ARNs, provider errors, or values. Apply
revalidates metadata, obtains the exact reviewed version through the distinct resolver
role, resolves every field before target mutation, and sends values only through protected
temporary material. Each field is limited to 8 KiB, the combined payload to 64 KiB, and a
Deployment to 128 secret environment keys. See
[`use-aws-secret-stores.md`](../how-to/use-aws-secret-stores.md) for IAM, KMS, rotation,
migration, and failure behavior.

### Backup destinations and Recovery Points

A Deployment opts into recovery by setting `recovery.destination` to one registered
Backup Destination name through the existing `plan_update_deployment` /
`update_deployment` pair; a bound database resource is required. PostgreSQL is always
selected. Valkey requires both an explicit `recovery.valkey: true` and a bound Valkey
Resource; `quiesce_wait_seconds` defaults to 30 and is bounded from 1 through 300.
On-demand Recovery Points are created and listed with:

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_create_recovery_point` | Read | Plan one on-demand Recovery Point and its selected components |
| `create_recovery_point` | Remote + destination write | Quiesce when required, capture, upload, verify, restore runtime, and publish one Recovery Point |
| `plan_delete_recovery_point` | Destination read | Resolve one immutable manifest and plan exact-version deletion without exposing storage identities |
| `delete_recovery_point` | Destination write | Delete reviewed component versions and the exact manifest version last |
| `get_recovery_schedule_status` | Remote read | Read bounded timer and latest-attempt status without raw systemd or runner output |
| `list_restores` | Destination read | List authoritative, secret-safe Restore records newest first |
| `plan_restore_deployment` | Destination + remote read | Default to every manifest component or normalize an explicit `postgres`/`valkey` selector; return full/partial semantics, exact effects, compatibility, and confirmation without mutation |
| `apply_restore_deployment` | Remote + destination write | Apply the exact reviewed selector under request-owned maintenance; protect matching current components, verify PostgreSQL in a shadow, replace only the registered Valkey prefix, and swap PostgreSQL last for full Restore |
| `plan_verify_restore` | Destination read | Plan private application and managed-process verification for a data-replaced Restore |
| `apply_verify_restore` | Remote + destination write | Resume managed processes behind maintenance, verify database connectivity and configured live-health probes privately, re-quiesce on failure, and restore routing only after retry-safe cleanup and final verification |

`create_recovery_point` takes a caller-supplied `request_id`; the Recovery Point's
identity is derived from `(deployment, destination, request_id)`, never from wall-clock
time. Re-applying the same `plan_id`/`request_id` after a prior success is a deterministic
no-op that returns the already-published manifest without re-running `pg_dump` or
re-uploading. `pg_dump` runs on the Deployment's Target with `--no-owner --no-privileges
--no-acl` (roles, ownership, and ACLs are never captured). On-demand and scheduled capture
both invoke the installed content-bound Target runner and the same capture, upload,
verification, idempotency, and retention implementation. The component upload is verified
against its declared SHA-256 before the immutable Recovery Manifest is published.
For a Valkey-inclusive point, only the selected Deployment route and registered managed
writers are quiesced; a binary-safe bounded scan captures only its registered prefix with
absolute expiry timestamps. Normal runtime is restored after every component upload is
verified and before the sole manifest is published. A failed or partial capture or upload
publishes no manifest and is cleaned up rather than left dangling. `list_recovery_points`
reads manifests directly from the bound destination — authoritative even if the owning
Target is gone — and rejects (without failing the whole call) any manifest whose
referenced component object no longer matches its declared checksum.

Manual deletion is separate from automatic retention. `retain_last` is the automatic-pruning
ceiling for verified, unprotected points after a verified replacement exists; it does not
prevent an operator from manually reducing inventory below that number. On-demand success,
including an idempotently verified retry, deletes eligible points oldest-first through the
same exact-version primitive. Protected Safety points and active Restore sources do not count
toward the ceiling and are never candidates. Pruning stops on its first failure without
invalidating the replacement. Deleting the final verified point manually requires both exact
confirmations returned by the plan.

A Deployment Recovery Policy normalizes `retain_last` to 7 by default (accepted range 1–365)
and accepts exactly one UTC cadence shape: `{kind: manual}`, `{kind: hourly, minute: 0}`,
`{kind: daily, hour: 2, minute: 0}`, or
`{kind: weekly, weekday: sun, hour: 2, minute: 0}`. The shown clock fields are defaults;
hour is 0–23 and minute is 0–59. Arbitrary time zones, seconds, cron expressions, and extra
calendar fields are rejected. Non-manual resource apply reconciles one persistent UTC systemd
timer with a stable 0–300 second Deployment-derived delay. Each activation selects only the
latest missed logical slot and derives its request identity from the normalized policy and slot.
Manual cadence and Deployment removal delete the timer, authority, status, and scheduled
credentials.

`plan_deployment_resources` includes a content-addressed `recovery_schedule` projection when a
Recovery Policy is bound. It shows enabled/manual state, normalized cadence and UTC calendar,
stable Deployment delay, policy and complete authority fingerprints, ambient/stored auth mode,
and fixed service/timer identities. The private authority fingerprint also binds immutable
placement, selected Resource names/providers/kinds/versions, destination execution policy, and
status identity. Secret references, credential filenames, and resolved credentials are omitted.

`gimme://deployments/{name}/recovery-schedule` and `get_recovery_schedule_status` expose
the same bounded projection. Manual cadence reports a locally known disabled timer without
contacting the Target. A scheduled cadence queries only its Deployment-derived timer and returns
fixed `status_unavailable` fields when the Target or observation is unavailable. The projection
contains normalized cadence, logical/effective next UTC time, bounded timer state, the latest
attempt fields, Recovery Point identities, and retention counts; it never includes raw systemd
properties, unit contents, commands, paths, provider responses, or logs. A missing timer is
reported explicitly. Scheduled status is observed evidence; S3 manifests remain authoritative.

The runner waits at most five minutes for the Deployment operation lock. A busy attempt records
`deployment_busy` and performs no capture or pruning. Fixed attempt outcomes are `succeeded`,
`backup_succeeded_retention_failed`, `deployment_busy`, `policy_stale`,
`credentials_unavailable`, `credentials_expired`, `destination_unavailable`, `capture_failed`,
`verification_failed`, `retention_failed`, and `status_unavailable`. A successful point remains
valid when retention fails, and a later successful capture retries pruning.

Ambient Backup Destination authentication persists no credential. `secret_refs` are resolved
only during resource apply, transferred through protected temporary files, installed root-owned,
and delivered with systemd `LoadCredential`. Reapply resources to rotate stored or session
credentials. Switching to ambient auth or manual cadence removes the stored scheduled
credential. The runner never refreshes session credentials itself.

Deletion accepts only a registered Deployment and Recovery Point ID. The private manifest
supplies every object key and exact S3 version; callers cannot provide a key, prefix, path,
or version. Components are deleted and verified one at a time, with the exact manifest
version last. A partial failure remains visible as `deletion_failed`; retry the original
apply with the same plan and confirmations. Safety points remain protected until their
authoritative Restore record is `completed`; source Recovery Points are likewise protected
while any Restore using them is incomplete. Object Lock or legal hold is never bypassed.
Provider protection and access failures return only fixed `recovery_point_object_protected`
or `recovery_point_deletion_denied` outcomes; transport, malformed-response, deletion, and
verification failures collapse to fixed `recovery_point_deletion_failed`.

Restore transitions are append-only, immutable objects in the bound Backup Destination.
`list_restores` and `gimme://deployments/{name}/restores/{request_id}` expose only the
Deployment and request identities, source Recovery Point identity, selected and untouched
component kinds, full/partial semantics, destination provider/kind/version, current bounded
state, timestamps, event count, and Safety Recovery Point identity.
Storage identities, database identities, paths, SQL, endpoints, and credentials remain private.

`plan_restore_deployment` is read-only. An omitted component selector chooses every component
in manifest order. An explicit selector is a non-empty, duplicate-free subset of `postgres`
and `valkey`, also normalized into manifest order. Selecting a strict subset is visibly partial
and its stronger confirmation states that consistency with untouched components is intentionally
broken. Plans expose only bounded destination Resource provenance for selected components. Valkey
planning requires the registered destination to have a supported provider and `valkey` kind, then
compares its exact version with the source component version.
An opaque request fingerprint binds the private source manifest, normalized selector, every
selected destination binding, recovery policy, immutable placement, and execution fingerprint.
Restore Records retain that fingerprint, the exact Safety component set, and all bounded
destination provenance; reuse after any
bound identity changes fails closed without exposing private manifest or placement values.
The state machine supports full PostgreSQL-and-Valkey Restore plus either explicit partial
selection. Valkey-only Restore captures a Valkey-only Safety Recovery Point, validates the source
archive before mutation, replaces and verifies only the registered prefix, and performs no
PostgreSQL mutation or cleanup. Full Restore protects each non-empty selected destination (and any
destination whose emptiness is ambiguous), prepares and verifies the PostgreSQL shadow first,
replaces and verifies Valkey second, and swaps PostgreSQL last. Thus an empty PostgreSQL database
paired with an ambiguous Valkey prefix produces a Valkey-only Safety point without changing the
full-Restore selector.
Valkey-only planning resolves PostgreSQL binding provenance but does not run PostgreSQL catalog
inspection or inspect its data.

For PostgreSQL selection, planning compares the source and current target-local PostgreSQL
versions exactly and runs one fixed catalog inspection to
classify the current database as `empty` or `nonempty`. Tables, partitioned tables, views,
materialized views, sequences, foreign tables, routines, user-defined composite/domain/enum/
range types, and non-baseline extensions all make the database non-empty; an ambiguous or
failed inspection fails closed. The plan contains no database name. Its exact confirmation names
the selected components; partial confirmation also names untouched components and the deliberate
consistency break. Planning also checks bounded free-space readiness on the controller temporary
directory, Target application filesystem, and PostgreSQL data filesystem. Insufficient capacity
returns `restore_capacity_insufficient` before maintenance or mutation.
`apply_restore_deployment` advances the destination-authoritative Restore record through
maintenance, Safety Recovery Point protection (when the destination was non-empty), exact
artifact verification, shadow verification, and atomic data replacement. A failed or successful
data swap remains behind the fixed maintenance route; the separate verification apply is the only
path back online.
If required Safety capture fails, Restore performs no source mutation, attempts to restore the
original runtime, and appends `safety_failed`. A matching retry re-enters request-owned maintenance
and repeats the complete Safety capture. Failure to restore the runtime instead returns the fixed
`recovery_runtime_restore_failed` code while retaining that recovery-required record state.
Artifact materialization and PostgreSQL shadow preparation are also pre-mutation boundaries. Their
failures append `artifact_failed` or `shadow_failed`, restore the original runtime, and allow the
same request to re-enter maintenance and resume from the last authoritative safe phase. Failures
after destination mutation can begin remain behind maintenance for verification or retry.

`apply_verify_restore` boots each configured live-health request directly through the current
Laravel application while the public Caddy route continues to return 503. It resumes only the
managed units recorded as active before maintenance. Any failed database, application, or process
check re-quiesces those units and appends `verification_failed`; a retry is required. After two
successful private checks around idempotent previous-database cleanup, it restores the saved route
and appends `completed`.

One reentrant, filesystem-backed Deployment operation lock covers Restore, Recovery Point capture,
deploy, runtime and resource reconciliation, binding and credential rotation, promotion, rollback,
removal, Deployment updates, and mutating Artisan work. Multi-Deployment and shared-Resource
operations acquire Deployment names in sorted order, preventing both interleaving and lock-order
deadlocks.

The request-owned maintenance helper also supports internal `resume` and `quiesce`
transitions for Restore verification. `resume` starts only the managed units recorded as
active before maintenance while leaving the public route on the fixed 503 response;
`quiesce` stops and verifies all registered managed units again after a failed private
check. Neither transition is exposed as a general-purpose MCP maintenance switch. Final
`exit` restores the normal route only after the appropriate processes are active.

## Application operation tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_artisan` | Read | Plan a structured, allowlisted Artisan command |
| `run_artisan` | Remote change | Run the exact reviewed Artisan invocation |
| `deployment_process_status` | Remote read | Inspect queue, Horizon, and scheduler systemd units |
| `diagnose_deployment` | Remote read | Run fixed secret-safe release, Laravel, database, filesystem, FPM, log-metadata, and configured live-health checks |
| `target_service_status` | Remote read | Inspect `postgresql`, `valkey-server`, or `caddy` |

## Runtime providers

| Provider | Meaning |
| --- | --- |
| `system` | Verify an exact host binary version |
| `mise` | Install and execute an exact version under `<apps_root>/.gimme/mise` |
| `bundled` | npm supplied by the selected Node.js; invalid for every other runtime |

PHP and Composer currently require `system`. Caddy and managed Laravel processes use
the PHP pin's `/usr/bin/phpX.Y` and `/run/php/phpX.Y-fpm.sock`. Node.js, Bun, pnpm,
Yarn, Python, Ruby, Go, and Java can use mise.
