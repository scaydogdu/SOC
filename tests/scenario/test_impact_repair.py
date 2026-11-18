import asyncio
from pathlib import Path

import pytest

from soc.enforce.checker import Checker
from soc.ledger.models import (
    Actor,
    ChunkBuilt,
    DecisionSubject,
    DecisionUse,
    GateDecision,
    OperationAuthority,
    RulingApproval,
    RulingBallot,
    StaleDetected,
    VersionRef,
)
from soc.memory import ChunkInput, ChunkOutput, Translation
from soc.orchestrator.ledger import open_run_ledger
from soc.orchestrator.repair import final_sweep, gate_with_repair, repair_on_read
from tests.ledger_examples import IDENTITY, RULING

TOY = Path("data/episodes/toy")
POLICY = OperationAuthority(operation="toy", principal="client")
CLIENT = Actor(agent="client", model="human", role="principal")
WORKER = Actor(agent="worker", model="model", role="worker")
APPROVAL = RulingApproval(operation="toy", approvers=["client"])
OLD = VersionRef(commitment_id=IDENTITY.id, version=1)
NEW = VersionRef(commitment_id=IDENTITY.id, version=2)


def _start(ledger, sites):
    identity = IDENTITY.model_copy(
        update={
            "authority": "principal",
            "owner": "client",
            "subject": DecisionSubject(kind="term", key="access token"),
        }
    )
    ruling = RULING.model_copy(
        update={"ratifier": "client", "consistency": "SERIAL", "sentence_ids": sites}
    )
    event = ledger.ratify(CLIENT, identity, 0, ruling, "original", approval=APPROVAL)
    return ruling, event.seq


def _build(chunk_id, number, observed, ref, site):
    return ChunkBuilt(
        ledger_version=observed,
        uses=[ref],
        decisions={},
        build_number=number,
        output_ref=f"{chunk_id}-{number}.json",
        use_sites=[DecisionUse(ref=ref, sentence_id=site)],
    )


def test_scope_reduction_marks_only_uses_at_removed_sites(tmp_path):
    path = tmp_path / "scoped.sqlite"
    kept, removed = "9999-1-s01", "9999-1-s03"
    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (_, ledger):
        ruling, observed = _start(ledger, [kept, removed])
        first = _build("chunk-1", 1, observed, OLD, kept)
        second = _build("chunk-2", 1, observed, OLD, removed)
        ledger.record(WORKER, first, chunk_id="chunk-1")
        ledger.record(WORKER, second, chunk_id="chunk-2")
        assert not ledger.revision_hints([OLD], use_sites=second.use_sites)
        ledger.transition(
            "Revised",
            CLIENT,
            IDENTITY.id,
            1,
            ruling.model_copy(update={"sentence_ids": [kept]}),
            "remove the other site",
            approval=APPROVAL,
        )
        assert ledger.snapshot().stale_chunks == {"chunk-2"}
        assert not ledger.revision_hints([OLD], use_sites=first.use_sites)
        (hint,) = ledger.revision_hints([OLD], use_sites=second.use_sites)
        assert hint.used == OLD and hint.current.version == 2
        assert hint.current.sentence_ids == [kept]
        with pytest.raises(ValueError, match="outside the recorded ruling's scope"):
            ledger.record(
                WORKER,
                _build("chunk-2", 2, ledger.snapshot().ledger_version, NEW, removed),
                chunk_id="chunk-2",
                base_build=1,
            )


def test_evidence_only_or_consistency_upgrade_keeps_existing_build_current(tmp_path):
    site = "9999-1-s01"
    voters = ["worker", "other", "reviewer"]
    identity = IDENTITY.model_copy(
        update={
            "authority": "triad",
            "owner": WORKER.agent,
            "subject": DecisionSubject(kind="term", key="access token"),
        }
    )
    ruling = RULING.model_copy(
        update={
            "ratifier": WORKER.agent,
            "consistency": "QUORUM",
            "sentence_ids": [site],
        }
    )

    def approval(supporters):
        return RulingApproval(operation="toy", triad=voters, approvers=supporters)

    def ballots(supporters):
        return tuple(
            RulingBallot(
                voter=voter, choice=ruling.rendering if voter in supporters else None
            )
            for voter in voters
        )

    with open_run_ledger(
        TOY, tmp_path / "evidence.sqlite", "toy", authority=POLICY
    ) as (_, ledger):
        event = ledger.ratify(
            WORKER,
            identity,
            0,
            ruling,
            "quorum",
            approval=approval(voters[:2]),
            ballots=ballots(voters[:2]),
        )
        build = _build("chunk-1", 1, event.seq, OLD, site)
        ledger.record(WORKER, build, chunk_id="chunk-1")

        evidence_only = ruling.model_copy(
            update={"evidence": [*ruling.evidence, "source:9999-1-s03"]}
        )
        unanimous = evidence_only.model_copy(
            update={
                "consistency": "ALL",
                "evidence": [*evidence_only.evidence, "source:9999-2-s01"],
            }
        )
        for base, revision, supporters in (
            (1, evidence_only, voters[:2]),
            (2, unanimous, voters),
        ):
            ledger.transition(
                "Revised",
                WORKER,
                identity.id,
                base,
                revision,
                "new evidence",
                approval=approval(supporters),
                ballots=ballots(supporters),
            )
            assert ledger.snapshot().stale_chunks == set()
            assert ledger.revision_hints(build.uses, use_sites=build.use_sites) == ()
            assert ledger.repair_request(WORKER, "chunk-1", "review") is None
            gate = ledger.check_provisional_dependencies(
                WORKER, "chunk-1", build_number=1
            )
            assert GateDecision.model_validate(gate.payload["data"]).accepted


