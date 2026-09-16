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
- Helper or stack-policy changes require another interactive bootstrap.
- The MCP surface has more plan/apply tools than an unrestricted shell wrapper.
- New privileged behavior requires helper validation, policy binding, tests, and
  documentation.
