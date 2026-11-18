# Review of uncommitted M1 live work and early M2 ledger work

> **Resolved 18 November 2026 in `0a3b62f` and `1edd8ca`.** The reviewer re-checked all six findings on those commits.
>
> - `./scripts/check` passes: 243 tests, ledger branch coverage 94.3%.
> - The reviewer's own break-it scripts now behave correctly:
>   - an evidence-only upgrade from QUORUM to ALL leaves the build current, and a rendering change still marks it stale (finding 1);
>   - an unknown chunk and a use site from another chunk are both refused (finding 2);
>   - approvals without matching ballots are refused, and votes are recorded as `VoteCast` events (finding 4);
>   - strict replay no longer falls back by request key (finding 6).
> - Finding 3 is covered by the new scenario tests listed in `M2_STATUS.md`.
> - Finding 5 is fixed: ruff format passes.
>
> The text below is the review as written against the 16:39 snapshot.

**Reviewer:** Claude · **Date:** 18 November 2026
**State reviewed:** a snapshot of the working tree taken at 16:39 on top of `07e48aa`. The snapshot holds 41 modified files and 14 new files. None of it is committed. Work that continued after the snapshot may already have changed some findings.
**Reviewed against:** `CODEX_BRIEF_M1.md`, `PROJECT_PLAN.md` sections 3.6 and 7, and decisions B1–B5 in `REVIEW_M0_2.md`.

The reviewer changed nothing here except adding this file. The break-it experiments ran on a copy.

## Verdict

The M2 ledger design is largely in place and well tested where it is tested. Two behaviours would distort the Keep experiment and must be fixed before Pilot A:

- a revision that changes nothing a translation must apply still blocks earlier chunks;
- chunk builds are not tied to the episode's chunks.

`./scripts/check` does not pass yet.

| Check | Result |
|---|---|
| Tests | 235 passed, 2 deselected |
| Ruff lint, strict mypy, import contract | Pass |
| Ruff format | **Fails:** `soc/ledger/store.py:315` |
| Branch coverage floor (90%) | **Fails:** ledger 264/296 (89.2%). Every other package passes |

## What is good

Each of decisions B1–B5 is built and tested.

- **B1 Authority classes.** Principal rulings require SERIAL consistency and the operation's principal. Triad rulings require three distinct members, enough distinct approvers for the consistency level, and no downgrade from the previous level. Only the principal may change the brief.
- **B2 One ruling per subject per use site.** An overlapping binding ruling is refused with `ConflictRaised` and a list of the overlaps. This is tested, including a widening revision that collides.
- **B3 Rebuilds.** `ChunkBuilt` uses compare-and-set on the build number and requires an output reference. The full history is kept, and usage moves to the latest build.
- **B4 Vocabulary.**
  - `soc:antecedent` and `soc:consequent` are on the version.
  - A new shape requires them to equal the scope and the rendering.
  - A revision must have `prov:used` and `prov:generated`.
  - A new `ChunkBuild` class and shape were added.
  - The fixtures were updated.
- **B5 Episode binding.** The ledger stores the public episode's hash and refuses to reopen with a different episode or with none.
- **Verified agreement path.** The orchestrator's `record_triad_ruling` checks the ballots against the agreement table before writing. The table is kept in configuration, as the plan's default for open decision 9 requires.

---

## A. Fix these

### 1. A revision that changes nothing applicable still blocks earlier chunks (high)

`_needs_refresh` (`soc/ledger/store.py:1191`) ends with `return True`. Any later version therefore counts as needing repair even when the rendering, rule, strength and sites are unchanged. Raising the consistency level or adding a citation marks every chunk built on the old version as stale, and the gate then blocks it.

Reproduction on the toy episode:

1. Ratify `D1` at QUORUM across its four sites.
2. Build `chunk-1` on version 1. The gate accepts it.
3. Revise to ALL with one more citation, keeping the rendering, rule, strength and sites the same.

Result: `stale_chunks == {"chunk-1"}` and the gate refuses with "chunk uses a superseded ruling in the affected scope".

Impact: the SOC arm records false blocks and spends repair budget on chunks that are correct. Both feed Keep's measures of false blocking, cost and completion.

**Required:** a later version needs refresh only when something the output must apply changed. That covers:

- rendering, rule or strength;
- revocation;
- a site the build used that is no longer covered;
- for rulings without sites, a scope key the build relied on that was removed.

Add a test that a revision changing only evidence or consistency marks nothing stale.

### 2. Chunk builds are not tied to the episode (medium)

`Ledger.record` accepts any non-empty `chunk_id`. It does not check that a use site's sentence belongs to that chunk, only that the site is in the ruling's scope. On the toy episode the ledger accepted a build for `no-such-chunk` whose use site was `9999-2-s03`, a sentence in chunk 4.

The ledger already holds the bound episode. Impact analysis and continuity are counted per chunk and per use site. Builds under invented chunk IDs, or sites outside the chunk, would be counted wrongly without any error.

**Required:**

- Give chunks a defined identifier derived from the episode, for example the one-based position. This depends on Can's open decision about chunk numbering; build the current convention behind a switch.
- Reject a build whose chunk is not in the episode.
- Reject a use site whose sentence is not in that chunk.

### 3. The impact-analysis code has no tests (medium)

The uncovered lines in `store.py` are, among others:

- `repair_request` (978–995);
- the per-site branch of `_needs_refresh` (1206–1216);
- `_revision_hints` (1222–1241).

This is the code that produces Keep's impact measure and the repair hints. It is also why ledger coverage is under the floor.

**Required:** scenario tests that cover:

- narrowing a ruling's sites, where only builds that used a removed site become stale;
- a repair request that returns the right hints and records the first detection path once;
- a rebuild that ignores the hint and keeps the old version, which must be marked stale again.

### 4. Approvals are asserted, not recorded (medium)

`RulingApproval` carries approver names. The ledger checks that there are enough of them and that they are distinct, but it records no `VoteCast` events, and no ballot reaches the ledger. The ballots are verified only in memory by `record_triad_ruling`. A ledger read later cannot show who voted, and any caller of `ratify` or `transition` can name approvers who never voted.

**Required:** do one of the following, and say which in the status file.

- **Preferred:** append the ballots as `VoteCast` events in the same transaction and have `_authorize_ruling` check approvers against them.
- **Alternative:** store the ballots inside the approval payload.

Either keeps Align's defection measure auditable from the ledger alone.

### 5. Formatting (low)

Run `ruff format`. `./scripts/check` must pass before the commit.

## B. Carried over from the earlier M1 notes

### 6. Replay can still return another run's response (low)

In `Mode.REPLAY`, a missing exact record falls back to `cache.read(key)`, the first stored response for the same input from any run (`soc/providers/client.py`, the cache lookup in `call`). Strict replay should fail with `CacheMiss`. Keep the fallback for `RECORD_MISSING` only.

## C. Notes, no change required

- **Retained spend reservation.** About USD 35 of the USD 40 cap is still reserved for five Gemini calls whose usage never came back. Real spend is USD 0.12. The guard behaves as designed. Can needs to release the reservation or raise the cap before the Gemini re-probe.
- **Schema upgrade.** It copies events into the new table with their hashes unchanged, and one legacy ledger test covers it. It briefly drops the append-only triggers inside one transaction, which is acceptable for a one-time upgrade.