def test_episode_bound_build_checks_chunk_and_sentence_and_pins_id_base(tmp_path):
    path = tmp_path / "chunks.sqlite"
    with open_run_ledger(TOY, path, "toy", authority=POLICY, chunk_id_base=0) as (
        _,
        ledger,
    ):
        ruling, observed = _start(ledger, ["9999-1-s01", "9999-2-s03"])
        assert ledger.public_chunk_ids == ("chunk-0", "chunk-1", "chunk-2", "chunk-3")
        first = _build("chunk-0", 1, observed, OLD, ruling.sentence_ids[0])
        with pytest.raises(ValueError, match="chunk is not in the public episode"):
            ledger.record(WORKER, first, chunk_id="no-such-chunk")
        with pytest.raises(ValueError, match="outside the scheduled chunk"):
            ledger.record(WORKER, first, chunk_id="chunk-3")
        assert ledger.snapshot().chunks == {}
        ledger.record(WORKER, first, chunk_id="chunk-0")
    with pytest.raises(ValueError, match="public episode differs"):
        with open_run_ledger(TOY, path, "toy", authority=POLICY):
            pytest.fail("opened under a different chunk numbering")
    with open_run_ledger(TOY, path, "toy", authority=POLICY, chunk_id_base=0) as (
        _,
        ledger,
    ):
        assert ledger.snapshot().chunks == {"chunk-0": first}


def test_rebuild_using_superseded_version_is_stale_again(tmp_path):
    site = "9999-1-s01"
    with open_run_ledger(TOY, tmp_path / "stale.sqlite", "toy", authority=POLICY) as (
        _,
        ledger,
    ):
        ruling, observed = _start(ledger, [site])
        first = _build("chunk-1", 1, observed, OLD, site)
        ledger.record(WORKER, first, chunk_id="chunk-1")
        revised = ruling.model_copy(update={"rendering": "erişim belirteci"})
        ledger.transition(
            "Revised",
            CLIENT,
            IDENTITY.id,
            1,
            revised,
            "new rendering",
            approval=APPROVAL,
        )
        request = ledger.repair_request(WORKER, "chunk-1", "review")
        assert request is not None and request.build == first
        (hint,) = request.hints
        assert hint.used == OLD and hint.current.rendering == revised.rendering
        assert ledger.repair_request(WORKER, "chunk-1", "gate").hints == (hint,)

        # A draft that started before the revision finishes after the hint was issued.
        still_old = _build("chunk-1", 2, observed, OLD, site)
        ledger.record(WORKER, still_old, chunk_id="chunk-1", base_build=1)
        assert ledger.snapshot().stale_chunks == {"chunk-1"}
        gate = ledger.check_provisional_dependencies(WORKER, "chunk-1", build_number=2)
        assert not GateDecision.model_validate(gate.payload["data"]).accepted
        assert ledger.repair_request(WORKER, "chunk-1", "final_sweep").hints == (hint,)
        detections = [
            StaleDetected.model_validate(event.payload["data"])
            for event in ledger._events()
            if event.type == "StaleDetected"
        ]
        assert detections == [
            StaleDetected(path="review", build_number=1),
            StaleDetected(path="gate", build_number=2),
        ]


