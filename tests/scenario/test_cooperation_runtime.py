import sqlite3
from pathlib import Path

import pytest

from soc.cooperation.runtime import CooperationJournal, CooperationSettings, actor_key
from soc.cooperation.standing import CooperationPolicy
from soc.ledger.models import (
    ChunkBuilt,
    DecisionCaptured,
    DecisionDeclared,
    ExecutorRebound,
    FlagRaised,
    FlagResolved,
    GateFinding,
    ResolutionBallot,
    ResolutionTrace,
    VersionRef,
)
from soc.orchestrator.ledger import open_run_ledger
from tests.scenario.test_ledger_checks import CLIENT, POLICY, TOY, WORKER, _ratify


def settings(**updates):
    return CooperationSettings(
        episode_position=1,
        visibility="hidden",
        policy=CooperationPolicy(minimum_checks=1, window=5),
        **updates,
    )


def test_runtime_journal_is_append_only_idempotent_and_rejects_configuration_drift(
    tmp_path,
):
    path = tmp_path / "cooperation.sqlite"
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        _ratify(ledger, "term", "erişim jetonu")
        journal = CooperationJournal(path, "toy", settings(), (WORKER,))
        first = journal.sync(ledger)
        assert journal.sync(ledger).snapshot(actor_key(WORKER)) == first.snapshot(
            actor_key(WORKER)
        )
        with sqlite3.connect(path) as db:
            assert db.execute("SELECT count(*) FROM source_events").fetchone()[0] == 1
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute("DELETE FROM source_events")
        with pytest.raises(ValueError, match="configuration"):
            CooperationJournal(path, "different-run", settings(), (WORKER,)).sync(
                ledger
            )


def test_runtime_checks_use_authored_build_and_do_not_punish_revision_rechecks(
    tmp_path,
):
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        _ratify(ledger, "term", "erişim jetonu")
        ledger.record(
            WORKER,
            ChunkBuilt(ledger_version=1, uses=[], decisions={}, output_ref="ref"),
            chunk_id="chunk-1",
        )
        # Gate events are normally emitted by check_builds, not by Ledger.record.
        verdicts = [GateFinding(rule="numbers", detail="Missing 3", outcome="block")]
        ledger.check_builds(
            CLIENT, {"chunk-1": 1}, path="gate", evaluate=lambda *_: verdicts
        )
        journal = CooperationJournal(
            tmp_path / "cooperation.sqlite", "toy", settings(), (WORKER,)
        )
        state = journal.sync(ledger).snapshot(actor_key(WORKER))
        assert state.checked == 1 and state.score == 0
        assert state.sanction == "replacement"
        verdicts[:] = [
            GateFinding(rule="impact", detail="Authorized revision", outcome="block")
        ]
        ledger.check_builds(
            CLIENT, {"chunk-1": 1}, path="final_sweep", evaluate=lambda *_: verdicts
        )
        assert journal.sync(ledger).snapshot(actor_key(WORKER)) == state
        with sqlite3.connect(journal.path) as db:
            text = " ".join(
                row[0] for row in db.execute("SELECT records FROM source_events")
            )
            assert "StandingUpdated" in text and "SanctionApplied" in text


def test_capture_checks_deduplicate_same_attempt_and_keep_unknowns_separate(tmp_path):
    ref = VersionRef(commitment_id="term", version=1)
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        _ratify(ledger, "term", "erişim jetonu")
        declaration = DecisionDeclared(ref=ref, sentence_id="9999-1-s01", verified=True)
        ledger.capture_draft(WORKER, "chunk-1", lambda _: [declaration])
        ledger.capture_draft(WORKER, "chunk-1", lambda _: [declaration])
        silent = DecisionCaptured(
            ref=ref, sentence_id="9999-1-s02", excerpt="erişim jetonu", start=0, end=13
        )
        ledger.capture_draft(WORKER, "chunk-1", lambda _: [silent])
        journal = CooperationJournal(
            tmp_path / "cooperation.sqlite", "toy", settings(), (WORKER,)
        )
        state = journal.sync(ledger).snapshot(actor_key(WORKER))
        assert (state.checked, state.passed) == (2, 1)


