import asyncio
import gzip
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from soc.providers.anthropic import AnthropicProvider
from soc.providers.cache import ResponseCache
from soc.providers.client import ModelClient, Price, ProviderLimits, Route, RunLimits
from soc.providers.fake import ScriptedProvider, ScriptMismatch
from soc.providers.gemini import GeminiProvider
from soc.providers.http import closed_schema
from soc.providers.models import (
    Message,
    Mode,
    Outcome,
    Provider,
    ProviderRequest,
    ProviderRequestError,
    Request,
)
from soc.providers.openai import OpenAIProvider


class Translation(BaseModel):
    value: str


class Answer(BaseModel):
    rows: list[Translation]
    rationale: str | None = None


MESSAGES = (
    Message(role="system", content="Preserve sentence IDs."),
    Message(role="user", content="Translate this."),
    Message(role="assistant", content="Previous translation."),
    Message(role="user", content="Continue."),
)
RAW = '{"rows":[{"value":"erişim"}],"rationale":null}'
MODELS = {
    OpenAIProvider: "gpt-6.1-sol",
    AnthropicProvider: "claude-sonnet-5-5",
    GeminiProvider: "gemini-3.8-flash",
}
SUCCESS = {
    OpenAIProvider: {
        "status": "completed",
        "output": [
            {"type": "reasoning"},
            {"type": "message", "content": [{"type": "output_text", "text": RAW}]},
        ],
        "usage": {"input_tokens": 11, "output_tokens": 9},
    },
    AnthropicProvider: {
        "stop_reason": "end_turn",
        "content": [{"type": "thinking"}, {"type": "text", "text": RAW}],
        "usage": {
            "input_tokens": 4,
            "cache_creation_input_tokens": 3,
            "cache_read_input_tokens": 4,
            "output_tokens": 9,
        },
    },
    GeminiProvider: {
        "candidates": [
            {
                "finishReason": "STOP",
                "content": {
                    "parts": [
                        {"text": "hidden thought", "thought": True},
                        {"text": RAW},
                    ],
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 11,
            "candidatesTokenCount": 6,
            "thoughtsTokenCount": 3,
            "totalTokenCount": 20,
        },
    },
}


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-openai")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-anthropic")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-gemini")


def wire_request(provider: type[Provider], **updates) -> ProviderRequest:
    fields = {
        "model": MODELS[provider],
        "effort": "high",
        "messages": MESSAGES,
        "schema_name": "Answer",
        "output_schema": Answer.model_json_schema(),
        "sampling": {"max_output_tokens": 2048},
    }
    return ProviderRequest(**(fields | updates))


def generate(provider: type[Provider], request: ProviderRequest, handler: Callable):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await provider(http).generate(request)

    return asyncio.run(run())


@pytest.mark.parametrize(
    "provider", [OpenAIProvider, AnthropicProvider, GeminiProvider]
)
@pytest.mark.parametrize("effort", ["low", "high"])
def test_wire_contract(provider, effort):
    def handler(request):
        data = json.loads(request.content)
        assert request.method == "POST"
        assert not request.url.query
        if provider is OpenAIProvider:
            assert request.url.path == "/v1/responses"
            assert request.headers["authorization"] == "Bearer synthetic-openai"
            assert data["model"] == "gpt-6.1-sol"
            assert data["max_output_tokens"] == 2048
            assert data["reasoning"] == {"effort": effort}
            assert data["store"] is False
            assert data["service_tier"] == "default"
            assert data["prompt_cache_options"] == {"mode": "explicit"}
            assert [item["role"] for item in data["input"]] == [
                "system",
                "user",
                "assistant",
                "user",
            ]
            output = data["text"]["format"]
            assert output["type"] == "json_schema" and output["strict"]
            schema = output["schema"]
            assert schema["required"] == ["rows", "rationale"]
        elif provider is AnthropicProvider:
            assert request.url.path == "/v1/messages"
            assert request.headers["x-api-key"] == "synthetic-anthropic"
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert data["model"] == "claude-sonnet-5-5"
            assert data["max_tokens"] == 2048
            assert data["system"] == [{"type": "text", "text": MESSAGES[0].content}]
            assert [item["role"] for item in data["messages"]] == [
                "user",
                "assistant",
                "user",
            ]
            assert data["output_config"]["effort"] == effort
            output = data["output_config"]["format"]
            assert output["type"] == "json_schema"
            schema = output["schema"]
            assert schema["required"] == ["rows"]
        else:
            assert request.url.path == "/v1beta/models/gemini-3.8-flash:generateContent"
            assert request.headers["x-goog-api-key"] == "synthetic-gemini"
            assert data["systemInstruction"] == {
                "parts": [{"text": MESSAGES[0].content}]
            }
            assert [item["role"] for item in data["contents"]] == [
                "user",
                "model",
                "user",
            ]
            config = data["generationConfig"]
            assert config["maxOutputTokens"] == 2048
            assert config["thinkingConfig"] == {"thinkingLevel": effort.upper()}
            assert config["responseMimeType"] == "application/json"
            assert config["responseJsonSchema"] == Answer.model_json_schema()
            return httpx.Response(200, json=SUCCESS[provider])
        assert schema["additionalProperties"] is False
        assert schema["$defs"]["Translation"]["additionalProperties"] is False
        assert "default" not in schema["properties"]["rationale"]
        return httpx.Response(200, json=SUCCESS[provider])

    result = generate(provider, wire_request(provider, effort=effort), handler)
    assert (result.outcome, result.input_tokens, result.output_tokens) == (
        Outcome.OK,
        11,
        9,
    )
    assert result.raw_text == RAW
    assert Answer.model_validate_json(result.raw_text).rows[0].value == "erişim"


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        (429, Outcome.RATE_LIMITED),
        (408, Outcome.SERVER_ERROR),
        (503, Outcome.SERVER_ERROR),
        (200, Outcome.PARSE_FAILED),
    ],
)
def test_http_outcomes(status, outcome):
    result = generate(
        OpenAIProvider,
        wire_request(OpenAIProvider),
        lambda _: httpx.Response(status, text="bad"),
    )
    assert result.outcome is outcome


