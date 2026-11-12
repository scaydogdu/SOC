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
        a = threads.submit(first.reserve, "probe", "a", MODEL)
        b = threads.submit(second.reserve, "toy", "b", MODEL)
        allowed = [
            reservation
            for reservation in (a.result(), b.result())
            if reservation is not None
        ]
    assert len(allowed) == 1
    reopened = SpendGuard(path, Decimal("1"), {MODEL: RATE})
    assert reopened.summary()["unsettled_reservations_usd"] == 1
    reopened.settle(allowed[0], MODEL, Reply(outcome=Outcome.SERVER_ERROR))
    assert reopened.reserve("probe", "retry", MODEL) is None
    with pytest.raises(ValueError, match="different spend cap"):
        SpendGuard(path, Decimal("2"), {MODEL: RATE})


def test_usage_over_bound_stops_the_campaign(tmp_path):
    guard = SpendGuard(tmp_path / "spend.sqlite", Decimal("4"), {MODEL: RATE})
    reservation = guard.reserve("probe", "a", MODEL)
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
    assert guard.reserve("toy", "b", MODEL) is None


def test_spend_cap_is_checked_before_each_physical_attempt(tmp_path):
    from soc.orchestrator.live import ProbeAnswer

    class Timeout:
        attempts = 0

        async def generate(self, request):
            self.attempts += 1
            return Reply(outcome=Outcome.SERVER_ERROR)

    provider = Timeout()
    guard = SpendGuard(tmp_path / "spend.sqlite", Decimal("1"), {MODEL: RATE})
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
            )
        )
    )
    assert result.reply.outcome is Outcome.BUDGET_EXHAUSTED
    assert provider.attempts == 1
    assert guard.summary()["unsettled_reservations_usd"] == 1


def campaign():
    return LiveConfig.model_validate(
        {
            "spend_cap_usd": "10",
            "token_cap_per_run": 5000,
            "probe_max_output_tokens": 100,
            "models": {
                MODEL: {"family": "openai", "limits": RATE},
                SUCCESSOR: {"family": "openai", "limits": RATE},
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
    assert report.spend["unsettled_reservations_usd"] == 1
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
        reservation = guard.reserve("probe", "call", model)
        assert reservation is not None
        guard.settle(
            reservation,
            model,
            Reply(
                outcome=Outcome.OK, input_tokens=1, output_tokens=1, usage_reported=True
            ),
        )
