# Review of milestone M0 — second pass

**Reviewer:** Claude · **Date:** 11 November 2026 · **Reviewed against:** `CODEX_BRIEF_M0.md`, `REVIEW_M0.md`, `M0_STATUS.md`
**State reviewed:** commit `b878200` ("Complete M0 ledger and validation exit tests").

The reviewer changed nothing in this repository except adding this file. All break-it experiments ran on a scratch copy.

## Verdict

**All ten exit tests pass and reproduce. M0 is accepted on one condition: fix the tombstone semantics (findings 1 and 2), which are inside M0's scope.** The other findings are either test-strength improvements or design questions for Can that should be answered before M2 builds on the ledger.

The hand-back report is accurate. Every figure the reviewer re-measured matched it.

## What was re-run

| Check | Reviewer's result |
|---|---|
| `./scripts/check` | 60 passed in 32.6 s; Ruff, mypy strict and the import contract clean; branch coverage ledger 96.7%, episode 98.1%, segmenter 98.5%, now enforced |
| Property test at scale | 2,500 passing, 0 failing, on a seed the report did not use (777), in 89 s |
| SHACL timing | Median 3.7 ms, p95 8.9 ms per write (report: 4.3 and 9.7) |
| First-review findings 1 to 9 | All fixed, with tests. Details in section D |
| The five format decisions | All implemented. Details in section D |

## Break-it results

Each change was applied to a scratch copy and the ledger tests re-run.

| Deliberate breakage | Caught? |
|---|---|
| Stale write wins (compare-and-set removed) | Yes |
| Conflict not written | Yes |
| No tombstone on supersede | Yes |
| Tombstone records the new rendering | Yes |
| SHACL validation skipped | Yes |
| Hash ignores the payload | Yes |
| Reader may go backwards | Yes |
| Append-only trigger removed | Yes |
| Version-contiguity trigger removed | Yes |
| Validity interval not closed on replay | Yes |
| Runtime loader reads `hidden.yaml` through `open()` | Yes (was missed in the first review) |
| Cross-reference text dropped | Yes (was missed in the first review) |
| Owner check removed in `_change` | No, but harmless: `_append` repeats the check |
| Supersede and tombstone committed in separate transactions | **No** — finding 4 |

---

## A. Fix these — inside M0's scope

### 1. A tombstone can mark the current rendering as forbidden (high)

`Ledger.is_tombstoned` (`soc/ledger/store.py:194`) answers from every tombstone ever written and never consults what is current. Two reproductions:

- **Scope narrowing.** Ratify rendering `belirteç` for scope `[sec-A, sec-B]`, then supersede with the same rendering for scope `[sec-A]`. The current version is `belirteç` in `sec-A`, and `is_tombstoned("belirteç", "sec-A")` returns `True`.
- **Re-adoption.** Supersede A with B, then B with A. A is current and `is_tombstoned("A", …)` returns `True`.

The first case is exactly the scope-limited revision the experiment's coverage rule now requires in every episode. A gate built on this lookup would reject correct output.

**Required:** a rendering that is current in a scope is never tombstoned in that scope. A tombstone covers only the scope the rendering actually lost. Add both cases as tests, and add the lookup to the property test (finding 3).

### 2. `Revised` can change the rendering without leaving a tombstone (high)

`soc/ledger/store.py:353` writes a tombstone only for `Superseded`. `Revised` accepts any new rendering (`state` `amended` or `ratified`) and writes none. After `Revised` from `old` to `new`, `is_tombstoned("old", …)` is `False` and the tombstone list is empty.

The project's design says a revision tombstones the old rendering so a stale chunk cannot spread it back. As built, which event type the caller picks decides whether that protection exists.

**Required:** make the rule depend on what changed, not on the event name: whenever a version change removes a rendering from a scope, that rendering is tombstoned for that scope. Then state in the README what `Revised` and `Superseded` each mean. If you believe they should differ in another way, put it in `OPEN_QUESTIONS.md` rather than deciding it.

### 3. The property test is too narrow to find finding 1 (medium)

`tests/property/test_ledger_state_machine.py` uses one commitment, one owner, one scope, at most six steps per sequence, and a fresh unique rendering on every change. It asserts replay equality, the chain, versions and the tombstone list, but never calls `is_tombstoned` or `Reader.in_scope`.

Ten thousand sequences of that shape cannot reach a re-adopted rendering, a narrowed scope, two commitments in one scope, or a read at an older version.

