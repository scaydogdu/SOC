import asyncio
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from soc.episode import read_yaml
from soc.ledger.models import (
    Actor,
    BriefChanged,
    DecisionSubject,
    OperationAuthority,
    RulingApproval,
    RulingBallot,
    VersionChange,
    VoteCast,
)
from soc.ledger.store import AuthorityError
from soc.memory import ChunkInput, ChunkOutput, Translation
from soc.orchestrator.ledger import open_run_ledger, record_triad_ruling
from soc.triad.agreement import AgreementRules, Ballot, PrincipalResolution, run_triad
from soc.triad.examination import Draft
from tests.ledger_examples import IDENTITY, RULING

TOY = Path("data/episodes/toy")
CONFIG = Path("config/m2-agreement.yaml")
POLICY = OperationAuthority(operation="toy", principal="client")
CLIENT = Actor(agent="client", model="human", role="principal")
VOTERS = ("left", "right", "reviewer")
NEXT_VOTERS = ("next-left", "next-right", "next-reviewer")


def _decision(episode, voters, rendering, agreement, *, kind="term"):
    sentence = episode.source[0]

    def draft(worker, text):
        return Draft(
            worker,
            ChunkOutput(sentences=[Translation(sentence_id=sentence.id, text=text)]),
        )

    async def review(*_args):
        return None

    async def respond(*_args):
        pytest.fail("there was no challenge")

    async def classify(_examination):
        return kind

    async def vote(voter, *_args):
        return Ballot(choice="second" if voter != voters[0] else "first")

    async def settle(principal, *_args):
        assert principal == CLIENT.agent
        return PrincipalResolution(rendering=rendering)

    result = asyncio.run(
        run_triad(
            ChunkInput(1, (sentence,)),
            draft(voters[0], "alternative"),
            draft(voters[1], rendering),
            source=episode.source,
            brief=episode.brief,
            commitments={},
            reviewer=voters[2],
            principal=CLIENT.agent,
            agreement=agreement,
            review=review,
            respond=respond,
            classify=classify,
            vote=vote,
            settle=settle,
        )
    )
    return result.decisions[0]


