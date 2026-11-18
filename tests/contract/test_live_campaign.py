import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from soc.baseline.retrieval import HashEmbedder
from soc.orchestrator.live import (
    LiveConfig,
    available_families,
    probe_roster,
    run_live_toy,
)
from soc.providers.cache import ResponseCache
from soc.providers.client import ModelClient, Price, ProviderLimits, Route, RunLimits
from soc.providers.fake import ScriptedProvider
from soc.providers.models import (
    Message,
    Mode,
    Outcome,
    ProviderRequestError,
    Reply,
    Request,
)
from soc.providers.spend import SpendBoundError, SpendGuard, SpendRate
from tests.scenario.test_runner import SCRIPT, TOY

ROOT = Path(__file__).resolve().parents[2]
MODEL = "fake-first"
SUCCESSOR = "fake-successor"
RATE = SpendRate(
    max_input_tokens=900,
    max_output_tokens=100,
    input_per_million=1000,
    output_per_million=1000,
)


def test_reservations_are_shared_and_survive_reopen(tmp_path):
    path = tmp_path / "spend.sqlite"
    first = SpendGuard(path, Decimal("1"), {MODEL: RATE})
    second = SpendGuard(path, Decimal("1"), {MODEL: RATE})
    with ThreadPoolExecutor(2) as threads:
        a = threads.submit(
            first.reserve, "probe", "a", MODEL, input_bound=900, output_bound=100
        )
        b = threads.submit(
            second.reserve, "toy", "b", MODEL, input_bound=900, output_bound=100
        )
        allowed = [
            reservation
            for reservation in (a.result(), b.result())
            if reservation is not None
        ]
    assert len(allowed) == 1
    reopened = SpendGuard(path, Decimal("1"), {MODEL: RATE})
    assert reopened.summary()["unsettled_reservations_usd"] == 1
    reopened.settle(allowed[0], MODEL, Reply(outcome=Outcome.SERVER_ERROR))
    assert (
        reopened.reserve("probe", "retry", MODEL, input_bound=900, output_bound=100)
        is None
    )
    with pytest.raises(ValueError, match="use spend set-cap"):
        SpendGuard(path, Decimal("2"), {MODEL: RATE})


def test_usage_over_bound_stops_the_campaign(tmp_path):
    guard = SpendGuard(tmp_path / "spend.sqlite", Decimal("4"), {MODEL: RATE})
    reservation = guard.reserve("probe", "a", MODEL, input_bound=900, output_bound=100)
    assert reservation is not None
    with pytest.raises(SpendBoundError):
        guard.settle(
            reservation,
            MODEL,
            Reply(
                outcome=Outcome.OK,
                input_tokens=901,
                output_tokens=10,
                usage_reported=True,
            ),
        )
    assert guard.summary()["bound_exceeded"] is True
    assert guard.reserve("toy", "b", MODEL, input_bound=900, output_bound=100) is None


def test_request_output_bound_allows_smaller_reservation(tmp_path):
    path = tmp_path / "spend.sqlite"
    guard = SpendGuard(path, Decimal("0.92"), {MODEL: RATE})
    assert guard.reserve("toy", "a", MODEL, input_bound=900, output_bound=100) is None
    reservation = guard.reserve("toy", "a", MODEL, input_bound=900, output_bound=20)
    assert reservation is not None
    reopened = SpendGuard(path, Decimal("0.92"), {MODEL: RATE})
    assert reopened.summary()["unsettled_reservations_usd"] == 0.92
    with pytest.raises(SpendBoundError):
        reopened.settle(
            reservation,
            MODEL,
            Reply(
                outcome=Outcome.OK,
                input_tokens=900,
                output_tokens=21,
                usage_reported=True,
            ),
        )
    assert reopened.summary()["bound_exceeded"] is True
    assert reopened.reserve("toy", "b", MODEL, input_bound=900, output_bound=20) is None


def test_spend_cap_is_checked_before_each_physical_attempt(tmp_path):
    from soc.orchestrator.live import ProbeAnswer

    class Timeout:
        attempts = 0

        async def generate(self, request):
            self.attempts += 1
            return Reply(outcome=Outcome.SERVER_ERROR)

    provider = Timeout()
    guard = SpendGuard(tmp_path / "spend.sqlite", Decimal("0.5"), {MODEL: RATE})
    client = ModelClient(
        run_id="probe",
        mode=Mode.LIVE,
        cache=ResponseCache(tmp_path / "cache.sqlite"),
        run_dir=tmp_path,
        limits=RunLimits(token_cap=100, initial_backoff_seconds=0),
        prices={MODEL: Price(input_per_million=1000, output_per_million=1000)},
        routes={MODEL: Route("fake", provider)},
        provider_limits={"fake": ProviderLimits(requests_per_minute=1e9)},
        spend_guard=guard,
    )
    result = asyncio.run(
        client.generate(
            Request(
                model=MODEL,
                effort="low",
                messages=(Message(role="user", content="probe"),),
                schema=ProbeAnswer,
                replicate=0,
                run_id="probe",
                call_id="first",
                sampling={"max_output_tokens": 50},
            )
        )
    )
    assert result.reply.outcome is Outcome.BUDGET_EXHAUSTED
    assert provider.attempts == 1
    assert 0.25 < guard.summary()["unsettled_reservations_usd"] <= 0.5


