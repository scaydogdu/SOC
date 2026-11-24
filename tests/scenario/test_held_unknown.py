import asyncio
import json
from pathlib import Path

import pytest
import yaml

from soc.baseline.memory import RAGMemory
from soc.baseline.retrieval import HashEmbedder
from soc.enforce.checker import Checker, Finding
from soc.enforce.meaning import (
    ModelMeaningResolver,
)
from soc.episode import EpisodeError, load_runtime, validate_runtime
from soc.ledger.models import Actor, OperationAuthority
from soc.memory import ChunkOutput, Translation
from soc.orchestrator.ledger import open_run_ledger
from soc.orchestrator.ledger_checks import _gate_finding
from soc.orchestrator.ledger_runner import (
    LedgerRunSpec,
    WorkerDraft,
    run_ledger_episode,
)
from soc.orchestrator.runner import ExperimentSettings, run_episode
from soc.principal.simulator import PrincipalSimulator
from soc.providers.cache import ResponseCache
from soc.providers.client import ModelClient, Price, ProviderLimits, Route, RunLimits
from soc.providers.models import Mode, Outcome, Reply
from soc.triad.examination import Draft

TOY = Path("data/episodes/toy")


def test_evaluation_preflight_names_missing_modal_decision_and_fixture_is_explicit():
    episode = load_runtime(TOY)
    evaluation = episode.model_copy(
        update={"brief": episode.brief.model_copy(update={"fixture": False})}
    )
    with pytest.raises(EpisodeError, match="D4.*MUST"):
        validate_runtime(evaluation, require_checkable=True)
    assert validate_runtime(episode, require_checkable=True) == episode


@pytest.mark.parametrize(
    "strength,outcome", [("must", "held"), ("should", "unknown"), ("may", "unknown")]
)
def test_unknown_must_is_held_without_changing_weaker_strengths(strength, outcome):
    finding = Finding(
        "bcp14",
        "9999-1-s03",
        "Runtime meaning undecided",
        strength=strength,
        status="unknown",
    )
    assert _gate_finding(finding).outcome == outcome


def public_episode(directory):
    directory.mkdir()
    rows = [
        {
            "id": f"9999-{i}-s01",
            "section": str(i),
            "text": "The client MUST keep 3 tokens.",
            "reading_text": "The client MUST keep 3 tokens.",
            "scope_keys": ["MUST"],
        }
        for i in range(1, 5)
    ]
    (directory / "source.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows)
    )
    (directory / "brief.yaml").write_text(
        yaml.safe_dump(
            {
                "fixture": True,
                "clauses": [{"id": "B1", "text": "Preserve normative force."}],
            }
        )
    )
    (directory / "events.yaml").write_text(
        yaml.safe_dump(
            {
                "chunks": [{"sentence_ids": [row["id"]]} for row in rows],
                "executor_change_after": 2,
                "brief_change": {"chunk": 3, "clauses": [], "revisions": []},
                "approvals": [],
            }
        )
    )


class Models:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = []
        self.prompts = []

    async def generate(self, request):
        self.calls.append(request.schema_name)
        self.prompts.append("\n".join(row.content for row in request.messages))
        if request.schema_name == "MeaningReply":
            if self.verdict == "unavailable":
                return Reply(outcome=Outcome.SERVER_ERROR, input_tokens=1)
            data = json.loads(request.messages[-1].content)
            text = data["translation"]
            verdict = self.verdict
            if verdict == "repair":
                verdict = "compliant" if "korumalıdır" in text else "violation"
            evidence = [{"reference": f"output:{data['source']['id']}", "quote": text}]
            if verdict == "bad-evidence":
                verdict, evidence = (
                    "compliant",
                    [
                        {
                            "reference": "source:nonexistent",
                            "quote": "PRIVATE_CANARY_NOT_SOURCE",
                        }
                    ],
                )
            raw = {
                "verdict": verdict,
                "rationale": "Scripted runtime meaning check",
                "evidence": evidence,
            }
        else:
            source = json.JSONDecoder().raw_decode(
                request.messages[-1].content.split("Source sentences\n", 1)[1]
            )[0]
            repaired = "Previous output" in request.messages[-1].content
            raw = {
                "sentences": [
                    {
                        "sentence_id": row["sentence_id"],
                        "text": "İstemci 3 jetonu korumalıdır."
                        if repaired
                        else "İstemci 3 jetonu korur.",
                    }
                    for row in source
                ]
            }
        return Reply(
            outcome=Outcome.OK,
            raw_text=json.dumps(raw, ensure_ascii=False),
            input_tokens=1,
            output_tokens=1,
        )


