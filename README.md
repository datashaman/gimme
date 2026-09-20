# Gimme MCP

Gimme is an alpha MCP deployment control plane for Ubuntu targets. It keeps desired
state locally, provisions a target through a narrowly scoped privileged helper, and
deploys PHP/Laravel or static applications through Deployer.

Version 0.6 has four explicit resources:

- a **target** is an independently provisioned Ubuntu machine;
- an **application** is reusable source/build metadata;
- a **resource** is a named, version-pinned PostgreSQL or Valkey service;
- a **deployment** places one application revision on one target at one stage.

This is alpha software and 0.6 is a hard state/API break. There are no compatibility
tools. The migration preserves existing remote paths, database identities, cache
prefixes, and URLs while recording observed runtime and service versions explicitly.

## Safety model

Gimme deliberately exposes no arbitrary shell, SQL, hostname, package, service, or
filesystem-path parameters. Remote mutations use reviewable, content-addressed plans.
Applying a stale plan fails closed. Deployment placement is allocated once and cannot
be changed through the update API.

Repository URLs must be credential-free. Authentication belongs in SSH agents,
repository-scoped deploy keys, or credential helpers. The Deployer child receives an
explicit environment allowlist, so unrelated shell credentials are not inherited.

Secrets are bounded `{store, secret, field}` references, never plaintext MCP arguments or
tool output. The built-in `local-sops` store resolves from `secrets.enc.json`; registered
AWS Secrets Manager stores use separate inspection and resolver roles. Values are placed in
an owner-only temporary file, transferred to an owner-only remote temporary file, and
removed after environment reconciliation. See
[`docs/how-to/use-aws-secret-stores.md`](docs/how-to/use-aws-secret-stores.md).

Create or edit the encrypted document with `sops config/secrets.enc.json`; its nested
JSON keys must match the references declared by deployments. Configure age through
`SOPS_AGE_KEY_FILE` or `SOPS_AGE_KEY` in the MCP server's environment.

Production is policy, not a branch convention. A production deployment requires:

- a `public_dns` target and explicit domain;
- an exact commit source;
- `APP_ENV=production` and `APP_DEBUG=false`;
- named health probes at explicitly selected candidate and live phases.

Staging also requires debug off plus candidate and live health gates. Local and preview
deployments are less restrictive. Caddy uses its internal CA for local mDNS and
automatic ACME HTTPS for public DNS.

## Install

```bash
# From the repository root:
uv sync
composer install
```

State defaults to `config/state.json`. Set `GIMME_STATE_DIR` to keep operational state
elsewhere; the directory contains:

```text
state.json          # schema-v8 desired state, fleet capacity, stores, pins, and Rollouts
secrets.enc.json    # SOPS-encrypted secret values
.gimme.lock         # local atomic-write lock
operations.jsonl    # append-only, secret-safe plan/apply/outcome evidence
.gimme-journal.lock # local journal append lock
applied-secrets/    # secret-free Applied Secret Manifest fingerprints
deployment-locks/   # owner-only Deployment resource locks
```

Operational state and encrypted secrets are ignored in this public source repository
because even encrypted documents, hostnames, repository URLs, and secret key names can
reveal private inventory. To make state Git-backed, point `GIMME_STATE_DIR` at a
separate private repository. Legacy 0.4 manifests remain ignored for the same reason.
Copy the example for a new installation, or use the migration tools for an older installation:

1. call `plan_state_migration`; schema-v5 and older inputs also require an explicit release mode
   for every Deployment and any Artifact Store/Application build policy required by artifact mode;
2. review its preserved placements and effects;
3. pass the exact `plan_id` and any required older-schema policy to `apply_state_migration`;
4. commit the resulting desired state only if that repository is intended to hold
   your operational inventory.

All reviewed plans include an `execution_fingerprint` covering Gimme, Deployer, privileged
helper, and dependency-lock inputs. Changing executable control-plane code after planning
invalidates the plan before apply; documentation and test-only changes do not.

See [`config/state.example.json`](config/state.example.json) for the complete shape.
For a public target, use `network.mode: "public_dns"`, omit `mdns_name`, declare one
or more literal `expected_addresses`, and give every deployment an explicit `domain`.
Planning verifies that DNS resolves to a declared address before Caddy is allowed to
request an ACME certificate.

For guided workflows, see:

- [`docs/tutorials/first-local-deployment.md`](docs/tutorials/first-local-deployment.md)
- [`docs/how-to/migrate-a-runtime-to-mise.md`](docs/how-to/migrate-a-runtime-to-mise.md)
- [`docs/how-to/use-aws-secret-stores.md`](docs/how-to/use-aws-secret-stores.md)
- [`docs/how-to/use-backup-destinations.md`](docs/how-to/use-backup-destinations.md)
- [`docs/how-to/place-deployments-on-a-fleet.md`](docs/how-to/place-deployments-on-a-fleet.md)
- [`docs/how-to/prepare-an-artifact-rollout.md`](docs/how-to/prepare-an-artifact-rollout.md)
- [`docs/how-to/restore-a-postgresql-deployment.md`](docs/how-to/restore-a-postgresql-deployment.md)
- [`docs/explanation/control-plane.md`](docs/explanation/control-plane.md)

