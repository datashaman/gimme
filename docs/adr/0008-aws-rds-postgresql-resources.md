# AWS RDS PostgreSQL as the first managed Resource provider

- Status: Accepted
- Date: 2026-09-17

Gimme's first managed PostgreSQL Resource provider is AWS RDS for PostgreSQL. One Resource owns one
private, encrypted Multi-AZ DB instance with a synchronous standby across the two data subnets of a
registered AWS Network. The instance may serve many Deployments, but each owns an isolated database,
a stable non-login owner role, generation-specific login roles, and a provider-backed Resource
Credential. Gimme creates only objects bearing its derived ownership identity and never adopts an
arbitrary existing RDS instance.

The AWS Network, EC2 Targets, subnets, routes, and security groups are pre-existing prerequisites.
Gimme creates Resource-owned RDS subnet and parameter groups but does not edit network policy. The
PostgreSQL security group may admit port 5432 only from security groups attached to the explicit
Administration Target and current bound Deployment Targets. The database is never public. All SQL
connections require verified TLS using a fixed versioned AWS RDS trust bundle, and the managed
parameter group forces SSL.

RDS generates, stores, and rotates the master credential in Secrets Manager. A separate exact
credential-resolver role may retrieve its reviewed version only at apply time. Because the local
control plane may not reach a private VPC and standard RDS PostgreSQL exposes no administrative SQL
API, one explicit Administration Target runs a root-owned policy-bound helper. The master credential
is transferred to that helper only through the protected ephemeral execution path; it never enters
the application runtime, command arguments, desired or observed state, plans, resources, journals,
logs, or errors.

Workload credentials are distinct from caller-managed Secret References. Gimme creates a tagged
Secrets Manager secret in one registered Secret Store namespace for each Deployment allocation. The
secret contains only a generation-specific username and a cryptographically random password.
Rotation creates a new login role and secret version, activates and health-checks the Deployment,
then retires the old role. This alternating-login design permits rollback without temporarily
invalidating the working credential. Workload rotation is explicit; only the RDS-managed master
credential follows the provider's default seven-day schedule.

Engine versions are exact AWS-listed PostgreSQL versions. Automatic minor upgrades and RDS Extended
Support are disabled. Reviewed updates may change only the same-major minor version, instance class,
increase allocated gp3 storage, or change backup and maintenance policy. Provider identity, AWS
Network, region, engine major, encryption, and storage decreases require a new Resource and an
explicit Restore or migration. Public databases, read replicas, RDS Proxy, three-instance Multi-AZ
clusters, arbitrary parameter or option input, and automatic storage scaling are outside the first
contract.

RDS reconciliation is asynchronous and resumable. An MCP apply polls for at most 30 seconds before
returning a bounded pending phase. Once started, the plan and Resource generation remain the
operation identity while AWS advances through expected states; unexpected identity, policy, or
provider changes fail closed as drift. Secret-free observations are cached in `observations.json`
and reconstructible from desired state, AWS ownership markers, PostgreSQL catalogs, Secrets Manager
metadata, Target manifests, and Recovery Manifests. Partial creation is retained for resume or
explicit cleanup and never automatically destroyed as rollback.

Deployment unbinding disables its login but retains its database as a Detached Allocation. Rebinding
to the same Resource restores that data identity with a new login generation. Purge requires a
verified PostgreSQL Recovery Point captured no more than 24 hours before write access was disabled,
with no later write-capable state. Whole-Resource destruction verifies the same evidence for every
allocation, creates a final RDS snapshot, retains automated backups for their configured period,
and requires separate destructive authority and exact confirmation.

Ordinary Resource removal is deliberately non-destructive: it removes desired registration and
leaves all AWS objects intact. A Retained Resource tombstone preserves only safe inventory identity,
blocks removal of referenced Provider Accounts and AWS Networks, and receives no reconciliation.
Forgetting the tombstone is a separate confirmed local-only action and does not make the retained
infrastructure adoptable again.

## Considered alternatives

- DigitalOcean Managed PostgreSQL was superseded when AWS Provider Accounts and Secrets Manager
  became the shared provider foundation.
- Existing-instance adoption was rejected because credential provenance, ownership, drift repair,
  and deletion safety would be ambiguous.
- Public RDS endpoints and source-IP firewalls were rejected in favor of same-VPC private access and
  security-group identity.
- IAM database authentication was rejected for the first contract because application token refresh
  would leak provider concerns into Laravel runtimes and would not remove the need for privileged
  database administration.
- Password replacement on one login role was rejected because activation failure would invalidate
  the previously working environment; generation roles provide transactional rollback.
- Automatic deletion after failed create or update was rejected because partial provider state can
  contain the only copy of application data.
- Treating Multi-AZ, automated backups, or final snapshots as independent recovery was rejected;
  Deployment Recovery Points remain the portable recovery boundary.

## Consequences

- AWS Secrets Manager support and PostgreSQL Recovery Points are prerequisites.
- Operators must provide four narrowly scoped roles and preconfigure exact network security-group
  relationships outside Gimme.
- One registered in-VPC Administration Target is required even when the Resource has no active
  Deployment bindings.
- RDS, Secrets Manager, snapshots, and retained infrastructure incur provider charges that Gimme
  reports structurally but does not price or optimize.
- Major upgrades, migration between Resources, capacity policy, richer extensions, observability,
  snapshot pruning, and additional managed providers remain separate work.