def test_transport_rejection_and_validation(monkeypatch):
    def network_failure(request):
        raise httpx.ReadTimeout("network unavailable", request=request)

    req = wire_request(OpenAIProvider)
    assert (
        generate(OpenAIProvider, req, network_failure).outcome is Outcome.SERVER_ERROR
    )
    with pytest.raises(ProviderRequestError, match="HTTP 404") as error:
        generate(
            OpenAIProvider, req, lambda _: httpx.Response(404, text="sensitive body")
        )
    assert "sensitive body" not in str(error.value)
    with pytest.raises(ProviderRequestError, match="unsupported sampling"):
        generate(
            OpenAIProvider,
            wire_request(OpenAIProvider, sampling={"top_p": 0.5}),
            network_failure,
        )
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ProviderRequestError, match="OPENAI_API_KEY"):
        generate(OpenAIProvider, req, network_failure)
    with pytest.raises(ProviderRequestError, match="object root"):
        closed_schema({"type": "array"}, require_all=True)
    with pytest.raises(ProviderRequestError, match="declared fields"):
        closed_schema(
            {"type": "object", "additionalProperties": {"type": "integer"}},
            require_all=True,
        )


@pytest.mark.parametrize(
    ("provider", "data", "outcome"),
    [
        (
            OpenAIProvider,
            {
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal"}]}],
            },
            Outcome.REFUSED,
        ),
        (
            OpenAIProvider,
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "content_filter"},
            },
            Outcome.REFUSED,
        ),
        (
            OpenAIProvider,
            {
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
            },
            Outcome.CUT_OFF,
        ),
        (
            OpenAIProvider,
            {"status": "failed", "error": {"code": "rate_limit_exceeded"}},
            Outcome.RATE_LIMITED,
        ),
        (
            OpenAIProvider,
            {"status": "failed", "error": {"code": "server_error"}},
            Outcome.SERVER_ERROR,
        ),
        (
            OpenAIProvider,
            {"status": "failed", "error": {"code": "invalid_prompt"}},
            Outcome.PARSE_FAILED,
        ),
        (
            AnthropicProvider,
            {
                "stop_reason": "refusal",
                "content": [],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            Outcome.REFUSED,
        ),
        (
            AnthropicProvider,
            {
                "stop_reason": "max_tokens",
                "content": [],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            Outcome.CUT_OFF,
        ),
        (
            GeminiProvider,
            {"promptFeedback": {"blockReason": "SAFETY"}},
            Outcome.REFUSED,
        ),
        (
            GeminiProvider,
            {"candidates": [{"finishReason": "MAX_TOKENS"}]},
            Outcome.CUT_OFF,
        ),
        (GeminiProvider, {"candidates": [{"finishReason": "SAFETY"}]}, Outcome.REFUSED),
        (GeminiProvider, {}, Outcome.PARSE_FAILED),
    ],
)
def test_provider_finish_states(provider, data, outcome):
    req = wire_request(provider, messages=MESSAGES[1:])
    assert (
        generate(provider, req, lambda _: httpx.Response(200, json=data)).outcome
        is outcome
    )


