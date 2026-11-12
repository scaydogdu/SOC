import asyncio
import gzip
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from pydantic import ValidationError

from soc.baseline.memory import RAGMemory
from soc.baseline.retrieval import HashEmbedder
from soc.enforce.checker import Checker, NormativeRendering
from soc.episode import Brief, load_runtime
from soc.memory import ChunkInput, ChunkOutput
from soc.orchestrator.journal import open_journal
from soc.orchestrator.prompts import digest, json_text
from soc.orchestrator.runner import ExperimentSettings, RunSpec, run_episode
from soc.providers.cache import ResponseCache
from soc.providers.client import (
    CacheMiss,
    ModelClient,
    Price,
    ProviderLimits,
    Route,
    RunLimits,
)
from soc.providers.fake import ScriptedProvider
from soc.providers.models import Mode

ROOT = Path(__file__).resolve().parents[2]
TOY = ROOT / "data" / "episodes" / "toy"
SCRIPT = ROOT / "tests" / "fixtures" / "toy-run.yaml"
SPEC = RunSpec(
    run_id="toy-recording",
    condition="rag",
    episode_id="toy",
    effort="low",
    initial_model="fake-first",
    successor_model="fake-successor",
    code_commit="test-fixture",
)
EXPERIMENT = ExperimentSettings(
    normative=(NormativeRendering("MUST", "zorunludur", "B1"),),
)
PROJECTIONS = (
    "run.json",
    "final_chunks.jsonl",
    "delivered_decisions.jsonl",
    "violations.jsonl",
    "final_findings.jsonl",
    "events.jsonl",
)


def client(directory, cache, *, fake=None, cap=1000):
    models = (SPEC.initial_model, SPEC.successor_model)
    return ModelClient(
        run_id=SPEC.run_id,
        mode=Mode.LIVE if fake else Mode.REPLAY,
        cache=ResponseCache(cache),
        run_dir=directory,
        limits=RunLimits(token_cap=cap, initial_backoff_seconds=0),
        prices={
            model: Price(input_per_million=2, output_per_million=4) for model in models
        },
        routes={model: Route("fake", fake) for model in models} if fake else None,
        provider_limits={"fake": ProviderLimits(requests_per_minute=1e9)}
        if fake
        else None,
    )


def rows(directory, name):
    return [json.loads(line) for line in (directory / name).read_text().splitlines()]


def run(
    directory,
    cache,
    *,
    fake=None,
    cap=1000,
    spec=SPEC,
    experiment=EXPERIMENT,
    episode=TOY,
    prompts=None,
):
    checker = Checker()
    model = client(directory, cache, fake=fake, cap=cap)

    async def worker(request, *, recover):
        if prompts is not None:
            prompts[request.call_id] = [
                message.model_dump() for message in request.messages
            ]
        return await model.generate(request, recover=recover)

    return asyncio.run(
        run_episode(
            episode,
            directory,
            spec=spec,
            experiment=experiment,
            memory=RAGMemory(HashEmbedder(), checker),
            checker=checker,
            client=model,
            worker=worker,
        )
    )


def select_steps(path, indices):
    source = yaml.safe_load(SCRIPT.read_text())
    path.write_text(
        yaml.safe_dump(
            {"steps": [source["steps"][i] for i in indices]}, allow_unicode=True
        )
    )
    return ScriptedProvider(path)


def assert_projections_match(left, right):
    for name in PROJECTIONS:
        assert (left / name).read_bytes() == (right / name).read_bytes(), name


