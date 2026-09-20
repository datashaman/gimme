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
bash scripts/gimme-verify
```

See `README.md` for operation, `docs/reference/mcp.md` for the MCP surface,
`CONTRIBUTING.md` for change requirements, and `SECURITY.md` for reporting.

## Agent skills

### Issue tracker

Issues and PRDs are tracked in GitHub Issues for `datashaman/gimme`. See
`docs/agents/issue-tracker.md`.

### Issue workflow

Read the full issue body, labels, and comments before acting. The issue body records
the reported problem and remains useful context. Once an issue is `ready-for-agent`,
its current Agent Brief is the implementation contract: its scope, constraints, and
acceptance-criteria checklist take precedence over the informational criteria in the
issue body. Later discussion does not change that contract unless the Agent Brief is
explicitly amended or superseded.

1. New issues start in `needs-triage`. Triage establishes the problem, desired
   outcome, evidence or reproduction, constraints, and affected domain concepts,
   then consolidates them into one current Agent Brief.
2. Route the issue with exactly one canonical triage label:
   - `needs-info`: ask specific questions and wait; do not guess at requirements.
   - `ready-for-agent`: the issue satisfies the readiness contract below.
   - `ready-for-human`: record the judgment, access, or coordination that requires a
     person.
   - `wontfix`: explain the decision and close the issue.
3. Split large work into independently deliverable vertical slices. Each child issue
   must state its parent, dependencies, and acceptance criteria; keep the parent for
   overall outcome tracking.
4. Implement only `ready-for-agent` issues. Keep changes within the stated scope and
   document material discoveries or scope changes on the issue instead of silently
   expanding it.
5. Work through the Agent Brief acceptance criteria as the implementation checklist.
6. Before creating a PR, review the complete change against the repository's base
   branch. Check correctness, regressions, security invariants, unintended changes,
   and test and documentation coverage; fix every actionable finding and rerun the
   relevant verification. Repeat the review/fix cycle until no actionable findings
   remain.
7. Open a PR that links the issue, describes behavior and risk, and reports the
   verification performed. The implementer posts an acceptance report that maps
   every Agent Brief criterion to concrete evidence such as tests, commands, code, or
   documentation.
8. Before merge, a reviewer checks every Agent Brief acceptance criterion against the
   evidence and current diff. Mark each satisfied checkbox in the Agent Brief. An
   unchecked criterion means the issue is not done: finish the work or explicitly
   amend the brief and record why. Do not defer an unmet criterion merely to close
   the issue.
9. Use a closing keyword such as `Fixes #123` only after that acceptance review, and
   only on the PR that completes the issue. Close non-code work only after an
   equivalent acceptance report and review. Re-triage genuinely new follow-up work
   instead of hiding it in the closed issue.

An issue is `ready-for-agent` only when its Agent Brief lets an unfamiliar agent
implement and verify it without product or security guesswork. The brief must
contain:

- the observable problem and desired outcome;
- bounded scope and explicit non-goals;
- testable acceptance criteria;
- relevant invariants, domain terms, and links to decisions or prior discussion;
- migration, compatibility, privilege, secret-handling, and documentation impact;
- dependencies and an expected verification path.

### Triage labels

Use the five canonical triage labels configured in GitHub. See
`docs/agents/triage-labels.md`.

### Domain docs

Gimme uses a single-context domain layout. See `docs/agents/domain.md`.