def test_yaml_fake_records_retries_and_replays(tmp_path: Path):
    script = tmp_path / "fake.yaml"
    script.write_text(
        """
steps:
  - match: {model: "fake-*", effort: high, contains: "Translate this"}
    outcome: rate_limited
  - outcome: refused
  - raw_text: "not JSON"
  - match: {contains: "Return valid JSON"}
    response: {rows: [{value: "erişim"}], rationale: null}
    delay_seconds: 0.001
    input_tokens: 9
    output_tokens: 4
  - outcome: cut_off
    raw_text: '{"rows":'
""",
        encoding="utf-8",
    )
    fake = ScriptedProvider(script)
    cache = ResponseCache(tmp_path / "cache.sqlite")
    common = {
        "run_id": "test",
        "cache": cache,
        "limits": RunLimits(token_cap=500, initial_backoff_seconds=0),
        "prices": {"fake-1": Price(input_per_million=0, output_per_million=0)},
    }
    live = ModelClient(
        **common,
        mode=Mode.LIVE,
        run_dir=tmp_path / "live",
        routes={"fake-1": Route("fake", fake)},
        provider_limits={"fake": ProviderLimits(requests_per_minute=1e12)},
    )
    replay = ModelClient(**common, mode=Mode.REPLAY, run_dir=tmp_path / "replay")

    async def run():
        wrong = wire_request(OpenAIProvider, model="fake-1", effort="low")
        with pytest.raises(ScriptMismatch, match="step 1"):
            await fake.generate(wrong)
        with pytest.raises(ScriptMismatch, match="remain"):
            fake.assert_finished()
        for call_id, expected in (("first", Outcome.OK), ("second", Outcome.CUT_OFF)):
            call = Request(
                model="fake-1",
                effort="high",
                messages=MESSAGES,
                schema=Answer,
                replicate=0,
                run_id="test",
                call_id=call_id,
            )
            result = await live.generate(call)
            assert result.reply.outcome is expected
            assert await replay.generate(call) == result
        fake.assert_finished()
        with pytest.raises(ScriptMismatch, match="exhausted"):
            await fake.generate(wrong)

    asyncio.run(run())


@pytest.mark.parametrize(
    "step",
    [
        "outcome: ok",
        "raw_text: raw\n    response: {rows: []}",
        "outcome: cache_miss",
    ],
)
def test_invalid_fake_script(tmp_path, step):
    path = tmp_path / "invalid.yaml"
    path.write_text(f"steps:\n  - {step}\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        ScriptedProvider(path)


@pytest.mark.parametrize("provider", [AnthropicProvider, GeminiProvider])
def test_provider_transient_error(provider):
    result = generate(provider, wire_request(provider), lambda _: httpx.Response(503))
    assert result.outcome is Outcome.SERVER_ERROR


def test_permanent_rejection_logged_without_repair_or_error_body(tmp_path):
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(401, text="synthetic-openai")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            model = ModelClient(
                run_id="run",
                mode=Mode.LIVE,
                cache=ResponseCache(tmp_path / "cache.sqlite"),
                run_dir=tmp_path,
                limits=RunLimits(token_cap=500),
                prices={
                    "gpt-6.1-sol": Price(input_per_million=0, output_per_million=0)
                },
                routes={"gpt-6.1-sol": Route("openai", OpenAIProvider(http))},
                provider_limits={"openai": ProviderLimits(requests_per_minute=1000)},
            )
            call = Request(
                model="gpt-6.1-sol",
                effort="low",
                messages=MESSAGES,
                schema=Answer,
                replicate=0,
                run_id="run",
                call_id="probe",
            )
            with pytest.raises(ProviderRequestError, match="HTTP 401"):
                await model.generate(call)

    asyncio.run(run())
    assert calls == 1
    with gzip.open(tmp_path / "calls.jsonl.gz", "rt") as log:
        (entry,) = log.readlines()
    assert json.loads(entry)["outcome"] == "parse_failed"
    assert "synthetic-openai" not in entry
