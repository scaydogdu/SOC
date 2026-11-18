# M1 status

## 12 November 2026 · Inspect AI trial

M1 started from the accepted M0 baseline. The trial used Inspect AI `0.3.289` in an isolated `uv` environment. All three providers' credentials were absent from the process environment, so the trial used a scripted response at Inspect's model boundary and inspected the installed provider adapters. Live calls made and spend incurred were both zero.

| §4.1 criterion | Evidence |
| --- | --- |
| Structured outputs across all three providers | The installed adapters serialize OpenAI Responses `text.format`, Google `response_json_schema` with `response_mime_type`, and Anthropic's older `output_format` beta request. Anthropic's current documentation uses `output_config.format`. Live schema-constrained calls are pending. |
| Roster effort settings | Installed adapters map Inspect's `reasoning_effort` to provider settings. Inspect documents automatic effort fallback for rejected settings, so a successful response alone would not prove that the configured effort was used. Live roster verification is pending. |
| Multi-call byte-identical replay without network | Two scripted responses replayed byte-for-byte with socket connects, DNS, and provider generation disabled. An unseen input reached the provider boundary, so SOC needs a separate cache-only path that fails on misses. |
| Input and output tokens per call | Both values were present in `ModelOutput.usage` and identical on replay. The trial injected 11 input and 5 output tokens per call. Live provider accounting is pending. |
| Independent model layer | `get_model().generate()` worked without constructing an Inspect evaluation task, solver, or scorer. |
| Licence | The installed wheel includes the MIT licence. |