**Required:** widen the machine: several commitments and scopes, renderings drawn from a small pool so they repeat, scope changes, an unauthorized actor, chunk builds, and longer sequences. Add invariants on the lookups: a current rendering is never tombstoned in its scope; `in_scope` at version V equals what a replay to V yields; a reader never goes backwards. Re-run the 10,000.

### 4. Supersede-and-tombstone atomicity is not pinned by a test (low)

Committing between the `Superseded` insert and the `Tombstoned` insert passes all 26 ledger tests, including the crash test, whose kill point is before that commit. Add a test that fails if the two events can be committed apart (for example, kill after the first commit point, or assert in the property test that every `Superseded` is immediately followed by its `Tombstoned`).

### 5. One sentence still has holes (low)

`9700-A-s01` reads `We would like to thank , , , , …`: the names live in `<contact fullname="…">` attributes. Either include the names in the reading text or mark the acknowledgements as not for translation, and say which in the README.

---

## B. Design questions — decided by Can on 11 November 2026

Codex raised the first two in `OPEN_QUESTIONS.md`. The reviewer found the next three. Each item keeps the reviewer's original recommendation for the record.

**Can decided B1 to B5 on 11 November 2026 and confirmed this to the reviewer the same day.** The "Decision" paragraph under each item is Can's and is binding for M2. Where a decision differs from the reviewer's recommendation, the decision governs. It goes further in three places:

- **B2:** a conflict is defined per decision subject and use site, not per scope.
- **B3:** a rebuild is accepted against the previous build number (compare-and-set).
- **B5:** brief citations resolve against the brief in force at the time of the write.

None of B1 to B5 is to be built in M0. Keep the current behaviour until M2.

### B1. Who owns a ruling

`Commitment.owner` is one agent ID, and only that agent may change the ruling (`store.py:300`). Any actor may create a commitment, including one owned by someone else at `SERIAL` level, and an actor's role is a string the caller supplies (`role="principal"` is accepted from any agent).

The design says something different: a triad's ruling may be revised by a new triad that cites new evidence, and only the principal owns the brief. An owner that is one agent's ID cannot express "a triad".

**Recommendation:** make the owner an authority class — the principal, or triads — and enforce who may ratify at each consistency level when the triad protocol arrives in M2. Keep the current check until then. Authentication of the caller belongs to the orchestrator.

**Decision.** Give each commitment an authority class scoped to the operation. The principal owns the brief and principal-issued rulings. An authorized triad can revise a triad-owned ruling when it cites new evidence and meets the approval requirements for the ruling's consistency level. The orchestrator authenticates the actor and verifies approvals. The ledger enforces the authority on each write and records the actual approvers. Keep the current agent-ID owner check until the M2 triad protocol is ready.

### B2. Two rulings on one scope raise no conflict

Compare-and-set is per commitment ID. Two commitments with different IDs can both be current for the scope `access token` with different renderings, and `in_scope` returns both. The design's central case — two triads ruling on one scope at once — produces a conflict only if both happen to use the same commitment ID.

**Recommendation:** make the conflict a property of the scope: either derive the commitment's identity from its scope, or reject a ratification whose scope overlaps a different current commitment and write a conflict event. This should be decided before M2.

**Decision.** Keep stable commitment IDs. Within an operation, allow one binding ruling per decision subject at each applicable use site. Two different IDs for the translation of *access token* in the same section conflict. A terminology ruling and a numeric-preservation ruling can both apply to the same sentence. Check for overlap in the same transaction as the write. On collision, record `ConflictRaised` with both IDs and the overlap. The existing ruling stays binding until the conflict is resolved. Use explicit, disjoint scopes for exceptions.

### B3. A repaired chunk cannot be recorded

A second `ChunkBuilt` for the same chunk is rejected as "chunk already built" (`store.py:412`). After a revision, a repaired chunk therefore cannot record the versions it now uses, and its usage record stays on the old version. The final sweep, which compares each chunk's recorded versions with the ledger, would flag it forever.

**Recommendation:** allow a rebuild event that carries a build number, keep the history, and let the projection show the latest build per chunk.

**Decision.** Append another `ChunkBuilt` for the same chunk with the next build number, starting at 1. Record the output reference, ledger snapshot, and commitment versions actually used. Accept the build against the previous build number so competing repairs cannot silently replace each other. Keep every build for audit and use the latest accepted build for current usage and the final sweep.

### B4. `antecedent` and `consequent` mean something else here

The brief fixed `soc:antecedent` and `soc:consequent` as commitment roles: the condition under which a commitment applies, and what it requires. The implementation uses them on events, as links to the old and new version (`store.py:340`), and a commitment has neither.

The ontology file is also thin: 22 lines, with no domain, range, label or comment, and no declaration of the data properties the shapes use (`scope`, `rendering`, `state`, `validFrom`, and others).

