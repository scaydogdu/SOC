# Brief: commitment timeline export

**From:** Claude (reviewer) · **For:** Codex · **Date:** 18 November 2026
**Fits in:** analysis tooling, alongside M2 and M3. It changes no runtime behaviour and no registered measure.

## 1. Purpose

Every SOC run leaves a `ledger.sqlite`. Build a command that turns any such ledger, together with its public episode, into one self-contained HTML page. The page shows:

- which version of each approved decision every chunk build used;
- which revisions flagged which builds as stale;
- how the gate and the repairs resolved them.

The page is used to:

- inspect pilot and main runs;
- explain gate decisions;
- put a run in the final presentation.

A reference page exists: `docs/reference/commitment-timeline-demo.html`. Claude built it by hand from a scripted toy run, using the public ledger API. Treat it as the specification for layout and behaviour; you don't need to reuse its code. Its data comes from a run that predates your scheduled runner, so its chunk IDs and actors may differ from yours.

## 2. Command

```sh
uv run python -m analysis.timeline RUN_DIR EPISODE_DIR --out PAGE.html [--json DATA.json]
```

- **Inputs.** `RUN_DIR` holds `ledger.sqlite`, as written by `run_ledger_episode`. `EPISODE_DIR` holds the public episode files, and only those. It never reads `hidden.yaml`, `seeds.jsonl` or `affected.jsonl`.
- **Outputs.**
  - `--out` writes the page.
  - `--json` also writes the exported data on its own, so `analysis/` and tests can use it.
- **Location.** The code goes in `analysis/timeline.py`. `analysis` may import `soc`; the import contract already forbids the reverse.

## 3. Hard rules

1. **Never write to a recorded ledger.**
   - Do not open a run's ledger with `Ledger(...)`, because its constructor can insert binding rows.
   - Open the file read-only, with SQLite URI `mode=ro`.
   - If you need a shared reader, add a small read-only function to `soc.ledger`, for example `read_events(path) -> list[Event]`. It must reuse the existing hashing code.
   - **Test:** the ledger file's SHA-256 is unchanged after an export.
2. **Verify before drawing.**
   - Re-check the hash chain and the `public_episode` hash binding against `EPISODE_DIR`.
   - If either fails, exit non-zero, naming the first broken sequence number. Never render a page for a broken or mismatched ledger.
3. **Deterministic output.**
   - The same ledger and episode always give byte-identical HTML and JSON.
   - Take no clock time, random IDs or ordering from dictionaries.
   - Sort by sequence number, then by ID.
4. **Self-contained page.**
   - One HTML file with inline CSS, inline JS and the data embedded as JSON. Escape `</` in the embedded JSON.
   - Load nothing from the network: no fonts, scripts or images.
   - Use system font stacks. The page must render offline from a release archive.
   - The reference page loads Google Fonts. Yours must not.
5. **Don't hide structural data.**
   - Show every ledger event in the log, including events of unknown type, which get a generic row.
   - An event that a view cannot place, such as a gate decision for a build that doesn't exist, is listed in a visible "unplaced events" note, never dropped.
6. **Label the page.** The page shows the run ID, the episode ID, the ledger's final sequence number and the head hash. If the run is scripted or a fixture, the page says so. Take that from the run's spec if it records it; otherwise add an optional `--label` text.

## 4. What the page shows

Match the reference page for each item below.

- **Header.** Counts of:
  - decisions, chunks and ledger events;
  - revisions;
  - builds flagged stale;
  - gate blocks;
  - chunks still stale at the end;
  - tombstones.
- **Matrix.**
  - **Layout:** one row per commitment and one column per episode chunk, in episode order, using the ledger's pinned `chunk_id_base`. A top row shows each build's gate result.
  - **Event markers:** column edges mark the executor change and the brief change, taken from the episode events and the `ExecutorRebound` and `BriefChanged` events. Follow the plan's convention that an event at chunk N happens after chunk N.
  - **Cells:** each cell lists, for every build of that chunk that used the commitment, the build number and version (`b2·v1`), with a status:
    - `blocked`: a `StaleMarked` event naming this commitment hit the build, and the build's gate decision refused it;
    - `stale`: a `StaleMarked` event naming this commitment hit the build after it was recorded and before the chunk's next build;
    - `current`: otherwise.

    Mark the last build in each cell. Write "not used" where no build used the commitment.

    Status must be shown in text or shape as well as colour.
