# Validate managed PostgreSQL locally and against AWS

Use the local scenario for routine proof. Use the AWS harness only in a disposable account and
isolated Gimme state directory: RDS instances, Multi-AZ capacity, storage, backup retention,
snapshots, Secrets Manager versions, and data transfer can incur charges until cleanup finishes.
Check the AWS Pricing Calculator for the selected region and instance class before opting in.

## Run the zero-cost PostgreSQL scenario

The local scenario starts an ephemeral TLS-enabled PostgreSQL cluster under a temporary directory
and invokes the exact fixed bind, login-retirement, and allocation-purge programs embedded in
`deploy/programs.php`:

```bash
uv run python tests/integration/rds_postgres_local_scenario.py
```

It requires PostgreSQL server binaries, `psql`, and OpenSSL. Set `GIMME_POSTGRES_BIN` to the
directory containing `initdb`, `pg_ctl`, and `postgres` when they are not discoverable. The
scenario verifies `verify-full` TLS, two isolated databases, least-privilege roles, the exact
provider extension version, workload activation, login generations, failed-activation rollback,
detachment, same-data rebind, and marker-guarded purge. It destroys its temporary cluster even on
failure and emits only a bounded JSON report. The disposable integration workflow runs this after
installing PostgreSQL; ordinary CI skips the scenario when server binaries are absent.

## Prepare an isolated AWS run

Follow [Use AWS RDS PostgreSQL](use-aws-rds-postgresql.md) for the two-private-subnet network,
Administration Target, security-group ingress, inspection and resolver roles, optional destructive
role, workload Secret Store, and pinned commercial-region trust bundle. Use a fresh bounded run id
and a Resource/Deployment dedicated to the run. Never point the harness at the repository's normal
`config` directory.

The Deployment must already be valid and deployable. Bind a manual Recovery Policy when you want
the harness to prove Recovery prerequisites or perform destructive cleanup. The destructive role
is unnecessary for the default retaining run and mandatory for destruction.

The inspection role creates and converges the instance and writes tagged workload credentials but
cannot read values. The resolver role reads only the RDS master credential and exact workload
credential versions during apply. The destructive role is assumed only for allocation/Resource
destruction. Keep those trust policies distinct and scoped to the identity running Gimme.

## Run while retaining data (default)

Creation and rotation require one explicit opt-in. Destruction is absent by default:

```bash
GIMME_STATE_DIR=/absolute/path/to/isolated-state \
GIMME_AWS_RDS_LIVE_CREATE=1 \
GIMME_AWS_RDS_RESOURCE=example-rds-postgres \
GIMME_AWS_RDS_DEPLOYMENT=example-live \
GIMME_AWS_RDS_RUN_ID=gimme-live-20260921-a \
uv run python tests/integration/aws_rds_postgres_live_smoke.py
```

The harness repeatedly applies the same content-addressed Resource operation through expected AWS
asynchronous phases, requires secret-free ready inspection, creates/reconciles the isolated
binding, activates the version-pinned Deployment credential, rotates it through the normal
health-probed path, and creates a verified manual Recovery Point when a policy is bound. It then
stops and reports `cleanup: retained_by_default`. The RDS instance, databases, credentials,
backups, and snapshots remain billable.

Output is bounded to Resource and Deployment names, topology booleans, readiness, rotation
generation, Recovery status, cleanup state, and final-snapshot verification. It never prints an
endpoint, ARN, username, password, provider response, or Secret value.

## Run separately authorized destruction

Only after reviewing the Recovery Point and accepting permanent database deletion, repeat with a
separate exact flag:

```bash
GIMME_STATE_DIR=/absolute/path/to/isolated-state \
GIMME_AWS_RDS_LIVE_CREATE=1 \
GIMME_AWS_RDS_LIVE_DESTROY=1 \
GIMME_AWS_RDS_RESOURCE=example-rds-postgres \
GIMME_AWS_RDS_DEPLOYMENT=example-live \
GIMME_AWS_RDS_RUN_ID=gimme-live-20260921-a \
uv run python tests/integration/aws_rds_postgres_live_smoke.py
```

This mode requires a manual Recovery Policy and destructive role. It removes the test Deployment,
which disables its login and records Recovery evidence, then applies the exact reviewed Resource
destruction plan until complete. Gimme disables deletion protection, verifies the deterministic
tagged final snapshot, retains automated backups, deletes the instance, and removes only its owned
parameter/subnet groups. The report says `destroyed_snapshot_retained`; Gimme never deletes the
final snapshot, other manual snapshots, or retained automated backups.

## Maintenance, rotation, and failure handling

- Run normal `plan_update_resource` / `update_resource` and apply convergence for maintenance
  windows, backup retention, instance class, same-major engine updates, or storage increases.
- Use only `plan_rotate_resource_credential` / `apply_rotate_resource_credential` for credentials.
  A failed activation restores the previous secret version and login before returning a bounded
  failure.
- Treat `pending`, `modifying`, `snapshotting`, `deleting`, and `cleaning` as resumable states.
  Retry the same plan and confirmation; do not create a second Resource or manually rename AWS
  objects.
- On access denial, update only the documented role action. On ownership/generation mismatch,
  stop and inspect tags; never broaden policy or delete the conflicting object.
- If the live harness fails after creating billable state, keep the isolated state directory and
  rerun the same bounded operation. Do not discard observations or Recovery evidence.

After destruction, inspect AWS for the verified final snapshot and retained automated backups.
Their retention or manual deletion is an explicit operator action outside Gimme. Remove bootstrap
security groups, subnets, VPC, IAM roles, and the isolated state directory only after confirming
they are dedicated to this run and no retained tombstone or snapshot still needs their inventory.
