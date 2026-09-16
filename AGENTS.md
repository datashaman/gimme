# Gimme agent context

Gimme is an alpha Python/FastMCP deployment control plane for Ubuntu targets. Deployer
executes bounded remote workflows; root changes go through policy-bound helpers in
`scripts/`.

## Domain model

- Target: machine identity, network policy, APT stack, and exact mise policy.
- Application: reusable repository, framework, build, health, Artisan, and PHP
  extension metadata.
- Resource: named, exact-version PostgreSQL or Valkey service on one target.
- Deployment: application source at one stage, with runtime pins, resource bindings,
  immutable placement, environment values, secrets, and processes.

Desired state is schema v3 in `config/state.json`. That file is operational inventory
and intentionally ignored. `config/state.example.json` must always validate.

## Invariants

- Never add arbitrary shell, SQL, package, service, hostname, or filesystem-path MCP
  inputs.
- Keep remote mutations behind content-addressed plan/apply pairs.
- Never return decrypted secret values from resources, tools, plans, logs, or errors.
- The MCP server never receives sudo passwords. Interactive bootstrap stays in the
  terminal CLI.
- Privileged helpers accept fixed executables, validated state, and target-bound
  policy hashes. Treat changes to them as security-sensitive.
- Preserve unrelated worktree changes and operational state.

## Verification

```bash
# From the repository root:
uv run ruff check .
uv run pytest -q
vendor/bin/phpstan analyse deploy.php deploy --level=5
uv run bandit -q -r src scripts
uv run pip-audit
git diff --check
```

See `README.md` for operation, `docs/reference/mcp.md` for the MCP surface,
`CONTRIBUTING.md` for change requirements, and `SECURITY.md` for reporting.
