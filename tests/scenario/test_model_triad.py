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


def test_rag_uses_same_model_protocol_and_delivers_reusable_term_notes(tmp_path):
    from soc.baseline.retrieval import HashEmbedder
    from soc.orchestrator.model_rag import run_model_rag_episode

    public = tmp_path / "public"
    _episode(public)
    key = tmp_path / "principal.yaml"
    key.write_text("answers: {}\n")
    principal = PrincipalSimulator(
        key, tmp_path / "answers.sqlite", episode_id="scope", actor=CLIENT
    )
    models = _TranslationModels()
    models.interrupt = models.retry_review = False
    cache = ResponseCache(tmp_path / "cache.sqlite")
    routes = {
        model: Route(model.split("/")[0], models)
        for model in (
            SPEC.initial_model,
            SPEC.successor_model,
            TEAM.peer_model,
            TEAM.reviewer_model,
        )
    }

    async def run(dir, mode):
        return await run_model_rag_episode(
            public,
            dir,
            spec=SPEC.model_copy(update={"condition": "rag"}),
            experiment=ExperimentSettings(),
            principal=principal,
            checker=Checker(),
            client=ModelClient(
                run_id=SPEC.run_id,
                mode=mode,
                run_dir=dir,
                cache=cache,
                limits=RunLimits(token_cap=1000),
                prices={
                    model: Price(input_per_million=0, output_per_million=0)
                    for model in routes
                },
                routes=routes if mode is Mode.LIVE else None,
                provider_limits={
                    route.family: ProviderLimits(requests_per_minute=1e12)
                    for route in routes.values()
                },
            ),
            team=TEAM,
            agreement=read_yaml(Path("config/m2-agreement.yaml"), AgreementRules),
            embedder=HashEmbedder(),
        )

    recorded = tmp_path / "rag"
    result = asyncio.run(run(recorded, Mode.LIVE))
    assert result.status == "completed" and result.chunks_completed == 4
    assert result.input_tokens + result.output_tokens == sum(
        cache.call_usage(SPEC.run_id).values()
    )
    assert len([stage for stage, _, _ in models.calls if stage == "Ballot"]) == 3
    later = [
        body
        for stage, _, body in models.calls
        if stage == "DraftReply" and body["source"][0]["id"] == "9999-1-s03"
    ]
    assert len(later) == 2 and all(TERM in body["retrieved_context"] for body in later)
    assert any(
        "Partial translation" in body.get("memory_handover", "") for body in later
    )
    calls = len(models.calls)
    assert asyncio.run(run(recorded, Mode.LIVE)) == result
    replay = tmp_path / "replay"
    assert asyncio.run(run(replay, Mode.REPLAY)) == result
    assert len(models.calls) == calls
    for name in ("final_chunks.jsonl", "final_findings.jsonl", "events.jsonl"):
        assert (recorded / name).read_bytes() == (replay / name).read_bytes()
    assert not (recorded / "ledger.sqlite").exists()


