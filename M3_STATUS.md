# M3 — cooperation layer and evaluation

Started on 24 November 2026 at Can's explicit request. M2 remains unaccepted: its live episode checks, provider re-probes and operator reconciliation are outstanding. Starting this work does not close M2 or authorize settlement, release or a cap change. The initial standing implementation followed `PROJECT_PLAN.md` section 8. During the runtime continuation, `CODEX_BRIEF_HELD_UNKNOWN.md` and a corresponding Pilot B plan update appeared in the working tree and were read; their held-finding requirements are implemented and verified in the later held-check section below.

## First implementation: runtime standing policy

`soc/cooperation/standing.py` supplies a deterministic in-memory projection of attributed runtime checks. The typed input carries a check ID, monotonic sequence, worker identity, check kind, pass/fail/unknown outcome, runtime evidence reference and observer identity. Supported kinds cover declaration/capture, gate checks, evidence-supported rulings, flags leading to rulings and observed review behavior. The component reads no files, imports no evaluation code and has no fields for ground truth, private answers or planted spans. Its caller must authenticate the observer, derive outcomes from actual runtime checks and attribute each to the checked worker. Schema validation alone does not authenticate a check.

Standing is the fraction of passes among an agent's most recent resolved checks. Unknown outcomes neither improve standing nor evict failures. Insufficient evidence uses a configurable neutral score. Duplicate check IDs, non-increasing sequences and repeated evidence for the same agent/check kind are rejected before mutation. Inherited evidence is protected against recounting for the successor.

Sanctions are cumulative permissions: lighter vote; lighter vote and no reviewing; no reviewing and no vote; replacement requested. Each snapshot recomputes them, so sanctions lift as compliant checks replace failures. Snapshot permissions and weights are results only: they do not yet alter triad voting, select reviewers, replace workers or write ledger events.

The fixed successor schedule follows the plan: episodes 1–6 inherit in the hidden arm and start fresh without a vote in the announced arm; episodes 7–12 swap. Episode positions are one-based and restricted to 1–12. Fresh successors need the configured minimum number of resolved checks before probation lifts. Inherited history retains its source attribution and is isolated from later predecessor updates. Reproduction requires replaying rebinds at their original positions among checks. Durable recording and runner integration remain unfinished.

## Provisional choices behind configuration

`config/m3-cooperation.yaml` supplies reviewable defaults, not registered experiment settings:

- Each resolved check has equal importance; window 20 checks, minimum evidence 5, neutral score 1.0.
- Strict thresholds: below 0.8 lighter vote, below 0.6 no reviewing, below 0.4 no vote, below 0.2 replacement.
- Normal vote weight equals standing. Lighter-vote and no-review stages multiply it by 0.5; no-vote and replacement weights are zero.
- Fresh successor probation uses the same five-check minimum. Unknown checks never lift probation.

These numeric choices are unspecified in the plan and logged in `OPEN_QUESTIONS.md`. None changes the existing M2 runner's behavior.

## Tests and verification

Tests were written first and initially failed collection because the module did not exist. The focused suite passed **42 cases**, with **36/36 branches** covered in `standing.py`:

- `test_standing_recomputes_recent_runtime_checks_and_ignores_unknowns`
- `test_sanction_thresholds_permissions_and_vote_weights`
- `test_each_sanction_lifts_as_recent_checks_show_compliance`
- `test_no_checks_and_small_samples_use_configurable_neutral_standing`
- `test_standing_replay_is_identical_and_rejects_duplicate_or_reordered_evidence`
- `test_successor_policy_counterbalances_all_twelve_episodes`
- `test_successor_inherits_history_or_starts_without_vote_then_recovers`
- `test_rebind_rejects_overwriting_an_existing_agent_or_self_replacement`
- `test_unknown_checks_cannot_lift_fresh_successor_probation`
- `test_rebind_replay_keeps_history_and_rejects_recounted_inherited_evidence`
- `test_standing_rejects_invalid_agent_identity`
- `test_custom_policy_changes_window_sanctions_and_warmup`
- `test_counterbalancing_rejects_unregistered_episode_or_arm`
- `test_cooperation_policy_rejects_invalid_or_unordered_thresholds`
- `test_runtime_check_schema_rejects_ground_truth_and_planted_error_records`
- `test_provisional_policy_is_loaded_from_configuration`

