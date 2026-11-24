import asyncio
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from soc.episode import load_runtime, read_yaml
from soc.ledger.models import Actor, DecisionSubject
from soc.ledger.store import AuthorityError
from soc.memory import ChunkInput, ChunkOutput, Translation
from soc.orchestrator.ledger import open_run_ledger, record_triad_ruling
from soc.triad.agreement import (
    AgreementRules,
    Ballot,
    PrincipalResolution,
    approved_rendering,
    run_triad,
)
from soc.triad.examination import (
    Answer,
    Challenge,
    Citation,
    Difference,
    Draft,
    Examination,
    Turn,
)
from tests.ledger_examples import IDENTITY, RULING
from tests.scenario.test_ruling_authority import (
    POLICY,
    TOY,
    VOTERS,
    _decision,
)

EXAM = Examination(Difference("site", "A", "B"), ())


def test_unweighted_resolution_trace_keeps_legacy_evidence_defaults(tmp_path):
    agreement = read_yaml(Path("config/m2-agreement.yaml"), AgreementRules)
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        episode,
        _,
    ):
        decision = _decision(episode, VOTERS, "B", agreement)
    examination = replace(
        decision.examination,
        turns=(
            Turn(
                1,
                Challenge(worker=VOTERS[1], question="source?"),
                Answer(
                    rationale="source",
                    citations=[Citation(reference="source:site", quote="source")],
                ),
            ),
        ),
    )
    decision = replace(decision, examination=examination)
    assert decision.trace().evidence_supporters == []
    weighted = replace(
        decision, vote_weights=tuple(dict.fromkeys(VOTERS, Fraction(1)).items())
    )
    assert weighted.trace().evidence_supporters == [VOTERS[1]]


def ballots(*choices):
    return tuple(
        (voter, Ballot(choice=choice))
        for voter, choice in zip(VOTERS, choices, strict=True)
    )


def test_weighted_majority_can_veto_but_cannot_replace_distinct_approvals():
    rows = ballots("first", "second", "second")
    assert approved_rendering(EXAM, "QUORUM", rows) == ("B", VOTERS[1:])
    weights = dict(
        zip(VOTERS, (Fraction(1), Fraction(1, 5), Fraction(1, 5)), strict=True)
    )
    assert approved_rendering(EXAM, "QUORUM", rows, weights=weights) is None
    assert (
        approved_rendering(
            EXAM, "QUORUM", ballots("first", None, None), weights=weights
        )
        is None
    )
    assert approved_rendering(
        EXAM, "QUORUM", rows, weights=dict.fromkeys(VOTERS, Fraction(1))
    ) == ("B", VOTERS[1:])


def test_zero_weights_abstentions_ties_and_all_never_manufacture_unanimity():
    equal = dict.fromkeys(VOTERS, Fraction(1))
    assert (
        approved_rendering(
            EXAM, "QUORUM", ballots("first", "second", None), weights=equal
        )
        is None
    )
    assert (
        approved_rendering(
            EXAM, "ALL", ballots("first", "first", "second"), weights=equal
        )
        is None
    )
    assert approved_rendering(
        EXAM, "ALL", ballots("first", "first", "first"), weights=equal
    ) == ("A", VOTERS)
    zero = dict.fromkeys(VOTERS, Fraction(0))
    assert (
        approved_rendering(EXAM, "ALL", ballots(None, None, None), weights=zero) is None
    )
    suspended = {**equal, VOTERS[0]: Fraction(0)}
    with pytest.raises(ValueError, match="zero-weight"):
        approved_rendering(
            EXAM, "ALL", ballots("first", "first", "first"), weights=suspended
        )


@pytest.mark.parametrize(
    "weights",
    [
        {},
        {"intruder": Fraction(1)},
        dict.fromkeys(VOTERS, Fraction(-1)),
        dict.fromkeys(VOTERS, Fraction(2)),
        dict.fromkeys(VOTERS, float("nan")),
    ],
)
def test_weights_require_exact_authenticated_participants_and_bounded_rationals(
    weights,
):
    with pytest.raises(ValueError):
        approved_rendering(
            EXAM, "QUORUM", ballots("first", "first", "second"), weights=weights
        )


def test_weighted_ruling_recomputes_the_result_against_trusted_weights(tmp_path):
    agreement = read_yaml(Path("config/m2-agreement.yaml"), AgreementRules)
    issuer = Actor(agent=VOTERS[2], model="model", role="reviewer")
    identity = IDENTITY.model_copy(
        update={
            "owner": issuer.agent,
            "authority": "triad",
            "subject": DecisionSubject(kind="term", key="access token"),
        }
    )
    ruling = RULING.model_copy(
        update={
            "ratifier": issuer.agent,
            "consistency": "QUORUM",
            "sentence_ids": ["9999-1-s01"],
        }
    )
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        episode,
        ledger,
    ):
        decision = _decision(episode, VOTERS, ruling.rendering, agreement)
        weights = dict.fromkeys(VOTERS, Fraction(1))
        decision = replace(decision, vote_weights=tuple(weights.items()))
        with pytest.raises(AuthorityError, match="weights"):
            record_triad_ruling(
                ledger,
                issuer,
                identity,
                0,
                ruling,
                decision,
                agreement=agreement,
                voters=VOTERS,
                reason="forged weights",
            )
        event = record_triad_ruling(
            ledger,
            issuer,
            identity,
            0,
            ruling,
            decision,
            agreement=agreement,
            voters=VOTERS,
            reason="weighted quorum",
            weights=weights,
        )
        assert event.type == "Ratified"
        assert ledger.verify() is None


@pytest.mark.parametrize(
    "kind,consistency", [("term", "QUORUM"), ("normative", "SERIAL")]
)
def test_suspended_voters_are_recorded_as_abstentions_without_a_model_call(
    kind, consistency
):
    episode = load_runtime(TOY)
    sentence = episode.source[0]
    called = []

    async def review(*_):
        return None

    async def respond(*_):
        pytest.fail("no response is needed")

    async def classify(*_):
        return kind

    async def vote(voter, *_):
        called.append(voter)
        return Ballot(choice="second")

    async def settle(*_):
        return PrincipalResolution(rendering="principal answer")

    def draft(worker, text):
        return Draft(
            worker,
            ChunkOutput(sentences=[Translation(sentence_id=sentence.id, text=text)]),
        )

    result = asyncio.run(
        run_triad(
            ChunkInput(1, (sentence,)),
            draft(VOTERS[0], "A"),
            draft(VOTERS[1], "B"),
            source=episode.source,
            brief=episode.brief,
            commitments={},
            reviewer=VOTERS[2],
            principal="client",
            agreement=read_yaml(Path("config/m2-agreement.yaml"), AgreementRules),
            classify=classify,
            vote=vote,
            settle=settle,
            review=review,
            respond=respond,
            weights={
                VOTERS[0]: Fraction(0),
                VOTERS[1]: Fraction(1),
                VOTERS[2]: Fraction(1),
            },
        )
    )
    (decision,) = result.decisions
    assert called == list(VOTERS[1:])
    assert decision.ballots[0][1].choice is None
    assert decision.consistency == consistency
    assert decision.trace().vote_weights[VOTERS[0]] == "0"
