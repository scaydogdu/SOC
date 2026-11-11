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
- `transition(kind, actor, commitment_id, base_version, ruling, reason)` revises, supersedes, or revokes an existing commitment. The commitment owner's durable agent ID controls updates. A failed compare-and-set returns a `ConflictRaised` event. `Revised` updates an existing ruling and may mark the new version amended or ratified. `Superseded` records a ratified replacement version. Any version change that removes a rendering from a scope records that loss in a tombstone within the same transaction.
- `record(actor, payload, chunk_id=...)` records a principal brief change, a chunk build, or an executor rebound. For rebinding, the caller supplies the durable executor agent as the debtor identity, with the model change in the payload. Each rebound closes the previous executor epoch and opens the next. The initial epoch starts at sequence 0.

Every accepted event advances the ledger sequence once. Only a commitment change advances its per-commitment version. The SQLite file uses WAL and `BEGIN IMMEDIATE`, with append-only and contiguous-version constraints. The event stores the typed payload and its JSON-LD graph. SHACL validates the graph inside the transaction before insertion. Prior versions, commitment citations, and chunk usage references are read from the log. Pass the public episode as `episode=...` to also validate verbatim source quotes and resolve source and brief citations. Later brief clauses become citable after their change event. Ledger authority checks assume the caller supplies authenticated actor identities and roles.

Use `snapshot()` for the refreshed projection, `rebuild()` for independent replay from an empty projection, and `verify()` for the first broken sequence number or `None`. `reader().in_scope(scope, at=version)` reads current or historical commitments, with a non-decreasing version per handle. Historical reads show the validity interval as known at the requested version. `is_tombstoned(rendering, scope)` reports a past removal while no current ruling uses that rendering in that scope. These read operations do not write to SQLite.

The hand-written ontology, context, SHACL shapes, and fixtures are under `soc/ledger/schema`. Run the extended state machine and toy write benchmark after setup:

```sh
SOC_LEDGER_EXAMPLES=10000 uv run --locked --offline pytest -q --hypothesis-show-statistics tests/property/test_ledger_state_machine.py
uv run --locked --offline python -m scripts.measure_shacl
```

The extended run can also be split into independent, seeded runs of 2,500 examples each. Run the following in four terminals with a different seed from 401 to 404:

```sh
SOC_LEDGER_RANDOM=1 SOC_LEDGER_EXAMPLES=2500 uv run --locked --offline pytest -q --hypothesis-seed=401 --hypothesis-show-statistics tests/property/test_ledger_state_machine.py
```