The coverage report and branch-floor script now include `soc/cooperation/` under the same 90% floor as other core packages. `ruff format` was run. `./scripts/check` exited successfully:

```text
All checks passed!
109 files already formatted
Success: no issues found in 55 source files
Contracts: 1 kept, 0 broken.
332 passed, 3 deselected in 45.07s
cooperation: 36/36 branches (100.0%)
timeline: 35/36 branches (97.2%)
ledger: 352/382 branches (92.1%)
episode: 52/54 branches (96.3%)
segmenter: 67/68 branches (98.5%)
providers: 155/166 branches (93.4%)
checker: 66/68 branches (97.1%)
baseline: 34/34 branches (100.0%)
```

## Remaining M3 work

1. Review the provisional weighted approval policy and numeric standing policy. Broaden instrument validation on Pilot A observations.
2. Automatic replacement needs an authorized candidate policy. Current opt-in runs stop with `cooperation_blocked` when replacement is required or the reviewer loses review permission.
   `CODEX_BRIEF_HELD_UNKNOWN.md` is implemented in the section below. Live meaning-model selection and instrument calibration remain open.
3. Announced/hidden prompt implementation and offline golden tests are delivered below. Validated roster tokenizer assets and provider-specific token parity remain pending.
4. Private planted-error injection/reversion and guarded scoring are delivered below. Pilot B still needs reviewed natural-error observations and a selected share; no live intervention was run.
5. All-seed evaluation of accepted, rejected and unfinished outputs; separate fidelity, preference and continuity scores.
6. Blinded flagship judge panel and input scrub tests.
7. All registered metrics and hand-counted log fixtures.
8. Episode-level aggregation, SciPy Wilcoxon, bootstrap, Holm cross-checks, 2,000-study calibration, power and label-swap/unit-count checks. Keep the one-sided non-inferiority interpretation distinct from the label-swap invariance test.

No full M3 exit criterion is claimed complete by this first component. No live calls were made and the real spend database is unchanged.

## Runtime integration and weighted approval — 24 November

`soc/cooperation/runtime.py` now derives checks from the verified public ledger, with standing keyed by the pair `(durable agent ID, model ID)`. This matters because executor rebinding preserves the durable agent ID. The journal binds its run ID, policy and initial participants; the run journal also fingerprints the complete opt-in configuration.

The append-only `cooperation.sqlite` sidecar stores each source event's sequence and hash, with derived `StandingUpdated`, sanction-transition and successor-transfer records. Every sync recomputes the projection, verifies the stored prefix, then appends missing source rows and records atomically. A restart catches up if the sidecar lags. Changed configuration, a different source prefix, edited projection records or an invalid ledger hash chain fail closed. These records are in the sidecar, not new events in the main ledger or changes to its ontology.

Runtime extraction currently handles:

- Verified declarations as passes, false declarations or observed silent uses as failures. Repeated capture of the same agent/model/decision/site within a build attempt counts once; the original draft observation takes precedence over the merged artifact's repeated observation.
- The first gate check for each authored build. Later rechecks do not create extra observations. Attribution follows `ChunkBuilt.actor`, including when a successor runs a final sweep over its predecessor's work. Administrative, artifact, provisional and unknown findings do not establish defection. A rule change since the build's observed snapshot also yields unknown rather than punishing compliance with the earlier authorized rules.
- Flags leading to triad resolutions as passes. Principal escalation is unknown, since escalation does not establish that the flag was wrong.
- Rulings won with evidence as passes for approving workers whose cross-examination answers survived the existing citation validator. The public resolution trace now records these supporters and rational vote weights. Losing a vote is not treated as incorrectness.
- Moderation results against subsequent runtime gate checks. A content violation at a moderated sentence is a failed review check; a passing gate is a pass. Failures elsewhere and administrative uncertainty are unknown. Private planted-error records and evaluation correctness never supply these labels. Private exposure/reversion instrumentation is delivered below; per-plant detection metrics still belong to the pending metrics/evaluation pass.

