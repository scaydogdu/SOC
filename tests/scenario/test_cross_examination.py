import asyncio
from pathlib import Path

import pytest

from soc.episode import load_runtime
from soc.ledger.models import CommitmentVersion
from soc.memory import ChunkInput, ChunkOutput, Translation
from soc.triad.examination import Answer, Challenge, Citation, Draft, examine_chunk
from tests.ledger_examples import RULING

TOY = Path("data/episodes/toy")


def _draft(worker, sentences):
    return Draft(
        worker,
        ChunkOutput(
            sentences=[
                Translation(sentence_id=sentence_id, text=text)
                for sentence_id, text in sentences
            ]
        ),
    )


def test_cross_examination_aligns_sentences_and_caps_questions():
    episode = load_runtime(TOY)
    chunk = ChunkInput(1, tuple(episode.source[:3]))
    first_site, second_site, agreed_site = (row.id for row in chunk.sentences)
    first = _draft(
        "worker-a", [(first_site, "A"), (second_site, "B"), (agreed_site, "C")]
    )
    second = _draft(
        "worker-b",
        [(agreed_site, "C"), (second_site, "Second B"), (first_site, "Second A")],
    )
    asked = []
    supported = Answer(
        rationale="The sources agree.",
        citations=[
            Citation(reference=f"source:{first_site}", quote="access token"),
            Citation(reference="brief:B1", quote=episode.brief.clauses[0].text),
            Citation(reference="commitment:D1", quote=RULING.rendering),
        ],
    )
    answers = iter(
        [
            supported,
            supported.model_copy(
                update={
                    "citations": [
                        *supported.citations,
                        Citation(
                            reference=f"source:{first_site}", quote="invented wording"
                        ),
                    ]
                }
            ),
            Answer(rationale="No citation.", citations=[]),
        ]
    )

    async def review(difference, previous):
        if difference.sentence_id == second_site and previous:
            return None
        worker = "worker-a" if difference.sentence_id == first_site else "worker-b"
        return Challenge(
            worker=worker, question=f"What supports {difference.sentence_id}?"
        )

    async def respond(difference, challenge):
        asked.append((difference.sentence_id, challenge.worker))
        return next(answers)

    binding = CommitmentVersion(
        **RULING.model_dump(), commitment_id="D1", version=1, valid_from=1
    )
    result = asyncio.run(
        examine_chunk(
            chunk,
            first,
            second,
            source=episode.source,
            brief=episode.brief,
            commitments={"D1": binding},
            review=review,
            respond=respond,
        )
    )
    assert asked == [
        (first_site, "worker-a"),
        (second_site, "worker-b"),
        (first_site, "worker-a"),
    ]
    assert [item.difference.sentence_id for item in result] == [first_site, second_site]
    assert result[0].difference.second == "Second A"
    assert [(turn.round_number, turn.answer) for turn in result[0].turns] == [
        (1, supported),
        (2, None),
    ]
    assert [turn.answer for turn in result[1].turns] == [None]


@pytest.mark.parametrize(("state", "closed"), [("provisional", None), ("ratified", 2)])
def test_only_current_binding_commitments_can_support_an_answer(state, closed):
    episode = load_runtime(TOY)
    sentence = episode.source[0]
    version = CommitmentVersion(
        **RULING.model_copy(update={"state": state}).model_dump(),
        commitment_id="D1",
        version=1,
        valid_from=1,
        valid_until=closed,
    )

    async def review(difference, previous):
        return None if previous else Challenge(worker="a", question="Why this wording?")

    async def respond(difference, challenge):
        return Answer(
            rationale="It matches the ruling.",
            citations=[Citation(reference="commitment:D1", quote=version.rendering)],
        )

    result = asyncio.run(
        examine_chunk(
            ChunkInput(1, (sentence,)),
            _draft("a", [(sentence.id, "A")]),
            _draft("b", [(sentence.id, "B")]),
            source=episode.source,
            brief=episode.brief,
            commitments={"D1": version},
            review=review,
            respond=respond,
        )
    )
    assert len(result[0].turns) == 1 and result[0].turns[0].answer is None


def test_malformed_draft_stops_before_cross_examination():
    episode = load_runtime(TOY)
    sentence = episode.source[0]

    async def unexpected(*args):
        pytest.fail("a malformed draft should be rejected before review")

    with pytest.raises(ValueError, match="each chunk sentence once"):
        asyncio.run(
            examine_chunk(
                ChunkInput(1, (sentence,)),
                _draft("a", [(sentence.id, "A"), (sentence.id, "duplicate")]),
                _draft("b", [(sentence.id, "B")]),
                source=episode.source,
                brief=episode.brief,
                commitments={},
                review=unexpected,
                respond=unexpected,
            )
        )
