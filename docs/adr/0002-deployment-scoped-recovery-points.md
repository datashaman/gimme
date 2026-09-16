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

Restore remains bound to the owning Deployment but may target empty replacement Resources after
Target loss when provider, kind, and exact version match. It runs inside a maintenance window,
protects existing data with a Safety Recovery Point, and fails closed until integrity, database,
health, and process verification succeed. Explicit partial restore is permitted, but must declare
that it intentionally breaks cross-component consistency.

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
- Gimme does not provide an MCP force-online bypass after failed restore verification.
