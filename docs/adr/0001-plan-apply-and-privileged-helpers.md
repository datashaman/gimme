# ADR 0001: Content-addressed plans and privileged helpers

- Status: Accepted
- Date: 2026-09-16

## Context

Gimme needs to provision packages, services, web routes, local DNS, databases, and
application processes while being controlled through an LLM-facing protocol. Passing
arbitrary commands or sudo credentials through MCP would make prompt mistakes and
injection disproportionately dangerous.

## Decision

Remote mutations use narrow tools with validated structured inputs. Material updates
are split into a read-only plan and an apply tool. The plan body is hashed; apply
recomputes it and rejects stale IDs.

Every plan body includes an `execution_fingerprint`. It hashes the Python control-plane
sources, Deployer recipe and engine sources, privileged helpers, and dependency manifests
and lockfiles. Operational state, secrets, documentation, and tests are excluded. Apply
recomputes this fingerprint, so upgrading or editing executable control-plane code makes
an earlier reviewed plan stale even when desired state and target observations are
otherwise unchanged.

Root operations run through target-bound helpers installed interactively from a
terminal. Their sudoers entries permit only fixed executables, not arbitrary
arguments. Helpers read owner-only desired-state files, validate them again, restrict
executables and packages, and use a policy hash tied to the target definition and
helper source.

The MCP server never receives a sudo password. Repository authentication uses SSH
agent forwarding or credential helpers outside desired state.

## Consequences

- Operators review exact effects and versions before mutation.
- Drift between planning and applying fails closed.
- Executable control-plane drift between planning and applying also fails closed.
- Helper or stack-policy changes require another interactive bootstrap.
- The MCP surface has more plan/apply tools than an unrestricted shell wrapper.
- New privileged behavior requires helper validation, policy binding, tests, and
  documentation.
