"""Capture worker decisions and gate a draft against the current ledger."""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence

from soc.enforce.checker import Checker, NormativeRendering
from soc.enforce.meaning import CheckAssessment, apply_resolution, assess, gate_finding
from soc.episode import Clause
from soc.ledger.models import (
    BINDING_STATES,
    Actor,
    ChunkBuilt,
    CommitmentVersion,
    DecisionCaptured,
    DecisionDeclared,
    Event,
    GateDecision,
    GateFinding,
    RepairPath,
    Tombstone,
    VersionRef,
)
from soc.ledger.store import Ledger, Projection
from soc.memory import (
    ChunkInput,
    ChunkOutput,
    Decision,
    DecisionRef,
    ForbiddenRendering,
)
from soc.triad.examination import Draft

_gate_finding = gate_finding


def _sites(
    chunk: ChunkInput,
    scope: Iterable[str],
    sentence_ids: list[str] | None,
) -> frozenset[str]:
    scope_keys = set(scope)
    allowed = None if sentence_ids is None else set(sentence_ids)
    return frozenset(
        sentence.id
        for sentence in chunk.sentences
        if sentence.translatable
        and scope_keys.intersection(sentence.scope_keys)
        and (allowed is None or sentence.id in allowed)
    )


def _binding(state: Projection, operation: str) -> list[CommitmentVersion]:
    return [
        version
        for key, version in state.current.items()
        if state.identities[key].operation == operation
        and version.state in BINDING_STATES
    ]


def _forbidden(
    state: Projection,
    operation: str,
    chunk: ChunkInput,
    binding: Sequence[CommitmentVersion],
) -> Iterable[tuple[Tombstone, frozenset[str]]]:
    for row in state.tombstones:
        identity = state.identities[row.commitment_id]
        if identity.operation != operation:
            continue
        sites = _sites(chunk, row.scope, row.sentence_ids)
        for current in binding:
            if current.rendering == row.rendering and (
                identity.subject is None
                or state.identities[current.commitment_id].subject == identity.subject
            ):
                sites -= _sites(chunk, current.scope, current.sentence_ids)
        if sites:
            yield row, sites


def gate_chunk(
    ledger: Ledger,
    actor: Actor,
    chunk_id: str,
    chunk: ChunkInput,
    *,
    build_number: int,
    checker: Checker,
    load_output: Callable[[str], ChunkOutput],
    normative: Sequence[NormativeRendering] = (),
    observe: Callable[[CheckAssessment], None] | None = None,
) -> Event:
    return gate_chunks(
        ledger,
        actor,
        {chunk_id: (chunk, build_number)},
        checker=checker,
        load_output=load_output,
        normative=normative,
        observe=observe,
    )[0]


def gate_chunks(
    ledger: Ledger,
    actor: Actor,
    chunks: Mapping[str, tuple[ChunkInput, int | None]],
    *,
    checker: Checker,
    load_output: Callable[[str], ChunkOutput],
    normative: Sequence[NormativeRendering] = (),
    observe: Callable[[CheckAssessment], None] | None = None,
    path: RepairPath = "gate",
) -> tuple[Event, ...]:
    """Check built artifacts and log all verdicts against one current snapshot."""
    return ledger.check_builds(
        actor,
        {key: number for key, (_, number) in chunks.items()},
        path=path,
        evaluate=_artifact_checks(
            ledger,
            {key: chunk for key, (chunk, _) in chunks.items()},
            checker,
            load_output,
            normative,
            observe,
        ),
    )


def score_chunks(
    ledger: Ledger,
    chunks: Mapping[str, ChunkInput],
    *,
    checker: Checker,
    load_output: Callable[[str], ChunkOutput],
    normative: Sequence[NormativeRendering] = (),
    observe: Callable[[CheckAssessment], None] | None = None,
) -> dict[str, GateDecision]:
    """Inspect the current builds of a partial run against the active brief."""
    return ledger.inspect_builds(
        dict.fromkeys(chunks),
        evaluate=_artifact_checks(
            ledger, chunks, checker, load_output, normative, observe
        ),
    )


