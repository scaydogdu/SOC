"""Exercise proposed SOC conditions offline and price their scripted request paths.

Reported usage is a planning assumption of one token per four UTF-8 bytes,
rounded up for each request and response. Reservation amounts use full request
bytes and the configured output limit. The retry envelope allows the client's
transient retries plus one schema repair and one refusal retry on every call.
It bounds this scripted call path. Live models can choose different paths.
"""

import argparse
import asyncio
import json
from collections import Counter
from collections.abc import Callable
from decimal import Decimal
from math import ceil
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from soc.enforce.checker import Checker
from soc.episode import read_yaml
from soc.ledger.models import Actor
from soc.orchestrator.live import LiveConfig, SOCCondition, campaign_client
from soc.orchestrator.model_triad import run_model_ledger_episode
from soc.principal.simulator import PrincipalSimulator
from soc.providers.anthropic import AnthropicProvider
from soc.providers.client import ProviderLimits, Route, RunLimits
from soc.providers.gemini import GeminiProvider
from soc.providers.http import JsonObject, payload_bytes
from soc.providers.models import Outcome, ProviderRequest, Reply
from soc.providers.openai import OpenAIProvider
from soc.providers.spend import _NANOS, SpendGuard
from soc.triad.agreement import AgreementRules

BUILDERS: dict[str, Callable[[ProviderRequest], JsonObject]] = {
    "anthropic": AnthropicProvider.request_payload,
    "openai": OpenAIProvider.request_payload,
    "google": GeminiProvider.request_payload,
}


class PlanningProvider:
    """Script one terminology divergence, then render applicable scheduled decisions."""

    def __init__(
        self, config: LiveConfig, condition: SOCCondition, family: str
    ) -> None:
        self.config = config
        self.condition = condition
        self.family = family
        self.records: list[dict[str, int | str]] = []

    def request_payload(self, request: ProviderRequest) -> JsonObject:
        return BUILDERS[self.family](request)

    async def generate(self, request: ProviderRequest) -> Reply:
        context = json.loads(request.messages[1].content)
        schema = request.schema_name
        if schema == "MeaningReply":
            response = {
                "verdict": "compliant",
                "rationale": "Scripted planning outcome, not a measured semantic judgment",
                "evidence": [
                    {
                        "reference": f"output:{context['source']['id']}",
                        "quote": context["translation"],
                    }
                ],
            }
        elif schema == "DraftReply":
            rows = []
            manifest = []
            for sentence in context["source"]:
                sid = sentence["id"]
                terms = [
                    row
                    for row in context["commitments"]
                    if sid in (row["sentence_ids"] or [sid])
                    and row["ref"]["commitment_id"] != "D4"
                ]
                text = ". ".join(row["rendering"] for row in terms) or sentence["text"]
                if sid == "9999-1-s01":
                    term = (
                        "erişim anahtarı"
                        if request.model == self.condition.run.initial_model
                        else "erişim jetonu"
                    )
                    text = f"{term} istemcinin kaynağı kullanmasını sağlar."
                rows.append({"sentence_id": sid, "text": text})
                manifest.extend(
                    {"ref": row["ref"], "sentence_id": sid} for row in terms
                )
            response = {"sentences": rows, "manifest": manifest}
        elif schema == "ReviewReply":
            response = {
                "challenge": None
                if context["turns"]
                else {
                    "worker": "peer",
                    "question": "Which source phrase names this token?",
                }
            }
        elif schema == "ResponseReply":
            response = {
                "answer": {
                    "rationale": "The source names the access token.",
                    "citations": [
                        {"reference": "source:9999-1-s01", "quote": "access token"}
                    ],
                }
            }
        elif schema == "Classification":
            response = {
                "kind": "term",
                "question": None,
                "term": {
                    "scope_key": "access token",
                    "first": "erişim anahtarı",
                    "second": "erişim jetonu",
                },
            }
        elif schema == "DiscussionReply":
            response = {"remark": "The source calls this an access token."}
        elif schema == "Ballot":
            response = {
                "choice": "first"
                if request.model == self.condition.run.initial_model
                else "second"
            }
        else:
            raise ValueError(f"unhandled scripted schema: {schema}")
        raw = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        size = payload_bytes(self.request_payload(request))
        # These are explicit planning assumptions, never claimed as measured live usage.
        input_tokens, output_tokens = ceil(size / 4), ceil(len(raw.encode("utf-8")) / 4)
        output_limit = request.sampling["max_output_tokens"]
        if type(output_limit) is not int or output_tokens > output_limit:
            raise ValueError("scripted response exceeds configured output limit")
        rate = self.config.models[request.model].limits
        self.records.append(
            {
                "model": request.model,
                "schema": schema,
                "input_bytes": size,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "expected_nanos": rate.charge(input_tokens, output_tokens),
                "reservation_nanos": rate.charge(size, output_limit),
            }
        )
        return Reply(
            outcome=Outcome.OK,
            raw_text=raw,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            usage_reported=True,
        )


