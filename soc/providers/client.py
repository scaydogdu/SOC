"""Record and replay validated model calls with run-wide accounting."""

import asyncio
import gzip
import json
import os
import random
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from soc.providers.cache import RETRYABLE_OUTCOMES, ResponseCache, cache_key
from soc.providers.http import output_limit, payload_bytes
from soc.providers.models import (
    Message,
    Mode,
    Outcome,
    Output,
    Provider,
    ProviderRequest,
    ProviderRequestError,
    RecordedReply,
    Reply,
    Request,
    Response,
    WireProvider,
)
from soc.providers.spend import SpendGuard

SCHEMA_REPAIR_INSTRUCTION = "Return valid JSON matching the supplied schema."


class CacheMiss(LookupError):
    outcome = Outcome.CACHE_MISS


def configured_credentials() -> tuple[str, ...]:
    return tuple(
        value
        for name in (
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
        )
        if (value := os.environ.get(name))
    )


def ensure_no_credentials(secrets: tuple[str, ...], *texts: str) -> None:
    if any(secret in text for secret in secrets for text in texts):
        raise ValueError("data contains a provider credential")


class Price(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    input_per_million: float = Field(ge=0)
    output_per_million: float = Field(ge=0)

    def estimate(self, reply: Reply) -> float:
        return (
            reply.input_tokens * self.input_per_million
            + reply.output_tokens * self.output_per_million
        ) / 1_000_000


class ProviderLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    concurrent: int = Field(default=1, ge=1)
    requests_per_minute: float = Field(gt=0)


class RunLimits(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    token_cap: int = Field(ge=0)
    transient_retries: int = Field(default=2, ge=0, le=20)
    initial_backoff_seconds: float = Field(default=0.5, ge=0)


@dataclass(frozen=True)
class Route:
    family: str
    provider: Provider


class _Throttle:
    def __init__(self, limits: ProviderLimits) -> None:
        self.slots = asyncio.Semaphore(limits.concurrent)
        self.interval = 60 / limits.requests_per_minute
        self._next = 0.0
        self._clock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._clock:
            delay = self._next - monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            self._next = monotonic() + self.interval


class ModelClient:
    """One instance per run. Replay instances cannot hold provider routes."""

    def __init__(
        self,
        *,
        run_id: str,
        mode: Mode,
        cache: ResponseCache,
        run_dir: Path,
        limits: RunLimits,
        prices: dict[str, Price],
        routes: dict[str, Route] | None = None,
        provider_limits: dict[str, ProviderLimits] | None = None,
        initial_tokens: int = 0,
        spend_guard: SpendGuard | None = None,
    ) -> None:
        if mode is Mode.REPLAY and routes:
            raise ValueError("replay does not accept provider routes")
        if initial_tokens < 0:
            raise ValueError("initial token usage must be nonnegative")
        self.run_id = run_id
        self.mode = mode
        self.cache = cache
        self.limits = limits
        self.prices = prices.copy()
        self._spend_guard = spend_guard
        self._routes = (routes or {}).copy()
        self._throttles = {
            name: _Throttle(config) for name, config in (provider_limits or {}).items()
        }
        if any(route.family not in self._throttles for route in self._routes.values()):
            raise ValueError("every provider route requires a configured limit")
        self.tokens = initial_tokens
        self._restored: dict[tuple[str, str], int] = {}
        self.run_dir = run_dir.resolve()
        self._log_path = self.run_dir / "calls.jsonl.gz"
        run_dir.mkdir(parents=True, exist_ok=True)

    def route_family(self, model: str) -> str | None:
        route = self._routes.get(model)
        return None if route is None else route.family

    def restore_recorded_usage(self) -> None:
        """Restore a resumed run's cached calls once before new work."""
        if self.tokens or self._restored:
            raise ValueError("restoring usage requires a fresh model client")
        self._restored = self.cache.call_usage(self.run_id)
        self.tokens = sum(self._restored.values())

    async def generate(
        self, request: Request[Output], *, recover: bool = False
    ) -> Response[Output]:
        if request.run_id != self.run_id:
            raise ValueError("request belongs to a different run")
        if request.replicate < 0:
            raise ValueError("replicate must be nonnegative")

        parameters = ProviderRequest(
            model=request.model,
            effort=request.effort,
            messages=request.messages,
            schema_name=request.schema.__name__,
            output_schema=request.schema.model_json_schema(),
            sampling=request.sampling,
        )
        secrets = configured_credentials()
        ensure_no_credentials(
            secrets, parameters.model_dump_json(), request.run_id, request.call_id
        )

        key = cache_key(parameters, request.replicate)
        price = self.prices[request.model]
        source: Literal["client", "cache", "provider"] = "client"
        cached = None
        if self.mode is not Mode.LIVE or recover:
            cached = self.cache.read_call(request.run_id, request.call_id, key)
            if (
                self.mode is Mode.RECORD_MISSING
                and not recover
                and cached is not None
                and cached.outcome in RETRYABLE_OUTCOMES
            ):
                cached = None
            if (
                cached is None
                and self.mode is Mode.RECORD_MISSING
                and self.tokens < self.limits.token_cap
            ):
                cached = self.cache.read(key)
                if cached is not None and cached.outcome in RETRYABLE_OUTCOMES:
                    cached = None
        if cached is not None:
            reply = cached
            source = "cache"
            if (request.call_id, key) not in self._restored:
                self.tokens += reply.input_tokens + reply.output_tokens
        elif self.mode is Mode.REPLAY:
            reply = RecordedReply(outcome=Outcome.CACHE_MISS, latency_seconds=0)
            self._log(request, key, price, reply, "cache", attempt=None)
            raise CacheMiss(f"no recorded response for {key}")
        elif self.tokens >= self.limits.token_cap:
            reply = RecordedReply(outcome=Outcome.BUDGET_EXHAUSTED, latency_seconds=0)
        else:
            reply = await self._live(request, parameters, key, price, secrets)
            source = "provider"

        if self.mode is not Mode.REPLAY:
            self.cache.write(request.run_id, request.call_id, key, reply)

        parsed = (
            request.schema.model_validate_json(reply.raw_text)
            if reply.outcome is Outcome.OK
            else None
        )
        if not (
            recover and cached is not None and self._has_summary(request.call_id, key)
        ):
            self._log(request, key, price, reply, source, attempt=None)
        return Response(reply, parsed, price.estimate(reply))

    def _has_summary(self, call_id: str, key: str) -> bool:
        if not self._log_path.exists():
            return False
        with gzip.open(self._log_path, "rt", encoding="utf-8") as log:
            return any(
                row["call_id"] == call_id
                and row["cache_key"] == key
                and row["attempt"] is None
                for row in map(json.loads, log)
            )

    async def _live(
        self,
        request: Request[Output],
        parameters: ProviderRequest,
        key: str,
        price: Price,
        secrets: tuple[str, ...],
    ) -> RecordedReply:
        route = self._routes[request.model]
        throttle = self._throttles[route.family]
        start = monotonic()
        input_tokens = output_tokens = transient = attempt = 0
        repaired = retried_refusal = False
        while True:
            async with throttle.slots:
                await throttle.wait()
                if self.tokens >= self.limits.token_cap:
                    reply = Reply(outcome=Outcome.BUDGET_EXHAUSTED)
                    break
                reservation = None
                if self._spend_guard is not None:
                    wire_payload = (
                        route.provider.request_payload(parameters)
                        if isinstance(route.provider, WireProvider)
                        else parameters.model_dump(mode="json")
                    )
                    reservation = self._spend_guard.reserve(
                        request.run_id,
                        request.call_id,
                        request.model,
                        input_bound=payload_bytes(wire_payload),
                        output_bound=output_limit(parameters),
                    )
                    if reservation is None:
                        reply = Reply(outcome=Outcome.BUDGET_EXHAUSTED)
                        break
                attempt_start = monotonic()
                try:
                    reply = await route.provider.generate(parameters)
                except ProviderRequestError:
                    if self._spend_guard is not None and reservation is not None:
                        self._spend_guard.settle(
                            reservation,
                            request.model,
                            Reply(outcome=Outcome.PARSE_FAILED),
                        )
                    self._log(
                        request,
                        key,
                        price,
                        RecordedReply(
                            outcome=Outcome.PARSE_FAILED,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            latency_seconds=monotonic() - start,
                        ),
                        "provider",
                        attempt=None,
                    )
                    raise
                self.tokens += reply.input_tokens + reply.output_tokens
                if self._spend_guard is not None and reservation is not None:
                    self._spend_guard.settle(reservation, request.model, reply)

            input_tokens += reply.input_tokens
            output_tokens += reply.output_tokens
            attempt += 1
            reply = _validate_reply(request.schema, reply, secrets)
            self._log(
                request,
                key,
                price,
                RecordedReply(
                    **reply.model_dump(), latency_seconds=monotonic() - attempt_start
                ),
                "provider",
                attempt=attempt,
            )
            if reply.outcome in (Outcome.RATE_LIMITED, Outcome.SERVER_ERROR):
                if transient >= self.limits.transient_retries:
                    break
                delay = self.limits.initial_backoff_seconds * (2**transient)
                transient += 1
                await asyncio.sleep(random.uniform(delay / 2, delay))
            elif reply.outcome is Outcome.PARSE_FAILED and not repaired:
                repaired = True
                parameters = parameters.model_copy(
                    update={
                        "messages": (
                            *parameters.messages,
                            Message(
                                role="user",
                                content=SCHEMA_REPAIR_INSTRUCTION,
                            ),
                        )
                    }
                )
            elif reply.outcome is Outcome.REFUSED and not retried_refusal:
                retried_refusal = True
            else:
                break

        return RecordedReply(
            outcome=reply.outcome,
            raw_text=reply.raw_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_reported=reply.usage_reported,
            latency_seconds=monotonic() - start,
        )

    def _log(
        self,
        request: Request[Output],
        key: str,
        price: Price,
        reply: RecordedReply,
        source: str,
        *,
        attempt: int | None,
    ) -> None:
        line = {
            "run_id": request.run_id,
            "call_id": request.call_id,
            "cache_key": key,
            "source": source,
            "attempt": attempt,
            "outcome": reply.outcome,
            "input_tokens": reply.input_tokens,
            "output_tokens": reply.output_tokens,
            "latency_seconds": reply.latency_seconds,
            "estimated_cost_usd": price.estimate(reply),
        }
        member = gzip.compress(
            (json.dumps(line, separators=(",", ":")) + "\n").encode()
        )
        previous = self._log_path.read_bytes() if self._log_path.exists() else b""
        temporary = self._log_path.with_suffix(".tmp")
        temporary.write_bytes(previous + member)
        temporary.replace(self._log_path)


def _validate_reply(
    schema: type[BaseModel], reply: Reply, secrets: tuple[str, ...]
) -> Reply:
    if any(secret in reply.raw_text for secret in secrets):
        outcome = Outcome.PARSE_FAILED if reply.outcome is Outcome.OK else reply.outcome
        return reply.model_copy(update={"outcome": outcome, "raw_text": ""})
    if reply.outcome is Outcome.OK:
        try:
            schema.model_validate_json(reply.raw_text)
        except ValidationError:
            return reply.model_copy(update={"outcome": Outcome.PARSE_FAILED})
    return reply
