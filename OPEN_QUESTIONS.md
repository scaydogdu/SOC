# Open questions for Can

- Where should the repository be hosted, and under what licence? The working repository is local.
- Confirm the proposed `soc:` vocabulary IRI `https://w3id.org/soc-testbed#`.
- Decide whether to keep SHACL in the write path after reviewing the measured timings.
- Confirm that chunk positions are one-based and that an event at chunk N happens after that chunk. Coverage therefore counts uses at or before N on the earlier side and uses after N on the later side.
- Confirm the ownership model for an existing ruling. The implementation uses the durable actor agent ID for both a revision and a later ratification. Caller authentication is outside M0.
- Confirm the episode-to-ledger citation boundary. The ledger validates source quote text and resolves source and brief citations when the caller supplies the public episode. Without the episode, those references receive structural validation. Confirm whether to make the public episode mandatory in the future orchestrator.

- Decide whether conflicts should extend across different commitment IDs with overlapping scope.
- Decide how to record repaired chunks, including build numbering and usage history.
- Confirm the intended commitment roles for `soc:antecedent` and `soc:consequent`, the PROV properties for linking revision events to versions, and the vocabulary declarations required before freezing the ontology.
- Confirm whether `Revised` and `Superseded` should differ beyond the event type and allowed replacement states.
