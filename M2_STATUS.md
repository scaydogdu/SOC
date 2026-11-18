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

## Model-backed SOC worker and reusable terms

The SOC worker now sends structured requests through `ModelClient` for both drafts, cross-examination, open discussion, classification, and voting. Drafts carry decision-use manifests. The worker records open flags, supplies current evidence and revision hints during review, and sends principal questions through the shared answer cache. It preserves prepared prompts and restores recorded token usage when resuming an interrupted model run.

For reusable terminology, a classification identifies the public scope key and a verbatim phrase from each draft. The selected sentence goes into the merged chunk. The selected phrase and the votes become a ledger ruling with sites derived from the public episode. Later workers receive the ruling, and their builds record where they used it. Coverage starts at the originating site and extends into matching later chunks until a scheduled principal approval takes ownership of that scope. The model-backed launcher uses the existing campaign's probe checks and spend guard.

New scenarios and contract checks

- `test_model_triad_records_reusable_term_and_resumes_through_shared_client` verifies a successful challenge, two-of-three vote, three later chunks using the ruling, a transient retry, an interrupted run, and exact cache replay.
- `test_model_triads_support_remaining_form_modes` completes model-backed open-discussion and principal-only episodes with scripted provider responses.
- `test_reusable_term_scope_stops_at_scheduled_principal_approval` exercises the boundary between reusable scopes and scheduled principal approvals.
- `test_soc_launch_requires_effort_probe_before_opening_run_or_spend` exercises launch preflight.

`./scripts/check` exited successfully.

```text
All checks passed!
91 files already formatted
Success: no issues found in 49 source files
Contracts: 1 kept, 0 broken.
255 passed, 2 deselected in 37.85s
ledger: 342/370 branches (92.4%)
episode: 50/52 branches (96.2%)
segmenter: 67/68 branches (98.5%)
providers: 150/160 branches (93.8%)
checker: 66/68 branches (97.1%)
baseline: 26/26 branches (100.0%)
```

M2 exit remains open. Fresh strict replay was verified with one chunk in flight. RAG triad parity, paired repair-precision reporting, and live runs in the SOC conditions remain pending. This execution environment has no configured provider credentials. The published campaign still records failed Gemini probes.

## RAG triad parity and paired repair precision

The model-backed RAG arm now uses the same draft, cross-examination, open discussion, classification, voting and principal callbacks as the SOC arm. RAG retains reusable terms in the glossary and retrieval index. It hands over the RAG notes and partial translation and uses text search to select prior chunks for repair. The shared public schedule defines where scheduled approvals and reusable terms apply. A RAG checkpoint retains agreed terms and principal answers for replay. The campaign condition selects the arm with `memory`. For matched scheduling the paired run uses one chunk in flight in both arms.

The evaluation-side command `python -m eval.repair_precision` reads the frozen repair sites and both run directories. It compares matched runs by scheduled decision and chunk, with a correct selection requiring an annotated repair site in that chunk. It reports counts and precision per decision and overall. The scripted toy selected five repair pairs in RAG and four in SOC, with two annotated pairs in each selection. This is a fixture result in which a pre-approval occurrence adds one RAG text match.

New checks

- `test_rag_uses_same_model_protocol_and_delivers_reusable_term_notes`
- `test_reusable_term_notes_preserve_first_rendering_at_overlapping_sites`
- `test_paired_model_arms_measure_brief_change_repair_selection`

`./scripts/check` exited successfully.

```text
All checks passed!
94 files already formatted
Success: no issues found in 51 source files
Contracts: 1 kept, 0 broken.
258 passed, 2 deselected in 42.56s
ledger: 340/368 branches (92.4%)
episode: 52/54 branches (96.3%)
segmenter: 67/68 branches (98.5%)
providers: 150/160 branches (93.8%)
checker: 66/68 branches (97.1%)
baseline: 34/34 branches (100.0%)
```

M2 live condition verification remains open. The current execution environment exposes zero configured provider families. The guarded launcher still requires passing effort probes for the selected roster.
