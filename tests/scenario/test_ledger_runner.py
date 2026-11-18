import asyncio
import json
from pathlib import Path

from soc.enforce.checker import Checker
from soc.ledger.models import Actor, FlagRaised, FlagResolved, OperationAuthority
from soc.memory import ChunkOutput, Translation
from soc.orchestrator.ledger import open_run_ledger
from soc.orchestrator.ledger_runner import (
    LedgerRunSpec,
    WorkerDraft,
    run_ledger_episode,
)
from soc.orchestrator.runner import ExperimentSettings
from soc.principal.simulator import PrincipalSimulator
from soc.providers.models import Outcome
from soc.triad.examination import Draft

TOY = Path("data/episodes/toy")
SPEC = LedgerRunSpec(
    run_id="scripted-soc",
    episode_id="toy",
    condition="soc",
    initial_model="old-fake",
    successor_model="new-fake",
    effort="low",
    code_commit="test",
)


def _worker_output(task):
    rendered = []
    for sentence in task.chunk.sentences:
        terms = [
            version.rendering
            for version in task.commitments.values()
            if sentence.id in (version.sentence_ids or [])
            and (version.rule is None or version.rule.kind != "non_lexical")
        ]
        rendered.append(
            Translation(
                sentence_id=sentence.id,
                text=". ".join(terms) if terms else sentence.text,
            )
        )
    return WorkerDraft(
        Draft(task.actor.agent, ChunkOutput(sentences=rendered)), task.ledger_version
    )


def test_ledger_runner_rebinds_resumes_repairs_and_gates_completed_episode(tmp_path):
    key = tmp_path / "principal.yaml"
    key.write_text("answers: {}\n")
    client = Actor(agent="client", model="simulator", role="principal")
    principal = PrincipalSimulator(
        key, tmp_path / "answers.sqlite", episode_id="toy", actor=client
    )
    run_dir = tmp_path / "run"
    calls = []
    interrupted = True

    async def worker(ledger, task):
        calls.append((task.chunk_id, task.round_number, task.actor.model))
        if task.chunk.position == 2 and task.round_number == 0:
            flag = FlagRaised(
                flag_id="open-1",
                sentence_id=task.chunk.sentences[0].id,
                reason="Resolve later",
            )
            recorded = ledger.record(task.actor, flag, chunk_id=task.chunk_id)
            assert ledger.record(task.actor, flag, chunk_id=task.chunk_id) == recorded
        if task.chunk.position >= 3 or task.round_number > 0:
            assert task.actor.model == "new-fake"
            assert task.handover is not None
            assert task.handover.ledger_version <= task.ledger_version
            assert "open-1" in task.handover.open_flags
            assert {"chunk-1", "chunk-2"} <= task.handover.partial_output.keys()
            assert {row.commitment_id for row in task.handover.commitments} >= {
                "D1",
                "D5",
            }
        if task.chunk.position == 4:
            if interrupted:
                return Outcome.BUDGET_EXHAUSTED
            assert task.commitments["D1"].version == 2
            assert "B2" in {clause.id for clause in task.brief.clauses}
            resolution = FlagResolved(flag_id="open-1")
            recorded = ledger.record(task.actor, resolution, chunk_id="chunk-2")
            assert ledger.record(task.actor, resolution, chunk_id="chunk-2") == recorded
        return _worker_output(task)

    async def run():
        return await run_ledger_episode(
            TOY,
            run_dir,
            spec=SPEC,
            experiment=ExperimentSettings(),
            principal=principal,
            checker=Checker(),
            worker=worker,
        )

    first = asyncio.run(run())
    assert first.status == "budget_exhausted"
    assert list(first.outputs) == ["chunk-1", "chunk-2", "chunk-3"]
    partial = {
        row["chunk"]: row
        for row in map(
            json.loads, (run_dir / "final_findings.jsonl").read_text().splitlines()
        )
    }
    assert set(partial) == {1, 2, 3}
    assert partial[1]["accepted"]
    assert not partial[2]["accepted"] and not partial[3]["accepted"]
    assert "impact" in {item["rule"] for item in partial[2]["findings"]}
    assert any(
        item["rule"] == "term" and "erişim belirteci" in item["detail"]
        for item in partial[2]["findings"]
    )
    interrupted = False
    result = asyncio.run(run())
    assert result.status == "completed"
    assert len(result.outputs) == 4
    assert calls[:5] == [
        ("chunk-1", 0, "old-fake"),
        ("chunk-2", 0, "old-fake"),
        ("chunk-3", 0, "new-fake"),
        ("chunk-4", 0, "new-fake"),
        ("chunk-4", 0, "new-fake"),
    ]
    assert calls[5:] == [("chunk-2", 1, "new-fake"), ("chunk-3", 1, "new-fake")]
    assert "erişim belirteci" in result.outputs["chunk-2"].sentences[0].text

    with open_run_ledger(
        TOY,
        run_dir / "ledger.sqlite",
        SPEC.run_id,
        authority=OperationAuthority(operation="toy", principal="client"),
    ) as (_, ledger):
        assert ledger.verify() is None
        state = ledger.snapshot()
        assert state == ledger.rebuild()
        assert all(gate.accepted for gate in state.gates.values())
        assert state.open_flags == {}
        assert state.first_detection[("chunk-2", 1)] == "final_sweep"
        assert [e.type for e in ledger._events()].count("ExecutorRebound") == 1
        assert [e.type for e in ledger._events()].count("BriefChanged") == 1


def test_ledger_runner_allows_three_chunks_in_flight_after_schedule_barrier(tmp_path):
    import shutil

    import yaml

    from soc.episode import load_runtime

    public = tmp_path / "episode"
    public.mkdir()
    for filename in ("source.jsonl", "brief.yaml", "events.yaml"):
        shutil.copyfile(TOY / filename, public / filename)
    episode = load_runtime(public)
    schedule = episode.events.model_dump(mode="json")
    with (public / "source.jsonl").open("a") as source:
        for index in (5, 6):
            sentence = episode.source[-1].model_copy(
                update={
                    "id": f"9999-2-s0{index}",
                    "text": "Use the public client.",
                    "reading_text": "Use the public client.",
                }
            )
            source.write(sentence.model_dump_json() + "\n")
            schedule["chunks"].append({"sentence_ids": [sentence.id]})
    (public / "events.yaml").write_text(yaml.safe_dump(schedule))
    key = tmp_path / "principal.yaml"
    key.write_text("answers: {}\n")
    principal = PrincipalSimulator(
        key,
        tmp_path / "answers.sqlite",
        episode_id="toy",
        actor=Actor(agent="client", model="simulator", role="principal"),
    )

    async def scenario():
        started = set()
        ready = asyncio.Event()

        async def worker(_ledger, task):
            if task.chunk.position >= 4 and task.round_number == 0:
                assert task.commitments["D1"].version == 2
                started.add(task.chunk.position)
                if started == {4, 5, 6}:
                    ready.set()
                await asyncio.wait_for(ready.wait(), timeout=3)
            return _worker_output(task)

        result = await run_ledger_episode(
            public,
            tmp_path / "run",
            spec=SPEC,
            experiment=ExperimentSettings(),
            principal=principal,
            checker=Checker(),
            worker=worker,
        )
        assert result.status == "completed"
        assert len(result.outputs) == 6

    asyncio.run(scenario())
