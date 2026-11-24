from pathlib import Path

import pytest

from eval.planted import NaturalError, Plant, PlantPlan, PlantSession
from soc.memory import ChunkOutput, Translation


def output(text="En az 3 dakika bekleyin.", sid="9999-1-s01"):
    return ChunkOutput(sentences=[Translation(sentence_id=sid, text=text)])


def plan(**updates):
    values = dict(
        id="PRIVATE_PLANT_CANARY",
        chunk=1,
        sentence_id="9999-1-s01",
        kind="numbers",
        natural_error_id="PRIVATE_NATURAL_CANARY",
        original="3 dakika",
        planted="4 dakika",
    )
    values.update(updates)
    return PlantPlan(
        observations=[
            NaturalError(
                id="PRIVATE_NATURAL_CANARY",
                chunk=1,
                kind="numbers",
                runtime_ref="PRIVATE_OBSERVATION_CANARY",
            )
        ],
        plants=[Plant(**values)],
    )


def session(tmp_path, config=None):
    return PlantSession(
        tmp_path / "private",
        "run",
        config or plan(),
        public_roots=(tmp_path / "public", tmp_path / "run"),
    )


def test_private_plant_matches_natural_type_and_reverts_before_scorer(tmp_path):
    probe = session(tmp_path)
    before = output()
    exposed = probe.apply(1, 0, "executor", before)
    assert exposed == output("En az 4 dakika bekleyin.")
    assert before == output()
    seen = []
    result = probe.score({1: exposed}, lambda clean: seen.append(clean) or "scored")
    assert result == "scored" and seen == [{1: before}]
    assert probe.apply(1, 1, "executor", before) == before
    assert probe.apply(1, 0, "peer", before) == before
    assert probe.revert({1: before}) == {1: before}
    assert probe.revert({}) == {}  # Missing/unfinished work remains missing.


def test_plants_require_observed_kind_from_same_chunk_and_unambiguous_spans(tmp_path):
    for updates in (
        {"chunk": 2},
        {"kind": "polarity"},
        {"natural_error_id": "unknown"},
        {"planted": "3 dakika"},
    ):
        with pytest.raises(ValueError):
            plan(**updates)
    probe = session(tmp_path)
    for text in (
        "3 dakika ve 3 dakika",
        "3 dakika ve 4 dakika",
        "Metin tamamen farklı.",
    ):
        with pytest.raises(ValueError, match="span"):
            probe.apply(1, 0, "executor", output(text))
    assert not (tmp_path / "public").exists()
    assert not (tmp_path / "run").exists()


def test_resume_is_idempotent_and_rejects_changed_draft_or_plan(tmp_path):
    probe = session(tmp_path)
    exposed = probe.apply(1, 0, "executor", output())
    resumed = session(tmp_path)
    assert resumed.apply(1, 0, "executor", output()) == exposed
    with pytest.raises(ValueError, match="draft changed"):
        resumed.apply(1, 0, "executor", output("Başka bir metin: 3 dakika."))
    with pytest.raises(ValueError, match="binding"):
        session(tmp_path, plan(planted="5 dakika"))
    assert (tmp_path / "private").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "private/plants.sqlite").stat().st_mode & 0o777 == 0o600


def test_scoring_refuses_ambiguous_rewrites_and_propagation_without_calling_scorer(
    tmp_path,
):
    probe = session(tmp_path)
    probe.apply(1, 0, "executor", output())
    called = []
    for outputs in (
        {1: output("En az dört dakika bekleyin.")},
        {1: output("4 dakika ve 4 dakika")},
        {2: output("4 dakika", "9999-1-s02")},
    ):
        with pytest.raises(ValueError, match="reversion"):
            probe.score(outputs, lambda clean: called.append(clean))
    assert called == []
    assert probe.revert({1: output("Lütfen 4 dakika bekleyin.")}) == {
        1: output("Lütfen 3 dakika bekleyin.")
    }


def test_private_records_cannot_live_under_public_or_prompt_roots(tmp_path):
    with pytest.raises(ValueError, match="private"):
        PlantSession(
            tmp_path / "public/private",
            "run",
            plan(),
            public_roots=(tmp_path / "public",),
        )
    with pytest.raises(ValueError, match="private"):
        PlantSession(tmp_path, "run", plan(), public_roots=(tmp_path / "public",))


