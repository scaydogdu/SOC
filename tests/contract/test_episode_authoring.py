import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
import yaml

from eval.authoring import draft_coverage, generate_skeleton, seal
from eval.episode import FILES, EpisodeError, validate
from eval.precheck import check_episode
from soc.providers.cache import ResponseCache
from soc.providers.client import ModelClient, Price, ProviderLimits, Route, RunLimits
from soc.providers.models import Mode, Outcome, Reply

ROOT = Path(__file__).resolve().parents[2]
TOY = ROOT / "data" / "episodes" / "toy"


def test_skeleton_proposes_terms_and_writes_a_draft_with_original_sentence_ids(
    tmp_path,
):
    rfc = tmp_path / "sample.xml"
    rfc.write_text(
        '<rfc number="9999"><middle>'
        '<section pn="section-1"><name>Start</name><t>Protect the access token.</t>'
        '<section pn="section-1.1"><name>Nested</name><t>The access token MUST expire.</t></section>'
        '</section><section pn="section-2"><name>Next</name><t>Protect the refresh token.</t>'
        '<section pn="section-2.1"><name>Nested</name><t>The access token MUST be bound.</t>'
        "<sourcecode>  a = 3\n</sourcecode></section></section>"
        '<section pn="section-3"><name>Excluded</name><t>The access token MAY rotate.</t></section>'
        "</middle></rfc>",
        encoding="utf-8",
    )
    package = tmp_path / "draft"
    candidates = {
        item.scope_key: item
        for item in generate_skeleton(rfc, package, first="1", last="2")
    }
    assert set(path.name for path in package.iterdir()) == FILES | {"manifest.json"}
    assert candidates["access token"].chunks == [1, 2, 4]
    assert candidates["access token"].sentence_uses == 3
    assert candidates["MUST"].chunks == [2, 4]
    assert candidates["MUST"].kind == "non_lexical"
    assert "MAY" not in candidates
    rows = [
        json.loads(line) for line in (package / "source.jsonl").read_text().splitlines()
    ]
    assert [row["id"] for row in rows] == [
        "9999-1-s01",
        "9999-1.1-s01",
        "9999-2-s01",
        "9999-2.1-s01",
        "9999-2.1-s02",
    ]
    assert rows[-1]["text"] == "  a = 3\n"
    assert rows[-1]["translatable"] is False
    schedule = yaml.safe_load((package / "events.yaml").read_text())
    assert schedule["executor_change_after"] is None
    assert schedule["brief_change"] == {"chunk": None, "revisions": [], "clauses": []}
    assert schedule["approvals"] == []
    assert (package / "seeds.jsonl").read_bytes() == b""
    hidden = yaml.safe_load((package / "hidden.yaml").read_text())
    assert hidden == {"preferences": [], "planted_errors": []}
    manifest = json.loads((package / "manifest.json").read_text())["sha256"]
    assert manifest == {
        name: hashlib.sha256((package / name).read_bytes()).hexdigest()
        for name in FILES
    }
    failed = {check.rule for check in draft_coverage(package) if not check.passed}
    assert failed == {
        "executor_position",
        "brief_position",
        "event_order",
        "executor_span",
        "brief_span",
        "revision_kinds",
    }
    with pytest.raises(FileExistsError):
        generate_skeleton(rfc, package, first="1", last="2")
    with pytest.raises(EpisodeError, match="ordered section range"):
        generate_skeleton(rfc, tmp_path / "reverse", first="3", last="1")


def test_coverage_reports_all_draft_gaps_and_seal_validates_before_changing_manifest(
    tmp_path,
):
    directory = tmp_path / "copy"
    shutil.copytree(TOY, directory)
    report = draft_coverage(directory)
    assert all(check.passed for check in report)
    assert next(
        check.observed for check in report if check.rule == "executor_span"
    ) == [
        "D1",
        "D2",
        "D3",
        "D4",
        "D5",
    ]

    manifest = directory / "manifest.json"
    before = manifest.read_bytes()
    affected = directory / "affected.jsonl"
    original = affected.read_text()
    rows = [json.loads(line) for line in original.splitlines()]
    rows[0]["repair"][0]["chunk"] = 4
    affected.write_text(json.dumps(rows[0]) + "\n", encoding="utf-8")
    failed = {check.rule for check in draft_coverage(directory) if not check.passed}
    assert failed == {"affected_positions", "affected_decisions", "affected_sides"}
    with pytest.raises(EpisodeError, match="outside its named chunk"):
        seal(directory)
    assert manifest.read_bytes() == before

    affected.write_text(original.replace('"D1",', '"D1" ,'), encoding="utf-8")
    with pytest.raises(EpisodeError, match="SHA-256 mismatch"):
        validate(directory)
    assert seal(directory) == validate(directory)
    assert manifest.read_bytes() != before


def test_precheck_hides_answer_keys_and_reports_seed_and_affected_disagreements(
    tmp_path,
):
    episode = validate(TOY)
    before = {path.name: path.read_bytes() for path in TOY.iterdir() if path.is_file()}
    predicted = [
        {
            "answer": "Treat the requirement as optional.",
            "rationale": "Test disagreement.",
        },
        {"repair": [], "apply": [], "rationale": "Test affected mismatch."},
        episode.affected[1].model_dump(mode="json", exclude={"decision_id"})
        | {"rationale": "Matched."},
    ]

    class Independent:
        def __init__(self):
            self.prompts = []

        async def generate(self, request):
            prompt = request.messages[-1].content
            self.prompts.append(prompt)
            payload = json.loads(prompt.split("\n", 1)[1])
            assert "expected_answer" not in payload
            assert episode.seeds[0].expected_answer not in prompt
            assert "repair" not in payload and "apply" not in payload
            return Reply(
                outcome=Outcome.OK,
                raw_text=json.dumps(predicted[len(self.prompts) - 1]),
                input_tokens=10,
                output_tokens=5,
            )

    provider = Independent()
    model = "gpt-6-astra"
    client = ModelClient(
        run_id="precheck",
        mode=Mode.LIVE,
        cache=ResponseCache(tmp_path / "cache.sqlite"),
        run_dir=tmp_path,
        limits=RunLimits(token_cap=100),
        prices={model: Price(input_per_million=0, output_per_million=0)},
        routes={model: Route("fake", provider)},
        provider_limits={"fake": ProviderLimits(requests_per_minute=1e9)},
    )
    report = asyncio.run(check_episode(episode, client, model, max_output_tokens=100))
    assert [(row.kind, row.id, row.status) for row in report] == [
        ("seed", "S1", "disagree"),
        ("affected", "D1", "disagree"),
        ("affected", "D5", "agree"),
    ]
    assert report[0].expected == episode.seeds[0].expected_answer
    assert report[1].predicted == {"repair": [], "apply": []}
    assert {
        path.name: path.read_bytes() for path in TOY.iterdir() if path.is_file()
    } == before