def campaign():
    return LiveConfig.model_validate(
        {
            "spend_cap_usd": "10",
            "token_cap_per_run": 5000,
            "probe_max_output_tokens": 100,
            "models": {
                MODEL: {
                    "family": "openai",
                    "limits": RATE.model_copy(update={"max_input_tokens": 100_000}),
                },
                SUCCESSOR: {
                    "family": "openai",
                    "limits": RATE.model_copy(update={"max_input_tokens": 100_000}),
                },
            },
            "provider_limits": {"openai": {"requests_per_minute": 1e9}},
            "toy": {
                "initial_model": MODEL,
                "successor_model": SUCCESSOR,
                "effort": "low",
            },
            "experiment": {
                "max_output_tokens": 100,
                "normative": [
                    {"keyword": "MUST", "value": "zorunludur", "brief_clause_id": "B1"}
                ],
            },
        }
    )


def test_probe_and_recorded_toy_share_spend_and_replay_without_routes(tmp_path):
    class Metered:
        def __init__(self):
            self.probe_calls = 0
            self.toy_calls = 0
            self.toy = ScriptedProvider(SCRIPT)

        async def generate(self, request):
            if request.schema_name == "ChunkOutput":
                self.toy_calls += 1
                reply = await self.toy.generate(request)
                return reply.model_copy(update={"usage_reported": True})
            self.probe_calls += 1
            return Reply(
                outcome=Outcome.OK,
                raw_text='{"value":"SOC_READY"}',
                input_tokens=8,
                output_tokens=2,
                usage_reported=True,
            )

    config = campaign()
    provider = Metered()
    routes = {model: Route("openai", provider) for model in config.models}
    report = asyncio.run(probe_roster(config, tmp_path, routes))
    assert [(row.model, row.effort) for row in report.results] == [
        (MODEL, "low"),
        (MODEL, "high"),
        (SUCCESSOR, "low"),
        (SUCCESSOR, "high"),
    ]
    assert all(
        row.status == "passed" and row.impossible.outcome is Outcome.PARSE_FAILED
        for row in report.results
    )
    assert provider.probe_calls == 12
    assert report.spend["metered_upper_estimate_usd"] == pytest.approx(0.12)
    assert asyncio.run(probe_roster(config, tmp_path, routes)) == report
    assert provider.probe_calls == 12
    assert asyncio.run(
        run_live_toy(config, tmp_path, TOY, "test", HashEmbedder(), routes=routes)
    )
    assert provider.toy_calls == 8
    spend = json.loads((tmp_path / "toy-spend.json").read_text())
    assert spend["reserved_attempts"] == 20
    assert spend["metered_upper_estimate_usd"] == pytest.approx(0.24)
    assert spend["unsettled_reservations_usd"] == 0
    assert asyncio.run(
        run_live_toy(config, tmp_path, TOY, "test", HashEmbedder(), routes=None)
    )
    assert provider.toy_calls == 8


def test_missing_credentials_and_rejected_model_are_reported(tmp_path, monkeypatch):
    class Rejected:
        async def generate(self, request):
            raise ProviderRequestError("provider rejected the request (HTTP 404)")

    for name in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_API_KEY",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert available_families() == frozenset()
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-key-available")
    assert available_families() == {"google"}
    config = campaign()
    report = asyncio.run(
        probe_roster(config, tmp_path, {MODEL: Route("openai", Rejected())})
    )
    assert [row.status for row in report.results] == [
        "failed",
        "not_run",
        "missing_credentials",
        "missing_credentials",
    ]
    assert report.results[0].detail == "provider rejected the request (HTTP 404)"
    assert 0 < report.spend["unsettled_reservations_usd"] < 1
    with pytest.raises(ValueError, match="credentials"):
        asyncio.run(
            run_live_toy(config, tmp_path, TOY, "test", HashEmbedder(), routes={})
        )


def test_shipped_campaign_covers_the_roster_and_can_reserve_any_model(tmp_path):
    config = LiveConfig.model_validate(
        yaml.safe_load((ROOT / "config/m1-live.yaml").read_text())
    )
    assert len(config.models) == 9
    assert {entry.family for entry in config.models.values()} == {
        "openai",
        "google",
        "anthropic",
    }
    guard = SpendGuard(
        tmp_path / "spend.sqlite",
        config.spend_cap_usd,
        {model: entry.limits for model, entry in config.models.items()},
    )
    for model in config.models:
        reservation = guard.reserve(
            "probe", "call", model, input_bound=512, output_bound=1024
        )
        assert reservation is not None
        guard.settle(
            reservation,
            model,
            Reply(
                outcome=Outcome.OK, input_tokens=1, output_tokens=1, usage_reported=True
            ),
        )