@pytest.mark.parametrize("memory", ["soc", "rag"])
@pytest.mark.parametrize("challenge", [False, True])
def test_real_triads_can_miss_a_plant_and_replay_then_score_only_restored_text(
    tmp_path, memory, challenge
):
    import asyncio
    import json

    from soc.baseline.retrieval import HashEmbedder
    from soc.enforce.checker import Checker
    from soc.episode import read_yaml
    from soc.orchestrator.model_rag import run_model_rag_episode
    from soc.orchestrator.model_triad import run_model_ledger_episode
    from soc.orchestrator.runner import ExperimentSettings
    from soc.principal.simulator import PrincipalSimulator
    from soc.providers.cache import ResponseCache
    from soc.providers.client import (
        ModelClient,
        Price,
        ProviderLimits,
        Route,
        RunLimits,
    )
    from soc.providers.models import Mode, Outcome, Reply
    from soc.triad.agreement import AgreementRules
    from tests.scenario.test_model_triad import (
        CLIENT,
        SPEC,
        TEAM,
        _episode,
        _TranslationModels,
    )

    class Models(_TranslationModels):
        def __init__(self):
            super().__init__()
            self.interrupt = self.retry_review = False
            self.outgoing = []

        async def generate(self, request):
            self.outgoing.append(request.messages)
            if request.schema_name == "Ballot":
                return Reply(
                    outcome=Outcome.OK,
                    raw_text='{"choice":"first"}',
                    input_tokens=1,
                    output_tokens=1,
                )
            if request.schema_name == "ReviewReply" and not challenge:
                return Reply(
                    outcome=Outcome.OK,
                    raw_text='{"challenge":null}',
                    input_tokens=1,
                    output_tokens=1,
                )
            return await super().generate(request)

    public = tmp_path / "public"
    _episode(public)
    config = PlantPlan(
        observations=[
            NaturalError(
                id="PRIVATE_NATURAL_CANARY",
                chunk=1,
                kind="term",
                runtime_ref="PRIVATE_OBSERVATION_CANARY",
            )
        ],
        plants=[
            Plant(
                id="PRIVATE_PLANT_CANARY",
                chunk=1,
                sentence_id="9999-1-s01",
                kind="term",
                natural_error_id="PRIVATE_NATURAL_CANARY",
                original="paketleri",
                planted="çerçeveleri",
            )
        ],
    )
    models = Models()
    key = tmp_path / "answers.yaml"
    key.write_text("answers: {}\n")
    principal = PrincipalSimulator(
        key, tmp_path / "answers.sqlite", episode_id="scope", actor=CLIENT
    )
    cache = ResponseCache(tmp_path / "cache.sqlite")
    model_ids = (
        SPEC.initial_model,
        SPEC.successor_model,
        TEAM.peer_model,
        TEAM.reviewer_model,
    )
    spec = SPEC.model_copy(update={"condition": memory})

    async def run(directory, mode, private):
        probe = PlantSession(
            private,
            spec.run_id,
            config,
            public_roots=(public, directory, Path("prompts")),
        )
        options = dict(
            spec=spec,
            experiment=ExperimentSettings(),
            principal=principal,
            checker=Checker(),
            team=TEAM,
            agreement=read_yaml(Path("config/m2-agreement.yaml"), AgreementRules),
            draft_intervention=probe,
            client=ModelClient(
                run_id=spec.run_id,
                run_dir=directory,
                mode=mode,
                cache=cache,
                limits=RunLimits(token_cap=1000, initial_backoff_seconds=0),
                prices={
                    model: Price(input_per_million=0, output_per_million=0)
                    for model in model_ids
                },
                routes={
                    model: Route(model.split("/")[0], models) for model in model_ids
                }
                if mode is Mode.LIVE
                else None,
                provider_limits={
                    family: ProviderLimits(requests_per_minute=1e12)
                    for family in ("family-a", "family-b", "family-c")
                },
            ),
        )
        runner = run_model_ledger_episode if memory == "soc" else run_model_rag_episode
        if memory == "rag":
            options["embedder"] = HashEmbedder()
        result = await runner(public, directory, **options)
        return result, probe

    directory = tmp_path / "run"
    result, probe = asyncio.run(run(directory, Mode.LIVE, tmp_path / "private-live"))
    assert result.status == "completed"
    rows = [
        json.loads(line)
        for line in (directory / "final_chunks.jsonl").read_text().splitlines()
    ]
    # Both runners publish sentence translations as the same output shape.
    outputs = {
        row["chunk"]: ChunkOutput.model_validate({"sentences": row["sentences"]})
        for row in rows
    }
    assert "çerçeveleri" in outputs[1].sentences[0].text
    clean = probe.score(outputs, lambda restored: restored)
    assert "paketleri" in clean[1].sentences[0].text
    assert all(
        "çerçeveleri" not in sentence.text
        for chunk in clean.values()
        for sentence in chunk.sentences
    )
    all_prompts = json.dumps(
        [
            [message.model_dump() for message in messages]
            for messages in models.outgoing
        ],
        ensure_ascii=False,
    )
    assert "çerçeveleri" in all_prompts
    for marker in (
        "PRIVATE_PLANT_CANARY",
        "PRIVATE_NATURAL_CANARY",
        "PRIVATE_OBSERVATION_CANARY",
        "natural_error_id",
        "plant_ids",
        "plants.sqlite",
    ):
        assert marker not in all_prompts
    calls = len(models.outgoing)
    resumed, _ = asyncio.run(run(directory, Mode.LIVE, tmp_path / "private-live"))
    replay, replay_probe = asyncio.run(
        run(tmp_path / "replay", Mode.REPLAY, tmp_path / "private-replay")
    )
    assert resumed == replay == result and len(models.outgoing) == calls
    assert replay_probe.revert(outputs) == clean