def test_normative_dissent_escalates_and_offline_audit_counts_votes_on_brief_gaps(
    tmp_path,
):
    from eval.brief_gap import brief_gap_votes, read_resolutions
    from soc.baseline.retrieval import HashEmbedder
    from soc.enforce.checker import NormativeRendering
    from soc.orchestrator.model_rag import run_model_rag_episode

    public = tmp_path / "public"
    sentences = _episode(public)
    normative, mislabeled, recognized, _ = [row["id"] for row in sentences]
    sentences[0]["text"] = sentences[0]["reading_text"] = (
        "The gateway MUST forward packets."
    )
    (public / "source.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in sentences)
    )
    (public / "brief.yaml").write_text(
        'clauses: [{id: B1, text: "Use zorunludur for MUST."}]\n'
    )

    key = tmp_path / "principal.yaml"
    accepted_normative = "Paketlerin ağ geçidi tarafından iletilmesi zorunludur."
    accepted_gap = "Tablo başlığı kısa kalır."
    key.write_text(
        yaml.safe_dump(
            {
                "answers": {
                    "How must the gateway requirement read?": accepted_normative,
                    "How long should the heading be?": accepted_gap,
                }
            },
            allow_unicode=True,
        )
    )
    principal = PrincipalSimulator(
        key, tmp_path / "answers.sqlite", episode_id="scope", actor=CLIENT
    )
    calls = []

    class Script:
        async def generate(self, request):
            body = json.loads(request.messages[1].content)
            stage = request.schema_name
            if stage == "DraftReply":
                (source,) = body["source"]
                sid = source["id"]
                by_sentence = {
                    normative: (
                        "Ağ geçidinin paket iletmesi zorunludur.",
                        "Ağ geçidi paket iletebilir.",
                    ),
                    mislabeled: (
                        "Bölüm başlığı açıklayıcıdır.",
                        "Bölüm başlığı kısadır.",
                    ),
                    recognized: (
                        "Tablo başlığı uzundur.",
                        "Tablo başlığı kısa olabilir.",
                    ),
                }
                choices = by_sentence.get(
                    sid, ("Paketler iletilir.", "Paketler iletilir.")
                )
                answer = {
                    "sentences": [
                        {
                            "sentence_id": sid,
                            "text": choices[int(request.model == TEAM.peer_model)],
                        }
                    ],
                    "manifest": [],
                }
            else:
                sid = body["difference"]["sentence_id"]
                if stage == "ReviewReply":
                    answer = {"challenge": None}
                elif stage == "Classification":
                    kind, question = {
                        normative: (
                            "normative",
                            "How must the gateway requirement read?",
                        ),
                        mislabeled: ("style", None),
                        recognized: ("brief_gap", "How long should the heading be?"),
                    }[sid]
                    answer = {"kind": kind, "term": None, "question": question}
                else:
                    assert stage == "Ballot" and sid != recognized
                    answer = {
                        "choice": "second"
                        if request.model == TEAM.peer_model
                        else "first"
                    }
            calls.append((stage, sid))
            return Reply(
                outcome=Outcome.OK,
                raw_text=json.dumps(answer, ensure_ascii=False),
                input_tokens=1,
                output_tokens=1,
            )

    routes = {
        model: Route(model.split("/")[0], Script())
        for model in (
            SPEC.initial_model,
            SPEC.successor_model,
            TEAM.peer_model,
            TEAM.reviewer_model,
        )
    }
    agreement = read_yaml(Path("config/m2-agreement.yaml"), AgreementRules)

    async def run(arm):
        spec = SPEC.model_copy(update={"run_id": f"resolution-{arm}", "condition": arm})
        directory = tmp_path / arm
        client = ModelClient(
            run_id=spec.run_id,
            mode=Mode.LIVE,
            run_dir=directory,
            cache=ResponseCache(tmp_path / "model.sqlite"),
            limits=RunLimits(token_cap=1000),
            prices={
                model: Price(input_per_million=0, output_per_million=0)
                for model in routes
            },
            routes=routes,
            provider_limits={
                route.family: ProviderLimits(requests_per_minute=1e12)
                for route in routes.values()
            },
        )
        options = dict(
            spec=spec,
            experiment=ExperimentSettings(
                normative=(NormativeRendering("MUST", "zorunludur", "B1"),)
            ),
            principal=principal,
            checker=Checker(),
            client=client,
            team=TEAM,
            agreement=agreement,
        )
        if arm == "rag":
            return await run_model_rag_episode(
                public, directory, **options, embedder=HashEmbedder()
            )
        return await run_model_ledger_episode(public, directory, **options)

    traces_by_arm = {}
    for arm in ("rag", "soc"):
        assert asyncio.run(run(arm)).status == "completed"
        directory = tmp_path / arm
        traces = list(read_resolutions(directory, arm))
        traces_by_arm[arm] = traces
        normative_trace, style_trace, gap_trace = traces
        assert normative_trace.sentence_id == normative
        assert (
            normative_trace.required == "ALL"
            and normative_trace.consistency == "SERIAL"
        )
        assert [ballot.choice for ballot in normative_trace.ballots] == [
            "first",
            "second",
            "first",
        ]
        assert normative_trace.approvers == [CLIENT.agent]
        assert normative_trace.rendering == accepted_normative
        assert (
            style_trace.decision_kind == "style" and style_trace.consistency == "QUORUM"
        )
        assert gap_trace.required == gap_trace.consistency == "SERIAL"
        assert gap_trace.ballots == []
        final = [
            json.loads(line)["sentences"][0]["text"]
            for line in (directory / "final_chunks.jsonl").read_text().splitlines()
        ]
        assert final[0] == accepted_normative and final[2] == accepted_gap
        assert brief_gap_votes(traces, {mislabeled, recognized}) == {
            "resolved_brief_gaps": 2,
            "brief_gaps_voted_on": 1,
        }
    assert traces_by_arm["rag"] == traces_by_arm["soc"]
    assert calls.count(("Ballot", normative)) == 6
    assert calls.count(("Ballot", recognized)) == 0