def test_soc_launch_requires_effort_probe_before_opening_run_or_spend(tmp_path):
    from soc.orchestrator.live import SOCCondition, run_live_soc
    from soc.triad.agreement import AgreementRules

    class NoCalls:
        async def generate(self, _request):
            pytest.fail("the launcher must validate probes before provider calls")

    config = LiveConfig.model_validate(
        yaml.safe_load((ROOT / "config/m1-live.yaml").read_text())
    )
    condition = SOCCondition.model_validate(
        {
            "run": {
                "run_id": "guarded-soc",
                "condition": "cross-examination",
                "episode_id": "toy",
                "initial_model": config.toy.initial_model,
                "successor_model": config.toy.successor_model,
                "effort": "low",
                "code_commit": "test",
            },
            "team": {
                "peer_model": "claude-sonnet-5-5",
                "reviewer_model": "gemini-3.8-flash",
            },
        }
    )
    agreement = AgreementRules.model_validate(
        yaml.safe_load((ROOT / "config/m2-agreement.yaml").read_text())
    )
    root = tmp_path / "campaign"
    run_dir = tmp_path / "run"
    routes = {
        model: Route(entry.family, NoCalls()) for model, entry in config.models.items()
    }
    with pytest.raises(ValueError, match="roster probe"):
        asyncio.run(
            run_live_soc(
                config,
                root,
                TOY,
                run_dir,
                condition=condition,
                answer_key=tmp_path / "unused.yaml",
                agreement=agreement,
                routes=routes,
            )
        )
    assert not root.exists() and not run_dir.exists()


def test_roster_request_bounds_support_three_outstanding_reservations(tmp_path):
    from soc.orchestrator.model_triad import DraftReply
    from soc.providers.anthropic import AnthropicProvider
    from soc.providers.gemini import GeminiProvider
    from soc.providers.http import payload_bytes
    from soc.providers.models import ProviderRequest
    from soc.providers.openai import OpenAIProvider

    builders = {
        "anthropic": AnthropicProvider.request_payload,
        "google": GeminiProvider.request_payload,
        "openai": OpenAIProvider.request_payload,
    }
    config = LiveConfig.model_validate(
        yaml.safe_load((ROOT / "config/m1-live.yaml").read_text())
    )
    experiment = config.experiment.model_copy(update={"max_output_tokens": 768})
    guard = SpendGuard(
        tmp_path / "spend.sqlite",
        config.spend_cap_usd,
        {model: row.limits for model, row in config.models.items()},
    )
    bounds = {}
    for model, entry in config.models.items():
        request = ProviderRequest(
            model=model,
            effort="low",
            schema_name="DraftReply",
            output_schema=DraftReply.model_json_schema(),
            sampling={"max_output_tokens": experiment.max_output_tokens},
            messages=(
                Message(role="system", content="Translate protocol text into Turkish."),
                Message(
                    role="user",
                    content="Translate the sentence and return its ID. 9999-1-s01: A gateway forwards packets.",
                ),
            ),
        )
        limits = (
            payload_bytes(builders[entry.family](request)),
            experiment.max_output_tokens,
        )
        assert entry.limits.charge(*limits) < 100_000_000
        bounds[model] = limits
    triad = ("gpt-6.1-sol", "claude-sonnet-5-5", "gemini-3.8-flash")
    with ThreadPoolExecutor(3) as pool:
        requests = [
            pool.submit(
                guard.reserve,
                "triad",
                key,
                key,
                input_bound=bounds[key][0],
                output_bound=bounds[key][1],
            )
            for key in triad
        ]
        assert all(request.result(timeout=5) for request in requests)
    assert 0 < guard.summary()["unsettled_reservations_usd"] < 0.3


@pytest.mark.parametrize(
    ("input_bound", "output_bound"), [(0, 50), (901, 50), (500, 101), (500, 0)]
)
def test_spend_refuses_bounds_outside_roster_ceilings(
    tmp_path, input_bound, output_bound
):
    guard = SpendGuard(tmp_path / "spend.sqlite", Decimal("40"), {MODEL: RATE})
    with pytest.raises(SpendBoundError, match="configured token limits"):
        guard.reserve(
            "triad", "first", MODEL, input_bound=input_bound, output_bound=output_bound
        )
    assert guard.summary()["reserved_attempts"] == 0


def test_usage_over_one_bound_trips_guard_even_below_total_reservation(tmp_path):
    guard = SpendGuard(tmp_path / "spend.sqlite", Decimal("40"), {MODEL: RATE})
    slot = guard.reserve("triad", "one", MODEL, input_bound=200, output_bound=100)
    assert slot is not None
    with pytest.raises(SpendBoundError):
        guard.settle(
            slot,
            MODEL,
            Reply(
                outcome=Outcome.OK,
                input_tokens=201,
                output_tokens=1,
                usage_reported=True,
            ),
        )
    assert guard.summary()["bound_exceeded"]
    assert (
        guard.reserve("triad", "next", MODEL, input_bound=100, output_bound=50) is None
    )
