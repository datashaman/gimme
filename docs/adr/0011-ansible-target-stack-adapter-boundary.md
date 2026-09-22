# ADR 0011: Bounded Ansible-backed Target stack adapter boundary

- Status: Accepted
- Date: 2026-09-22

## Context

Gimme's `ubuntu-systemd` Execution Profile (ADR 0010) reconciles Target package,
service, hostname, Avahi, Caddy, and mise state through `DeployerRunner`-transported
Deployer tasks and the `gimme-provision-stack` privileged helper. This duplicates
behavior a battle-tested reconciliation engine already provides, which increases
Gimme's own validation and maintenance surface for host state that is not, itself,
part of Gimme's control-plane identity.

Issue #206 asks for a migration architecture in which Gimme keeps owning typed desired
state, content-addressed plan/apply, policy boundaries, secret redaction, and
identity, while a reviewed engine performs host reconciliation. Ansible is the
candidate first replacement backend for Target stack reconciliation. This decision
defines only the adapter boundary and a non-mutating rendering seam; it does not
select Ansible as a live execution path.

## Decision

Add a `TargetStackRenderInput` contract and a first `AnsibleTargetStackRenderer`
implementation in `gimme.target_stack_adapter`, with these properties:

- **Input is derived, never caller-supplied.** `render_target_stack_input` builds a
  `TargetStackRenderInput` only from one validated `TargetConfig` (its `stack`,
  `network`, and `runtimes` policy) and its currently derived `target_sites` output.
  It accepts no MCP parameter, CLI argument, playbook path, shell command, host path,
  or secret value. The resulting model is a fixed, `extra="forbid"` schema with the
  same name/version/pattern constraints already enforced on that desired state.
- **Rendering is pure and non-mutating.** `AnsibleTargetStackRenderer.render` is a
  deterministic function of its validated input to a plain, JSON-serializable,
  secret-free dict: equal input produces byte-equal output. The module imports no
  process, network, or SSH transport (no `subprocess`, `socket`, Deployer runner, or
  privileged-helper invocation), so it cannot contact a Target or mutate local or
  remote state.
- **Render and apply are distinct contracts.** `TargetStackAdapterRenderer` and
  `TargetStackAdapterApplier` are separate `Protocol`s. The initial Ansible adapter
  implements only the renderer protocol; it defines no `apply` method and is not an
  instance of the applier protocol. Adding a real apply path is a future, separately
  reviewed decision.
- **Not a live execution path.** This slice adds no MCP tool, CLI argument, desired
  state field, or environment configuration that can select the Ansible adapter.
  `plan_target_stack`, `apply_target_stack`, `inspect_target`, and
  `gimme-bootstrap-target` keep dispatching to `TargetRuntimeOrchestrator` and the
  existing Deployer/privileged-helper path, unchanged. Existing Target desired state
  needs no migration and this seam produces no remote change.
- **Fingerprint coverage is automatic.** `execution_fingerprint` already hashes every
  file under `src/gimme/**/*.py` (ADR 0001), so `gimme.target_stack_adapter` is a
  fingerprinted executable input from the moment it exists: editing the renderer or
  its contract invalidates a previously reviewed plan the same way any other
  control-plane source change does.
- **Fixed, non-secret validation failure.** Input that fails the contract's own
  pattern/shape validation raises `TargetStackAdapterError` with a fixed message; the
  rejected value itself is never interpolated into the error.

Deferred to future, separately reviewed decisions:

- Selecting, invoking, or implementing Ansible remote apply against a Target.
- Installing Ansible on a Target, or retaining any Ansible fact cache, inventory, or
  run state anywhere.
- Replacing `gimme-provision-stack`, Target bootstrap, Deployer transport, Caddy/site
  reconciliation, Deployment process/runtime reconciliation, or Recovery Schedule
  reconciliation.
- Any new Target/Deployment schema field, MCP tool or input, or execution-profile
  migration.
- Every AWS resource adapter (OpenTofu/Terraform, CloudFormation, or another bounded
  engine) and any Kubernetes or continuously running controller.

## Consequences

- The seam gives a later Ansible apply implementation a concrete, tested contract to
  target without widening today's MCP surface or weakening plan/apply, target-bound
  policy, or secret-handling invariants.
- Because the renderer is pure and unreachable from any tool or CLI entry point, this
  slice carries no operational risk to existing Targets; `bash scripts/gimme-verify`
  and the existing Target stack tests are unaffected.
- A future decision must still specify the real Ansible transport (inventory source,
  connection plugin, fact-cache location and ownership, execution-fingerprint
  coverage of playbook/role sources, and how apply integrates with content-addressed
  plan/apply and target-bound policy hashes) before any Target is allowed to select
  it.
- Renderer output intentionally does not encode a runnable playbook; it is a bounded
  vars/inventory representation. Defining the actual Ansible role/playbook sources,
  and fingerprinting them, is part of the deferred apply decision.

## Considered alternatives

### Wire the renderer into `plan_target_stack` behind a feature flag

Rejected. A selectable-but-inert flag is still a new MCP-observable input surface and
invites accidental selection before an apply path, secret-handling, and fact-cache
story exist. Keeping the adapter unreachable from every tool and CLI argument is
simpler to verify and matches the issue's requirement that this slice make no remote
change.

### Render a literal Ansible playbook now

Rejected. A playbook is executable content; committing to its shape before the apply
transport, role layout, and fact-cache ownership are decided would likely require a
breaking rewrite. A bounded vars/inventory representation is enough to prove the
contract without prematurely fixing execution details.