[Inspect model API](https://inspect.aisi.org.uk/reference/inspect_ai.model.html) · [Inspect provider behavior](https://inspect.aisi.org.uk/providers.html) · [Inspect licence](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/LICENSE)

Reproduce the trial after installing the pinned dependency in the isolated environment.

```sh
uv run --no-project --python 3.12 --with inspect-ai==0.3.289 -- python scripts/trial_inspect.py
```

```text
2 calls: response bytes and reported usage matched with provider and sockets disabled
Cache miss attempted provider generation
```

**Working choice.** Start with thin provider clients behind the shared SOC interface and keep replay independent of provider code. This lets the offline work proceed while Can's Inspect AI decision and live probe results are pending.

## Provider parameter verification

- **Anthropic Messages.** Send JSON Schema through `output_config.format` and set `output_config.effort` to `low` or `high`. The effort guide lists Opus 5.5, Sonnet 5.5, and Haiku 5.5 with both levels. [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs) · [Effort](https://platform.claude.com/docs/en/build-with-claude/effort)
- **OpenAI Responses.** Send a strict JSON Schema through `text.format` and set `reasoning.effort` to `low` or `high`. The GPT-6 guide lists the three roster IDs and calls for omitting `temperature`, `top_p`, and `top_logprobs` when reasoning effort is enabled. [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs) · [GPT-6 guide](https://developers.openai.com/api/docs/guides/latest-model)
- **Google Gemini.** Generate Content supports JSON schema through the SDK's `response_json_schema` with JSON MIME type. `response_format` is the Interactions API field. Set `thinking_level` to `low` or `high`. Both are documented for Argon and 3.8 Flash. Flash-Lite also offers `minimal`. [Structured outputs](https://aistudio.google.com/docs/structured-output) · [Gemini 4 Argon](https://aistudio.google.com/docs/models/gemini-4-argon) · [Gemini thinking](https://ai.google.dev/gemini-api/docs/thinking)

These are documentation and installed-source checks. The roster probe, provider token accounting, and recorded live toy run still require environment-provided API keys and the configured M1 spend cap.

## Starting regression check

`./scripts/check` passed. Ruff, strict mypy, and the import contract passed. Pytest reported `74 passed in 28.49s`. Branch coverage was 96.8% for the ledger, 98.1% for episodes, and 98.5% for the segmenter. These checks cover the existing M0 suite. The next M1 implementation step is the shared model-client interface and SOC record-and-replay cache.

## Shared model client and cache (§4.2)

- `soc/providers/models.py` defines the typed request and adapter contract. The client validates successful structured responses with the caller's Pydantic model. It retries a parse failure once with a JSON repair instruction, a refusal once, and transient rate-limit or server failures with configured exponential backoff and jitter. A cut-off ends the call.
- The SQLite cache hashes the model, effort, full messages, schema, sampling settings, and replicate. The first response for a key is available for reuse. Each run and call ID also has an exact response record, so two calls with identical inputs can replay their distinct responses. Replay accepts no provider routes and raises `CacheMiss` for a missing response while the run has budget.
- A per-family semaphore and request interval constrain provider attempts, including retries. The per-run token cap stops new attempts once reported usage reaches the threshold. Attempts already in flight can finish and make the final total exceed the cap. Exact replay preserves their results and usage.
- `calls.jsonl.gz` records attempt rows and logical call summaries with cache key, outcome, latency, token counts, and estimated cost from configured model prices. Use summary rows (`attempt` is `null`) for logical-call totals. The log omits prompts and response text. The client rejects requests containing configured provider keys and strips echoed keys from responses before caching.

The 14 focused tests passed. `./scripts/check` reported `88 passed in 31.23s`, with Ruff, strict mypy, and the import contract passing. Provider branch coverage was `42/42 (100.0%)`. The offline suite allows Unix-domain sockets for the asyncio event loop and still blocks network sockets and DNS. These tests use in-process scripted replies. The provider adapters, YAML-driven fakes, hard live-spend cap, and live probes are pending.

## Provider adapters and YAML fakes (§4.3–4.4)

- The three adapters use a shared `httpx.AsyncClient` with provider-specific request and response models. Tests inject `httpx.MockTransport`. Credentials come from the provider's environment variable and travel in request headers. Transport failures, HTTP 429, refusals, cut-offs, and malformed responses map to shared outcomes. A permanent request rejection raises a sanitized `ProviderRequestError`, logs a failed call, and bypasses repair and transient retries.
- OpenAI Responses uses strict `text.format` and `reasoning.effort`. Anthropic Messages uses `output_config.format` and `output_config.effort`. Both reject unsupported sampling fields for the reasoning roster. Gemini Generate Content uses `responseJsonSchema`, JSON MIME type, and `thinkingConfig.thinkingLevel`. The Gemini adapter counts thought tokens with output tokens. The Anthropic adapter includes cached prompt tokens in input usage. [OpenAI Responses](https://developers.openai.com/api/reference/resources/responses/methods/create) · [Anthropic Messages](https://platform.claude.com/docs/en/api/messages) · [Gemini Generate Content](https://aistudio.google.com/api-reference/generate-content)
- `ScriptedProvider` consumes an ordered YAML `steps` list, one entry per physical attempt. An entry can match a model glob, effort, or prompt substring and return structured data, raw text, or a failure, with optional latency and usage. The fake uses the same model client and cache as the live adapters. A two-call scripted run exercised a transient retry, refusal retry, JSON repair, and cut-off and replayed both responses exactly.

`./scripts/check` reported `118 passed in 30.11s`, including 30 adapter and scripted-provider tests. Ruff, strict mypy, and the import contract passed. Provider branch coverage was `118/122 (96.7%)`.

Live roster access, provider-reported usage, and the hard spend cap remain pending. These adapter tests used mocked HTTP responses. They made no live calls and incurred no spend. The next offline step is the shared memory interface and deterministic checker.

## Shared memory contract and deterministic checker (§4.5–4.6)

- `soc.memory.Memory` defines the runtime operations for approval, revision, chunk context, applicable decisions, forbidden renderings, recording output, finding affected chunks, and executor handover. The context pairs prompt text with exact decision IDs and versions so the runner can log delivery. Applicable decisions are available independently of prompt delivery for scoring. The RAG implementation and reusable memory contract test are pending.
- The checker preserves aligned sentence IDs, exact non-translatable text, source number spelling and count, and approved BCP 14 renderings. It checks scoped required and forbidden renderings with a replaceable Turkish stem matcher. Each finding carries the rule, sentence, strength, available decision or brief references, and original output offsets when an offending span exists. Normative counts use non-overlapping occurrences, including when two keywords share a Turkish rendering or a longer negative phrase contains a positive marker.
- **Working choices.** Approved modal wording is passed as explicit `NormativeRendering` values tied to active brief clauses. Missing or conflicting mappings report `unknown`. The toy episode lacks this wording for `MUST`; its descriptive interpretation supplies no exact phrase. The existing episode schema is retained. General negation is deferred to the M3 meaning checker. The Turkish matcher recognizes common suffixes and consonant softening, with lexical exceptions such as vowel deletion left for the Pilot A lemmatizer evaluation.

The checker fixture has 54 paired broken and corrected cases, nine per rule, plus four tests for unknown mappings, scope and attribution, replacement lemmatizers, and original-text offsets. `./scripts/check` reported `176 passed in 25.72s`. Ruff, strict mypy, and the import contract passed. Checker branch coverage was `64/66 (97.0%)`, and provider branch coverage remained `118/122 (96.7%)`.

## RAG baseline (§4.7)

- `RAGMemory` records approved notes with rationale and keeps their revision history. It pins the current glossary and style guide in each prompt, with exact delivered decision versions, and appends retrieved notes with rationale. A revision re-indexes the current note and retains the original scope. Executor handover carries the current notes and latest partial translation.
- The baseline finds affected chunks by searching every saved output with the checker's inflection matcher. The search can find old wording produced before approval or outside the decision's scope. It uses the latest output per chunk and reports no forbidden renderings. The shared repair loop and budget belong to the runner in §4.8.
- **Working choices.** Retrieval defaults to five notes and equal weights for dense cosine similarity and BM25. Dense scores below zero are floored at zero and BM25 is scaled by the best score for the query. Ties are resolved by decision ID. BGE-M3 uses a pinned upstream checkpoint and loads from the local cache by default. A deterministic hash embedder drives the offline tests. [Pinned checkpoint](https://huggingface.co/BAAI/bge-m3/tree/5617a9f61b028005a4858fdac845db406aefb181)

The offline suite reported `189 passed, 1 deselected in 33.71s`. Ruff, strict mypy, and the import contract passed. Baseline branch coverage was `26/26 (100.0%)`. The reusable memory contract ran against the RAG implementation. The selected fixtures also verified separate dense and lexical contributions to retrieval, pinning, revision replacement, and scanning saved output independently of prompt delivery.

The separate nightly smoke test ran against the actual pinned checkpoint and reported `1 passed, 10 deselected in 4.25s`. It verified normalized 1,024-dimensional embeddings with network sockets blocked and zero DNS lookups. The adapter opens the resolved snapshot directory directly in local mode. This avoids a metadata lookup attempted by the model library when given the repository name.

## Sequential runner (§4.8)

- `run_episode` reads the public episode through `load_runtime`, builds one chunk at a time, and applies approvals, executor handover, and brief revisions after their scheduled chunks. It uses the shared checker and one experiment-level repair budget per chunk, including draft and revision repairs. The worker call is injectable for later protocol work.
- `journal.sqlite` stores completed worker steps and a prompt hash for each pending step. Resume reconstructs memory and reports from completed steps. A pending call with an exact cached response is recovered without another provider request. A crash before the cache contains the response can require repeating the request. Compressed call logs and report files are published by atomic replacement.
- Reports include actual decision versions delivered, first-pass and later findings, final chunks and findings, scheduled events, token counts, estimated cost, and template and prompt hashes. A golden fixture covers the versioned translation, handover, and repair prompts. The toy CLI uses test-only wording for `MUST`; the public episode still needs an approved mapping.

The scripted toy completed four chunks with eight worker calls, including one draft repair and three revision repairs. It consumed 80 input and 40 output tokens in the scripted accounting. The recording and network-free replay produced byte-identical run projections. A separate re-score reproduced the final findings. Two SIGKILL tests interrupted a cached call before and after its log summary and recovered the same output with eight physical attempts. The suite also verified scored partial output at the token cap, the same repair policy under two condition labels, exact delivery logging, and the absence of key-like strings in rendered prompts and run artifacts. The actual ledger arm is M2 work.

`./scripts/check` reported `197 passed, 1 deselected in 32.21s`. Ruff, strict mypy, and the import contract passed. Branch coverage was 95.3% for providers, 97.0% for the checker, and 100.0% for the baseline. Live roster and toy runs, the hard live-spend cap, and episode-authoring tools remain open.

## Live campaign tooling (§4.9)

- The opt-in nightly commands probe the nine named models at both effort levels. Each check validates a positive structured response and token counts, then verifies that a contradictory local contract yields a refusal or parse failure. The report records each model and effort, token usage, latency, and any failure. The live toy uses GPT-6.1 Sol with GPT-6 Luna as successor, after those models pass their probes. The command compares all run projections with a fresh cache-only replay.
- **Working choices.** The shipped configuration sets a shared USD 40 ceiling. The SQLite spend ledger reserves at the documented token limits and conservative price ceilings before every request and retry. Known usage settles at the configured ceilings. Unknown usage and interrupted requests retain their reservations. The largest single reservation is about USD 31.83 for GPT-6 Astra. The OpenAI adapter selects standard processing and disables implicit cache writes. The campaign uses direct text endpoints and sends no tools or explicit caching requests. [OpenAI model limits](https://developers.openai.com/api/docs/models) · [OpenAI pricing](https://developers.openai.com/api/docs/pricing) · [Claude pricing](https://platform.claude.com/docs/en/about-claude/pricing) · [Gemini pricing](https://aistudio.google.com/docs/pricing)

The no-credential preflight reported `0/18 effort probes passed`, all 18 marked `missing_credentials`, with zero provider attempts and USD 0 spent. The paid nightly test was skipped. Mocked tests exercised the probe, the toy and replay, reservation recovery, concurrent reservation attempts, and a retry blocked by the cap. `./scripts/check` reported `203 passed, 2 deselected in 31.31s`. Ruff, strict mypy, and the import contract passed. Provider branch coverage was `138/146 (94.5%)`. Provider availability, live token accounting, and the real toy replay remain unverified until the environment keys are available.

## Episode authoring tools (§4.10)

- `eval.authoring skeleton` generates the seven-file draft package from an inclusive section range. Chunks follow RFC sections, and IDs and verbatim segments come from the existing segmenter. Candidate scope keys come from recurring two- and three-word phrases, optional glossary entries, and BCP 14 keywords.
- `eval.authoring coverage` lists each condition independently, including unfilled event positions. It shares its coverage predicates with the full validator. `seal` validates the completed draft before updating its manifest.
- `eval.precheck` uses a probed flagship at high effort. Its requests contain the seed or affected-set task with source context and omit the author's expected answer and affected lists. The report compares normalized seed answers and exact affected use-site multisets. A paraphrase may be flagged for adjudication. Precheck output and cache are separate from runtime output and the episode package. Calls share the campaign spend ceiling.

The RFC 9700 CLI smoke generated 41 candidates from sections 1–4. Its unfilled draft passed 6 of 12 coverage checks, and the existing toy passed 16 of 16. Mocked precheck found two deliberate disagreements and one agreement, with the episode files unchanged. `./scripts/check` reported `206 passed, 2 deselected in 27.05s`. Ruff, strict mypy, and the import contract passed. Provider branch coverage was 94.5%, checker coverage was 97.0%, and baseline coverage was 100.0%. The actual flagship precheck remains unverified until live credentials and its successful probe are available.

## M1 exit checklist

Checked on 18 November 2026. The live roster probe passed 14 of 18 checks. All OpenAI and Anthropic combinations and both Gemini 3.5 Flash-Lite combinations passed. Gemini 4 Argon was rate-limited and the spend guard blocked its retry. Gemini 3.8 Flash returned server errors. Both models' high-effort checks were skipped after their low-effort failures.

Five Gemini attempts retained USD 34.9568 in reservations because usage was unavailable. The remaining headroom was enough for the live toy once the guard used the OpenAI Responses request's enforced 4,096-token output limit. The guard still uses the configured input limit and retains the full reservation when usage is unknown. It stops further requests if a settled charge exceeds the reserved amount.

The live RAG toy used the pinned local BGE-M3 checkpoint and changed executors from GPT-6.1 Sol to GPT-6 Luna. It completed four chunks in six physical OpenAI calls, with 5,032 input and 720 output tokens. The toy's metered upper estimate was USD 0.0085324 and the campaign total was USD 0.1193114, against the original USD 40 cap. A fresh replay with network sockets disabled matched all six recorded output files byte-for-byte. The checker reported three `unknown` findings with the current modal configuration.

| # | Exit test | Evidence and status |
| --- | --- | --- |
| 1 | Inspect AI trial | Six-criterion report and reproducible offline cache trial above. Retention decision remains open. |
| 2 | Contract probe | 14 of 18 combinations passed. Gemini 4 Argon and Gemini 3.8 Flash remain open. |
| 3 | Replay | Scripted and live toy outputs matched their cache-only replays with network sockets disabled. The scripted test also verified a cache miss fails. |
| 4 | Re-scoring | Independently re-scoring the saved toy translations reproduced the final findings. |
| 5 | Failure handling | Tests exercise transient backoff, refusal and cut-off outcomes, and one JSON repair retry. |
| 6 | Budget guard | At the token cap, the scripted run returned `budget_exhausted` and preserved and scored two chunks. |
| 7 | Resume | Two simulated process kills recovered the cached response and produced the same projections as uninterrupted replay, with eight physical calls. |
| 8 | Checker fixture | 54 paired broken and corrected cases across the six rules, plus explicit `unknown`, scope, and offset checks. |
| 9 | Memory interface | The RAG workflow passed the reusable memory contract. |
| 10 | Delivered decisions | The eight toy prompts matched their logged decision IDs and versions. |
| 11 | Scripted baseline | Four chunks completed, including executor and brief changes, revision search, and repairs. |
| 12 | Live baseline | Four chunks completed using the two probed OpenAI models and pinned BGE-M3. The checker reported three `unknown` findings. |
| 13 | Repair budget | One experiment-level cap is enforced under two condition labels. The actual ledger arm will exercise the policy in M2. |
| 14 | Secrets | The toy test scanned rendered prompts, cache, logs, and run artifacts for key-like strings. |
| 15 | Boundaries | Import-linter passed. The toy ran from the three public episode files alone. |
| 16 | Hygiene | Offline results and package coverage below. |

`./scripts/check` reported `207 passed, 2 deselected in 34.47s`. Ruff, strict mypy, and the import contract passed. Branch coverage was providers `141/150 (94.0%)`, checker `64/66 (97.0%)`, and baseline `26/26 (100.0%)`.
