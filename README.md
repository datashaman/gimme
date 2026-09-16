# Gimme MCP

Gimme is an alpha MCP deployment control plane for Ubuntu targets. It keeps desired
state locally, provisions a target through a narrowly scoped privileged helper, and
deploys PHP/Laravel or static applications through Deployer.

Version 0.5 has three explicit resources:

- a **target** is an independently provisioned Ubuntu machine;
- an **application** is reusable source/build metadata;
- a **deployment** places one application revision on one target at one stage.

This is a hard API break from 0.4. There are no `app`/`environment` compatibility
tools. The migration preserves existing remote paths, database identities, cache
prefixes, and URLs, but writes them into the new model.

## Safety model

Gimme deliberately exposes no arbitrary shell, SQL, hostname, package, service, or
filesystem-path parameters. Remote mutations use reviewable, content-addressed plans.
Applying a stale plan fails closed. Deployment placement is allocated once and cannot
be changed through the update API.

Repository URLs must be credential-free. Authentication belongs in SSH agents,
repository-scoped deploy keys, or credential helpers. The Deployer child receives an
explicit environment allowlist, so unrelated shell credentials are not inherited.

Secrets are references such as `my-deployment/STRIPE_KEY`, never MCP arguments or
tool output. They are resolved from `secrets.enc.json` with SOPS, placed in an
owner-only temporary file, transferred to an owner-only remote temporary file, and
removed after environment reconciliation.

Create or edit the encrypted document with `sops config/secrets.enc.json`; its nested
JSON keys must match the references declared by deployments. Configure age through
`SOPS_AGE_KEY_FILE` or `SOPS_AGE_KEY` in the MCP server's environment.

Production is policy, not a branch convention. A production deployment requires:

- a `public_dns` target and explicit domain;
- an exact commit source;
- `APP_ENV=production` and `APP_DEBUG=false`;
- a health gate before and after the current-symlink switch.

Staging also requires debug off and a health gate. Local and preview deployments are
less restrictive. Caddy uses its internal CA for local mDNS and automatic ACME HTTPS
for public DNS.

## Install

```bash
uv sync
composer install
```

State defaults to `config/state.json`. Set `GIMME_STATE_DIR` to keep operational state
elsewhere; the directory contains:

```text
state.json          # schema-v2 targets, applications, deployments, secret references
secrets.enc.json    # SOPS-encrypted secret values
.gimme.lock         # local atomic-write lock
```

Schema-v2 `state.json` and SOPS-encrypted `secrets.enc.json` are intentionally
Git-trackable; plaintext secrets are not. Legacy 0.4 manifests remain ignored because
they may contain machine or client identifiers. Copy the example for a new installation,
or use the migration tools for a 0.4 installation:

1. call `plan_state_migration`;
2. review its preserved placements and effects;
3. pass its exact `plan_id` to `apply_state_migration`;
4. commit the resulting desired state only if that repository is intended to hold
   your operational inventory.

See [`config/state.example.json`](config/state.example.json) for the complete shape.
For a public target, use `network.mode: "public_dns"`, omit `mdns_name`, declare one
or more literal `expected_addresses`, and give every deployment an explicit `domain`.
Planning verifies that DNS resolves to a declared address before Caddy is allowed to
request an ACME certificate.

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

The second command grants the deployment user PostgreSQL `CREATEDB` and `CREATEROLE`;
they allow database lifecycle management but do not grant operating-system root.

For a local mDNS target, import the exported Caddy public root once on each development
client. The CA private key never leaves the target:

```bash
scp devbox.local:/srv/gimme/apps/.caddy-local-root.crt /tmp/gimme-caddy-root.crt
```

Trust only a CA retrieved from a target you control.

## Frontend builds

Applications may select `npm`, `pnpm`, `yarn`, or `bun`. The target declares exact
toolchain versions and deployment verifies them before running a build. Gimme accepts
only a validated script name, never a free-form command, and requires exactly one
matching lockfile with no conflicting package-manager lockfiles.

| Manager | Frozen install |
| --- | --- |
| npm | `npm ci --no-audit --no-fund` |
| pnpm | `pnpm install --frozen-lockfile` |
| Yarn 1 | `yarn install --frozen-lockfile --non-interactive` |
| Yarn 2+ | `yarn install --immutable` |
| Bun | `bun install --frozen-lockfile` |

The target administrator owns installation of those exact binaries. Gimme will not
silently substitute a package manager or update a lockfile.

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

1. Register or migrate targets, applications, and deployments.
2. `inspect_target`, then `plan_target_stack` / `apply_target_stack`.
3. `plan_deployment_resources` / `apply_deployment_resources` to reconcile routing,
   PostgreSQL, Valkey, runtime values, workers, Horizon, and the scheduler.
4. `plan_deployment` to review the resolved commit and Deployer task graph, then
   `apply_deployment` with the exact plan.
5. Use `list_releases`, `rollback_deployment`, deployment-scoped Artisan tools, and
   `deployment_process_status` for operations.
6. Use `plan_promotion` / `promote_deployment` to deploy the exact current commit from
   one deployment to another. The destination source is pinned only after success.
7. Use `plan_remove_deployment` / `remove_deployment` for explicit cleanup.

Laravel candidate health runs inside the release before activation. The live HTTPS
health gate runs after activation and automatically restores the prior release on
failure. Queue workers use `queue:restart`; Horizon uses `horizon:terminate`, matching
Laravel's graceful restart model and avoiding a PHP-FPM reload.

## MCP surface

Read-only resources:

- `gimme://state`
- `gimme://targets/{name}`
- `gimme://applications/{name}`
- `gimme://deployments/{name}`

The tools cover state migration, registration and reviewed updates, target inspection
and stack reconciliation, deployment resources, deploy/rollback/promotion/removal,
allowlisted Artisan execution, process status, and allowlisted service status. Query
`tools/list`, `resources/list`, and `resources/templates/list` from the client for the
authoritative schemas.

## Current scope

Gimme 0.5 provides the multi-target foundation and strong production invariants. It
still provisions target-local PostgreSQL and Valkey. Managed cloud databases, backups,
HA, external secret stores, immutable build artifacts, traffic splitting, and fleet
scheduling are intentionally future work rather than implied production guarantees.

## Development

```bash
uv run ruff check src tests scripts/gimme-provision-stack
uv run pytest
vendor/bin/phpstan analyse deploy.php --level=5
uv run bandit -q -r src scripts
uv run pip-audit
```

The project is licensed under the MIT License.
