# Place a Deployment on a registered Target fleet

Schema v8 gives every Target a bounded `deployment_slots` capacity from 0 through 1024. Every
registered Deployment consumes one slot on its immutable Target until removal succeeds. Capacity
is an admission-control count, not a CPU, memory, disk, or live-utilization guarantee.

## Migrate existing state

Use `plan_state_migration` and `apply_state_migration` with the reviewed `plan_id` for schema-v7
state; no artifact-policy arguments need to be repeated. Migration adds an empty Rollout
collection and preserves
every Deployment's Target and placement, records an explicit immutable placement decision, and
sets each Target's capacity to `max(existing deployment count, 1)`. It therefore creates no
accidental spare capacity. Increase capacity later with the normal Target update plan if desired.

Reducing capacity below current reservations is allowed. `gimme://fleet` then reports the Target
as overcommitted, while every existing Deployment and reservation remains in place. Zero capacity
prevents new registration but does not evict an existing Deployment.

## Choose explicit or policy placement

A new `DeploymentRegistration` must choose exactly one selector:

- `target`: one explicit registered Target. Planning checks policy, Resource compatibility, and
  available capacity without requiring the Target to be reachable.
- `placement_policy.candidates`: 1–64 unique registered Target names. Planning performs fresh,
  bounded readiness and runtime inspection for those Targets.

Resource bindings remain exact and must already be compatible with a candidate. Placement never
substitutes a database, Valkey Resource, hostname, or runtime. System and bundled runtime pins
must already match; a supported missing mise-managed pin remains installable after placement.

Call `plan_register_deployment` and review every normalized candidate, capacity calculation,
eligibility result, safe rejection code, selected Target, and derived placement. Selection is
deterministic and independent of caller order:

1. lowest occupied-slot ratio;
2. greatest free-slot count;
3. lexical Target name.

If no Target is eligible, the plan is inspectable with `ready: false` and cannot be applied. Pass
the unchanged `plan_id` to `register_deployment`. Apply repeats policy and readiness inspection,
then reloads, validates, reserves, and writes under one exclusive state lock. A capacity,
reservation, policy, binding, runtime, or observation change rejects the plan as stale; concurrent
last-slot claims cannot both succeed.

## Inspect and recover

Read `gimme://fleet` for desired capacity, Deployment and temporary Rollout reservations, free
slots, and overcommit without remote
calls. Use `inspect_fleet` for bounded current readiness. Results contain fixed status codes and
fingerprints, never raw command output, exception text, secrets, or credentials.

Placement is initial and immutable. Target loss leaves the Deployment and its slot reserved on
that Target and reports degraded readiness; Gimme does not relocate, evict, or release it.
Retry after restoring the Target or explicitly remove the Deployment. Target migration,
rebalancing, failover, and replacement are separate workflows.
