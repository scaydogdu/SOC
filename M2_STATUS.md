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

## Offline exit verification

Both arms now record the classification, required and actual approval levels, individual ballots, and selected rendering for resolved flags. An offline audit counts a non-abstaining vote at an independently annotated brief-gap site even if the model called it style. Site annotations stay in evaluation. A scripted normative disagreement received two votes for one draft and one for the other. Both arms applied the principal's answer under the unanimity rule.

A model step now carries forward the ledger sequence observed during cross-examination. The new scenario commits a ruling revision during a review call, checks that the next response receives its hint, and validates a citation to the new version. When a SOC run ends early, publication checks the saved chunks against the active brief and current ledger. This scoring read leaves the prior gate and repair history intact for resume.

| M2 scripted scenario | Verified behavior |
| --- | --- |
| 1. Reused terminology | `test_model_triad_records_reusable_term_and_resumes_through_shared_client` records a two-of-three term vote and later uses. |
| 2. Normative dissent | `test_normative_dissent_escalates_and_offline_audit_counts_votes_on_brief_gaps` applies the principal's answer in both arms. |
| 3. Brief gap voted on | The same test counts the vote on an annotated taste question classified as style, and verifies a recognized gap goes directly to the principal. |
| 4. Fabricated citation | `test_cross_examination_aligns_sentences_and_caps_questions` treats the answer as absent. |
| 5. Silent decision | `test_capture_records_verifiable_manifest_entries_and_silent_uses` records the undeclared rendering. |
| 6. Concurrent rulings | `test_two_authorized_triads_collide_on_same_subject_and_site` records one binding vote result and one conflict. |
| 7. Stale and copied usage | `test_gate_checks_latest_rulings_tombstones_and_strength_by_site` and `test_repairs_at_review_gate_and_final_sweep_are_logged_once` exercise the stale earlier build and blocking of old output in a later chunk. |
| 8. In-flight revision | `test_model_cross_examination_receives_ledger_revision_on_next_step` commits a real revision between the review and the answer. |
| 9. Executor replacement | `test_ledger_runner_rebinds_resumes_repairs_and_gates_completed_episode` checks open flags, partial output and a monotonic ledger sequence. |
| 10. Brief change | `test_paired_model_arms_measure_brief_change_repair_selection` checks both scheduled revisions and reports repair precision for each arm. |
| 11. Planted error | M3 placeholder as specified in the plan. |
| 12. Budget exhaustion | The ledger-runner resume scenario and `test_budget_exhaustion_keeps_and_scores_partial_output` check partial scoring. The SOC regression verifies that published findings use the revised brief while resume still sees the original gate history. |
| 13. Retry and resume | The reusable-terminology model scenario and `test_process_killed_after_cache_write_resumes_without_repeating_call` check retry and resume from saved work. |
| 14. Rebuild and acceptance | `test_repairs_at_review_gate_and_final_sweep_are_logged_once` verifies that final acceptance uses the new builds. |

`./scripts/check` exited successfully.

```text
All checks passed!
96 files already formatted
Success: no issues found in 52 source files
Contracts: 1 kept, 0 broken.
261 passed, 2 deselected in 39.48s
ledger: 344/374 branches (92.0%)
episode: 52/54 branches (96.3%)
segmenter: 67/68 branches (98.5%)
providers: 150/160 branches (93.8%)
checker: 66/68 branches (97.1%)
baseline: 34/34 branches (100.0%)
```

M2 still requires a live episode in every SOC condition. No provider family was configured in this environment at verification time. The existing campaign's effort probes and spend cap remain the launch controls.

## Commitment timeline export

`uv run --locked --offline python -m analysis.timeline RUN_DIR EPISODE_DIR --out PAGE.html --json DATA.json` exports schema version 1. Supply `--label` to label a run whose manifest lacks a label. The [scripted toy page](docs/examples/commitment-timeline.html) was generated from the scheduled SOC runner with deterministic fixture timestamps and event IDs.

The exporter opens one read-only SQLite snapshot. It checks the existing event hashes and the public episode binding before writing the page or JSON. The page embeds its data and uses system fonts, inline CSS, and inline JavaScript. Unknown event types and events with unresolved placement remain visible in the log and the unplaced-events note.