### Opt-in behavior

`LedgerRunSpec.cooperation` defaults to `None`, so existing M2 configurations retain equal voting and no cooperation enforcement. When enabled, it requires one chunk in flight, an explicit one-based episode position and a visibility value. At this stage visibility controls successor counterbalancing only: the announced prompt section and token matching remain unfinished. These settings do not yet define complete Align conditions.

Example structure inside a condition's `run` mapping:

```yaml
max_in_flight: 1
cooperation:
  episode_position: 1
  visibility: hidden
  policy:
    window: 20
    minimum_checks: 5
```

The model-backed SOC runner syncs the observer before a chunk, freezes that chunk's weights, and logs gate results before the next chunk. Suspended voters make no voting model call; their authenticated ballot slot records an abstention. Ineligible reviewers and replacement sanctions stop the run before drafting. The run publishes partial output with status `blocked` and outcome `cooperation_blocked`; it does not swap models. The RAG launcher and direct RAG entry point reject cooperation settings rather than silently ignoring them.

The provisional weighted rule keeps the existing distinct-approver floor **and** requires supporters to hold strictly more than half the triad's total weight. Abstentions retain their weight in the denominator, suspended members have zero weight, and a zero-weight member cannot cast a non-abstaining ballot. All-zero or tied support escalates to the principal. ONE retains its existing approval floor and ties; QUORUM still needs two distinct supporters; ALL still needs all three. Weights can veto an ordinary quorum but cannot let one member replace it. `record_triad_ruling` recomputes the result against separately supplied trusted runtime weights; the ledger still authenticates the actual recorded ballots and approvers in its ruling transaction.

### Added verification

Tests were added before each implementation and initially failed on missing weighted parameters, a missing runtime module and absent integration. `test_model_triad_records_reusable_term_and_resumes_through_shared_client` now also runs with hidden and announced successor settings: interruption, resume, cache-only replay and completed-run restart preserve outputs and derived records without additional model calls. Repeated final sweeps append source audit rows but do not alter standing.

New weighted tests cover quorum veto, preservation of distinct approvals and unanimity, zero weights, abstention, ties, bounded rational weights, forged decision metadata and suppressed voter calls. Runtime scenarios cover immutable history, configuration drift, duplicate capture, build-author attribution, authorized revisions, unknown checks, evidence credit, model-specific inheritance, replay, edited projections, source forks, invalid hash chains, participant identity, sequential scheduling, moderated-site attribution and stopping before calls on sanctions.

New test names in `test_weighted_agreement.py`:

- `test_weighted_majority_can_veto_but_cannot_replace_distinct_approvals`
- `test_unweighted_resolution_trace_keeps_legacy_evidence_defaults`
- `test_zero_weights_abstentions_ties_and_all_never_manufacture_unanimity`
- `test_weights_require_exact_authenticated_participants_and_bounded_rationals`
- `test_weighted_ruling_recomputes_the_result_against_trusted_weights`
- `test_suspended_voters_are_recorded_as_abstentions_without_a_model_call`

New test names in `test_cooperation_runtime.py`:

- `test_runtime_journal_is_append_only_idempotent_and_rejects_configuration_drift`
- `test_runtime_checks_use_authored_build_and_do_not_punish_revision_rechecks`
- `test_capture_checks_deduplicate_same_attempt_and_keep_unknowns_separate`
- `test_runtime_successor_uses_model_identity_and_replays_transfer`
- `test_runtime_journal_rejects_ledger_prefix_change_and_tampered_projection`
- `test_administrative_and_unknown_gate_findings_are_not_defection`
- `test_first_gate_after_authorized_rule_change_is_unknown`
- `test_runtime_credits_only_validated_evidence_supporters_and_principal_flags_remain_unknown`
- `test_cooperation_rejects_unknown_draft_author_and_inconsistent_transfer`
- `test_cooperation_detects_same_length_fork_and_broken_ledger_hashes`
- `test_cooperation_requires_distinct_participants_and_serial_scheduling`
- `test_review_check_attributes_only_moderated_sites`
- `test_model_runner_stops_before_calls_when_review_or_replacement_is_sanctioned`

`ruff format` ran and `./scripts/check` passed:

```text
All checks passed!
113 files already formatted
Success: no issues found in 56 source files
Contracts: 1 kept, 0 broken.
364 passed, 3 deselected in 48.70s
cooperation: 87/92 branches (94.6%)
timeline: 35/36 branches (97.2%)
ledger: 354/384 branches (92.2%)
episode: 52/54 branches (96.3%)
segmenter: 67/68 branches (98.5%)
providers: 155/166 branches (93.4%)
checker: 66/68 branches (97.1%)
baseline: 34/34 branches (100.0%)
```

No live provider calls, real campaign settlements or cap changes were made. The independently added held-unknown brief and plan edit are preserved outside this implementation commit.

## Held unknown checks — 24 November

Implemented `CODEX_BRIEF_HELD_UNKNOWN.md`. Can answered the toy question explicitly: **keep the toy as an exempt fixture**. Its public `brief.yaml` now has `fixture: true`, and the manifest hash was updated. This bypasses configuration preflight only; no runtime unknown becomes a pass. No modal wording was invented or approved by this exemption.

### Preflight and configuration

Evaluation episodes default to configuration checking in `validate_runtime`, complete episode validation, `eval.precheck`, both live launch paths and the runners. `ExperimentSettings.require_checkable` defaults to true. Approved mappings can be supplied in public `Brief.normative` entries with `keyword`, `value`, `brief_clause_id` and optional strength, or through the experiment's existing normative configuration. Live preflight runs before probes, provider requests, campaign creation or principal-key access. Storage/read-only ledger validation remains structural because storage does not own experiment configuration; it does not certify checker readiness.

Errors name the affected decision and missing piece. Examples:

- `decision D4: missing or ambiguous approved modal wording for MUST at 9999-1-s03`
- `decision D1: missing checkable wording`
- `decision D1: no deterministic checker for rule kind 'unimplemented'`

Unknown rule kinds are no longer silently treated as terminology by the runtime checker. Fixture metadata is excluded from worker and meaning-model prompts; approved modal requirements remain public.

### Held findings and resolution records

`GateOutcome` now includes `held` and `unverified` in `soc/ledger/models.py`. A checker unknown on a `must` requirement becomes held. The ledger boundary also normalizes raw unknown findings unless explicitly weaker. `GateDecision.accepted` is false while held findings exist, but held findings do not mark a build stale and do not start repairs. Drafting continues to the final resolution phase. `should` and `may` unknowns keep their prior non-blocking treatment.

The shared `soc/enforce/meaning.py` path builds a content-addressed `HeldCheck` from the public source, translation, brief, requirements, chunk and build. Changed wording, rules, output or build creates a different ID. Applicable sites are normalized to the current source; unrelated decisions cannot alter the request. Forbidden requirements participate in the bound context. Active modal wording reaches both arms, while future modal mappings stay out of prompts until their brief clause becomes active. A resolver returns `compliant`, `violation` or `unknown`, with a rationale and exact source/output/brief citations. Conclusive replies without verifiable citations become unknown. Provider failures are distinguished from semantic uncertainty and stop the run with the provider outcome; they are not retried across subsequent held sites in a loop.

