"""Anthropic Messages API adapter."""

import httpx
from pydantic import BaseModel, Field

from soc.providers.http import api_key, closed_schema, post, sampling
from soc.providers.models import Outcome, ProviderRequest, Reply

_STOP = {
    "end_turn": Outcome.OK,
    "stop_sequence": Outcome.OK,
    "refusal": Outcome.REFUSED,
    "max_tokens": Outcome.CUT_OFF,
    "model_context_window_exceeded": Outcome.CUT_OFF,
}


class _Usage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_creation_input_tokens: int | None = Field(default=None, ge=0)
    cache_read_input_tokens: int | None = Field(default=None, ge=0)


class _Block(BaseModel):
    type: str
    text: str = ""


class _Response(BaseModel):
    content: list[_Block]
    stop_reason: str
    usage: _Usage


class AnthropicProvider:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def generate(self, request: ProviderRequest) -> Reply:
        settings = sampling(request, {"max_output_tokens"})
        system = [
            message.content for message in request.messages if message.role == "system"
        ]
        wire = await post(
            self._http,
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key("ANTHROPIC_API_KEY"),
                "anthropic-version": "2023-06-01",
            },
            payload={
                "model": request.model,
                "max_tokens": settings.get("max_output_tokens", 4096),
                "system": [{"type": "text", "text": text} for text in system],
                "messages": [
                    message.model_dump(mode="json")
                    for message in request.messages
                    if message.role != "system"
                ],
                "output_config": {
                    "effort": request.effort,
                    "format": {
                        "type": "json_schema",
                        "schema": closed_schema(
                            request.output_schema, require_all=False
                        ),
                    },
                },
            },
            response_type=_Response,
        )
        if isinstance(wire, Reply):
            return wire
        return Reply(
            outcome=_STOP.get(wire.stop_reason, Outcome.PARSE_FAILED),
            raw_text="".join(
                block.text for block in wire.content if block.type == "text"
            ),
            input_tokens=(
                wire.usage.input_tokens
                + (wire.usage.cache_creation_input_tokens or 0)
                + (wire.usage.cache_read_input_tokens or 0)
            ),
            output_tokens=wire.usage.output_tokens,
            usage_reported=True,
        )
