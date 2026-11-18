from pathlib import Path

from soc.enforce.checker import Checker, NormativeRendering
from soc.ledger.models import (
    Actor,
    BriefChanged,
    ChunkBuilt,
    DecisionCaptured,
    DecisionDeclared,
    DecisionSubject,
    DecisionUse,
    GateDecision,
    OperationAuthority,
    RulingApproval,
    VersionRef,
)
from soc.memory import ChunkInput, ChunkOutput, Translation
from soc.orchestrator.ledger import open_run_ledger
from soc.orchestrator.ledger_checks import capture_worker_draft, gate_chunk
from soc.triad.examination import Draft
from tests.ledger_examples import IDENTITY, RULING

TOY = Path("data/episodes/toy")
POLICY = OperationAuthority(operation="toy", principal="client")
CLIENT = Actor(agent="client", model="human", role="principal")
WORKER = Actor(agent="worker", model="model", role="worker")
APPROVAL = RulingApproval(operation="toy", approvers=[CLIENT.agent])
SITE = "9999-1-s01"


def _ratify(ledger, commitment_id, rendering, strength="must"):
    identity = IDENTITY.model_copy(
        update={
            "id": commitment_id,
            "authority": "principal",
            "owner": CLIENT.agent,
            "subject": DecisionSubject(kind="term", key=commitment_id),
        }
    )
    ruling = RULING.model_copy(
        update={
            "rendering": rendering,
            "strength": strength,
            "ratifier": CLIENT.agent,
            "consistency": "SERIAL",
            "sentence_ids": [SITE],
        }
    )
    ledger.ratify(CLIENT, identity, 0, ruling, "binding", approval=APPROVAL)
    return ruling


def _output(chunk, *strings):
    return ChunkOutput(
        sentences=[
            Translation(sentence_id=sentence.id, text=text)
            for sentence, text in zip(chunk.sentences, strings, strict=True)
        ]
    )


def _build(ledger, ref, number=1):
    return ledger.record(
        WORKER,
        ChunkBuilt(
            ledger_version=ledger.snapshot().ledger_version,
            uses=[ref],
            decisions={},
            build_number=number,
            output_ref=f"build-{number}.json",
        ),
        chunk_id="chunk-1",
        base_build=number - 1,
    )


