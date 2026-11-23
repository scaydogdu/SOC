# Pilot A collection kit

Pilot A runs from 23–29 November 2026 on pilot material only. Its observations tune the instruments and are never scored as evaluation results. These empty templates organize collection; they contain no collected or adjudicated examples. The toy episode is a smoke fixture and does not replace the pilot corpus.

## Run order

1. Complete the operator reconciliation in `CODEX_BRIEF_M2_LIVE.md` before further provider calls. Preserve the existing model roster and campaign cap. Run the roster probes and stop on unresolved provider failures.
2. Run the approved conditions on pilot episodes, preserving the episode revision, code commit, configuration and run directory. Record measured tokens, latency and failures per model and effort in `model-runs.csv`. Keep failures in the denominator. Use one row per model/effort/run; label missing provider usage as missing rather than zero. A cache replay is not another live observation.
3. Collect 300 distinct source/translation sentence pairs with contextual artifacts for checker review. Record rule labels in `checker-labels.csv`. The same pair may have several rule rows; count distinct `pair_id` values toward 300. Include compliant and violating examples for each rule, with negation, numeric multiplicity, normative force, approved terms, forbidden terms and verbatim material represented. Keep naturally occurring and deliberately constructed examples identifiable. Do not count existing unit-test cases as newly collected pilot observations.
4. Collect approximately 200 inflected term occurrences in `lemma-labels.csv`. Include compounds, case and possession suffixes, consonant alternation, apostrophes and near misses. Label the expected match independently of the current stem matcher's output. For negatives, include the observed candidate span and set `expected_match` to `no`.
5. Compare retrieval depth and dense/BM25 mix on the same pilot queries in `retrieval-trials.csv`, with the same note snapshots and pinned BGE-M3 revision. Record the required current decision IDs before inspecting retrieved results. Include post-revision queries to detect superseded notes. Choose settings from measured results; the current defaults remain `top_k=5`, `dense_weight=0.5` until that decision.
6. Review results with Can, choose the shared Form/Align effort and lemmatizer, and record the retrieval settings and rationale. Do not infer those selections from the provisional low-effort M2 smoke configuration.

## Annotation and artifact conventions

Use UTF-8 CSV with a header, quote embedded commas/newlines, and stable IDs. Store provider caches and raw outputs in the external campaign directory; record artifact references here only after checking them for secrets and private principal answers. Never place API keys in a template or artifact reference.

For checker labels, `context_ref` identifies a saved input bundle containing source sentences, translation, brief, applicable decisions/forbidden renderings, normative mappings and checker configuration. This is needed to reproduce contextual rules; a sentence pair alone is insufficient. `source_text` and `translation_text` identify the reviewed pair. `rule` uses the implementation IDs: `ids`, `verbatim`, `bcp14`, `numbers`, `term`, `forbidden`. Normative polarity is reviewed under `bcp14`; this template does not claim a separate general negation detector exists.

Set `origin` to `natural` or `constructed`; `gold_label` to `violation`, `compliant` or `unknown`; and `review_status` to `draft`, `reviewed` or `adjudicated`. Leave the gold label blank until reviewed. Keep the annotator's judgment independent of checker output, then populate `checker_label`. Record the checker revision. Unresolved labels and `unknown` outcomes must be reported separately, with their counts; they cannot count as correct detections. Escalate disagreements to Can and preserve the rationale.

For lemma labels, offsets are zero-based Python character positions into `text`, with an exclusive `end`. `text[start:end]` must equal `surface`; they are not UTF-8 byte offsets. `phrase` is the approved base phrase. Record `matcher`, `matcher_revision` and `predicted_match` after annotation. Evaluate candidates on the same occurrences and report false positives and false negatives as well as accuracy.

For run summaries, times are UTC ISO 8601; `wall_seconds` is measured elapsed time for that model's calls in the run, with the measurement method in `notes`. `physical_attempts` includes retries; `failed_attempts` includes every failed physical attempt; `failed_logical_calls` counts calls still failed after retries. Input/output tokens and `settled_usd` are measured totals, not the scripted estimates. `usage_complete` is `yes` or `no`. Keep run-level completion and instrument data separate.

For retrieval trials, encode each list of decision IDs as a JSON array inside the CSV field. `required_decision_ids` is the reference set; `retrieved_decision_ids` is ranked and versioned. Record `superseded_decision_ids` if obsolete versions were returned. Each trial must reference its query and note snapshot so changes in the note corpus cannot masquerade as parameter improvements.

## Completion evidence

Report distinct pair/occurrence counts and label coverage, per-rule precision and recall with TP/FP/FN counts, lemma accuracy with correct/total counts, retrieval comparisons, and tokens/latency/failure rates by model and effort. Report undefined ratios as undefined, not as a pass. The plan's instrument targets are checker precision ≥0.95 and recall ≥0.90 per rule on 300 pairs, and lemma accuracy ≥0.95 on about 200 occurrences. Collection templates and synthetic smoke tests do not establish those targets.

Save the chosen settings and unresolved issues in `M2_STATUS.md` and `OPEN_QUESTIONS.md`. Keep Pilot A results out of the evaluation dataset. M2 also requires one completed live episode per SOC condition, cache-only replay comparisons and timeline exports; this kit does not close that gate.
