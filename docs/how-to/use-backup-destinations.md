# Use S3-compatible Backup Destinations for on-demand Recovery Points

Gimme's first Recovery Point tracer registers one named S3-compatible Backup
Destination, binds it to a Deployment through a Recovery Policy, and creates and
inventories on-demand PostgreSQL and opt-in Valkey Recovery Points. Only the control-plane process
resolves any Backup Destination credential; Targets never receive it.

## Bucket prerequisites

Provision the bucket outside Gimme before registering it:

- versioning must be enabled (not merely available and unset, and not suspended);
- either server-side AES256 or a registered customer-managed KMS key (same account,
  same region as the destination);
- TLS only — Gimme always connects over HTTPS and never accepts a caller-supplied
  scheme;
- either the control plane's ambient AWS credential chain has S3 read/write/delete
  access to the bucket, or an encrypted credential reference resolves an access key
  pair with that access.

On a self-hosted S3-compatible server such as MinIO, `{"method": "aes256"}` still
requires a KMS backend on the server — unlike AWS S3, MinIO has no key-management-free
SSE-S3 path, so a `PutObject` with `ServerSideEncryption: AES256` fails with
`NotImplemented` unless the server was started with a KMS configured (for example
`MINIO_KMS_SECRET_KEY`).

## Register a destination

Use `plan_register_backup_destination` to review the proposed destination, then pass its
`plan_id` to `register_backup_destination`. A destination fixes:

- one bounded, non-IP-literal bucket name and SDK-known region;
- an optional custom HTTPS endpoint (for non-AWS S3-compatible stores) and addressing
  style (`virtual_hosted` or `path`);
- one encryption policy (`{"method": "aes256"}` or `{"method": "kms", "kms_key_arn":
  "..."}`, same-region only);
- one authentication mode: `{"mode": "ambient"}` uses the control plane's own AWS
  identity; `{"mode": "credential_reference", "access_key_id": {...}, "secret_access_key":
  {...}}` points at two `{store, secret, field}` Secret References, exactly like a
  Deployment secret.

`plan_register_backup_destination`/`plan_update_backup_destination` never call the
destination — a plan tool must not touch remote state, and the endpoint is
caller-supplied. The live preflight instead runs during
`register_backup_destination`/`update_backup_destination` apply, once the reviewed plan
is confirmed, for both authentication modes (this also keeps `credential_reference` auth
from resolving plaintext before plan time, the same no-plaintext-at-plan-time boundary
AWS Secret Stores use). Preflight rejects a bucket with unavailable or disabled
versioning, then round-trips one probe object (write, read, delete) to prove
write/read/delete capability, leaving nothing behind either on success or on a failed
probe; a failed preflight leaves nothing registered.

## Bind a Recovery Policy

A Deployment opts into recovery by setting `recovery.destination` on an ordinary
`plan_update_deployment` / `update_deployment` call — there is no separate binding tool.
A bound database resource is required; a static deployment (which cannot bind a
database) cannot bind recovery either.

```json
{
  "recovery": {
    "destination": "primary",
    "valkey": false,
    "quiesce_wait_seconds": 30
  }
}
```

PostgreSQL is always included. Valkey is excluded unless `valkey` is explicitly
`true`, and enabling it requires a Valkey binding. `quiesce_wait_seconds` accepts 1
through 300 seconds and defaults to 30.

Enable Valkey durability only when the Deployment owns durable state in its registered
key prefix and that state must move back in time with PostgreSQL. Do not enable it for
ordinary caches that can be rebuilt. Restoring old Valkey state can resurrect queued
jobs, sessions, locks, rate limits, and cached records; for those workloads, restoring
PostgreSQL alone is often safer.

The consistency boundary covers only Gimme-managed writers. During capture, Gimme puts
the selected Deployment route into a fixed 503 maintenance response, waits the drain
interval, and stops only that Deployment's registered workers or Horizon process and
scheduler. It does not stop the shared Valkey service, block other prefixes, or discover
unmanaged cron jobs, external workers, direct database clients, or other Valkey writers.
Choose a drain interval long enough for the longest in-flight HTTP request to finish,
without making the maintenance window unnecessarily long. Operators must stop or avoid
all unmanaged writers themselves.

## Create and list on-demand Recovery Points