def _artifact_checks(
    ledger: Ledger,
    chunks: Mapping[str, ChunkInput],
    checker: Checker,
    load_output: Callable[[str], ChunkOutput],
    normative: Sequence[NormativeRendering],
    observe: Callable[[CheckAssessment], None] | None = None,
) -> Callable[[str, Projection, ChunkBuilt], list[GateFinding]]:
    authority = ledger.authority
    if authority is None:
        raise ValueError("the translation gate requires an operation-scoped ledger")
    operation = authority.operation
    initial_brief = ledger.initial_brief()

    def evaluate(
        chunk_id: str, state: Projection, build: ChunkBuilt
    ) -> list[GateFinding]:
        chunk = chunks[chunk_id]
        try:
            if build.output_ref is None:
                raise ValueError("missing output reference")
            output = load_output(build.output_ref)
        except (OSError, LookupError, ValueError):
            return [
                GateFinding(
                    rule="artifact",
                    detail="Chunk output could not be loaded",
                    outcome="block",
                )
            ]

        binding = _binding(state, operation)
        decisions = [
            Decision(
                ref=DecisionRef(row.commitment_id, row.version),
                value=row.rendering,
                scope_keys=frozenset(row.scope),
                kind=row.rule.kind if row.rule is not None else "term",
                strength=row.strength,
                sentence_ids=_sites(chunk, row.scope, row.sentence_ids),
            )
            for row in binding
        ]
        forbidden = [
            ForbiddenRendering(
                row.rendering,
                frozenset(row.scope),
                row.commitment_id,
                state.current[row.commitment_id].strength,
                sites,
            )
            for row, sites in _forbidden(state, operation, chunk, binding)
        ]
        clauses = {clause.id: clause.text for clause in initial_brief.clauses}
        clauses.update(state.brief)
        brief = initial_brief.model_copy(
            update={
                "clauses": [Clause(id=key, text=text) for key, text in clauses.items()]
            }
        )
        findings, assessment = assess(
            checker,
            chunk_id,
            build.build_number,
            chunk.sentences,
            output,
            brief,
            decisions=decisions,
            forbidden=forbidden,
            normative=normative,
        )
        if observe is not None:
            observe(assessment)
        checks = {row.check_id: row for row in assessment.held}
        return [
            apply_resolution(
                finding,
                checks[finding.check_id],
                state.held_resolutions.get(finding.check_id),
            )
            if finding.check_id in checks
            else finding
            for finding in findings
        ]

    return evaluate


def capture_worker_draft(
    ledger: Ledger,
    actor: Actor,
    chunk_id: str,
    chunk: ChunkInput,
    draft: Draft,
    *,
    checker: Checker,
    at: int | None = None,
) -> tuple[Event, ...]:
    """Record manifest entries and observable undeclared uses of ledger renderings."""
    authority = ledger.authority
    if authority is None or actor.agent != draft.worker:
        raise ValueError(
            "capture requires an operation-scoped ledger and the draft author"
        )
    operation = authority.operation

    def observe(state: Projection) -> list[DecisionDeclared | DecisionCaptured]:
        counts = Counter(row.sentence_id for row in draft.output.sentences)
        texts = {
            row.sentence_id: row.text
            for row in draft.output.sentences
            if counts[row.sentence_id] == 1
        }
        binding = _binding(state, operation)
        current = [
            *binding,
            *(
                row
                for key, row in state.current.items()
                if row.state == "provisional"
                and state.identities[key].operation == operation
            ),
        ]
        renderings = [
            (
                row.commitment_id,
                row.version,
                row.rendering,
                _sites(chunk, row.scope, row.sentence_ids),
            )
            for row in current
        ]
        renderings.extend(
            (row.commitment_id, row.version, row.rendering, sites)
            for row, sites in _forbidden(state, operation, chunk, binding)
        )
        observed = {}
        for commitment_id, version, rendering, sites in renderings:
            for sentence_id in sorted(sites & texts.keys()):
                text = texts[sentence_id]
                spans = checker.find_rendering(rendering, text)
                if spans:
                    span = spans[0]
                    observed[commitment_id, version, sentence_id] = DecisionCaptured(
                        ref=VersionRef(commitment_id=commitment_id, version=version),
                        sentence_id=sentence_id,
                        excerpt=text[span.start : span.end],
                        start=span.start,
                        end=span.end,
                    )
        declared = {
            (row.ref.commitment_id, row.ref.version, row.sentence_id): row
            for row in draft.manifest
        }
        return [
            *(
                DecisionDeclared(
                    ref=row.ref, sentence_id=row.sentence_id, verified=key in observed
                )
                for key, row in declared.items()
            ),
            *(row for key, row in observed.items() if key not in declared),
        ]

    return ledger.capture_draft(actor, chunk_id, observe, at=at)