Results append to `meaning.sqlite` in both arms. SOC additionally appends a `HeldResolved` ledger event and projects it into `held_resolutions`; RAG projects the same result into `events.jsonl`. The event contains the check ID, chunk/build, resolver identity, verdict, rationale, evidence, token usage and estimated cost. SOC validates its resolver/chunk/build and held-finding reference before writing, and conflicting reuse of an ID is refused. The underlying records are append-only. Ledger writes still undergo the existing hash-chain and SHACL checks.

A compliant verdict clears that held finding. A violation becomes a block and enters the normal bounded repair path. Unknown verdicts remain held. Source-context binding prevents an earlier compliant verdict from approving a changed requirement or output. RAG applies late resolutions at the same finalization phase during resume, rather than letting a cached future verdict alter earlier drafting. Mean-model usage is preserved in RAG totals on record, replay and resume; both arms publish a separate `meaning_usage` component.

### Final output and scoring

Unresolved findings publish as `unverified` in `final_findings.jsonl`; run status is `unverified`, and `assembly.json` does not accept assembly. Partial provider-failed or budget-exhausted runs also retain their unresolved findings. Existing outputs remain available for evaluation. Completed resolution and repair runs replay byte-identically for chunks, findings, unknown reports and assembly metadata.

`eval.continuity.score_continuity` applies an unverified veto to independent use-site assessments: an unverified or missing use cannot count as kept, even if the candidate assessment says kept. A sentence-wide finding without a decision ID conservatively excludes all assessed uses on that sentence. It does not turn ordinary rejection into incorrectness: rejected and unfinished outputs remain assessable. This supplies the continuity aggregation boundary; the complete all-seed evaluator and judge panel remain unfinished.

`unknowns.json` records the condition, episode, run, must-check opportunities, unknowns, configuration/runtime causes, compliant/violating resolutions and unverified counts. `unverified` includes unresolved historical check contexts; `unverified_final` counts findings remaining on published output. A content-addressed audit prevents identical rechecks or resumed runs from inflating the denominator. Opportunities include passing identity/numeric/verbatim/modal/applicable-decision checks, with the precise rule/sentence/decision counting documented in `Checker.count_must_checks`. They do not equal the number of findings.

`analysis.unknowns` aggregates counts by condition and episode and then divides; it does not average percentages. Duplicate runs and inconsistent counts are refused. `pilot_b_unknowns_pass` reports only the tenth Pilot B criterion: zero configuration-caused unknowns and runtime unknowns at most the configurable 1% threshold. Empty data does not pass. This does not certify the other nine go/no-go criteria.

```sh
uv run --locked --offline python -m eval.continuity JUDGMENTS.jsonl RUN_DIR
uv run --locked --offline python -m analysis.unknowns RUN_A/unknowns.json RUN_B/unknowns.json --limit 0.01
```

The timeline now displays held gates separately from blocks and excludes held-only verdicts from the block count. Its scripted HTML and JSON fixtures were regenerated; nine timeline tests pass. The optional browser/nightly checks were not run.

### Resolver setting and current toy preparation

`experiment.meaning_model` is optional and defaults to `None`. Without a selected resolver, held output remains unverified with no additional provider calls. Set it to a configured roster ID to use the same recorded meaning adapter in SOC and RAG. Its route must pass probes before live launch. Injectable resolvers support offline scenarios. The model's judgments are runtime decisions, not ground truth; Pilot B still needs instrument validation.

The four existing toy preparation paths now correctly report `unverified` with the current configuration. Their latest scripted estimate is documented in `M2_STATUS.md`; it excludes meaning calls and cannot be used to claim completed conditions. Select the resolver and refresh the estimate/budget before launching. No live call, settlement, release or cap change was made.

### Tests and exit evidence

Tests were added first and failed on the missing preflight API and the previous unknown/pass behavior. New scenarios:

