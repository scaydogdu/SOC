import asyncio
import gzip
import json
from collections import deque
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from soc.providers.cache import ResponseCache
from soc.providers.client import (
    CacheMiss,
    ModelClient,
    Price,
    ProviderLimits,
    Route,
    RunLimits,
)
from soc.providers.models import Message, Mode, Outcome, ProviderRequest, Reply, Request


class Answer(BaseModel):
    model_config = ConfigDict(strict=True)
    value: str


class OtherAnswer(BaseModel):
    count: int


class Script:
    def __init__(self, *replies: Reply) -> None:
        self.replies = deque(replies)
        self.requests: list[ProviderRequest] = []

    async def generate(self, request: ProviderRequest) -> Reply:
        self.requests.append(request)
        return self.replies.popleft()


def good(value: str) -> Reply:
    return Reply(
        outcome=Outcome.OK,
        raw_text=json.dumps({"value": value}),
        input_tokens=8,
        output_tokens=2,
    )


def request(call_id: str = "c1") -> Request[Answer]:
    return Request(
        model="family/model",
        effort="low",
        messages=(Message(role="user", content="Translate erişim."),),
        schema=Answer,
        replicate=0,
        run_id="run",
        call_id=call_id,
        sampling={"max_output_tokens": 128, "seed": 7},
    )


def client(
    tmp_path: Path,
    mode: Mode,
    provider=None,
    *,
    cap: int = 1000,
    concurrent: int = 1,
    initial_tokens: int = 0,
    retries: int = 2,
    rpm: float = 1e12,
) -> ModelClient:
    return ModelClient(
        run_id="run",
        mode=mode,
        cache=ResponseCache(tmp_path / "cache.sqlite"),
        run_dir=tmp_path / mode.value,
        limits=RunLimits(
            token_cap=cap, initial_backoff_seconds=0, transient_retries=retries
        ),
        prices={
            "family/model": Price(input_per_million=2, output_per_million=5),
            "family/other": Price(input_per_million=2, output_per_million=5),
        },
        routes={"family/model": Route("family", provider)}
        if provider is not None
        else None,
        provider_limits={
            "family": ProviderLimits(concurrent=concurrent, requests_per_minute=rpm)
        },
        initial_tokens=initial_tokens,
    )


def logs(tmp_path: Path, mode: Mode) -> list[dict]:
    with gzip.open(tmp_path / mode.value / "calls.jsonl.gz", "rt") as file:
        return [json.loads(line) for line in file]


def test_live_records_each_call_and_replay_preserves_identical_request_outputs(
    tmp_path,
):
    provider = Script(good("bir"), good("iki"))
    live = client(tmp_path, Mode.LIVE, provider)
    replay = client(tmp_path, Mode.REPLAY)

    async def scenario():
        first = await live.generate(request())
        second = await live.generate(request("c2"))
        assert first.parsed == Answer(value="bir")
        assert second.parsed == Answer(value="iki")
        assert first.estimated_cost_usd == pytest.approx(0.000026)
        assert await replay.generate(request()) == first
        assert await replay.generate(request("c2")) == second

    asyncio.run(scenario())
    assert len(provider.requests) == 2
    assert replay.tokens == live.tokens == 20
    assert [row["attempt"] for row in logs(tmp_path, Mode.LIVE)] == [1, None, 1, None]
    assert {row["source"] for row in logs(tmp_path, Mode.REPLAY)} == {"cache"}


def test_cache_key_covers_request_and_record_missing_reuses_hit(tmp_path):
    req = request()
    provider = Script(good("recorded"))
    recording = client(tmp_path, Mode.RECORD_MISSING, provider)
    replay = client(tmp_path, Mode.REPLAY)

    async def scenario():
        original = await recording.generate(req)
        reordered = {"seed": 7, "max_output_tokens": 128}
        assert (
            await recording.generate(replace(req, sampling=reordered, call_id="c2"))
            == original
        )
        for changed in (
            replace(req, model="family/other"),
            replace(req, replicate=1),
            replace(req, effort="high"),
            replace(
                req, messages=(*req.messages, Message(role="assistant", content="ok"))
            ),
            replace(req, sampling={"seed": 8}),
            replace(req, schema=OtherAnswer),
        ):
            with pytest.raises(CacheMiss, match="no recorded response"):
                await replay.generate(changed)

    asyncio.run(scenario())
    assert len(provider.requests) == 1
    entries = logs(tmp_path, Mode.REPLAY)
    assert len({row["cache_key"] for row in entries}) == 6
    assert {row["outcome"] for row in entries} == {"cache_miss"}
    with pytest.raises(ValueError, match="replay does not accept"):
        client(tmp_path, Mode.REPLAY, provider)


@pytest.mark.parametrize(
    ("attempts", "outcome", "extra_message"),
    [
        ([Outcome.RATE_LIMITED, Outcome.SERVER_ERROR, Outcome.OK], Outcome.OK, False),
        ([Outcome.RATE_LIMITED] * 3, Outcome.RATE_LIMITED, False),
        ([Outcome.PARSE_FAILED] * 2, Outcome.PARSE_FAILED, True),
        ([Outcome.PARSE_FAILED, Outcome.OK], Outcome.OK, True),
        ([Outcome.REFUSED] * 2, Outcome.REFUSED, False),
        ([Outcome.CUT_OFF], Outcome.CUT_OFF, False),
    ],
)
def test_retry_classification(tmp_path, attempts, outcome, extra_message):
    replies = [
        Reply(outcome=Outcome.OK, raw_text='{"value": 7}', input_tokens=1)
        if attempt is Outcome.PARSE_FAILED
        else good("ok")
        if attempt is Outcome.OK
        else Reply(outcome=attempt, input_tokens=1)
        for attempt in attempts
    ]
    provider = Script(*replies)
    model = client(tmp_path, Mode.LIVE, provider)
    result = asyncio.run(model.generate(request()))
    assert result.reply.outcome is outcome
    assert len(provider.requests) == len(attempts)
    assert (len(provider.requests[-1].messages) == 2) is extra_message
    assert result.reply.input_tokens == sum(reply.input_tokens for reply in replies)
    assert (result.parsed is not None) is (outcome is Outcome.OK)


