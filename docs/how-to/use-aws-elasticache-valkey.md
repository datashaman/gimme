# Use AWS ElastiCache for Valkey as a managed Resource

[ADR 0009](../adr/0009-synchronously-durable-aws-valkey.md) describes managed Valkey on AWS
ElastiCache: one shard with one cross-AZ replica, cluster mode, TLS, encryption at rest, and
synchronous durability. This page describes what is implemented today. The provider is not
finished: see the list below.

## Current scope

Implemented: registering the Resource, provisioning one replication group (with its subnet
group, parameter group, user group, and administrative user), reviewed updates and drift,
live inspection with fixed readiness codes, typed Deployment bindings with a per-Deployment ACL
user, namespace, and credential, the `laravel-cluster-v1` application contract with its
pre-switchover probes, retention-by-default removal, forgetting a retained tombstone,
destruction with a separate destructive role, snapshot inventory, restore from a snapshot (or
recreation of an empty group) after a group is lost, and per-Deployment credential rotation.

The disposable Laravel test suite the ADR calls for (cache, session, queue, and
Horizon driven through a real Laravel application, and ACL denials seen from it) does not
exist yet; the probes below use the Redis protocol directly.

The AWS calls have only been exercised against botocore stubs. Nothing here has run against a
live account, so the IAM statements (destructive role included) and the ACL access strings below
are unverified.

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

The inspection role gains two statements, plus one more for snapshots and restore (below) (replace `<region>` and `<account>`). Neither has a
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

Snapshot inventory and restore need the inspection role to read snapshots and to name one when it
creates a group from it. `CreateReplicationGroup` with `SnapshotName` is authorized against the
snapshot as well as the group, and the snapshot of a destroyed group is not named `gimme-*` unless
it is Gimme's own final snapshot, so this statement covers snapshots of `gimme-*` groups only:

```json
{
  "Sid": "ElastiCacheSnapshotsForRestore",
  "Effect": "Allow",
  "Action": ["elasticache:DescribeSnapshots", "elasticache:CreateReplicationGroup"],
  "Resource": ["arn:aws:elasticache:<region>:<account>:snapshot:gimme-*"]
}
```

`DescribeSnapshots` may not support resource-level permissions and then needs `"Resource": "*"`.
The resolver role already reads the workload namespace, which a restore uses to recreate an ACL
user from the credential already stored, and rotation reads the same secret. Whether these
statements are sufficient is unverified.

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

The **destructive role** is a third, optional role on the Provider Account
(`destructive_role_arn`, in the same account and distinct from the other two). It is assumed only
while applying a confirmed destruction, never while planning, registering, updating, or
inspecting, so registering it does not test it. Give it only these deletes, and let only a person
assume it (for example behind an MFA condition):

```json
{
  "Sid": "ElastiCacheDestroyGimmeOwned",
  "Effect": "Allow",
  "Action": [
    "elasticache:DeleteReplicationGroup",
    "elasticache:CreateSnapshot",
    "elasticache:DeleteUserGroup",
    "elasticache:DeleteUser",
    "elasticache:DeleteCacheParameterGroup",
    "elasticache:DeleteCacheSubnetGroup"
  ],
  "Resource": [
    "arn:aws:elasticache:<region>:<account>:replicationgroup:gimme-*",
    "arn:aws:elasticache:<region>:<account>:cluster:gimme-*",
    "arn:aws:elasticache:<region>:<account>:snapshot:gimme-*",
    "arn:aws:elasticache:<region>:<account>:usergroup:gimme-*",
    "arn:aws:elasticache:<region>:<account>:user:gimme-*",
    "arn:aws:elasticache:<region>:<account>:parametergroup:gimme-*",
    "arn:aws:elasticache:<region>:<account>:subnetgroup:gimme-*"
  ]
}
```

Privilege impact: this is the only Gimme role that can delete anything, and it can delete any
`gimme-*` ElastiCache object in the account, so Gimme's own checks (below) are the second line of
defense, not the only one. It has no Secrets Manager permission: destruction never deletes
secrets. Rotation deletes the previous ACL user with the same statement, so `DeleteUser` is used
for more than destruction. Whether the final snapshot needs `CreateSnapshot` on the snapshot resource, and whether
deleting a `default`-named user is allowed, are unverified.

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
a second one. Once AWS acknowledges the create request, Gimme records a local `provisioning`
operation before the first follow-up read. If that read is temporarily unavailable,
`inspect_resource` reports `phase: pending`, `operation: provisioning`, and a bounded refresh
error; repeat the same apply after AWS is reachable again.

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

Binding returns once ElastiCache accepts the change; the user group may stay `modifying` for a
short while, during which another binding fails with `aws_elasticache_user_group_bind_invalid_state`
and can simply be retried. The credential is proven at the next deploy by the activation
probes below.

