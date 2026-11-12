"""Provider-neutral requests, outcomes, and transport contract."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class Outcome(StrEnum):
    OK = "ok"
    PARSE_FAILED = "parse_failed"
    REFUSED = "refused"
    CUT_OFF = "cut_off"
    RATE_LIMITED = "rate_limited"
    SERVER_ERROR = "server_error"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CACHE_MISS = "cache_miss"


class Mode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"
    RECORD_MISSING = "record_missing"


class ProviderRequestError(RuntimeError):
    """A request configuration or a permanent provider rejection."""


class Message(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


Output = TypeVar("Output", bound=BaseModel)


@dataclass(frozen=True)
class Request[Output: BaseModel]:
    model: str
    effort: Literal["low", "high"]
    messages: tuple[Message, ...]
    schema: type[Output]
    replicate: int
    run_id: str
    call_id: str
    sampling: dict[str, JsonValue] = field(default_factory=dict)


class ProviderRequest(BaseModel):
    """Parameters an adapter needs for one physical attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    effort: Literal["low", "high"]
    messages: tuple[Message, ...]
    schema_name: str
    output_schema: dict[str, JsonValue]
    sampling: dict[str, JsonValue]


class Reply(BaseModel):
    """An adapter returns response text and usage without raw HTTP data."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    outcome: Outcome
    raw_text: str = ""
    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0
    usage_reported: bool = False


class RecordedReply(Reply):
    latency_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]


@dataclass(frozen=True)
class Response[Output: BaseModel]:
    reply: RecordedReply
    parsed: Output | None
    estimated_cost_usd: float


class Provider(Protocol):
    async def generate(self, request: ProviderRequest) -> Reply:
        """Classify provider errors and return a single attempt's outcome."""
        ...
