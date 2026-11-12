# Open questions for Can

- Which licence should the project use? The repository is hosted at [scaydogdu/SOC](https://github.com/scaydogdu/SOC).
- Confirm the proposed `soc:` vocabulary IRI `https://w3id.org/soc-testbed#`.
- Decide whether to keep SHACL in the write path after reviewing the measured timings.
- Confirm that chunk positions are one-based and that an event at chunk N happens after that chunk. Coverage therefore counts uses at or before N on the earlier side and uses after N on the later side.
- Confirm whether `Revised` and `Superseded` should differ beyond the event type and allowed replacement states.

The five M2 design decisions approved on 11 November 2026 are recorded in [Section B of the second M0 review](REVIEW_M0_2.md#b-design-questions--decided-by-can-on-11-november-2026).