## Target bootstrap

SSH must work against `bootstrap_hostname`. For local targets, normal operations use
the advertised `hostname`, which lets host-specific SSH settings such as agent
forwarding apply consistently.

The MCP server never handles a sudo password. Perform the initial helper installation
from a terminal:

```bash
uv run gimme-bootstrap-target devbox
uv run gimme-bootstrap-database devbox
```

This keeps one interactive terminal open for sudo and installs root-owned,
policy-bound helpers. Later MCP reconciliations use only those exact sudo rules. Run
the bootstrap command again after changing the target's stack policy or upgrading
Gimme's helper implementation.
The bootstrap command inherits the terminal directly, so the password prompt appears
live and input remains hidden; do not run it through a non-interactive pipe.

The second command grants the deployment user PostgreSQL `CREATEDB` and `CREATEROLE`;
they allow database lifecycle management but do not grant operating-system root.

For a local mDNS target, import the exported Caddy public root once on each development
workstation. The CA private key never leaves the target:

```bash
scp devbox.local:/srv/gimme/apps/.caddy-local-root.crt /tmp/gimme-caddy-root.crt
```

Trust only a CA retrieved from a target you control.

## Runtime versions

Every deployment declares exact runtime versions. A pin has a `provider` and a
`version`; Gimme never resolves ranges such as `latest`, `^22`, or `8.4.*`.

- `system` selects an exact host binary and verifies its full version.
- `mise` installs and executes that exact user-space runtime from
  `<apps_root>/.gimme/mise` without shell activation.
- `bundled` is valid only for npm, whose version is supplied by the selected Node.js.

Set `target.runtimes.mise_version` whenever any deployment uses mise and include
`mise` plus `software-properties-common` in the target stack. On Ubuntu 26.04 Gimme's
privileged helper enables only the fixed official `ppa:jdxcode/mise` source; it never
pipes a remote installer into a privileged shell. Use
`plan_deployment_runtimes` and `apply_deployment_runtimes` to review and install the
declared mise pins. Multiple Node.js, Bun, pnpm, Yarn, Python, Ruby, Go, and Java
versions can coexist because each deployment command runs through
`mise exec tool@version`.

PHP web deployments deliberately use the `system` provider: the exact PHP patch is
verified, Deployer uses `/usr/bin/phpX.Y`, Caddy uses
`/run/php/phpX.Y-fpm.sock`, and queue/Horizon/scheduler units use that same CLI.
Composer is also system-pinned so it cannot silently execute under another PHP.
Application `php_extensions` are exact required capabilities checked before deploy;
their packages remain part of the target's reviewed APT stack.

PostgreSQL and Valkey are named resources with explicit versions and deployment
bindings. The current `target_local` provider permits one version of each service per
target; the model leaves room for external or isolated providers later without
changing deployment identity.

## Frontend builds

Applications may select `npm`, `pnpm`, `yarn`, or `bun`. The deployment declares exact
runtime versions and verifies them before running a build. Gimme accepts
only a validated script name, never a free-form command, and requires exactly one
matching lockfile with no conflicting package-manager lockfiles.

| Manager | Frozen install |
| --- | --- |
| npm | `npm ci --no-audit --no-fund` |
| pnpm | `pnpm install --frozen-lockfile` |
| Yarn 1 | `yarn install --frozen-lockfile --non-interactive` |
| Yarn 2+ | `yarn install --immutable` |
| Bun | `bun install --frozen-lockfile` |

Gimme will not silently substitute a package manager or update a lockfile.

## Run

```bash
uv run gimme-mcp
```

Example stdio client configuration:

```json
{
  "mcpServers": {
    "gimme": {
      "command": "uv",
      "args": ["--directory", "/path/to/gimme", "run", "gimme-mcp"]
    }
  }
}
```

## Workflow

1. Register or migrate targets, applications, and resources; use
   `plan_register_deployment` / `register_deployment` for reviewed explicit or fleet placement.
2. `inspect_target`, then `plan_target_stack` / `apply_target_stack`.
3. `plan_deployment_runtimes` / `apply_deployment_runtimes` to install and verify pins.
4. `plan_deployment_resources` / `apply_deployment_resources` to reconcile routing,
   PostgreSQL, Valkey, runtime values, workers, Horizon, and the scheduler.
5. For artifact mode, `plan_build_artifact` / `build_artifact` on the Build Target. Then use
   `plan_deployment` to review the exact publication (or inspect `artifact_missing`) and Deployer
   task graph, followed by `apply_deployment` with the exact plan. Source mode resolves and
   deploys its reviewed commit directly.
