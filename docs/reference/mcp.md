# MCP reference

Gimme is a local stdio MCP server built with FastMCP. Its desired state is stored in
schema-v4 JSON; decrypted secrets are never returned by resources or tools.

Runtime schemas returned by MCP discovery are authoritative. This page documents the
stable intent, mutation boundary, and pairing of each primitive.

## Plan and apply rules

- Read-only inspection and planning tools do not mutate local or remote state.
- An `apply_*`, `update_*`, `promote_*`, or `remove_*` tool requiring a `plan_id`
  recomputes the plan and rejects stale or altered IDs.
- Every plan includes an `execution_fingerprint`; executable control-plane or dependency
  changes invalidate its `plan_id` before apply.
- Target, Application, Resource, and Deployment registration tools create local entries
  directly. Provider Account and Secret Store registration is plan/apply because it verifies
  external identity and policy.
- `rollback_deployment` and `remove_deployment` require exact confirmation text.
- Remote operations are restricted to registered targets and validated fields. There
  is no arbitrary shell, SQL, service, package, or filesystem-path tool.

## Resources

| URI | Contents |
| --- | --- |
| `gimme://state` | Complete desired state without decrypted secret values |
| `gimme://operations` | The 50 most recent secret-safe operation events, newest first |
| `gimme://targets/{name}` | One target and its network, stack, and runtime policy |
| `gimme://applications/{name}` | One reusable application definition |
| `gimme://provider-accounts/{name}` | One credential-free provider identity policy |
| `gimme://secret-stores/{name}` | One bounded Secret Store policy and derived ownership tag |
| `gimme://resources/{name}` | One named PostgreSQL or Valkey resource |
| `gimme://deployments/{name}` | One deployment, including pins, bindings, and placement |
| `gimme://operations/{correlation_id}` | One operation trace in chronological order |

The seven parameterized URIs are resource templates. `gimme://state` and
`gimme://operations` are concrete resources.

## State and inventory tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_state_migration` | Read | Inspect installed versions and plan migration to schema v4 |
| `apply_state_migration` | Local write | Apply the exact migration plan atomically |
| `list_targets` | Read | List registered targets and provisioning policy |
| `list_applications` | Read | List application source/build definitions |
| `list_provider_accounts` | Read | List credential-free external-provider identity policy |
| `list_secret_stores` | Read | List bounded Secret Store policy without secret identities or values |
| `list_resources` | Read | List named resources, optionally filtered by target |
| `list_deployments` | Read | List deployments, optionally filtered by target |
| `list_operations` | Read | List recent journal events with exact operation, subject, and correlation filters |

## Operation journal

Gimme appends control-plane evidence to `operations.jsonl` beside desired state. Plan
calls record a `plan` event and return its `correlation_id`. Mutation calls record an
`apply` event before work starts and an `outcome` event afterward under a new correlation
ID. When the supplied plan was previously observed, `plan_correlation_id` links the apply
trace to it. Stale plans and policy rejections are classified without copying exception
text into the journal.

Each JSONL record contains only its schema version, event and correlation IDs, UTC
timestamp, fixed operation name, phase, status, bounded registered object names, optional
plan ID/link, and an optional safe error code. Gimme never journals definitions, command
arguments or output, environment values, secret references, or exception messages.
Correlation IDs are generated after a plan is hashed, so they do not alter `plan_id` or
make otherwise identical plans differ. Journal files and locks use mode `0600`; reads fail
closed if a record does not validate. `list_operations` returns newest events first and is
bounded to 200 records per call.

## Registration and update tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_register_provider_account` | Provider read | Verify both exact AWS roles and plan registration |
| `register_provider_account` | Local write | Reverify and register an AWS Provider Account |
| `plan_update_provider_account` | Provider read | Reverify and plan an account policy update |
| `update_provider_account` | Local write | Apply an exact account update plan |
| `plan_remove_provider_account` | Read | Plan local removal when no store references the account |
| `remove_provider_account` | Local write | Remove only the local account registration |
| `plan_register_secret_store` | Provider read | Verify region and inspection identity and plan store registration |
| `register_secret_store` | Local write | Register a bounded AWS Secrets Manager store |
| `plan_update_secret_store` | Provider read | Plan a bounded store policy update |
| `update_secret_store` | Local write | Apply an exact store update plan |
| `plan_remove_secret_store` | Read | Plan local removal when no Deployment references the store |
| `remove_secret_store` | Local write | Remove only the local store registration |
| `register_target` | Local write | Register a target |
| `plan_update_target` | Read | Diff a proposed target update |
| `update_target` | Local write | Apply an exact target update plan |
| `register_application` | Local write | Register reusable source/build metadata |
| `plan_update_application` | Read | Diff a proposed application update |
| `update_application` | Local write | Apply an exact application update plan |
| `register_resource` | Local write | Register a named, exact-version PostgreSQL or Valkey resource |
| `plan_update_resource` | Read | Diff a proposed resource update |
| `update_resource` | Local write | Apply an exact resource update plan |
| `register_deployment` | Local write | Register a deployment and allocate immutable placement identities |
| `plan_update_deployment` | Read | Diff a deployment update while preserving placement |
| `update_deployment` | Local write | Apply an exact deployment update plan |