def test_model_cross_examination_receives_ledger_revision_on_next_step(tmp_path):
    from soc.ledger.models import DecisionSubject, RulingApproval
    from soc.memory import ChunkInput
    from soc.orchestrator.ledger_runner import ChunkTask, WorkerDraft
    from soc.orchestrator.model_triad import ModelTriad
    from tests.ledger_examples import IDENTITY, RULING

    public = Path("data/episodes/toy")
    key = tmp_path / "principal.yaml"
    key.write_text("answers: {}\n")
    principal = PrincipalSimulator(
        key, tmp_path / "answers.sqlite", episode_id="toy", actor=CLIENT
    )
    identity = IDENTITY.model_copy(
        update={
            "authority": "principal",
            "owner": CLIENT.agent,
            "subject": DecisionSubject(kind="term", key="access token"),
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
    replacement = "erişim belirteci"
    revision_seq = None

    with open_run_ledger(
        public,
        tmp_path / "ledger.sqlite",
        SPEC.run_id,
        authority=OperationAuthority(operation="toy", principal=CLIENT.agent),
    ) as (episode, ledger):
        first = ledger.ratify(
            CLIENT, identity, 0, ruling, "original", approval=approval
        )
        snapshot = ledger.snapshot()

        class Script:
            async def generate(self, request):
                nonlocal revision_seq
                stage = request.schema_name
                body = json.loads(request.messages[1].content)
                if stage == "DraftReply":
                    answer = {
                        "sentences": [
                            {
                                "sentence_id": "9999-1-s01",
                                "text": replacement
                                if request.model == TEAM.peer_model
                                else ruling.rendering,
                            }
                        ],
                        "manifest": [],
                    }
                elif stage == "ReviewReply" and not body["turns"]:
                    assert body["revision_hints"] == []
                    assert body["commitments"][0]["ref"]["version"] == 1
                    revision_seq = ledger.transition(
                        "Revised",
                        CLIENT,
                        identity.id,
                        1,
                        ruling.model_copy(
                            update={"rendering": replacement, "state": "amended"}
                        ),
                        "principal update during review",
                        approval=approval,
                    ).seq
                    answer = {
                        "challenge": {
                            "worker": "peer",
                            "question": "Which ruling applies?",
                        }
                    }
                elif stage == "ResponseReply":
                    assert body["revision_hints"] == [
                        {
                            "used": {"commitment_id": identity.id, "version": 1},
                            "current_rendering": replacement,
                        }
                    ]
                    assert body["commitments"][0]["ref"]["version"] == 2
                    answer = {
                        "answer": {
                            "rationale": "The principal revised the term.",
                            "citations": [
                                {"reference": "commitment:D1", "quote": replacement}
                            ],
                        }
                    }
                elif stage == "ReviewReply":
                    assert (
                        body["turns"][0]["answer"]["citations"][0]["quote"]
                        == replacement
                    )
                    answer = {"challenge": None}
                elif stage == "Classification":
                    answer = {"kind": "style", "term": None, "question": None}
                else:
                    assert stage == "Ballot"
                    answer = {"choice": "second"}
                return Reply(outcome=Outcome.OK, raw_text=json.dumps(answer))

        routes = {
            model: Route(model.split("/")[0], Script())
            for model in (
                SPEC.initial_model,
                SPEC.successor_model,
                TEAM.peer_model,
                TEAM.reviewer_model,
            )
        }
        client = ModelClient(
            run_id=SPEC.run_id,
            mode=Mode.LIVE,
            run_dir=tmp_path / "calls",
            cache=ResponseCache(tmp_path / "cache.sqlite"),
            limits=RunLimits(token_cap=100),
            prices={
                model: Price(input_per_million=0, output_per_million=0)
                for model in routes
            },
            routes=routes,
            provider_limits={
                route.family: ProviderLimits(requests_per_minute=1e12)
                for route in routes.values()
            },
        )
        triad = ModelTriad(
            SPEC,
            ExperimentSettings(),
            client,
            TEAM,
            read_yaml(Path("config/m2-agreement.yaml"), AgreementRules),
            principal,
            Checker(),
        )
        result = asyncio.run(
            triad(
                ledger,
                ChunkTask(
                    chunk_id="chunk-1",
                    chunk=ChunkInput(1, (episode.source[0],)),
                    actor=Actor(
                        agent="executor", model=SPEC.initial_model, role="executor"
                    ),
                    ledger_version=first.seq,
                    brief=episode.brief,
                    commitments=snapshot.current,
                    resolution="cross_examination",
                    handover=None,
                    round_number=0,
                    previous=None,
                    findings=(),
                    hints=(),
                ),
            )
        )
        assert isinstance(result, WorkerDraft)
        assert revision_seq is not None and result.ledger_version >= revision_seq
        assert result.draft.output.sentences[0].text == replacement
        assert ledger.verify() is None