Binding needs a `ready` Resource by a fresh live read, so a `degraded` one, including unsafe
security group drift, takes no new binding and existing Deployments keep running. Binding again
keeps the recorded credential and only re-applies the profile. A Deployment name that has no
recorded allocation but already has a user gets a fresh credential. Removing a Deployment Target
from `deployment_security_group_ids` while a Deployment on it is bound, and moving
`workload_secret_store` once credentials exist, are refused by `plan_update_resource`, and a
bound Resource cannot be removed.

## The Laravel contract and activation probes

A Deployment bound to a managed Resource receives the fixed `laravel-cluster-v1` contract. Gimme
never patches application source and accepts no paths: it writes protected `GIMME_VALKEY_*`
values to the shared `.env` and selects the Redis adapter for exactly the declared uses
(`CACHE_STORE`, `SESSION_DRIVER`, `QUEUE_CONNECTION`, and `HORIZON_PREFIX` with `queue`). A use the
Deployment did not declare is pinned to `file`, `file`, or `sync`, so the Target-local Redis
defaults can never be used. None of those keys, and nothing beginning `GIMME_VALKEY_`, can be
set through the Deployment's `variables` or `secrets`; state validation refuses it.

The application must read them in its own cluster-aware PhpRedis or Predis configuration:

| Key | Value |
| --- | --- |
| `GIMME_VALKEY_CONTRACT` | `laravel-cluster-v1` |
| `GIMME_VALKEY_HOST`, `_PORT` | the configuration endpoint; the client discovers the nodes |
| `GIMME_VALKEY_SCHEME`, `_CLUSTER`, `_VERIFY_PEER` | `tls`, `true`, `true` |
| `GIMME_VALKEY_READ_REPLICAS` | `false`; primary reads only |
| `GIMME_VALKEY_TIMEOUT_SECONDS`, `_RETRIES`, `_BACKOFF_MS`, `_BACKOFF_CAP_MS`, `_JITTER` | `2`, `3`, `100`, `2000`, `true` |
| `GIMME_VALKEY_USES` | the declared uses, comma-separated |
| `GIMME_VALKEY_CACHE_PREFIX`, `_SESSION_PREFIX`, `_QUEUE_PREFIX`, `_HORIZON_PREFIX` | the derived namespaces |
| `GIMME_VALKEY_USERNAME`, `_PASSWORD` | the Resource Credential, resolved from Secrets Manager at apply time |

The credential is read by the Provider Account's resolver role, so its `GetSecretValue`
permission must cover `<prefix>/<resource>/<deployment>` in the workload Secret Store (see
[AWS secret stores](use-aws-secret-stores.md)); a credential it cannot read fails
`apply_deployment_resources` with a bounded secret error.

`plan_deployment_resources` shows the contract (uses, namespaces, adapters, the fixed probe
names, a digest of the injected values) but never the endpoint or a credential, and reports
`valkey_resource_not_ready` or `valkey_binding_missing` until the Resource is ready and the
Deployment is bound; `plan_deployment` refuses in the same cases.

Before the release symlink switches, `deploy` runs a fixed probe program on the Target, ahead of
the candidate health check. It reads only the shared `.env` and the candidate's `composer.lock`,
takes no path or command from a caller, and stops at the first failure. The order is:

1. `environment`: the `.env` matches the planned contract and holds a credential.
2. `horizon-compatibility` (only when the Deployment runs Horizon): the locked `laravel/framework` is 13.5.0 or later and
   `laravel/horizon` 5.46.0 or later.
3. `tls`: the server certificate verifies against the Target's system trust store, with hostname
   checking. There is no way to disable it or to supply a certificate.
4. `auth` and `default-user`: the credential authenticates, and the default user cannot be used
   without one.
5. `cluster`: `cluster_state:ok` and exactly one shard serving slots 0 to 16383.
6. `read-after-write`: a write to the primary reads back. At most two `MOVED` redirects are
   followed, and only to a host in the configuration endpoint's own domain; anything else fails
   closed.
7. `namespace`: a key, a channel, and read-only administrative commands outside the namespace
   are all denied with `NOPERM`. No destructive command is ever sent to prove a denial.
8. `use-cache`, `use-session`, `use-queue`, and `use-horizon` (only with Horizon): a bounded
   write, read, and delete per declared use, including TTLs, counters, `SET NX`, lists, sorted sets, hashes, and a Lua script.
9. `cleanup`: the probe keys are removed.

Probe keys live under `<namespace>_probe:<random>:` with a 30 to 60 second TTL and are also
deleted on failure. Output is only `GIMME_VALKEY_PROBE|<check>|ready|failed|<code>`; credentials,
values, and server messages are never printed. A failed probe fails the deploy before the
symlink switches, so the current release stays live.

