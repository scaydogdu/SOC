# M3 — cooperation layer and evaluation

Started on 24 November 2026 at Can's explicit request. M2 remains unaccepted: its live episode checks, provider re-probes and operator reconciliation are outstanding. Starting this work does not close M2 or authorize settlement, release or a cap change. The initial standing implementation followed `PROJECT_PLAN.md` section 8. During the runtime continuation, `CODEX_BRIEF_HELD_UNKNOWN.md` and a corresponding Pilot B plan update appeared in the working tree and were read; their held-finding requirements are the next scoped M3 task and are not implemented by this cooperation change.

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
   The next gate/checker task is `CODEX_BRIEF_HELD_UNKNOWN.md`: configuration preflight, held `must` unknowns, shared resolution with RAG, unverified output/scoring and the new Pilot B unknown-rate check. This work remains outstanding.
3. Announced/hidden prompts, tokenizer-measured length matching and golden leakage tests.
4. Private planted-error injection/reversion and proof that no planted span remains before scoring.
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
- Moderation results against subsequent runtime gate checks. A content violation at a moderated sentence is a failed review check; a passing gate is a pass. Failures elsewhere and administrative uncertainty are unknown. Private planted-error records and evaluation correctness never supply these labels. Planted-error catch/miss instrumentation still remains to be built.

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
