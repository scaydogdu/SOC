"""Align draft sentences and check the evidence offered during cross-examination."""

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from soc.episode import Brief, Sentence
from soc.ledger.models import (
    BINDING_STATES,
    CommitmentVersion,
    DecisionUse,
    Record,
    Text,
)
from soc.memory import ChunkInput, ChunkOutput


@dataclass(frozen=True)
class Draft:
    worker: str
    output: ChunkOutput
    manifest: tuple[DecisionUse, ...] = ()


@dataclass(frozen=True)
class Difference:
    sentence_id: str
    first: str
    second: str


class Challenge(Record):
    worker: Text
    question: Text


class Citation(Record):
    reference: Text
    quote: Text


class Answer(Record):
    rationale: Text
    citations: list[Citation]


@dataclass(frozen=True)
class Turn:
    round_number: int
    challenge: Challenge
    answer: Answer | None


@dataclass(frozen=True)
class Examination:
    difference: Difference
    turns: tuple[Turn, ...]


type Review = Callable[[Difference, tuple[Turn, ...]], Awaitable[Challenge | None]]
type Respond = Callable[[Difference, Challenge], Awaitable[Answer | None]]


def _aligned(draft: Draft, expected: set[str]) -> dict[str, str]:
    sentences = {
        sentence.sentence_id: sentence.text for sentence in draft.output.sentences
    }
    if sentences.keys() != expected or len(sentences) != len(draft.output.sentences):
        raise ValueError(
            f"draft by {draft.worker} must return each chunk sentence once"
        )
    return sentences


async def examine_chunk(
    chunk: ChunkInput,
    first: Draft,
    second: Draft,
    *,
    source: Sequence[Sentence],
    brief: Brief,
    commitments: Mapping[str, CommitmentVersion],
    review: Review,
    respond: Respond,
) -> tuple[Examination, ...]:
    """Ask one question per active difference in each of up to two rounds."""
    if first.worker == second.worker or not all(
        worker.strip() for worker in (first.worker, second.worker)
    ):
        raise ValueError("cross-examination requires two distinct draft workers")
    sentence_ids = [sentence.id for sentence in chunk.sentences]
    expected = set(sentence_ids)
    left, right = _aligned(first, expected), _aligned(second, expected)
    differences = [
        Difference(sid, left[sid], right[sid])
        for sid in sentence_ids
        if left[sid] != right[sid]
    ]
    evidence = {f"source:{row.id}": row.text for row in source}
    evidence.update({f"brief:{clause.id}": clause.text for clause in brief.clauses})
    evidence.update(
        {
            f"commitment:{key}": version.rendering
            for key, version in commitments.items()
            if version.state in BINDING_STATES and version.valid_until is None
        }
    )

    turns: dict[str, list[Turn]] = {item.sentence_id: [] for item in differences}
    active = differences
    for round_number in (1, 2):
        next_round = []
        for difference in active:
            history = turns[difference.sentence_id]
            challenge = await review(difference, tuple(history))
            if challenge is None:
                continue
            if challenge.worker not in {first.worker, second.worker}:
                raise ValueError("challenge must name one of the draft workers")
            answer = await respond(difference, challenge)
            if answer is not None and (
                not answer.citations
                or any(
                    cite.quote not in evidence.get(cite.reference, "")
                    for cite in answer.citations
                )
            ):
                answer = None
            history.append(Turn(round_number, challenge, answer))
            next_round.append(difference)
        active = next_round
    return tuple(
        Examination(difference, tuple(turns[difference.sentence_id]))
        for difference in differences
    )