@pytest.mark.parametrize(
    "visibility,sanction", [("hidden", "none"), ("announced", "no_vote")]
)
def test_runtime_successor_uses_model_identity_and_replays_transfer(
    tmp_path, visibility, sanction
):
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        successor = WORKER.model_copy(update={"model": "next-model"})
        ledger.record(
            successor,
            ExecutorRebound(
                old_model=WORKER.model, new_model=successor.model, ledger_version=0
            ),
        )
        config = settings().model_copy(update={"visibility": visibility})
        journal = CooperationJournal(
            tmp_path / "cooperation.sqlite", "toy", config, (WORKER,)
        )
        first = journal.sync(ledger).snapshot(actor_key(successor))
        restored = CooperationJournal(journal.path, "toy", config, (WORKER,)).sync(
            ledger
        )
        assert first == restored.snapshot(actor_key(successor))
        assert first.sanction == sanction
        assert actor_key(WORKER) != actor_key(successor)


def test_runtime_journal_rejects_ledger_prefix_change_and_tampered_projection(tmp_path):
    path = tmp_path / "cooperation.sqlite"
    config = settings()
    with open_run_ledger(TOY, tmp_path / "one.sqlite", "toy", authority=POLICY) as (
        _,
        first,
    ):
        _ratify(first, "term", "erişim jetonu")
        CooperationJournal(path, "toy", config, (WORKER,)).sync(first)
        with sqlite3.connect(path) as db:
            db.execute("DROP TRIGGER source_no_update")
            db.execute("UPDATE source_events SET records = '[{}]'")
        with pytest.raises(ValueError, match="projection"):
            CooperationJournal(path, "toy", config, (WORKER,)).sync(first)
    with open_run_ledger(TOY, tmp_path / "two.sqlite", "toy", authority=POLICY) as (
        _,
        second,
    ):
        with pytest.raises(ValueError, match="prefix"):
            CooperationJournal(path, "toy", config, (WORKER,)).sync(second)


@pytest.mark.parametrize(
    "rule,outcome", [("artifact", "block"), ("term", "unknown"), ("impact", "block")]
)
def test_administrative_and_unknown_gate_findings_are_not_defection(
    tmp_path, rule, outcome
):
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        ledger.record(
            WORKER,
            ChunkBuilt(ledger_version=0, uses=[], decisions={}, output_ref="ref"),
            chunk_id="chunk-1",
        )
        ledger.check_builds(
            CLIENT,
            {"chunk-1": 1},
            evaluate=lambda *_: [
                GateFinding(
                    rule=rule, detail="not a runtime compliance result", outcome=outcome
                )
            ],
        )
        state = (
            CooperationJournal(
                tmp_path / "cooperation.sqlite", "toy", settings(), (WORKER,)
            )
            .sync(ledger)
            .snapshot(actor_key(WORKER))
        )
        assert state.checked == 0 and state.score == 1


@pytest.mark.parametrize("revision_before_build", [False, True])
def test_first_gate_after_authorized_rule_change_is_unknown(
    tmp_path, revision_before_build
):
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        if revision_before_build:
            _ratify(ledger, "term", "erişim jetonu")
        ledger.record(
            WORKER,
            ChunkBuilt(ledger_version=0, uses=[], decisions={}, output_ref="ref"),
            chunk_id="chunk-1",
        )
        if not revision_before_build:
            _ratify(ledger, "term", "erişim jetonu")
        ledger.check_builds(
            CLIENT,
            {"chunk-1": 1},
            evaluate=lambda *_: [
                GateFinding(rule="term", detail="new ruling", outcome="block")
            ],
        )
        state = (
            CooperationJournal(
                tmp_path / "cooperation.sqlite", "toy", settings(), (WORKER,)
            )
            .sync(ledger)
            .snapshot(actor_key(WORKER))
        )
        assert state.checked == 0