def test_reversion_cli_preserves_raw_and_continuity_requires_verified_export(
    tmp_path, monkeypatch, capsys
):
    import json

    from eval.continuity import main as continuity_main
    from eval.planted import main, read_outputs

    probe = session(tmp_path)
    exposed = probe.apply(1, 0, "executor", output())
    run = tmp_path / "run"
    run.mkdir()
    raw = run / "final_chunks.jsonl"
    raw.write_text(json.dumps({"chunk": 1, **exposed.model_dump()}) + "\n")
    original_bytes = raw.read_bytes()
    manifest = run / "run.json"
    manifest.write_text(
        json.dumps(
            {
                "run_id": "run",
                "model_protocol": {"draft_intervention": probe.fingerprint},
            }
        )
    )
    (run / "final_findings.jsonl").write_text('{"chunk":1,"findings":[]}\n')
    judgments = run / "judgments.jsonl"
    judgments.write_text(
        '{"decision_id":"D1","chunk":1,"sentence_id":"9999-1-s01","kept":true}\n'
    )
    monkeypatch.setattr("sys.argv", ["continuity", str(judgments), str(run)])
    with pytest.raises(SystemExit):
        continuity_main()
    restored = run / "restored.jsonl"
    args = [
        "planted",
        str(tmp_path / "private"),
        str(raw),
        str(restored),
        "--run-id",
        "run",
    ]
    monkeypatch.setattr("sys.argv", args)
    main()
    assert raw.read_bytes() == original_bytes
    assert read_outputs(restored) == {1: output()}
    with pytest.raises(ValueError, match="new output"):
        main()
    for output_path, run_id in ((raw, "run"), (run / "other.jsonl", "wrong-run")):
        monkeypatch.setattr(
            "sys.argv",
            [
                "planted",
                str(tmp_path / "private"),
                str(raw),
                str(output_path),
                "--run-id",
                run_id,
            ],
        )
        with pytest.raises(ValueError):
            main()
    scoring = [
        "continuity",
        str(judgments),
        str(run),
        "--private-plants",
        str(tmp_path / "private"),
        "--restored-output",
        str(restored),
    ]
    monkeypatch.setattr("sys.argv", scoring)
    continuity_main()
    assert json.loads(capsys.readouterr().out)["kept"] == 1
    restored.write_bytes(original_bytes)
    with pytest.raises(ValueError, match="restored evaluation output"):
        continuity_main()
    manifest.write_text(
        json.dumps({"run_id": "run", "draft_intervention": "changed-fingerprint"})
    )
    with pytest.raises(ValueError, match="fingerprint"):
        continuity_main()
    for rows in (
        '{"chunk":0,"sentences":[]}\n',
        '{"chunk":true,"sentences":[]}\n',
        '{"chunk":1,"sentences":[]}\n' * 2,
    ):
        restored.write_text(rows)
        with pytest.raises(ValueError, match="one-based"):
            read_outputs(restored)


