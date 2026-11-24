# M3 — cooperation layer and evaluation

Started on 24 November 2026 at Can's explicit request. M2 remains unaccepted: its live episode checks, provider re-probes and operator reconciliation are outstanding. Starting this work does not close M2 or authorize settlement, release or a cap change. No M3-specific brief is present; `PROJECT_PLAN.md` section 8 supplies the scope.

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

1. Trusted runtime check extraction, durable standing/sanction records and runner integration. Weighted voting must preserve authenticated ballots and authority checks; its interaction with QUORUM/ALL needs an explicit policy.
2. Enforce sanctions/replacement, persist successor transfer and test interrupted/replayed runs.
3. Announced/hidden prompts, tokenizer-measured length matching and golden leakage tests.
4. Private planted-error injection/reversion and proof that no planted span remains before scoring.
5. All-seed evaluation of accepted, rejected and unfinished outputs; separate fidelity, preference and continuity scores.
6. Blinded flagship judge panel and input scrub tests.
7. All registered metrics and hand-counted log fixtures.
8. Episode-level aggregation, SciPy Wilcoxon, bootstrap, Holm cross-checks, 2,000-study calibration, power and label-swap/unit-count checks. Keep the one-sided non-inferiority interpretation distinct from the label-swap invariance test.

No full M3 exit criterion is claimed complete by this first component. No live calls were made and the real spend database is unchanged.
