"""Opt-in nightly provider probes and a recorded live toy run."""

import argparse
import asyncio
import os
import subprocess
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from soc.baseline.memory import RAGMemory
from soc.baseline.retrieval import BGEM3Embedder, Embedder
from soc.enforce.checker import Checker
from soc.orchestrator.journal import run_lock
from soc.orchestrator.prompts import digest, json_text
from soc.orchestrator.runner import ExperimentSettings, RunSpec, run_episode
from soc.providers.anthropic import AnthropicProvider
from soc.providers.cache import ResponseCache
from soc.providers.client import (
    ModelClient,
    Price,
    ProviderLimits,
    Route,
    RunLimits,
    configured_credentials,
    ensure_no_credentials,
)
from soc.providers.gemini import GeminiProvider
from soc.providers.models import (
    Message,
    Mode,
    Outcome,
    Output,
    Provider,
    ProviderRequestError,
    Request,
)
from soc.providers.openai import OpenAIProvider
from soc.providers.spend import SpendGuard, SpendRate

type Family = Literal["anthropic", "openai", "google"]
type Effort = Literal["low", "high"]
type ProbeStatus = Literal["passed", "failed", "missing_credentials", "not_run"]

_KEY_NAMES = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
}
_ADAPTERS: dict[str, Callable[[httpx.AsyncClient], Provider]] = {
    "anthropic": AnthropicProvider,
    "openai": OpenAIProvider,
    "google": GeminiProvider,
}
_PROJECTIONS = (
    "run.json",
    "final_chunks.jsonl",
    "delivered_decisions.jsonl",
    "violations.jsonl",
    "final_findings.jsonl",
    "events.jsonl",
)


class LiveModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    family: Family
    limits: SpendRate


class ToyModels(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    initial_model: str
    successor_model: str
    effort: Effort


class LiveConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    spend_cap_usd: Decimal = Field(gt=0, le=100_000)
    token_cap_per_run: int = Field(gt=0)
    probe_max_output_tokens: int = Field(gt=0)
    models: dict[str, LiveModel] = Field(min_length=1)
    provider_limits: dict[Family, ProviderLimits]
    toy: ToyModels
    experiment: ExperimentSettings

    @model_validator(mode="after")
    def validate_campaign(self) -> "LiveConfig":
        if {self.toy.initial_model, self.toy.successor_model} - self.models.keys():
            raise ValueError("toy models must be in the configured roster")
        for model in self.models.values():
            if model.family not in self.provider_limits:
                raise ValueError(f"missing request limits for {model.family}")
            if (
                max(self.probe_max_output_tokens, self.experiment.max_output_tokens)
                > model.limits.max_output_tokens
            ):
                raise ValueError(
                    "requested output limit exceeds a configured model bound"
                )
        return self

    def fingerprint(self) -> str:
        return digest(self.model_dump_json())

    def probe_fingerprint(self) -> str:
        return digest(
            json_text(
                {
                    "models": {
                        key: model.model_dump(mode="json")
                        for key, model in self.models.items()
                    },
                    "probe_max_output_tokens": self.probe_max_output_tokens,
                }
            )
        )


class ProbeAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    value: str

    @field_validator("value")
    @classmethod
    def expected_value(cls, value: str) -> str:
        if value != "SOC_READY":
            raise ValueError("the probe expects SOC_READY")
        return value


class ImpossibleAnswer(ProbeAnswer):
    @field_validator("value")
    @classmethod
    def contradictory_requirement(cls, value: str) -> str:
        # The wire schema allows SOC_READY and the local contract requires a
        # contradictory value. A provider accepting the wire schema is testable.
        raise ValueError("the probe's local requirement cannot be satisfied")


class ProbeCall(BaseModel):
    outcome: Outcome
    input_tokens: int
    output_tokens: int
    latency_seconds: float
    usage_reported: bool


class ProbeRow(BaseModel):
    model: str
    effort: Effort
    status: ProbeStatus
    positive: ProbeCall | None = None
    impossible: ProbeCall | None = None
    detail: str | None = None


class ProbeReport(BaseModel):
    configuration: str
    results: list[ProbeRow]
    spend: dict[str, bool | float | int]


def available_families() -> frozenset[str]:
    return frozenset(
        family
        for family, keys in _KEY_NAMES.items()
        if any(os.environ.get(key) for key in keys)
    )


def _guard(config: LiveConfig, root: Path) -> SpendGuard:
    return SpendGuard(
        root / "spend.sqlite",
        config.spend_cap_usd,
        {model: entry.limits for model, entry in config.models.items()},
    )


def _client(
    config: LiveConfig,
    root: Path,
    run_dir: Path,
    run_id: str,
    *,
    routes: Mapping[str, Route] | None,
    guard: SpendGuard | None = None,
) -> ModelClient:
    return ModelClient(
        run_id=run_id,
        mode=Mode.LIVE if routes is not None else Mode.REPLAY,
        cache=ResponseCache(root / "cache.sqlite"),
        run_dir=run_dir,
        limits=RunLimits(token_cap=config.token_cap_per_run),
        prices={
            key: Price(
                input_per_million=float(entry.limits.input_per_million),
                output_per_million=float(entry.limits.output_per_million),
            )
            for key, entry in config.models.items()
        },
        routes=dict(routes) if routes is not None else None,
        provider_limits={
            str(family): limits for family, limits in config.provider_limits.items()
        },
        spend_guard=guard,
    )


def _write_report(path: Path, text: str) -> None:
    ensure_no_credentials(configured_credentials(), text)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text + "\n", encoding="utf-8")
    temporary.replace(path)


