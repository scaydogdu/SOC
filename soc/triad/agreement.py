"""Classify draft differences, decide them under the configured rules, and merge."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Literal, Self, get_args

from pydantic import model_validator

from soc.episode import Brief, Sentence
from soc.ledger.models import (
    TRIAD_APPROVAL_COUNTS,
    CommitmentVersion,
    Consistency,
    Record,
    ResolutionBallot,
    ResolutionTrace,
    Text,
)
from soc.memory import ChunkInput, ChunkOutput, Translation
from soc.triad.examination import (
    Difference,
    Draft,
    Examination,
    ReadEvidence,
    Remark,
    Respond,
    Review,
    align_drafts,
    examine_chunk,
)

DecisionType = Literal[
    "provisional", "term", "style", "normative", "brief_gap", "brief_change"
]
type Choice = Literal["first", "second"]
type ResolutionMode = Literal["cross_examination", "open_discussion", "principal_only"]


class AgreementRules(Record):
    rules: dict[DecisionType, Consistency]

    @model_validator(mode="after")
    def check_rules(self) -> Self:
        if self.rules.keys() != set(get_args(DecisionType)):
            raise ValueError("the agreement table must cover every decision type")
        if (
            self.rules["brief_gap"] != "SERIAL"
            or self.rules["brief_change"] != "SERIAL"
        ):
            raise ValueError("brief gaps and changes require the principal")
        if self.rules["provisional"] != "ONE":
            raise ValueError("the provisional decision type requires ONE")
        return self

    def required(self, kind: DecisionType, mode: ResolutionMode) -> Consistency:
        return "SERIAL" if mode == "principal_only" else self.rules[kind]


class Ballot(Record):
    choice: Choice | None


class PrincipalResolution(Record):
    rendering: Text


class TermTarget(Record):
    """One reusable scope key with a verbatim phrase from each draft."""

    scope_key: Text
    first: Text
    second: Text

    def validate_difference(self, examination: Examination) -> None:
        difference = examination.difference
        if (
            self.first == self.second
            or difference.first.count(self.first) != 1
            or difference.second.count(self.second) != 1
        ):
            raise ValueError(
                "term choices must be distinct and occur once in their drafts"
            )

    def merge_principal(self, examination: Examination, rendering: str) -> str:
        return examination.difference.first.replace(self.first, rendering, 1)


class Classification(Record):
    kind: DecisionType
    term: TermTarget | None = None
    question: Text | None = None

    @model_validator(mode="after")
    def check_term(self) -> Self:
        if self.term is not None and self.kind != "term":
            raise ValueError("only a terminology decision may define a term target")
        return self


@dataclass(frozen=True)
class Decision:
    examination: Examination
    kind: DecisionType
    required: Consistency
    consistency: Consistency
    rendering: str
    approvals: tuple[str, ...]
    ballots: tuple[tuple[str, Ballot], ...]
    term: TermTarget | None = None
    term_rendering: str | None = None
    vote_weights: tuple[tuple[str, Fraction], ...] = ()

    def trace(self) -> ResolutionTrace:
        return ResolutionTrace(
            sentence_id=self.examination.difference.sentence_id,
            decision_kind=self.kind,
            required=self.required,
            consistency=self.consistency,
            rendering=self.rendering,
            approvers=list(self.approvals),
            ballots=[
                ResolutionBallot(voter=voter, choice=ballot.choice)
                for voter, ballot in self.ballots
            ],
            vote_weights={voter: str(weight) for voter, weight in self.vote_weights},
            evidence_supporters=sorted(
                {
                    turn.challenge.worker
                    for turn in self.examination.turns
                    if turn.answer is not None
                    and turn.challenge.worker in self.approvals
                }
            )
            if self.vote_weights
            else [],
        )

    @property
    def provisional(self) -> bool:
        return self.consistency == "ONE"


@dataclass(frozen=True)
class TriadResult:
    draft: ChunkOutput
    decisions: tuple[Decision, ...]


type Classify = Callable[[Examination], Awaitable[DecisionType | Classification]]
type Vote = Callable[[str, Examination, DecisionType], Awaitable[Ballot]]
type Settle = Callable[[str, Examination, DecisionType], Awaitable[PrincipalResolution]]
type Discuss = Callable[[str, Examination], Awaitable[str | None]]


async def _discuss(
    examinations: tuple[Examination, ...],
    participants: tuple[str, str, str],
    discuss: Discuss,
) -> tuple[Examination, ...]:
    results = []
    for examination in examinations:
        remarks: list[Remark] = []
        for speaker in participants:
            text = await discuss(
                speaker, Examination(examination.difference, (), tuple(remarks))
            )
            if text is not None and text.strip():
                remarks.append(Remark(speaker, text))
        results.append(Examination(examination.difference, (), tuple(remarks)))
    return tuple(results)


def approved_rendering(
    examination: Examination,
    required: Consistency,
    ballots: Sequence[tuple[str, Ballot]],
    *,
    weights: Mapping[str, Fraction] | None = None,
) -> tuple[str, tuple[str, ...]] | None:
    """Return the unique draft choice that clears the approval threshold."""
    if required == "SERIAL":
        return None
    if weights is not None:
        voters = [voter for voter, _ in ballots]
        if (
            len(voters) != 3
            or len(set(voters)) != 3
            or weights.keys() != set(voters)
            or any(
                not isinstance(weight, Fraction) or not 0 <= weight <= 1
                for weight in weights.values()
            )
        ):
            raise ValueError(
                "weights require three distinct voters and bounded rational values"
            )
        if any(
            weights[voter] == 0 and ballot.choice is not None
            for voter, ballot in ballots
        ):
            raise ValueError("a zero-weight voter must abstain")
    first = tuple(voter for voter, ballot in ballots if ballot.choice == "first")
    second = tuple(voter for voter, ballot in ballots if ballot.choice == "second")
    if (
        len(first) == len(second)
        or max(len(first), len(second)) < TRIAD_APPROVAL_COUNTS[required]
    ):
        return None
    supporters = first if len(first) > len(second) else second
    if weights is not None and sum(weights[voter] for voter in supporters) * 2 <= sum(
        weights.values()
    ):
        return None
    return (
        (examination.difference.first, first)
        if len(first) > len(second)
        else (examination.difference.second, second)
    )


async def _decide(
    examination: Examination,
    kind: DecisionType,
    required: Consistency,
    term: TermTarget | None,
    voters: tuple[str, str, str],
    principal: str,
    vote: Vote,
    settle: Settle,
    weights: Mapping[str, Fraction] | None,
) -> Decision:
    ballots: list[tuple[str, Ballot]] = []
    if term is not None:
        term.validate_difference(examination)
    if required != "SERIAL":
        for voter in voters:
            ballot = (
                Ballot(choice=None)
                if weights is not None and weights[voter] == 0
                else await vote(voter, examination, kind)
            )
            ballots.append((voter, ballot))
        if approved := approved_rendering(
            examination, required, ballots, weights=weights
        ):
            rendering, approvals = approved
            term_rendering = None
            if term is not None:
                term_rendering = (
                    term.first
                    if rendering == examination.difference.first
                    else term.second
                )
            return Decision(
                examination,
                kind,
                required,
                required,
                rendering,
                approvals,
                tuple(ballots),
                term,
                term_rendering,
                tuple(weights.items()) if weights is not None else (),
            )

    answer = await settle(principal, examination, kind)
    return Decision(
        examination,
        kind,
        required,
        "SERIAL",
        answer.rendering
        if term is None
        else term.merge_principal(examination, answer.rendering),
        (principal,),
        tuple(ballots),
        term,
        answer.rendering if term is not None else None,
        tuple(weights.items()) if weights is not None else (),
    )


async def run_triad(
    chunk: ChunkInput,
    first: Draft,
    second: Draft,
    *,
    source: Sequence[Sentence],
    brief: Brief,
    commitments: Mapping[str, CommitmentVersion],
    reviewer: str,
    principal: str,
    agreement: AgreementRules,
    classify: Classify,
    vote: Vote,
    settle: Settle,
    review: Review | None = None,
    respond: Respond | None = None,
    mode: ResolutionMode = "cross_examination",
    discuss: Discuss | None = None,
    read_evidence: ReadEvidence | None = None,
    weights: Mapping[str, Fraction] | None = None,
) -> TriadResult:
    """Resolve each examined difference and assemble a draft in source order."""
    voters = (first.worker, second.worker, reviewer)
    participants = (*voters, principal)
    if len(set(participants)) != 4 or any(not agent.strip() for agent in participants):
        raise ValueError(
            "workers, reviewer and principal must have distinct identities"
        )
    if weights is not None:
        # Validate even when drafts agree or classification routes straight to the principal.
        approved_rendering(
            Examination(Difference("validation", "first", "second"), ()),
            "QUORUM",
            [(voter, Ballot(choice=None)) for voter in voters],
            weights=weights,
        )
    if mode == "cross_examination":
        if review is None or respond is None:
            raise ValueError("cross-examination requires review and response callbacks")
        examinations = await examine_chunk(
            chunk,
            first,
            second,
            source=source,
            brief=brief,
            commitments=commitments,
            review=review,
            respond=respond,
            read_evidence=read_evidence,
        )
    else:
        examinations = tuple(
            Examination(difference, ())
            for difference in align_drafts(chunk, first, second)
        )
        if mode == "open_discussion":
            if discuss is None:
                raise ValueError("open discussion requires a discussion callback")
            examinations = await _discuss(examinations, voters, discuss)
    decisions = []
    renderings = {row.sentence_id: row.text for row in first.output.sentences}
    for examination in examinations:
        classified = await classify(examination)
        kind = classified.kind if isinstance(classified, Classification) else classified
        term = classified.term if isinstance(classified, Classification) else None
        decision = await _decide(
            examination,
            kind,
            agreement.required(kind, mode),
            term,
            voters,
            principal,
            vote,
            settle,
            weights,
        )
        decisions.append(decision)
        renderings[examination.difference.sentence_id] = decision.rendering
    draft = ChunkOutput(
        sentences=[
            Translation(sentence_id=sentence.id, text=renderings[sentence.id])
            for sentence in chunk.sentences
        ]
    )
    return TriadResult(draft, tuple(decisions))