6. Use `list_releases`; `plan_rollback_deployment` / `rollback_deployment` with the exact plan and
   displayed confirmation; deployment-scoped Artisan tools; `deployment_process_status`; and
   `diagnose_deployment` for operations.
7. Use `plan_promotion` / `promote_deployment` to deploy the exact current source commit or live
   artifact from one compatible Deployment to another. Artifact promotion reads verified release
   metadata and never contacts the Build Target. The destination source is pinned only after
   success.
8. Use `plan_remove_deployment` / `remove_deployment` for explicit cleanup.

For an artifact-mode staging or production Deployment, `plan_start_rollout` / `start_rollout`
prepares a separately health-checked candidate at guaranteed `100/0` stable/candidate traffic.
Inspect it with `inspect_rollout` or `gimme://deployments/{name}/rollout`. Preparation reserves one
temporary Target slot, is retryable after interruption, never runs migrations or candidate
background processes, and blocks ordinary deploy/promotion/rollback/update/removal until the
Rollout is completed or reversed. Shift reviewed traffic with `plan_rollout_weights` /
`apply_rollout_weights`; weights assign new cookie-accepting cohorts and are not an instantaneous
global request percentage. Existing signed cohorts stay sticky while their backend is nonzero and
healthy, and failed transitions restore the prior route before desired weights can change.
At `0/100`, `plan_complete_rollout` / `complete_rollout` promote the candidate and hand off
background ownership; `plan_reverse_rollout` / `reverse_rollout` restore stable from any
recoverable phase. Both release temporary capacity only after verified Target cleanup.

See [Build once and deploy an Application Artifact](docs/how-to/use-application-artifacts.md)
for migration, IAM separation, publishing, multi-Target deployment, promotion, rollback,
reproducibility failures, costs, and retention boundaries.

Laravel candidate probes run inside the release before activation. Live HTTPS probes
run after activation and automatically restore the prior release if any live probe
fails. Each probe has a stable `name`, a bounded absolute `path`, and explicit `phases`
chosen from `candidate` and `live`; applications and deployments can add probes to the
primary inherited or overridden health definition. `diagnose_deployment` reports only
secret-safe states, numeric log metadata and HTTP statuses—never log content or decrypted
values. Queue workers use `queue:restart`; Horizon uses `horizon:terminate`, matching
Laravel's graceful restart model and avoiding a PHP-FPM reload.

## MCP surface

Read-only resources:

- `gimme://state`
- `gimme://fleet`
- `gimme://operations`
- `gimme://targets/{name}`
- `gimme://applications/{name}`
- `gimme://applications/{name}/artifacts/{build_id}`
- `gimme://provider-accounts/{name}`
- `gimme://secret-stores/{name}`
- `gimme://artifact-stores/{name}`
- `gimme://resources/{name}`
- `gimme://aws-networks/{name}/valkey-options`
- `gimme://deployments/{name}`
- `gimme://operations/{correlation_id}`

`list_operations` and the operation resources expose a separate append-only audit
journal. It contains bounded object names, plan and correlation IDs, timestamps, phases,
classified outcomes, and safe error codes. Definitions, command arguments and output,
environment values, secret references, and exception text are never recorded.

See [`docs/reference/mcp.md`](docs/reference/mcp.md) for the complete tool and resource
catalog. Runtime schemas returned by `tools/list`, `resources/list`, and
`resources/templates/list` remain authoritative.

## Current scope

Gimme provides the multi-target foundation, local SOPS, bounded AWS Secrets Manager stores,
versioned S3-compatible Artifact Stores, deterministic Laravel artifact publication with Composer
plus npm, pnpm, Yarn, or Bun and protected build-only secrets, and multi-Target artifact
deployment with existing health-gated activation and automatic live rollback. Artifact and source
promotion reuse the current live release without rebuilding, and content-addressed rollback
verifies retained source or artifact releases before activation. Traffic splitting and fleet
scheduling remain future work rather than implied production guarantees.

Resource provider coverage:

| Resource | Local (target-local) | AWS (managed) |
| --- | --- | --- |
| PostgreSQL | ✅ | ✅ (RDS; see [ADR 0008](docs/adr/0008-aws-rds-postgresql-resources.md)) |
| Valkey | ✅ | provisioning, updates, bindings, Laravel contract, inspection, destruction, restore, and credential rotation (ElastiCache; see [ADR 0009](docs/adr/0009-synchronously-durable-aws-valkey.md)) |

The managed AWS RDS PostgreSQL provider covers registration, provisioning, deployment
binding, and non-destructive-by-default cleanup for a single generation of credentials.
Workload credential rotation, Detached Allocation rebind, and destructive instance deletion
are not yet implemented; a Retained Resource tombstone can be forgotten. See
[`docs/reference/mcp.md`](docs/reference/mcp.md#managed-aws-rds-postgresql-resources).

## Development

```bash
# From the repository root:
bash scripts/gimme-verify
```

The project is licensed under the MIT License.