def test_new_triad_can_revise_using_the_clause_now_in_force(tmp_path):
    path = tmp_path / "triad.sqlite"
    agreement = read_yaml(CONFIG, AgreementRules)
    issuer = Actor(agent=VOTERS[2], model="model-a", role="reviewer")
    successor = Actor(agent=NEXT_VOTERS[2], model="model-b", role="reviewer")
    identity = IDENTITY.model_copy(
        update={
            "owner": issuer.agent,
            "authority": "triad",
            "subject": DecisionSubject(kind="term", key="access token"),
        }
    )
    initial = RULING.model_copy(
        update={
            "ratifier": issuer.agent,
            "consistency": "QUORUM",
            "sentence_ids": ["9999-1-s01"],
        }
    )
    revision = initial.model_copy(
        update={
            "ratifier": successor.agent,
            "rendering": "erişim belirteci",
            "state": "amended",
        }
    )
    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (episode, ledger):
        duplicate = RulingApproval(
            operation="toy", triad=list(VOTERS), approvers=[VOTERS[2], VOTERS[2]]
        )
        with pytest.raises(AuthorityError, match="required distinct approvals"):
            ledger.ratify(
                issuer, identity, 0, initial, "duplicate approver", approval=duplicate
            )
        unverified = RulingApproval(
            operation="toy", triad=list(VOTERS), approvers=list(VOTERS[1:])
        )
        with pytest.raises(AuthorityError, match="must match the recorded ballots"):
            ledger.ratify(
                issuer, identity, 0, initial, "missing ballots", approval=unverified
            )
        wrong_votes = tuple(
            RulingBallot(
                voter=voter, choice=initial.rendering if voter != VOTERS[1] else None
            )
            for voter in VOTERS
        )
        with pytest.raises(AuthorityError, match="must match the recorded ballots"):
            ledger.ratify(
                issuer,
                identity,
                0,
                initial,
                "mismatched ballots",
                approval=unverified,
                ballots=wrong_votes,
            )
        assert ledger._events() == []
        first = _decision(episode, VOTERS, initial.rendering, agreement)
        forged = replace(first, approvals=(VOTERS[0], VOTERS[2]))
        with pytest.raises(AuthorityError, match="verified ballot result"):
            record_triad_ruling(
                ledger,
                issuer,
                identity,
                0,
                initial,
                forged,
                agreement=agreement,
                voters=VOTERS,
                reason="spoofed supporters",
            )
        event = record_triad_ruling(
            ledger,
            issuer,
            identity,
            0,
            initial,
            first,
            agreement=agreement,
            voters=VOTERS,
            reason="initial quorum",
        )
        assert event.type == "Ratified"
        first_approval = VersionChange.model_validate(event.payload["data"]).approval
        assert first_approval and first_approval.approvers == [VOTERS[1], VOTERS[2]]

        second = _decision(episode, NEXT_VOTERS, revision.rendering, agreement)
        with pytest.raises(AuthorityError, match="requires new evidence"):
            record_triad_ruling(
                ledger,
                successor,
                identity,
                1,
                revision,
                second,
                agreement=agreement,
                voters=NEXT_VOTERS,
                reason="same evidence",
            )
        assert ledger.snapshot().ledger_version == event.seq + len(VOTERS)
        assert [row.type for row in ledger._events()] == ["Ratified", *["VoteCast"] * 3]
        votes = [
            VoteCast.model_validate(row.payload["data"])
            for row in ledger._events(after=event.seq)
        ]
        assert [(vote.voter, vote.choice) for vote in votes] == [
            (VOTERS[0], "alternative"),
            (VOTERS[1], initial.rendering),
            (VOTERS[2], initial.rendering),
        ]
        assert {vote.decision_id for vote in votes} == {
            event.payload["@graph"][0]["generated"]
        }
        changed = ledger.record(
            CLIENT,
            BriefChanged(clauses={"B1": "Use erişim belirteci for the access token."}),
        )
        recorded = record_triad_ruling(
            ledger,
            successor,
            identity,
            1,
            revision,
            second,
            agreement=agreement,
            voters=NEXT_VOTERS,
            reason="new principal wording",
        )
        change = VersionChange.model_validate(recorded.payload["data"])
        assert change.approval and change.approval.approvers == [
            NEXT_VOTERS[1],
            NEXT_VOTERS[2],
        ]
        assert change.brief_citations[0].ledger_version == changed.seq
        node = next(
            row
            for row in recorded.payload["@graph"]
            if row["@id"] == recorded.payload["@graph"][0]["generated"]
        )
        assert node["approver"] == [
            f"urn:soc:agent:{agent}" for agent in change.approval.approvers
        ]
        assert (
            ledger.reader()
            .in_scope("access token", sentence_id="9999-1-s01")[identity.id]
            .rendering
            == revision.rendering
        )

    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (_, ledger):
        assert ledger.verify() is None
        assert ledger.snapshot() == ledger.rebuild()
    with pytest.raises(AuthorityError, match="recorded operation authority"):
        with open_run_ledger(TOY, path, "toy"):
            pytest.fail("an episode-only handle reopened an authorized ledger")


def test_only_the_registered_principal_can_issue_serial_rulings_and_change_brief(
    tmp_path,
):
    path = tmp_path / "principal.sqlite"
    agreement = read_yaml(CONFIG, AgreementRules)
    impostor = CLIENT.model_copy(update={"agent": "impostor"})
    identity = IDENTITY.model_copy(
        update={
            "owner": CLIENT.agent,
            "authority": "principal",
            "subject": DecisionSubject(kind="normative", key="force"),
        }
    )
    ruling = RULING.model_copy(
        update={
            "ratifier": CLIENT.agent,
            "consistency": "SERIAL",
            "sentence_ids": ["9999-1-s01"],
        }
    )
    approval = RulingApproval(operation="toy", approvers=[CLIENT.agent])
    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (episode, ledger):
        with pytest.raises(AuthorityError, match="principal may change the brief"):
            ledger.record(impostor, BriefChanged(clauses={"B1": "spoofed change"}))
        with pytest.raises(AuthorityError, match="principal may issue"):
            ledger.ratify(
                impostor,
                identity,
                0,
                ruling.model_copy(update={"ratifier": impostor.agent}),
                "spoofed ruling",
                approval=approval,
            )
        decision = _decision(
            episode, VOTERS, ruling.rendering, agreement, kind="normative"
        )
        event = record_triad_ruling(
            ledger,
            CLIENT,
            identity,
            0,
            ruling,
            decision,
            agreement=agreement,
            voters=VOTERS,
            reason="unanimity failed",
        )
        assert VersionChange.model_validate(event.payload["data"]).approval == approval
        triad = RulingApproval(
            operation="toy", triad=list(VOTERS), approvers=list(VOTERS)
        )
        reviewer = Actor(agent=VOTERS[2], model="model-a", role="reviewer")
        with pytest.raises(AuthorityError, match="principal may issue"):
            ledger.transition(
                "Revised",
                reviewer,
                identity.id,
                1,
                ruling.model_copy(
                    update={"ratifier": reviewer.agent, "consistency": "ALL"}
                ),
                "triad overreach",
                approval=triad,
            )
        assert ledger.snapshot().ledger_version == event.seq + len(VOTERS)
        assert [row.type for row in ledger._events(after=event.seq)] == ["VoteCast"] * 3


