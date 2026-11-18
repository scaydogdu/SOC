"""Count votes on externally annotated brief gaps from completed resolution records."""

import argparse
import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Literal

from soc.ledger.models import FlagResolved, ResolutionTrace


def read_resolutions(
    run: Path, arm: Literal["rag", "soc"]
) -> Iterator[ResolutionTrace]:
    if arm == "rag":
        with (run / "events.jsonl").open(encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row["event"] == "flag_resolved":
                    yield ResolutionTrace.model_validate(row["trace"])
        return

    uri = (run / "ledger.sqlite").resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        for (payload,) in db.execute(
            "SELECT payload FROM events WHERE type = 'FlagResolved' ORDER BY seq"
        ):
            resolved = FlagResolved.model_validate(json.loads(payload)["data"])
            if resolved.trace is not None:
                yield resolved.trace


def brief_gap_votes(
    resolutions: Iterable[ResolutionTrace], annotated_sites: set[str]
) -> dict[str, int]:
    """Count resolved flags at annotated sites that received a non-abstaining vote."""
    reviewed = voted = 0
    for resolution in resolutions:
        if resolution.sentence_id in annotated_sites:
            reviewed += 1
            voted += any(ballot.choice is not None for ballot in resolution.ballots)
    return {"resolved_brief_gaps": reviewed, "brief_gaps_voted_on": voted}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run", type=Path)
    parser.add_argument("--arm", choices=("rag", "soc"), required=True)
    parser.add_argument("--sentence-id", action="append", required=True)
    args = parser.parse_args()
    print(
        json.dumps(
            brief_gap_votes(read_resolutions(args.run, args.arm), set(args.sentence_id))
        )
    )


if __name__ == "__main__":
    main()