def test_gate_checks_latest_rulings_tombstones_and_strength_by_site(tmp_path):
    with open_run_ledger(TOY, tmp_path / "gate.sqlite", "toy", authority=POLICY) as (
        episode,
        ledger,
    ):
        chunk = ChunkInput(1, (episode.source[0], episode.source[2]))
        must = _ratify(ledger, "required", "erişim jetonu")
        _ratify(ledger, "advised", "birincil istemci", "should")
        _ratify(ledger, "optional", "doğrulama etiketi", "may")
        _build(ledger, VersionRef(commitment_id="required", version=1))
        ledger.transition(
            "Revised",
            CLIENT,
            "required",
            1,
            must.model_copy(
                update={"rendering": "erişim belirteci", "state": "amended"}
            ),
            "replace old wording",
            approval=APPROVAL,
        )
        outputs = {
            "build-1.json": _output(chunk, "erişim jetonu", "erişim jetonu"),
            "build-2.json": _output(chunk, "erişim belirteci", "erişim jetonu"),
        }
        loaded = []

        def load(ref):
            loaded.append(ref)
            return outputs[ref]

        def check(number):
            event = gate_chunk(
                ledger,
                WORKER,
                "chunk-1",
                chunk,
                build_number=number,
                checker=Checker(),
                load_output=load,
                normative=(NormativeRendering("MUST", "zorunludur", "B2"),),
            )
            assert (event.type, event.chunk_id) == ("GateDecision", "chunk-1")
            return GateDecision.model_validate(event.payload["data"])

        blocked = check(1)
        assert not blocked.accepted
        assert [
            (row.rule, row.sentence_id)
            for row in blocked.findings
            if row.outcome == "block"
        ] == [
            ("impact", None),
            ("term", SITE),
            ("forbidden", SITE),
        ]
        _build(ledger, VersionRef(commitment_id="required", version=2), number=2)
        repaired = check(2)
        assert repaired.accepted and repaired.build_number == 2
        assert {
            (row.rule, row.outcome, row.commitment_id) for row in repaired.findings
        } == {
            ("term", "warning", "advised"),
            ("term", "allowed", "optional"),
            ("bcp14", "unknown", None),
        }
        assert "Missing approved rendering" in repaired.reason
        assert loaded == ["build-1.json", "build-2.json"]
        ledger.record(
            CLIENT, BriefChanged(clauses={"B2": "Render MUST as zorunludur."})
        )
        rebound_brief = check(2)
        assert not rebound_brief.accepted
        assert [
            (row.rule, row.outcome)
            for row in rebound_brief.findings
            if row.outcome == "block"
        ] == [("bcp14", "block")]
        del outputs["build-2.json"]
        unavailable = check(2)
        assert not unavailable.accepted and unavailable.findings[0].rule == "artifact"
        ledger.transition(
            "Revised",
            CLIENT,
            "required",
            2,
            must.model_copy(update={"strength": "may", "state": "amended"}),
            "readopt as optional",
            approval=APPROVAL,
        )
        _build(ledger, VersionRef(commitment_id="required", version=3), number=3)
        outputs["build-3.json"] = _output(
            chunk, "erişim jetonu ve erişim belirteci", "zorunludur"
        )
        readopted = check(3)
        assert readopted.accepted
        assert [
            (row.outcome, row.detail)
            for row in readopted.findings
            if row.rule == "forbidden"
        ] == [
            ("allowed", "Forbidden rendering 'erişim belirteci'"),
        ]
        assert ledger.verify() is None


def test_capture_records_verifiable_manifest_entries_and_silent_uses(tmp_path):
    with open_run_ledger(TOY, tmp_path / "capture.sqlite", "toy", authority=POLICY) as (
        episode,
        ledger,
    ):
        chunk = ChunkInput(1, (episode.source[0],))
        must = _ratify(ledger, "required", "erişim jetonu")
        _ratify(ledger, "advised", "birincil istemci", "should")
        ledger.transition(
            "Revised",
            CLIENT,
            "required",
            1,
            must.model_copy(update={"rendering": "erişim belirteci"}),
            "retire old wording",
            approval=APPROVAL,
        )
        old = VersionRef(commitment_id="required", version=1)
        current = VersionRef(commitment_id="required", version=2)
        advised = VersionRef(commitment_id="advised", version=1)
        bogus = VersionRef(commitment_id="unknown", version=1)
        text = "erişim jetonu, erişim belirteci ve birincil istemci"
        draft = Draft(
            WORKER.agent,
            _output(chunk, text),
            manifest=(
                DecisionUse(ref=advised, sentence_id=SITE),
                DecisionUse(ref=bogus, sentence_id=SITE),
            ),
        )
        events = capture_worker_draft(
            ledger, WORKER, "chunk-1", chunk, draft, checker=Checker()
        )
        assert [event.type for event in events] == [
            "DecisionDeclared",
            "DecisionDeclared",
            "DecisionCaptured",
            "DecisionCaptured",
        ]
        declarations = [
            DecisionDeclared.model_validate(event.payload["data"])
            for event in events[:2]
        ]
        assert [(item.ref, item.verified) for item in declarations] == [
            (advised, True),
            (bogus, False),
        ]
        captured = [
            DecisionCaptured.model_validate(event.payload["data"])
            for event in events[2:]
        ]
        assert [(item.ref, item.excerpt) for item in captured] == [
            (current, "erişim belirteci"),
            (old, "erişim jetonu"),
        ]
        assert all(text[item.start : item.end] == item.excerpt for item in captured)
        assert all(
            event.actor == WORKER and event.chunk_id == "chunk-1" for event in events
        )
        assert ledger.snapshot().current["required"].version == 2
        assert ledger.verify() is None
