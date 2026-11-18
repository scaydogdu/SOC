# Semantic Operation Continuity testbed

M0 builds the foundations for testing whether a task-owned ledger preserves translation decisions across model replacement and brief changes. The milestone brief is in [CODEX_BRIEF_M0.md](CODEX_BRIEF_M0.md).

## Setup and checks

With `uv` available on your PATH, run `uv sync --locked` to install the locked Python 3.12 environment. Run the every-commit suite with:

```sh
./scripts/check
```

After setup, the script uses uv's offline mode and pytest disables sockets. The script fails if any implemented core area has less than 90% branch coverage. The GitHub Actions workflow runs the same script on pushes and pull requests.

```sh
uv run --locked --offline python -m eval.episode data/episodes/toy
uv run --locked --offline python -m soc.tasks.segmenter tests/fixtures/rfc9700.xml
```

The segmenter accepts `--glossary` with one UTF-8 term per line. Joining the JSONL records' `text` values recovers each section's exact character content in order, including whitespace. Use `reading_text` for translation. It expands prepared cross-references, includes contact names in the acknowledgements, and collapses layout whitespace. Whitespace-only records, boilerplate, and the table of contents are marked non-translatable. Heading and nested section contents have their own XML structure and are excluded from the parent's text. Source code and artwork preserve the literal inner XML bytes, including entity spellings and line endings, and receive `translatable: false`. Glossary matching is case-insensitive and includes regular plurals and possessives. Prepared XML supplies section numbering, including appendix letters and boilerplate labels. The test fixture is the RFC Editor's [RFC 9700 XML](https://www.rfc-editor.org/rfc/rfc9700.xml), with its notices intact.

## Episode schema

All seven files use strict Pydantic models in `soc.episode` or `eval.episode`. Unknown fields are rejected. JSONL files contain one JSON record per line.

| File | Fields |
| --- | --- |
| `source.jsonl` | `id`, `section`, `text`, `reading_text`, `scope_keys` (list of strings), `translatable` (boolean, defaults to true) |
| `brief.yaml` | `clauses` (list of records with `id` and `text`) |
| `events.yaml` | `chunks` (ordered records with `sentence_ids`), `executor_change_after` (chunk), `brief_change` (chunk, `clauses`, and `revisions` with `decision_id` and new `value`), `approvals` (each with `decision_id`, `kind`, chunk, `brief_clause_ids`, approved `value`, and `scope_keys`) |
| `hidden.yaml` | `preferences` (records with `id`, `text`, and `brief_clause_ids`), `planted_errors` (independent records with `id`, `chunk`, `sentence_id`, and `kind`) |
| `seeds.jsonl` | `id`, `type`, `sentence_ids`, `expected_answer`, `correctness` (one of `source_fidelity`, `client_preference`, `commitment_continuity`) |
| `affected.jsonl` | `decision_id`, `repair` (earlier use sites), and `apply` (later use sites). Both lists contain records with `chunk` and `sentence_id` |
| `manifest.json` | `sha256` mapping each of the six content filenames to its lowercase SHA-256 digest |

Chunk positions start at 1. Approvals and changes take effect after their named chunk. Each decision has its own ID and one or more scope keys. An approval tags the decision as `term`, `non_lexical`, or `scope_limited`. The validator derives coverage by matching scope keys against sentence uses in chunks after approval. It checks that affected use sites match the decision scope and fall on their declared side of the brief change.

`soc.episode.load_runtime()` opens the three public files and returns the public schedule. `eval.episode.load_full()` verifies the manifest and reads the complete package. `eval.episode.validate()` also checks references, event positions, and coverage. Finder's `.DS_Store` is the only name ignored when checking the seven-file layout. Import-linter enforces the dependency boundary around `eval` and `analysis`.

When the orchestrator is implemented, the trusted run launcher should materialize a public-only directory for the translation workers and give the planting controller a separate schedule. Prompt builders should receive only the public episode and the intended chunk input, with no filesystem access to the frozen package or to the planting schedule.

## Ledger

Open one `soc.ledger.store.Ledger(path, run_id)` per process, using it as a context manager. The write API has three entry points:

- `ratify(actor, identity, base_version, ruling, reason)` creates or ratifies a commitment. Use base version 0 for a new commitment.
- `transition(kind, actor, commitment_id, base_version, ruling, reason)` revises, supersedes, or revokes an existing commitment. A failed compare-and-set returns a `ConflictRaised` event. `Revised` updates an existing ruling and may mark the new version amended or ratified. `Superseded` records a ratified replacement version. Any version change that removes a rendering from a scope records that loss in a tombstone within the same transaction.
- `record(actor, payload, chunk_id=..., base_build=...)` records a principal brief change, a chunk build, or an executor rebound. The first chunk build uses base 0 and build number 1. A repair supplies the previously accepted build number as its base and appends the next build with its output reference, ledger snapshot, and commitment versions used. `snapshot().chunks` and `snapshot().usage` reflect the latest accepted builds. `snapshot().chunk_history` keeps each build for audit. For rebinding, the caller supplies the durable executor agent as the debtor identity, with the model change in the payload. Each rebound closes the previous executor epoch and opens the next. The initial epoch starts at sequence 0.

Every accepted event advances the ledger sequence once. Only a commitment change advances its per-commitment version. The SQLite file uses WAL and `BEGIN IMMEDIATE`, with append-only and contiguous-version constraints. The event stores the typed payload and its JSON-LD graph. SHACL validates the graph inside the transaction before insertion. Prior versions, commitment citations, and chunk usage references are read from the log. Pass the public episode as `episode=...` to also validate verbatim source quotes and resolve source and brief citations. Later brief clauses become citable after their change event. Ledger authority checks assume the caller supplies authenticated actor identities and roles.

Use `soc.orchestrator.ledger.open_run_ledger(episode_dir, ledger_path, run_id)` to open a writable run ledger. It reads the three public files and validates their references before opening SQLite. The ledger binds to that episode on first use and requires it when reopened. Brief citations record the clause text and the sequence at which it took effect, with sequence 0 for the initial brief. Replacing a clause affects subsequent citations. Isolated ledgers can be constructed without an episode for tests.

Open an M2 run ledger with `authority=OperationAuthority(operation=..., principal=...)` to enforce authority for its operation. The same authority is required when reopening. The registered principal changes the brief and issues principal rulings. `record_triad_ruling` checks the ballots from the orchestrator's authenticated participants against the agreement table and the ruling's rendering and sentence. An authorized triad can revise a triad-owned ruling with new evidence and the required distinct approvals. The ledger checks the approvers against the submitted ballots and records each ballot as a `VoteCast` event in the same transaction as its ruling, including dissent and abstentions. Legacy runs use the durable agent-ID owner check.

A verified `ONE` decision can be recorded as a provisional commitment and referenced by a draft build. `check_provisional_dependencies` logs a gate decision against the latest build. It blocks unresolved normative dependencies, any unresolved provisional ruling used by more than three current drafts, and drafts whose provisional ruling was overturned. Rebuilding a draft refreshes the usage index. Confirmation with the required vote count clears a provisional dependency when the rendering and applicability are unchanged.

The SOC gate loads the output referenced by the accepted build and checks it against the current ledger and brief. It records a `GateDecision` with blocking `must` findings, `should` warnings, and permitted `may` findings. A tombstoned rendering uses the commitment's current strength and can be readopted at a specific site. Uncheckable rules are recorded as `unknown`. Workers can submit a manifest of commitment versions and sentence IDs with their draft. Capture records verified and unsupported declarations and logs observable undeclared uses of known current, provisional, or tombstoned renderings.

A bound ledger derives build IDs from its public episode in schedule order, starting at `chunk-1`. It rejects unknown build IDs and recorded use sites from other chunks. The `chunk_id_base` option controls the ledger ID convention and is pinned on reopen. SOC revisions use build usage records to mark stale drafts. Include `ChunkBuilt.use_sites` to identify the sentences where a version was used. Older builds with version references alone receive a conservative scope check. `Ledger.revision_hints` exposes applicable revisions for drafts still in progress. The repair entry points use one rebuild callback and record whether review, the gate, or the final sweep first found each stale build. The final sweep requires every scheduled chunk and gates their current artifacts in one transaction.

For site-specific rulings, supply a canonical `DecisionSubject(kind, key)` on the commitment and the applicable source `sentence_ids` on each ruling. With a public episode attached, the ledger checks that each site is translatable and matches the ruling's scope keys. An overlapping binding ruling for the same subject in the same operation produces a `ConflictRaised` event with the candidate ID, the blocking IDs and versions, and the shared sites. Use disjoint site lists for exceptions. Legacy commitments without a subject retain scope-key behavior.

Use `snapshot()` for the refreshed projection, `rebuild()` for independent replay from an empty projection, and `verify()` for the first broken sequence number or `None`. `reader().in_scope(scope, at=version)` reads current or historical commitments, with a non-decreasing version per handle. Historical reads show the validity interval as known at the requested version. `is_tombstoned(rendering, scope)` reports a past removal while no current ruling uses that rendering in that scope. Both queries accept `sentence_id=...` to check a specific use site. These read operations do not write to SQLite.

Every commitment version stores exactly one `soc:strength` value, `must` (required), `should` (advised), or `may` (optional). The typed input defaults to `must` when the field is omitted. Strength is independent of state and consistency. Historical records without the field read as `must` through the typed projection. M0 records strength for later use.

The hand-written ontology, context, SHACL shapes, and fixtures are under `soc/ledger/schema`. Each commitment version expresses its scope keys through `soc:antecedent` and its required rendering through `soc:consequent`. A revision activity links to its input version with `prov:used` and its output version with `prov:generated`. The generated version links to its predecessor with `prov:wasRevisionOf`. Historical events retain the JSON-LD context and graph stored at write time. See the [approved M2 ontology design](REVIEW_M0_2.md#b4-antecedent-and-consequent-mean-something-else-here).

Run the extended state machine and toy write benchmark after setup:

```sh
SOC_LEDGER_EXAMPLES=10000 uv run --locked --offline pytest -q --hypothesis-show-statistics tests/property/test_ledger_state_machine.py
uv run --locked --offline python -m scripts.measure_shacl
```

The extended run can also be split into independent, seeded runs of 2,500 examples each. Run the following in four terminals with a different seed from 401 to 404:

```sh
SOC_LEDGER_RANDOM=1 SOC_LEDGER_EXAMPLES=2500 uv run --locked --offline pytest -q --hypothesis-seed=401 --hypothesis-show-statistics tests/property/test_ledger_state_machine.py
```

## Shared translation checker

`soc.enforce.checker.Checker` accepts source sentences, an aligned `ChunkOutput`, the active brief, and applicable decisions and forbidden renderings. It checks sentence IDs, verbatim segments, capitalized BCP 14 wording, numbers, approved terms, and forbidden wording. Findings include the rule, sentence ID, available decision or brief references, strength, and output character offsets when there is text to point to.

Pass approved Turkish modal wording as `NormativeRendering` entries tied to active brief clauses. Missing or conflicting wording produces `unknown`. The checker covers negation through BCP 14 polarity. General negation and semantic checks belong to the M3 meaning checker. The M1 Turkish stem matcher handles common inflections and consonant softening. Pilot A evaluates replacements behind the `Lemmatizer` interface.

## RAG baseline

`soc.baseline.memory.RAGMemory` implements the shared memory interface. Each prompt receives the current glossary and style guide, plus the top retrieved notes with rationale. `RetrievalConfig` defaults to five notes and equal weights for normalized BM25 and dense cosine similarity. A revision replaces the indexed note, keeps its history, and searches saved output for the previous wording with the checker's inflection matcher. Executor handover includes the current notes and partial translation.

The offline suite uses a deterministic hash embedder. To provision the pinned BGE-M3 checkpoint and run its separate smoke test, use the optional `embeddings` dependencies.

```sh
uv run --locked --extra embeddings python -c 'from soc.baseline.retrieval import BGEM3Embedder; BGEM3Embedder(local_files_only=False)'
uv run --locked --offline --extra embeddings pytest -q -m nightly tests/unit/test_rag_memory.py
```

The normal adapter loads the checkpoint from the local model cache. [Pinned BGE-M3 model](https://huggingface.co/BAAI/bge-m3/tree/5617a9f61b028005a4858fdac845db406aefb181)

## Offline episode runner

Record the scripted toy episode and replay it from its cache.

```sh
soc_run_root=$(mktemp -d)
uv run --locked --offline python -m soc.orchestrator data/episodes/toy "$soc_run_root/recorded" \
  --config tests/fixtures/toy-run-config.yaml --cache "$soc_run_root/cache.sqlite" \
  --script tests/fixtures/toy-run.yaml
uv run --locked --offline python -m soc.orchestrator data/episodes/toy "$soc_run_root/replay" \
  --config tests/fixtures/toy-run-config.yaml --cache "$soc_run_root/cache.sqlite"
```

Use the same run directory and configuration to resume. The runner restores completed worker steps from `journal.sqlite` and recovers a pending step from its exact cached response when present. It can repeat an interrupted request if the response had yet to reach the cache.

`run.json` records the run identity, configuration, prompt hashes, status, token counts, and estimated cost. The directory also contains compressed call logs, delivered decision versions, scheduled events, per-attempt checker findings, final chunks, and a final check against the active brief. The single experiment-level repair limit covers both draft and post-revision repairs. The toy CLI uses scripted responses, a hash embedder, and test-only modal wording.

## Nightly live checks

Set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, and either `GEMINI_API_KEY` or `GOOGLE_API_KEY` in the execution environment. Use one campaign directory for probes, the live toy, and its replay. The live toy uses the pinned local BGE-M3 checkpoint.

```sh
uv run --locked python -m soc.orchestrator.live probe --output /path/to/m1-campaign
uv run --locked --extra embeddings python -m soc.orchestrator.live toy --output /path/to/m1-campaign
uv run --locked --offline --extra embeddings python -m soc.orchestrator.live replay --output /path/to/m1-campaign
```

Keep the campaign directory to reuse its SQLite response cache and spend ledger. Live commands reuse recorded responses for matching model, effort, messages, schema, sampling settings and replicate. They make provider calls on cache misses and can retry previous rate-limit, server or budget failures. Use `replay` to reproduce the saved toy with zero provider calls and no API keys. Replay requires an exact recorded run, call ID and request key and takes the code revision from the recorded run.

`config/m1-live.yaml` sets a shared USD 40 ceiling on standard text-generation charges. Before each physical attempt, including retries, the persistent spend ledger reserves at the configured model limits. OpenAI Responses requests with an explicit output limit use the smaller of the request and model output limits. Provider-reported usage settles the reservation at the configured price ceilings. A call with unknown usage retains its reservation. These figures are local upper estimates of token charges. Use the provider invoices to reconcile billed spend.

## Episode authoring

Create a draft from an inclusive RFC section range. The generator makes one chunk per section, retains stable source IDs, and prints candidate scope keys. Add `--glossary FILE` to include known terms. Fill the brief, events, preferences, seeds, and affected sites in the draft before sealing it.

```sh
uv run --locked --offline python -m eval.authoring skeleton tests/fixtures/rfc9700.xml /path/to/draft --first 1 --last 4
uv run --locked --offline python -m eval.authoring coverage /path/to/draft
uv run --locked --offline python -m eval.authoring seal /path/to/draft
```

The coverage report works before the event positions are filled and lists all unmet checks. Sealing validates the complete package before updating its manifest. For a nightly independent review, run `python -m eval.precheck /path/to/draft --campaign /path/to/m1-campaign` after the chosen flagship passes its high-effort probe. The precheck keeps the answer keys out of model prompts, puts reports and cache entries outside the episode, and leaves disagreements for the author to adjudicate. Seed wording differences, including paraphrases, appear for review.
