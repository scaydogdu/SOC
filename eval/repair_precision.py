"""Compare the two arms' brief-change repair selections against the frozen episode."""

import argparse
import json
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from pathlib import Path

from pydantic import BaseModel

from eval.episode import FullEpisode, validate
from soc.ledger.models import StaleMarked, VersionRef
from soc.orchestrator.prompts import digest

type Targets = dict[str, set[int]]


class Counts(BaseModel, frozen=True):
    selected: int
    correct: int
    expected: int

    @property
    def precision(self) -> float | None:
        return self.correct / self.selected if self.selected else None


def _totals(rows: Iterable[Counts]) -> Counts:
    rows = tuple(rows)
    return Counts(
        selected=sum(row.selected for row in rows),
        correct=sum(row.correct for row in rows),
        expected=sum(row.expected for row in rows),
    )


def _scores(targets: Targets, expected: Targets) -> dict[str, object]:
    rows = {}
    for key, gold in sorted(expected.items()):
        selected = targets[key]
        rows[key] = Counts(
            selected=len(selected), correct=len(selected & gold), expected=len(gold)
        )
    total = _totals(rows.values())

    def report(count: Counts) -> dict[str, object]:
        return {**count.model_dump(), "precision": count.precision}

    return {
        "total": report(total),
        "decisions": {key: report(row) for key, row in rows.items()},
    }


def _rag_targets(run: Path, boundary: int, ids: set[str]) -> Targets:
    found: Targets = {}
    with (run / "events.jsonl").open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event["event"] != "revised" or event["after_chunk"] != boundary:
                continue
            key = event["decision"]["decision_id"]
            chunks = event["affected_chunks"]
            if (
                key not in ids
                or key in found
                or any(
                    type(number) is not int or not 1 <= number <= boundary
                    for number in chunks
                )
            ):
                raise ValueError("invalid RAG repair selection")
            found[key] = set(chunks)
    return found


def _soc_targets(run: Path, base: int, boundary: int, ids: set[str]) -> Targets:
    found: Targets = {}
    uri = (run / "ledger.sqlite").resolve().as_uri() + "?mode=ro"
    positions = {
        f"chunk-{number + base - 1}": number for number in range(1, boundary + 1)
    }
    with closing(sqlite3.connect(uri, uri=True)) as db:
        for key in ids:
            row = db.execute(
                "SELECT seq FROM events WHERE commitment_id = ? AND type = 'Revised' "
                "AND new_version = 2 AND json_extract(payload, '$.data.commitment.authority') = 'principal'",
                (key,),
            ).fetchone()
            if row is None:
                continue
            found[key] = set()
            # A scheduled principal revision emits its impacted builds before the
            # tombstone, any later builds, or any later decision.
            marked = db.execute(
                "SELECT payload FROM events WHERE seq = ? AND type = 'StaleMarked' "
                "AND commitment_id = ?",
                (row[0] + 1, key),
            ).fetchone()
            if marked is None:
                continue
            data = StaleMarked.model_validate(json.loads(marked[0])["data"])
            if data.replaced_by != VersionRef(commitment_id=key, version=2) or any(
                chunk_id not in positions for chunk_id in data.chunk_ids
            ):
                raise ValueError("invalid SOC repair selection")
            found[key] = {positions[chunk_id] for chunk_id in data.chunk_ids}
    return found


def paired_repair_precision(
    episode: FullEpisode, rag: Path, soc: Path
) -> dict[str, object]:
    """Use decision/chunk pairs so both mechanisms have the same denominator."""
    manifest = [
        json.loads((run / "run.json").read_text(encoding="utf-8")) for run in (rag, soc)
    ]
    episode_digest = digest(episode.runtime.model_dump_json())
    if any(run.get("episode_sha256") != episode_digest for run in manifest):
        raise ValueError("both runs must come from the supplied public episode")
    matched = (
        "episode_id",
        "replicate",
        "initial_model",
        "successor_model",
        "effort",
        "resolution",
        "max_in_flight",
        "model_protocol",
    )
    if any(manifest[0].get(key) != manifest[1].get(key) for key in matched):
        raise ValueError(
            "paired runs must match replicate, effort, models, resolution and scheduling"
        )
    if any(run.get("status") == "running" for run in manifest):
        raise ValueError("both runs must have reached the brief change")
    change = episode.runtime.events.brief_change
    ids = {revision.decision_id for revision in change.revisions}
    expected = {
        row.decision_id: {site.chunk for site in row.repair}
        for row in episode.affected
        if row.decision_id in ids
    }
    if expected.keys() != ids:
        raise ValueError(
            "the frozen episode needs repair annotations for each revision"
        )
    base = manifest[1]["chunk_id_base"]
    if type(base) is not int or base not in (0, 1):
        raise ValueError("unknown SOC chunk ID convention")
    rag_targets = _rag_targets(rag, change.chunk, ids)
    soc_targets = _soc_targets(soc, base, change.chunk, ids)
    if rag_targets.keys() != ids or soc_targets.keys() != ids:
        raise ValueError("both runs must have recorded every scheduled revision")
    return {
        "unit": "decision_chunk",
        "after_chunk": change.chunk,
        "rag": _scores(rag_targets, expected),
        "soc": _scores(soc_targets, expected),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    parser.add_argument("--rag", type=Path, required=True)
    parser.add_argument("--soc", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = paired_repair_precision(validate(args.episode), args.rag, args.soc)
    result = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(result, end="")
    else:
        args.output.write_text(result, encoding="utf-8")


if __name__ == "__main__":
    main()
