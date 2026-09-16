# Domain docs

Gimme is a single-context repository.

## Before exploring

- Read `AGENTS.md` for the current domain model, security invariants, and verification command.
- Read root `CONTEXT.md` when it exists.
- Read relevant decisions under `docs/adr/`.
- If `CONTEXT.md` does not exist, proceed silently. It is created lazily when domain
  terminology needs deeper treatment.

## Vocabulary

Use the domain terms defined in `CONTEXT.md` and `AGENTS.md`, especially Target,
Application, Resource, and Deployment. Do not introduce synonyms that blur their
established meanings.

## Architectural decisions

Surface any conflict with an existing ADR explicitly rather than silently
overriding it. Gimme currently stores system-wide ADRs under `docs/adr/`.
