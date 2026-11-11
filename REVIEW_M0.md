# Review of milestone M0 — first pass

**Reviewer:** Claude · **Date:** 10 November 2026 · **Reviewed against:** `CODEX_BRIEF_M0.md`
**State reviewed:** working tree with no commits; `./scripts/check` passing (23 tests, about 1.4 s, offline).

The reviewer changed nothing in this repository except adding this file. Break-it experiments ran on a scratch copy.

## Verdict

**M0 is not complete.** Sections 4.1 to 4.3 of the brief are built and tested. Sections 4.4 (ledger core) and 4.5 (commitment ontology and SHACL shapes) are not started, and the hand-back report in section 8 is missing.

| Brief section | State |
|---|---|
| 4.1 Repository, CI, import contract | Built |
| 4.2 Episode package, two loaders, validator, toy episode | Built; see findings 4 to 8 |
| 4.3 RFC segmenter | Built; see findings 1 to 3 and 9 |
| 4.4 Ledger core | **Not started** (`soc/ledger/__init__.py` is a stub) |
| 4.5 Ontology, JSON-LD context, SHACL shapes, fixtures | **Not started** |
| 8 Hand-back report | **Missing** |

| Exit test | State |
|---|---|
| 1 Ledger property test | Cannot run |
| 2 Concurrency | Cannot run |
| 3 Crash | Cannot run |
| 4 Replay | Cannot run |
| 5 Append-only | Cannot run |
| 6 Schema | Cannot run |
| 7 Segmenter round trip | Passes as written, but the reference shares the implementation's blind spot (finding 1) |
| 8 Episode validator | Passes |
| 9 Boundaries | Passes, but the loader test can be bypassed (finding 4) |
| 10 Hygiene | Passes for the code that exists; the 90% floor is not enforced (finding 8) |

## What is good

- The check script, CI workflow and import-linter contract are in place and run offline in well under the five-minute budget.
- Strict, frozen Pydantic models reject unknown fields, so an answer field cannot ride along in a runtime file.
- The validator's rule-named errors and the broken-copy tests are real: 11 of 13 deliberate breakages were caught by the suite.
- Verbatim blocks keep their literal bytes, and BCP 14 detection matches the 124 tagged key words in RFC 9700 exactly.

---

## A. Fix these — they are within the brief

### 1. The segmenter drops cross-reference text (high)

`soc/tasks/segmenter.py:74` (`_parts`) reads element text and tails only. In prepared RFC XML, a cross-reference carries its visible text in the `derivedContent` attribute: RFC 9700 has 402 `<xref>` elements, 294 with non-empty `derivedContent`.

Result: `9700-1.1-s02` reads `presents the updated OAuth attacker model.` The source says "Section 3 presents the updated OAuth attacker model." 33 translatable sentences start with a lowercase letter for this reason, and many more have a hole mid-sentence ("according to , or").

The round-trip test does not catch it because `tests/unit/test_segmenter.py:45` builds the expected text with `itertext()`, which has the same blind spot. A test whose reference is computed the same way as the implementation cannot fail on this.

**Required:** sentence text used for translation must contain the cross-reference text a reader sees. How that interacts with the exact round trip is open question 5 and is Can's decision; see section B. At minimum, add a test that asserts a known sentence reads correctly, independent of `itertext()`.

### 2. Non-content is emitted as translatable sentences (high)

On RFC 9700, of 915 segments:

- 91 are whitespace only and marked `translatable: true`;
- the table of contents and boilerplate are emitted as translatable sentences, including bare `.` fragments (`9700-toc.1-s49`);
- 518 translatable sentences begin with XML layout whitespace, and 582 contain internal layout newlines.

Every one of these would become a unit of translation work and a scored item.

**Required:** no whitespace-only segment may be translatable. Table of contents and boilerplate handling is tied to open question 5; see section B.

### 3. Glossary matching misses plurals (medium)

`soc/tasks/segmenter.py:134` matches a term only when followed by a non-word character. Of 51 sentences containing "access tokens", 46 do not receive the `access token` scope key.

Scope keys drive the coverage rule in `eval/episode.py:178` and will drive scope lookup in the ledger. Missing keys mean missed uses.

**Required:** match regular English plurals and possessives of glossary terms, with a test on RFC 9700 counts.

### 4. The runtime-boundary test can be bypassed (medium)

`tests/contract/test_episode.py:15` patches `Path.read_text` only. A loader that reads `hidden.yaml` with `open()` passes all 23 tests (verified on a scratch copy).

The brief's checklist asks that the runtime loader's inability to reach evaluation data be structural. Today all seven files sit in one directory that is handed to runtime code.

**Required:** make the test independent of the read method (for example, give the runtime loader a directory that contains only the three public files and assert it still loads, and assert the loader never opens other names by patching at the OS-open level). Say in the report how runtime code will be kept from the evaluation files once the orchestrator exists.

### 5. An empty chunk is accepted (low)

`eval/episode.py:126` checks that chunks partition the sentences, which an extra chunk with no sentences still satisfies. An event can then be scheduled "after" a chunk that contains nothing.

