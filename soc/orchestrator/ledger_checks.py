"""Capture worker decisions and gate a draft against the current ledger."""

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence

from soc.enforce.checker import Checker, Finding, NormativeRendering
from soc.episode import Brief, Clause
from soc.ledger.models import (
    BINDING_STATES,
    Actor,
    ChunkBuilt,
    CommitmentVersion,
    DecisionCaptured,
    DecisionDeclared,
    Event,
    GateFinding,
    GateOutcome,
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

_OUTCOMES: dict[str, GateOutcome] = {
    "must": "block",
    "should": "warning",
    "may": "allowed",
}


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


def _gate_finding(finding: Finding) -> GateFinding:
    outcome: GateOutcome = (
        "unknown" if finding.status == "unknown" else _OUTCOMES[finding.strength]
    )
    return GateFinding(
        rule=finding.rule,
        detail=finding.detail,
        outcome=outcome,
        sentence_id=finding.sentence_id,
        commitment_id=finding.decision_id,
        strength=finding.strength,
        start=None if finding.span is None else finding.span.start,
        end=None if finding.span is None else finding.span.end,
    )


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
) -> Event:
    return gate_chunks(
        ledger,
        actor,
        {chunk_id: (chunk, build_number)},
        checker=checker,
        load_output=load_output,
        normative=normative,
    )[0]


def gate_chunks(
    ledger: Ledger,
    actor: Actor,
    chunks: Mapping[str, tuple[ChunkInput, int | None]],
    *,
    checker: Checker,
    load_output: Callable[[str], ChunkOutput],
    normative: Sequence[NormativeRendering] = (),
    path: RepairPath = "gate",
) -> tuple[Event, ...]:
    """Check built artifacts and log all verdicts against one current snapshot."""
    authority = ledger.authority
    if authority is None:
        raise ValueError("the translation gate requires an operation-scoped ledger")
    operation = authority.operation
    initial_brief = ledger.initial_brief()

    def evaluate(
        chunk_id: str, state: Projection, build: ChunkBuilt
    ) -> list[GateFinding]:
        chunk, _ = chunks[chunk_id]
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
                kind="non_lexical"
                if row.rule is not None and row.rule.kind == "non_lexical"
                else "term",
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
        brief = Brief(
            clauses=[Clause(id=key, text=text) for key, text in clauses.items()]
        )
        return [
            _gate_finding(finding)
            for finding in checker.check(
                chunk.sentences,
                output,
                brief,
                decisions=decisions,
                forbidden=forbidden,
                normative=normative,
            )
        ]

    return ledger.check_builds(
        actor,
        {key: number for key, (_, number) in chunks.items()},
        path=path,
        evaluate=evaluate,
    )


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
