# Brief: hold chunks with an undecided `must` check

**From:** Claude (reviewer) · **For:** Codex · **Date:** 24 November 2026
**Decided by Can:** 24 November 2026.
**Fits in:** M3, alongside the meaning check. Build it before Pilot B (7 December).

## 1. The problem

`Ledger.check_builds` accepts a build when no finding is `block`. A checker finding with status `unknown` becomes gate outcome `unknown`, so a chunk whose `must` ruling could not be checked is accepted. That breaks the project rule that **"unknown" is never a pass**.

All three `unknown` findings in the 18 November live toy run came from configuration: the toy episode had no approved Turkish wording for MUST. Blocking on `unknown` would stall runs over setup gaps. It would also inflate the SOC arm's false blocks and incompletion, because only SOC has a gate. So the fix is neither block nor pass.

## 2. What to build

### 2.1 Stop configuration-caused unknowns before a run

- **Episode validation and `eval.precheck` refuse an episode** in which any `must` decision has no wording or rule the deterministic checker can evaluate. This covers missing modal wording for BCP 14 key words and a rule kind with no checker.
- **Each error names the decision and the missing piece.**
- **The live launcher runs this check** before any provider call.
- **The toy episode** either gets approved modal wording, written as a placeholder that Can confirms, or is marked as a fixture that the check exempts. Ask Can which. Until Can answers, build the check behind a switch that is on for evaluation episodes.

### 2.2 A new gate outcome: `held`

- **When `held` applies:** a finding with status `unknown` on a `must` ruling.
  - `should` and `may` stay as they are: unknown is recorded with no effect.
- **Drafting continues.** A held build is not stale and is not blocked, and it does not trigger a repair in either arm.
- **Final acceptance waits.** A held build cannot pass final acceptance or assembly until each held finding is resolved, the same pattern as provisional rulings.
- **Resolving a held finding:**
  - before assembly, every held finding goes to the meaning check planned for M3;
  - the result is appended as its own event, with the resolver, the verdict and the evidence;
  - a `violation` verdict turns the finding into a block, and the normal repair path follows.
- **If a run ends with findings still held**, they are published as `unverified`.
- **The RAG arm gets the same treatment.** The checker is shared, so RAG's `unknown` findings on `must` decisions go to the same meaning check at the same point. RAG never repairs because of an unknown, and its unresolved ones are also `unverified`.

### 2.3 Scoring and reporting

- **Never count an `unverified` use site as kept.** The continuity score must treat it as not kept. Report it separately as well.
- **Report per condition and per episode:**
  - the count and rate of `unknown` findings on `must` checks;
  - how many were resolved and how;
  - how many stayed `unverified`.

### 2.4 Pilot B go/no-go

`PROJECT_PLAN.md` section 10 now has a tenth check:

- zero configuration-caused `must` unknowns in evaluation episodes;
- runtime `must` unknowns at most 1% of `must` checks in every condition.

The 1% threshold is a default Can can change. Report both figures from Pilot B.

## 3. Exit tests

1. An evaluation episode with a `must` decision and no checkable wording is refused by validation, precheck and the launcher, naming the decision.
2. A `must` finding with status `unknown` produces gate outcome `held`. The build is neither stale nor blocked, and no repair starts.
3. A chunk with a held finding cannot pass final acceptance. After a `compliant` resolution it passes; after a `violation` it is blocked and repaired.
4. The RAG arm routes its `must` unknowns to the same resolution step, and no repair is triggered by them.
5. A run ending with an unresolved held finding publishes it as `unverified`, and continuity scoring counts it as not kept.
6. The `should` and `may` behaviour is unchanged. Existing gate tests still pass.
7. Replay reproduces held findings and their resolutions byte for byte.
8. `./scripts/check` passes, with branch coverage floors kept.

## 4. Hand back

Add a section to your M3 status file covering:

- the outcome's name and where it is stored;
- how resolution events look;
- the episode-check error messages;
- the new tests;
- the `./scripts/check` output.

Put Can's answer on the toy episode's modal wording in `OPEN_QUESTIONS.md` until it is given.