## Target and runtime tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `inspect_target` | Remote read | Inspect OS, services, helpers, TLS, SSH agent, sites, and global tools |
| `plan_target_stack` | Remote read | Resolve APT candidates and helper readiness |
| `apply_target_stack` | Remote write | Reconcile packages, services, sites, and mDNS through the installed helper |
| `plan_deployment_runtimes` | Read | Show exact runtime pins, mise version, and required PHP extensions |
| `apply_deployment_runtimes` | Remote write | Install declared mise pins and verify every runtime/resource version |

The MCP server never accepts a sudo password. Run `uv run gimme-bootstrap-target
<target>` in a terminal when a target plan reports `bootstrap_required`.

## Deployment lifecycle tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_deployment_resources` | Read | Plan routing, database/cache identities, environment, secrets, and processes |
| `apply_deployment_resources` | Remote write | Reconcile the exact resource plan |
| `plan_deployment` | Remote read | Resolve one commit, verify pins, and render the Deployer graph |
| `apply_deployment` | Remote change | Deploy the exact reviewed revision with health gates |
| `list_releases` | Remote read | List retained releases and the current release |
| `rollback_deployment` | Remote change | Restore the previous release after exact confirmation |
| `plan_promotion` | Remote read | Pin the live commit from one deployment for another |
| `promote_deployment` | Remote change | Deploy and record the exact promoted commit |
| `plan_remove_deployment` | Read | Plan route, process, data, and release cleanup |
| `remove_deployment` | Remote change | Perform exact confirmed cleanup and remove local registration |

Each named health probe explicitly selects `candidate`, `live`, or both phases. Candidate
probes run before the `current` symlink switch. Live HTTPS probes run afterward and
restore the previous release if any live probe fails. Applications and deployments can
add probes to the primary inherited or overridden health definition.
For deployments with Horizon, queue workers, or a scheduler, planning also verifies the
content-bound privileged process helper and required PHP process extensions. Apply is
blocked before deployment when that preflight reports `bootstrap_required` or a missing
extension.

### Secret planning and resolution

Deployment secrets are structured `{store, secret, field}` references. The fixed
`local-sops` store accepts no path and always uses `secrets.enc.json` in the state
directory. AWS stores derive a complete name from a registered prefix and require the
derived `gimme:secret-store=<store-name>` ownership tag.

Planning reads SOPS encrypted structure or calls AWS `DescribeSecret` through the
inspection role. Returned plans contain environment keys and reference/version
fingerprints, not ciphertext, AWS version IDs, ARNs, provider errors, or values. Apply
revalidates metadata, obtains the exact reviewed version through the distinct resolver
role, resolves every field before target mutation, and sends values only through protected
temporary material. Each field is limited to 8 KiB, the combined payload to 64 KiB, and a
Deployment to 128 secret environment keys. See
[`use-aws-secret-stores.md`](../how-to/use-aws-secret-stores.md) for IAM, KMS, rotation,
migration, and failure behavior.

## Application operation tools

| Tool | Access | Purpose |
| --- | --- | --- |
| `plan_artisan` | Read | Plan a structured, allowlisted Artisan command |
| `run_artisan` | Remote change | Run the exact reviewed Artisan invocation |
| `deployment_process_status` | Remote read | Inspect queue, Horizon, and scheduler systemd units |
| `diagnose_deployment` | Remote read | Run fixed secret-safe release, Laravel, database, filesystem, FPM, log-metadata, and configured live-health checks |
| `target_service_status` | Remote read | Inspect `postgresql`, `valkey-server`, or `caddy` |

## Runtime providers

| Provider | Meaning |
| --- | --- |
| `system` | Verify an exact host binary version |
| `mise` | Install and execute an exact version under `<apps_root>/.gimme/mise` |
| `bundled` | npm supplied by the selected Node.js; invalid for every other runtime |

PHP and Composer currently require `system`. Caddy and managed Laravel processes use
the PHP pin's `/usr/bin/phpX.Y` and `/run/php/phpX.Y-fpm.sock`. Node.js, Bun, pnpm,
Yarn, Python, Ruby, Go, and Java can use mise.
