# SOC testbed — end-to-end plan for Codex

**Written:** 11 November 2026 by Claude (reviewer) · **Owner of every decision:** Can Aydoğdu · **Builder:** Codex
**Starts from:** milestone M0, accepted on 11 November 2026 at commit `a2ed844`.

This file is the route from M0 to the final delivery on 4 January 2027. It is self-contained: you do not need the proposal, the deck or the implementation-plan document. It replaces nothing already in the repository; `CODEX_BRIEF_M0.md`, `REVIEW_M0.md`, `REVIEW_M0_2.md` and `OPEN_QUESTIONS.md` stay as the record.

Read sections 1 to 4 once. Before each milestone, read that milestone's section in full. Claude will also write a short brief per milestone with the exact checklist, as for M0; where a brief and this plan disagree, the brief governs, and the difference is a question for Can.

---

## 1. What is being built, in one page

**The research question.** When several AI agents share one long task, do decisions stay intact when a model is replaced and when the client changes the brief? The project's name for the failure is *commitment drift*: work continues and looks fine, but breaks a decision that was already accepted.

**The task.** Translate IETF security RFCs from English into Turkish, starting with RFC 9700 (OAuth 2.0 Security Best Current Practice). Translation is used because a decision in one section binds others and many errors are checkable, down to SHOULD NOT.

**The agents.** Triads: two workers from different model families draft the same chunk independently; a reviewer from the third family diffs the drafts, turns divergences into flags and moderates a cross-examination. Families: Claude, GPT, Gemini. A simulated *principal* (the client) holds the brief and about five unwritten preferences per episode, answers brief gaps and deadlocks, and changes the brief once per episode.

**Two memory systems compared.**

| | RAG workflow (baseline) | SOC ledger |
|---|---|---|
| Keeps decisions as | Notes, glossary and style guide, pinned in every prompt, plus retrieved notes | Versioned commitments in the event ledger built in M0 |
| Finds work a revision affects by | Searching outputs for the old rendering with the shared checker | Usage records: which chunk used which version |
| Stops stale work with | A repair round after the checker | A gate that rejects output breaking the current version |
| Survives a model change by | Handing over glossary, style guide, notes and partial output | Rebinding the operation; commitments stay in place |

Both systems get the same triads, the same approved decisions and the same brief change at the same points, and use the same deterministic checker. The difference must come from how decisions are kept, not from what is known.

**Three experiments.**