def test_backoff_and_rate_spacing(tmp_path, monkeypatch):
    now = 100.0
    delays = []

    async def sleep(seconds):
        nonlocal now
        delays.append(seconds)
        now += seconds

    monkeypatch.setattr("soc.providers.client.monotonic", lambda: now)
    monkeypatch.setattr("soc.providers.client.asyncio.sleep", sleep)
    monkeypatch.setattr("soc.providers.client.random.uniform", lambda low, high: high)
    provider = Script(
        Reply(outcome=Outcome.RATE_LIMITED),
        Reply(outcome=Outcome.SERVER_ERROR),
        good("ok"),
    )
    model = client(tmp_path, Mode.LIVE, provider, rpm=30)
    model.limits = RunLimits(token_cap=1000, initial_backoff_seconds=0.5)
    assert asyncio.run(model.generate(request())).reply.outcome is Outcome.OK
    assert delays == [0.5, 1.5, 1.0, 1.0]


def test_budget_stops_queued_attempts_and_replays_partial_run(tmp_path):
    class SlowProvider:
        attempts = 0

        async def generate(self, request):
            self.attempts += 1
            await asyncio.sleep(0)
            return good("first")

    provider = SlowProvider()
    model = client(tmp_path, Mode.LIVE, provider, cap=10)
    replay = client(tmp_path, Mode.REPLAY, cap=10)

    async def scenario():
        first, second = await asyncio.gather(
            model.generate(request()), model.generate(request("c2"))
        )
        assert first.parsed == Answer(value="first")
        assert second.reply.outcome is Outcome.BUDGET_EXHAUSTED
        assert await replay.generate(request()) == first
        assert (
            await replay.generate(request("c2"))
        ).reply.outcome is Outcome.BUDGET_EXHAUSTED

    asyncio.run(scenario())
    assert provider.attempts == 1


def test_retry_budget_exhaustion_records_prior_usage_for_replay(tmp_path):
    provider = Script(Reply(outcome=Outcome.SERVER_ERROR, input_tokens=4))
    live = client(tmp_path, Mode.LIVE, provider, cap=4)
    replay = client(tmp_path, Mode.REPLAY, cap=4)
    first = asyncio.run(live.generate(request()))
    assert first.reply.outcome is Outcome.BUDGET_EXHAUSTED
    assert first.reply.input_tokens == 4
    assert asyncio.run(replay.generate(request())) == first
    assert replay.tokens == 4


def test_credentials_stay_out_of_cache_and_log(tmp_path, monkeypatch):
    secret = "synthetic-secret-92fb"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    provider = Script(
        good(secret), Reply(outcome=Outcome.REFUSED, raw_text=secret), good("safe")
    )
    model = client(tmp_path, Mode.LIVE, provider)
    with pytest.raises(ValueError, match="provider credential"):
        asyncio.run(model.generate(request(secret)))
    assert asyncio.run(model.generate(request())).parsed == Answer(value="safe")
    assert secret.encode() not in (tmp_path / "cache.sqlite").read_bytes()
    assert secret not in json.dumps(logs(tmp_path, Mode.LIVE))


def test_invalid_configuration_and_request(tmp_path):
    with pytest.raises(ValidationError):
        ProviderLimits(requests_per_minute=0)
    with pytest.raises(ValueError, match="nonnegative"):
        client(tmp_path, Mode.LIVE, initial_tokens=-1)
    model = client(tmp_path, Mode.LIVE, cap=0)
    with pytest.raises(ValueError, match="different run"):
        asyncio.run(model.generate(replace(request(), run_id="other")))
    with pytest.raises(ValueError, match="replicate must be nonnegative"):
        asyncio.run(model.generate(replace(request(), replicate=-1)))
    assert (
        asyncio.run(model.generate(request())).reply.outcome is Outcome.BUDGET_EXHAUSTED
    )
    with pytest.raises(ValueError, match="configured limit"):
        ModelClient(
            run_id="run",
            mode=Mode.LIVE,
            cache=ResponseCache(tmp_path / "cache.sqlite"),
            run_dir=tmp_path,
            limits=RunLimits(token_cap=10),
            prices={},
            routes={"family/model": Route("family", Script())},
        )


def test_concurrent_recording_replays_already_started_calls(tmp_path):
    class OverlappingProvider:
        def __init__(self):
            self.started = 0
            self.ready = asyncio.Event()

        async def generate(self, request):
            self.started += 1
            value = str(self.started)
            if self.started == 2:
                self.ready.set()
            await self.ready.wait()
            return good(value)

    provider = OverlappingProvider()
    live = client(tmp_path, Mode.LIVE, provider, concurrent=2, cap=10)
    replay = client(tmp_path, Mode.REPLAY, cap=10)

    async def scenario():
        calls = [request(f"c{number}") for number in (1, 2, 3)]
        recorded = await asyncio.wait_for(
            asyncio.gather(*(live.generate(call) for call in calls)), timeout=2
        )
        assert [item.reply.outcome for item in recorded] == [
            Outcome.OK,
            Outcome.OK,
            Outcome.BUDGET_EXHAUSTED,
        ]
        assert [await replay.generate(call) for call in calls] == recorded

    asyncio.run(scenario())
    assert provider.started == 2
    assert live.tokens == replay.tokens == 20