def test_toy_episode_and_network_free_replay(tmp_path, monkeypatch):
    key = "sk-proj-synthetic-ci-secret-928341"
    monkeypatch.setenv("OPENAI_API_KEY", key)
    public = tmp_path / "public"
    public.mkdir()
    for name in ("brief.yaml", "source.jsonl", "events.yaml"):
        shutil.copyfile(TOY / name, public / name)
    recorded = tmp_path / "recorded"
    replay = tmp_path / "replay"
    cache = tmp_path / "cache.sqlite"
    fake = ScriptedProvider(SCRIPT)
    prompts = {}
    result = run(recorded, cache, fake=fake, episode=public, prompts=prompts)
    fake.assert_finished()
    assert result.status == "completed" and result.chunks_completed == 4
    assert result.input_tokens == 80 and result.output_tokens == 40
    assert result.estimated_cost_usd == pytest.approx(0.00032)
    assert run(replay, cache, episode=public) == result
    assert_projections_match(recorded, replay)

    events = rows(recorded, "events.jsonl")
    assert [item["after_chunk"] for item in events] == [1, 1, 1, 1, 1, 2, 3, 3, 3]
    assert events[5]["model"] == "fake-successor"
    assert events[6]["event"] == "brief_changed"
    assert [item["affected_chunks"] for item in events[-2:]] == [[1, 2, 3], [2, 3]]
    delivered = rows(recorded, "delivered_decisions.jsonl")
    assert [item["decisions"] for item in delivered[:1]] == [[]]
    assert len(delivered) == 8
    assert [item["chunk"] for item in delivered] == [1, 2, 2, 3, 1, 2, 3, 4]
    for attempt in delivered:
        messages = prompts[attempt["call_id"]]
        assert digest(json_text(messages)) == attempt["prompt_hash"]
        lines = messages[-1]["content"].splitlines()
        refs = set()
        for index, title in enumerate(lines):
            if title in {
                "Glossary",
                "Style guide",
                "Retrieved notes",
                "Current note index",
            }:
                payload = json.loads(lines[index + 1])
                notes = payload["decisions"] if title == "Style guide" else payload
                refs.update((note["decision_id"], note["version"]) for note in notes)
        assert refs == {
            (ref["decision_id"], ref["version"]) for ref in attempt["decisions"]
        }
    for attempt in delivered[1:]:
        versions = {
            item["decision_id"]: item["version"] for item in attempt["decisions"]
        }
        assert set(versions) == {"D1", "D2", "D3", "D4", "D5"}
        revised = attempt["call_id"] in {
            "chunk-1-round-1",
            "chunk-2-round-2",
            "chunk-3-round-1",
            "chunk-4-round-0",
        }
        assert versions["D1"] == versions["D5"] == (2 if revised else 1)
    attempts = rows(recorded, "violations.jsonl")
    assert [finding["rule"] for finding in attempts[1]["findings"]] == ["term"]
    assert not any(row["findings"] for row in rows(recorded, "final_findings.jsonl"))

    # Re-score the saved final chunks with a fresh checker and the reconstructed brief.
    episode = load_runtime(public)
    checker = Checker()
    memory = RAGMemory(HashEmbedder(), checker)
    for approval in episode.events.approvals:
        memory.record_approval(approval, episode.brief)
    current = Brief(
        clauses=[*episode.brief.clauses, *episode.events.brief_change.clauses]
    )
    for revision in episode.events.brief_change.revisions:
        memory.apply_revision(revision, current)
    source = {item.id: item for item in episode.source}
    rescored = []
    for row in rows(recorded, "final_chunks.jsonl"):
        position = row["chunk"]
        ids = episode.events.chunks[position - 1].sentence_ids
        chunk = ChunkInput(position, tuple(source[key] for key in ids))
        output = ChunkOutput.model_validate({"sentences": row["sentences"]})
        findings = checker.check(
            chunk.sentences,
            output,
            current,
            decisions=memory.decisions_for(chunk),
            normative=EXPERIMENT.normative,
        )
        rescored.append(
            {"chunk": position, "findings": [asdict(item) for item in findings]}
        )
    assert rescored == rows(recorded, "final_findings.jsonl")

    key_pattern = re.compile(rb"(?:sk-proj-|sk-ant-|AIza)[a-zA-Z0-9_-]{10,}")
    assert not key_pattern.search(json_text(prompts).encode())
    for path in [
        cache,
        *recorded.iterdir(),
        *replay.iterdir(),
        *ROOT.joinpath("prompts").glob("*.txt"),
    ]:
        content = (
            gzip.decompress(path.read_bytes())
            if path.suffix == ".gz"
            else path.read_bytes()
        )
        assert key.encode() not in content
        assert not key_pattern.search(content)


def test_budget_exhaustion_keeps_and_scores_partial_output(tmp_path):
    cache = tmp_path / "cache.sqlite"
    fake = select_steps(tmp_path / "budget.yaml", [0, 1])
    recorded = tmp_path / "partial"
    result = run(recorded, cache, fake=fake, cap=30)
    fake.assert_finished()
    assert result.status == "budget_exhausted"
    assert result.chunks_completed == 2
    assert rows(recorded, "final_findings.jsonl")[1]["findings"][0]["rule"] == "term"
    replay = tmp_path / "replay"
    assert run(replay, cache, cap=30) == result
    assert_projections_match(recorded, replay)


