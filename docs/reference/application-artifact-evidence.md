# Application Artifact acceptance evidence

This map reconciles the parent Application Artifact brief in issue #1 with executable evidence.
The canonical scenario is
`test_two_targets_reuse_one_exact_artifact_without_builder_or_repository`; all named tests live in
`tests/test_artifact_build.py`, `tests/test_artifact_deployment.py`, or
`tests/test_artifact_stores.py` unless another file is shown.

| Acceptance criterion | Evidence |
| --- | --- |
| Strict release mode, stage, store/build unions, references, and separate auth | `test_release_mode_policy_has_no_source_or_missing_build_fallback`, `test_artifact_store_auth_is_reference_only_and_local_sops_bound`, and model validation in `gimme.control` |
| Explicit hard state migration | `test_schema_v5_requires_explicit_complete_release_policy`; `plan_state_migration` / `apply_state_migration` |
| Complete reproducible build identity, independent of secret values | `test_build_plan_binds_every_reviewed_identity_input`, `test_build_identity_changes_with_reviewed_inputs`, `test_secret_reference_value_does_not_change_build_id` |
| Idempotence and conflicting-byte rejection | build publication tests for `idempotent` and `non_reproducible_build`; manifest-last logic in `deploy/artifact.py` |
| Isolated, cleaned builder workspace without Deployment/Resource secrets | workspace, cleanup, secret-scope, interruption, and timeout tests in `test_artifact_build.py` |
| Frozen Composer plus npm, pnpm, Yarn 1/2+, and Bun; stale locks rejected | parameterized frontend command/lock tests in `test_artifact_build.py` |
| Required Laravel content and fixed exclusions | archive selection, mutation, link, special-file, environment, cache, and package metadata tests in `test_artifact_build.py` |
| Deterministic package, safe extraction, both digests, read-back, manifest-last | deterministic archive/build tests plus safe extractor and corruption tests in `test_artifact_deployment.py` |
| Build-secret transfer, redaction, cleanup, leakage rejection, and safe failures | build-secret tests plus `test_public_state_and_plans_hide_artifact_auth_and_build_secret_references` |
| Destination performs no build and rejects missing/stale/incompatible/corrupt input | deployment planning/apply tests and `test_two_targets_reuse_one_exact_artifact_without_builder_or_repository` |
| Existing health, migrations, activation, rollback, cleanup, and process behavior | artifact release graph assertions in `test_artifact_deployment.py` and existing deployment lifecycle suites |
| Promotion reuses a compatible exact live artifact | `test_artifact_promotion_reuses_live_publication_without_build_target` and incompatible/stale/failure promotion tests |
| Content-addressed rollback verifies retained artifacts and rejects tampering | `test_content_addressed_rollback_plans_and_applies_exact_release`, `test_rollback_inventory_selects_and_verifies_exact_predecessor`, and retry tests |
| Source mode remains local/preview only | release-mode validation tests and existing source deployment suite |
| Two Targets, reader-only operation, Build Target/Git loss, and exact reuse | `test_two_targets_reuse_one_exact_artifact_without_builder_or_repository`; reader-denial and no-publisher-fallback tests |
| Bounded, safe, non-deleting inventory | inventory empty/degraded/bounds tests in `test_artifact_build.py`; no delete operation exists in the artifact interface |
| Complete MCP discovery/reference and operator workflow | `test_hard_v6_tool_surface`, [MCP reference](mcp.md), and [operator guide](../how-to/use-application-artifacts.md) |
| Complete repository verification | `bash scripts/gimme-verify` |

The two-Target scenario reuses one `build_id`, manifest version, package version, archive digest,
and immutable-tree digest across separate Target roots. Its destination command seam always fails,
proving deployment/promotion materialization and rollback inspection do not invoke Git, Composer,
Node, dependency installation, or frontend builds after publication.