- `test_evaluation_preflight_names_missing_modal_decision_and_fixture_is_explicit`
- `test_unknown_must_is_held_without_changing_weaker_strengths`
- `test_a_real_violation_still_blocks_when_other_findings_are_held`
- `test_both_arms_hold_without_repair_resolve_and_publish_unverified` (both arms; absent resolver, unknown, invalid evidence, compliant, repair and persistent violation)
- `test_held_resolution_replays_byte_identically_and_resumes_without_calls` (both arms; compliant, repair and unknown)
- `test_evaluation_validator_precheck_and_launcher_refuse_before_any_provider_or_key_read`
- `test_public_modal_configuration_and_unsupported_rules_name_the_decision`
- `test_meaning_provider_failure_stops_instead_of_becoming_semantic_unknown`
- `test_resolutions_are_context_bound_and_append_only`
- `test_unknown_rule_is_not_treated_as_a_term_and_check_counts_include_passes`
- `test_continuity_counts_unverified_and_missing_uses_as_not_kept`
- `test_unknown_report_aggregation_uses_counts_and_rejects_duplicate_runs`
- `test_held_reporting_cli_and_no_data_do_not_claim_a_pass`
- `test_meaning_prompts_never_read_private_keys_or_fixture_metadata`
- `test_unconfigured_meaning_model_is_refused_without_substitution`
- `test_equivalent_applicable_requirements_share_context_but_forbidden_changes_do_not`
- `test_active_modal_wording_reaches_both_arms_without_future_or_fixture_metadata`

These cover all eight brief exit requirements. Existing toy tests were updated to expect unverified output where their missing modal configuration previously passed; their stale repair and scheduling assertions remain in place. The translation prompt golden remains unchanged for ordinary findings. Coverage floors were kept and a new floor covers continuity/unknown reporting.

`ruff format` ran. `./scripts/check` exited successfully:

```text
All checks passed!
119 files already formatted
Success: no issues found in 59 source files
Contracts: 1 kept, 0 broken.
401 passed, 3 deselected in 51.42s
held_reporting: 20/22 branches (90.9%)
cooperation: 87/92 branches (94.6%)
timeline: 35/36 branches (97.2%)
ledger: 363/396 branches (91.7%)
episode: 60/62 branches (96.8%)
segmenter: 67/68 branches (98.5%)
providers: 155/166 branches (93.4%)
checker: 116/120 branches (96.7%)
baseline: 34/34 branches (100.0%)
```

## Announced/hidden cooperation prompts — 24 November 2026

Implemented M3 section 8 item 3 behind the existing opt-in cooperation setting.
Announced system paragraphs state the configured recent-check rules, strict
sanction thresholds, voting/review permissions, recovery, probation and the
recipient's own standing and weight. Hidden paragraphs contain neutral document
facts and stay identical across standing changes. No other agent's score or any
private evaluation/injector input is read.

Both treatments are padded independently to a configurable fixed budget
(default 1,024 tokens). Fixed length prevents hidden prompt length from encoding
standing. The renderer checks both the treatment sections and assembled system
messages with a strict under-2% difference. Each model needs an explicit local
Hugging Face-compatible tokenizer JSON, SHA-256 and provenance identifier.
Asset truncation and padding are disabled during measurement.
Missing models, changed bytes, unreadable files and unmatchable budgets fail
before a model request. The executor successor also needs an asset. Install the
optional runtime dependency with `uv sync --locked --extra cooperation`; dev
installs include it for offline checks. Configuration shape is documented in
`config/m3-prompt-tokenizers.example.yaml`.

`triad-prompt-*.json` records section/system counts and tokenizer provenance
outside the model messages. Runtime prompt variants reach draft, review,
cross-examination, classification, discussion and voting calls; principal and
meaning-check calls retain their existing inputs. Non-cooperation runs retain
their previous model messages. Resume verifies the saved system text and audit.

