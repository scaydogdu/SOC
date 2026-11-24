"""Aggregate observed must-check counts by condition and episode, without averaging rates."""

import argparse
import json
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

COUNTS = (
    "must_checks",
    "unknown_must",
    "configuration_unknowns",
    "runtime_unknowns",
    "resolved",
    "compliant",
    "violation",
    "unverified",
    "unverified_final",
)


def aggregate(paths: Sequence[Path], *, limit: float = 0.01) -> list[dict[str, object]]:
    if not 0 <= limit <= 1:
        raise ValueError("unknown-rate limit must lie between zero and one")
    groups: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: dict.fromkeys(COUNTS, 0)
    )
    seen = set()
    for path in paths:
        data = json.loads(path.read_text())
        identity = (data["condition"], data["episode_id"], data["run_id"])
        if identity in seen:
            raise ValueError("duplicate run in unknown-rate aggregation")
        seen.add(identity)
        values = {name: data[name] for name in COUNTS}
        if any(type(value) is not int or value < 0 for value in values.values()):
            raise ValueError("unknown reports require nonnegative integer counts")
        if (
            values["unknown_must"]
            != values["configuration_unknowns"] + values["runtime_unknowns"]
            or values["resolved"] != values["compliant"] + values["violation"]
            or values["resolved"] + values["unverified"] != values["unknown_must"]
            or values["unknown_must"] > values["must_checks"]
        ):
            raise ValueError("inconsistent unknown report counts")
        group = groups[identity[:2]]
        for name in COUNTS:
            group[name] += values[name]
    result = []
    for (condition, episode), counts in sorted(groups.items()):
        denominator = counts["must_checks"]
        result.append(
            {
                "condition": condition,
                "episode_id": episode,
                **counts,
                "unknown_rate": counts["unknown_must"] / denominator
                if denominator
                else None,
                "runtime_unknown_rate": counts["runtime_unknowns"] / denominator
                if denominator
                else None,
                "runtime_rate_limit": limit,
                "pilot_b_unknowns_pass": bool(
                    denominator
                    and counts["configuration_unknowns"] == 0
                    and counts["runtime_unknowns"] / denominator <= limit
                ),
            }
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--limit", type=float, default=0.01)
    args = parser.parse_args()
    print(json.dumps(aggregate(args.reports, limit=args.limit), indent=2))


if __name__ == "__main__":
    main()
