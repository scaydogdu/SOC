import asyncio
import json
from pathlib import Path

import pytest
import yaml

from soc.enforce.checker import Checker
from soc.episode import read_yaml
from soc.ledger.models import Actor, OperationAuthority, VoteCast
from soc.orchestrator.ledger import open_run_ledger
from soc.orchestrator.ledger_runner import LedgerRunSpec
from soc.orchestrator.model_triad import ModelTeam, run_model_ledger_episode
from soc.orchestrator.runner import ExperimentSettings
from soc.principal.simulator import PrincipalSimulator
from soc.providers.cache import ResponseCache
from soc.providers.client import ModelClient, Price, ProviderLimits, Route, RunLimits
from soc.providers.models import Mode, Outcome, Reply
from soc.triad.agreement import AgreementRules

SPEC = LedgerRunSpec(
    run_id="model-triad",
    condition="soc",
    episode_id="scope",
    initial_model="family-a/main",
    successor_model="family-a/small",
    effort="low",
    code_commit="test",
    max_in_flight=1,
)
TEAM = ModelTeam(peer_model="family-b/main", reviewer_model="family-c/main")
CLIENT = Actor(agent="client", model="simulator", role="principal")
TERM = "ağ geçidi"


def _episode(root):
    root.mkdir()
    sentences = [
        {
            "id": f"9999-1-s0{position}",
            "section": "1",
            "text": "A gateway forwards packets.",
            "reading_text": "A gateway forwards packets.",
            "scope_keys": ["gateway"],
        }
        for position in range(1, 5)
    ]
    (root / "source.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in sentences)
    )
    (root / "brief.yaml").write_text("clauses: []\n")
    (root / "events.yaml").write_text(
        yaml.safe_dump(
            {
                "chunks": [{"sentence_ids": [row["id"]]} for row in sentences],
                "executor_change_after": 2,
                "brief_change": {"chunk": 3, "clauses": [], "revisions": []},
                "approvals": [],
            }
        )
    )
    return sentences


class _TranslationModels:
    def __init__(self):
        self.calls = []
        self.interrupt = True
        self.retry_review = True

    async def generate(self, request):
        stage = request.schema_name
        body = json.loads(request.messages[1].content)
        self.calls.append((stage, request.model, body))
        if stage == "DraftReply":
            (sentence,) = body["source"]
            (commitments, manifest) = (body["commitments"], [])
            if sentence["id"] == "9999-1-s01":
                assert commitments == []
                word = TERM if request.model == TEAM.peer_model else "ağ kapısı"
            else:
                (ruling,) = commitments
                assert ruling["state"] == "ratified"
                word = ruling["rendering"]
                if request.model == TEAM.peer_model:
                    manifest = [{"ref": ruling["ref"], "sentence_id": sentence["id"]}]
            answer = {
                "sentences": [
                    {"sentence_id": sentence["id"], "text": f"{word} paketleri iletir."}
                ],
                "manifest": manifest,
            }
        elif stage == "ReviewReply":
            if self.interrupt:
                self.interrupt = False
                raise RuntimeError("test interruption before review response")
            if self.retry_review:
                self.retry_review = False
                return Reply(outcome=Outcome.SERVER_ERROR, input_tokens=1)
            answer = {
                "challenge": None
                if body["turns"]
                else {
                    "worker": "peer",
                    "question": "Which source phrase supports your choice?",
                }
            }
        elif stage == "ResponseReply":
            assert request.model == TEAM.peer_model
            answer = {
                "answer": {
                    "rationale": "The sentence names a gateway.",
                    "citations": [
                        {"reference": "source:9999-1-s01", "quote": "gateway"}
                    ],
                }
            }
        elif stage == "DiscussionReply":
            answer = {"remark": "The source describes a network gateway."}
        elif stage == "Classification":
            if body["turns"]:
                assert body["turns"][0]["answer"] is not None
            if body["discussion"]:
                assert len(body["discussion"]) == 3
            answer = {
                "kind": "term",
                "term": {"scope_key": "gateway", "first": "ağ kapısı", "second": TERM},
                "question": None,
            }
        else:
            assert stage == "Ballot"
            answer = {
                "choice": "first" if request.model == SPEC.initial_model else "second"
            }
        return Reply(
            outcome=Outcome.OK,
            raw_text=json.dumps(answer, ensure_ascii=False),
            input_tokens=1,
            output_tokens=1,
        )


