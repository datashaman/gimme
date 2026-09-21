# Artifact Rollout acceptance evidence

This matrix is the durable acceptance record for parent issue #6 and slices #171–#174. The
zero-cost operator proof is `tests/integration/rollout_local_scenario.py`; the production behavior
is exercised by the cited tests and the canonical `scripts/gimme-verify` gate.

| # | Parent criterion | Evidence |
|---:|---|---|
| 1 | Artifact-mode staging/production only | `RolloutOrchestrator._context`; `test_rollout_admission_is_bounded` |
| 2 | Reject source, local, and preview | `_context`; `test_source_mode_is_rejected_before_remote_reads`; parametrized stage test |
| 3 | Exactly one Rollout per Deployment | `ControlState.rollouts` keyed by Deployment; generation-conflict and retry tests |
| 4 | Verified live stable and different verified candidate | `_context`, `live_release`, artifact context; start tests |
| 5 | Full compatibility contract | policy/contract/evidence fingerprints; changed-contract and changed-policy tests |
| 6 | Persist `preparing`, reserve, isolate, probe at `100/0` | `start`; `main_prepare`; start and interruption tests |
| 7 | Capacity admission and no duplicate reservation | `fleet_state`; capacity and retry tests |
| 8 | Distinct trees, pools, sockets, runtime, logs, identities | `gimme-provision-rollout`; helper recipe tests |
| 9 | Isolated static backend | `main_prepare` and `internal_sites` static branches; generated-config tests |
| 10 | Share only declared environment/Resources/storage | `gimme:rollout:prepare`; release-contract and policy fingerprints |
| 11 | No Rollout migrations | dedicated Deployer tasks; `test_rollout_prepare_is_zero_traffic_and_dependency_install_free` |
| 12 | Block schema-changing Artisan | `plan_artisan`; `test_rollout_blocks_schema_artisan_but_keeps_ordinary_commands_on_stable` |
| 13 | Stable owns background processes until completion | `background_owner`; start, staged-sequence, and completion tests |
| 14 | Candidate is web-only while active | separate Caddy/FPM candidate and no process provisioning during prepare/weights |
| 15 | Integer weights 0–100 totaling 100 | MCP schema, model/helper validation; invalid-weight tests |
| 16 | Candidate traffic requires ready direct health | `_weights_context`; readiness test |
| 17 | Weighted new cohorts and signed derived cookie | `public_site`; helper cookie test; local operator scenario |
| 18 | Secure/HttpOnly/SameSite=Lax/Path=/ | anchored Caddy response-header policy; helper config test |
| 19 | Valid-cookie stickiness while eligible | Caddy cookie policy; deterministic local scenario |
| 20 | Zero-weight exclusion including old cookies | zero upstream omission; helper and scenario tests |
| 21 | Rotate affinity on terminal/replacement | lifecycle affinity generation and signing-key rotation; lifecycle/scenario tests |
| 22 | Root-owned Target-only key | `signing_key`; mode/rotation/redaction tests |
| 23 | Direct health bypasses affinity | `probe` against loopback stable/candidate; transition failure tests |
| 24 | Preflight, atomic route install, direct/public verify | `main_weights`; orchestration and helper transaction tests |
| 25 | Restore exact prior route/weights on failure | route snapshot/restore; reload and rollback tests |
| 26 | Active health excludes without desired mutation | derived Caddy health policy; config and failed-apply tests |
| 27 | Retry only safe GET/HEAD connection failures | `lb_retry_match`; config and local scenario tests |
| 28 | No autonomous weight/promotion/completion | plan/apply-only MCP surface and operator documentation |
| 29 | Bounded inspection fields | `inspect`, `_validate_observed`; inspection/Target-loss tests |
| 30 | No bodies/logs/cookies/IPs/samples/secrets | public projection and fixed errors; redaction tests and scenario output |
| 31 | Completion requires healthy `0/100` | `_lifecycle_context`; completion-admission test |
| 32 | Transactional process handoff, promotion, cleanup, health, capacity | `main_finalize`, process `refresh`, `complete`; completion tests |
| 33 | Failed completion restores stable transaction | finalization rollback; public-health, cleanup, and rollback-failure tests |
| 34 | Reversal from preparing/active/degraded | `plan_reverse`/`reverse`; parametrized reversal test |
| 35 | Pin both artifacts while recoverable | ordinary deployment/pruning entrypoints blocked; guard test |
| 36 | Return terminal artifacts to ordinary retention | deterministic promoted release and terminal blocker/capacity rules |
| 37 | Block deploy/promotion/rollback/removal/update/pruning | `_require_no_rollout`; enumerated guard test |
| 38 | Shared Resource/secret changes are transactional or rejected | deployment Resource reconciliation guard; guard test |
| 39 | Ordinary Artisan targets stable; restarts affect owner | Artisan policy; process refresh only during finalization; tests |
| 40 | Serialize Rollout/shared mutations | `_deployment_resource_lock`; MCP apply wrappers and lock tests |
| 41 | Bounded Target policy/state | strict helper documents, protected modes, `_validate_observed`; state-shape test |
| 42 | Persist preparation before mutation | `start.reserve`; interruption test |
| 43 | Persist weights only after verified route | `apply_weights`; target-failure and interrupted-persist tests |
| 44 | Idempotent matching-generation retry | start, weights, and terminal-record retry tests |
| 45 | Fail closed on missing/mismatched/ambiguous generations | `_observed`, lifecycle context; drift and mismatch tests |
| 46 | Target loss preserves placement and both slots | fixed Target errors; loss/capacity tests |
| 47 | Plans explain artifact/capacity/process/route/health/phase/rollback/pruning | start, weight, and lifecycle exact-plan builders; plan assertions |
| 48 | Reject stale artifact/capability/capacity/route/process/health/Resource/environment/Rollout | policy/contract/evidence/route fingerprints and apply rechecks; stale-state tests |
| 49 | Complete failure and lifecycle test matrix | `test_rollout.py`, `test_rollout_helper.py`, `test_process_helper.py`, local scenario |
| 50 | Explain cohort rather than instantaneous percentages | operator guide, MCP reference, README, ADR 0006 |
| 51 | Full verification | `bash scripts/gimme-verify` |

