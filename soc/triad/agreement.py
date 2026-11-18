"""Classify draft differences, decide them under the configured rules, and merge."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Self, get_args

from pydantic import model_validator

from soc.episode import Brief, Sentence
from soc.ledger.models import (
    TRIAD_APPROVAL_COUNTS,
    CommitmentVersion,
    Consistency,
    Record,
    Text,
)
from soc.memory import ChunkInput, ChunkOutput, Translation
from soc.triad.examination import Draft, Examination, Respond, Review, examine_chunk

DecisionType = Literal[
    "provisional", "term", "style", "normative", "brief_gap", "brief_change"
]
type Choice = Literal["first", "second"]


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


class Ballot(Record):
    choice: Choice | None


class PrincipalResolution(Record):
    rendering: Text


@dataclass(frozen=True)
class Decision:
    examination: Examination
    kind: DecisionType
    required: Consistency
    consistency: Consistency
    rendering: str
    approvals: tuple[str, ...]
    ballots: tuple[tuple[str, Ballot], ...]

    @property
    def provisional(self) -> bool:
        return self.consistency == "ONE"


@dataclass(frozen=True)
class TriadResult:
    draft: ChunkOutput
    decisions: tuple[Decision, ...]


type Classify = Callable[[Examination], Awaitable[DecisionType]]
type Vote = Callable[[str, Examination, DecisionType], Awaitable[Ballot]]
type Settle = Callable[[str, Examination, DecisionType], Awaitable[PrincipalResolution]]


def approved_rendering(
    examination: Examination,
    required: Consistency,
    ballots: Sequence[tuple[str, Ballot]],
) -> tuple[str, tuple[str, ...]] | None:
    """Return the unique draft choice that clears the approval threshold."""
    if required == "SERIAL":
        return None
    first = tuple(voter for voter, ballot in ballots if ballot.choice == "first")
    second = tuple(voter for voter, ballot in ballots if ballot.choice == "second")
    if (
        len(first) == len(second)
        or max(len(first), len(second)) < TRIAD_APPROVAL_COUNTS[required]
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
    voters: tuple[str, str, str],
    principal: str,
    vote: Vote,
    settle: Settle,
) -> Decision:
    ballots: list[tuple[str, Ballot]] = []
    if required != "SERIAL":
        for voter in voters:
            ballots.append((voter, await vote(voter, examination, kind)))
        if approved := approved_rendering(examination, required, ballots):
            rendering, approvals = approved
            return Decision(
                examination,
                kind,
                required,
                required,
                rendering,
                approvals,
                tuple(ballots),
            )

    answer = await settle(principal, examination, kind)
    return Decision(
        examination,
        kind,
        required,
        "SERIAL",
        answer.rendering,
        (principal,),
        tuple(ballots),
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
    review: Review,
    respond: Respond,
    classify: Classify,
    vote: Vote,
    settle: Settle,
) -> TriadResult:
    """Resolve each examined difference and assemble a draft in source order."""
    voters = (first.worker, second.worker, reviewer)
    participants = (*voters, principal)
    if len(set(participants)) != 4 or any(not agent.strip() for agent in participants):
        raise ValueError(
            "workers, reviewer and principal must have distinct identities"
        )
    examinations = await examine_chunk(
        chunk,
        first,
        second,
        source=source,
        brief=brief,
        commitments=commitments,
        review=review,
        respond=respond,
    )
    decisions = []
    renderings = {row.sentence_id: row.text for row in first.output.sentences}
    for examination in examinations:
        kind = await classify(examination)
        decision = await _decide(
            examination, kind, agreement.rules[kind], voters, principal, vote, settle
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
