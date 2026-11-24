import json
from fractions import Fraction
from hashlib import sha256
from pathlib import Path

import pytest

from soc.cooperation.prompts import PromptPolicy, PromptRenderer, TokenizerSpec
from soc.cooperation.standing import CooperationPolicy, Standing

FIXTURE = Path(__file__).parents[1] / "fixtures/cooperation-tokenizer.json"


def prompt_policy(models=("worker",)):
    asset = TokenizerSpec(
        path=str(FIXTURE.resolve()),
        sha256=sha256(FIXTURE.read_bytes()).hexdigest(),
        identifier="test-reference-bpe-v1; not a provider tokenizer",
    )
    return PromptPolicy(tokenizers={model: asset for model in models})


def test_prompt_pair_is_matched_and_hidden_is_independent_of_standing():
    renderer = PromptRenderer(prompt_policy(), CooperationPolicy())
    hidden = set()
    for checked in (0, 4, 5, 20):
        for passed in range(checked + 1):
            score = Fraction(passed, checked) if checked >= 5 else Fraction(1)
            for sanction in (
                "none",
                "lighter_vote",
                "no_review",
                "no_vote",
                "replacement",
            ):
                state = Standing(score, checked, passed, sanction, score / 2)
                pair = renderer.pair("worker", state)
                hidden.add(pair.hidden)
                assert abs(pair.announced_tokens - pair.hidden_tokens) * 50 < min(
                    pair.announced_tokens, pair.hidden_tokens
                )
                assert f"Your score is {score}" in pair.announced
    assert len(hidden) == 1
    text = hidden.pop().lower()
    for forbidden in (
        "standing",
        "sanction",
        "vote",
        "score",
        "threshold",
        "replacement",
        "test-reference",
        "tokenizer",
    ):
        assert forbidden not in text


def test_prompt_assets_are_pinned_and_missing_models_fail_closed(tmp_path):
    policy = prompt_policy()
    renderer = PromptRenderer(policy, CooperationPolicy())
    state = Standing(Fraction(1), 0, 0, "none", Fraction(1))
    with pytest.raises(ValueError, match="tokenizer"):
        renderer.pair("unknown-model", state)
    corrupt = policy.tokenizers["worker"].model_copy(update={"sha256": "0" * 64})
    with pytest.raises(ValueError, match="hash"):
        PromptRenderer(
            PromptPolicy(tokenizers={"worker": corrupt}), CooperationPolicy()
        )
    missing = corrupt.model_copy(update={"path": str(tmp_path / "missing.json")})
    with pytest.raises(ValueError, match="tokenizer"):
        PromptRenderer(
            PromptPolicy(tokenizers={"worker": missing}), CooperationPolicy()
        )
    with pytest.raises(ValueError, match="budget"):
        PromptRenderer(
            policy.model_copy(update={"section_tokens": 8}), CooperationPolicy()
        ).pair("worker", state)


def test_cooperation_golden_prompt_and_configured_rules():
    renderer = PromptRenderer(prompt_policy(), CooperationPolicy())
    state = Standing(Fraction(2, 5), 5, 2, "no_review", Fraction(1, 5))
    pair = renderer.pair("worker", state)
    golden = json.loads(FIXTURE.with_name("cooperation-prompts.json").read_text())
    assert {
        "announced": pair.announced,
        "hidden": pair.hidden,
        "audit": pair.audit(),
    } == golden
    for arm in ("announced", "hidden"):
        system, audit = renderer.system("worker", "Translate the source.", pair, arm)
        assert system == "Translate the source.\n\n" + getattr(pair, arm)
        assert audit["announced_system_tokens"] == audit["hidden_system_tokens"]
    policy = CooperationPolicy(window=8, minimum_checks=3, no_review_below=0.55)
    custom = PromptRenderer(prompt_policy(), policy).pair("worker", state)
    assert "recent 8 known" in custom.announced
    assert "Before 3 known checks" in custom.announced
    assert "below 0.55 reviewing" in custom.announced
    assert custom.hidden == pair.hidden


def test_unmatchable_tokenizer_is_refused(tmp_path):
    from tokenizers import Tokenizer, models

    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    # Every entire paragraph maps to a single token: padding cannot reach budget.
    path = tmp_path / "one-token.json"
    tokenizer.save(str(path))
    asset = TokenizerSpec(
        path=str(path),
        sha256=sha256(path.read_bytes()).hexdigest(),
        identifier="unmatchable reference",
    )
    with pytest.raises(ValueError, match="cannot match"):
        PromptRenderer(PromptPolicy(tokenizers={"worker": asset}), CooperationPolicy())


def test_assembled_system_length_guard_refuses_a_mismatched_pair():
    from dataclasses import replace

    renderer = PromptRenderer(prompt_policy(), CooperationPolicy())
    state = Standing(Fraction(1), 0, 0, "none", Fraction(1))
    pair = renderer.pair("worker", state)
    with pytest.raises(ValueError, match="system prompts differ"):
        renderer.system(
            "worker", "Translate.", replace(pair, announced="short"), "announced"
        )


@pytest.mark.parametrize("assets", [None, "incomplete"])
def test_cooperation_model_run_requires_assets_before_any_call(tmp_path, assets):
    import asyncio
    from types import SimpleNamespace

    from soc.cooperation.runtime import CooperationSettings
    from soc.episode import read_yaml
    from soc.orchestrator.model_triad import run_model_ledger_episode
    from soc.orchestrator.runner import ExperimentSettings
    from soc.providers.models import Mode
    from soc.triad.agreement import AgreementRules
    from tests.scenario.test_model_triad import SPEC, TEAM

    spec = SPEC.model_copy(
        update={
            "cooperation": CooperationSettings(
                episode_position=1,
                visibility="hidden",
                prompts=None if assets is None else prompt_policy(),
            )
        }
    )
    client = SimpleNamespace(
        run_id=spec.run_id,
        run_dir=tmp_path.resolve(),
        mode=Mode.REPLAY,
        prices={
            model: None
            for model in (
                spec.initial_model,
                spec.successor_model,
                TEAM.peer_model,
                TEAM.reviewer_model,
            )
        },
    )
    with pytest.raises(ValueError, match="tokenizer"):
        asyncio.run(
            run_model_ledger_episode(
                tmp_path / "absent",
                tmp_path,
                spec=spec,
                experiment=ExperimentSettings(),
                principal=SimpleNamespace(actor=None),
                checker=None,
                client=client,
                team=TEAM,
                agreement=read_yaml(Path("config/m2-agreement.yaml"), AgreementRules),
            )
        )
    assert list(tmp_path.iterdir()) == []


def test_token_counts_ignore_asset_truncation_and_padding(tmp_path):
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(FIXTURE))
    tokenizer.enable_truncation(max_length=8)
    tokenizer.enable_padding(length=2048, pad_id=0, pad_token="[UNK]")
    path = tmp_path / "preconfigured-tokenizer.json"
    tokenizer.save(str(path))
    asset = TokenizerSpec(
        path=str(path),
        sha256=sha256(path.read_bytes()).hexdigest(),
        identifier="reference with truncation and padding",
    )
    renderer = PromptRenderer(
        PromptPolicy(tokenizers={"worker": asset}), CooperationPolicy()
    )
    reference = PromptRenderer(prompt_policy(), CooperationPolicy())
    text = "A page contains many sentences. " * 50
    assert renderer.count("worker", text) == reference.count("worker", text) > 8
    state = Standing(Fraction(1), 0, 0, "none", Fraction(1))
    assert renderer.pair("worker", state).announced_tokens == 1024