| Experiment | Conditions | Runs | Primary measure |
|---|---|---|---|
| **Keep (primary)** | RAG workflow or SOC ledger × low or high effort | 144 | Commitment continuity per episode, counted per use site |
| Form | Principal settles every flag; triad by open discussion; triad by cross-examination | 108 | Resolution accuracy against principal-only, non-inferiority with a 5-point margin |
| Align | Votes weighted by standing, rules hidden or announced (equal votes reuse Form's cross-examination runs) | 72 | Defection rate, announced versus hidden |
| Reference | Each flagship model alone | 36 | Context only |
| **Total** | Each condition × 12 frozen episodes × 3 runs (reference × 1) | **360** | |

Every run has two events: the executing model is replaced after about 40% of chunks, and the principal changes the brief after about 60%.

**What must never happen.** A critique, a review comment, or an agent's suggestion changing scope. Only Can changes the design. In the testbed, only the principal changes the brief. Recency never decides between two rulings.

---

## 2. How to work

These rules held for M0 and hold for every milestone.

1. **Build only the current milestone.** Each section lists what is out of scope. Do not start the next milestone early unless the current one is accepted.
2. **Do not change the design.** Items marked *fixed* or *decided* are built as written. Items marked *your choice* are yours; report each choice in one line.
3. **Questions go to `OPEN_QUESTIONS.md`.** If something is ambiguous or looks wrong, write it there and build the least surprising reading. Do not settle a design question yourself, and do not record a decision as Can's unless Can made it.
4. **Done means the exit tests pass.** Do not report a test as passing unless you ran it and saw it pass. Paste real output into the status file.
5. **No live model call in the every-commit suite.** Live calls go only through the recording client, and only in the nightly and pilot layers.
6. **Never commit secrets.** API keys come from the environment or a secret store, never the repository, prompts, logs or run files.
7. **One code path per mechanism.** Conditions differ by configuration, never by a second implementation of the same step.
8. **Ground truth never reaches runtime.** `soc/` may not import `eval/` or `analysis/`, directly or indirectly. This contract is never relaxed.
9. **Hand back with a status file.** For each milestone write `M<n>_STATUS.md`: what was built, real output of every exit test, measured numbers, every choice, anything skipped or failing. Commit, then tell Can.
10. **Review gate.** Claude reviews each hand-back against its brief and writes `REVIEW_M<n>.md`. A milestone is accepted only when Can and the review say so. Fix review findings in section A of a review before starting the next milestone; section B items are questions for Can.

---

## 3. Everything already fixed or decided

Build on these. Do not reopen them.

### 3.1 Technical decisions

| Area | Decision |
|---|---|
| Language and tooling | Python 3.12, uv, pytest, Ruff, mypy strict on core packages, import-linter, Hypothesis |
| Ledger | One SQLite file per run, WAL, append-only hash-chained events, projections rebuilt from events, compare-and-set per write (built in M0) |
| Commitment schema | Hand-written ontology, JSON-LD with PROV-O terms, SHACL validating every write inside the transaction (built in M0). Keep SHACL in the write path unless Can says otherwise |
| Sentence identity | Stable IDs such as `9700-4.2-s03`; workers must return the same IDs |
| Model output | Pydantic schemas sent as structured outputs to all three providers, validated again locally; one repair retry, then the call counts as failed |
| Structured-output fields | Ask for a short rationale, never step-by-step reasoning inside structured output |
| Record and replay | A content-addressed response cache keyed by model, effort, messages, schema and replicate number |
| Turkish lemma matching | Chosen by accuracy on about 200 inflected glossary-term occurrences among Zemberek, zeyrek and Stanza; fallback is a stem matcher that handles consonant softening (belirteç / belirteci) |
| RAG retrieval | BGE-M3 embeddings run locally, pinned by revision, mixed with BM25; depth and mix tuned in Pilot A |
| Statistics | SciPy Wilcoxon, own bootstrap and Holm code tested against statsmodels, the `krippendorff` package |
| Effort | Labels are provider-specific; compare by tokens and cost, not by label |

### 3.2 Model roster

As listed in the providers' documentation on 27 October 2026. **No ID has been probed yet**; M1 probes every one. If an ID does not exist or behaves differently, record it in `OPEN_QUESTIONS.md` and stop using it; do not substitute another model on your own.

| Family | Flagship (judge, reference) | Mid (triad member) | Small (pilot; candidate successor) |
|---|---|---|---|
| Anthropic | `claude-opus-5-5` | `claude-sonnet-5-5` | `claude-haiku-5-5` |
| OpenAI | `gpt-6-astra` | `gpt-6.1-sol` | `gpt-6-luna` |
| Google | `gemini-4-argon` | `gemini-3.8-flash` | `gemini-3.5-flash-lite` |

### 3.3 Episode format (decided 10 November)

Built in M0 and documented in the README: approvals carry their content; planted errors are separate from evaluation seeds; affected sets hold both the `repair` side and the `apply` side; a decision has its own ID and lists its scope keys; each sentence has exact `text` for the round trip and a clean `reading_text`; table of contents and boilerplate are not translated.

### 3.4 Coverage rule for every evaluation episode (decided 10 November)

- At least five approved decisions used on both sides of the executor change.
- One brief change that revises at least two decisions used on both sides of it: one a term, and one non-lexical or scope-limited that the checker can verify by rule.
- Continuity is scored per use site listed in the episode's affected set; repair precision is reported for both systems.

### 3.5 Measures added on 10 November

Delivered or broken (for each lost commitment: never reached the prompt, or reached it and was broken), false blocks (rejections the evaluation finds compliant, including rejections of work following the authorized revision), capture accuracy, resume cost (tokens and repair rounds per chunk before and after the executor change), repair precision for both systems.

### 3.6 Ledger design for M2 (decided by Can, 11 November)

From `REVIEW_M0_2.md` section B. The Decision paragraphs there are the full text; in short:

- **B1 Authority.** Each commitment has an authority class scoped to the operation. The principal owns the brief and principal-issued rulings. An authorized triad may revise a triad-owned ruling when it cites new evidence and meets the approval requirement of the ruling's consistency level. The orchestrator authenticates and verifies approvals; the ledger enforces authority on each write and records the actual approvers.
- **B2 Conflicts.** Stable commitment IDs. Within an operation, one binding ruling per decision subject at each use site. Overlap is checked in the write transaction; a collision writes `ConflictRaised` with both IDs and the overlap, and the existing ruling stays binding until resolved. Exceptions use explicit, disjoint scopes.
- **B3 Rebuilds.** A repaired chunk appends another `ChunkBuilt` with the next build number, accepted by compare-and-set against the previous build number, recording output reference, ledger snapshot and versions used. Keep every build; the latest accepted build counts.
- **B4 Vocabulary.** `soc:antecedent` (when the ruling applies) and `soc:consequent` (what it requires) move onto the commitment version. A revision links the old version with `prov:used` and the new with `prov:generated`; the new version keeps `prov:wasRevisionOf`. Align context and shapes before the freeze.
- **B5 Episode required.** The orchestrator must pass the validated public episode when it opens a writable ledger. Brief citations resolve against the brief in force at the write. Episode-free construction stays for isolated ledger tests.

### 3.7 Strength (decided 11 November, built in M0)

`soc:strength` = `must` | `should` | `may`. Every commitment in the translation experiment is `must`. The gate built in M2 acts on it: `must` blocks, `should` warns with a recorded reason, `may` never blocks.

### 3.8 Explicitly out of scope for the whole project term

The operational profile (count limits on actions, attempt records, risk classes, visibility at handover) is documented as proposed and unbuilt. Economic incentives between independent owners, adversarial peers, effort allocation, and a ledger replicated across families are also out of scope.

---

## 4. Decisions still open, and when they are needed

Do not decide these. If one blocks you, say so in the status file and build the default shown, behind a configuration switch.

| Question | Needed by | Default until Can decides |
|---|---|---|
| Licence for the public repository | Before the repository is made public | None; keep private |
| `soc:` namespace `https://w3id.org/soc-testbed#` | Freeze (13 Dec) | Keep as provisional constant |
| Chunk positions one-based, events after chunk N | M1 start | Current convention |
| Should `Revised` and `Superseded` differ beyond name and allowed states | M2 start | Current behaviour |
| Equal maximum repair rounds per chunk for both systems, and report first-pass violations too | M1 baseline repair loop | A configurable maximum, same value for both arms, default 2 |
| Unanimity (`ALL`) covers only normative force, polarity and numbers; wording follows the brief or two-of-three | M2 voting rules | Build the rule as a per-decision-type table in configuration; ship the current rule (all normative meaning needs `ALL`) |
| Align: caveat that effects may be prompt compliance, plus an analysis of behaviour against displayed standing | M3 analysis | Build the descriptive analysis; it changes no registered test |
| Successor at the executor change | M2 executor change | Same family's small model, rotating which family is replaced |
| Number of Pilot B episodes | M3 end | Three |
| Where main runs execute | M4 | A small cloud VM |
| Public OSF registration in addition to the repository tag | M5 | Repository tag only |
| Keep Inspect AI's model layer | End of the M1 trial | Your own thin clients |

---

## 5. Schedule

M0 finished on 11 November, four days early. Start M1 immediately; the early finish is the project's only slack. Keep the window end dates; finishing early is fine, finishing late triggers the slip rules in section 13.

| Milestone | Window | Gate |
|---|---|---|
| M0 Foundations | 10–15 Nov | **Accepted 11 Nov** |
| M1 Providers, checker, RAG baseline | 12–22 Nov | Review + Can |
| M2 SOC lifecycle and triads, with Pilot A | 23–29 Nov | Review + Can |
| M3 Cooperation layer and evaluation | 30 Nov–6 Dec | Review + Can |
| M4 Pilot B and instrument validation | 7–12 Dec | Nine go/no-go checks |
| M5 Freeze and registration | 13 Dec | Runner locks |
| M6 Main runs, analysis, release | 14 Dec–4 Jan | All 360 runs accounted for |
| Episode authoring (parallel track) | 12 Nov–6 Dec | Coverage rule passes for all 12 |

---

## 6. M1 — Providers, checker, RAG baseline (12–22 Nov)

### Build

1. **Inspect AI trial, one day, first.** Try UK AISI's Inspect AI model layer in place of hand-written clients. Keep it only if its cache replays a recorded run byte-for-byte and it supports structured outputs and the effort settings for all three providers. Report the result either way; Can decides whether to keep it.
2. **Provider clients** behind one interface (`soc/providers/`): structured outputs per provider, effort mapping, retries with backoff, per-provider rate limits and concurrency caps, a per-run token cap (budget guard), refusals and max-token cut-offs logged as their own outcomes.
3. **Record-and-replay cache**, content-addressed. Replay mode never calls the network.
4. **Scripted fake models** driven by YAML scripts, so the protocol can be tested offline.
5. **Checker v1** (`soc/enforce/`), deterministic: BCP 14 key words preserved (in capitals only), numbers and negations preserved, code and artwork untouched, glossary terms rendered as approved, tombstoned renderings detected through the ledger's lookup. Lemma matching starts with the stem matcher; Pilot A picks the lemmatizer.
6. **RAG workflow** (`soc/baseline/`): notes written on every approved decision; glossary and style guide pinned in every prompt; retrieval of the most similar notes (BGE-M3 + BM25); after a revision the note is replaced and re-indexed and outputs where the checker finds the old rendering are repaired. It runs behind the same memory interface the ledger will use, and uses the same checker.
7. **Repair budget** as configuration, identical for both arms (section 4).
8. **Delivered-decision logging**: whatever builds a prompt records which decisions it delivered, in both arms, so "delivered or broken" can be scored.
9. **Live contract probe**: one schema-constrained call per roster model per effort setting it will use. Nightly only.

### Exit tests

- Every roster model answers a schema-constrained probe at each effort setting it will use, or the failure is in `OPEN_QUESTIONS.md`.
- A recorded run replays with identical outputs; re-scoring from logs gives identical results.
- The checker passes a 50-case fixture covering each rule, including inflected Turkish forms.
- The RAG baseline completes the toy episode end to end with fake models, and once with live models.
- Rate-limit and server errors are retried with backoff; an interrupted run resumes from its last event.
- No prompt or log contains an API key (a test scans for the key pattern).
- Every-commit suite still offline and under five minutes; coverage floor held for new core packages.

### Out of scope

Triads, cross-examination, capture, the SOC gate, impact analysis, the principal simulator, cooperation, evaluation and judges.

---

## 7. M2 — SOC lifecycle and triads (23–29 Nov), Pilot A alongside

### Build

1. **Ledger changes B1 to B5** (section 3.6), with tests, fixtures and shapes. B4 changes the vocabulary: update `ontology.ttl`, the context, the shapes and the README together.
2. **Triad protocol** (`soc/triad/`): two independent drafts per chunk; sentence-aligned diff; one question per difference to the worker whose rendering is challenged; answers must cite the source, a ratified commitment or the brief, checked by an evidence validator (an uncited or fabricated citation counts as no answer); at most two rounds; votes by the agreement rule for the decision type; merge.
3. **Agreement rules** as a per-decision-type table in configuration: `ONE` provisional, `QUORUM` two of three for terminology and style, `ALL` for normative meaning, `SERIAL` principal only for brief gaps and brief changes (section 4 on the scope of `ALL`).
4. **Provisional use**: a provisional reading may appear in drafts; a chunk cannot pass final acceptance while it depends on an unresolved normative decision or a provisional one used in more than three chunks; if overturned, every draft that used it is marked stale.
5. **Capture**: each worker returns a decision manifest; capture compares output with the ledger and records undeclared choices as silent decisions.
6. **Gate**: rejects output that breaks the current version or reuses a tombstoned rendering; acts on strength (`must` blocks, `should` warns with reason, `may` never blocks); logs every decision as `GateDecision`.
7. **Impact analysis and repair paths**: on each new version, mark chunks whose usage records hold an older version in the affected scope; hints to chunks in progress; read repair at review and at the gate; a final sweep before assembly. Log which path caught each stale chunk.
8. **Executor change**: rebind the operation; the successor receives ratified and provisional commitments, open flags and partial output, and never reads an older ledger version than its predecessor.
9. **Principal simulator** (`soc/principal/`): the only runtime component that reads the answer key; answers only what is asked; the same normalized question gets the same answer in every condition (answer cache); every answer counts as principal burden; it issues the brief change.
10. **Orchestrator** (`soc/orchestrator/`): run specs, conditions as configuration, the event schedule, chunk scheduling with up to three chunks in flight (so rulings can collide), resume from the last event. It requires the public episode (B5).
11. **Form's two other modes** as configuration: principal settles every flag; open discussion without cross-examination.

### Pilot A (23–29 Nov, needs M1 only)

Run on pilot episodes, never scored. Set each model's low and high effort; pick the single setting Form and Align use; tune retrieval depth and mix; collect 300 sentence pairs for checker validation and about 200 inflected term occurrences to choose the lemmatizer. Report tokens, latency and failures per model.

### Exit tests — the 14 scripted scenarios, with fake models

1. Two drafts diverge on a term; valid cross-examination ends in a two-of-three ruling that a later chunk uses.
2. A normative divergence keeps one dissenter; unanimity fails; the principal decides.
3. A worker meets a taste question the brief does not cover; a triad that votes on it is counted as a brief gap voted on.
4. An answer quotes text not in the source; the evidence check fails; it counts as no answer.
5. A worker renders a term without declaring it; capture records a silent decision.
6. Two triads ratify different renderings for the same decision subject and use site; one stands, the other becomes a conflict (B2).
7. A ruling is revised while a finished chunk holds the old rendering; impact analysis marks it; a later chunk copying the old rendering is rejected at the gate.
8. A ruling lands mid cross-examination; the chunk receives it as a hint at its next step.
9. A model is replaced; open flags survive; the successor never reads an older ledger version.
10. The brief changes at 60% revising a term and a scope-limited decision; SOC repairs from usage records, the RAG workflow from text search; repair precision is logged for both.
11. A reviewer passes a planted error (placeholder until M3).
12. A run hits its token cap; it ends budget-exhausted and its partial output is still scored.
13. Provider errors are retried; an interrupted run resumes from its last event.
14. A repaired chunk records a new build; the final sweep accepts it (B3).

Plus: one live episode completes in every SOC condition.

### Out of scope

Standing, weighted votes, sanctions, planted-error injection, evaluation pass, judges, metrics, statistics.

---

## 8. M3 — Cooperation layer and evaluation (30 Nov–6 Dec)

### Build

1. **Standing** (`soc/cooperation/`), from runtime checks only, never from ground truth: choices declared or silent, violations caught at the gate, rulings won with evidence, flags that led to rulings, planted errors caught or missed. Recomputed from recent checks.
2. **Weighted votes**, **graduated sanctions** (lighter vote, no reviewing, no vote, replacement), each step lifted when recent checks show compliance; **inherited or fresh standing** for a successor, counterbalanced: episodes 1–6 hidden arm inherits and announced arm starts without a vote; episodes 7–12 swap.
3. **Announced and hidden prompt variants**: announced states the rules and the agent's standing; hidden gets a neutral paragraph within 2% in tokens. A golden-prompt test proves hidden prompts carry no rule text or standing.
4. **Planted-error injector and reverter**: errors copy the types of natural errors in that chunk; their records live where no prompt builder can read them; every planted span is reverted before scoring, and the evaluation asserts none remains.
5. **Evaluation pass** (`eval/`): scores every seeded item in every run, including rejected and unfinished work; source fidelity, client preference and commitment continuity scored separately.
6. **Judge panel**: one flagship per family, the same for every condition, blind to condition (a scrub test on judge inputs); scores kept per judge.
7. **Every metric** in the measures table, including those in section 3.5, the Cassandra rate (correct lone readings outvoted), flips, rubber-stamped reviews, defection rate, principal burden, completion and cost.
8. **Analysis pipeline** (`analysis/`): score → average each condition's three runs per episode → three primary tests (Form non-inferiority at 5 points, one-sided; Keep SOC-minus-RAG continuity averaged over effort; Align announced-minus-hidden defection), Wilcoxon signed-rank with bootstrap intervals, Holm across the three → describe everything else as paired differences with 95% bootstrap intervals → report from fixed seeds. Include the Align descriptive analysis in section 4.

### Exit tests

- Every metric on a hand-built log matches a hand count.
- On 2,000 simulated studies, the rejection rate with no true effect is within 1.5 points of alpha, and power for a paired effect of 0.9 is near 80%.
- A label-swap test flips every difference's sign and leaves every p-value unchanged; a test proves no analysis counts more than 12 units.
- Announced and hidden prompt sections differ by under 2% in tokens.
- Planted errors are fully reverted before scoring.
- Canary strings planted in hidden preferences and expected answers never appear in any outgoing prompt.

---

## 9. Episode authoring — parallel track (12 Nov–6 Dec)

Can authors and reviews; Codex builds the tooling.

- **Tooling (start in M1):** a template generator that segments a chosen RFC range, proposes scope keys and candidate decisions, and writes a package skeleton; a command that reports coverage-rule status for a draft package; a flagship-model pre-check of expected answers and affected sets whose disagreements are listed for Can.
- **Content:** 12 evaluation episodes from RFC 9700 and two or three related OAuth RFCs published after March 2015 (same IETF Trust provisions), each 8–12 sections chunked by section, about 30 seeded items per source document, about five unwritten preferences, the two events, and affected sets with both sides. Three or more pilot episodes from other sections, never scored. Outputs keep the IETF notices.
- **Done when:** all 12 pass the validator, including the coverage rule, from the package alone, and Can has adjudicated every pre-check disagreement. Late warning: fewer than 12 reviewed by 6 December.

---

## 10. M4 — Pilot B and instrument validation (7–12 Dec)

Run the full matrix once on the pilot episodes: 3 Form, 4 Keep, 2 Align, 3 reference conditions (36 runs with three episodes). Set the planted-error share from the natural error rate, the per-run token cap at three times the pilot median, and timeouts. Report the baseline's headroom (its continuity after both events) and, if Can decides it, the deadlock rate under `ALL`. **Measure run throughput**: the one-week run window assumes under an hour per run and eight at once.

### Instrument thresholds

| Instrument | Threshold |
|---|---|
| Deterministic checker rules | Precision ≥ 0.95 and recall ≥ 0.90 per rule on 300 sentence pairs |
| LLM meaning check | Recall ≥ 0.80; used as a flagger only |
| Lemma matcher | Accuracy ≥ 0.95 on about 200 inflected occurrences |
| Capture | Recall and precision ≥ 0.90 |
| Judge panel | Krippendorff's alpha ≥ 0.67 among judges and against the author |
| Seeds and affected sets | Every disagreement adjudicated by Can |
| Principal simulator | Every seeded question answered from the key |
| RAG baseline strength | Keeps ≥ 90% of approved decisions before the first event; otherwise tune it until it does |

### Go/no-go — the freeze goes ahead only if all nine hold

1. Every condition completes every pilot episode, under 5% of calls failing after retries.
2. Workers keep sentence IDs in ≥ 99% of outputs.
3. Fewer than 1% of calls per model fail to parse after one repair retry.
4. Every instrument meets its threshold.
5. The RAG baseline keeps ≥ 90% of approved decisions before the first event.
6. The coverage rule holds for every evaluation episode.
7. The canary scan finds nothing in any pilot prompt.
8. A pilot run replays exactly from the cache.
9. A dry run of the analysis on pilot data produces every registered table.

---

## 11. M5 — Freeze and registration (13 Dec)

Tag a release. Write a manifest hashing code, prompts, configs (models, effort, retrieval depth, caps, planted-error share), episodes, seeds and analysis code. Write the pre-registration document: the three primary tests, the margin, alpha and Holm, the descriptive measures, and the rerun and exclusion rules below. The runner refuses to start a main run if any hashed artifact differs.

**Rerun and exclusion rules:** an infrastructure fault reruns from scratch with the same spec and the failed attempt is reported; a budget-exhausted run is not rerun and counts against completion; a refused call is retried once; if a frozen model disappears or a bug affects results, every condition of the affected episodes is handled the same way and logged as a deviation.

**Done when:** the runner rejects a config that differs from the manifest, and a dry run on replayed pilot data produces every registered table.

---

## 12. M6 — Main runs, analysis, release (14 Dec–4 Jan)

| Window | Work |
|---|---|
| 14–16 Dec | Keep, 144 runs, blocks of one episode and replicate with its four conditions in random order |
| 17–20 Dec | Form and Align, 180 runs, blocks of five conditions in random order |
| 14–20 Dec, idle capacity | Reference, 36 runs |
| 21–27 Dec | Scoring as runs finish, registered analysis, Can's blind review sample |
| 28 Dec–4 Jan | Report and demo (Can's), public repository with replay instructions |

- One scheduler process caps concurrency per provider; chunks run in parallel within a run.
- Every run writes events as it goes and resumes from its last event.
- Run directories copied to object storage nightly; keys in the VM's secret store.
- A progress view shows only operational numbers. **Comparisons between conditions stay locked until the matrix is complete.** More than 5% failed calls in an hour pauses the queue.
- Record each run's final event hash and count in `run.json`, outside the ledger, so a truncated ledger is detectable.

**Done when:** all 360 runs are completed, budget-exhausted or rerun under the registered rule, and one command rebuilds every results table from the raw logs.

---

## 13. If the schedule slips

- **Early sign:** M2's exit tests not met on 29 November.
- **Response:** register and run Keep first on 13 December; register Form and Align before their runs start. Keep still needs the planted-error injector, because planted errors appear in every condition.
- **Cut, in this order** (none changes a registered test): the demo's ledger viewer, replaced by a command-line timeline; the LLM half of the reviewer's diff, leaving the deterministic diff and self-reports (decided before registration); the reference runs, down to one rotating flagship.
- **Never cut:** instrument validation, the baseline's tuning, the freeze, the commitment schema.
- If even that fails, tell Can at once; asking the course for more time is Can's call.

---

## 14. Invariants that must hold in every run

1. The event log is append-only. 2. Projections equal a replay of the events. 3. A ratification applies only if its base version is current; otherwise it is a conflict. 4. Versions rise by one and never repeat. 5. A ruling binds only at its agreement level, with evidence that passed the check. 6. Only the principal changes the brief. 7. A tombstoned rendering never passes the gate where it is not current. 8. Every chunk whose usage holds an older version of a revised commitment is marked stale. 9. A successor never reads an older ledger version than its predecessor. 10. Ground truth never reaches an agent. 11. No agent sees planted-error records or condition names. 12. Judges never see the condition. 13. Hidden-arm prompts carry no standing or rule text. 14. Every condition gets the same episodes, seeds, event schedule and planted errors. 15. The same principal question gets the same answer in every condition. 16. Main runs start only on registered artifacts.

Those checked at runtime stop the run rather than letting it skew a result.

---

## 15. Next action

Start M1 now with the Inspect AI trial. The full M1 checklist is in `CODEX_BRIEF_M1.md`, which governs where it is more specific than section 6 of this plan. Ask Can for the provider API keys through the environment; do not request or store them in the repository.
