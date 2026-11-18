"""Export a verified, self-contained SOC commitment timeline.

Schema v1 is an object with run metadata (run_id, episode_id, label,
head_hash, final_sequence, chunk_id_base), the public schedule boundaries
and observed changes, ordered chunks and commitments, tombstones, raw
events, unplaced-event notes, and summary counts. Each chunk contains its
public sentences and successive builds. Builds contain the recorded
ledger snapshot, uses and use sites, gate verdicts, stale marks, repair
detections, and per-commitment display statuses. Each commitment contains
its identity and its successive version records with reasons and ballots.
The event payloads omit the repeated JSON-LD context and provenance graph.
"""

import argparse
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, get_args

from soc.episode import Episode, load_runtime
from soc.ledger.models import (
    ChunkBuilt,
    EventType,
    GateDecision,
    StaleDetected,
    StaleMarked,
    Tombstone,
    VersionChange,
    VoteCast,
)
from soc.ledger.read_only import RecordedLedger, read_events
from soc.ledger.schema.validation import version_iri
from soc.ledger.store import GENESIS, _episode_fingerprint

SCHEMA_VERSION = 1
TEMPLATE = Path(__file__).with_name("timeline.html")
KNOWN_EVENTS = frozenset(get_args(EventType))
type Row = dict[str, Any]


@dataclass
class Build:
    seq: int
    data: ChunkBuilt
    gates: list[Row] = field(default_factory=list)
    marks: list[Row] = field(default_factory=list)
    detections: list[Row] = field(default_factory=list)

    def export(self) -> Row:
        statuses = {}
        for use in self.data.uses:
            key = use.commitment_id
            marks = [mark["seq"] for mark in self.marks if mark["commitment_id"] == key]
            status = "current"
            if marks:
                status = (
                    "blocked"
                    if any(
                        not gate["accepted"] and gate["seq"] > marks[0]
                        for gate in self.gates
                    )
                    else "stale"
                )
            statuses[key] = status
        return {
            "seq": self.seq,
            **self.data.model_dump(mode="json"),
            "statuses": statuses,
            "gate_decisions": self.gates,
            "stale_marks": self.marks,
            "detections": self.detections,
        }


class Timeline:
    def __init__(self, ledger: RecordedLedger, episode: Episode) -> None:
        self.events: list[Row] = [dict(event) for event in ledger.events]
        source = {item.id: item.model_dump(mode="json") for item in episode.source}
        self.chunks = {
            f"chunk-{position + ledger.chunk_id_base - 1}": {
                "id": f"chunk-{position + ledger.chunk_id_base - 1}",
                "position": position,
                "sentences": [source[sid] for sid in chunk.sentence_ids],
            }
            for position, chunk in enumerate(episode.events.chunks, 1)
        }
        self.builds: dict[str, dict[int, Build]] = {key: {} for key in self.chunks}
        self.commitments: dict[str, Row] = {}
        self.versions: dict[str, Row] = {}
        self.tombstones: list[Row] = []
        self.unplaced: list[Row] = []

    def unplace(self, event: Row, reason: str) -> None:
        self.unplaced.append(
            {"seq": event["seq"], "type": event["type"], "reason": reason}
        )

    def project(self) -> None:
        handlers: dict[str, Callable[[Row, Row], None]] = {
            "ChunkBuilt": self._build,
            "VoteCast": self._vote,
            "GateDecision": self._decision,
            "StaleMarked": self._mark,
            "StaleDetected": self._detected,
            "Tombstoned": self._tombstone,
        }
        for event in self.events:
            kind = event["type"]
            if kind not in KNOWN_EVENTS:
                self.unplace(event, "Unknown event type")
                continue
            if event["chunk_id"] is not None and event["chunk_id"] not in self.chunks:
                self.unplace(event, "Event names a chunk outside the public schedule")
                continue
            handler = (
                self._version
                if event["new_version"] is not None
                else handlers.get(kind)
            )
            if handler is not None:
                try:
                    handler(event, event["payload"]["data"])
                except (KeyError, TypeError, ValueError) as exc:
                    self.unplace(
                        event, f"Cannot place event data ({type(exc).__name__})"
                    )

    def _version(self, event: Row, data: Row) -> None:
        change = VersionChange.model_validate(data)
        key = change.commitment.id
        record = self.commitments.setdefault(
            key, {**change.commitment.model_dump(mode="json"), "versions": []}
        )
        version = change.version
        versions = record["versions"]
        if versions:
            versions[-1]["version"]["valid_until"] = version.valid_from
        row = {
            "seq": event["seq"],
            **change.model_dump(mode="json", exclude={"commitment"}),
            "ballots": [],
        }
        versions.append(row)
        self.versions[version_iri(key, version.version)] = row

    def _build(self, event: Row, data: Row) -> None:
        build = ChunkBuilt.model_validate(data)
        self.builds[event["chunk_id"]][build.build_number] = Build(event["seq"], build)
        if any(
            version_iri(ref.commitment_id, ref.version) not in self.versions
            for ref in build.uses
        ):
            self.unplace(event, "Build references an unavailable commitment version")

    def _vote(self, event: Row, data: Row) -> None:
        vote = VoteCast.model_validate(data)
        version = self.versions[vote.decision_id]
        choice = vote.choice
        status = (
            "abstention"
            if choice is None
            else ("support" if choice == version["version"]["rendering"] else "dissent")
        )
        version["ballots"].append(
            {"seq": event["seq"], **vote.model_dump(mode="json"), "status": status}
        )

    def _decision(self, event: Row, data: Row) -> None:
        verdict = GateDecision.model_validate(data)
        builds = self.builds[event["chunk_id"]]
        # Older gate records were attached to the build current at that sequence.
        number = (
            verdict.build_number
            if verdict.build_number is not None
            else next(reversed(builds), 0)
        )
        builds[number].gates.append(
            {"seq": event["seq"], **verdict.model_dump(mode="json")}
        )

    def _mark(self, event: Row, data: Row) -> None:
        mark = StaleMarked.model_validate(data)
        ref = mark.replaced_by or mark.overturned
        key = ref.commitment_id if ref is not None else event["commitment_id"]
        for chunk_id in mark.chunk_ids:
            builds = self.builds.get(chunk_id, {})
            if not builds:
                self.unplace(event, f"Stale mark has no build for {chunk_id}")
                continue
            builds[next(reversed(builds))].marks.append(
                {
                    "seq": event["seq"],
                    "commitment_id": key,
                    **mark.model_dump(mode="json", exclude={"chunk_ids"}),
                }
            )

    def _detected(self, event: Row, data: Row) -> None:
        detection = StaleDetected.model_validate(data)
        self.builds[event["chunk_id"]][detection.build_number].detections.append(
            {
                "seq": event["seq"],
                **detection.model_dump(mode="json"),
            }
        )

    def _tombstone(self, event: Row, data: Row) -> None:
        row = Tombstone.model_validate(data)
        self.tombstones.append({"seq": event["seq"], **row.model_dump(mode="json")})
        if version_iri(row.commitment_id, row.version) not in self.versions:
            self.unplace(
                event, "Tombstone references an unavailable commitment version"
            )


