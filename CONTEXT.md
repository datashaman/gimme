# Gimme

Gimme models the desired and observed state involved in placing and operating applications
on explicitly registered machines.

## Language

**Target**:
A registered machine with one machine identity, network policy, package stack, and runtime
policy. It declares a bounded Deployment-slot capacity used only for placement admission. Reducing
capacity below existing reservations marks the Target overcommitted but never evicts or relocates a
Deployment. Zero slots drains new admission while preserving existing placements; explicit and
policy-driven placement obey the same capacity limit.
_Avoid_: Host, server, box

**Provider Attachment**:
An optional, exact association between a Target and its compute identity in one Provider Account
and region. Provider topology, addresses, state, and network membership are observed from that
identity rather than copied into desired state.
_Avoid_: Cloud host, instance metadata

**Administration Target**:
The explicit registered Target through which Gimme performs fixed private-network administration
for one managed Resource. It is an execution boundary rather than a Deployment placement and
therefore consumes no Deployment slot.
_Avoid_: Deployment Target, bastion, database host

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
configuration and authentication policy, while Resources and stores retain service intent.
Authentication may use ambient identity, exact assumed roles, or encrypted credential references;
distinct capabilities remain separated where the provider permits it.
_Avoid_: Cloud token, provider config

**AWS Network**:
A named, validated set of pre-existing AWS data-network and edge prerequisites within one Provider
Account, region, and VPC. It bounds the subnets, security groups, TLS certificate, hosted zone, and
DNS suffix from which Gimme may create managed Resources and one supported AWS routing topology;
it does not grant generic infrastructure provisioning.
_Avoid_: VPC, cloud network, load balancer

**Execution Profile**:
A named, versioned, policy-bound selection of one execution engine and its bounded placement,
capability, and lifecycle contract for materializing a Deployment. It renders portable Deployment
intent into derived engine-native objects under existing Target, Provider Account, and network
boundaries; it is neither a generic cloud account nor arbitrary infrastructure-as-code.
_Avoid_: Provider, target, cluster configuration, task definition

**Execution Engine**:
The versioned runtime materialization mechanism selected by an Execution Profile, such as
`ubuntu-systemd`, `docker-compose`, `ecs-fargate`, `kubernetes`, or `lambda`. Its renderer is a
security-sensitive boundary: it accepts only validated portable intent, produces canonical derived
state, and rejects capability requests it cannot represent faithfully.
_Avoid_: Provider, arbitrary executor, infrastructure API

**Process Role**:
One bounded operational purpose declared by a Deployment: `web`, `worker`, `scheduler`, or
`realtime`. The Application defines its fixed framework command and behavior; the selected
Execution Profile determines how the role is materialized without accepting arbitrary commands.
_Avoid_: Process command, service definition, container command

**Secret Store**:
A named, bounded external location from which the control plane may resolve Deployment secrets at
the protected execution boundary. It belongs to one Provider Account and constrains account,
region, namespace, ownership, and version-selection policy. Targets never receive Secret Store
credentials.
_Avoid_: Secret file, environment, vault token

**Secret Reference**:
A Deployment-owned selection of one top-level string field from one secret in a registered Secret
Store. It names no provider ARN or version directly; planning pins the store's current version
metadata, while only apply may retrieve plaintext.
_Avoid_: Secret value, provider URI

**Applied Secret Manifest**:
The replaceable, secret-free record of which exact external secret versions were last applied to a
Deployment. It contains only environment keys and identity/version fingerprints, enabling rotation
and drift planning without reading plaintext or persisting value hashes.
_Avoid_: Secret cache, environment backup

**Resource**:
A named, versioned service supplied either by one Target or by a registered Provider Account and
shareable by multiple Deployments. Its provider is a bounded implementation detail behind the
Resource contract. A managed PostgreSQL Resource owns one provider cluster; each bound Deployment
owns an isolated database and least-privilege role within it. A managed Valkey Resource owns one
provider cluster; each bound Deployment owns an isolated key namespace and access identity.
_Avoid_: Deployment database, application service

**Durable Resource**:
A Resource whose provider contract preserves acknowledged writes through its explicitly supported
infrastructure-failure scenarios. Durability does not recover logical deletion, application error,
credential misuse, expiry, or a failure outside that contract; those require Recovery Points and a
separate recovery objective.
_Avoid_: Backup, indestructible Resource, globally durable Resource

