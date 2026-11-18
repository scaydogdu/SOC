"""Nightly independent checks of a sealed draft, with author keys kept private."""

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

import httpx
import yaml
from pydantic import field_validator

from eval.episode import Affected, FullEpisode, Seed, UseSite, validate
from soc.episode import Record
from soc.orchestrator.journal import run_lock
from soc.orchestrator.live import (
    LiveConfig,
    campaign_client,
    campaign_spend,
    live_routes,
    require_probes,
)
from soc.orchestrator.prompts import digest, json_text
from soc.providers.client import (
    ModelClient,
    configured_credentials,
    ensure_no_credentials,
)
from soc.providers.models import Message, Output, ProviderRequestError, Request

FLAGSHIPS = ("gpt-6-astra", "claude-opus-5-5", "gemini-4-argon")


class SeedPrediction(Record):
    answer: str
    rationale: str


class PredictedSite(Record):
    chunk: int
    sentence_id: str

    @field_validator("chunk")
    @classmethod
    def one_based(cls, value: int) -> int:
        if value < 1:
            raise ValueError("chunk positions are one-based")
        return value


class AffectedPrediction(Record):
    repair: list[PredictedSite]
    apply: list[PredictedSite]
    rationale: str


@dataclass(frozen=True)
class Comparison:
    kind: Literal["seed", "affected"]
    id: str
    status: Literal["agree", "disagree", "unavailable"]
    expected: object
    predicted: object
    detail: str


def _source(episode: FullEpisode) -> dict[str, dict[str, object]]:
    return {
        sentence.id: {
            "sentence_id": sentence.id,
            "text": sentence.reading_text,
            "scope_keys": sentence.scope_keys,
        }
        for sentence in episode.runtime.source
    }


def _seed_prompt(episode: FullEpisode, seed: Seed) -> str:
    source = _source(episode)
    data: dict[str, object] = {
        "seed_type": seed.type,
        "correctness": seed.correctness,
        "sentences": [source[key] for key in seed.sentence_ids],
        "brief": episode.runtime.brief.model_dump(mode="json"),
    }
    if seed.correctness == "client_preference":
        data["client_preferences"] = [
            pref.model_dump(mode="json") for pref in episode.hidden.preferences
        ]
    if seed.correctness == "commitment_continuity":
        data["schedule"] = episode.runtime.events.model_dump(mode="json")
    return (
        "Give a concise independent answer for this episode seed type. "
        "Use the source and the supplied requirements. Give a short rationale.\n"
        + json_text(data)
    )


def _affected_prompt(episode: FullEpisode, entry: Affected) -> str:
    events = episode.runtime.events
    approval = next(
        item for item in events.approvals if item.decision_id == entry.decision_id
    )
    revision = next(
        item
        for item in events.brief_change.revisions
        if item.decision_id == entry.decision_id
    )
    source = _source(episode)
    return (
        "Find sentence use sites affected by the approved decision's revision. "
        "Repair sites use the old decision after its approval through the change chunk. "
        "Apply sites use the new decision after the change chunk. "
        "Return one-based chunk positions and exact sentence IDs, with a short rationale.\n"
        + json_text(
            {
                "approval": approval.model_dump(mode="json"),
                "revision": revision.model_dump(mode="json"),
                "change_after_chunk": events.brief_change.chunk,
                "chunks": [
                    {
                        "chunk": position,
                        "sentences": [source[key] for key in chunk.sentence_ids],
                    }
                    for position, chunk in enumerate(events.chunks, 1)
                ],
            }
        )
    )


async def _predict(
    client: ModelClient,
    model: str,
    call_id: str,
    prompt: str,
    schema: type[Output],
    max_output_tokens: int,
) -> tuple[Output | None, str]:
    try:
        response = await client.generate(
            Request(
                model=model,
                effort="high",
                messages=(Message(role="user", content=prompt),),
                schema=schema,
                replicate=0,
                run_id=client.run_id,
                call_id=call_id,
                sampling={"max_output_tokens": max_output_tokens},
            )
        )
    except ProviderRequestError as error:
        return None, str(error)
    return response.parsed, response.reply.outcome.value