`cooperation.sqlite` now freezes each chunk/repair attempt's participant models,
standing and source ledger prefix before requests. Both voting and displayed
standing use that snapshot. Resume rederives it from the original verified ledger
prefix; newly appended checks affect the next attempt. Append-only triggers,
participant matching and source/snapshot verification reject changed contexts.
Replay reproduces snapshots and prepared prompts; source hashes stay bound to
each run's own ledger (output references can differ between directories).

### Validation limits and remaining work

The committed BPE tokenizer is explicitly a synthetic local reference, trained
only on public prompt text. Golden tests prove local matching and leakage
properties, not equivalence to hosted provider tokenizers. Provider-specific
assets and token parity are still pending; no real provider calls were made.
The budget, policy and padding wording remain configurable provisional choices
in `OPEN_QUESTIONS.md`. The retained $34.9568 reservation and campaign cap are
unchanged. M2 live conditions and the rest of M3 remain incomplete. Next is
private planted-error injection/reversion with a clean-output scoring gate.

### New and extended tests

- `test_prompt_pair_is_matched_and_hidden_is_independent_of_standing`
- `test_prompt_assets_are_pinned_and_missing_models_fail_closed`
- `test_cooperation_golden_prompt_and_configured_rules`
- `test_unmatchable_tokenizer_is_refused`
- `test_assembled_system_length_guard_refuses_a_mismatched_pair`
- `test_cooperation_model_run_requires_assets_before_any_call`
- `test_token_counts_ignore_asset_truncation_and_padding`
- `test_attempt_freezes_standing_across_resume_and_next_attempt_uses_new_checks`
- Extended `test_model_triad_records_reusable_term_and_resumes_through_shared_client`
  for both visibility arms: request leakage, local section/system counts,
  byte-identical prepared prompts and frozen standings across replay/resume.

`ruff format` ran. Final `./scripts/check` exited 0:

```text
All checks passed!
122 files already formatted
Success: no issues found in 60 source files
Contracts: 1 kept, 0 broken.
410 passed, 3 deselected in 73.66s (0:01:13)
held_reporting: 20/22 branches (90.9%)
cooperation: 113/118 branches (95.8%)
timeline: 35/36 branches (97.2%)
ledger: 363/396 branches (91.7%)
episode: 60/62 branches (96.8%)
segmenter: 67/68 branches (98.5%)
providers: 155/166 branches (93.4%)
checker: 116/120 branches (96.7%)
baseline: 34/34 branches (100.0%)
```

The three deselected nightly checks were not run. No live provider validation
or campaign change is included in this result.

## Private planted errors and reversion — 24 November 2026

Implemented M3 section 8 item 4 as an explicit opt-in draft intervention. The
SOC triad, RAG triad and single-worker RAG runners accept `draft_intervention`;
its default is `None`. The controller runs after a parsed draft and before
review/gating. Raw model replies remain in their ordinary cache, so recovery
reapplies the same intervention exactly once rather than caching a fabricated
model response. The opaque plan fingerprint binds run configuration/resume.
Ordinary runs keep their previous model messages and configuration identity.

`eval.planted.PlantPlan` binds each plant to an authored natural-error
observation of the same kind in the same one-based chunk. The observation names
a reviewed runtime reference; this is calibration input, not an automatically
verified semantic label. An edit specifies its sentence, worker and zero-based
attempt, plus exact original/planted spans. Missing/repeated original spans,
existing planted text, duplicate targets and overlapping/cascading edits are
refused. The example `config/m3-plants.example.json` is illustrative only, not a
calibrated plan. No share, random allocation or synthesis policy was selected.
Use the same reviewed plan for each condition; mismatched spans stop the run
rather than changing the assignment silently.

`PlantSession` stores the plan and before/after exposures in an append-only
private SQLite journal, bound to its run. Its directory must be outside all
supplied episode, run and prompt roots; it uses directory mode 0700 and database
mode 0600, and refuses database symlinks/non-files. Constructor callers must
supply those public roots. Runtime imports only the narrow `DraftIntervention`
protocol in `soc/interventions.py`; it neither imports evaluation code nor
receives private paths/record fields through that protocol. Only ordinary
translation text enters subsequent prompts. Existing runtime checks observe
behavior normally; private catch/miss ground truth never updates standing.

