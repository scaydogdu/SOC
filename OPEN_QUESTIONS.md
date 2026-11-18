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
- Confirm the executor successor policy. The plan defaults to the same family’s small model and rotates the family.
- Confirm the number of Pilot B episodes, the main-run host, and whether to register publicly on OSF.

- Confirm how frozen episodes should supply question aliases or a principal fallback for their private preferences. The simulator currently takes an isolated question-to-answer map and an optional trusted fallback callback.

The five M2 design decisions approved on 11 November 2026 are recorded in [Section B of the second M0 review](REVIEW_M0_2.md#b-design-questions--decided-by-can-on-11-november-2026).