def _run_metadata(directory: Path, run_id: str) -> Row:
    manifest = directory / "run.json"
    if not manifest.is_file():
        return {}
    metadata: Row = json.loads(manifest.read_text(encoding="utf-8"))
    if metadata.get("run_id") != run_id:
        raise ValueError("ledger sequence 1: run metadata names another run")
    return metadata


def export_data(run_dir: Path, episode_dir: Path, *, label: str | None = None) -> Row:
    episode = load_runtime(episode_dir)
    ledger = read_events(run_dir / "ledger.sqlite", episode)
    run = _run_metadata(run_dir, ledger.run_id)
    run_label = label or run.get("label")
    if run_label is None and (run.get("scripted") or run.get("fixture")):
        run_label = "Scripted fixture"
    timeline = Timeline(ledger, episode)
    timeline.project()
    events: list[Row] = [
        {
            **event,
            "payload": {
                key: value
                for key, value in event["payload"].items()
                if key not in {"@graph", "@context"}
            },
        }
        for event in timeline.events
    ]
    chunks: list[Row] = [
        {
            **chunk,
            "builds": [build.export() for build in timeline.builds[key].values()],
        }
        for key, chunk in timeline.chunks.items()
    ]
    stale = [
        chunk["id"]
        for chunk in chunks
        if chunk["builds"] and chunk["builds"][-1]["stale_marks"]
    ]
    schedule = episode.events
    return {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": ledger.run_id,
            "episode_id": run.get("episode_id")
            or _episode_fingerprint(episode, 1)[:12],
            "label": run_label,
            "head_hash": events[-1]["hash"] if events else GENESIS,
            "final_sequence": len(events),
            "chunk_id_base": ledger.chunk_id_base,
        },
        "schedule": {
            "executor_change_after": schedule.executor_change_after,
            "brief_change_after": schedule.brief_change.chunk,
            "observed_changes": [
                event["seq"]
                for event in events
                if event["type"] in {"ExecutorRebound", "BriefChanged"}
            ],
        },
        "chunks": chunks,
        "commitments": list(timeline.commitments.values()),
        "tombstones": timeline.tombstones,
        "events": events,
        "unplaced_events": timeline.unplaced,
        "final_stale": stale,
        "summary": {
            "decisions": len(timeline.commitments),
            "chunks": len(chunks),
            "events": len(events),
            "revisions": sum(
                event["new_version"] is not None and bool(event["base_version"])
                for event in events
            ),
            "stale_builds": sum(
                bool(build["stale_marks"])
                for chunk in chunks
                for build in chunk["builds"]
            ),
            "gate_blocks": sum(_gate_block(event) for event in events),
            "final_stale": len(stale),
            "tombstones": len(timeline.tombstones),
        },
    }


def _gate_block(event: Row) -> bool:
    if event["type"] != "GateDecision":
        return False
    data = event["payload"].get("data")
    return isinstance(data, dict) and data.get("accepted") is False


def _check_paths(ledger: Path, outputs: Sequence[Path]) -> None:
    protected = {
        ledger.resolve(),
        *(
            ledger.with_name(ledger.name + suffix).resolve()
            for suffix in ("-wal", "-shm", "-journal")
        ),
    }
    paths = [path.resolve() for path in outputs]
    if len(set(paths)) != len(paths) or any(
        path in protected or (path.exists() and path.samefile(ledger)) for path in paths
    ):
        raise ValueError(
            "choose distinct export paths outside the recorded database files"
        )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("episode_dir", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--json", dest="data", type=Path)
    parser.add_argument("--label")
    args = parser.parse_args(argv)
    try:
        _check_paths(
            args.run_dir / "ledger.sqlite",
            [args.out] if args.data is None else [args.out, args.data],
        )
        data = export_data(args.run_dir, args.episode_dir, label=args.label)
        encoded = json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        html = TEMPLATE.read_text(encoding="utf-8").replace(
            "@@TIMELINE_DATA@@", encoded.replace("</", "<\\/")
        )
        args.out.write_text(html, encoding="utf-8")
        if args.data is not None:
            args.data.write_text(encoded, encoding="utf-8")
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
