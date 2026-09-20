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

`create_recovery_point` transfers only reviewed policy and protected credentials to the
registered Target, then invokes the same fixed runner used by scheduled captures. The runner
serializes scheduled and on-demand work with the Deployment operation lock, so independent
control-plane processes cannot capture the same Deployment concurrently. Different Deployments
use independent locks and may capture concurrently.

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
Both scheduled and on-demand success enforce `retain_last`: verified, unprotected points are
removed oldest-first after the replacement verifies. A pruning failure preserves the new point,
stops further deletion, and returns `backup_succeeded_retention_failed`; retry retention with a
later successful capture or use the explicit manual deletion workflow.

The policy model accepts strict UTC `manual`, `hourly`, `daily`, and `weekly` cadence shapes and
a bounded `retain_last` value from 1 through 365. The defaults are manual cadence and seven
retained points. A non-manual schedule requires the registered Target APT stack to include
`python3-boto3`; planning reports `recovery_schedule_runtime_missing` and apply remains
unavailable otherwise. Applying Deployment resources installs or updates one persistent UTC
systemd timer and its content-bound runner policy. Switching to manual cadence or removing the
Deployment disables and removes the units, scheduled authority, status, and stored credentials.

The timer computes only the latest missed logical slot after downtime. A stable
Deployment-derived delay of 0–300 seconds spreads load without changing that slot or its
deterministic request identity, so restart and catch-up retries converge on the same Recovery
Point. The runner waits at most five minutes for the Deployment lock. `deployment_busy` means it
performed no capture or retention and will wait for a later timer activation or on-demand call.

Ambient Target identity writes no credential file. A destination using `secret_refs` resolves
credentials only during apply, transfers them through owner-only files, and installs them as a
root-owned systemd credential. Reapply Deployment resources to rotate stored or session
credentials; the runner does not refresh expiring sessions. `credentials_expired` therefore
requires reapplication. Switching to ambient authentication or manual cadence removes the
persisted scheduled credential.
Inspect `get_recovery_schedule_status(deployment)` or
`gimme://deployments/{name}/recovery-schedule` for bounded timer state and logical/effective next
UTC times. `status_unavailable` means the Target observation could not be obtained; destination
manifests remain the authoritative Recovery Point inventory.
The status reports only the latest scheduled attempt. `backup_succeeded_retention_failed` means
the new point is valid but pruning stopped at its first failed oldest candidate; a later
successful capture retries retention. `policy_stale`, `credentials_unavailable`,
`credentials_expired`, `destination_unavailable`, `capture_failed`, and `verification_failed`
are fixed safe outcomes; reapply policy or credentials after correcting the indicated class of
failure. Raw target, systemd, provider, and credential errors are never returned.
Before apply, inspect the `recovery_schedule` section of `plan_deployment_resources`. Its
authority fingerprint changes with policy, placement, selected Resource provenance, destination
execution policy, or auth mode, while the plan exposes no secret reference or credential path.