Display choices

- The first approved version fixes each commitment row's position. Chunk columns follow the public schedule and the ID base pinned by the ledger.
- A build's matrix cell becomes blocked when a recorded refusal follows that commitment's stale mark. The gate header shows the latest verdict for each build and the detail panel retains all verdicts. Historical verdicts without a build number attach to the build current at the time.
- Missing sentence-level use records display as missing. An episode without a manifest ID uses a short fingerprint of the public episode. The JSON omits repeated provenance graphs and contexts.

`test_timeline_scripted_toy_golden_round_trip_and_read_only` compares the committed JSON golden and example page, checks the ledger file hash, and verifies the mode of the SQLite connection. The other timeline tests cover corruption, episode mismatch, the four matrix statuses, unknown and unplaced events, legacy gates and ballots, safe output paths, and zero-based chunk IDs.

The headless Chrome test `test_timeline_chrome_phone_interactions_without_script_errors` passed locally. It runs under the nightly marker so the standard suite can run without Chrome. It checks phone-width scrolling, matrix and detail selection, log clearing, vote filtering, and visibility of an unknown event.

`./scripts/check` exited successfully.

```text
All checks passed!
100 files already formatted
Success: no issues found in 53 source files
Contracts: 1 kept, 0 broken.
270 passed, 3 deselected in 42.69s
timeline: 35/36 branches (97.2%)
ledger: 352/382 branches (92.1%)
episode: 52/54 branches (96.3%)
segmenter: 67/68 branches (98.5%)
providers: 150/160 branches (93.8%)
checker: 66/68 branches (97.1%)
baseline: 34/34 branches (100.0%)
```

A fresh interactive zsh session now sees all three provider families. Live M2 remains open pending the spend-guard changes and campaign checks in the new live handoff.

## Live campaign spend guard and operator handoff

A physical attempt now reserves the UTF-8 byte length of its provider JSON body at the configured input rate and the explicit output limit at the configured output rate. The body includes the messages, system text and structured output schema. A schema-repair retry builds and reserves its new body separately. Model limits validate the request before dispatch, and the recorded input and output reservations independently bound settlement. The byte estimate is an operational bound checked against usage, including provider processing that the client cannot tokenize locally.

All three adapter output limits include billed reasoning tokens, so the formula uses one output allowance.

