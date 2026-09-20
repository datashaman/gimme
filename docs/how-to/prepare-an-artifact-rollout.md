# Prepare an Artifact Rollout candidate

Use this workflow for an artifact-mode staging or production Deployment after its current release
is healthy and the desired source resolves to a different published artifact.

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
