# M2 ledger review fixes

18 November 2026. This records the fixes for the 16:39 working-tree review.

## Changes

- Revision impact follows what a build used. A changed rendering, rule or strength, a revocation, or a removed applicable site or scope key can require refresh. Revisions that add evidence or strengthen approval leave applicable builds current. Builds with only version references use conservative scope matching.
- An episode-bound ledger validates each build ID and every declared use site's chunk membership. The `chunk_id_base` switch defaults to one-based IDs and is pinned to the ledger on reopen. Episode event positions retain the plan's existing convention pending Can's decision.
- The repair path returns the applicable version hints and records the first detection path per build. A new build that still depends on an obsolete version can be marked stale again.
- The ledger checks a triad's approvers against its ballots. Each successful ruling and its `VoteCast` events commit in one SQLite transaction. Votes identify the ruling version, voter and selected rendering, with a null choice for abstention. Votes that led to a principal escalation are also retained.
- Strict replay uses the exact run, call ID and request key. A miss raises `CacheMiss`, including at the token cap. `RECORD_MISSING` can still reuse a response by request key.
- Open decisions are tracked in [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md). The Gemini reservation decision remains with Can.

## Regression tests

New tests

- `test_evidence_only_or_consistency_upgrade_keeps_existing_build_current`
- `test_episode_bound_build_checks_chunk_and_sentence_and_pins_id_base`
- `test_rebuild_using_superseded_version_is_stale_again`
- `test_ruling_without_sites_marks_builds_that_used_removed_scope_key`
- `test_ballot_append_failure_rolls_back_ruling_and_earlier_ballots`
- `test_strict_replay_requires_exact_run_and_call_even_at_token_cap`

The suite also exercises site narrowing in `test_scope_reduction_marks_only_uses_at_removed_sites` and hints and first-detection paths in `test_repairs_at_review_gate_and_final_sweep_are_logged_once`. The authority scenarios now check missing and mismatched ballots, recorded dissent and ballots retained on principal escalation.

## Verification

`ruff format` was applied. `./scripts/check` exited successfully. The staged M1 changes separately passed with 209 tests and 2 deselected. The full review-fix check reported:

```text
All checks passed!
82 files already formatted
Success: no issues found in 44 source files
Contracts: 1 kept, 0 broken.
243 passed, 2 deselected in 32.75s
ledger: 298/316 branches (94.3%)
episode: 50/52 branches (96.2%)
segmenter: 67/68 branches (98.5%)
providers: 145/154 branches (94.2%)
checker: 66/68 branches (97.1%)
baseline: 26/26 branches (100.0%)
```

## Scripted lifecycle integration

The SOC runner now takes a validated public episode, writes builds to the ledger, and runs up to three chunks in flight between scheduled events. It resumes from saved builds and gate records. Scheduled decisions use source sites after their approval boundary. Rebinding preserves the executor's durable ID and gives the successor the brief, current rulings, open flags, and partial output. The same handover snapshot is available on resume. The final sweep repairs old uses and gates every current build. Budget exhaustion publishes partial output and available gate findings.

The principal reads a separate question-to-answer map and can use a trusted fallback for missing questions. A private SQLite cache pins the key per episode and serves normalized questions across conditions. Each logical answer appends a principal-burden event, with idempotency by question ID. The simulator issues the scheduled brief change and the runner applies its declared revisions. The triad supports principal-only and one pass of open discussion alongside cross-examination. A cross-examination callback can refresh the active evidence between steps.

New scenarios

- `test_ledger_runner_rebinds_resumes_repairs_and_gates_completed_episode`
- `test_ledger_runner_allows_three_chunks_in_flight_after_schedule_barrier`
- `test_principal_cache_is_shared_and_each_logical_answer_counts_once`
- `test_only_principal_issues_scheduled_brief_change_and_resume_does_not_duplicate_it`
- `test_form_modes_share_alignment_and_apply_their_configured_resolution` covers two modes.
- `test_cross_examination_validates_against_ruling_available_at_next_step`

`./scripts/check` exited successfully.

```text
All checks passed!
89 files already formatted
Success: no issues found in 48 source files
Contracts: 1 kept, 0 broken.
250 passed, 2 deselected in 40.78s
ledger: 337/366 branches (92.1%)
episode: 50/52 branches (96.2%)
segmenter: 67/68 branches (98.5%)
providers: 145/154 branches (94.2%)
checker: 66/68 branches (97.1%)
baseline: 26/26 branches (100.0%)
```

M2 exit remains open. The SOC runner currently uses injected workers. The model-backed triad worker, reusable term rulings spanning later use sites, triad parity in the RAG arm, paired repair-precision reporting, and end-to-end live condition runs remain to be verified. The frozen episode preference mapping is recorded in [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md).
