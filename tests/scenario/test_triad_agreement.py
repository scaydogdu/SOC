import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from soc.episode import load_runtime, read_yaml
from soc.memory import ChunkInput, ChunkOutput, Translation
from soc.triad.agreement import AgreementRules, Ballot, PrincipalResolution, run_triad
from soc.triad.examination import Answer, Challenge, Citation, Draft

CONFIG = Path("config/m2-agreement.yaml")
TOY = Path("data/episodes/toy")


def test_triad_merges_quorum_unanimity_provisional_and_principal_rulings():
    episode = load_runtime(TOY)
    chunk = ChunkInput(1, tuple(episode.source[:7]))
    ids = [sentence.id for sentence in chunk.sentences]
    kinds = dict(
        zip(
            ids,
            ["term", "normative", "brief_gap", "provisional", "style", "normative"],
            strict=False,
        )
    )

    def draft(worker, order):
        return Draft(
            worker,
            ChunkOutput(
                sentences=[
                    Translation(
                        sentence_id=sid,
                        text=f"{worker} {sid}" if sid in kinds else "agreed",
                    )
                    for sid in order
                ]
            ),
        )

    votes = []
    principal_calls = []
    choices = {
        ids[0]: ("first", "second", "second"),
        ids[1]: ("first", "first", "second"),
        ids[3]: (None, "second", None),
        ids[4]: ("first", "second", None),
        ids[5]: ("first", "first", "first"),
    }
    participants = ("left", "right", "reviewer")

    async def review(difference, previous):
        if difference.sentence_id == ids[0] and not previous:
            return Challenge(
                worker="right", question="Which source wording supports the term?"
            )
        return None

    async def respond(difference, challenge):
        assert challenge.worker == "right"
        return Answer(
            rationale="The source names the token.",
            citations=[Citation(reference=f"source:{ids[0]}", quote="access token")],
        )

    async def classify(examination):
        return kinds[examination.difference.sentence_id]

    async def vote(voter, examination, kind):
        sid = examination.difference.sentence_id
        if sid == ids[0]:
            assert examination.turns[0].answer is not None
        votes.append((voter, sid))
        return Ballot(choice=choices[sid][participants.index(voter)])

    async def settle(principal, examination, kind):
        assert principal == "client"
        sid = examination.difference.sentence_id
        principal_calls.append(sid)
        return PrincipalResolution(rendering=f"principal {sid}")

    result = asyncio.run(
        run_triad(
            chunk,
            draft("left", ids),
            draft("right", reversed(ids)),
            source=episode.source,
            brief=episode.brief,
            commitments={},
            reviewer="reviewer",
            principal="client",
            agreement=read_yaml(CONFIG, AgreementRules),
            review=review,
            respond=respond,
            classify=classify,
            vote=vote,
            settle=settle,
        )
    )

    assert [(item.sentence_id, item.text) for item in result.draft.sentences] == [
        (ids[0], f"right {ids[0]}"),
        (ids[1], f"principal {ids[1]}"),
        (ids[2], f"principal {ids[2]}"),
        (ids[3], f"right {ids[3]}"),
        (ids[4], f"principal {ids[4]}"),
        (ids[5], f"left {ids[5]}"),
        (ids[6], "agreed"),
    ]
    assert principal_calls == [ids[1], ids[2], ids[4]]
    assert votes == [(voter, sid) for sid in choices for voter in participants]
    assert [
        (item.required, item.consistency, item.approvals) for item in result.decisions
    ] == [
        ("QUORUM", "QUORUM", ("right", "reviewer")),
        ("ALL", "SERIAL", ("client",)),
        ("SERIAL", "SERIAL", ("client",)),
        ("ONE", "ONE", ("right",)),
        ("QUORUM", "SERIAL", ("client",)),
        ("ALL", "ALL", participants),
    ]
    assert result.decisions[2].ballots == ()
    assert [item.provisional for item in result.decisions] == [
        False,
        False,
        False,
        True,
        False,
        False,
    ]


def test_agreement_table_requires_all_types_and_reserves_brief_for_principal():
    default = read_yaml(CONFIG, AgreementRules)
    incomplete = {
        key: rule for key, rule in default.rules.items() if key != "normative"
    }
    with pytest.raises(ValidationError, match="every decision type"):
        AgreementRules(rules=incomplete)
    with pytest.raises(
        ValidationError, match="brief gaps and changes require the principal"
    ):
        AgreementRules.model_validate(
            {"rules": {**default.rules, "brief_gap": "QUORUM"}}
        )