- OpenAI Responses `max_output_tokens` limits generated tokens including hidden reasoning and formatting. [OpenAI token counting](https://developers.openai.com/api/docs/guides/token-counting)
- Anthropic Messages `max_tokens` includes thinking and visible output for the current turn. [Anthropic thinking documentation](https://platform.claude.com/docs/en/build-with-claude/thinking)
- Gemini GenerateContent `generationConfig.maxOutputTokens` caps the combined thought and candidate tokens. The adapter retains the selected `thinkingLevel` and uses a single direct request without continuation. [Google GenerateContent thinking limits](https://aistudio.google.com/docs/thinking?generate-content=true#token-limits)

The test draft with a 768-token output limit reserves under USD 0.10 on all nine roster models, and three trial reservations fit concurrently under USD 40. The existing campaign limit of 4,096 output tokens alone costs USD 0.3072 at the GPT-6 Astra output ceiling. The campaign configuration still uses 4,096. This arithmetic makes USD 0.10 an unsuitable upper-bound target for that combination.

### Historical input audit

`uv run --locked --offline --extra embeddings python -m scripts.audit_campaign_bounds CAMPAIGN_DIR` rebuilt the original probe envelopes and replayed the recorded RAG toy from a temporary copy of the cache with the pinned local embedder. All **36 logical cache hashes** matched. The physical request logs contained **48 attempts with input usage** and **5 attempts without usage**. For the 48 measured attempts, including schema retries, reported input usage was within the reconstructed byte bound in every case. The largest measured input was 1,299 tokens.

### Operator handoff

`python -m soc.orchestrator.live spend status --output CAMPAIGN_DIR` read the existing campaign. USD 40.00 cap, USD 0.1193114 settled, USD 34.9568 reserved, and USD 4.9238886 available. All five open rows belong to run `m1-probe-25531a709b883f28`.

| Reservation | Model | Call | Recorded outcome | Reserved USD |
|---|---|---|---|---:|
| 37 | Gemini 4 Argon | `gemini-4-argon-low-valid` | `rate_limited` | 26.40000 |
| 38 | Gemini 3.8 Flash | `gemini-3.8-flash-low-valid` | `server_error` | 2.63304 |
| 39 | Gemini 3.8 Flash | `gemini-3.8-flash-low-valid` | `server_error` | 2.63304 |
| 40 | Gemini 3.8 Flash | `gemini-3.8-flash-low-valid` | `server_error` | 2.63304 |
| 43 | Gemini 3.5 Flash-Lite | `gemini-3.5-flash-lite-low-impossible` | `server_error` | 0.65768 |

Can can use `spend settle --reservation ID --usd AMOUNT --reason TEXT --approved-by NAME --output CAMPAIGN_DIR` after confirming each amount in Google billing. An approved cap change uses `spend set-cap --usd AMOUNT --reason TEXT --approved-by NAME --output CAMPAIGN_DIR`. Both record the prior and new amounts in the append-only operator audit. The real campaign remains at the values above. Post-settlement status and the roster re-probe are pending.

### Proposed SOC live conditions

- Keep SOC cross-examination at low effort.
- Keep SOC cross-examination at high effort.
- Form SOC open discussion at the shared Form effort.
- Form SOC principal-only at the shared Form effort.

The Form cross-examination run can share the Keep cross-examination run at the selected Form effort when the rest of the run configuration matches. The list, common Form effort and campaign budget await Can's confirmation. The latest recorded probe remains 14 of 18 combinations. Gemini 4 Argon and Gemini 3.8 Flash await re-probe after reconciliation. Cost estimates and live runs follow those prerequisites.

### Verification

New tests exercise three outstanding reservations, rejection above either model limit, breach detection below the combined dollar envelope, exact wire sizes on schema retry, read-only legacy status, and append-only operator actions on temporary databases.

```text
Success: no issues found in 54 source files
Contracts: 1 kept, 0 broken.
280 passed, 3 deselected in 46.41s
timeline: 35/36 branches (97.2%)
ledger: 352/382 branches (92.1%)
episode: 52/54 branches (96.3%)
segmenter: 67/68 branches (98.5%)
providers: 155/166 branches (93.4%)
checker: 66/68 branches (97.1%)
baseline: 34/34 branches (100.0%)
```

## Live preparation on 23 November 2026

**24 November update:** the held-unknown change supersedes the earlier toy completion claim below. Can explicitly chose the toy as an exempt fixture for configuration preflight. Runtime `must` unknowns still require resolution. With the current `meaning_model: null`, each prepared condition now ends `unverified`; this is not a completed live exit check. The offline estimator was rerun: 70 drafting/triad calls, USD 0.1411541 assumed usage cost, USD 14.2153766 scripted retry envelope, peak reservation USD 0.075544. These estimates exclude meaning-model calls because no model is selected. The report retains unresolved checks across historical content contexts; the final artifact count is separately available as `unverified_final` in each run's `unknowns.json`. Select the shared resolver, reestimate its calls and budget, reconcile the existing reservations and pass the probes before live launch. No real campaign action was performed for this update.

The four proposed YAML files under `config/m2-live/` use the toy episode, GPT-6.1 Sol with GPT-6 Luna as successor, Claude Sonnet 5.5 as peer, and Gemini 3.8 Flash as reviewer. One chunk runs at a time. The proposed conditions are cross-examination at low and high effort, open discussion at low effort, and principal-only at low effort. The two Form modes use provisional low effort pending Can's choice. Form cross-examination can reuse the Keep run at that shared effort.

### Billing inspection and readiness refresh

Can's subsequent instruction to continue is treated as approval of the four prepared M2 conditions, provisional low effort for open discussion/principal-only, the external toy principal wording, and the proposed USD 15 incremental budget within the existing cap. This does not settle historical reservations or authorize a cap change. The USD 15 proposal is an operator budget; it has not changed the campaign's USD 40 stored cap. Launch remains subject to the explicit operator reconciliation and passing provider probes. Form/Align's final effort selection still follows Pilot A.

On 23 November, a read-only browser inspection of Google AI Studio showed Gemini Project (`gen-lang-client-0637236478`) on the free tier. Its Spend page stated that no billing was currently configured. The usage chart showed HTTP 429 and 503 errors, consistent with the recorded failure types, but did not establish charges for the five individual attempts. Google Cloud also showed billing disabled for both Gemini-related projects in the account. These current settings do not establish historical per-attempt charges or independently identify the project behind the campaign credential.

A fresh `spend status` still reports USD 0.1193114 settled and USD 34.9568 reserved under the USD 40 cap, leaving USD 4.9238886 headroom. No reservation was settled or released, no cap was changed, and no new provider call was made. The operator reconciliation required by the live brief remains pending.

The offline preparation was rerun for all four conditions; each completed and reproduced the estimates below. The prepared defaults are approved as recorded above; live launch still requires operator reconciliation and passing provider probes. Pilot A is also unfinished: its retrieval tuning, 300 sentence pairs and approximately 200 inflected term occurrences must not be represented as completed by these toy runs.

The readiness refresh passed `./scripts/check`: `All checks passed!`, `105 files already formatted`, `Success: no issues found in 54 source files`, `Contracts: 1 kept, 0 broken.`, and `290 passed, 3 deselected in 47.24s`. All module branch coverage floors passed; ledger coverage remained 352/382 branches (92.1%). No source changes or new tests were needed for this refresh.

### Pilot A collection preparation

`docs/pilot-a/README.md` now gives the collection and review workflow, with empty CSV templates for checker labels, inflected occurrences, model/effort usage and retrieval trials. It specifies contextual checker artifacts, independent labels, unknown outcomes, character offsets, retry denominators, pinned retrieval snapshots and the plan's instrument targets. No pilot observations have been collected by this preparation, no settings selected, and no provider calls made. The templates keep collection work ready while the campaign awaits operator reconciliation.

All four CSV files parsed as header-only tables with unique columns. This documentation change adds no tests. `./scripts/check` passed again:

```text
All checks passed!
106 files already formatted
Success: no issues found in 54 source files
Contracts: 1 kept, 0 broken.
290 passed, 3 deselected in 48.82s
timeline: 35/36 branches (97.2%)
ledger: 352/382 branches (92.1%)
episode: 52/54 branches (96.3%)
segmenter: 67/68 branches (98.5%)
providers: 155/166 branches (93.4%)
checker: 66/68 branches (97.1%)
baseline: 34/34 branches (100.0%)
```

A draft question-to-answer key is at `~/Library/Application Support/SOC/principal/toy.yaml`, outside the repository, with permissions 0600. It contains answers for the known toy terms and normative questions. The continuation approval recorded above covers this toy wording. The preparation tests insert a separate unasked preference and verify that its text, question and private key path never reach any worker prompt.

### Scripted cost estimate

Run `uv run --locked --offline python -m scripts.prepare_m2_live --principal-key "$HOME/Library/Application Support/SOC/principal/toy.yaml" --out /tmp/m2-live-estimate.json` to reproduce the estimate with temporary campaign databases.

| Proposed condition | Calls | Assumed input tokens | Assumed output tokens | Expected USD | Full request reservations USD | Retry envelope USD | Peak concurrent reservation USD |
|---|---:|---:|---:|---:|---:|---:|---:|
| Cross-examination high | 19 | 18,539 | 1,358 | 0.0367391 | 0.7649737 | 3.8356909 | 0.075544 |
| Cross-examination low | 19 | 18,535 | 1,358 | 0.0367348 | 0.7649381 | 3.8355129 | 0.075540 |
| Open discussion low | 19 | 18,185 | 1,338 | 0.0368033 | 0.7963571 | 3.9933679 | 0.075540 |
| Principal-only low | 13 | 16,031 | 1,284 | 0.0308769 | 0.5087565 | 2.5508049 | 0.075540 |
| Total | 70 | 71,290 | 5,338 | 0.1411541 | 2.8350254 | 14.2153766 | 0.075544 |

Usage is a planning assumption of one token per four UTF-8 bytes, rounded up separately per request and response. Reservations use the full request byte length plus the 4,096-token output allowance. The retry envelope includes two transient retries, one schema repair and one refusal retry on every scripted call, with an extra user-message allowance after a schema failure. These amounts cover this scripted decision path. Live classifications, answers and repairs can produce additional calls and different reasoning usage.

### Current campaign and required decisions

The read-only status check still reports a USD 40 cap, USD 0.1193114 settled and USD 34.9568 held in reservations 37, 38, 39, 40 and 43. Available headroom is USD 4.9238886. Operator reconciliation against Google billing is pending. The recorded probe report remains 14 of 18 effort combinations. The re-probe follows the reconciliation step in the live brief. No new provider calls were made for this preparation.

The continuation approval above covers the prepared four-condition list, the provisional low Form effort, toy principal wording and proposed budget. The live handoff still reserves operator settlement and cap changes for Can; neither was performed.

### Prepared launch and replay commands

After confirmation and passing effort probes, run from the repository with a fresh `m2-` revision directory. The explicit code revision overrides the preparation baseline in each condition YAML.

```sh
soc_campaign="$HOME/Library/Application Support/SOC/campaigns/m1-2026-11-18"
soc_principal="$HOME/Library/Application Support/SOC/principal/toy.yaml"
soc_revision=$(git rev-parse HEAD)

soc_run_condition() {
  soc_case="$1"
  soc_record="$soc_campaign/m2-$soc_revision/$soc_case"
  soc_replay="$soc_campaign/m2-$soc_revision/$soc_case-replay"
  uv run --locked python -m soc.orchestrator.live soc \
    --output "$soc_campaign" --episode data/episodes/toy \
    --condition "config/m2-live/$soc_case.yaml" --principal-key "$soc_principal" \
    --code-commit "$soc_revision" --run-dir "$soc_record" &&
  uv run --locked --offline python -m soc.orchestrator.live soc-replay \
    --output "$soc_campaign" --episode data/episodes/toy \
    --condition "config/m2-live/$soc_case.yaml" --principal-key "$soc_principal" \
    --code-commit "$soc_revision" --run-dir "$soc_replay" &&
  cmp "$soc_record/final_chunks.jsonl" "$soc_replay/final_chunks.jsonl" &&
  cmp "$soc_record/final_findings.jsonl" "$soc_replay/final_findings.jsonl" &&
  uv run --locked --offline python -m analysis.timeline "$soc_record" data/episodes/toy \
    --out "docs/examples/m2-$soc_case.html" --label "Live toy $soc_case"
}

soc_run_condition cross-examination-low &&
soc_run_condition cross-examination-high &&
soc_run_condition open-discussion-low &&
soc_run_condition principal-only-low
```

### Schema and preparation checks

Added a valid graph covering authority, decision subject and a brief citation, plus invalid fixtures for `DecisionSubjectShape`, `BriefCitationShape`, and the authority-class constraint in `CommitmentShape`. Each invalid fixture fails only its named shape.

`test_proposed_live_condition_completes_script_and_keeps_private_key_out_of_prompts` covers all four conditions. `test_live_condition_matrix_has_three_modes_and_both_keep_efforts` verifies the proposed matrix. `test_live_condition_uses_explicit_code_revision_for_record_and_replay` verifies the launch override.

`./scripts/check` exited successfully.

```text
All checks passed!
105 files already formatted
Success: no issues found in 54 source files
Contracts: 1 kept, 0 broken.
290 passed, 3 deselected in 43.93s
timeline: 35/36 branches (97.2%)
ledger: 352/382 branches (92.1%)
episode: 52/54 branches (96.3%)
segmenter: 67/68 branches (98.5%)
providers: 155/166 branches (93.4%)
checker: 66/68 branches (97.1%)
baseline: 34/34 branches (100.0%)
```
