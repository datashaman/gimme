# Deployment-scoped recovery points in versioned object storage

- Status: Accepted
- Date: 2026-09-16

Gimme recovery points belong to Deployments rather than shared PostgreSQL or Valkey
Resources. Their immutable, secret-free manifests live in one named, versioned S3-compatible
Backup Destination selected by each Deployment Recovery Policy, so inventory survives loss of
the Target or local operational records. PostgreSQL participates by default; Valkey is opt-in
because its deployment-owned prefix may contain disposable or unsafe-to-resurrect state.

Scheduled capture runs through target-side, policy-bound systemd units because the local stdio
MCP server is not an always-running controller. PostgreSQL-only capture is online, while a
Valkey-inclusive recovery point briefly quiesces only its Deployment to produce a coherent set.
Transfers require TLS, server-side encryption, integrity verification, and either ambient Target
identity or encrypted secret references.

Schedules use persistent UTC systemd timers. After downtime, only the most recent missed slot
runs; its scheduled UTC instant participates in a deterministic request identity so delayed starts
and retries cannot publish duplicate Recovery Points. A stable Deployment-derived delay of up to
five minutes spreads Target load without changing that logical slot or serializing independent
Deployments behind a Target-global lock.

Scheduled transfers prefer ambient Target workload identity. When a Backup Destination uses
encrypted secret references, resource reconciliation installs a root-owned credential consumed
through systemd's credential mechanism; secrets never appear in unit configuration or status.
Policy reapplication rotates it, and disabling scheduling or removing the Deployment removes it.

Each scheduled Deployment exposes bounded Recovery Schedule Status from atomic Target-local
observations. It reports timer and latest-attempt outcomes without raw system output; S3 Recovery
Manifests remain authoritative inventory when that Target is unavailable.

Restore remains bound to the owning Deployment but may target empty replacement Resources after
Target loss when provider, kind, and exact version match. It runs inside a maintenance window,
protects existing data with a Safety Recovery Point, and fails closed until integrity, database,
health, and process verification succeed. Explicit partial restore is permitted, but must declare
that it intentionally breaks cross-component consistency.

A Safety Recovery Point contains exactly the components the Restore will overwrite. This permits
PostgreSQL-only or Valkey-only Safety Recovery Points for explicit partial Restore even though an
ordinary Recovery Policy includes PostgreSQL by default.

Each Restore request writes an append-only, secret-free Restore Record to its Backup Destination.
That record, rather than controller or Target-local state, is authoritative for resumption and for
deciding whether a Safety Recovery Point remains protected. Recovery Manifests stay immutable.

PostgreSQL Restore loads and verifies a derived shadow database before swapping it into the
Deployment's registered database identity. The previous database remains available while managed
processes and private application health are verified, and is removed only after the Restore
Record reaches `completed`.

Valkey Restore verifies its complete archive, incrementally clears only the Deployment's registered
prefix, replays binary-safe records with absolute expiry, and incrementally verifies the result.
Because a prefix cannot be swapped atomically without blocking the shared Resource, any failure
remains in maintenance and retry clears and replays that prefix from the verified archive.

For a full PostgreSQL-and-Valkey Restore, both source artifacts and the paired Safety Recovery
Point verify first. PostgreSQL is prepared in its shadow database, the Valkey prefix is then
replaced and verified, and the PostgreSQL name swap is the final data activation step before
private application and managed-process verification.

Valkey replacement compatibility is evaluated against the Deployment's immutable registered
prefix and the bound Resource's provider, kind, and exact version. An empty replacement means
that prefix contains no keys; the shared Resource may already contain unrelated Deployment
prefixes, which Restore must not inspect beyond bounded isolation checks or modify.

## Considered alternatives

- Whole-Resource snapshots were rejected because one Resource can serve several Deployments and
  restoring it would overwrite unrelated data.
- Target-local backup storage was rejected as the durable destination because it does not survive
  Target or disk loss.
- Scheduling in the MCP process was rejected because stdio sessions are not continuously running.
- Mandatory client-side encryption was deferred because its separate key-recovery lifecycle can
  make otherwise healthy recovery points permanently unusable; portable server-side encryption
  is the required baseline.

## Consequences

- Backup and restore operations must preserve Deployment database and Valkey-prefix isolation.
- Bucket versioning is mandatory; Object Lock remains optional operator hardening.
- Deployment-changing operations must serialize with backup and restore.
- Retention may prune only verified, unprotected recovery points after a replacement verifies.
- `retain_last` is an automatic-pruning ceiling, not a minimum guarantee after manual deletion;
  protected Safety Recovery Points do not satisfy it.
- A verified new Recovery Point remains successful if later pruning fails. Retention stops at the
  first failed oldest candidate, reports degraded status, and retries only after a later successful
  capture or an explicit manual deletion.
- A Safety Recovery Point remains protected until its authoritative Restore Record is completed.
- PostgreSQL Restore never streams an unverified dump directly into the live database identity.
- Valkey Restore never snapshots, restarts, globally locks, or executes one blocking full-prefix
  operation against the shared Resource.
- Gimme does not provide an MCP force-online bypass after failed restore verification.
