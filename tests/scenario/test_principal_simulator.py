import asyncio
from pathlib import Path

import pytest

from soc.ledger.models import Actor, BriefChanged, OperationAuthority, PrincipalAnswered
from soc.ledger.store import AuthorityError
from soc.orchestrator.ledger import open_run_ledger
from soc.principal.simulator import PrincipalSimulator, UnansweredQuestion

TOY = Path("data/episodes/toy")
AUTHORITY = OperationAuthority(operation="toy", principal="client")
CLIENT = Actor(agent="client", model="simulator", role="principal")


def test_principal_cache_is_shared_and_each_logical_answer_counts_once(tmp_path):
    key = tmp_path / "principal.yaml"
    key.write_text(
        'answers:\n  "Expand initials?": "Expand on first mention."\n'
        '  "How long?": "Keep the example terse."\n'
    )
    cache = tmp_path / "answers.sqlite"
    questions = []

    async def fallback(question):
        questions.append(question)
        return "Use active voice." if question == "which voice?" else None

    async def scenario():
        for arm in ("cross-examination", "principal-only"):
            simulator = PrincipalSimulator(
                key, cache, episode_id="toy", actor=CLIENT, fallback=fallback
            )
            with open_run_ledger(
                TOY, tmp_path / f"{arm}.sqlite", arm, authority=AUTHORITY
            ) as (_, ledger):
                for request in ("rendering-1", "rendering-1", "rendering-2"):
                    assert (
                        await simulator.answer(
                            ledger, request, "  Which   VOICE? ", chunk_id="chunk-1"
                        )
                        == "Use active voice."
                    )
                assert (
                    await simulator.answer(
                        ledger, "style-1", "expand initials?", chunk_id="chunk-1"
                    )
                    == "Expand on first mention."
                )
                with pytest.raises(UnansweredQuestion):
                    await simulator.answer(
                        ledger, "unknown", "Unknown?", chunk_id="chunk-1"
                    )
                with pytest.raises(ValueError, match="was reused"):
                    await simulator.answer(
                        ledger, "style-1", "How long?", chunk_id="chunk-1"
                    )
                answers = [
                    PrincipalAnswered.model_validate(event.payload["data"])
                    for event in ledger._events()
                    if event.type == "PrincipalAnswered"
                ]
                assert [answer.question_id for answer in answers] == [
                    "rendering-1",
                    "rendering-2",
                    "style-1",
                ]
                assert all(
                    answer.answer != "Keep the example terse." for answer in answers
                )
        assert questions == ["which voice?", "unknown?", "unknown?"]

    asyncio.run(scenario())


def test_only_principal_issues_scheduled_brief_change_and_resume_does_not_duplicate_it(
    tmp_path,
):
    key = tmp_path / "principal.yaml"
    key.write_text("answers: {}\n")
    simulator = PrincipalSimulator(
        key, tmp_path / "answers.sqlite", episode_id="toy", actor=CLIENT
    )
    path = tmp_path / "run.sqlite"
    with open_run_ledger(TOY, path, "toy", authority=AUTHORITY) as (episode, ledger):
        change = episode.events.brief_change
        with pytest.raises(ValueError, match="public episode schedule"):
            simulator.issue_brief_change(ledger, change, after_chunk=change.chunk - 1)
        with pytest.raises(AuthorityError, match="operation's principal"):
            ledger.record(
                Actor(agent="worker", model="fake", role="worker"),
                BriefChanged(clauses={"B2": "spoofed"}),
            )
        event = simulator.issue_brief_change(ledger, change, after_chunk=change.chunk)
    with open_run_ledger(TOY, path, "toy", authority=AUTHORITY) as (episode, ledger):
        assert (
            simulator.issue_brief_change(
                ledger, episode.events.brief_change, after_chunk=change.chunk
            )
            == event
        )
        assert ledger.snapshot().brief == {
            clause.id: clause.text for clause in change.clauses
        }
        assert ledger.verify() is None