### Reversion and scoring boundary

`session.score(outputs, scorer)` restores recorded edits and asserts no active
planted span remains before calling the scorer. The scorer receives only chunk
translations. Restoration replaces the planted span with the original worker
span; it does not correct unrelated natural mistakes or use expected answers.
Already repaired spans and missing/unfinished chunks are preserved. Other edits
outside the span survive. Unrecognized paraphrases, duplicate/propagated planted
text, changed bindings and altered exposure records stop scoring for review.
This conservative exact-span instrument does not claim to reverse arbitrary
semantic paraphrases.

Raw workflow artifacts, outcomes, checks and costs are retained. A separate clean
export can be produced without overwriting raw output:

```sh
uv run --locked --offline python -m eval.planted PRIVATE_DIR RUN/final_chunks.jsonl RUN/restored.jsonl --run-id RUN_ID
uv run --locked --offline python -m eval.continuity JUDGMENTS.jsonl RUN --private-plants PRIVATE_DIR --restored-output RUN/restored.jsonl
```

Author independent judgments against the restored export. For a registered
intervention run, the continuity CLI refuses scoring unless both private records
and the restored export are supplied. It checks the plan fingerprint and compares
the export against freshly verified reversion. This extends the current
continuity boundary; the all-seed fidelity/preference evaluator and blinded panel
remain the next M3 work, and must use this same clean-output boundary.

### New scenarios and limits

Tests were added first and failed on the missing controller module. New tests:

- `test_private_plant_matches_natural_type_and_reverts_before_scorer`
- `test_plants_require_observed_kind_from_same_chunk_and_unambiguous_spans`
- `test_resume_is_idempotent_and_rejects_changed_draft_or_plan`
- `test_scoring_refuses_ambiguous_rewrites_and_propagation_without_calling_scorer`
- `test_private_records_cannot_live_under_public_or_prompt_roots`
- `test_real_triads_can_miss_a_plant_and_replay_then_score_only_restored_text`
  (SOC/RAG, with and without a reviewer challenge; a plant can survive voting)
- `test_reversion_cli_preserves_raw_and_continuity_requires_verified_export`
- `test_private_journal_rejects_record_tampering_and_duplicate_inputs`
- `test_single_worker_rag_uses_same_intervention_and_checkpointed_output`
- `test_private_database_rejects_symlinks_and_non_files`

Canary IDs and observation references never appear in captured outgoing prompts.
Record/resume and strict replay reproduce the same contaminated workflow output,
then the same restored scoring text, without extra provider calls. Tests use fake
models only. Coverage includes a separate 90% branch floor for `eval/planted.py`.
No live intervention, reservation settlement, cap change or policy decision was
made. The retained $34.9568 reservation, Pilot B share selection and validated
provider tokenizer assets remain unchanged/pending; M2 live and M3 remain open.

`ruff format` ran. Final `./scripts/check` exited 0:

```text
All checks passed!
125 files already formatted
Success: no issues found in 62 source files
Contracts: 1 kept, 0 broken.
423 passed, 3 deselected in 75.22s (0:01:15)
planted_errors: 58/60 branches (96.7%)
held_reporting: 28/30 branches (93.3%)
cooperation: 113/118 branches (95.8%)
timeline: 35/36 branches (97.2%)
ledger: 363/396 branches (91.7%)
episode: 60/62 branches (96.8%)
segmenter: 67/68 branches (98.5%)
providers: 155/166 branches (93.4%)
checker: 116/120 branches (96.7%)
baseline: 34/34 branches (100.0%)
```

The three deselected nightly checks were not run. No provider token-parity,
live calibration or complete M3 evaluation claim is implied by these checks.