async def _call_probe(
    client: ModelClient,
    model: str,
    effort: Effort,
    schema: type[Output],
    purpose: str,
    instruction: str,
    max_output_tokens: int,
) -> ProbeCall:
    result = await client.generate(
        Request(
            model=model,
            effort=effort,
            messages=(Message(role="user", content=instruction),),
            schema=schema,
            replicate=0,
            run_id=client.run_id,
            call_id=f"{model}-{effort}-{purpose}",
            sampling={"max_output_tokens": max_output_tokens},
        ),
        recover=True,
    )
    reply = result.reply
    return ProbeCall(
        outcome=reply.outcome,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
        latency_seconds=reply.latency_seconds,
        usage_reported=reply.usage_reported,
    )


async def probe_roster(
    config: LiveConfig, root: Path, routes: Mapping[str, Route]
) -> ProbeReport:
    ensure_no_credentials(configured_credentials(), config.model_dump_json())
    with run_lock(root / "probes"):
        return await _probe_roster(config, root, routes)


async def _probe_roster(
    config: LiveConfig, root: Path, routes: Mapping[str, Route]
) -> ProbeReport:
    guard = _guard(config, root)
    run_id = f"m1-probe-{config.probe_fingerprint()[:16]}"
    client = _client(config, root, root / "probes", run_id, routes=routes, guard=guard)
    results: list[ProbeRow] = []
    efforts: tuple[Effort, ...] = ("low", "high")
    for model, entry in config.models.items():
        prior_failure = False
        for effort in efforts:
            if model not in routes:
                missing_detail = (
                    f"set {' or '.join(_KEY_NAMES[entry.family])} in the environment"
                )
                results.append(
                    ProbeRow(
                        model=model,
                        effort=effort,
                        status="missing_credentials",
                        detail=missing_detail,
                    )
                )
                continue
            if prior_failure:
                results.append(
                    ProbeRow(
                        model=model,
                        effort=effort,
                        status="not_run",
                        detail="earlier probe failed",
                    )
                )
                continue
            positive = impossible = None
            detail: str | None = None
            try:
                positive = await _call_probe(
                    client,
                    model,
                    effort,
                    ProbeAnswer,
                    "valid",
                    "Return SOC_READY in the value field.",
                    config.probe_max_output_tokens,
                )
                if (
                    positive.outcome is Outcome.OK
                    and positive.usage_reported
                    and min(positive.input_tokens, positive.output_tokens) > 0
                ):
                    impossible = await _call_probe(
                        client,
                        model,
                        effort,
                        ImpossibleAnswer,
                        "impossible",
                        "Return SOC_READY and satisfy the contradictory requirement that value is NEVER_READY.",
                        config.probe_max_output_tokens,
                    )
                    if impossible.outcome not in {
                        Outcome.REFUSED,
                        Outcome.PARSE_FAILED,
                    }:
                        detail = f"impossible request ended {impossible.outcome}"
                else:
                    detail = f"valid request ended {positive.outcome} or lacked usable token counts"
            except ProviderRequestError as exc:
                detail = str(exc)
            prior_failure = detail is not None
            results.append(
                ProbeRow(
                    model=model,
                    effort=effort,
                    status="failed" if prior_failure else "passed",
                    positive=positive,
                    impossible=impossible,
                    detail=detail,
                )
            )
    report = ProbeReport(
        configuration=config.probe_fingerprint(), results=results, spend=guard.summary()
    )
    _write_report(root / "probe-report.json", report.model_dump_json(indent=2))
    return report


