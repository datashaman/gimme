# Deterministic initial Deployment placement

- Status: Accepted
- Date: 2026-09-17

Gimme supports deterministic initial placement of a Deployment across an explicit bounded set of
registered Targets. Placement is admission control, not orchestration: after registration, the
selected Target and the Deployment's placement identities remain immutable. Target loss,
reconciliation, and capacity changes never relocate or evict a Deployment.

Each Target declares a bounded number of Deployment slots. Every registered Deployment consumes one
slot on its selected Target until it is successfully removed from desired state, regardless of
whether it has been deployed or is healthy. Zero slots drains new admission. Reducing capacity below
existing reservations marks a Target overcommitted and blocks new placement without disturbing
existing Deployments. Explicit and policy-driven placement obey the same capacity limit.

A Placement Policy contains from one through 64 unique registered Target names. It has no tag
selector, wildcard, implicit fleet, or caller-controlled priority. Planning evaluates desired
Application, Deployment, Resource, Target, network, stage, and runtime policy together with fresh
bounded Target readiness observations. Resource bindings are fixed before placement; target-local
Resources restrict eligibility to their owning Target, and Gimme never substitutes a supposedly
equivalent Resource.

An eligible Target must be reachable and ready for its required base capabilities. Exact system
runtimes must already match. A supported Deployment-specific mise pin may be absent because runtime
reconciliation installs it after placement. Unreachable, unbootstrapped, incompatible, full, and
overcommitted Targets receive bounded safe rejection codes rather than raw command or exception
output.

Selection orders eligible Targets by lowest occupied-slot ratio, then greatest absolute free-slot
count, then lexical Target name. Caller order cannot influence the result. Planning reports every
candidate's capacity, reservations, eligibility result, and tie-break inputs. Apply repeats all
policy and readiness observations and rejects a stale decision.

Deployment registration becomes a content-addressed plan/apply operation for both explicit and
policy placement. Explicit placement remains an operator override and may be planned without
contacting an unprovisioned Target, but it still observes desired-state compatibility and capacity.
Policy placement contacts every candidate because Gimme, rather than the operator, is choosing the
Target.

Apply acquires one exclusive desired-state transaction, reloads state, revalidates the plan and
reservation, and writes the Deployment atomically. Locking only the final write is insufficient:
two concurrent registrations could otherwise claim the same last slot.

Every Deployment retains an immutable, secret-free Placement Decision. It records explicit or
policy mode, normalized candidates, the versioned selection rule, bounded capacity and eligibility
results, the selected Target, and fingerprints of relevant policy and readiness observations. It
contains no raw diagnostics, exceptions, timestamps that affect identity, or credentials.

Existing state migrates without inventing spare capacity. Each existing Target receives the greater
of one slot or its current Deployment count. Operators explicitly raise capacity when they want new
admission.

## Considered alternatives

- Live CPU, memory, and disk scheduling was rejected because Gimme does not yet enforce matching
  per-Deployment limits; transient utilization would also make planning unstable.
- Automatic failover and rebalancing were rejected because local runtime state, routing, and data
  movement require a separate migration and recovery contract.
- Target tags and implicit all-Target fleets were rejected because explicit candidates keep the
  authority boundary reviewable and bounded.
- Selecting or substituting Resources alongside Targets was rejected because Resource identity is
  part of Deployment data identity, not a scheduling preference.
- Letting explicit placement bypass capacity was rejected because Target capacity would cease to be
  a reliable admission invariant.

## Consequences

- Target capacity is deliberately coarse and does not promise CPU, memory, or disk isolation.
- Failed, unhealthy, and unreachable Deployments retain their reservations until removal.
- A Target can remain overcommitted indefinitely without forced remediation.
- Target migration, rebalancing, HA, automatic failover, resource enforcement, and richer selectors
  remain separate work.
