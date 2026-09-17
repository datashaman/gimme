# AWS Provider Accounts and external Secret Stores

- Status: Accepted
- Date: 2026-09-17

Gimme introduces AWS as its first shared external-provider foundation and AWS Secrets Manager as its
first external Secret Store. Provider Accounts describe bounded control-plane identity and role
assumption. Secret Stores describe bounded secret locations and policy. Deployments retain only
structured Secret References; provider-specific ARNs, versions, credentials, and request parameters
are not MCP inputs.

AWS authentication starts from the standard ambient credential chain. Gimme stores no static AWS
access keys and accepts no arbitrary profile, external ID, or session parameter. An AWS Provider
Account declares its expected account ID and two exact same-account roles. The inspection role can
read secret metadata but cannot retrieve values. The resolver role can retrieve values for the
configured namespace and may decrypt one declared customer-managed KMS key. Planning and apply use
different roles so the no-plaintext planning boundary is enforced by IAM as well as code.

Provider Account registration verifies that the ambient identity can assume both roles and that
each resolves to the expected account. Secret Store registration verifies identity and bounded
configuration, but does not use broad secret enumeration to infer permissions. Access to a concrete
secret is verified only when a Deployment references it.

An AWS Secret Store belongs to one Provider Account and declares one SDK-known region, one bounded
name prefix, the ownership tag `gimme:secret-store=<store-name>`, and one KMS policy. A secret must
match both prefix and tag. The KMS policy accepts either the regional AWS-managed Secrets Manager
key or one exact same-account, same-region customer-managed key. Cross-account secrets, replicated
cross-region lookup, aliases, multiple allowed keys, and caller-provided encryption contexts are
outside the first contract.

A Secret Reference selects one top-level string field using a registered store, a bounded relative
secret identity, and a field name. Gimme derives the provider name. Callers cannot provide an ARN,
URI, version, staging label, JSONPath, or plaintext. AWS values must be JSON objects with unique
top-level keys and string values. Binary, nested, multiline, NUL-containing, oversized, and file
secrets require separate future contracts.

Planning uses secret metadata to resolve the unique `AWSCURRENT` version and returns only safe
reference and version fingerprints. It never calls a value-retrieval operation. AWS metadata cannot
prove that a requested JSON field exists, so field presence is an apply-time validation. Apply
repeats metadata resolution, rejects stale versions, and requests the exact reviewed version ID
rather than implicitly reading whichever version is current at execution time.

All selected values resolve and validate in control-plane memory before target upload. Plaintext is
placed only in owner-readable temporary material, sent through the existing protected execution
boundary, and removed on every exit path. Targets never receive AWS credentials. Plaintext is never
stored in desired state, observed control-plane state, MCP results, plans, resources, journals,
diagnostics, logs, or exception text.

Secret activation is transactional at the Deployment boundary. Under the resource lock, Gimme
preserves the current protected environment and Applied Secret Manifest, atomically installs the new
environment and manifest, rebuilds framework caches, restarts configured long-lived processes, and
runs live health probes. Failed activation restores and reactivates the previous state. Failure to
restore health produces a bounded degraded outcome without exposing provider or secret details.

The Applied Secret Manifest contains environment keys and hashes of store, reference, and exact
version identity. It contains no secret name, ARN, plaintext, or value hash. Later plans use it to
classify current, rotated, missing, and unknown state without retrieving values. A missing or corrupt
manifest requires reconciliation but does not erase or invalidate the current environment.

Local SOPS remains a built-in Secret Store bound exclusively to Gimme's fixed encrypted document.
Existing string paths migrate once to structured store, secret, and field selections with no legacy
compatibility layer. SOPS planning reads encrypted structure and fingerprints selected ciphertext;
it does not decrypt. Apply requires that ciphertext fingerprint to match the reviewed plan before
decrypting.

Provider Account and Secret Store registration and updates use content-addressed plans. Their local
removal is also planned and is blocked by references, but never mutates AWS. Gimme does not create,
edit, rotate, tag, or delete AWS secrets, IAM roles, policies, or KMS keys. AWS owns secret lifecycle;
Gimme observes a rotated version on the next Deployment resource plan and applies it only through an
explicit reviewed operation.

## Considered alternatives

- DigitalOcean-specific identity was rejected as the platform foundation once RDS and Secrets
  Manager became the intended managed-service path.
- Giving Targets direct Secrets Manager access was rejected because it spreads cloud authority to
  every application machine and departs from the existing control-plane resolution boundary.
- One role for metadata and plaintext was rejected because code-only separation cannot prevent a
  planning regression from retrieving values.
- Static access keys in desired state or the SOPS document were rejected in favor of ambient
  identity and exact role assumption.
- Plan-time value retrieval was rejected even when output is redacted; metadata pins the reviewed
  AWS version, while local SOPS exposes encrypted structure.
- Automatic secret rollout was rejected because rotation can break an application and therefore
  requires reviewed, health-gated activation.

## Consequences

- AWS Provider Accounts become a prerequisite for the first RDS PostgreSQL provider.
- Operators must provision roles, scoped policies, ownership tags, Secrets Manager values, and any
  customer-managed KMS key outside Gimme.
- Plans can validate AWS secret and version availability but not JSON field presence.
- Every external secret update requires a new content-addressed Deployment resource plan.
- Secret-file mounts, cross-account secret access, other providers, and automatic rotation remain
  separate work.
