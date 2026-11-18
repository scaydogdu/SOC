# SOC testbed — Milestone M1 brief for Codex

**Milestone:** M1 Providers, checker and RAG baseline · **Window:** 12 to 22 November 2026 · **Written:** 11 November 2026
**Builder:** Codex · **Reviewer and tester:** Claude · **Owner of every decision:** Can Aydoğdu
**Starts from:** M0, accepted at commit `a2ed844`. **Read first:** `PROJECT_PLAN.md` sections 1 to 4.

Where this brief and `PROJECT_PLAN.md` disagree, this brief governs and the difference is a question for Can. The working rules in `PROJECT_PLAN.md` section 2 apply unchanged.

---

## 1. What M1 is for

M1 builds everything the experiment needs that is not the SOC ledger: the clients that call the three model families, the cache that makes every run replayable, scripted fake models, the deterministic checker shared by both memory systems, and the RAG workflow that SOC is compared against.

The RAG workflow is the baseline of the primary experiment. **It must be strong.** If it is weak, the experiment shows nothing. Build it as a competent engineer would build a real glossary-and-retrieval translation pipeline, not as a straw man. Its strength is measured before the freeze, and if it keeps fewer than 90% of approved decisions it is tuned until it does.

## 2. Work order

Do the steps in this order. Steps 1 and 2 decide how much of steps 3 and 4 you write yourself.

1. Inspect AI trial (one day).
2. The model-client interface and the record-and-replay cache.
3. Provider clients, or the Inspect AI adapter.
4. Scripted fake models.
5. The memory interface.
6. Checker v1.
7. The RAG workflow.
8. A minimal runner for one worker per chunk.
9. Live contract probe and one live toy run.
10. Episode-authoring tooling (parallel track; see section 4.10).

---

## 3. Fixed decisions for M1