What this does not prove: the probes speak the Redis protocol directly, not through Laravel's
Redis adapters, and they have only run against a local TLS, cluster-mode, ACL-enforcing Redis,
not against ElastiCache. Whether real Laravel and Horizon traffic stays inside the `laravel-v1`
command profile is unverified.

## Destroy a Resource

`plan_destroy_resource` then `apply_destroy_resource` permanently deletes the replication group
and its data, and what Gimme created around it: the user group, the default, administrative, and
per-Deployment users, the parameter group, and the subnet group. Then it removes the local
registration. Nothing else changes, and there is no tombstone because nothing is retained.

Preconditions, all checked again at apply:

- the Provider Account has a `destructive_role_arn` (`aws_elasticache_destroy_role_missing`);
- no Deployment references the Resource, and the observation records no allocation for a
  Deployment that still exists (`aws_elasticache_destroy_bindings_remain`);
- the Resource has been provisioned and observed (`aws_elasticache_destroy_not_observed`; run
  `inspect_resource` to refresh a lost cache);
- the live group is the one that was planned: the same identity as observed when the plan was
  made (`aws_elasticache_destroy_identity_changed`, and a changed observation makes the plan
  stale), it carries this Resource's ownership tag (`aws_elasticache_group_ownership_mismatch`),
  and it is `available` or already `deleting` (`aws_elasticache_destroy_invalid_state`);
- the exact confirmation `DESTROY RESOURCE <name>`.

Planning reads only local state, and `plan_destroy_resource` lists what is destroyed and what is
retained. The group is deleted with a final snapshot named `<group>-final-<8 hex>`, derived from
the group's identity so a retry never creates a second one; a name that already exists fails with
`aws_elasticache_destroy_group_snapshot_exists`. **The final snapshot, manual snapshots, and the
workload secrets (the administrative secret and each Deployment's credential) are retained and keep
costing money until you delete them.** A later Resource of the same name reuses and overwrites those
secrets.

Deleting a group takes minutes, so apply polls for 30 seconds and returns `phase: deleting`. Repeat
the same call, with the same plan, to continue: it deletes nothing twice, then removes the
dependents in dependency order, reading each object's `gimme:resource` tag with the inspection role
before deleting it with the destructive role (`aws_elasticache_destroy_ownership_mismatch` stops the
sequence for an object that is not this Resource's). An object that is already gone counts as done.
ElastiCache deletes the user group asynchronously; if it has not released its users yet, apply returns
`phase: waiting_for_user_group`. `inspect_resource` reports the same phase under `progress`; repeat
the same confirmed call once AWS has finished the user-group deletion.
A user whose binding crashed before its allocation was recorded is not known to Gimme and is left;
delete it yourself. While a destruction is in progress the Resource cannot be provisioned or bound again
(`aws_elasticache_destroy_in_progress`). A failure part-way leaves the registration and the
progress marker in place, and the same call resumes. `apply_cleanup_resource` abandons a stuck
destruction and retains what is left.

## Recover a lost group

If a replication group disappears from AWS (deleted by hand, or the account lost it) while Gimme's
observation says it existed, `apply_resource` refuses with
`aws_elasticache_group_missing_replace_explicitly` rather than quietly creating an empty group. Two
explicit ways forward exist, and neither needs the destructive role. Both keep every Deployment's
existing Resource Credential: the ElastiCache user group and any missing ACL user are created
again from the credential already in the Secret Store, and no credential is generated or rotated.

- `list_resource_snapshots` shows the snapshots of the group, by name and status. Then
  `plan_restore_resource` and `apply_restore_resource` create the group from one of them. A
  snapshot of a destroyed group also works (its final snapshot), in which case no allocations
  remain and Deployments bind again.
- `plan_recreate_empty_resource` and `apply_recreate_empty_resource` (confirmation
  `RECREATE EMPTY RESOURCE <name>`) accept the loss of the data and create an empty group.

Apply checks, before creating anything, that the group is absent (`aws_elasticache_restore_group_exists`),
that the snapshot belongs to this group (`aws_elasticache_restore_snapshot_missing`), is `available`
(`aws_elasticache_restore_snapshot_unavailable`), and was taken on an engine no newer than the
Resource's `engine_version` (`aws_elasticache_restore_engine_older`). Plans read only local state and
are identical before, during, and after an interrupted apply.

Restoring takes minutes, so apply polls for 30 seconds and returns `phase: restoring`. The Resource
then stays `restoring` (shown by `inspect_resource`, with `operation: restoring`) and refuses
provisioning, binding, rotation, and destruction (`aws_elasticache_restore_in_progress`), and no
Deployment is handed the new endpoint by a deploy, until each recorded Deployment has passed a
verification: its environment is refreshed to the new endpoint, the activation probe runs against its
current release (a Deployment that was never deployed skips the probe), and its workers restart.
Repeat the same call to continue. A failed verification raises
`aws_elasticache_restore_verification_failed` with the progress kept (`inspect_resource` shows
`progress.verified` and `progress.failed`), so the repeat re-verifies only the Deployments that did
not pass and never creates the group again. An allocation whose Deployment no longer exists still
has its ACL user restored but has nothing to verify. Only when every Deployment has
passed does the Resource become `ready`. A group that fails to create
(`aws_elasticache_restore_create_failed`) ends the restore. Once no Deployment references the
Resource, `apply_cleanup_resource` abandons a restore that cannot finish and retains what exists.

Verification takes the same per-Deployment lock as `apply_deployment_resources`, but nothing stops
a deploy of a bound Deployment from running alongside a restore or rotation. Do not deploy one
while its Resource is `restoring` or being rotated.

## Rotate a Deployment credential

`plan_rotate_resource_credential` and `apply_rotate_resource_credential` replace one Deployment's
ACL user and Resource Credential. The Provider Account needs the destructive role, because the
previous user is deleted (`aws_elasticache_destroy_role_missing`). The plan reads only local state.

Apply never leaves the credential in use invalid:

1. It records a `rotating` marker, then creates the next generation's ACL user (a new user id and
   username, with the same access string) and adds it to the group's user group. It never adopts a
   user that already exists (`aws_elasticache_rotate_candidate_exists`, which touches nothing).
2. It writes the new credential as the secret's current version, keeping the previous version.
3. It refreshes the Deployment's environment, runs the activation probe against its current
   release, and restarts its workers.
4. Only then does it delete the previous ACL user, reading its ownership tag first.

If step 1 to 3 fails, the previous credential is made the secret's current version again (from the
`AWSPREVIOUS` version, and only if it is not already current), the environment is refreshed and
probed again, and the new user is deleted: `aws_elasticache_rotate_switch_failed`. If that rollback
cannot finish, `aws_elasticache_rotate_rollback_failed` keeps the marker. A failure after the
switch, in step 4, keeps the new credential and the marker in phase `cleanup`. Either way, the same
call resolves it: it finishes the cleanup or the rollback and then stops, so plan again to
rotate. While a rotation is unfinished the Resource refuses provisioning, binding, restore, and
destruction (`aws_elasticache_rotate_in_progress`), and a rotation of a different Deployment
refuses the same way. The group must be `ready` to start one
(`aws_elasticache_rotate_resource_not_ready`).

