# Gimme

Gimme models the desired and observed state involved in placing and operating applications
on explicitly registered machines.

## Language

**Target**:
A registered machine with one machine identity, network policy, package stack, and runtime
policy.
_Avoid_: Host, server, box

**Application**:
A reusable source and build definition that can participate in many Deployments.
_Avoid_: App instance, site

**Application Artifact**:
A content-addressed, immutable package of one Application revision and its production dependencies
and compiled assets, built under exact declared inputs. Its secret-free provenance manifest is the
publication marker and authoritative identity; Deployments verify and activate the artifact without
rebuilding it. Its reviewed `build_id` hashes declared inputs, while its `artifact_digest` hashes
the deterministic package bytes; rebuilding one `build_id` to different bytes fails closed.
_Avoid_: Release checkout, build directory

**Artifact Store**:
A named, versioned S3-compatible object-storage location for Application Artifacts and their
provenance manifests. It has a separate lifecycle from recovery storage, and Gimme derives every
object identity rather than accepting caller-supplied bucket paths or keys.
_Avoid_: Backup Destination, upload path

**Build Target**:
The registered Target selected by an Application's build policy to produce Application Artifacts
in an isolated derived workspace. It may also host Deployments, but build and release remain
separate operations; it receives no Deployment runtime secrets or Resource access.
_Avoid_: Deployment Target, CI runner

**Provider Account**:
A named control-plane identity for one external service provider. It owns bounded provider
configuration and encrypted credential references, while Resources retain service intent. An
operator credential handles ordinary inspection and reconciliation; an optional destructive
credential is resolved only for explicitly confirmed cleanup.
_Avoid_: Cloud token, provider config

**Resource**:
A named, versioned service supplied either by one Target or by a registered Provider Account and
shareable by multiple Deployments. Its provider is a bounded implementation detail behind the
Resource contract. A managed PostgreSQL Resource owns one provider cluster; each bound Deployment
owns an isolated database and least-privilege role within it.
_Avoid_: Deployment database, application service

**Observed Resource State**:
A secret-free, replaceable cache of provider identity, health, version, and drift. Desired state
remains declarative; provider ownership tags permit observed identity to be rebuilt without making
one control-plane workstation authoritative. Ambiguous identity fails closed.
_Avoid_: Desired resource config, provider credentials

**Deployment**:
One Application placed at one stage on one Target, with isolated runtime identity, data
identity, environment, processes, and routing. Its explicit release mode is `source` for a
destination-built local or preview workflow, or `artifact` for immutable build/release separation;
staging and production require artifact mode.
_Avoid_: Environment, release, app

**Recovery Point**:
A Deployment-scoped representation of restorable data at a moment in time. It contains one
or more Component Backups and retains the identities and versions of their bound Resources.
It may be restored only into the same registered Deployment that owns it. Bound Resources may
be the originals or empty replacements whose providers, kinds, and exact versions match. It is
published only after every selected Component Backup passes verification; Valkey-inclusive
capture briefly makes the Deployment unavailable so the selected components form a coherent set.
_Avoid_: Resource backup, service snapshot

**Component Backup**:
The part of a Recovery Point representing one Deployment-owned data component, such as its
PostgreSQL database or an explicitly selected Valkey key prefix. Valkey data is never assumed
to be durable merely because the Deployment binds a Valkey Resource; expiring keys retain
their absolute expiry and are not resurrected after that time.
_Avoid_: Whole-service backup

**Recovery Policy**:
A Deployment-owned declaration of which bound data components participate in Recovery Points.
PostgreSQL participates by default; Valkey participation is opt-in. Its cadence is manual,
hourly, daily, or weekly, and on-demand Recovery Points remain available at every cadence.
Scheduled times are expressed in UTC: hourly selects a minute, daily selects a time, and
weekly selects a weekday and time. Cadence defaults to manual; hourly defaults to minute 0,
daily to 02:00, and weekly to Sunday at 02:00. Persistent timers run only the most recent
missed slot after downtime, using that exact scheduled slot as part of the idempotent request
identity. Scheduled execution adds a stable Deployment-derived delay of up to five minutes to
spread Target load; the configured UTC slot remains the logical schedule.
Its `retain_last` value is an automatic-pruning ceiling from 1 to 365, defaulting to 7; pruning
occurs only after a replacement has been verified and never reduces eligible points below that
count. Manual deletion may reduce the count further, with stronger confirmation for the final
verified point. Protected Safety Recovery Points do not satisfy the retention count. The policy
selects exactly one named Backup Destination; many Deployments may share that destination, but
one Recovery Point is not replicated across destinations.
_Avoid_: Resource backup policy

**Backup Destination**:
A named, versioned S3-compatible object-storage location where Recovery Points are retained independently
of their Target. A Backup Destination belongs to control-plane policy, not to a Deployment;
transfers and stored objects are encrypted, and every Component Backup has verified integrity.
It is accessed from a Deployment's current Target using either that Target's ambient workload
identity or encrypted secret references.
_Avoid_: Backup path, upload directory

**Recovery Manifest**:
The immutable, secret-free description of one verified Recovery Point stored in its Backup
Destination. Recovery Manifests are the authoritative inventory even if the owning Target or
local operational records are unavailable.
_Avoid_: Local backup record, journal entry

**Safety Recovery Point**:
A protected Recovery Point captured and verified immediately before a Restore replaces non-empty
data. It contains exactly the components that Restore will overwrite, so a partial Restore may
produce a PostgreSQL-only or Valkey-only Safety Recovery Point even though ordinary Recovery
Policies include PostgreSQL. It is not eligible for retention pruning while the Restore remains
unsuccessful.
_Avoid_: Automatic rollback, temporary dump

**Restore**:
An explicitly confirmed operation that replaces selected Deployment-owned data from one of its
Recovery Points during a controlled maintenance window. A failed or unverified Restore leaves
the Deployment unavailable rather than exposing partially restored data. It restores every
Component Backup by default; an explicitly selected partial Restore intentionally leaves all
unselected components untouched. PostgreSQL data is loaded and verified in a derived shadow
database before a bounded name swap; the previous database is retained until the Restore completes.
_Avoid_: Import, clone, rollback

**Restore Record**:
The append-only, secret-free history of one Restore request stored in the Deployment's Backup
Destination. It is authoritative across controller or Target loss and records bounded lifecycle
transitions from start through verification or failure. A Safety Recovery Point remains protected
until its Restore Record reaches `completed`; target-local status is only a resumable cache.
_Avoid_: Restore log, local restore status

**Recovery Schedule Status**:
The secret-free observed state of one Deployment's target-side recovery timer and latest scheduled
attempt, including its logical slot, effective execution time, bounded outcome, and retention result.
It is unavailable with its Target and is never authoritative Recovery Point inventory.
_Avoid_: Backup history, recovery inventory

## Example dialogue

> **Developer:** Can I restore the Valkey Resource for `shop-production`?
>
> **Domain expert:** Restore the `shop-production` Recovery Point instead. The Valkey
> Resource may also serve other Deployments; only the Component Backup for that
> Deployment-owned key prefix belongs to this recovery operation.
