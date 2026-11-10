# M0 review follow-up

## Delivered

- **4.1 Repository.** Local Git repository, Python 3.12 and uv lockfile, GitHub Actions, offline check script, and the runtime import boundary. The script enforces a 90% branch floor separately for each core area with executable branches.
- **4.2 Episode format.** Approvals and revisions carry values. Decision IDs are separate from scope keys. The validator rejects empty chunks, duplicate approvals and revisions, and affected sites on the wrong side of the brief change or outside the decision's scope. Only `.DS_Store` is ignored when checking the package layout. The toy fixture and hashes reflect the new schema.
- **4.3 Segmenter.** `text` remains the exact section-local text. `reading_text` expands cross-references and normalizes layout whitespace. Boilerplate, table of contents, whitespace-only records, and verbatim blocks are non-translatable. All 53 translatable RFC 9700 sentences containing “access tokens” receive the `access token` key.
- **Runtime separation.** The planting records live in `hidden.yaml` as an independent list, preserving the seven-file layout and keeping them separate from evaluation seeds. The loader succeeds with only the three public files present. A Python audit hook in a subprocess checks which files the loader opens when the full package is present. A future trusted launcher will mount only the public files for translation workers and pass a private planting schedule to a separate controller. The launcher and controller remain outside M0.

The added abbreviation rules are `Sec.`, `et al.`, `U.S.`, and `No.`. Each appears in zero translatable RFC 9700 sentences, and removing each rule individually changed the RFC 9700 segment count by zero. A synthetic regression exercises all four.

## Exit tests

| Exit test | Result |
| --- | --- |
| 1. Ledger state machine, including 10,000 sequences | Pending ledger |
| 2. Eight writers, 1,000 attempts | Pending ledger |
| 3. Mid-transaction writer kill | Pending ledger |
| 4. Replay equality | Pending ledger |
| 5. Direct SQL append-only enforcement | Pending ledger |
| 6. SHACL acceptance and rejection fixtures | Pending ontology and shapes |
| 7. RFC 9700 round trip and stable IDs | Passed. A separate assertion checks the published reading of a known cross-reference |
| 8. Episode validation | Passed. The toy loads and the mutated copies fail their intended validation rules |
| 9. Import and runtime boundaries | Passed. One import contract kept, none broken. Public-only and file-open tests passed |
| 10. Hygiene and budget | Ruff and strict mypy passed. Offline suite took about 2 seconds end to end. Episode branch coverage was 51/52 (98.1%), and segmenter coverage was 65/66 (98.5%). Ledger coverage remains pending |

Pytest reported **34 passed**. SHACL median and p95 remain pending until validation runs inside ledger writes.

## Implementation choices and open decisions

The [README](README.md) documents the episode field names and the three approval kinds. The provisional vocabulary IRI is `https://w3id.org/soc-testbed#`. The after-chunk convention is still provisional. See [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md) for Can's remaining decisions. Sections 4.4 and 4.5 and the final M0 hand-back remain outstanding.
