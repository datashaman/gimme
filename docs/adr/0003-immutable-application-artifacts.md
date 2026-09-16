# Immutable application artifacts in versioned object storage

- Status: Accepted
- Date: 2026-09-16

Gimme separates build from release by publishing content-addressed Application Artifacts to a
named, versioned S3-compatible Artifact Store. The artifact's secret-free provenance manifest is
written only after the package verifies and is the authoritative publication and inventory record.
Artifact bytes move directly between registered machines and the store; they never pass through an
MCP response.

Artifact Stores are distinct from Backup Destinations because source-derived packages and
Deployment recovery data have different ownership, retention, access, and disaster-recovery
lifecycles. They reuse the same bounded endpoint, TLS, authentication, encryption, versioning, and
secret-handling principles. Gimme derives all object identities and exposes no arbitrary bucket key
or path input.

The first builder is a registered Build Target selected by Application policy. It uses reviewed
Target capabilities and exact runtime inputs in an isolated derived workspace. A Build Target may
also host Deployments, but building never occurs in a Deployment path and receives no Deployment
runtime secrets or Resource access. Future CI builders may implement the same artifact contract.

Artifact identity has two layers. A reviewed `build_id` hashes the exact source commit, dependency
lockfiles, normalized build policy, runtime and platform capability, and Gimme execution
fingerprint. The `artifact_digest` hashes deterministic package bytes. The first verified
publication for a `build_id` wins; a later build with different bytes is rejected as
non-reproducible rather than silently replacing or forking that identity. Secret values belong to
neither public identity and may authenticate downloads but must not intentionally affect output.

The Laravel package contains the exact tracked source, production Composer dependencies, compiled
frontend output, and required Composer-generated runtime metadata. It excludes Git metadata,
frontend dependency trees, environment files, shared runtime storage, environment-derived Laravel
caches, build caches, logs, special files, and unsafe symlinks. Destination release may link shared
state, apply permissions, run environment-specific Laravel optimize and migration tasks, and execute
health gates, but it never runs Composer or a frontend install/build.

Application build policy may declare bounded build-only SOPS secret references. Apply resolves them
into owner-only temporary material used solely by the fixed checkout, dependency, and build
environment; cleanup precedes packaging and occurs on every exit. Gimme redacts known values and
scans the final workspace and package for exact-value leakage, while treating repository build code
as trusted code that could deliberately exfiltrate its credentials. Deployment runtime secrets and
Resource credentials are never supplied to a build.

Applications own their Build Target, Artifact Store, packaging policy, and build-only secrets.
Build planning is Deployment-contextual: it resolves that Deployment's source and combines the
Application policy with the Deployment's exact runtime pins and platform capability. The resulting
artifact belongs to the Application and is reusable only where those inputs yield the same
`build_id`. Composer and frontend tools are build-side capabilities; destination Targets verify
only what execution requires. Promotion reuses a compatible live artifact and never rebuilds it.

Deployment release mode is explicit. `source` preserves destination checkout/build workflows for
local and preview development only. `artifact` is available at every stage and is mandatory for
staging and production. There is no branch convention, implicit fallback, or deprecation path; this
is a hard alpha state/API boundary.

Each materialized artifact release carries secret-free Gimme metadata and a canonical digest of its
immutable tree. Shared environment and storage plus fixed regenerated Laravel cache locations are
outside that tree. Activation and content-addressed rollback both verify artifact identity, archive
integrity, safe extraction, release metadata, and the immutable-tree digest before health gates and
symlink switching. Release inspection and promotion read artifact metadata rather than Git state;
tampered retained releases fail closed and are never rebuilt implicitly.

Artifact Store policy separates `publisher_auth` from `reader_auth`. Build Targets use publisher
authority for probe, upload, read-back verification, and manifest publication; destination Targets
receive read authority only. Either role may use ambient Target identity or distinct encrypted
secret references, resolved and transferred ephemerally per operation. Gimme observes capability
readiness but external S3/IAM policy remains the enforcement boundary.

The first slice never deletes published Artifact Store objects. Target `keep_releases` still bounds
materialized release directories, but safe store garbage collection requires reachability across
desired Deployments, current and retained rollback releases, and promotion history. That lifecycle
is separate work; artifact inventory reports integrity and degradation without pruning.

## Considered alternatives

- Target-local artifact storage was rejected as authoritative because it does not support durable
  promotion across Targets or survive loss of the build machine.
- Passing artifact bytes through the local stdio MCP server was rejected because it couples large
  data transfer and secrets to an interactive control-plane session.
- Reusing Backup Destinations was rejected because retention and access policy for deployable code
  should not be coupled to recovery data.

## Consequences

- Artifact publication and deployment require content-addressed plan/apply workflows.
- Destination Targets verify provenance and package integrity before activation and never rebuild.
- Artifact rollback verifies the retained immutable release tree before switching.
- Artifact manifests and public projections remain secret-free.
