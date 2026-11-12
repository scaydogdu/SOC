"""Run an offline episode from scripted responses or a recorded cache."""

import argparse
import asyncio
import gzip
import json
import subprocess
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from soc.baseline.memory import RAGMemory
from soc.baseline.retrieval import HashEmbedder
from soc.enforce.checker import Checker
from soc.orchestrator.runner import ExperimentSettings, RunSpec, run_episode
from soc.providers.cache import ResponseCache
from soc.providers.client import ModelClient, Price, ProviderLimits, Route, RunLimits
from soc.providers.fake import ScriptedProvider
from soc.providers.models import Mode


class _OfflineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run: RunSpec
    experiment: ExperimentSettings = ExperimentSettings()
    limits: RunLimits = RunLimits(token_cap=100_000)


def _recorded_attempts(directory: Path) -> int:
    path = directory / "calls.jsonl.gz"
    if not path.exists():
        return 0
    with gzip.open(path, "rt", encoding="utf-8") as log:
        return sum(
            row["source"] == "provider" and row["attempt"] is not None
            for row in map(json.loads, log)
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument(
        "--script", type=Path, help="YAML responses. Omit for cache-only replay."
    )
    args = parser.parse_args()
    code_commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[2], text=True
    ).strip()
    raw = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    raw["run"]["code_commit"] = code_commit
    config = _OfflineConfig.model_validate(raw)
    models = {config.run.initial_model, config.run.successor_model}
    routes = {}
    mode = Mode.REPLAY
    if args.script is not None:
        fake = ScriptedProvider(
            args.script, consumed_attempts=_recorded_attempts(args.run_dir)
        )
        routes = {model: Route("fake", fake) for model in models}
        mode = Mode.LIVE
    client = ModelClient(
        run_id=config.run.run_id,
        mode=mode,
        cache=ResponseCache(args.cache),
        run_dir=args.run_dir,
        limits=config.limits,
        prices={
            model: Price(input_per_million=0, output_per_million=0) for model in models
        },
        routes=routes,
        provider_limits={"fake": ProviderLimits(requests_per_minute=60_000)},
    )
    checker = Checker()
    result = asyncio.run(
        run_episode(
            args.episode,
            args.run_dir,
            spec=config.run,
            experiment=config.experiment,
            memory=RAGMemory(HashEmbedder(), checker),
            checker=checker,
            client=client,
        )
    )
    print(f"{result.status}: {result.chunks_completed} chunks")
    return 0 if result.status == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