**Recommendation:** rename the event links (for example to PROV's `used` and `generated`), reserve the two terms for the commitment, and complete the ontology's declarations before it is frozen. The same vocabulary is meant to serve the Semantic Web project.

**Decision.** Put `soc:antecedent` and `soc:consequent` on each commitment version. The antecedent expresses when the ruling applies, and the consequent expresses what it requires. A revision activity links to the old version with `prov:used` and the new version with `prov:generated`. The new version links back to the old one with `prov:wasRevisionOf`. Complete the vocabulary declarations, including domains, ranges, labels, and comments, and align the JSON-LD context and SHACL shapes before freezing the schema.

### B5. The episode is optional when writing

Without an episode, the ledger checks citations for form only (`store.py:250`). **Recommendation:** make the public episode mandatory in the orchestrator, as Codex suggests, and keep the optional form for unit tests.

**Decision.** Require the validated public episode when the orchestrator opens a writable ledger for a run. Resolve source citations and verify verbatim quotes against that episode. Resolve brief citations against the brief in force at the write, including accepted changes. Keep episode-free construction for isolated ledger tests, where source and brief references receive structural validation.

### B6. Questions that can now be closed

| Question | Recommendation |
|---|---|
| Keep SHACL in the write path | **Yes.** At about 4 ms median and 9 ms at the 95th percentile per write, it is negligible beside a model call. The Pydantic fallback is not needed |
| One-based chunks; events take effect after chunk N | Confirm |
| `soc:` IRI `https://w3id.org/soc-testbed#` | Keep as provisional; it is not registered |
| Hosting and licence | Can's |

### B7. Noted for a later milestone

The hash chain detects any change to an event but not the removal of the last events: after dropping the delete trigger and deleting the tail, `verify()` returns clean. Recording the final hash and event count outside the ledger, in `run.json`, would close that. No action in M0.

---

## E. A change decided by Can on 11 November — add strength

This is new scope, decided after this review was written. It is small, and it is the only part of a larger proposal that you should build.

**Add `soc:strength` to a commitment version.**

- Three values: `must`, `should`, `may`. These are the BCP 14 words; in plain terms, required, advised and nice to have.
- Required on every version. When a caller gives none, the typed record defaults to `must`.
- Declare the property in `ontology.ttl`, add it to the typed record, the JSON-LD nodes and the README.
- Add a shape that rejects any other value, with one valid fixture covering all three values and one invalid fixture.
- Store it only. The gate that acts on it — block on `must`, warn on `should`, never block on `may` — belongs to a later milestone. Do not build that behaviour now.
- Strength is independent of `state` and `consistency`. Do not derive one from another.

**Do not build the rest.** The same proposal describes an "operational profile": count limits on actions, attempt records, a risk class on operations and visibility labels at handover. Can approved documenting it, not building it. It is described in the ontology documentation as proposed and unbuilt.

## C. Still worth knowing

- `ratify` accepts only `state="ratified"`, so proposed and provisional decisions cannot be stored yet. That matches the brief; it becomes M2 work.
- Each ratification event stores about 2.3 KB of JSON-LD. Fine at this scale.
- `REVIEW_M0.md` is untracked. Commit both review files if you want the history to carry them.

## D. First-review items, re-checked

| Item | State |
|---|---|
| 1. Cross-reference text | Fixed. `9700-1.1-s02` reads "Section 3 presents the updated OAuth attacker model." |
| 2. Non-content marked translatable | Fixed. 643 of 915 segments are translatable; none is whitespace-only; none is in the table of contents or boilerplate |
| 3. Plural glossary terms | Fixed. 0 of 47 "access tokens" sentences miss the key |
| 4. Boundary test | Fixed. Reading through `open()` now fails the suite |
| 5. Empty chunk, 6. duplicate decisions | Fixed, with tests |
| 7. `.DS_Store` | Fixed. A package with `.DS_Store` validates |
| 8. Coverage floor | Fixed. Enforced per area by `scripts/check_branch_coverage.py` |
| 9. Abbreviations | Fixed, with a synthetic test; none occurs in RFC 9700 |
| B1 Approvals carry content | Done: `value` on approvals, `revisions` with a value on the brief change |
| B2 Planted errors separate | Done: own records in `hidden.yaml`, absent from the runtime loader |
| B3 Affected holds both sides | Done: `repair` and `apply` lists |
| B4 Decision IDs separate | Done: `D1`…`D5` with `scope_keys` |
| Round trip | Done: exact `text` plus a clean `reading_text` |
