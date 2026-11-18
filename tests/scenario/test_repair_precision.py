import asyncio
import json
from pathlib import Path

import pytest

from eval.episode import validate
from eval.repair_precision import paired_repair_precision
from soc.baseline.retrieval import HashEmbedder
from soc.enforce.checker import Checker
from soc.episode import read_yaml
from soc.ledger.models import Actor
from soc.orchestrator.ledger_runner import LedgerRunSpec
from soc.orchestrator.model_rag import run_model_rag_episode
from soc.orchestrator.model_triad import ModelTeam, run_model_ledger_episode
from soc.orchestrator.runner import ExperimentSettings
from soc.principal.simulator import PrincipalSimulator
from soc.providers.cache import ResponseCache
from soc.providers.client import ModelClient, Price, ProviderLimits, Route, RunLimits
from soc.providers.models import Mode, Outcome, Reply
from soc.triad.agreement import AgreementRules

TOY = Path("data/episodes/toy")


def test_paired_model_arms_measure_brief_change_repair_selection(tmp_path):
    key = tmp_path / "principal.yaml"
    key.write_text("answers: {}\n")
    principal = PrincipalSimulator(
        key,
        tmp_path / "principal.sqlite",
        episode_id="toy",
        actor=Actor(agent="client", model="simulator", role="principal"),
    )
    agreement = read_yaml(Path("config/m2-agreement.yaml"), AgreementRules)

    class FakeTriad:
        async def generate(self, request):
            assert request.schema_name == "DraftReply"
            body = json.loads(request.messages[1].content)
            rendered = []
            for sentence in body["source"]:
                terms = [
                    row["rendering"]
                    for row in body["commitments"]
                    if sentence["id"] in row["sentence_ids"]
                    and row["ref"]["commitment_id"] != "D4"
                ]
                if sentence["id"] == "9999-1-s01":
                    # Before scheduled approval, this rendering can match the
                    # baseline's later text search without having used a ruling.
                    terms.append("erişim jetonu")
                rendered.append(
                    {
                        "sentence_id": sentence["id"],
                        "text": ". ".join(terms) if terms else sentence["text"],
                    }
                )
            return Reply(
                outcome=Outcome.OK,
                raw_text=json.dumps(
                    {"sentences": rendered, "manifest": []}, ensure_ascii=False
                ),
                input_tokens=1,
                output_tokens=1,
            )

    models = FakeTriad()
    routes = {
        model: Route(model[:1], models)
        for model in ("a-executor", "a-successor", "b-peer", "c-reviewer")
    }

    async def run(arm):
        spec = LedgerRunSpec(
            run_id=f"paired-{arm}",
            condition=arm,
            episode_id="toy",
            initial_model="a-executor",
            successor_model="a-successor",
            effort="low",
            code_commit="test",
            max_in_flight=1,
        )
        directory = tmp_path / arm
        client = ModelClient(
            run_id=spec.run_id,
            mode=Mode.LIVE,
            cache=ResponseCache(tmp_path / "reply.sqlite"),
            run_dir=directory,
            limits=RunLimits(token_cap=1000),
            prices={
                model: Price(input_per_million=0, output_per_million=0)
                for model in routes
            },
            routes=routes,
            provider_limits={
                family: ProviderLimits(requests_per_minute=1e12)
                for family in ("a", "b", "c")
            },
        )
        options = dict(
            spec=spec,
            experiment=ExperimentSettings(),
            principal=principal,
            checker=Checker(),
            client=client,
            team=ModelTeam("b-peer", "c-reviewer"),
            agreement=agreement,
        )
        if arm == "soc":
            return await run_model_ledger_episode(TOY, directory, **options)
        return await run_model_rag_episode(
            TOY, directory, **options, embedder=HashEmbedder()
        )

    for arm in ("rag", "soc"):
        assert asyncio.run(run(arm)).status == "completed"

    report = paired_repair_precision(validate(TOY), tmp_path / "rag", tmp_path / "soc")
    assert report["unit"] == "decision_chunk"
    assert report["after_chunk"] == 3
    assert report["rag"]["decisions"]["D1"] == {
        "selected": 3,
        "correct": 1,
        "expected": 1,
        "precision": 1 / 3,
    }
    assert report["soc"]["decisions"]["D1"] == {
        "selected": 2,
        "correct": 1,
        "expected": 1,
        "precision": 1 / 2,
    }
    assert report["soc"]["decisions"]["D5"] == report["rag"]["decisions"]["D5"]
    assert report["soc"]["total"]["correct"] == report["rag"]["total"]["correct"] == 2

    manifest = tmp_path / "rag" / "run.json"
    data = json.loads(manifest.read_text())
    data["episode_sha256"] = "different episode"
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="supplied public episode"):
        paired_repair_precision(validate(TOY), tmp_path / "rag", tmp_path / "soc")