**Resource Credential**:
A provider-backed secret owned by one Resource allocation rather than supplied by an operator.
Gimme may create and rotate it through a bounded provider contract, but plaintext exists only at
protected execution boundaries and never enters desired or observed state.
_Avoid_: Secret Reference, master password, environment value

**Resource Binding**:
A Deployment-owned association with one registered Resource for an explicit set of supported uses.
The binding owns the Deployment's isolated allocation and access identity; it does not grant every
capability of the shared Resource. A Valkey binding distinguishes cache, session, and queue use.
_Avoid_: Resource, connection string, implicit cache

**Detached Allocation**:
A retained Deployment-owned data allocation and disabled access identity that remains inside a
managed Resource after its Deployment binding is removed. It may be a database or a bounded key
namespace. Rebinding restores the same data identity; destructive purge requires verified Recovery
Point evidence.
_Avoid_: Orphan database, Resource backup

**Retained Resource**:
Provider infrastructure deliberately left intact after its Resource registration is removed.
Gimme keeps only a secret-free inventory tombstone and neither reconciles nor silently re-adopts it.
_Avoid_: Managed Resource, Observed Resource State

**Observed Resource State**:
A secret-free, replaceable cache of provider identity, health, version, and drift. Desired state
remains declarative; provider ownership tags permit observed identity to be rebuilt without making
one control-plane workstation authoritative. Ambiguous identity fails closed.
_Avoid_: Desired resource config, provider credentials

**Deployment**:
A logical placement of one Application at one stage, with one data identity, environment, release
identity, process policy, and public route. A single topology has one Deployment Replica; a supported
high-availability topology has multiple Replicas sharing those identities. Its explicit release mode
is `source` for a destination-built local or preview workflow, or `artifact` for immutable
build/release separation; staging and production require artifact mode.
_Avoid_: Environment, release, app

**Deployment Replica**:
One Target-specific runtime materialization of a Deployment. It has immutable Target placement and
runtime/process identity, consumes one Target Deployment slot, and runs the Deployment's exact live
Application Artifact. Replicas share only the Deployment's explicitly declared managed Resources,
environment, data identity, and public route.
_Avoid_: Deployment, release, database replica

**Observed Topology State**:
The secret-free, replaceable cache of a Deployment's Replica generations, provider identities,
health, routing state, and resumable operation phase. Desired state, provider ownership markers,
and Target manifests can reconstruct it; ambiguous identity fails closed.
_Avoid_: Deployment configuration, operation journal, controller database

**Rollout**:
A Deployment-owned, temporary traffic transition between its currently live stable Application
Artifact and one different compatible candidate Application Artifact. New request cohorts receive
explicit integer weights totaling 100 and retain release affinity. Completion makes the candidate
the sole live artifact; reversal restores the stable artifact. A Rollout never changes Deployment
placement, data identity, or public route identity. Its bounded lifecycle is resumable across
control-plane or Target interruption and fails closed when recorded generations disagree.
_Avoid_: Deployment, release, load balancer

**Placement Policy**:
A bounded single-topology Deployment declaration that deterministically selects its initial Target
from an explicit set of registered Targets. Capacity is a declared count of Deployment slots on each
Target, and each registered Deployment Replica consumes one slot until it is removed from desired
state, regardless of deployment or health status; transient machine utilization is not placement
input.
Once selected, the Deployment retains that Target and its immutable placement identities until a
separate migration is planned and confirmed; target loss never causes automatic relocation.
_Avoid_: Scheduler, failover policy, target preference

**Placement Decision**:
The immutable, secret-free explanation of how a Deployment received its Target. It records whether
selection was explicit or policy-driven, the normalized candidate set, the versioned selection
rule, bounded capacity and eligibility results, the selected Target, and fingerprints of the policy
and readiness observations. It contains no raw command output, exception text, or credentials.
_Avoid_: Scheduler state, capacity history

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
PostgreSQL database or an explicitly selected Valkey key prefix. Resource durability protects
acknowledged writes from supported infrastructure failures; it does not protect against logical
deletion, application error, or expiry. Expiring Valkey keys retain their absolute expiry and are
not resurrected after that time.
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