Existing connections that authenticated as the deleted user are closed by ElastiCache, and PHP
processes that cached configuration keep the old credential until they reload it. Whether
`apply_deployment_resources` and a worker restart reach every such process is unverified; the probe
proves the new credential from the Target, not from each running process. Whether ElastiCache lets
a user be deleted while it is still a member of a user group is also unverified.

## Forget a retained tombstone

`plan_cleanup_resource` and `apply_cleanup_resource` retain the infrastructure and write a
tombstone. `plan_forget_resource` and `apply_forget_resource` (confirmation `FORGET <name>`) delete
only that local file, and only when no Resource of that name is registered. They make no AWS call
and do not make the retained group adoptable again; to delete it, use the AWS console or CLI. It works for RDS tombstones too.

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
money until you delete them yourself. To delete them through Gimme instead, use
[Destroy a Resource](#destroy-a-resource) before removing the registration.

## Failure codes

Provider failures become fixed `aws_elasticache_<operation>_<reason>` codes, for example
`aws_elasticache_create_access_denied`. Reasons are `access_denied`, `missing`, `already_exists`,
`invalid_state`, `throttled`, `revoked`, and `unavailable`, and AWS messages, ARNs, and values
are never included. Operations include `subnet_group`, `parameter_group`,
`parameter_group_verify`, `parameter_group_modify`, `user_create`, `user_group_create`,
`user_describe`, `user_bind`, `user_group_bind`, `user_restore`, `user_group_restore`, `snapshots`,
`credential_read`, `rotate_user`, `security_group`, `tags`, `describe`, `describe_cluster`, `update_actions`, `node_types`,
`options`, `modify`, and `create`.
`aws_elasticache_group_ownership_mismatch` and
`aws_elasticache_parameter_group_ownership_mismatch` mean a same-named object exists that this
Resource does not own; rename or delete it yourself. `unavailable` covers anything unclassified,
such as an unsupported node type or a quota; call the same API with the same role in your own
terminal to read the error.
