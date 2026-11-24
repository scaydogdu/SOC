# Open questions for Can

- Which licence should the project use? The repository is hosted at [scaydogdu/SOC](https://github.com/scaydogdu/SOC).
- Confirm the proposed `soc:` vocabulary IRI `https://w3id.org/soc-testbed#`.
- Decide whether to keep SHACL in the write path after reviewing the measured timings.
- Confirm that chunk positions are one-based and that an event at chunk N happens after that chunk. Coverage currently counts uses at or before N on the earlier side and uses after N on the later side. The ledger defaults to `chunk_id_base=1` and pins the selected ID base for each bound episode.
- Confirm whether `Revised` and `Superseded` should differ beyond the event type and allowed replacement states.
- Decide whether to keep Inspect AI's model layer after reviewing the [M1 trial](M1_STATUS.md). Thin clients are the working path.
- Decide whether an episode revision should be able to narrow or widen a decision's scope keys. M1 treats a scope-limited revision as a value change within the existing scope, as specified in the M1 brief.
- Approve the toy episode's Turkish renderings for BCP 14 keywords. The public brief describes preserving normative force, and D4 records an interpretation. The checker needs the actual approved modal wording tied to a brief clause and reports `unknown` when it is missing.
- Resolve the Gemini 4 Argon rate limit and Gemini 3.8 Flash server errors, then verify both effort levels for each model. The completed preflight and live toy are recorded in the [M1 exit checklist](M1_STATUS.md#m1-exit-checklist).

- Confirm the shared maximum of two repair rounds per chunk and whether to report first-pass violations.
- Decide whether `ALL` should cover only normative force, polarity and numbers. The M2 agreement table currently requires `ALL` for normative meaning.
- Confirm the Align standing analysis and the interpretation of announced rules as possible prompt compliance.
- M3 standing policy: confirm the recent-check window, minimum evidence, relative importance of runtime check kinds, sanction thresholds, lighter-vote multiplier and probation recovery rule. Initial configurable defaults are in `config/m3-cooperation.yaml`; the runners do not yet enforce them.
- M3 weighted approval: specify how standing weights affect QUORUM while preserving authenticated approvers and ALL normative unanimity. No weighted threshold is implemented yet.
- M3 analysis: the exit test says label swapping preserves every p-value, but the Form non-inferiority test is one-sided with a fixed margin. Confirm that invariance applies to two-sided paired tests, while non-inferiority is tested with its direction/margin transformed explicitly.
- Confirm the executor successor policy. The plan defaults to the same family’s small model and rotates the family.
- Confirm the number of Pilot B episodes, the main-run host, and whether to register publicly on OSF.

- Confirm how frozen episodes should supply question aliases or a principal fallback for their private preferences. The simulator currently takes an isolated question-to-answer map and an optional trusted fallback callback.

- Reconcile the five open Gemini reservations in the existing campaign against the Google billing console. The operator records confirmed amounts before the roster re-probe.
- Select the final shared Form/Align effort after Pilot A. The prepared M2 smoke conditions retain low effort for the two other modes.

- The continuation approval in `M2_STATUS.md` covers the four prepared M2 conditions, provisional low effort for the two other modes, external toy principal wording and proposed USD 15 incremental budget. Operator reconciliation and passing provider probes remain required before live launch; the existing cap and reservations are unchanged.

The five M2 design decisions approved on 11 November 2026 are recorded in [Section B of the second M0 review](REVIEW_M0_2.md#b-design-questions--decided-by-can-on-11-november-2026).
