import sqlite3
from pathlib import Path

import pytest

from soc.episode import read_yaml
from soc.ledger import store
from soc.ledger.models import (
    Actor,
    ChunkBuilt,
    DecisionSubject,
    GateDecision,
    OperationAuthority,
    RulingApproval,
    RulingBallot,
    StaleMarked,
    VersionRef,
)
from soc.orchestrator.ledger import open_run_ledger, record_triad_ruling
from soc.triad.agreement import AgreementRules, Ballot, Decision
from soc.triad.examination import Difference, Examination
from tests.ledger_examples import IDENTITY, OWNER, RULING

TOY = Path("data/episodes/toy")
CONFIG = Path("config/m2-agreement.yaml")
POLICY = OperationAuthority(operation="toy", principal="client")
VOTERS = ("left", "right", "reviewer")
ISSUER = Actor(agent="reviewer", model="model-a", role="reviewer")
SITE = "9999-1-s01"
ONE = RulingApproval(operation="toy", triad=list(VOTERS), approvers=["right"])
QUORUM = RulingApproval(
    operation="toy", triad=list(VOTERS), approvers=["right", "reviewer"]
)


def _ballots(approval, ruling):
    return tuple(
        RulingBallot(
            voter=voter,
            choice=ruling.rendering if voter in approval.approvers else None,
        )
        for voter in VOTERS
    )


def _identity(kind):
    return IDENTITY.model_copy(
        update={
            "owner": ISSUER.agent,
            "authority": "triad",
            "subject": DecisionSubject(kind=kind, key="access token"),
        }
    )


def _provisional(sites):
    return RULING.model_copy(
        update={
            "ratifier": ISSUER.agent,
            "consistency": "ONE",
            "state": "provisional",
            "sentence_ids": sites,
        }
    )


def _build(ledger, chunk, ledger_version, ruling, *, version=1, number=1):
    record = ChunkBuilt(
        ledger_version=ledger_version,
        uses=[VersionRef(commitment_id=IDENTITY.id, version=version)],
        decisions={IDENTITY.id: ruling.rendering},
        build_number=number,
        output_ref=f"outputs/{chunk}/build-{number}.json",
    )
    return ledger.record(ISSUER, record, chunk_id=chunk, base_build=number - 1)


def _gate(ledger, chunk, number=1):
    event = ledger.check_provisional_dependencies(ISSUER, chunk, build_number=number)
    assert event.type == "GateDecision" and event.chunk_id == chunk
    return GateDecision.model_validate(event.payload["data"])


def test_four_drafts_block_and_overturn_marks_current_and_late_builds(tmp_path):
    path = tmp_path / "provisional.sqlite"
    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (episode, ledger):
        sites = [row.id for row in episode.source if "access token" in row.scope_keys]
        proposal = _provisional(sites)
        initial = ledger.propose(
            ISSUER,
            _identity("term"),
            proposal,
            "initial reading",
            approval=ONE,
            ballots=_ballots(ONE, proposal),
        )
        assert initial.type == "Provisional" and initial.new_version == 1
        for position in range(1, 4):
            _build(ledger, f"chunk-{position}", initial.seq, proposal)
        assert _gate(ledger, "chunk-1").accepted
        _build(ledger, "chunk-4", initial.seq, proposal)
        for position in range(1, 5):
            result = _gate(ledger, f"chunk-{position}")
            assert not result.accepted and "used in 4 chunks" in result.reason

        revised = proposal.model_copy(
            update={
                "state": "amended",
                "consistency": "QUORUM",
                "rendering": "erişim belirteci",
                "evidence": [*proposal.evidence, "source:9999-2-s01"],
            }
        )
        decision = ledger.transition(
            "Revised",
            ISSUER,
            IDENTITY.id,
            1,
            revised,
            "overturned",
            approval=QUORUM,
            ballots=_ballots(QUORUM, revised),
        )
        marked = next(
            event
            for event in ledger._events(after=decision.seq)
            if event.type == "StaleMarked"
        )
        assert marked.type == "StaleMarked"
        assert StaleMarked.model_validate(marked.payload["data"]).chunk_ids == [
            "chunk-1",
            "chunk-2",
            "chunk-3",
            "chunk-4",
        ]
        assert "overturned" in _gate(ledger, "chunk-4").reason
        _build(ledger, "chunk-4", initial.seq, proposal, number=2)
        assert not _gate(ledger, "chunk-4", 2).accepted

        _build(ledger, "chunk-1", decision.seq, revised, version=2, number=2)
        with pytest.raises(ValueError, match="current chunk build"):
            _gate(ledger, "chunk-1")
        assert _gate(ledger, "chunk-1", 2).accepted

    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (_, ledger):
        assert ledger.snapshot().stale_chunks == {
            "chunk-2",
            "chunk-3",
            "chunk-4",
        }
        assert ledger.snapshot() == ledger.rebuild()
        assert ledger.verify() is None