def execute(root, arm, verdict, *, replay=False, output="record", cache=None):
    public = root / "public"
    if not public.exists():
        public_episode(public)
    directory = root / output
    spec = LedgerRunSpec(
        run_id=f"held-{arm}",
        condition=arm,
        episode_id="held",
        initial_model="draft",
        successor_model="next",
        effort="low",
        code_commit="test",
        max_in_flight=1,
    )
    models = Models(verdict)
    routes = {model: Route("fake", models) for model in ("draft", "next", "meaning")}
    client = ModelClient(
        run_id=spec.run_id,
        run_dir=directory,
        mode=Mode.REPLAY if replay else Mode.LIVE,
        cache=ResponseCache(cache or root / "cache.sqlite"),
        prices={
            model: Price(input_per_million=0, output_per_million=0) for model in routes
        },
        routes=None if replay else routes,
        limits=RunLimits(
            token_cap=1000, transient_retries=0, initial_backoff_seconds=0
        ),
        provider_limits={"fake": ProviderLimits(requests_per_minute=1e12)},
    )
    resolver = (
        None
        if verdict is None
        else ModelMeaningResolver(client, "meaning", "low", 0, 768)
    )
    calls = []

    async def worker(_ledger, task):
        calls.append((task.chunk_id, task.round_number))
        text = (
            "İstemci 3 jetonu korumalıdır."
            if task.round_number
            else "İstemci 3 jetonu korur."
        )
        return WorkerDraft(
            Draft(
                task.actor.agent,
                ChunkOutput(
                    sentences=[
                        Translation(sentence_id=row.id, text=text)
                        for row in task.chunk.sentences
                    ]
                ),
            ),
            task.ledger_version,
        )

    if arm == "soc":
        key = root / "key.yaml"
        key.write_text("answers: {}\n")
        result = asyncio.run(
            run_ledger_episode(
                public,
                directory,
                spec=spec,
                experiment=ExperimentSettings(),
                principal=PrincipalSimulator(
                    key,
                    root / "principal.sqlite",
                    episode_id="held",
                    actor=Actor(agent="principal", model="simulator", role="principal"),
                ),
                checker=Checker(),
                worker=worker,
                meaning_resolver=resolver,
            )
        )
    else:
        checker = Checker()
        result = asyncio.run(
            run_episode(
                public,
                directory,
                spec=spec,
                experiment=ExperimentSettings(),
                memory=RAGMemory(HashEmbedder(), checker),
                checker=checker,
                client=client,
                meaning_resolver=resolver,
            )
        )
    return result, models, calls


@pytest.mark.parametrize("arm", ["soc", "rag"])
@pytest.mark.parametrize(
    "verdict,status",
    [
        (None, "unverified"),
        ("unknown", "unverified"),
        ("bad-evidence", "unverified"),
        ("compliant", "completed"),
        ("repair", "completed"),
        ("violation", "blocked"),
    ],
)
def test_both_arms_hold_without_repair_resolve_and_publish_unverified(
    tmp_path, arm, verdict, status
):
    result, models, calls = execute(tmp_path, arm, verdict)
    assert result.status == status
    directory = tmp_path / "record"
    assembly = json.loads((directory / "assembly.json").read_text())
    assert assembly["accepted"] == (status == "completed")
    report = json.loads((directory / "unknowns.json").read_text())
    assert report["unknown_must"] >= 4 and report["configuration_unknowns"] >= 4
    assert report["must_checks"] >= 12 and not report["pilot_b_unknowns_pass"]
    if verdict in {None, "unknown", "bad-evidence", "compliant"}:
        assert (
            len(calls) == 4 if arm == "soc" else models.calls.count("ChunkOutput") == 4
        )
    rows = [
        json.loads(line)
        for line in (directory / "final_findings.jsonl").read_text().splitlines()
    ]
    if status == "unverified":
        assert all(
            any(
                row.get("outcome", row.get("status")) == "unverified"
                for row in chunk["findings"]
            )
            for chunk in rows
        )
    if verdict is not None:
        if arm == "soc":
            with open_run_ledger(
                tmp_path / "public",
                directory / "ledger.sqlite",
                "held-soc",
                authority=OperationAuthority(operation="held", principal="principal"),
            ) as (_, ledger):
                assert ledger.verify() is None
                assert any(row.type == "HeldResolved" for row in ledger.history())
                assert not ledger.snapshot().stale_chunks
                assert ledger.snapshot() == ledger.rebuild()
        else:
            assert any(
                json.loads(line)["event"] == "HeldResolved"
                for line in (directory / "events.jsonl").read_text().splitlines()
            )


@pytest.mark.parametrize("arm", ["soc", "rag"])
@pytest.mark.parametrize("verdict", ["compliant", "repair", "unknown"])
def test_held_resolution_replays_byte_identically_and_resumes_without_calls(
    tmp_path, arm, verdict
):
    first, _, _ = execute(tmp_path, arm, verdict)
    replay, models, _ = execute(tmp_path, arm, verdict, replay=True, output="replay")
    assert replay == first and models.calls == []
    for name in (
        "final_chunks.jsonl",
        "final_findings.jsonl",
        "unknowns.json",
        "assembly.json",
    ):
        assert (tmp_path / "record" / name).read_bytes() == (
            tmp_path / "replay" / name
        ).read_bytes()
    resumed, models, _ = execute(tmp_path, arm, verdict)
    assert resumed == first and models.calls == []


def test_a_real_violation_still_blocks_when_other_findings_are_held(tmp_path):
    public = tmp_path / "public"
    public_episode(public)
    key = tmp_path / "key.yaml"
    key.write_text("answers: {}\n")
    spec = LedgerRunSpec(
        run_id="mixed",
        condition="soc",
        episode_id="held",
        initial_model="draft",
        successor_model="next",
        effort="low",
        code_commit="test",
        max_in_flight=1,
    )

    async def worker(_ledger, task):
        return WorkerDraft(
            Draft(
                task.actor.agent,
                ChunkOutput(
                    sentences=[
                        Translation(sentence_id=row.id, text="7 jeton")
                        for row in task.chunk.sentences
                    ]
                ),
            ),
            task.ledger_version,
        )

    result = asyncio.run(
        run_ledger_episode(
            public,
            tmp_path / "run",
            spec=spec,
            experiment=ExperimentSettings(max_repair_rounds=0),
            principal=PrincipalSimulator(
                key,
                tmp_path / "principal.sqlite",
                episode_id="held",
                actor=Actor(agent="principal", model="simulator", role="principal"),
            ),
            checker=Checker(),
            worker=worker,
        )
    )
    assert result.status == "blocked"
    rows = [
        json.loads(line)
        for line in (tmp_path / "run/final_findings.jsonl").read_text().splitlines()
    ]
    assert all(
        {row["outcome"] for row in chunk["findings"]} == {"block", "unverified"}
        for chunk in rows
    )
