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

**Resource**:
A named, versioned service available on one Target and shareable by multiple Deployments.
_Avoid_: Deployment database, application service

**Deployment**:
One Application placed at one stage on one Target, with isolated runtime identity, data
identity, environment, processes, and routing.
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
weekly selects a weekday and time.
It retains the most recent 1 to 365 verified Recovery Points, defaulting to 7; pruning occurs
only after a replacement has been verified. It selects exactly one named Backup Destination;
many Deployments may share that destination, but one Recovery Point is not replicated across
destinations.
_Avoid_: Resource backup policy

**Backup Destination**:
A named, versioned S3-compatible object-storage location where Recovery Points are retained independently
of their Target. A Backup Destination belongs to control-plane policy, not to a Deployment;
transfers and stored objects are encrypted, and every Component Backup has verified integrity.
Its Target authentication is either ambient workload identity or encrypted secret references.
_Avoid_: Backup path, upload directory

**Recovery Manifest**:
The immutable, secret-free description of one verified Recovery Point stored in its Backup
Destination. Recovery Manifests are the authoritative inventory even if the owning Target or
local operational records are unavailable.
_Avoid_: Local backup record, journal entry

**Safety Recovery Point**:
A protected Recovery Point captured and verified immediately before a Restore replaces non-empty
data. It is not eligible for retention pruning while the Restore remains unsuccessful.
_Avoid_: Automatic rollback, temporary dump

**Restore**:
An explicitly confirmed operation that replaces selected Deployment-owned data from one of its
Recovery Points during a controlled maintenance window. A failed or unverified Restore leaves
the Deployment unavailable rather than exposing partially restored data. It restores every
Component Backup by default; an explicitly selected partial Restore intentionally leaves all
unselected components untouched.
_Avoid_: Import, clone, rollback

## Example dialogue

> **Developer:** Can I restore the Valkey Resource for `shop-production`?
>
> **Domain expert:** Restore the `shop-production` Recovery Point instead. The Valkey
> Resource may also serve other Deployments; only the Component Backup for that
> Deployment-owned key prefix belongs to this recovery operation.