def test_private_journal_rejects_record_tampering_and_duplicate_inputs(tmp_path):
    import sqlite3

    config = plan()
    for changes in (
        {"observations": config.observations * 2},
        {"plants": config.plants * 2},
        {
            "plants": [
                config.plants[0],
                config.plants[0].model_copy(
                    update={"id": "other", "sentence_id": "9999-1-s02"}
                ),
            ]
        },
    ):
        with pytest.raises(ValueError):
            PlantPlan(**(config.model_dump() | changes))
    with pytest.raises(ValueError, match="private"):
        PlantSession(tmp_path / "private", "run", config, public_roots=())
    probe = session(tmp_path)
    duplicate = ChunkOutput(sentences=output().sentences * 2)
    with pytest.raises(ValueError, match="unique sentence"):
        probe.apply(1, 0, "executor", duplicate)
    with pytest.raises(ValueError, match="unique sentence"):
        probe.revert({1: duplicate})
    probe.apply(1, 0, "executor", output())
    with sqlite3.connect(probe.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM exposures")
        db.execute("DROP TRIGGER exposures_no_update")
        db.execute(
            "UPDATE exposures SET value=json_set(value, '$.plant_ids', json('[]'))"
        )
    with pytest.raises(ValueError, match="exposure changed"):
        probe.revert({1: output()})
    with sqlite3.connect(probe.path) as db:
        db.execute("DROP TRIGGER binding_no_update")
        db.execute("UPDATE binding SET value='[]'")
    with pytest.raises(ValueError, match="binding changed"):
        probe.apply(1, 1, "executor", output())


def test_single_worker_rag_uses_same_intervention_and_checkpointed_output(tmp_path):
    import asyncio
    import json

    from soc.baseline.memory import RAGMemory
    from soc.baseline.retrieval import HashEmbedder
    from soc.enforce.checker import Checker
    from soc.orchestrator.runner import ExperimentSettings, run_episode
    from soc.providers.cache import ResponseCache
    from soc.providers.client import (
        ModelClient,
        Price,
        ProviderLimits,
        Route,
        RunLimits,
    )
    from soc.providers.models import Mode, Outcome, Reply
    from tests.scenario.test_model_triad import SPEC, _episode

    public = tmp_path / "public"
    _episode(public)
    config = PlantPlan(
        observations=[
            NaturalError(
                id="natural", chunk=1, kind="term", runtime_ref="observed:public-draft"
            )
        ],
        plants=[
            Plant(
                id="probe",
                chunk=1,
                sentence_id="9999-1-s01",
                kind="term",
                natural_error_id="natural",
                original="paketleri",
                planted="çerçeveleri",
            )
        ],
    )
    probe = PlantSession(
        tmp_path / "private",
        SPEC.run_id,
        config,
        public_roots=(public, tmp_path / "run"),
    )

    class Models:
        def __init__(self):
            self.calls = 0

        async def generate(self, request):
            self.calls += 1
            # The public fixture schedules one sentence per chunk.
            chunk = self.calls
            answer = output("Ağ kapısı paketleri iletir.", f"9999-1-s0{chunk}")
            return Reply(
                outcome=Outcome.OK,
                raw_text=answer.model_dump_json(),
                input_tokens=1,
                output_tokens=1,
            )

    models = Models()
    cache = ResponseCache(tmp_path / "cache.sqlite")
    ids = (SPEC.initial_model, SPEC.successor_model)

    async def run(directory, controller):
        checker = Checker()
        return await run_episode(
            public,
            directory,
            spec=SPEC,
            experiment=ExperimentSettings(),
            memory=RAGMemory(HashEmbedder(), checker),
            checker=checker,
            draft_intervention=controller,
            client=ModelClient(
                run_id=SPEC.run_id,
                run_dir=directory,
                mode=Mode.LIVE,
                cache=cache,
                limits=RunLimits(token_cap=1000),
                prices={
                    model: Price(input_per_million=0, output_per_million=0)
                    for model in ids
                },
                routes={model: Route("fake", models) for model in ids},
                provider_limits={"fake": ProviderLimits(requests_per_minute=1e12)},
            ),
        )

    directory = tmp_path / "run"
    first = asyncio.run(run(directory, probe))
    assert first.status == "completed" and models.calls == 4
    assert asyncio.run(run(directory, probe)) == first and models.calls == 4
    rows = [
        json.loads(line)
        for line in (directory / "final_chunks.jsonl").read_text().splitlines()
    ]
    outputs = {
        row["chunk"]: ChunkOutput.model_validate({"sentences": row["sentences"]})
        for row in rows
    }
    assert "çerçeveleri" in outputs[1].sentences[0].text
    assert "paketleri" in probe.score(outputs, lambda clean: clean)[1].sentences[0].text
    with pytest.raises(ValueError, match="configuration"):
        asyncio.run(run(directory, None))


def test_private_database_rejects_symlinks_and_non_files(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    database = private / "plants.sqlite"
    database.symlink_to(tmp_path / "public-leak.sqlite")
    with pytest.raises(ValueError, match="regular file"):
        session(tmp_path)
    assert not (tmp_path / "public-leak.sqlite").exists()
    database.unlink()
    database.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        session(tmp_path)