def test_repairs_at_review_gate_and_final_sweep_are_logged_once(tmp_path):
    path = tmp_path / "repair.sqlite"
    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (_, ledger):
        chunks = {
            f"chunk-{pos}": ChunkInput(pos, sentences)
            for pos, sentences in enumerate(ledger.public_chunks(), 1)
        }
        sites = {key: chunk.sentences[0].id for key, chunk in chunks.items()}
        ruling, observed = _start(ledger, list(sites.values()))
        outputs = {}

        def store_output(build, chunk_id, rendering):
            outputs[build.output_ref] = ChunkOutput(
                sentences=[
                    Translation(
                        sentence_id=sentence.id,
                        text=rendering
                        if sentence.id == sites[chunk_id]
                        else sentence.text,
                    )
                    for sentence in chunks[chunk_id].sentences
                ]
            )

        def initial(chunk_id):
            build = _build(chunk_id, 1, observed, OLD, sites[chunk_id])
            store_output(build, chunk_id, ruling.rendering)
            ledger.record(WORKER, build, chunk_id=chunk_id)

        for chunk_id in list(chunks)[:3]:
            initial(chunk_id)
        revision = ruling.model_copy(update={"rendering": "erişim belirteci"})
        ledger.transition(
            "Revised",
            CLIENT,
            IDENTITY.id,
            1,
            revision,
            "new rendering",
            approval=APPROVAL,
        )
        initial("chunk-4")
        assert ledger.snapshot().stale_chunks == set(chunks)

        invoked = []

        async def repair(request):
            invoked.append((request.chunk_id, request.path))
            (hint,) = request.hints
            assert (hint.used, hint.current.version) == (OLD, NEW.version)
            build = _build(
                request.chunk_id,
                request.build.build_number + 1,
                request.ledger_version,
                NEW,
                sites[request.chunk_id],
            )
            store_output(build, request.chunk_id, hint.current.rendering)
            return build

        async def no_budget(_request):
            return None

        async def scenario():
            await repair_on_read(ledger, WORKER, "chunk-1", "review", repair)
            gate = await gate_with_repair(
                ledger,
                WORKER,
                "chunk-2",
                chunks["chunk-2"],
                repair=repair,
                checker=Checker(),
                load_output=outputs.__getitem__,
            )
            assert GateDecision.model_validate(gate.payload["data"]).accepted
            with pytest.raises(ValueError, match="every scheduled"):
                await final_sweep(
                    ledger,
                    WORKER,
                    {},
                    repair=repair,
                    checker=Checker(),
                    load_output=outputs.__getitem__,
                )
            blocked = await final_sweep(
                ledger,
                WORKER,
                chunks,
                repair=no_budget,
                checker=Checker(),
                load_output=outputs.__getitem__,
            )
            assert not blocked.accepted
            completed = await final_sweep(
                ledger,
                WORKER,
                chunks,
                repair=repair,
                checker=Checker(),
                load_output=outputs.__getitem__,
            )
            assert completed.accepted and len(completed.decisions) == len(chunks)

        asyncio.run(scenario())
        assert invoked == [
            ("chunk-1", "review"),
            ("chunk-2", "gate"),
            ("chunk-3", "final_sweep"),
            ("chunk-4", "final_sweep"),
        ]
        assert not ledger.snapshot().stale_chunks
        assert ledger.snapshot().usage == {(IDENTITY.id, 2): list(chunks)}
        logged = [
            (event.chunk_id, StaleDetected.model_validate(event.payload["data"]).path)
            for event in ledger._events()
            if event.type == "StaleDetected"
        ]
        assert logged == invoked

    with open_run_ledger(TOY, path, "toy", authority=POLICY) as (_, ledger):
        assert ledger.snapshot() == ledger.rebuild()
        assert ledger.verify() is None


def test_ruling_without_sites_marks_builds_that_used_removed_scope_key(tmp_path):
    with open_run_ledger(
        TOY, tmp_path / "scope-keys.sqlite", "toy", authority=POLICY
    ) as (_, ledger):
        identity = IDENTITY.model_copy(
            update={"authority": "principal", "owner": CLIENT.agent}
        )
        ruling = RULING.model_copy(
            update={
                "ratifier": CLIENT.agent,
                "consistency": "SERIAL",
                "scope": ["access token", "refresh token"],
            }
        )
        observed = ledger.ratify(
            CLIENT, identity, 0, ruling, "original", approval=APPROVAL
        ).seq
        first = _build("chunk-1", 1, observed, OLD, "9999-1-s01")
        second = _build("chunk-2", 1, observed, OLD, "9999-1-s03")
        ledger.record(WORKER, first, chunk_id="chunk-1")
        ledger.record(WORKER, second, chunk_id="chunk-2")
        ledger.transition(
            "Revised",
            CLIENT,
            identity.id,
            1,
            ruling.model_copy(update={"scope": ["access token"]}),
            "remove refresh-token scope",
            approval=APPROVAL,
        )
        assert ledger.snapshot().stale_chunks == {"chunk-2"}
        assert ledger.revision_hints(first.uses, use_sites=first.use_sites) == ()
        assert [
            hint.used
            for hint in ledger.revision_hints(second.uses, use_sites=second.use_sites)
        ] == [OLD]
