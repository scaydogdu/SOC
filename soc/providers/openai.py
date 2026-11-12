"""OpenAI Responses API adapter."""

import httpx
from pydantic import BaseModel, Field

from soc.providers.http import api_key, closed_schema, post, sampling
from soc.providers.models import Outcome, ProviderRequest, Reply


class _Usage(BaseModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _Content(BaseModel):
    type: str
    text: str = ""


class _Item(BaseModel):
    type: str
    content: list[_Content] = Field(default_factory=list)


class _Reason(BaseModel):
    reason: str


class _Failure(BaseModel):
    code: str | None = None


class _Response(BaseModel):
    status: str
    output: list[_Item] = Field(default_factory=list)
    usage: _Usage | None = None
    incomplete_details: _Reason | None = None
    error: _Failure | None = None

    def outcome(self, content: list[_Content]) -> Outcome:
        if any(part.type == "refusal" for part in content):
            return Outcome.REFUSED
        if self.status == "completed":
            return Outcome.OK
        if self.incomplete_details is not None:
            if self.incomplete_details.reason == "content_filter":
                return Outcome.REFUSED
            if self.incomplete_details.reason == "max_output_tokens":
                return Outcome.CUT_OFF
        if self.error is not None:
            if self.error.code == "rate_limit_exceeded":
                return Outcome.RATE_LIMITED
            if self.error.code == "server_error":
                return Outcome.SERVER_ERROR
        return Outcome.PARSE_FAILED


class OpenAIProvider:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def generate(self, request: ProviderRequest) -> Reply:
        settings = sampling(request, {"max_output_tokens"})
        wire = await post(
            self._http,
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key('OPENAI_API_KEY')}"},
            payload={
                **settings,
                "model": request.model,
                "input": [
                    message.model_dump(mode="json") for message in request.messages
                ],
                "reasoning": {"effort": request.effort},
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": request.schema_name,
                        "strict": True,
                        "schema": closed_schema(
                            request.output_schema, require_all=True
                        ),
                    }
                },
                "store": False,
                "service_tier": "default",
                "prompt_cache_options": {"mode": "explicit"},
            },
            response_type=_Response,
        )
        if isinstance(wire, Reply):
            return wire
        content = [
            part
            for item in wire.output
            if item.type == "message"
            for part in item.content
        ]
        return Reply(
            outcome=wire.outcome(content),
            raw_text="".join(
                part.text for part in content if part.type == "output_text"
            ),
            input_tokens=wire.usage.input_tokens if wire.usage else 0,
            output_tokens=wire.usage.output_tokens if wire.usage else 0,
            usage_reported=wire.usage is not None,
        )