`plan_create_recovery_point(deployment, request_id)` plans one capture; pass its
`plan_id` and the same `request_id` to `create_recovery_point`. The Recovery Point's
identity is derived from `(deployment, destination, request_id)`, never from wall-clock
time, so retrying the exact same `plan_id`/`request_id` — for example after a dropped
connection — is a deterministic no-op that returns the already-published manifest
without re-running `pg_dump` or re-uploading. A different `request_id` always produces a
new, distinct Recovery Point.

Capture runs `pg_dump --no-owner --no-privileges --no-acl` against the Deployment's
isolated database on its Target, so roles, ownership, ACLs, and credential material are
never part of the dump. With Valkey enabled, a binary-safe incremental scan selects only
the registered Deployment prefix, deduplicates results, and records each value with its
absolute expiry time; persistent keys remain persistent and keys that expire during
capture are omitted. Keys, values, prefixes, and serialized payloads never appear in MCP
results or manifests.

Because on-demand capture runs while the MCP server is live
(unlike future scheduled, systemd-timer-driven capture), the dump is pulled back to the
control plane over the same transport already used for Deployment secret files, then
uploaded from there with server-side encryption and a SHA-256 checksum. Every selected
component is uploaded and read back for checksum verification while maintenance remains
active. Gimme then restores the previously active managed processes and normal route and
publishes the single immutable Recovery Manifest last. A capture, upload, verification,
or runtime-restoration failure publishes no Recovery Point and preserves earlier points;
failed process restoration deliberately leaves the route in maintenance for a same-request
cleanup retry.

`list_recovery_points(deployment)` reads Recovery Manifests directly from the bound
destination — authoritative inventory even if the Target is gone — and returns only
bounded, secret-safe metadata (recovery point ID, creation time, state, and each
component's kind and byte count). Keys, checksums, and S3 version IDs remain private. A
manifest whose referenced component object no longer
matches its declared checksum is rejected and excluded from `recovery_points`, but does
not fail the rest of the listing; its ID is reported under `rejected`.

`create_recovery_point` serializes concurrent applies against the same Deployment through
a local, per-control-plane lock. That lock does not extend across two independent
control-plane processes (different state directories) targeting the same real
destination bucket — Gimme assumes a single operator per Deployment, the same
trusted-operator model the plan/apply split already relies on elsewhere. Running more
than one control plane against the same Deployment's recovery destination at once is
unsupported and can race.

## Delete a Recovery Point manually

Inspect `plan_delete_recovery_point(deployment, recovery_point_id)`, then pass its
`plan_id` and exact `confirmation` to `delete_recovery_point`. If the plan identifies the
point as the final verified Recovery Point, also pass its exact
`last_recovery_point_confirmation`.

Manual deletion and automatic retention have different intent. Automatic retention may
prune only after a verified replacement exists and never below `retain_last`. Manual
deletion is an explicit operator decision and may reduce inventory below that value.
Neither mode may delete a Safety Recovery Point or source Recovery Point while its Restore
is unresolved.

Deletion uses only exact versions named by the selected immutable manifest. It deletes
components first and the manifest last; there is no arbitrary object or prefix deletion
interface. A partial failure leaves the point in `deletion_failed` with bounded progress
counts. Retry the original call with the same plan and confirmations; already-absent exact
versions are skipped. Gimme never bypasses S3 Object Lock, legal hold, or destination policy.

For the full or partial Deployment Restore procedure, Target-loss replacement, and
failed-verification recovery, see [Restore a Deployment](restore-a-postgresql-deployment.md).
Scheduled/systemd-timer execution remains separate work described in
[ADR 0002](../adr/0002-deployment-scoped-recovery-points.md). Successful on-demand capture now
enforces `retain_last`: verified, unprotected points are removed oldest-first after the
replacement verifies. A pruning failure preserves the new point, stops further deletion, and
returns `backup_succeeded_retention_failed`; retry retention with a later successful capture or
use the explicit manual deletion workflow.

The policy model already accepts strict UTC `manual`, `hourly`, `daily`, and `weekly` cadence
shapes and a bounded `retain_last` value from 1 through 365. Until timer reconciliation lands,
leave `cadence` as `{kind: manual}`; the default is manual and the default retained count is 7.