def test_runtime_credits_only_validated_evidence_supporters_and_principal_flags_remain_unknown(
    tmp_path,
):
    reviewer = WORKER.model_copy(update={"agent": "reviewer"})
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        for flag_id, consistency, approvers in (
            ("triad", "QUORUM", [WORKER.agent, reviewer.agent]),
            ("principal", "SERIAL", [CLIENT.agent]),
        ):
            ledger.record(
                reviewer,
                FlagRaised(
                    flag_id=flag_id, sentence_id="9999-1-s01", reason="difference"
                ),
                chunk_id="chunk-1",
            )
            ledger.record(
                reviewer,
                FlagResolved(
                    flag_id=flag_id,
                    trace=ResolutionTrace(
                        sentence_id="9999-1-s01",
                        decision_kind="term",
                        required="QUORUM",
                        consistency=consistency,
                        rendering="term",
                        approvers=approvers,
                        ballots=[ResolutionBallot(voter=WORKER.agent, choice="first")],
                        evidence_supporters=[WORKER.agent, "intruder"],
                    ),
                ),
                chunk_id="chunk-1",
            )
        tracker = CooperationJournal(
            tmp_path / "cooperation.sqlite", "toy", settings(), (WORKER, reviewer)
        ).sync(ledger)
        assert tracker.snapshot(actor_key(WORKER)).checked == 1
        assert tracker.snapshot(actor_key(reviewer)).checked == 1


def test_cooperation_rejects_unknown_draft_author_and_inconsistent_transfer(tmp_path):
    journal = CooperationJournal(
        tmp_path / "cooperation.sqlite", "toy", settings(), (WORKER,)
    )
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        _ratify(ledger, "term", "erişim jetonu")
        ledger.capture_draft(
            CLIENT,
            "chunk-1",
            lambda _: [
                DecisionDeclared(
                    ref=VersionRef(commitment_id="term", version=1),
                    sentence_id="9999-1-s01",
                    verified=True,
                )
            ],
        )
        with pytest.raises(ValueError, match="unauthenticated"):
            journal.sync(ledger)
    with open_run_ledger(TOY, tmp_path / "other.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        ledger.record(
            WORKER,
            ExecutorRebound(
                old_model="wrong-model", new_model=WORKER.model, ledger_version=0
            ),
        )
        with pytest.raises(ValueError, match="transfer"):
            journal.sync(ledger)


def test_cooperation_detects_same_length_fork_and_broken_ledger_hashes(tmp_path):
    journal = CooperationJournal(
        tmp_path / "cooperation.sqlite", "toy", settings(), (WORKER,)
    )
    for filename in ("first.sqlite", "second.sqlite"):
        with open_run_ledger(TOY, tmp_path / filename, "toy", authority=POLICY) as (
            _,
            ledger,
        ):
            _ratify(ledger, "term", "erişim jetonu")
            if filename == "first.sqlite":
                journal.sync(ledger)
            else:
                with pytest.raises(ValueError, match="prefix"):
                    journal.sync(ledger)
                ledger._db.execute("DROP TRIGGER event_no_update")
                ledger._db.execute("UPDATE events SET hash = 'broken'")
                with pytest.raises(ValueError, match="integrity"):
                    ledger.history()


def test_cooperation_requires_distinct_participants_and_serial_scheduling(tmp_path):
    import asyncio

    from pydantic import ValidationError

    from soc.orchestrator.ledger_runner import LedgerRunSpec
    from soc.orchestrator.live import SOCCondition
    from soc.orchestrator.model_rag import run_model_rag_episode
    from tests.scenario.test_model_triad import SPEC, TEAM

    for participants in ((), (WORKER, WORKER)):
        with pytest.raises(ValueError, match="distinct"):
            CooperationJournal(
                tmp_path / "cooperation.sqlite", "toy", settings(), participants
            )
    with pytest.raises(ValidationError, match="max_in_flight"):
        LedgerRunSpec.model_validate(
            {**SPEC.model_dump(), "max_in_flight": 2, "cooperation": settings()}
        )
    with pytest.raises(ValidationError, match="SOC memory"):
        SOCCondition(
            run=SPEC.model_copy(update={"cooperation": settings()}),
            team=TEAM,
            memory="rag",
        )
    with pytest.raises(ValueError, match="SOC memory"):
        asyncio.run(
            run_model_rag_episode(
                TOY,
                tmp_path / "rag",
                spec=SPEC.model_copy(update={"cooperation": settings()}),
                experiment=None,
                principal=None,
                checker=None,
                client=None,
                team=None,
                agreement=None,
                embedder=None,
            )
        )


