# M0 hand-back

## Delivered

| Brief section | Implementation |
| --- | --- |
| 4.1 Repository | Python 3.12, uv lockfile, GitHub Actions, offline suite, strict typing, import contract, and a per-area 90% branch gate |
| 4.2 Episode package | Typed public and evaluation loaders, validator CLI, frozen toy episode, and rule-specific broken copies. Decisions carry values and separate scope keys. Affected sets distinguish repair from later application. The private planting schedule is independent of evaluation seeds |
| 4.3 RFC segmenter | Checked-in RFC 9700 XML with notices, deterministic IDs, exact section round trip, literal artwork and code, clean reading text with cross-references, and capitalized BCP 14, number, and glossary scope keys |
| 4.4 Ledger | SQLite WAL with database append-only and contiguous-version rules, per-write CAS, conflict events, atomic tombstones, owner and principal checks, replayable projections, monotonic readers, and a hash-chain verifier |
| 4.5 Ontology and validation | Hand-written PROV-O vocabulary, local JSON-LD context, typed payloads, SHACL shapes, and 14 graph fixtures. All M0 writes run SHACL inside the transaction |

The review's episode and segmenter regressions remain covered. All 53 translatable RFC 9700 sentences containing “access tokens” receive the `access token` key. The four added abbreviations, `Sec.`, `et al.`, `U.S.`, and `No.`, affect zero RFC 9700 sentences individually. A synthetic test exercises all four.

## Exit tests

| Exit test | Observed result |
| --- | --- |
| 1. State machine | 10,000 passing sequences, 0 failures, across four seeded runs of 2,500 on the final implementation. The stale-write mutation was caught in a scratch copy |
| 2. Concurrent CAS | 8 writer processes, 1,000 attempts, 125 ratifications, 875 `ConflictRaised` events, and 1,000 distinct event IDs. The chain verified |
| 3. Crash | SIGKILL after the supersession insert and before the tombstone. Reopen recovered the last committed snapshot and intact chain. Retry used the next sequence and committed both events |
| 4. Replay | Full replay equaled the live projection throughout the generated sequences and after the process test |
| 5. Append-only | Direct SQL UPDATE and DELETE failed. Duplicate and skipped versions failed, including through INSERT OR REPLACE. Deliberate changes to event content and JSON whitespace were detected |
| 6. Shapes | 4 valid graph fixtures accepted and 10 invalid fixtures rejected. Each invalid graph named only the expected shape. The executor rebound test preserved all commitment validity intervals |
| 7. RFC 9700 | Every section round-tripped. Two runs produced identical IDs and records. A separate assertion checked the published reading of a known cross-reference |
| 8. Episodes | Toy package accepted. Broken copies failed on their intended rules |
| 9. Boundary | 1 import contract kept, 0 broken. The runtime loader ran with the three public files and the audit test confirmed it opened only those filenames in a full package |
| 10. Hygiene | `60 passed in 27.59s` in pytest and 29.29 seconds end to end. Ruff and strict mypy passed. Branch coverage: ledger 118/122 (96.7%), episode 51/52 (98.1%), segmenter 65/66 (98.5%) |

Extended Hypothesis output for the final implementation:

| Seed | Passing | Failing | Discarded by Hypothesis | Runtime |
| --- | ---: | ---: | ---: | ---: |
| 401 | 2,500 | 0 | 540 | 124.98 s |
| 402 | 2,500 | 0 | 512 | 125.32 s |
| 403 | 2,500 | 0 | 535 | 124.00 s |
| 404 | 2,500 | 0 | 558 | 125.29 s |

Toy validation used one warm-up run followed by ten fresh toy runs with 13 writes each. Measured JSON-LD parsing and SHACL together over 130 writes. Median **4.308 ms**, p95 **9.732 ms**.

## Implementation choices

- **CI** uses GitHub Actions. Test setup uses the uv lockfile, and execution uses offline uv with pytest sockets disabled.
- **Episode format** field names are in the [README](README.md). Approval kinds are `term`, `non_lexical`, and `scope_limited`. Approvals and changes use one-based, after-chunk timing provisionally.
- **Ledger schema and API** use strict SQLite rows, append-only triggers, a contiguous global sequence, and a contiguous per-commitment version. The global sequence includes conflicts and tombstones. Only ruling events advance the commitment version. The three write calls are `ratify`, `transition`, and `record`.
- **Projection storage** is in memory and refreshes from persisted events. Prior validity intervals close in replay at the replacing event's sequence. Executor epoch changes leave commitment intervals intact.
- **Vocabulary** uses the provisional `soc:` IRI `https://w3id.org/soc-testbed#`. SHACL remains in the write path.
- **Runtime data separation** keeps the independent planting records in `hidden.yaml`, maintaining the seven-file package. The future trusted launcher should mount only the public files for prompt workers and pass the planting schedule directly to a separate controller.

The later event types have payload models only. Provider calls, orchestration, prompt builders, and evaluation remain outside M0. The ledger verifies external source and brief references when supplied the public episode. Standalone callers receive structural validation for those references. Authority uses the supplied actor identities and roles, with caller authentication left to integration.