- **Detail panel.**
  - **Decision:** every version with:
    - rendering, state, consistency, strength and validity interval;
    - evidence and reason;
    - its ballots, marking dissent and abstention;
    - its tombstones.
  - **Chunk:** source sentences, then every build with the ledger snapshot it read, the versions it used, the gate reason, its stale marks, its detection path and its output reference.
  - **Cell:** the use sites with sentence text, and how each build used the commitment there.
- **Ledger strip.** One tick per event in sequence order, coloured by kind, with the executor and brief changes marked. Blocked gate decisions are visually distinct.
- **Event log.**
  - **Table:** sequence number, type, actor, a one-line summary and the hash prefix.
  - **Filters:** by kind, with votes off by default.
  - **Selection:** selecting a matrix cell, row or column narrows the log to related events, with a visible "Show all".
- **Themes and widths.** Light and dark themes following `prefers-color-scheme`. Phone width is supported: the matrix and the log scroll inside their own boxes.

## 5. Data export

Document the JSON schema in the module docstring and in `README.md`. At minimum it must hold:

- run and episode identity;
- chunks with sentences and scope keys, and the executor and brief change positions;
- commitments with subject and authority;
- versions with ballots;
- builds with uses, use sites, gate decisions, stale marks and detections;
- tombstones;
- the raw event list without the `@graph` provenance blocks, so the file stays small.

Give the export a `schema_version`.

## 6. Not now, but leave room

- **The RAG arm has no ledger.** Do not fake one. Add an optional `--rag RUN_DIR` later, once M3 produces per-use-site continuity scores. It would show, for the same episode:
  - the RAG arm's repair selections, from `eval.repair_precision`;
  - its continuity results per use site.

  Make the page structure able to take a second matrix without a rewrite.
- **Evaluation data stays out of this page.** Seeds, affected sets and scores belong to `eval`. Any later view that overlays them must be a separate evaluation-side command. That keeps this exporter on the runtime side of the boundary.

## 7. Exit tests

1. Export the scripted toy SOC run from your existing scenario tests. The JSON matches a committed golden file, and a second export is byte-identical.
2. The ledger file's SHA-256 is unchanged after export. A test that patches `sqlite3.connect` shows the database is opened with `mode=ro`.
3. A ledger with one altered byte exits non-zero, naming the right sequence number. A ledger paired with a different episode exits non-zero.
4. Status rules from section 4, each with its own fixture:
   - an evidence-only revision leaves cells `current`;
   - a rendering revision makes earlier builds `stale`;
   - a repair that keeps the old version is `blocked` when the gate refuses it;
   - a correct rebuild ends `current`.
5. The counts in the summary equal counts computed directly from the event list.
6. The page HTML contains no `http://` or `https://` resource reference.
7. The embedded JSON round-trips: parse it out of the HTML and compare it with `--json`.
8. An unknown event type appears in the log and in the unplaced events note, and the export does not crash.
9. Run the page's script in a headless browser and check that it throws no errors. If that needs a browser dependency the CI can't carry, mark the test `nightly` and say so in the status file.
10. `./scripts/check` passes, and coverage for `analysis/timeline.py` is at least 90% of branches. Add it to the branch-coverage script.

## 8. Out of scope

- Live updating, servers, and any write path back into a ledger.
- Charts of experiment results. Those come with the M3 analysis pipeline.
- Changes to ledger semantics. If the export exposes a ledger behaviour that looks wrong, log it in `OPEN_QUESTIONS.md` and keep drawing what the ledger says.

## 9. Hand back

- Add a section to `M2_STATUS.md` covering:
  - the command;
  - the JSON schema version;
  - the golden-file test names;
  - a generated page for the scripted toy run, committed under `docs/examples/`.
- Include the `./scripts/check` output.
- List every choice you made that this brief left open.
