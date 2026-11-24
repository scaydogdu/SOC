import asyncio
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
import yaml

from analysis.unknowns import aggregate
from eval.continuity import PublishedChunk, UseAssessment, score_continuity
from eval.episode import load_full, validate
from eval.precheck import check_episode
from soc.enforce.checker import Checker, NormativeRendering
from soc.enforce.meaning import (
    MeaningEvidence,
    MeaningReply,
    MeaningStore,
    assess,
    checked_reply,
)
from soc.episode import (
    Approval,
    Clause,
    EpisodeError,
    ModalRendering,
    load_runtime,
    validate_runtime,
)
from soc.ledger.models import Actor
from soc.memory import ChunkOutput, Decision, DecisionRef, Translation
from soc.orchestrator.live import LiveConfig, SOCCondition, run_live_soc
from tests.scenario.test_held_unknown import TOY, execute, public_episode


def evaluation_copy(directory):
    shutil.copytree(TOY, directory)
    brief = yaml.safe_load((directory / "brief.yaml").read_text())
    brief["fixture"] = False
    (directory / "brief.yaml").write_text(yaml.safe_dump(brief))
    manifest = json.loads((directory / "manifest.json").read_text())
    manifest["sha256"]["brief.yaml"] = hashlib.sha256(
        (directory / "brief.yaml").read_bytes()
    ).hexdigest()
    (directory / "manifest.json").write_text(json.dumps(manifest))


def test_evaluation_validator_precheck_and_launcher_refuse_before_any_provider_or_key_read(
    tmp_path,
):
    public = tmp_path / "evaluation"
    evaluation_copy(public)
    with pytest.raises(EpisodeError, match="D4.*MUST"):
        validate(public)
    full = load_full(public)
    with pytest.raises(EpisodeError, match="D4.*MUST"):
        asyncio.run(check_episode(full, None, "unused", max_output_tokens=768))
    config = LiveConfig.model_validate(
        yaml.safe_load(Path("config/m1-live.yaml").read_text())
    )
    condition = SOCCondition.model_validate(
        yaml.safe_load(Path("config/m2-live/cross-examination-low.yaml").read_text())
    )
    with pytest.raises(EpisodeError, match="D4.*MUST"):
        asyncio.run(
            run_live_soc(
                config,
                tmp_path / "campaign",
                public,
                tmp_path / "run",
                condition=condition,
                answer_key=tmp_path / "NONEXISTENT_KEY",
                agreement=None,
                routes={},
            )
        )
    assert not (tmp_path / "campaign").exists() and not (tmp_path / "run").exists()


def test_public_modal_configuration_and_unsupported_rules_name_the_decision(tmp_path):
    public = tmp_path / "evaluation"
    evaluation_copy(public)
    episode = load_runtime(public)
    mapping = ModalRendering(keyword="MUST", value="zorunludur", brief_clause_id="B1")
    configured = episode.model_copy(
        update={"brief": episode.brief.model_copy(update={"normative": [mapping]})}
    )
    assert validate_runtime(configured) == configured
    assert (
        validate_runtime(
            episode, normative=(NormativeRendering("MUST", "zorunludur", "B1"),)
        )
        == episode
    )
    ambiguous = configured.model_copy(
        update={
            "brief": configured.brief.model_copy(
                update={
                    "clauses": [
                        *configured.brief.clauses,
                        Clause(id="B2", text="Additional clause"),
                    ],
                    "normative": [
                        mapping,
                        mapping.model_copy(update={"brief_clause_id": "B2"}),
                    ],
                }
            )
        }
    )
    with pytest.raises(EpisodeError, match="D4.*ambiguous.*MUST"):
        validate_runtime(ambiguous)
    data = episode.events.approvals[0].model_dump()
    for change in ({"kind": "unimplemented"}, {"value": " "}):
        with pytest.raises(ValueError, match=data["decision_id"]):
            Approval.model_validate({**data, **change})
    bad = episode.events.approvals[0].model_copy(update={"kind": "unimplemented"})
    invalid = episode.model_copy(
        update={"events": episode.events.model_copy(update={"approvals": [bad]})}
    )
    with pytest.raises(EpisodeError, match=bad.decision_id):
        validate_runtime(invalid)
    protected = episode.model_copy(
        update={
            "source": [
                row.model_copy(update={"translatable": False}) for row in episode.source
            ]
        }
    )
    assert validate_runtime(protected) == protected


