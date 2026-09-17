# Sticky weighted Rollouts between immutable artifacts

- Status: Accepted
- Date: 2026-09-17

Gimme supports one active Rollout per artifact-mode staging or production Deployment. A Rollout is a
temporary, resumable traffic transition between the Deployment's currently live stable Application
Artifact and one different compatible candidate. Source-mode, local, and preview Deployments retain
one active release and do not participate.

The two artifacts must match on Application identity, packaging format, framework, exact runtime
pins, PHP extensions, Target capability, Deployment environment, secrets, Resource bindings,
process declarations, and public health contract. They differ only in source-derived artifact
content. A Rollout never changes Deployment placement, public route identity, or data identity.

Starting a Rollout always creates a zero-traffic candidate at weights `100/0`. Gimme materializes and
verifies the candidate, creates a distinct web backend, and runs direct candidate health probes. A
separate content-addressed weight transition is required before public candidate traffic. Weights
are integers from zero through 100 and total exactly 100.

Stable and candidate use separate immutable trees, runtime directories, logs, health identities,
and PHP-FPM pools and sockets where applicable. Static applications receive distinct static
backends. Both revisions share only the Deployment's declared protected environment, Resources,
cache namespace, and shared storage. Only stable owns queue workers, Horizon, and the scheduler
during the Rollout. Background work cannot be meaningfully weighted, and running both revisions
would consume jobs with unpredictable code or duplicate scheduled work.

Rollouts perform no database migration. Candidates must tolerate the current shared schema and data.
Expand migrations occur before a Rollout and contract migrations after completion. Schema-changing
Artisan commands are rejected while a Rollout is active; other permitted commands run against stable
and process restart commands affect stable background ownership.

Traffic uses sticky weighted cohorts. A first eligible request is assigned according to the reviewed
weights and receives a Gimme-managed signed affinity cookie. Subsequent requests remain on that
release while it has nonzero weight and remains healthy. Weight changes influence new cohorts rather
than promising an instantaneous global traffic percentage. Clients that reject cookies receive no
affinity guarantee. Zero-weight backends cannot be selected, including by an old cookie.

The Target generates and protects the affinity signing key. Only the policy-bound routing helper
consumes it; the control plane observes a generation fingerprint, never the value. Completion,
reversal, or Rollout replacement rotates the generation so stale cookies cannot select retired
artifacts. Cookie name and attributes are derived: secure, HTTP-only, same-site lax, and scoped to
the root path. MCP accepts no routing snippet, upstream, socket, port, filesystem path, cookie name,
or signing key.

Every weight transition preflights both direct backends, atomically installs the reviewed route, and
then verifies both direct identities and the public live route. Apply-time failure restores the exact
prior route and validates stable. Caddy active health checks may temporarily exclude an unhealthy
backend between Gimme operations, but they do not mutate desired weights or promote a release.
Connection retries are restricted to safe GET and HEAD requests.

Rollout lifecycle is bounded and resumable: preparing, active, completing, reversing, completed,
reversed, or degraded. Preparing is persisted before remote mutation and owns a temporary Target
capacity reservation. Target-side policy state records only plan identity, generation, phase,
backend identities, route fingerprint, and safe outcomes. Retry resumes the matching generation;
missing, mismatched, or ambiguous local and Target records fail closed.

An active Rollout reserves one additional Deployment slot because it doubles the web runtime. Start
is rejected on a full or overcommitted Target. The reservation survives interruption and degraded
state and is released only after completion or reversal has removed the retired backend and process
state. Retry never claims another slot.

Completion requires candidate weight 100. It health-gates a transactional handoff of background
ownership, makes candidate the sole live artifact, removes Rollout routing, rotates affinity, and
releases temporary capacity. Failure restores stable routing and process ownership. Reversal can
begin from preparing, active, or degraded state, but requires healthy stable and releases capacity
only after candidate retirement.

Both artifacts remain pinned against pruning while any Rollout state is active or recoverable.
Ordinary deployment, promotion, rollback, Deployment removal, incompatible Deployment updates, and
artifact pruning are blocked. Shared environment and secret reconciliation may proceed only when it
transactionally activates and health-checks both web revisions under the same Deployment lock.

Inspection reports artifact identities, desired weights, affinity generation, backend eligibility,
bounded health results, runtime readiness, background ownership, route fingerprint, lifecycle phase,
and safe drift codes. It never returns bodies, logs, cookies, addresses, request samples, secrets, or
per-user data. Request analytics and automated statistical decisions are not part of Rollout state.

## Considered alternatives

- Per-request weighted routing was rejected because Laravel pages, assets, and sessions can cross
  incompatible revisions between requests.
- Running background processes for both artifacts was rejected because HTTP weights cannot govern
  queue consumption or scheduler duplication.
- Candidate migrations were rejected because shared-schema mutation can make reversal impossible.
- Immediate nonzero traffic at start was rejected in favor of a separate reviewed exposure step.
- Autonomous promotion and rollback were rejected because health alone cannot judge application
  correctness and all durable traffic changes require reviewed plan/apply.
- Ignoring temporary capacity was rejected because a Rollout materially doubles web runtime load.

## Consequences

- Immutable artifact support and deterministic Target capacity are prerequisites.
- Operators must leave spare Target capacity for zero-downtime Rollouts.
- Applications must use backward-compatible expand/contract schema changes.
- Cookie-rejecting clients do not receive sticky cohorts.
- Cross-Target HA, analytics, automatic promotion, and richer release topologies remain separate
  work.
