"""Enforce the branch floor separately for each core area."""

import json
import sys

groups = {
    "planted_errors": ("eval/planted.py",),
    "held_reporting": ("eval/continuity.py", "analysis/unknowns.py"),
    "cooperation": ("soc/cooperation/",),
    "timeline": ("analysis/timeline.py",),
    "ledger": ("soc/ledger/",),
    "episode": ("soc/episode.py", "eval/episode.py"),
    "segmenter": ("soc/tasks/",),
    "providers": ("soc/providers/",),
    "checker": ("soc/enforce/",),
    "baseline": ("soc/baseline/",),
}
files = json.load(sys.stdin)["files"]
failed = False
for name, paths in groups.items():
    selected = [
        record["summary"] for path, record in files.items() if path.startswith(paths)
    ]
    covered = sum(item["covered_branches"] for item in selected)
    total = sum(item["num_branches"] for item in selected)
    if total:
        rate = covered / total
        print(f"{name}: {covered}/{total} branches ({rate:.1%})")
        failed |= rate < 0.90
    else:
        print(f"{name}: no executable branches")
raise SystemExit(int(failed))