@pytest.mark.parametrize("arm", ["soc", "rag"])
def test_meaning_provider_failure_stops_instead_of_becoming_semantic_unknown(
    tmp_path, arm
):
    result, models, _ = execute(tmp_path, arm, "unavailable")
    assert result.status == "failed" and models.calls.count("MeaningReply") == 1
    assert (
        json.loads((tmp_path / "record/run.json").read_text())["outcome"]
        == "server_error"
    )
    assert not json.loads((tmp_path / "record/assembly.json").read_text())["accepted"]


def held_check(tmp_path):
    public = tmp_path / "public"
    public_episode(public)
    episode = load_runtime(public)
    source = episode.source[:1]
    output = ChunkOutput(
        sentences=[Translation(sentence_id=source[0].id, text="3 jeton")]
    )
    _, assessment = assess(Checker(), "chunk-1", 1, source, output, episode.brief)
    return assessment.held[0], assessment


def test_resolutions_are_context_bound_and_append_only(tmp_path):
    check, assessment = held_check(tmp_path)
    assert (
        check.model_copy(update={"translation": "changed"}).check_id != check.check_id
    )
    assert check.model_copy(update={"build_number": 2}).check_id != check.check_id
    assert (
        check.model_copy(
            update={"brief": check.brief.model_copy(update={"clauses": []})}
        ).check_id
        != check.check_id
    )

    class Resolver:
        actor = Actor(agent="resolver", model="fake", role="resolver")

        async def __call__(self, request):
            return MeaningReply(
                verdict="compliant",
                rationale="Verified source quote",
                evidence=[
                    MeaningEvidence(
                        reference=f"source:{request.source.id}", quote="MUST"
                    )
                ],
            )

    store = MeaningStore(tmp_path / "meaning.sqlite")
    store.observe(assessment.context_id, assessment.must_checks, assessment.held)
    with pytest.raises(ValueError, match="audit context"):
        store.observe(assessment.context_id, 999, assessment.held)
    resolver = Resolver()
    result = asyncio.run(store.resolve(check, resolver))
    assert asyncio.run(store.resolve(check, resolver)) == result
    resolver.actor = resolver.actor.model_copy(update={"model": "different"})
    with pytest.raises(ValueError, match="resolver changed"):
        asyncio.run(store.resolve(check, resolver))
    with sqlite3.connect(store.path) as db:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("DELETE FROM resolutions")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            db.execute("UPDATE checks SET must_checks=999")
    assert (
        checked_reply(
            check,
            MeaningReply(verdict="violation", rationale="No citations", evidence=[]),
        ).verdict
        == "unknown"
    )
    assert (
        checked_reply(
            check,
            MeaningReply(
                verdict="compliant",
                rationale="Brief",
                evidence=[
                    MeaningEvidence(
                        reference="brief:B1", quote="Preserve normative force."
                    )
                ],
            ),
        ).verdict
        == "compliant"
    )
    assert MeaningStore(tmp_path / "empty.sqlite").report()["unknown_rate"] is None


def test_unknown_rule_is_not_treated_as_a_term_and_check_counts_include_passes(
    tmp_path,
):
    check, _ = held_check(tmp_path)
    source = check.source
    checker = Checker()
    decision = Decision(
        DecisionRef("unsupported", 1), "3 jeton", frozenset({"MUST"}), "not-a-checker"
    )
    output = ChunkOutput(sentences=[Translation(sentence_id=source.id, text="3 jeton")])
    findings = checker.check([source], output, check.brief, decisions=[decision])
    assert any(row.rule == "rule" and row.status == "unknown" for row in findings)
    assert (
        checker.count_must_checks([source], output, check.brief, decisions=[decision])
        == 4
    )
    assert (
        checker.count_must_checks([source], ChunkOutput(sentences=[]), check.brief) == 1
    )
    protected = source.model_copy(update={"translatable": False})
    assert checker.count_must_checks([protected], output, check.brief) == 2


