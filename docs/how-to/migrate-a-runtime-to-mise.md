# Migrate a runtime to mise

Use this procedure to move a deployment from a global system runtime to an exact
mise-managed version without changing the active release first.

## Update the target policy

Propose a target update that:

- sets `target.runtimes.mise_version` to an exact version;
- includes `software-properties-common` and `mise` in `target.stack.packages`.

Review `plan_update_target`, then pass its `plan_id` to `update_target`.

On Ubuntu 26.04, the helper enables only the fixed official
`ppa:jdxcode/mise`. Repository URLs are not user-configurable MCP inputs.

## Bootstrap the changed helper policy

Call `plan_target_stack`. A changed stack or helper hash reports
`bootstrap_required`. From the Gimme repository root, run:

```bash
uv run gimme-bootstrap-target devbox
```

This is the only interactive sudo step. Re-run `plan_target_stack`; apply it only when
the plan is ready and the exact mise binary version matches desired state.

## Change one deployment pin

Copy the current deployment registration, change only the selected runtime provider
to `mise`, and keep an exact version. For npm, keep npm as `bundled` and move its Node
pin to mise:

```json
{
  "node": {"provider": "mise", "version": "22.22.1"},
  "npm": {"provider": "bundled", "version": "9.2.0"}
}
```

Review `plan_update_deployment`, then apply it with `update_deployment`.

## Install and verify

Review `plan_deployment_runtimes` and apply it. Gimme installs tools under
`<apps_root>/.gimme/mise` and executes commands with `mise exec tool@version`; shell
activation is unnecessary.

Finally call `plan_deployment`. Do not apply the deployment unless its preflight shows
every declared runtime and bound resource at the exact desired version.

PHP web deployments and Composer remain `system` providers because Caddy, PHP-FPM,
Deployer, queue workers, Horizon, and the scheduler must share one explicit PHP
minor-version installation.