@pytest.mark.parametrize(
    "finding_site,expected", [("9999-1-s01", "fail"), ("9999-1-s02", "unknown")]
)
def test_review_check_attributes_only_moderated_sites(tmp_path, finding_site, expected):
    import json

    reviewer = WORKER.model_copy(update={"agent": "reviewer"})
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        ledger.record(
            reviewer,
            FlagRaised(flag_id="flag", sentence_id="9999-1-s01", reason="difference"),
            chunk_id="chunk-1",
        )
        ledger.record(
            reviewer,
            FlagResolved(
                flag_id="flag",
                trace=ResolutionTrace(
                    sentence_id="9999-1-s01",
                    decision_kind="term",
                    required="QUORUM",
                    consistency="QUORUM",
                    rendering="term",
                    approvers=[WORKER.agent, reviewer.agent],
                    ballots=[],
                ),
            ),
            chunk_id="chunk-1",
        )
        ledger.record(
            WORKER,
            ChunkBuilt(ledger_version=2, uses=[], decisions={}, output_ref="ref"),
            chunk_id="chunk-1",
        )
        ledger.check_builds(
            CLIENT,
            {"chunk-1": 1},
            evaluate=lambda *_: [
                GateFinding(
                    rule="term",
                    detail="wrong term",
                    outcome="block",
                    sentence_id=finding_site,
                )
            ],
        )
        path = tmp_path / "cooperation.sqlite"
        CooperationJournal(path, "toy", settings(), (WORKER, reviewer)).sync(ledger)
        with sqlite3.connect(path) as db:
            checks = [
                row["check"]
                for (stored,) in db.execute("SELECT records FROM source_events")
                for row in json.loads(stored)
                if row["kind"] == "StandingUpdated"
            ]
        (review,) = [row for row in checks if row["kind"] == "review"]
        assert review["agent"] == actor_key(reviewer) and review["outcome"] == expected


@pytest.mark.parametrize("blocked_agent,passes", [("reviewer", 1), ("peer", 0)])
def test_model_runner_stops_before_calls_when_review_or_replacement_is_sanctioned(
    tmp_path, blocked_agent, passes
):
    import asyncio
    from types import SimpleNamespace

    from soc.enforce.checker import Checker
    from soc.episode import read_yaml
    from soc.memory import ChunkInput
    from soc.orchestrator.ledger_runner import ChunkTask
    from soc.orchestrator.model_triad import ModelTriad
    from soc.orchestrator.runner import ExperimentSettings
    from soc.providers.models import Outcome
    from soc.triad.agreement import AgreementRules
    from tests.scenario.test_model_triad import SPEC, TEAM

    triad = ModelTriad(
        SPEC,
        ExperimentSettings(),
        None,
        TEAM,
        read_yaml(Path("config/m2-agreement.yaml"), AgreementRules),
        SimpleNamespace(actor=CLIENT),
        Checker(),
    )
    executor = WORKER.model_copy(
        update={"agent": "executor", "model": SPEC.initial_model, "role": "executor"}
    )
    triad.cooperation = CooperationJournal(
        tmp_path / "cooperation.sqlite",
        "toy",
        settings(),
        (executor, triad.peer, triad.reviewer),
    )
    actor = triad.reviewer if blocked_agent == "reviewer" else triad.peer
    with open_run_ledger(TOY, tmp_path / "ledger.sqlite", "toy", authority=POLICY) as (
        episode,
        ledger,
    ):
        _ratify(ledger, "term", "erişim jetonu")
        observations = [
            DecisionDeclared(
                ref=VersionRef(commitment_id="term", version=1),
                sentence_id="9999-1-s01",
                verified=False,
            )
        ]
        if passes:
            observations.append(
                DecisionDeclared(
                    ref=VersionRef(commitment_id="term", version=1),
                    sentence_id="9999-1-s02",
                    verified=True,
                )
            )
        ledger.capture_draft(actor, "chunk-1", lambda _: observations)
        task = ChunkTask(
            "chunk-1",
            ChunkInput(1, tuple(episode.source[:3])),
            executor,
            ledger.snapshot().ledger_version,
            episode.brief,
            {},
            "cross_examination",
            None,
            0,
            None,
            (),
            (),
        )
        assert asyncio.run(triad(ledger, task)) is Outcome.COOPERATION_BLOCKED