def _assert_probed(config: LiveConfig, root: Path, routes: Mapping[str, Route]) -> None:
    selected = {config.toy.initial_model, config.toy.successor_model}
    if selected - routes.keys():
        raise ValueError("live toy requires credentials for both selected models")
    path = root / "probe-report.json"
    if not path.is_file():
        raise ValueError("run the roster probe before the live toy")
    report = ProbeReport.model_validate_json(path.read_bytes())
    if report.configuration != config.probe_fingerprint():
        raise ValueError("roster probe used a different campaign configuration")
    passed = {
        row.model
        for row in report.results
        if row.effort == config.toy.effort and row.status == "passed"
    }
    failed = {row.model for row in report.results if row.status == "failed"}
    if selected - passed or selected & failed:
        raise ValueError("a selected toy model has not passed its effort probe")


async def run_live_toy(
    config: LiveConfig,
    root: Path,
    episode: Path,
    code_commit: str,
    embedder: Embedder,
    *,
    routes: Mapping[str, Route] | None,
) -> bool:
    spec = RunSpec(
        run_id=f"m1-live-toy-{config.fingerprint()[:16]}",
        condition="rag",
        episode_id=episode.name,
        initial_model=config.toy.initial_model,
        successor_model=config.toy.successor_model,
        effort=config.toy.effort,
        code_commit=code_commit,
    )
    recorded = root / "toy"
    guard = None
    if routes is not None:
        _assert_probed(config, root, routes)
        guard = _guard(config, root)
        checker = Checker()
        result = await run_episode(
            episode,
            recorded,
            spec=spec,
            experiment=config.experiment,
            memory=RAGMemory(embedder, checker),
            checker=checker,
            client=_client(
                config, root, recorded, spec.run_id, routes=routes, guard=guard
            ),
        )
        _write_report(root / "toy-spend.json", json_text(guard.summary()))
        if result.status != "completed":
            return False
    replay = root / "toy-replay"
    checker = Checker()
    await run_episode(
        episode,
        replay,
        spec=spec,
        experiment=config.experiment,
        memory=RAGMemory(embedder, checker),
        checker=checker,
        client=_client(config, root, replay, spec.run_id, routes=None),
    )
    for name in _PROJECTIONS:
        if (recorded / name).read_bytes() != (replay / name).read_bytes():
            raise RuntimeError(f"live toy replay differed in {name}")
    return True


async def _main(args: argparse.Namespace) -> int:
    config = LiveConfig.model_validate(
        yaml.safe_load(args.config.read_text(encoding="utf-8"))
    )
    if args.command == "replay":
        okay = await run_live_toy(
            config,
            args.output,
            args.episode,
            args.code_commit,
            BGEM3Embedder(),
            routes=None,
        )
        return int(not okay)
    available = available_families()
    async with httpx.AsyncClient(timeout=60) as http:
        adapters = {
            family: adapter(http)
            for family, adapter in _ADAPTERS.items()
            if family in available
        }
        routes = {
            model: Route(entry.family, adapters[entry.family])
            for model, entry in config.models.items()
            if entry.family in adapters
        }
        if args.command == "probe":
            report = await probe_roster(config, args.output, routes)
            passed = sum(result.status == "passed" for result in report.results)
            print(f"{passed}/{len(report.results)} effort probes passed")
            return int(passed != len(report.results))
        _assert_probed(config, args.output, routes)
        okay = await run_live_toy(
            config,
            args.output,
            args.episode,
            args.code_commit,
            BGEM3Embedder(),
            routes=routes,
        )
        print("Live toy replay matched" if okay else "Live toy incomplete")
        return int(not okay)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("probe", "toy", "replay"))
    parser.add_argument("--config", type=Path, default=Path("config/m1-live.yaml"))
    parser.add_argument(
        "--output", type=Path, required=True, help="Shared M1 campaign directory."
    )
    parser.add_argument("--episode", type=Path, default=Path("data/episodes/toy"))
    parser.add_argument("--code-commit")
    args = parser.parse_args()
    if args.code_commit is None and args.command != "probe":
        args.code_commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], text=True
        ).strip()
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
