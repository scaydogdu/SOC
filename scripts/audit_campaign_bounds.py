"""Reconstruct M1 requests and check recorded input usage without contacting providers."""

import argparse
import asyncio
import gzip
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from soc.baseline.memory import RAGMemory
from soc.baseline.retrieval import BGEM3Embedder
from soc.enforce.checker import Checker
from soc.orchestrator.live import (
    ImpossibleAnswer,
    LiveConfig,
    ProbeAnswer,
    campaign_client,
)
from soc.orchestrator.runner import RunSpec, run_episode
from soc.providers.anthropic import AnthropicProvider
from soc.providers.cache import cache_key
from soc.providers.client import SCHEMA_REPAIR_INSTRUCTION
from soc.providers.gemini import GeminiProvider
from soc.providers.http import payload_bytes
from soc.providers.models import Message, ProviderRequest
from soc.providers.openai import OpenAIProvider

PAYLOAD = {
    "anthropic": AnthropicProvider.request_payload,
    "google": GeminiProvider.request_payload,
    "openai": OpenAIProvider.request_payload,
}


def _readonly(root: Path, database: str) -> sqlite3.Connection:
    return sqlite3.connect((root / database).resolve().as_uri() + "?mode=ro", uri=True)


async def toy_requests(config, campaign, scratch, episode, requests):
    run_meta = json.loads((campaign / "toy" / "run.json").read_text(encoding="utf-8"))
    spec = RunSpec.model_validate({key: run_meta[key] for key in RunSpec.model_fields})
    with (
        closing(_readonly(campaign, "cache.sqlite")) as source,
        closing(sqlite3.connect(scratch / "cache.sqlite")) as copied,
    ):
        source.backup(copied)
    run_dir = scratch / "toy-audit"
    client = campaign_client(config, scratch, run_dir, spec.run_id, routes=None)
    checker = Checker()

    async def record_request(request, *, recover):
        wire = ProviderRequest(
            model=request.model,
            effort=request.effort,
            messages=request.messages,
            schema_name=request.schema.__name__,
            output_schema=request.schema.model_json_schema(),
            sampling=request.sampling,
        )
        requests[
            (request.run_id, request.call_id, cache_key(wire, request.replicate))
        ] = wire
        return await client.generate(request, recover=recover)

    result = await run_episode(
        episode,
        run_dir,
        spec=spec,
        experiment=config.experiment,
        memory=RAGMemory(BGEM3Embedder(), checker),
        checker=checker,
        client=client,
        worker=record_request,
    )
    if result.status != "completed":
        raise ValueError("the recorded toy did not replay to completion")


def audit(config, campaign, episode):
    requests = {}
    with closing(_readonly(campaign, "cache.sqlite")) as db:
        logical = db.execute(
            "SELECT run_id, call_id, key, reply FROM calls ORDER BY run_id, call_id, key"
        ).fetchall()
    for run, call, key, _ in logical:
        if not run.startswith("m1-probe-"):
            continue
        model, effort, purpose = call.rsplit("-", 2)
        if purpose == "valid":
            schema, prompt = ProbeAnswer, "Return SOC_READY in the value field."
        elif purpose == "impossible":
            schema, prompt = (
                ImpossibleAnswer,
                "Return SOC_READY and satisfy the contradictory requirement that value is NEVER_READY.",
            )
        else:
            raise ValueError(f"unknown probe purpose: {call}")
        wire = ProviderRequest(
            model=model,
            effort=effort,
            messages=(Message(role="user", content=prompt),),
            schema_name=schema.__name__,
            output_schema=schema.model_json_schema(),
            sampling={"max_output_tokens": config.probe_max_output_tokens},
        )
        if cache_key(wire, 0) != key:
            raise ValueError(f"probe request hash differs: {call}")
        requests[(run, call, key)] = wire
    with TemporaryDirectory(prefix="soc-bound-audit-") as directory:
        asyncio.run(toy_requests(config, campaign, Path(directory), episode, requests))
    observed = defaultdict(list)
    for directory in ("probes", "toy"):
        with gzip.open(
            campaign / directory / "calls.jsonl.gz", "rt", encoding="utf-8"
        ) as stream:
            for line in stream:
                event = json.loads(line)
                if event["source"] == "provider" and event["attempt"] is not None:
                    observed[
                        (event["run_id"], event["call_id"], event["cache_key"])
                    ].append(event)
    checked = missing = largest = 0
    mismatches = []
    for run, call, key, raw in logical:
        original = requests[run, call, key]
        to_payload = PAYLOAD[config.models[original.model].family]
        current = original
        logical_tokens = logical_bound = 0
        for event in observed[run, call, key]:
            if event["attempt"] == 1:
                current = original
                logical_tokens = logical_bound = 0
            bound = payload_bytes(to_payload(current))
            tokens = event["input_tokens"]
            if tokens:
                checked += 1
                logical_tokens += tokens
                logical_bound += bound
                largest = max(largest, tokens)
                if tokens > bound:
                    mismatches.append(
                        {
                            "call_id": call,
                            "attempt": event["attempt"],
                            "input_tokens": tokens,
                            "bound": bound,
                        }
                    )
            else:
                missing += 1
            if event["outcome"] == "parse_failed" and current == original:
                current = original.model_copy(
                    update={
                        "messages": (
                            *original.messages,
                            Message(role="user", content=SCHEMA_REPAIR_INSTRUCTION),
                        )
                    }
                )
        if (
            json.loads(raw)["input_tokens"] != logical_tokens
            or logical_tokens > logical_bound
        ):
            mismatches.append(
                {
                    "call_id": call,
                    "error": "cached input does not match the measured attempts or their total bound",
                }
            )
    return {
        "cached_calls": len(logical),
        "matched_request_keys": len(requests),
        "attempts_with_input_usage": checked,
        "attempts_without_input_usage": missing,
        "largest_input_tokens": largest,
        "mismatches": mismatches,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("campaign", type=Path)
    parser.add_argument("--config", type=Path, default=Path("config/m1-live.yaml"))
    parser.add_argument("--episode", type=Path, default=Path("data/episodes/toy"))
    args = parser.parse_args()
    config = LiveConfig.model_validate(
        yaml.safe_load(args.config.read_text(encoding="utf-8"))
    )
    report = audit(config, args.campaign, args.episode)
    print(json.dumps(report, indent=2))
    return int(bool(report["mismatches"]))


if __name__ == "__main__":
    raise SystemExit(main())
