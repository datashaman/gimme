# Managed PostgreSQL through bounded Provider Accounts

- Status: Accepted
- Date: 2026-09-16

Gimme introduces Provider Accounts as named control-plane identities for external service
providers. The first provider is DigitalOcean Managed PostgreSQL. Provider-specific capabilities
remain behind the Resource domain model: MCP accepts no arbitrary cloud request, SQL, endpoint,
hostname, or filesystem path.

A managed PostgreSQL Resource owns one Gimme-created DigitalOcean cluster and may serve multiple
Deployments. Each Deployment receives a distinct database and least-privilege role. Existing
clusters are not adopted in the first slice. Provider ownership is established by a stable Gimme
identity and tags; destructive actions require an exact identity match and fail closed on missing
or ambiguous matches.

Provider Accounts hold encrypted references to two authorities. The required operator credential
may read, create, update, and view database credentials but cannot delete. The optional destructive
credential includes deletion authority and is resolved only during an explicitly confirmed cleanup
apply. Provider and workload credentials never appear in desired state projections, observed state,
plans, logs, journals, or tool results. DigitalOcean owns generated workload passwords; Gimme fetches
them just in time and transfers them through protected ephemeral inputs.

Managed cluster intent pins a provider-listed region, PostgreSQL major-version slug, database size
slug, and node count from one through three. DigitalOcean manages minor releases; Gimme reports the
observed semantic version without treating patch movement as drift. Region, major version, size,
and node count are immutable in the first slice. Ordinary reconciliation repairs ownership tags,
firewall rules, databases, users, privileges, and bindings, but reports immutable-field drift
instead of resizing, replacing, or upgrading a cluster.

Clusters use their public endpoint with verified TLS. DigitalOcean trusted sources are the union of
the observed public IPv4 addresses of bound Targets. Each address is discovered through one fixed,
read-only target-side probe rather than caller-provided network input. Failed discovery or changed
addresses require a fresh plan. Fixed, identifier-safe SQL templates run from a bound Target to
establish database ownership, using provider administration credentials ephemerally; MCP exposes no
SQL surface.

Provider mutations run from the Python control plane behind a narrow adapter. Target workflows
remain responsible for bounded machine operations and the binding bridge. Resource creation is
asynchronous: apply starts or resumes reconciliation, polls for a bounded interval, and may return a
safe pending state. Retrying is idempotent and must never create a second cluster after timeout or
client disconnection.

Desired and observed state remain separate. Desired state carries service intent and a stable
ownership key. Ignored observed state caches provider object identity and safe status, and can be
rebuilt from provider tags. Provider state is never silently written into desired state.

Lifecycle defaults to `retain`. Removing a Deployment retains its database and role unless a
separate destructive cleanup is planned and confirmed. Resource destruction requires no bound
Deployments, matching ownership identity, the destructive credential, exact confirmation, and a
recent verified PostgreSQL Recovery Point in independent versioned S3 storage. DigitalOcean native
backups are useful operational protection but are not an independent recovery boundary.

The MCP surface remains domain-oriented: generic Provider Account registration and update,
Resource plan/apply/inspect/remove operations, provider option discovery, and safe Resource status.
There are no DigitalOcean-prefixed tools or generic provider escape hatches.

## Considered alternatives

- AWS RDS was deferred because IAM and network topology would dominate the first tracer bullet.
- Neon and Supabase were deferred because their project and branching abstractions add concepts not
  required to prove the managed Resource contract.
- Attaching to existing clusters was rejected for the first slice because safe adoption requires a
  separate ownership and conflict-resolution protocol.
- Storing workload passwords in Gimme's encrypted secret document was rejected because DigitalOcean
  can remain their authority and provide them just in time.
- Allowing the operator credential to delete was rejected because provider scopes can enforce a
  stronger separation than application-level confirmation alone.
- Treating provider-native backups as deletion protection was rejected because they share the
  cluster provider lifecycle.

## Consequences

- The desired-state schema requires a one-way alpha migration for Provider Accounts and
  discriminated Resource providers.
- Complete destructive cleanup depends on deployment-scoped PostgreSQL Recovery Points.
- Managed Resource reconciliation requires provider API access from the control plane and database
  connectivity from every bound Target.
- Resize, major upgrade, import, private networking, replicas, connection pools, custom storage,
  Advanced Edition, IPv6 trusted sources, and additional providers remain separate work.