def test_ballot_append_failure_rolls_back_ruling_and_earlier_ballots(tmp_path):
    path = tmp_path / "atomic-votes.sqlite"
    issuer = Actor(agent=VOTERS[2], model="reviewer-model", role="reviewer")
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
    agreement = read_yaml(CONFIG, AgreementRules)
    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (episode, ledger):
        decision = _decision(episode, VOTERS, ruling.rendering, agreement)
        with sqlite3.connect(path) as db:
            db.execute("""
                CREATE TRIGGER fail_second_vote BEFORE INSERT ON events
                WHEN NEW.type = 'VoteCast' AND NEW.seq = 3
                BEGIN
                    SELECT RAISE(ABORT, 'simulated ballot write failure');
                END
                """)

        def record():
            return record_triad_ruling(
                ledger,
                issuer,
                identity,
                0,
                ruling,
                decision,
                agreement=agreement,
                voters=VOTERS,
                reason="initial quorum",
            )

        with pytest.raises(
            sqlite3.IntegrityError, match="simulated ballot write failure"
        ):
            record()
        assert ledger._events() == []
        assert ledger.snapshot().current == {}
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER fail_second_vote")
        assert record().seq == 1
        assert [event.type for event in ledger._events()] == ["Ratified"] + [
            "VoteCast"
        ] * 3


def test_two_authorized_triads_collide_on_same_subject_and_site(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from soc.ledger.models import RulingConflict

    path = tmp_path / "two-triads.sqlite"
    agreement = read_yaml(CONFIG, AgreementRules)
    subjects = DecisionSubject(kind="term", key="access token")
    site = "9999-1-s01"
    with open_run_ledger(TOY, path, "toy", authority=POLICY):
        pass
    ready = Barrier(2, timeout=10)

    def submit(voters, rendering):
        issuer = Actor(agent=voters[2], model="scripted", role="reviewer")
        identity = IDENTITY.model_copy(
            update={
                "id": f"candidate-{issuer.agent}",
                "owner": issuer.agent,
                "authority": "triad",
                "subject": subjects,
            }
        )
        ruling = RULING.model_copy(
            update={
                "ratifier": issuer.agent,
                "rendering": rendering,
                "consistency": "QUORUM",
                "sentence_ids": [site],
            }
        )
        with open_run_ledger(TOY, path, "toy", authority=POLICY) as (episode, ledger):
            decision = _decision(episode, voters, rendering, agreement)
            ready.wait()
            return record_triad_ruling(
                ledger,
                issuer,
                identity,
                0,
                ruling,
                decision,
                agreement=agreement,
                voters=voters,
                reason="approved by scripted triad",
            )

    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(submit, VOTERS, "erişim jetonu")
        second = workers.submit(submit, NEXT_VOTERS, "erişim belirteci")
        events = [first.result(timeout=15), second.result(timeout=15)]
    assert sorted(event.type for event in events) == ["ConflictRaised", "Ratified"]
    winner = next(event for event in events if event.type == "Ratified")
    loser = next(event for event in events if event.type == "ConflictRaised")
    conflict = RulingConflict.model_validate(loser.payload["data"])
    assert conflict.candidate_id == loser.commitment_id
    assert conflict.subject == subjects
    (overlap,) = conflict.overlaps
    assert overlap.commitment_id == winner.commitment_id and overlap.sentence_ids == [
        site
    ]
    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (_, ledger):
        assert set(ledger.snapshot().current) == {winner.commitment_id}
        assert sum(event.type == "VoteCast" for event in ledger._events()) == 3
        assert ledger.verify() is None
