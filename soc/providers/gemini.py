"""Google Gemini Generate Content API adapter."""

from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, JsonValue

from soc.providers.http import JsonObject, api_key, post, sampling
from soc.providers.models import Outcome, ProviderRequest, Reply

_SAMPLING = {
    "max_output_tokens": "maxOutputTokens",
    "temperature": "temperature",
    "top_p": "topP",
    "seed": "seed",
}
_REFUSALS = {
    "SAFETY",
    "RECITATION",
    "BLOCKLIST",
    "PROHIBITED_CONTENT",
    "SPII",
    "IMAGE_SAFETY",
    "IMAGE_PROHIBITED_CONTENT",
    "IMAGE_RECITATION",
}


class _Usage(BaseModel):
    prompt: int = Field(default=0, alias="promptTokenCount", ge=0)
    output: int = Field(default=0, alias="candidatesTokenCount", ge=0)
    thoughts: int = Field(default=0, alias="thoughtsTokenCount", ge=0)
    total: int | None = Field(default=None, alias="totalTokenCount", ge=0)


class _Part(BaseModel):
    text: str = ""
    thought: bool = False


class _Content(BaseModel):
    parts: list[_Part] = Field(default_factory=list)


class _Candidate(BaseModel):
    content: _Content = Field(default_factory=_Content)
    finish_reason: str = Field(default="", alias="finishReason")


class _Feedback(BaseModel):
    reason: str = Field(default="", alias="blockReason")


class _Response(BaseModel):
    candidates: list[_Candidate] = Field(default_factory=list)
    feedback: _Feedback = Field(default_factory=_Feedback, alias="promptFeedback")
    usage: _Usage = Field(default_factory=_Usage, alias="usageMetadata")

    def outcome(self, reason: str) -> Outcome:
        if (
            self.feedback.reason not in ("", "BLOCK_REASON_UNSPECIFIED")
            or reason in _REFUSALS
        ):
            return Outcome.REFUSED
        if reason == "MAX_TOKENS":
            return Outcome.CUT_OFF
        return Outcome.OK if reason == "STOP" else Outcome.PARSE_FAILED


class GeminiProvider:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def generate(self, request: ProviderRequest) -> Reply:
        settings = sampling(request, set(_SAMPLING))
        instructions: list[JsonValue] = [
            {"text": message.content}
            for message in request.messages
            if message.role == "system"
        ]
        payload: JsonObject = {
            "contents": [
                {
                    "role": "model" if message.role == "assistant" else "user",
                    "parts": [{"text": message.content}],
                }
                for message in request.messages
                if message.role != "system"
            ],
            "generationConfig": {
                **{_SAMPLING[name]: value for name, value in settings.items()},
                "responseMimeType": "application/json",
                "responseJsonSchema": request.output_schema,
                "thinkingConfig": {"thinkingLevel": request.effort.upper()},
            },
        }
        if instructions:
            payload["systemInstruction"] = {"parts": instructions}
        wire = await post(
            self._http,
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{quote(request.model, safe='')}:generateContent",
            headers={"x-goog-api-key": api_key("GEMINI_API_KEY", "GOOGLE_API_KEY")},
            payload=payload,
            response_type=_Response,
        )
        if isinstance(wire, Reply):
            return wire
        candidate = wire.candidates[0] if wire.candidates else _Candidate()
        return Reply(
            outcome=wire.outcome(candidate.finish_reason),
            raw_text="".join(
                part.text for part in candidate.content.parts if not part.thought
            ),
            input_tokens=wire.usage.prompt,
            output_tokens=max(
                wire.usage.output + wire.usage.thoughts,
                (wire.usage.total or 0) - wire.usage.prompt,
            ),
            usage_reported=wire.usage.prompt > 0 and wire.usage.total is not None,
        )