def test_model_triad_records_reusable_term_and_resumes_through_shared_client(tmp_path):
    public = tmp_path / "public"
    sentences = _episode(public)
    key = tmp_path / "principal.yaml"
    key.write_text("answers: {}\n")
    principal = PrincipalSimulator(
        key, tmp_path / "answers.sqlite", episode_id="scope", actor=CLIENT
    )
    agreement = read_yaml(Path("config/m2-agreement.yaml"), AgreementRules)
    models = _TranslationModels()
    cache = ResponseCache(tmp_path / "cache.sqlite")
    run_dir = tmp_path / "run"
    model_ids = {
        SPEC.initial_model,
        SPEC.successor_model,
        TEAM.peer_model,
        TEAM.reviewer_model,
    }

    def client(directory, mode):
        return ModelClient(
            run_id=SPEC.run_id,
            run_dir=directory,
            mode=mode,
            cache=cache,
            limits=RunLimits(
                token_cap=1000, initial_backoff_seconds=0, transient_retries=1
            ),
            prices={
                model: Price(input_per_million=0, output_per_million=0)
                for model in model_ids
            },
            routes={model: Route(model.split("/")[0], models) for model in model_ids}
            if mode is Mode.LIVE
            else None,
            provider_limits={
                family: ProviderLimits(requests_per_minute=1e12)
                for family in ("family-a", "family-b", "family-c")
            },
        )

    async def run(directory, provider):
        return await run_model_ledger_episode(
            public,
            directory,
            spec=SPEC,
            experiment=ExperimentSettings(),
            principal=principal,
            checker=Checker(),
            client=provider,
            team=TEAM,
            agreement=agreement,
        )

    with pytest.raises(ExceptionGroup) as stopped:
        asyncio.run(run(run_dir, client(run_dir, Mode.LIVE)))
    (error,) = stopped.value.exceptions
    assert str(error) == "test interruption before review response"
    resumed = client(run_dir, Mode.LIVE)
    result = asyncio.run(run(run_dir, resumed))
    assert result.status == "completed"
    assert len([call for call in models.calls if call[0] == "DraftReply"]) == 8
    assert resumed.tokens == sum(cache.call_usage(SPEC.run_id).values())

    with open_run_ledger(
        public,
        run_dir / "ledger.sqlite",
        SPEC.run_id,
        authority=OperationAuthority(operation="scope", principal=CLIENT.agent),
    ) as (_, ledger):
        state = ledger.snapshot()
        assert ledger.verify() is None
        (term,) = state.current.values()
        assert term.rendering == TERM and term.consistency == "QUORUM"
        assert term.sentence_ids == [row["id"] for row in sentences]
        assert state.usage[term.commitment_id, 1] == [
            "chunk-1",
            "chunk-2",
            "chunk-3",
            "chunk-4",
        ]
        votes = [
            VoteCast.model_validate(event.payload["data"]).choice
            for event in ledger._events()
            if event.type == "VoteCast"
        ]
        assert votes == ["ağ kapısı", TERM, TERM]
        assert sum(event.type == "FlagRaised" for event in ledger._events()) == 1
        assert state.open_flags == {}

    replay_dir = tmp_path / "replay"
    calls = len(models.calls)
    replay = asyncio.run(run(replay_dir, client(replay_dir, Mode.REPLAY)))
    assert replay == result and len(models.calls) == calls
    for name in ("final_chunks.jsonl", "final_findings.jsonl"):
        assert (run_dir / name).read_bytes() == (replay_dir / name).read_bytes()


@pytest.mark.parametrize("resolution", ["open_discussion", "principal_only"])
def test_model_triads_support_remaining_form_modes(tmp_path, resolution):
    public = tmp_path / "public"
    _episode(public)
    key = tmp_path / "principal.yaml"
    key.write_text(
        f'answers:\n  "Which Turkish rendering should we use for gateway?": "{TERM}"\n'
    )
    spec = SPEC.model_copy(update={"resolution": resolution})
    models = _TranslationModels()
    run_dir = tmp_path / "run"
    routes = {
        model: Route(model.split("/")[0], models)
        for model in (
            spec.initial_model,
            spec.successor_model,
            TEAM.peer_model,
            TEAM.reviewer_model,
        )
    }
    client = ModelClient(
        run_id=spec.run_id,
        mode=Mode.LIVE,
        run_dir=run_dir,
        cache=ResponseCache(tmp_path / "cache.sqlite"),
        limits=RunLimits(token_cap=1000),
        prices={
            model: Price(input_per_million=0, output_per_million=0) for model in routes
        },
        routes=routes,
        provider_limits={
            route.family: ProviderLimits(requests_per_minute=1e12)
            for route in routes.values()
        },
    )
    result = asyncio.run(
        run_model_ledger_episode(
            public,
            run_dir,
            spec=spec,
            experiment=ExperimentSettings(),
            principal=PrincipalSimulator(
                key, tmp_path / "answers.sqlite", episode_id="scope", actor=CLIENT
            ),
            checker=Checker(),
            client=client,
            team=TEAM,
            agreement=read_yaml(Path("config/m2-agreement.yaml"), AgreementRules),
        )
    )
    assert result.status == "completed"
    stages = [stage for stage, _, _ in models.calls]
    assert "ReviewReply" not in stages and "ResponseReply" not in stages
    with open_run_ledger(
        public,
        run_dir / "ledger.sqlite",
        spec.run_id,
        authority=OperationAuthority(operation="scope", principal=CLIENT.agent),
    ) as (_, ledger):
        (ruling,) = ledger.snapshot().current.values()
        answers = [
            event for event in ledger._events() if event.type == "PrincipalAnswered"
        ]
        if resolution == "open_discussion":
            assert stages.count("DiscussionReply") == stages.count("Ballot") == 3
            assert ruling.consistency == "QUORUM"
            assert not answers
        else:
            assert "DiscussionReply" not in stages and "Ballot" not in stages
            assert ruling.consistency == "SERIAL"
            assert len(answers) == 1


def test_reusable_term_scope_stops_at_scheduled_principal_approval(tmp_path):
    public = tmp_path / "public"
    sentences = _episode(public)
    path = public / "events.yaml"
    schedule = yaml.safe_load(path.read_text())
    schedule["approvals"] = [
        {
            "decision_id": "D1",
            "value": "yönlendirici",
            "scope_keys": ["gateway"],
            "kind": "term",
            "chunk": 2,
            "brief_clause_ids": [],
        }
    ]
    path.write_text(yaml.safe_dump(schedule))
    with open_run_ledger(public, tmp_path / "term.sqlite", "scope") as (_, ledger):
        assert ledger.reusable_term_sites(sentences[0]["id"], "gateway") == tuple(
            row["id"] for row in sentences[:2]
        )
        assert ledger.reusable_term_sites(sentences[2]["id"], "gateway") == ()
        with pytest.raises(ValueError, match="scope key"):
            ledger.reusable_term_sites(sentences[0]["id"], "unmatched")
