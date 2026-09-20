# Restore a Deployment

Use this runbook to replace one Deployment's PostgreSQL database, registered Valkey prefix,
or both from a verified Recovery Point. Restore is intentionally two-stage: data replacement
always stops behind a fixed public 503 response, then a separate content-addressed verification
apply is the only path back online.

Omitting `components` restores every component in manifest order. For a PostgreSQL-and-Valkey
point, `components: ["postgres"]` or `components: ["valkey"]` requests an explicit partial
Restore. Its stronger confirmation names the untouched component and warns that cross-component
consistency is intentionally broken. Source and destination Resource versions must match exactly.

## Before starting

- Stop or fence unmanaged writers. Gimme drains the public route and stops only the
  workers, Horizon process, and scheduler it manages.
- Confirm the Deployment still points at the intended Target, Backup Destination, PostgreSQL
  Resource, and—when selected—Valkey Resource.
- Use `list_recovery_points` to select a `verified` Recovery Point belonging to this
  Deployment.
- Choose a new request ID. Reuse that same ID for every retry of this Restore; never start
  a second request to recover the first one.

Do not manually rename or drop databases, edit Caddy configuration, start managed units,
or delete the source or Safety Recovery Point while a Restore is unresolved.

## Restore normally

1. Call `plan_restore_deployment` with the Deployment, Recovery Point ID, and request ID.
   Omit `components` for full Restore, or pass exactly the component subset to restore partially.
2. Check `ready`, `readiness_issues`, source/destination provenance and exact versions,
   whether the destination is empty, and the returned confirmation text.
3. Call `apply_restore_deployment` with the same identities and component selector, exact
   `plan_id`, and exact confirmation.
4. Inspect the request with `list_restores` or
   `gimme://deployments/{name}/restores/{request_id}`. A successful first stage reports
   `data_replaced` and `recovery_required: true`; the public route must still return 503.
5. Call `plan_verify_restore` with the Deployment and request ID, then pass its exact
   `plan_id` to `apply_verify_restore`.
6. Confirm the Restore record reports `completed`, `recovery_required` is false, and the
   normal route is healthy.

Before mutation, Gimme captures and verifies one request-bound Safety Recovery Point containing
exactly the selected non-empty or ambiguously empty destinations. PostgreSQL-only Safety contains
only PostgreSQL; Valkey-only Safety contains only the registered prefix. Full Restore usually
protects both, but omits a selected destination that is proven empty; the plan and Restore Record
expose the exact bounded `safety_components` set.
It verifies each exact immutable source object version. Full Restore then loads and checks a
derived PostgreSQL shadow, clears/replays/verifies only the registered Valkey prefix, and performs
the OID-bound PostgreSQL name swap last. The previous database remains available through private
verification and is dropped during retry-safe cleanup.

Valkey replay is binary-safe and prefix-scoped. Persistent keys remain persistent. Expiring keys
retain their original absolute server timestamp, and records already expired at replay time are
skipped. Gimme never accepts a key or pattern, never uses `FLUSH*`, and never restarts, snapshots,
or globally locks the shared service.

Verification resumes only the managed units that were active before maintenance. It tests
database connectivity and runs every configured live-health probe directly through the
current Laravel application, without exposing the route. Failure re-quiesces managed
processes and appends `verification_failed`. Cleanup is followed by a second private check
before the saved route is restored.

## Restore after Target loss

Rebuild and bootstrap the registered Target first, then reconcile the same exact Resource
versions and Deployment bindings. A replacement PostgreSQL database must be empty.
`plan_restore_deployment` reports `destination.empty: true`; PostgreSQL-only Restore can then
skip Safety because there is no current data to protect. Valkey emptiness is prefix-scoped;
ambiguous inspection remains fail-safe and captures the selected prefix even when the replacement
Resource is expected to be empty. Unrelated prefixes do not make that prefix incompatible.
Run the same apply and verification sequence above.

If a replacement Resource version differs from its source component, stop. Restore refuses the
mismatch; install or register the exact source version rather than editing the plan or manually
loading either service.

## Recover from a failed Restore

The destination-authoritative Restore record tells you which call to retry:

| Current state | Action |
| --- | --- |
| `started` through `maintenance_entered` | Request a fresh `plan_restore_deployment` with the same request ID and retry `apply_restore_deployment` |
| `safety_failed` | The original runtime was restored without source mutation. Request a fresh plan with the same request ID and retry; if the prior error was `recovery_runtime_restore_failed`, repair the runtime first |
| `safety_verified` through `shadow_verified` | Request a fresh `plan_restore_deployment` with the same request ID and retry `apply_restore_deployment` |
| `data_replaced` | Run `plan_verify_restore`, then `apply_verify_restore` |
| `verification_failed` | Correct the application, database, process, or health-check fault; request a fresh verification plan and retry it |
| `verification_succeeded` or `cleanup_completed` | Retry a fresh verification plan; cleanup and maintenance exit are idempotent |
| `completed` | No recovery action is required |

After Safety capture succeeds, every failure before completion leaves the public route in
maintenance. A Safety capture failure happens before source mutation, attempts to restore the
original runtime, and records `safety_failed`. There is no MCP force-online bypass. A lost
response is safe to retry: target state, immutable Restore events,
PostgreSQL OIDs, replayed-and-reverified Valkey state, and the protected maintenance-exit receipt
distinguish completed work from work that must still run. A Valkey retry clears and replays the
selected prefix from the verified archive; it never guesses which individual keys completed.

The source Recovery Point and any Safety Recovery Point are deletion-protected until the
Restore reaches `completed`. Tool results, plans, records, logs, and bounded failure codes
never include database credentials, Valkey credentials or prefixes, object keys, local paths,
SQL, binary payloads, absolute key timestamps, or restored data.
