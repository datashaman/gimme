# Control-plane model

Gimme separates durable intent from observed machine state. The MCP server owns local
desired state; Deployer transports bounded operations; root-owned helpers reconcile
the small subset that needs privilege.

## Four objects

A target identifies an Ubuntu machine and declares its network, APT stack, services,
and exact mise version. An application describes reusable source and build behavior.
A resource names a versioned PostgreSQL or Valkey service on one target. A deployment
combines those objects with a source ref, stage policy, runtime pins, bindings,
environment, secrets, processes, and immutable placement.

This separation permits the same application to have several simultaneous branch,
preview, staging, or production deployments without sharing paths, databases, cache
prefixes, routes, or systemd units.

## Authority boundaries

The local state file is authoritative for intent but is not trusted as arbitrary root
input. Pydantic validates cross-object references and policy before operations are
rendered. Deployer receives an explicit environment allowlist. Privileged helpers
accept fixed executable vectors, target-bound policy hashes, allowlisted packages and
services, and owner-only desired-state files.

Secrets remain references in desired state. SOPS resolves them immediately before a
deployment resource reconciliation; plaintext exists only in restricted temporary
files and is never returned through MCP.

## Why plans are content-addressed

Plans include resolved versions, revisions, identities, effects, and readiness. Their
`plan_id` hashes the complete plan body. Apply tools recompute the plan, so a changed
branch head, DNS answer, package candidate, secret readiness condition, or definition
makes the reviewed ID stale.

The plan body also carries `execution_fingerprint`, a SHA-256 digest of the executable
Gimme, Deployer, helper, and dependency-lock inputs. A code or dependency change therefore
invalidates an already reviewed plan. Documentation and test changes do not, because they
cannot alter the applied operation.

Creation tools are the exception: registration allocates new local identity and does
not contact a target. Updates and remote mutations use plan/apply pairs.

## Runtime ownership

Deployments own runtime pins because two deployments on one target may require
different Node.js, Bun, Python, Ruby, Go, or Java versions. mise provides coexistence
without modifying login shells. npm is bound to its selected Node.js installation.

PHP and Composer are system-pinned because PHP web traffic and managed Laravel
processes cross several system-level components. The PHP pin selects both
`/usr/bin/phpX.Y` and `/run/php/phpX.Y-fpm.sock`.

Target-local PostgreSQL and Valkey currently permit one exact version per target. They
are named resources so external or isolated providers can be added later without
changing deployment identity.

## Deployer recipe modules

`deploy.php` is the task-registration entry point and preserves the task interface
used by the Python control plane. Stable implementation details live under `deploy/`:

- `configuration.php` validates environment and desired-state inputs and renders
  runtime commands.
- `programs.php` contains the bounded health-probe and environment-reconciliation
  programs sent to a target.
- `state.php` renders helper state, site definitions, and policy hashes.

This keeps validation, generated programs, and privileged-helper policy local to one
module each while leaving Deployer task names and ordering unchanged.