Can's remaining decisions are recorded in [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md).

## Second review follow-up

The ledger now records the scopes each version change removes from the previous rendering. The tombstone append shares the version change's transaction. A rendering still used by any current commitment in a scope remains allowed there. Historical reads show the validity interval visible at the requested ledger version.

The widened state machine models three commitments, overlapping scopes, recurring renderings, unauthorized changes, stale writes, and chunk builds. It checks every historical reader result against replay to that version and checks the tombstone lookup against the model. A separate test injects a failed tombstone append and confirms that the version change is invisible to another connection and rolls back. The test failed when a separate commit was deliberately inserted on a scratch copy.

RFC 9700 acknowledgements now include the inline contact names in reading text. The section round trip still passes.

The offline suite reported **65 passed in 31.72 seconds**, with **33.64 seconds** elapsed for the full check script. Branch coverage was **96.8%** for the ledger, **98.1%** for episodes, and **98.5%** for the segmenter. Ruff, strict mypy, and the import contract passed. The second review's design questions remain in [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md).

The expanded Hypothesis run completed with **10,000 passing sequences and zero failures**. Seeds 801 through 810 each produced 1,000 passing sequences, with a longest run of 108.67 seconds. Reproduce each seed with the documented command and `SOC_LEDGER_EXAMPLES=1000`. The updated toy workload measured **4.100 ms median** and **9.810 ms p95** for validation per write over 130 writes.

## 11 November acceptance checks and strength

Findings A1–A5 were implemented in `3ea735e`. This follow-up adds Hypothesis invariants after each state-machine step. A persistent reader checks the current scope results and the model checks every rendering and scope against the tombstone lookup. The test still compares every historical version with prefix replay and checks that a reader rejects a backwards read.

Strength is a lowercase string on the typed ruling and commitment version. The typed default is `must` when omitted. The existing JSON-LD context expands the property to `soc:strength`. The ontology declares the property with domain `soc:CommitmentVersion` and range `xsd:string`. `StrengthShape` requires exactly one of `must`, `should`, and `may`. Stored values are independent of state and consistency. Reopening a pre-strength ledger resolves the missing field to `must` in the typed projection and retains its original event and hash chain.

| Focused test | Result |
| --- | --- |
| Scope narrowing with the same rendering | PASSED. Only the lost scope was tombstoned |
| Revision and re-adoption | PASSED. The old rendering was tombstoned after revision and allowed again after re-adoption |
| Overlapping current commitments | PASSED. A second current ruling kept the shared rendering allowed |
| Supersession and tombstone atomicity | PASSED. An injected failure rolled back both writes, and a second connection saw only the earlier ratification |
| RFC 9700 section round trip | PASSED |
| RFC 9700 acknowledgements | PASSED. Contact names and the full sentence were present in the reading text |
| Strength default, JSON and JSON-LD persistence, replay | PASSED. All three values round-tripped with unchanged state and consistency |
| Typed validation | PASSED for rejection of `MUST`, `required`, and explicit null |
| SHACL fixtures and cardinality | PASSED for the three valid values and for rejection of an unknown value, zero values, and two values |

The focused run reported `14 passed, 29 deselected in 0.69s`. The complete ledger unit, RFC unit, and ledger schema run reported `43 passed in 1.16s`. The offline suite reported `73 passed in 31.41s`, with 33.17 seconds elapsed for `./scripts/check`. Ruff, strict mypy, and the import contract passed. Branch coverage was 96.8% for the ledger, 98.1% for episodes, and 98.5% for the segmenter. A compatibility check used a SQLite ledger created by `3ea735e`, reopened it with a typed `must`, appended a version with `may`, and verified the original event and the hash chain.

Deferred by scope are the Section B design decisions, strength-based gate behavior, the operational profile, and M1 or later work.

The final Hypothesis run passed **10,000 sequences with zero failures**. Each run stopped at `settings.max_examples=1000`. Reproduce using the seeded command in the README with `SOC_LEDGER_EXAMPLES=1000` and seeds 901–910.

| Seed | Passing | Failing | Discarded | Runtime |
| --- | ---: | ---: | ---: | ---: |
| 901 | 1,000 | 0 | 128 | 104.11 s |
| 902 | 1,000 | 0 | 133 | 98.88 s |
| 903 | 1,000 | 0 | 144 | 102.71 s |
| 904 | 1,000 | 0 | 149 | 101.49 s |
| 905 | 1,000 | 0 | 149 | 102.46 s |
| 906 | 1,000 | 0 | 119 | 92.58 s |
| 907 | 1,000 | 0 | 131 | 99.83 s |
| 908 | 1,000 | 0 | 122 | 94.46 s |
| 909 | 1,000 | 0 | 154 | 97.55 s |
| 910 | 1,000 | 0 | 141 | 101.80 s |

The toy benchmark measured 130 writes after one warm-up. SHACL including JSON-LD parsing took **3.910 ms median** and **9.681 ms p95** per write.