def test_normative_provisional_blocks_until_unanimous_confirmation(tmp_path):
    agreement = read_yaml(CONFIG, AgreementRules)
    proposal = _provisional([SITE])
    confirmation = proposal.model_copy(
        update={"state": "ratified", "consistency": "ALL"}
    )
    identity = _identity("normative")
    examination = Examination(Difference(SITE, "alternative", proposal.rendering), ())
    provisional = Decision(
        examination,
        "provisional",
        "ONE",
        "ONE",
        proposal.rendering,
        ("right",),
        (
            ("left", Ballot(choice=None)),
            ("right", Ballot(choice="second")),
            ("reviewer", Ballot(choice=None)),
        ),
    )
    binding = Decision(
        examination,
        "normative",
        "ALL",
        "ALL",
        proposal.rendering,
        VOTERS,
        tuple((agent, Ballot(choice="second")) for agent in VOTERS),
    )
    with open_run_ledger(
        TOY, tmp_path / "normative.sqlite", "toy", authority=POLICY
    ) as (_, ledger):
        original = record_triad_ruling(
            ledger,
            ISSUER,
            identity,
            0,
            proposal,
            provisional,
            agreement=agreement,
            voters=VOTERS,
            reason="provisional",
        )
        assert original.type == "Provisional"
        _build(ledger, "chunk-1", original.seq, proposal)
        gated = _gate(ledger, "chunk-1")
        assert not gated.accepted and "unresolved normative ruling" in gated.reason
        record_triad_ruling(
            ledger,
            ISSUER,
            identity,
            1,
            confirmation,
            binding,
            agreement=agreement,
            voters=VOTERS,
            reason="confirmed",
        )
        assert _gate(ledger, "chunk-1").accepted
        assert not ledger.snapshot().stale_chunks


def test_schema_upgrade_preserves_event_hashes_and_accepts_provisional_writes(
    tmp_path, monkeypatch
):
    database = tmp_path / "legacy.sqlite"
    old_ddl = tmp_path / "store-v0.sql"
    old_ddl.write_text(store.DDL.read_text().replace("'Provisional', ", ""))
    with monkeypatch.context() as patch:
        patch.setattr(store, "DDL", old_ddl)
        with store.Ledger(database, "toy") as ledger:
            legacy = ledger.ratify(OWNER, IDENTITY, 0, RULING, "legacy")
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA user_version = 0")

    with open_run_ledger(TOY, database, "toy", authority=POLICY) as (_, ledger):
        assert ledger._events() == [legacy]
        new = _identity("term").model_copy(update={"id": "new"})
        proposal = _provisional([SITE])
        appended = ledger.propose(
            ISSUER,
            new,
            proposal,
            "first draft",
            approval=ONE,
            ballots=_ballots(ONE, proposal),
        )
        assert appended.prev_hash == legacy.hash
        assert appended.type == "Provisional" and ledger.verify() is None
