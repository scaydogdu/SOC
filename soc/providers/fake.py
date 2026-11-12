"""Offline provider driven by ordered YAML request patterns and responses."""

import asyncio
import json
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from soc.providers.models import Outcome, ProviderRequest, Reply


class _Match(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = "*"
    effort: Literal["low", "high"] | None = None
    contains: str = ""

    def accepts(self, request: ProviderRequest) -> bool:
        return (
            fnmatchcase(request.model, self.model)
            and (self.effort is None or request.effort == self.effort)
            and any(self.contains in message.content for message in request.messages)
        )


class _Step(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    match: _Match = Field(default_factory=_Match)
    outcome: Literal[
        Outcome.OK,
        Outcome.PARSE_FAILED,
        Outcome.REFUSED,
        Outcome.CUT_OFF,
        Outcome.RATE_LIMITED,
        Outcome.SERVER_ERROR,
    ] = Outcome.OK
    response: dict[str, JsonValue] | None = None
    raw_text: str | None = None
    delay_seconds: float = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_content(self) -> Self:
        if self.response is not None and self.raw_text is not None:
            raise ValueError("choose either response or raw_text")
        if (
            self.outcome == Outcome.OK
            and self.response is None
            and self.raw_text is None
        ):
            raise ValueError("a successful step requires response or raw_text")
        return self

    def reply(self) -> Reply:
        raw = self.raw_text or ""
        if self.response is not None:
            raw = json.dumps(self.response, ensure_ascii=False, allow_nan=False)
        return Reply(
            outcome=Outcome(self.outcome),
            raw_text=raw,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
        )


class _Script(BaseModel):
    model_config = ConfigDict(extra="forbid")

    steps: Annotated[list[_Step], Field(min_length=1)]


class ScriptMismatch(AssertionError):
    """The next physical attempt differs from the scripted expectation."""


class ScriptedProvider:
    def __init__(self, path: Path, *, consumed_attempts: int = 0) -> None:
        self._script = _Script.model_validate(
            yaml.safe_load(path.read_text(encoding="utf-8"))
        )
        if not 0 <= consumed_attempts <= len(self._script.steps):
            raise ValueError("recorded attempts lie outside the supplied script")
        self._next = consumed_attempts

    async def generate(self, request: ProviderRequest) -> Reply:
        if self._next >= len(self._script.steps):
            raise ScriptMismatch("script exhausted before the next provider attempt")
        step = self._script.steps[self._next]
        if not step.match.accepts(request):
            raise ScriptMismatch(
                f"provider attempt does not match script step {self._next + 1}"
            )
        self._next += 1
        if step.delay_seconds:
            await asyncio.sleep(step.delay_seconds)
        return step.reply()

    def assert_finished(self) -> None:
        remaining = len(self._script.steps) - self._next
        if remaining:
            raise ScriptMismatch(f"{remaining} scripted provider attempt(s) remain")