### 6. A decision listed twice in the brief change is accepted (low)

`eval/episode.py:201` converts `decision_ids` to a set, so duplicates pass silently. Duplicate `decision_id` values among approvals are also collapsed by the dict at line 186.

### 7. A `.DS_Store` file fails a valid episode (low)

`eval/episode.py:80` requires the directory to hold exactly seven names. On macOS, opening the folder in Finder creates `.DS_Store` and the validator then reports `manifest: expected exactly seven episode files`. Also add `.DS_Store` to `.gitignore`.

**Required:** decide and document which stray names are ignored; do not weaken the check that every one of the seven files is present and hashed.

### 8. The 90% coverage floor is not enforced (low)

`scripts/check:7` reports coverage but sets no failure threshold. Add one for the core packages. `pyproject.toml:39` already lists `soc.ledger`, which produces a "never imported" warning until the ledger exists.

### 9. The abbreviation list is short (low)

`soc/tasks/segmenter.py:17` handles `e.g.`, `i.e.`, `cf.`, `vs.` and `etc.` These still split wrongly: `See Sec. 4`, `Hardt et al. describe`, `in the U.S. only`, `No. 5`. Add the common ones with tests, and report how many sentences in RFC 9700 each affects.

---

## B. Format and definition questions — B1 to B4 and the round trip are now decided

Can decided these five on 10 November 2026 ("Yes, all five"), accepting the reviewer's recommendations. Each decision is stated under its item. Implement them as written; field names remain your choice and must be documented in the README. Part of the cause of these gaps is that the brief left field names to you without spelling these out.

Three questions in B5 are **still open**: the chunk convention, the vocabulary IRI, and SHACL in the write path. Hosting and licence are also still Can's.

### B1. Approved decisions carry no content

`Approval` (`soc/episode.py:41`) has an ID, a kind, a chunk and brief clause IDs, but not the approved rendering or reading. `BriefChange` has free-text clauses but no new value per revised decision. The main experiment must feed the same approved decisions, with their content, to two memory systems at the same points. The format cannot express that yet.

**Decided:** each approval carries the approved rendering or reading, and the brief change carries a new value for each decision it revises. The validator rejects an approval without content and a revised decision without a new value.

### B2. Planted errors reference evaluation seeds

`PlantedError.seed_id` (`soc/episode.py:54`) points at `seeds.jsonl`, and the validator requires it to. The project's design keeps planted errors — runtime checks on reviewers — separate from evaluation seeds, and the seed ID is currently visible to runtime code.

**Decided:** planted errors get their own records, separate from `seeds.jsonl`, and their identifiers stay out of everything the runtime loader returns. The orchestrator still needs to know when and where to plant; say in the report how it gets that without the prompt builders being able to read it.

### B3. What "affected" means

The toy's `affected.jsonl` lists use sites *after* the brief change (chunk 4). Impact analysis is scored on *earlier* chunks that were built on the old version and need repair; continuity after the change is scored on later uses. The format needs to say which it holds, or hold both with the side derivable.

**Decided:** `affected.jsonl` holds both sides for each revised decision: the earlier chunks that were built on the old version and need repair, and the later use sites that must apply the new version. The side must be explicit or derivable from the chunk position, and the validator checks that each site sits on the side it claims. Update the toy episode to match.

### B4. Decision IDs are scope keys

README line 36 makes a decision ID the same string as a scope key. That couples the decision's identity to segmenter output, and the toy's `exception-scope` key is one the segmenter would never produce. This may be fine; it should be a choice, not an accident.

**Decided:** separate them. A decision has its own ID and lists the scope keys it covers. Coverage is computed through that list.

### B5. The open questions in `OPEN_QUESTIONS.md`

| Question | Status |
|---|---|
| Round-trip reference for XML | **Decided:** keep the exact section text for the round trip, and add a clean reading text per sentence that includes cross-reference text and has layout whitespace normalised. Mark table of contents and boilerplate as not for translation. This resolves findings 1 and 2 |
| One-based chunks; an event at chunk N happens after it | **Open.** The reviewer recommends confirming; Can has not answered. Keep the current convention meanwhile |
| `soc:` IRI `https://w3id.org/soc-testbed#` | **Open.** The reviewer finds it acceptable as provisional; it is not registered. Keep it in one constant |
| Keep SHACL in the write path | **Open.** Cannot be decided yet; no timings exist |
| Hosting and licence | **Open.** Can's |

---

## C. Still to build for M0

1. Section 4.4, the ledger core, with exit tests 1 to 5.
2. Section 4.5, the ontology, context, shapes and fixtures, with exit test 6 and the validation timings.
3. Section 8, the hand-back report, including every "your choice" made so far (CI system, field names, decision tagging, the IRI) and anything skipped.
4. A first commit, so later reviews can be made against a diff.

5. The five decisions in section B, applied to the episode format, the validator, the segmenter, the toy episode and the README.

Findings 1 to 9 and the section B decisions do not block starting the ledger.
