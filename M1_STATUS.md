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