## Failure matrix

| Injection | Expected safe result | Automated evidence |
|---|---|---|
| Candidate or stable direct health | Public route unchanged or restored; desired weights unchanged | `test_direct_backend_failure_leaves_public_route_unchanged` |
| Caddy validation/reload | Exact prior route restored | `test_route_reload_failure_restores_exact_prior_route` |
| Public completion health | Release, route, and `current` restored | `test_completion_public_health_failure_restores_release_route_and_current` |
| Process handoff | Target apply fails; local phase degrades and reservation remains | `test_failed_finalization_keeps_reservation_and_records_degraded` plus process refresh test |
| Cleanup | Candidate pool/runtime and route restored | `test_completion_cleanup_failure_rolls_back_candidate_pool_and_route` |
| Rollback itself | Fixed redacted degraded error | route and completion rollback-failure tests |
| Preparing/weight/finalization interruption | Matching generation resumes without another slot/release/route | three interruption/retry tests in `test_rollout.py` |
| Capacity exhaustion | Start rejected before reservation | `test_rollout_admission_is_bounded` |
| Target loss | Fixed error/drift, unchanged placement and reservation | Target-loss tests |

## Reproduction

```bash
uv run python tests/integration/rollout_local_scenario.py
bash scripts/gimme-verify
```

The local scenario has no network or cloud dependency. It prints artifact identifiers, aggregate
cohort counts, health-check counts, and terminal ownership only. Cookies, client identities, keys,
addresses, paths, bodies, logs, commands, secrets, and request samples are deliberately absent.