@pytest.mark.parametrize("format", ["soc", "rag"])
def test_continuity_counts_unverified_and_missing_uses_as_not_kept(format):
    finding = {
        "sentence_id": "site-2",
        "strength": "must",
        **({"outcome": "unverified"} if format == "soc" else {"status": "unverified"}),
    }
    published = [
        PublishedChunk(chunk=1, findings=[]),
        PublishedChunk.model_validate({"chunk": 2, "findings": [finding]}),
    ]
    assessments = [
        UseAssessment(decision_id="D1", sentence_id=f"site-{i}", chunk=i, kept=True)
        for i in (1, 2, 3)
    ]
    score = score_continuity(assessments, published)
    assert (score["total"], score["kept"], score["unverified"], score["missing"]) == (
        3,
        1,
        1,
        1,
    )
    assert score["continuity"] == 1 / 3
    assert score_continuity([], [])["continuity"] is None
    with pytest.raises(ValueError, match="unique"):
        score_continuity([assessments[0], assessments[0]], published)
    weak = PublishedChunk.model_validate(
        {"chunk": 1, "findings": [{"status": "unknown", "strength": "should"}]}
    )
    assert score_continuity([assessments[0]], [weak])["kept"] == 1


def test_unknown_report_aggregation_uses_counts_and_rejects_duplicate_runs(tmp_path):
    paths = []
    for run, count, unknown in (("one", 100, 1), ("two", 900, 0)):
        data = {
            "condition": "soc",
            "episode_id": "E1",
            "run_id": run,
            "must_checks": count,
            "unknown_must": unknown,
            "configuration_unknowns": 0,
            "runtime_unknowns": unknown,
            "resolved": unknown,
            "compliant": unknown,
            "violation": 0,
            "unverified": 0,
            "unverified_final": 0,
        }
        path = tmp_path / f"{run}.json"
        path.write_text(json.dumps(data))
        paths.append(path)
    (report,) = aggregate(paths)
    assert report["runtime_unknown_rate"] == 0.001 and report["pilot_b_unknowns_pass"]
    with pytest.raises(ValueError, match="duplicate"):
        aggregate([paths[0], paths[0]])
    with pytest.raises(ValueError, match="limit"):
        aggregate(paths, limit=float("nan"))
    data["unknown_must"] = 2
    paths[1].write_text(json.dumps(data))
    with pytest.raises(ValueError, match="inconsistent"):
        aggregate(paths)


def test_held_reporting_cli_and_no_data_do_not_claim_a_pass(
    tmp_path, monkeypatch, capsys
):
    from analysis.unknowns import main as unknown_main
    from eval.continuity import main as continuity_main

    run = tmp_path / "run"
    run.mkdir()
    (run / "final_findings.jsonl").write_text(
        '{"chunk":1,"findings":[{"status":"unverified","sentence_id":"s"}]}\n'
    )
    judgments = tmp_path / "judgments.jsonl"
    judgments.write_text(
        UseAssessment(
            decision_id="D", sentence_id="s", chunk=1, kept=True
        ).model_dump_json()
        + "\n"
    )
    monkeypatch.setattr("sys.argv", ["continuity", str(judgments), str(run)])
    continuity_main()
    assert json.loads(capsys.readouterr().out)["kept"] == 0
    report = tmp_path / "unknowns.json"
    data = {
        "condition": "soc",
        "episode_id": "E",
        "run_id": "R",
        **MeaningStore(tmp_path / "meaning.sqlite").report(),
    }
    report.write_text(json.dumps(data))
    monkeypatch.setattr("sys.argv", ["unknowns", str(report)])
    unknown_main()
    (result,) = json.loads(capsys.readouterr().out)
    assert result["unknown_rate"] is None and not result["pilot_b_unknowns_pass"]
    data["must_checks"] = True
    report.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="integer"):
        aggregate([report])


