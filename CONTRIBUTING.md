# Contributing

Gimme is alpha software with intentionally hard schema changes. Open an issue before a
large compatibility or privilege-boundary change so the desired model is explicit.

## Development setup

```bash
# From the repository root:
uv sync
composer install
```

Do not commit `config/state.json`, encrypted operational secrets, hostnames, private
repository URLs, client names, or tokens. Use the documentation-safe example values.

## Checks

Run the complete local verification suite before opening a pull request:

```bash
# From the repository root:
uv run ruff check .
uv run pytest -q
vendor/bin/phpstan analyse deploy.php deploy --level=5
uv run bandit -q -r src scripts
uv run pip-audit
git diff --check
```

Changes to `deploy.php`, `deploy/*.php`, either privileged helper, or desired-state
models require regression tests. Keep remote command inputs structured and
allowlisted.

## Pull requests

Describe the user-visible behavior, migration impact, privilege impact, and checks
run. Update `CHANGELOG.md`, the canonical state example, and the MCP reference when a
schema or tool changes.

Report vulnerabilities through GitHub private vulnerability reporting as described in
`SECURITY.md`; do not open a public security issue.
