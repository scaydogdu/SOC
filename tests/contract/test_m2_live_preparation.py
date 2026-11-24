import asyncio
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from scripts.prepare_m2_live import estimate_condition
from soc.episode import read_yaml
from soc.orchestrator.live import LiveConfig, SOCCondition
from soc.triad.agreement import AgreementRules

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted((ROOT / "config/m2-live").glob("*.yaml"))


@pytest.mark.parametrize("condition_file", FILES, ids=lambda path: path.stem)
def test_proposed_live_condition_keeps_unresolved_checks_unverified_and_private_key_out_of_prompts(
    tmp_path, condition_file
):
    condition = SOCCondition.model_validate(yaml.safe_load(condition_file.read_text()))
    config = LiveConfig.model_validate(
        yaml.safe_load((ROOT / "config/m1-live.yaml").read_text())
    )
    agreement = read_yaml(ROOT / "config/m2-agreement.yaml", AgreementRules)
    canary = "UNASKED_PRIVATE_PREFERENCE_908315"
    key = tmp_path / "isolated-principal" / "toy.yaml"
    key.parent.mkdir()
    key.write_text(
        yaml.safe_dump(
            {
                "answers": {
                    "Which Turkish rendering should we use for access token?": "erişim jetonu",
                    "What is the private unused preference?": canary,
                }
            },
            allow_unicode=True,
        )
    )
    assert condition.run.max_in_flight == 1
    result = asyncio.run(
        estimate_condition(
            config,
            condition,
            ROOT / "data/episodes/toy",
            key,
            tmp_path / "trial",
            agreement,
        )
    )
    assert result["status"] == "unverified" and result["unverified_must"] > 0
    assert result["physical_calls"] == (
        13 if condition.run.resolution == "principal_only" else 19
    )
    assert (
        0
        < result["expected_usd"]
        < result["scripted_path_reserved_usd"]
        < result["retry_envelope_usd"]
    )
    assert (
        result["peak_concurrent_reservation_usd"] < result["scripted_path_reserved_usd"]
    )
    for path in (tmp_path / "trial/run").glob("triad-prompt-*.json"):
        body = path.read_text()
        assert str(key) not in body and canary not in body
        assert "What is the private unused preference?" not in body
    expected = result["expected_usd"]
    from soc.orchestrator.spend_ops import spend_status

    status = spend_status(tmp_path / "trial/campaign")
    assert Decimal(str(status["settled_usd"])) == Decimal(str(expected))
    assert status["reserved_usd"] == 0


def test_live_condition_matrix_has_three_modes_and_both_keep_efforts():
    conditions = [
        SOCCondition.model_validate(yaml.safe_load(path.read_text())) for path in FILES
    ]
    assert {(c.run.resolution, c.run.effort) for c in conditions} == {
        ("cross_examination", "low"),
        ("cross_examination", "high"),
        ("open_discussion", "low"),
        ("principal_only", "low"),
    }
    assert len({c.run.run_id for c in conditions}) == 4
    assert {c.team.reviewer_model for c in conditions} == {"gemini-3.8-flash"}
    assert {c.team.peer_model for c in conditions} == {"claude-sonnet-5-5"}


def test_live_condition_uses_explicit_code_revision_for_record_and_replay(
    tmp_path, monkeypatch
):
    from argparse import Namespace

    from soc.orchestrator import live
    from soc.orchestrator.ledger_runner import LedgerRunResult
    from soc.providers.models import Outcome

    seen = []

    async def fake_run(_config, _root, _episode, _directory, **kwargs):
        seen.append(kwargs["condition"].run.code_commit)
        return LedgerRunResult("completed", Outcome.OK, {})

    monkeypatch.setattr(live, "run_live_soc", fake_run)
    for command in ("soc", "soc-replay"):
        args = Namespace(
            config=ROOT / "config/m1-live.yaml",
            command=command,
            condition=FILES[0],
            agreement=ROOT / "config/m2-agreement.yaml",
            output=tmp_path / "campaign",
            episode=ROOT / "data/episodes/toy",
            run_dir=tmp_path / command,
            principal_key=tmp_path / "private.yaml",
            code_commit="approved-recorded-revision",
        )
        assert asyncio.run(live._main(args)) == 0
    assert seen == ["approved-recorded-revision"] * 2