| Area | Decision |
|---|---|
| Structured outputs | Every model call that expects data uses a Pydantic schema sent as the provider's structured output, and the response is validated again locally. One repair retry on a parse failure, then the call counts as failed |
| Rationale | Schemas ask for a short rationale field, never step-by-step reasoning |
| Refusals and cut-offs | Logged as their own outcomes, never retried as ordinary errors. A refused call is retried once; a second refusal is a failed call |
| Effort | Configured per model as `low` or `high`. Each provider's own parameter is mapped from that label. Compare by tokens and cost, not by label |
| Cache key | Model ID, effort, full message list, schema, sampling settings and replicate number. Content-addressed. Replay mode never touches the network and fails loudly on a cache miss |
| Secrets | API keys come only from environment variables. Never in the repository, prompts, logs, cache files or run files |
| Offline suite | The every-commit suite stays offline and under five minutes. Anything that needs a network or a large model download runs only in a separately marked nightly layer |
| Repair budget | A configurable maximum number of repair rounds per chunk, **the same value for both memory systems**, default 2. Do not choose different values per arm |
| Chunk positions | One-based; an event at chunk N takes effect after chunk N (current convention; still awaiting Can's confirmation) |

### Provider parameters — verify before use

The project's planning notes recorded these on 27 October 2026. They may be wrong or out of date. **Check each against the provider's current documentation before relying on it**, and record what you found in the status file.

| Provider | Structured output | Effort |
|---|---|---|
| Anthropic | `output_config.format` | `output_config.effort`, low to max |
| OpenAI | `text.format` with strict schemas | `reasoning.effort`; `none` and `minimal` reportedly not accepted on the mid-tier model |
| Google | `response_format` | `thinking_level`: low, medium, high (minimal on the small model) |

The roster is in `PROJECT_PLAN.md` section 3.2. No ID has been probed yet. If an ID does not exist or misbehaves, record it in `OPEN_QUESTIONS.md` and stop using it; **do not substitute another model**.

---

## 4. What to build

### 4.1 Inspect AI trial — one day, first

Try the model layer of UK AISI's Inspect AI (`inspect_ai`) as the provider layer.

Keep it only if **all** of these hold, each shown by a test or a recorded experiment:

1. It calls all three providers with structured outputs.
2. It accepts the effort settings each roster model needs.
3. Its cache, or a cache you put in front of it, replays a recorded multi-call run with byte-identical responses and no network.
4. It reports input and output tokens per call.
5. It does not force its own evaluation framework on the code above the provider interface.
6. Its licence permits use in an open-source repository.

Write the result in the status file either way, with the evidence. **Keeping or dropping it is Can's decision**; until Can decides, continue on whichever path you judge faster, behind the same interface, so switching later costs only the adapter.

### 4.2 Model-client interface and record-and-replay cache

Location: `soc/providers/`.

- **One interface** for all models: a request (model, effort, messages, output schema, sampling settings, replicate number, run and call identifiers) and a response (parsed object or a typed failure, raw text, input tokens, output tokens, latency, outcome).
- **Outcomes** are an enumeration: `ok`, `parse_failed`, `refused`, `cut_off`, `rate_limited`, `server_error`, `budget_exhausted`, `cache_miss`.
- **Modes:** `live` (call and record), `replay` (cache only; a miss is an error), `record_missing` (replay, and call live only on a miss).
- **Every call is logged** to the run's `calls.jsonl` (compressed), with its cache key, outcome, tokens and latency. No key or secret ever appears.
- **Retries:** exponential backoff with jitter on `rate_limited` and `server_error`; a configured maximum.
- **Rate limits and concurrency:** per-provider caps, configurable.
- **Budget guard:** a per-run token cap. When it is reached, further calls return `budget_exhausted` and the run ends gracefully with its partial output kept.
- **Cost:** a per-model price table in configuration; every call records an estimated cost. Prices are configuration, not code.

### 4.3 Provider clients

Three thin clients (or the Inspect AI adapter) behind the interface in 4.2. Each maps the effort label, sends the schema as structured output, extracts tokens, and classifies failures into the outcomes above. Keep provider-specific code out of everything above `soc/providers/`.

### 4.4 Scripted fake models

A fake provider driven by YAML scripts, in `soc/providers/` or `tests/`. A script says, per call pattern, what to return: a parsed object, a malformed response, a refusal, a cut-off, a rate-limit error, or a delay. Fakes go through the same interface and the same cache, so a fake run is replayable like a live one.

Every M1 test that exercises the runner, checker or baseline uses fakes. M2's 14 scripted scenarios will be built on them, so make scripts easy to write and read.

### 4.5 The memory interface

Location: `soc/` (your choice of module; `soc/memory.py` or similar). This is the seam where the two memory systems are swapped. The RAG workflow implements it in M1; an adapter over the M0 ledger implements it in M2. **Design it now so that the M2 adapter needs no change to the RAG code or the runner.**

It must support these operations, under names of your choice:

| Operation | Meaning |
|---|---|
| Record an approved decision | An approval arrives at its scheduled point, with its value and scope keys |
| Apply a revision | The brief change revises a decision to a new value |
| Context for a chunk | Returns what a worker's prompt receives for this chunk, **and the list of decisions delivered**, with the version or note revision of each |
| Forbidden renderings for a chunk | What the checker must treat as forbidden in this chunk's scope (superseded renderings) |
| After an output | Lets the system record what a chunk used |
| Affected chunks after a revision | Which already-built chunks the system believes need repair |
| Executor change | What the successor model receives |

Two rules:

- **Delivered-decision logging is mandatory in both systems.** The run log must record, for every chunk, which decisions were in its prompt. This is how the experiment tells "never delivered" apart from "delivered and broken".
- **Both systems receive identical approvals and revisions at identical points.** The runner feeds them; the memory system never reads the episode's schedule on its own.

### 4.6 Checker v1

Location: `soc/enforce/`. Deterministic, no model calls. Shared unchanged by both memory systems. It takes a chunk's source sentences, the worker's output aligned by sentence ID, the brief, the decisions in force, and the forbidden renderings, and returns a list of violations.

Each violation records: rule ID, sentence ID, the decision or brief clause involved, the offending text span, and the **strength** of the commitment involved (`must`, `should` or `may`; every commitment in the experiment is `must`, but carry the field).

| Rule | Checks |
|---|---|
| `ids` | Every source sentence ID appears exactly once in the output, and no other ID appears |
| `verbatim` | Non-translatable segments (code, artwork) are byte-identical to the source |
| `bcp14` | For each capitalized BCP 14 key word in a source sentence, the output contains the brief's approved Turkish rendering for that key word. Count matters: two key words in the source need two renderings |
| `numbers` | Every number in the source sentence appears in the output, unchanged |
| `term` | For each glossary term or approved decision in scope, the output uses the approved value, allowing Turkish inflection |
| `forbidden` | No forbidden rendering appears in the output, allowing Turkish inflection |

- **Negation.** Turkish marks negation inside the verb, so a general negation check is not deterministic. In M1, negation is covered only through `bcp14` (for example MUST NOT versus MUST). General meaning, including negation, is the LLM meaning check built in M3. Say this in the README.
- **Turkish inflection.** Use the stem matcher in M1: match an approved term against inflected forms, including consonant softening (*belirteç* → *belirteci*, *belirteçler*, *belirtecin*). Put it behind an interface named for the lemmatizer, so Pilot A can swap in Zemberek, zeyrek or Stanza.
- **No silent passes.** If a rule cannot be evaluated (for example a missing brief rendering for a key word), it returns an explicit `unknown` result, not a pass.

### 4.7 The RAG workflow

Location: `soc/baseline/`. Implements the memory interface.

- **Notes.** Every approved decision becomes a note: decision, value, scope keys, rationale. Notes are versioned; a revision replaces the note's content and keeps its history.
- **Glossary and style guide**, built from the brief and the approved decisions, **pinned in every prompt**. This is the remedy published for constraints lost when context is compacted; the baseline must have it.
- **Retrieval** of the notes most similar to the chunk: BGE-M3 embeddings mixed with BM25, top *k*. Depth *k* and the mix weight are configuration, tuned in Pilot A.
  - BGE-M3 is a large local download. Pin it by revision. In the every-commit suite use a small deterministic stand-in embedder behind the same interface; the real model runs only in the nightly layer.
- **After a revision.** Replace and re-index the note; update the glossary; then search every built chunk's output with the shared checker for the old value (inflection-aware) and mark the hits for repair. That list is the baseline's answer to "affected chunks".
- **Repair.** A marked chunk is re-prompted with the current glossary and the checker's violations, up to the shared repair budget.
- **Executor change.** The new model receives the glossary, style guide, note index and partial output.

Do not give the baseline usage records, compare-and-set, tombstones or a gate. Those are what SOC adds and what the experiment tests.

### 4.8 Minimal runner

A sequential runner, one worker per chunk, enough to run a whole episode through a memory system with the checker and the repair loop, and to apply the executor change and brief change at their scheduled points. Triads replace the single worker in M2; build the runner so that the per-chunk step is the only part M2 replaces.

- The runner reads the episode through the **runtime loader only** (`soc.episode.load_runtime`).
- It writes a run directory: `calls.jsonl`, the final chunks with sentence IDs, a delivered-decisions log, a violations log, and `run.json` (condition, episode, replicate, models, effort, code commit, prompt hashes, status, tokens, cost).
- It resumes an interrupted run from its last completed step.
- Prompts live in `prompts/` as versioned templates, rendered from fixtures in a golden-snapshot test (the plan names `syrupy`; your choice of tool). Any prompt change is a visible test change.

### 4.9 Live contract probe and one live toy run

- **Probe:** one schema-constrained call per roster model, per effort setting it will use: the call succeeds, the effort is accepted, tokens are reported, and a deliberately impossible request is classified as `refused` or `parse_failed` rather than crashing. Marked nightly; never in the every-commit suite.
- **Live toy run:** run the toy episode once through the RAG workflow with live models. Record it, then prove it replays identically from the cache.
- **Keys and cost.** Ask Can to set the API keys as environment variables. Set a hard total spend cap for M1 live calls in configuration and report actual spend.

### 4.10 Episode-authoring tooling (parallel track)

Can authors the 12 evaluation episodes; you build the tools. Start once 4.6 exists.

1. **Package skeleton generator:** given an RFC XML file and a section range, segment it, propose scope keys and candidate decisions (terms that recur across chunks, BCP 14 key words), chunk by section, and write a package skeleton with empty seeds, preferences and events.
2. **Coverage report:** for a draft package, report the coverage rule's status item by item, so Can can see what is missing before validating.
3. **Pre-check (nightly, live):** a flagship model independently answers each seed and each affected set; list every disagreement for Can to adjudicate. Never edit the package automatically.

---

## 5. Exit tests

M1 is done only when each line passes and the real output is in `M1_STATUS.md`.

| # | Test | Passes when |
|---|---|---|
| 1 | Inspect AI trial | A written result against the six criteria in 4.1, with evidence |
| 2 | Contract probe | Every roster model answers at each effort it will use, or its failure is in `OPEN_QUESTIONS.md` |
| 3 | Replay | A recorded multi-call run, fake and live, replays with identical outputs and no network; a cache miss in replay mode fails loudly |
| 4 | Re-scoring | Re-running the checker over a recorded run's outputs gives identical violations |
| 5 | Failure handling | With fakes: rate-limit and server errors are retried with backoff; refusals and cut-offs are classified; a parse failure gets exactly one repair retry |
| 6 | Budget guard | A run that reaches its token cap ends `budget_exhausted` with partial output kept and scored |
| 7 | Resume | A run killed mid-episode resumes from its last completed step and produces the same final output as an uninterrupted replay |
| 8 | Checker fixture | At least 50 cases, at least five per rule, each case failing its rule when the output is broken and passing when it is correct; includes inflected Turkish forms and consonant softening; includes an `unknown` case |
| 9 | Memory interface | The RAG workflow passes an interface contract test written so the M2 ledger adapter can run the same test |
| 10 | Delivered decisions | For every chunk of a toy run, the log lists exactly the decisions in that chunk's prompt |
| 11 | Baseline, fake models | The toy episode completes end to end, including the executor change, the brief change, revision search and repair |
| 12 | Baseline, live models | The toy episode completes once with live models, and replays identically |
| 13 | Repair budget | Both the configured maximum and its equality across arms are enforced by a test |
| 14 | Secrets | A test scans every prompt, log and run file from the suite for key-like strings and finds none |
| 15 | Boundaries | The import contract holds; the runner uses only the runtime loader |
| 16 | Hygiene | Ruff and mypy strict clean on new core packages; branch coverage at least 90% on `soc/providers`, `soc/enforce`, `soc/baseline`; every-commit suite offline and under five minutes |

---

## 6. What the reviewer will check

- The cache key covers everything that changes a response; two calls differing only in effort or replicate never share an entry.
- Replay mode cannot reach the network, structurally, not by convention.
- No secret appears anywhere the suite writes.
- Fakes go through the same interface and cache as real providers.
- Every checker rule can fail, and fails for its intended reason only; `unknown` is never counted as a pass.
- The inflection matcher handles consonant softening and does not over-match unrelated words sharing a prefix.
- The baseline is genuinely strong: pinned glossary, retrieval, revision search with the shared checker, repair within the shared budget. No SOC mechanism has leaked into it.
- The memory interface can carry the ledger in M2 without changing the RAG code or the runner.
- Delivered decisions are logged identically in both future arms.
- Provider parameters were checked against current documentation, and the status file says what was found.
- Nothing from section 7 was built, and nothing in section 4 was quietly simplified.

## 7. Out of scope for M1

Triads, cross-examination, votes, capture, the SOC gate, impact analysis from usage records, the ledger adapter for the memory interface, the principal simulator, the five M2 ledger changes (B1 to B5), standing, sanctions, planted errors, the LLM meaning check, the evaluation pass, judges, metrics, statistics, the operational profile. Do not author evaluation episodes; build only the tooling in 4.10.

## 8. Open questions already known

Do not settle these; build the default.

| Question | Default in M1 |
|---|---|
| Keep Inspect AI's model layer | Your choice of path behind the interface, until Can decides |
| Equal repair rounds for both systems, and whether to report first-pass violations too | Equal, configurable, default 2; log first-pass violations anyway |
| One-based chunks, events after chunk N | Current convention |
| **New:** a scope-limited revision cannot change scope | `Revision` in the episode format carries only `decision_id` and `value`, so the brief change cannot narrow or widen a decision's scope keys. The coverage rule requires a scope-limited revision in every episode. Treat a scope-limited revision as a value change in M1 and record this question in `OPEN_QUESTIONS.md` for Can |

## 9. What to hand back

`M1_STATUS.md` containing:

1. What was built, by section of this brief.
2. The Inspect AI trial result and the provider parameters as you found them in current documentation.
3. The real output of every exit test.
4. Measured numbers: suite duration, coverage per package, live tokens and spend, probe latency per model.
5. Every "your choice", one line each.
6. Anything skipped, stubbed or failing, stated plainly.
7. Updated `OPEN_QUESTIONS.md`.

Commit the work. Do not commit `gitlab-sign-in.jpg`, currently untracked in the repository root, or any other file not part of the project.
