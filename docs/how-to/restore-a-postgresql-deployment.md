# Restore a PostgreSQL Deployment

Use this runbook to replace one Deployment's target-local PostgreSQL database from a
verified Recovery Point. Restore is intentionally two-stage: data replacement always
stops behind a fixed public 503 response, then a separate content-addressed verification
apply is the only path back online.

Current execution support is PostgreSQL-only. A PostgreSQL-only Recovery Point is selected
fully by default. For a PostgreSQL-and-Valkey point, pass `components: ["postgres"]` to make
an explicit partial Restore. Its confirmation warns that consistency with untouched Valkey
state is intentionally broken. The source and destination PostgreSQL versions must match exactly.

## Before starting

- Stop or fence unmanaged writers. Gimme drains the public route and stops only the
  workers, Horizon process, and scheduler it manages.
- Confirm the Deployment still points at the intended Target, Backup Destination, and
  target-local PostgreSQL Resource.
- Use `list_recovery_points` to select a `verified` Recovery Point belonging to this
  Deployment.
- Choose a new request ID. Reuse that same ID for every retry of this Restore; never start
  a second request to recover the first one.

Do not manually rename or drop databases, edit Caddy configuration, start managed units,
or delete the source or Safety Recovery Point while a Restore is unresolved.

## Restore normally

1. Call `plan_restore_deployment` with the Deployment, Recovery Point ID, and request ID.
   Omit `components` only when every component should be selected; until Valkey execution lands,
   multi-component points require the explicit PostgreSQL selector above.
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

For a non-empty destination, Gimme captures and verifies a request-bound Safety Recovery
Point before preparing the source. It verifies the exact immutable source object version,
loads a derived shadow database, checks it, terminates connections only to the selected
Deployment database, and performs an OID-bound name swap. The previous database remains
available through private verification and is dropped during retry-safe cleanup.

Verification resumes only the managed units that were active before maintenance. It tests
database connectivity and runs every configured live-health probe directly through the
current Laravel application, without exposing the route. Failure re-quiesces managed
processes and appends `verification_failed`. Cleanup is followed by a second private check
before the saved route is restored.

## Restore after Target loss

Rebuild and bootstrap the registered Target first, then reconcile the same exact
target-local PostgreSQL Resource version and Deployment resources. The replacement
database must be empty. `plan_restore_deployment` reports `destination.empty: true`; in
that case no Safety Recovery Point is needed because there is no current data to protect.
Run the same apply and verification sequence above.

If the replacement PostgreSQL version differs from the source, stop. Restore refuses the
mismatch; install or register the exact source version rather than editing the plan or
forcing `pg_restore` manually.

## Recover from a failed Restore

The destination-authoritative Restore record tells you which call to retry:

| Current state | Action |
| --- | --- |
| `started` through `shadow_verified` | Request a fresh `plan_restore_deployment` with the same request ID and retry `apply_restore_deployment` |
| `data_replaced` | Run `plan_verify_restore`, then `apply_verify_restore` |
| `verification_failed` | Correct the application, database, process, or health-check fault; request a fresh verification plan and retry it |
| `verification_succeeded` or `cleanup_completed` | Retry a fresh verification plan; cleanup and maintenance exit are idempotent |
| `completed` | No recovery action is required |

Every failure before completion leaves the public route in maintenance. There is no MCP
force-online bypass. A lost response is safe to retry: target state, immutable Restore
events, PostgreSQL OIDs, and the protected maintenance-exit receipt distinguish completed
work from work that must still run.

The source Recovery Point and any Safety Recovery Point are deletion-protected until the
Restore reaches `completed`. Tool results, plans, records, logs, and bounded failure codes
never include database credentials, object keys, local paths, SQL, or restored data.