def test_meaning_prompts_never_read_private_keys_or_fixture_metadata(tmp_path):
    public_episode(tmp_path / "public")
    canary = "PRIVATE_HIDDEN_EXPECTED_77429"
    (tmp_path / "public/hidden.yaml").write_text(canary)
    (tmp_path / "public/seeds.jsonl").write_text(
        json.dumps({"expected_answer": canary})
    )
    result, models, _ = execute(tmp_path, "rag", "compliant")
    assert (
        result.status == "completed"
        and result.input_tokens == result.output_tokens == 8
    )
    assert all(
        canary not in prompt
        and "expected_answer" not in prompt
        and '"fixture"' not in prompt
        for prompt in models.prompts
    )


def test_unconfigured_meaning_model_is_refused_without_substitution():
    data = yaml.safe_load(Path("config/m1-live.yaml").read_text())
    data["experiment"]["meaning_model"] = "unknown-model"
    with pytest.raises(ValueError, match="configured roster"):
        LiveConfig.model_validate(data)


def test_equivalent_applicable_requirements_share_context_but_forbidden_changes_do_not(
    tmp_path,
):
    from soc.memory import ForbiddenRendering

    check, _ = held_check(tmp_path)
    output = ChunkOutput(
        sentences=[Translation(sentence_id=check.source.id, text=check.translation)]
    )
    relevant = Decision(DecisionRef("D", 1), "3 jeton", frozenset({"MUST"}), "term")
    irrelevant = Decision(
        DecisionRef("OTHER", 1),
        "Unrelated public rule",
        frozenset({"other-scope"}),
        "term",
    )

    def inspect(decisions, forbidden=()):
        _, assessment = assess(
            Checker(),
            "chunk-1",
            1,
            [check.source],
            output,
            check.brief,
            decisions=decisions,
            forbidden=forbidden,
        )
        return assessment

    first = inspect([relevant])
    same = inspect([relevant, irrelevant])
    scoped = inspect(
        [
            relevant.__class__(
                relevant.ref,
                relevant.value,
                relevant.scope_keys,
                relevant.kind,
                sentence_ids=frozenset({check.source.id, "9999-2-s01"}),
            )
        ]
    )
    assert first.context_id == same.context_id == scoped.context_id
    assert first.held[0].check_id == same.held[0].check_id == scoped.held[0].check_id
    changed = inspect(
        [relevant], [ForbiddenRendering("obsolete", frozenset({"MUST"}), "old")]
    )
    assert (
        changed.context_id != first.context_id
        and changed.held[0].check_id != first.held[0].check_id
    )


def test_active_modal_wording_reaches_both_arms_without_future_or_fixture_metadata(
    tmp_path,
):
    from soc.baseline.memory import RAGMemory
    from soc.baseline.retrieval import HashEmbedder
    from soc.memory import ChunkInput

    check, _ = held_check(tmp_path)
    brief = check.brief.model_copy(
        update={
            "normative": [
                ModalRendering(
                    keyword="MUST", value="ACTIVE_MODAL", brief_clause_id="B1"
                ),
                ModalRendering(
                    keyword="SHOULD", value="FUTURE_MODAL", brief_clause_id="B2"
                ),
            ]
        }
    )
    visible = json.dumps(brief.public_data())
    memory = RAGMemory(HashEmbedder(), Checker())
    baseline = memory.context_for(ChunkInput(1, (check.source,)), brief).text
    assert "ACTIVE_MODAL" in visible and "ACTIVE_MODAL" in baseline
    assert (
        "FUTURE_MODAL" not in visible
        and "FUTURE_MODAL" not in baseline
        and '"fixture"' not in visible
    )
    changed = brief.model_copy(
        update={
            "clauses": [*brief.clauses, Clause(id="B2", text="Authorized new clause")]
        }
    )
    assert "FUTURE_MODAL" in json.dumps(changed.public_data())