async def estimate_condition(
    config: LiveConfig,
    condition: SOCCondition,
    episode: Path,
    key: Path,
    directory: Path,
    agreement: AgreementRules,
) -> dict[str, object]:
    root, run_dir = directory / "campaign", directory / "run"
    guard = SpendGuard(
        root / "spend.sqlite",
        config.spend_cap_usd,
        {model: entry.limits for model, entry in config.models.items()},
    )
    families = {
        config.models[model].family
        for model in (
            condition.run.initial_model,
            condition.run.successor_model,
            condition.team.peer_model,
            condition.team.reviewer_model,
        )
    }
    providers = {
        family: PlanningProvider(config, condition, family) for family in families
    }
    routes = {
        model: Route(entry.family, providers[entry.family])
        for model, entry in config.models.items()
        if entry.family in providers
    }
    # Rate limits affect elapsed time, while the retry count and reservation sizes stay shared.
    offline_config = config.model_copy(
        update={
            "provider_limits": {
                family: ProviderLimits(requests_per_minute=1e12) for family in families
            }
        }
    )
    client = campaign_client(
        offline_config, root, run_dir, condition.run.run_id, routes=routes, guard=guard
    )
    principal = PrincipalSimulator(
        key,
        directory / "principal.sqlite",
        episode_id=condition.run.episode_id,
        actor=Actor(agent="principal", model="simulator", role="principal"),
    )
    result = await run_model_ledger_episode(
        episode,
        run_dir,
        spec=condition.run,
        experiment=config.experiment,
        principal=principal,
        checker=Checker(),
        client=client,
        team=condition.team,
        agreement=agreement,
    )
    if result.status not in {"completed", "unverified"}:
        raise ValueError(f"scripted condition ended {result.status}")
    records = [record for provider in providers.values() for record in provider.records]
    initial_reservation = sum(int(row["reservation_nanos"]) for row in records)
    expected = sum(int(row["expected_nanos"]) for row in records)
    peak = max(int(row["reservation_nanos"]) for row in records)
    # ModelClient allows transient retries, one schema repair and one refusal retry.
    attempts_per_call = (
        RunLimits(token_cap=config.token_cap_per_run).transient_retries + 3
    )
    # A schema retry adds a short user message. Price that increment at full UTF-8 bytes.
    repair_message = {
        "role": "user",
        "content": "Return valid JSON matching the supplied schema.",
    }
    extra_bytes = (
        len(
            json.dumps(
                repair_message, ensure_ascii=False, separators=(",", ":")
            ).encode()
        )
        + 1
    )
    retry_reservation = sum(
        config.models[str(row["model"])].limits.charge(
            int(row["input_bytes"]) + extra_bytes, config.experiment.max_output_tokens
        )
        for row in records
    )
    for path in run_dir.glob("triad-prompt-*.json"):
        if str(key) in path.read_text(encoding="utf-8"):
            raise ValueError("private key path reached a model prompt")
    return {
        "run_id": condition.run.run_id,
        "mode": condition.run.resolution,
        "effort": condition.run.effort,
        "status": result.status,
        "meaning_model": config.experiment.meaning_model,
        "unverified_must": json.loads((run_dir / "unknowns.json").read_text())[
            "unverified"
        ],
        "physical_calls": len(records),
        "schemas": dict(sorted(Counter(str(row["schema"]) for row in records).items())),
        "assumed_input_tokens": sum(int(row["input_tokens"]) for row in records),
        "assumed_output_tokens": sum(int(row["output_tokens"]) for row in records),
        "expected_usd": expected / _NANOS,
        "scripted_path_reserved_usd": initial_reservation / _NANOS,
        "retry_envelope_usd": (
            initial_reservation + (attempts_per_call - 1) * retry_reservation
        )
        / _NANOS,
        "peak_concurrent_reservation_usd": peak / _NANOS,
        "private_key_path_in_prompts": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/m1-live.yaml"))
    parser.add_argument("--conditions", type=Path, default=Path("config/m2-live"))
    parser.add_argument("--episode", type=Path, default=Path("data/episodes/toy"))
    parser.add_argument("--principal-key", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    config = LiveConfig.model_validate(
        yaml.safe_load(args.config.read_text(encoding="utf-8"))
    )
    agreement = read_yaml(Path("config/m2-agreement.yaml"), AgreementRules)
    results = []
    if not any(args.conditions.glob("*.yaml")):
        parser.error("the condition directory contains no YAML files")
    with TemporaryDirectory(prefix="soc-m2-planning-") as temporary:
        for index, path in enumerate(sorted(args.conditions.glob("*.yaml"))):
            condition = SOCCondition.model_validate(
                yaml.safe_load(path.read_text(encoding="utf-8"))
            )
            if condition.run.max_in_flight != 1 or condition.memory != "soc":
                raise ValueError("planning fixtures require sequential SOC conditions")
            result = asyncio.run(
                estimate_condition(
                    config,
                    condition,
                    args.episode,
                    args.principal_key,
                    Path(temporary) / str(index),
                    agreement,
                )
            )
            results.append({"condition_file": path.as_posix(), **result})
    report = {
        "usage_assumption": "ceil(UTF-8 request/response bytes divided by four); scripted responses",
        "reservation_formula": "request bytes times input rate plus output limit times output rate",
        "live_cost_uncertainty": "Estimates cover this scripted call path; live decisions and repairs can add calls.",
        "conditions": results,
        "total_expected_usd": str(
            sum(Decimal(str(row["expected_usd"])) for row in results)
        ),
        "total_retry_envelope_usd": str(
            sum(Decimal(str(row["retry_envelope_usd"])) for row in results)
        ),
        "peak_concurrent_reservation_usd": max(
            row["peak_concurrent_reservation_usd"] for row in results
        ),
    }
    args.out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