def _sites(sites: Sequence[UseSite | PredictedSite]) -> Counter[tuple[int, str]]:
    return Counter((site.chunk, site.sentence_id) for site in sites)


async def check_episode(
    episode: FullEpisode, client: ModelClient, model: str, *, max_output_tokens: int
) -> tuple[Comparison, ...]:
    results = []
    for index, seed in enumerate(episode.seeds, 1):
        prediction, outcome = await _predict(
            client,
            model,
            f"seed-{index}",
            _seed_prompt(episode, seed),
            SeedPrediction,
            max_output_tokens,
        )
        if prediction is None:
            results.append(
                Comparison(
                    "seed", seed.id, "unavailable", seed.expected_answer, None, outcome
                )
            )
            continue
        matches = (
            prediction.answer.casefold().split()
            == seed.expected_answer.casefold().split()
        )
        results.append(
            Comparison(
                "seed",
                seed.id,
                "agree" if matches else "disagree",
                seed.expected_answer,
                prediction.answer,
                prediction.rationale,
            )
        )

    for index, entry in enumerate(episode.affected, 1):
        prediction_set, outcome = await _predict(
            client,
            model,
            f"affected-{index}",
            _affected_prompt(episode, entry),
            AffectedPrediction,
            max_output_tokens,
        )
        expected = entry.model_dump(mode="json", exclude={"decision_id"})
        if prediction_set is None:
            results.append(
                Comparison(
                    "affected",
                    entry.decision_id,
                    "unavailable",
                    expected,
                    None,
                    outcome,
                )
            )
            continue
        matches = _sites(prediction_set.repair) == _sites(entry.repair) and _sites(
            prediction_set.apply
        ) == _sites(entry.apply)
        results.append(
            Comparison(
                "affected",
                entry.decision_id,
                "agree" if matches else "disagree",
                expected,
                prediction_set.model_dump(mode="json", exclude={"rationale"}),
                prediction_set.rationale,
            )
        )
    return tuple(results)


async def _main(args: argparse.Namespace) -> int:
    config = LiveConfig.model_validate(
        yaml.safe_load(args.config.read_text(encoding="utf-8"))
    )
    episode = validate(args.episode)
    run_id = digest(
        json_text(
            [args.model, config.probe_fingerprint(), episode.manifest.model_dump()]
        )
    )[:24]
    private = args.campaign / "precheck" / run_id
    if private.resolve().is_relative_to(args.episode.resolve()):
        raise ValueError("precheck output must be outside the episode package")
    async with httpx.AsyncClient(timeout=60) as http:
        routes = live_routes(config, http)
        require_probes(config, args.campaign, routes, {args.model}, "high")
        with run_lock(private):
            guard = campaign_spend(config, args.campaign)
            client = campaign_client(
                config,
                private,
                private,
                run_id,
                routes={args.model: routes[args.model]},
                guard=guard,
            )
            results = await check_episode(
                episode,
                client,
                args.model,
                max_output_tokens=config.probe_max_output_tokens,
            )
            report = {
                "episode": args.episode.name,
                "model": args.model,
                "results": [asdict(item) for item in results],
                "campaign_spend": guard.summary(),
            }
            payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            ensure_no_credentials(configured_credentials(), payload)
            temporary = private / ".report.tmp"
            temporary.write_text(payload, encoding="utf-8")
            temporary.replace(private / "report.json")
    disagreements = sum(item.status == "disagree" for item in results)
    unavailable = sum(item.status == "unavailable" for item in results)
    print(
        f"{disagreements} disagreements, {unavailable} unavailable. Report: {private / 'report.json'}"
    )
    return int(bool(disagreements or unavailable))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=Path("config/m1-live.yaml"))
    parser.add_argument("--model", choices=FLAGSHIPS, default=FLAGSHIPS[0])
    return asyncio.run(_main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