def test_shared_repair_cap_includes_draft_and_post_revision_repairs(tmp_path):
    settings = ExperimentSettings(max_repair_rounds=1, normative=EXPERIMENT.normative)
    for label in ("rag", "alternate-memory"):
        directory = tmp_path / label
        fake = select_steps(tmp_path / f"{label}.yaml", [0, 1, 2, 3, 4, 6, 7])
        spec = SPEC.model_copy(update={"condition": label})
        assert (
            run(
                directory,
                directory / "cache.sqlite",
                fake=fake,
                experiment=settings,
                spec=spec,
            ).status
            == "completed"
        )
        fake.assert_finished()
        rows_by_chunk = rows(directory, "violations.jsonl")
        assert max(row["round"] for row in rows_by_chunk) == 1
        assert sum(row["chunk"] == 2 for row in rows_by_chunk) == 2
        assert rows(directory, "final_findings.jsonl")[1]["findings"]
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RunSpec.model_validate(SPEC.model_dump() | {"max_repair_rounds": 2})


@pytest.mark.parametrize("kill_before_summary", [True, False])
def test_process_killed_after_cache_write_resumes_without_repeating_call(
    tmp_path, kill_before_summary
):
    directory = tmp_path / "interrupted"
    cache = tmp_path / "cache.sqlite"
    killed = subprocess.run(
        [
            sys.executable,
            "-m",
            "tests.scenario.test_runner",
            str(directory),
            str(cache),
            str(int(kill_before_summary)),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert killed.returncode == -signal.SIGKILL, killed.stderr
    assert json.loads((directory / "run.json").read_text())["status"] == "running"
    with sqlite3.connect(directory / "journal.sqlite") as journal:
        assert journal.execute(
            "SELECT call_id, result IS NOT NULL FROM attempts ORDER BY call_id"
        ).fetchall() == [
            ("chunk-1-round-0", 1),
            ("chunk-2-round-0", 0),
        ]
    remaining = select_steps(tmp_path / "remaining.yaml", range(2, 8))
    result = run(directory, cache, fake=remaining)
    remaining.assert_finished()
    assert result.status == "completed" and result.output_tokens == 40
    replay = tmp_path / "replay"
    assert run(replay, cache) == result
    assert_projections_match(directory, replay)
    with gzip.open(directory / "calls.jsonl.gz", "rt") as log:
        attempts = [json.loads(line) for line in log]
    assert len([row for row in attempts if row["attempt"] is not None]) == 8
    summaries = [row for row in attempts if row["attempt"] is None]
    assert len(summaries) == len({row["call_id"] for row in summaries}) == 8


def test_resume_rejects_configuration_changes_and_concurrent_writer(tmp_path):
    directory = tmp_path / "run"
    cache = tmp_path / "cache.sqlite"
    fake = ScriptedProvider(SCRIPT)
    assert run(directory, cache, fake=fake).status == "completed"
    before = (directory / "run.json").read_bytes()
    with pytest.raises(ValueError, match="configuration"):
        run(directory, cache, experiment=ExperimentSettings(max_repair_rounds=1))
    assert (directory / "run.json").read_bytes() == before
    with (
        open_journal(tmp_path / "locked", "identity"),
        pytest.raises(RuntimeError, match="another process"),
    ):
        with open_journal(tmp_path / "locked", "identity"):
            pytest.fail("second writer obtained the run directory")


def test_replay_miss_is_loud_and_recorded_as_failed(tmp_path):
    directory = tmp_path / "miss"
    with pytest.raises(CacheMiss):
        run(directory, tmp_path / "cache.sqlite")
    assert json.loads((directory / "run.json").read_text())["status"] == "failed"


async def _interrupted(directory: Path, cache: Path, kill_before_summary: bool) -> None:
    fake = ScriptedProvider(SCRIPT)
    model = client(directory, cache, fake=fake)
    checker = Checker()

    async def worker(request, *, recover):
        response = await model.generate(request, recover=recover)
        if request.call_id == "chunk-2-round-0" and not kill_before_summary:
            os.kill(os.getpid(), signal.SIGKILL)
        return response

    log = model._log

    def interrupt_log(request, *args, attempt):
        if (
            kill_before_summary
            and request.call_id == "chunk-2-round-0"
            and attempt is None
        ):
            os.kill(os.getpid(), signal.SIGKILL)
        log(request, *args, attempt=attempt)

    with patch.object(model, "_log", interrupt_log):
        await run_episode(
            TOY,
            directory,
            spec=SPEC,
            experiment=EXPERIMENT,
            client=model,
            checker=checker,
            memory=RAGMemory(HashEmbedder(), checker),
            worker=worker,
        )


if __name__ == "__main__":
    asyncio.run(_interrupted(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3] == "1"))
