# Prepare an Artifact Rollout candidate

Use this workflow for an artifact-mode staging or production Deployment after its current release
is healthy and the desired source resolves to a different published artifact.

## Prerequisites and capacity

The Target needs the current privileged helpers, Caddy, the declared system PHP runtime, and one
free `deployment_slots` entry. Stable must be a verified live artifact and candidate must be a
different verified publication with matching Application, framework, runtime/extensions,
Deployment environment, Resource bindings, process declarations, and public-health contract. Run
backward-compatible expand migrations before starting; Rollout operations never migrate shared
data.

1. Read `gimme://fleet`. The Deployment's Target needs one free slot; preparation holds that slot
   until the Rollout is completed or reversed.
2. Call `plan_start_rollout` with the Deployment name. Review the stable and candidate identities,
   generation, `100/0` weights, compatibility fingerprints, reservation, and effects.
3. Pass the unchanged `plan_id` to `start_rollout`.
4. Read `inspect_rollout` or `gimme://deployments/{name}/rollout`. A prepared candidate reports
   `phase: active`, `backend_ready: true`, `outcome: ready`, `background_owner: stable`, and weights
   `100/0`.
5. Call `plan_rollout_weights` with integer `stable_weight` and `candidate_weight` values from 0 to
   100 totaling exactly 100. Review the artifacts, current/proposed weights, affinity generation,
   route fingerprints, and effects, then pass the unchanged values and `plan_id` to
   `apply_rollout_weights`.
6. Inspect again. Confirm desired weights, `affinity_generation`, backend eligibility, bounded
   health, `route_fingerprint`, `phase`, and `drift` before requesting another transition.
7. To accept the candidate, first apply `0/100`, then call `plan_complete_rollout` and pass its
   unchanged `plan_id` to `complete_rollout`. Completion promotes the exact candidate to the
   ordinary retained release inventory and hands it workers, Horizon, and scheduler ownership.
8. To abandon a `preparing`, `active`, or `degraded` generation, call `plan_reverse_rollout` and
   pass its unchanged `plan_id` to `reverse_rollout`. Reversal restores stable-only web and
   background service and retires the candidate.

Preparation cannot send traffic to the candidate. It materializes the exact reviewed publication
without Git, Composer, Node, dependency installation, frontend building, or database migrations.
The candidate has its own generation-derived tree, runtime directory, logs, loopback backend, and
PHP-FPM pool/socket. It shares only the Deployment's declared environment, secrets, Resources,
cache namespace, and shared storage. Stable remains the only worker, Horizon, and scheduler owner.

Traffic weights apply to new clients that accept the signed Gimme affinity cookie. They do not
promise that the same percentage of all requests changes immediately: existing valid cohorts stay
on their assigned release while it has nonzero weight and remains healthy, and clients that reject
cookies are not sticky. Setting a release to zero removes it from selection, including for an old
cookie. The Target retains the signing key; it is never shown by inspection or stored in desired
state.

Every transition probes both releases directly before route installation and verifies both direct
backends plus the public route afterward. If health, Caddy validation, or reload fails, Gimme
restores the exact prior route and verifies stable service. Desired weights remain unchanged. Caddy
active health can temporarily exclude an unhealthy backend without autonomously changing weights,
promoting, or completing the Rollout.

Completion and reversal persist a transition phase before touching the Target. They rotate
affinity and release the temporary Target slot only after route, process, health, and cleanup
converge. If the caller is interrupted after the Target finishes, request a fresh plan and retry;
the matching terminal Target record finishes local state without creating another release or
reservation. A Target outage or generation mismatch fails closed and keeps the Deployment on its
Target with the slot reserved. If rollback itself is degraded, restore the Target and retry the
same generation rather than editing state manually.

Rollouts never run database migrations. Schema-changing Artisan commands are blocked while a
generation is active or recoverable. Other explicitly allowlisted Artisan commands continue to
address stable `current`; all shared mutations serialize under the Deployment lock. Ordinary
resource/secret/process reconciliation remains blocked while it cannot activate and verify both
web revisions transactionally.

If the call is interrupted or fails, inspection reports `preparing` or `degraded`. Request a fresh
plan and call `start_rollout` again. The plan retains the same generation only when stable,
candidate, policy, compatibility, and private publication evidence still match; the retry replaces
the same derived candidate tree and does not reserve another slot. Restore an unavailable Target
before retrying. A conflicting generation or changed policy must be resolved by the later reverse
or completion workflow; do not edit desired state manually.

Ordinary deploy, runtime/resource reconciliation, promotion, rollback, Deployment update/removal,
and changes to referenced Target, Application, Resource, or Artifact Store policy are blocked while
the Rollout is active or recoverable. This preparation slice does not expose backend addresses,
ports, paths, sockets, object versions, command output, response bodies, cookies, or secrets.

## Failure recovery

- For `preparing` or `degraded`, restore Target reachability and request a fresh plan. A matching
  retry reuses the generation and reservation.
- A failed weight apply leaves desired weights unchanged and restores the prior route. Fix the
  bounded health or Target condition, then request a fresh weight plan.
- A failed completion or reversal keeps the temporary slot. Restore the Target and retry the same
  reviewed outcome; a matching terminal Target record safely finishes interrupted local state.
- Never repair a Rollout by editing desired JSON, Caddy fragments, Target policy, symlinks, or
  process units. Generation ambiguity deliberately fails closed.

## Cost and operational scope

A Rollout uses one additional web runtime, a PHP-FPM pool where applicable, loopback Caddy
backends, logs, and one temporary Target slot. It creates no cloud infrastructure, database, or
cache and runs no candidate workers. The deterministic local proof is bill-free:

```bash
uv run python tests/integration/rollout_local_scenario.py
```

It emits bounded aggregate counts only—no cookies, client samples, addresses, paths, or protected
data. Real traffic ratios can differ because existing cookie cohorts stay sticky.

## Non-goals

Rollouts do not provide cross-Target HA, more than two revisions, source/preview splitting,
database migration orchestration, candidate background processing, automatic decisions, request
analytics, CDN integration, or non-cookie affinity. MCP never accepts custom Caddy, upstream,
socket, port, path, cookie, or signing-key input.
