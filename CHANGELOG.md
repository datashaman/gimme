# Changelog

All notable changes to this alpha project are recorded here. Until 1.0, minor releases
may contain deliberate schema and MCP API breaks.

## [Unreleased]

- Added a private append-only, secret-safe operation journal with correlated plan/apply
  outcomes, classified failures, an MCP resource, trace template, and filtered read tool.
- Added fixed official Ubuntu PPA bootstrap for an exact mise version.
- Added a complete MCP primitive reference and manifest validation documentation.
- Consolidated local and CI verification into one command that covers every Deployer
  module, and migrated the scheduled disposable-host smoke test to schema v3.

## [0.6.0] - 2026-09-16

### Added

- Schema-v3 named PostgreSQL and Valkey resources with deployment bindings.
- Deployment-scoped exact runtime pins for PHP, Composer, Node.js, npm, pnpm, Yarn,
  Bun, Python, Ruby, Go, and Java.
- mise runtime installation and execution without shell activation.
- Version-specific PHP CLI, FPM, queue, Horizon, scheduler, and health commands.
- Runtime plan/apply tools and observed schema-v2 migration.

### Changed

- Replaced target-global frontend toolchains with deployment runtime pins.
- Made PHP, Composer, PostgreSQL, and Valkey versions explicit.
- This release is a hard desired-state and MCP API break.

## [0.5.0] - 2026-09-15

- Introduced explicit target, application, and deployment control-plane objects.
- Added stages, promotion, worktree-oriented deployments, health gates, SOPS secret
  references, Horizon, scheduler, and public/local DNS policies.

[Unreleased]: https://github.com/datashaman/gimme/compare/main...HEAD
[0.6.0]: https://github.com/datashaman/gimme/compare/4fb0e97...a9ed1a3
[0.5.0]: https://github.com/datashaman/gimme/commits/4fb0e97
