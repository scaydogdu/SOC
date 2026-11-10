"""Complete episode loader and offline package validator."""

import argparse
import hashlib
import re
from pathlib import Path
from typing import Literal

from pydantic import PositiveInt

from soc.episode import (
    SENTENCE_ID,
    Episode,
    NonBlank,
    Record,
    load_runtime,
    read_lines,
    read_yaml,
)

FILES = frozenset(
    {
        "source.jsonl",
        "brief.yaml",
        "events.yaml",
        "hidden.yaml",
        "seeds.jsonl",
        "affected.jsonl",
    }
)
SHA256 = re.compile(r"[0-9a-f]{64}")


class Preference(Record):
    id: str
    text: str
    brief_clause_ids: list[str]


class PlantedError(Record):
    id: NonBlank
    chunk: PositiveInt
    sentence_id: str
    kind: NonBlank


class Hidden(Record):
    preferences: list[Preference]
    planted_errors: list[PlantedError]


class Seed(Record):
    id: str
    type: str
    sentence_ids: list[str]
    expected_answer: str
    correctness: Literal[
        "source_fidelity", "client_preference", "commitment_continuity"
    ]


class UseSite(Record):
    chunk: PositiveInt
    sentence_id: str


class Affected(Record):
    decision_id: str
    repair: list[UseSite]
    apply: list[UseSite]


class Manifest(Record):
    sha256: dict[str, str]


class FullEpisode(Record):
    runtime: Episode
    hidden: Hidden
    seeds: list[Seed]
    affected: list[Affected]
    manifest: Manifest


class EpisodeError(ValueError):
    """A package violates a named validation rule."""


def _manifest(directory: Path) -> Manifest:
    if {
        path.name for path in directory.iterdir() if path.name != ".DS_Store"
    } != FILES | {"manifest.json"}:
        raise EpisodeError("manifest: expected exactly seven episode files")
    try:
        manifest = Manifest.model_validate_json(
            (directory / "manifest.json").read_bytes()
        )
    except ValueError as exc:
        raise EpisodeError(f"manifest: invalid format: {exc}") from exc
    if manifest.sha256.keys() != FILES:
        raise EpisodeError("manifest: hash entries must name all six content files")
    for name, digest in manifest.sha256.items():
        path = directory / name
        if (
            not path.is_file()
            or path.is_symlink()
            or not SHA256.fullmatch(digest)
            or hashlib.sha256(path.read_bytes()).hexdigest() != digest
        ):
            raise EpisodeError(f"manifest: SHA-256 mismatch for {name}")
    return manifest


def load_full(directory: Path) -> FullEpisode:
    return FullEpisode(
        manifest=_manifest(directory),
        runtime=load_runtime(directory),
        hidden=read_yaml(directory / "hidden.yaml", Hidden),
        seeds=read_lines(directory / "seeds.jsonl", Seed),
        affected=read_lines(directory / "affected.jsonl", Affected),
    )


def validate(directory: Path) -> FullEpisode:
    episode = load_full(directory)
    runtime = episode.runtime
    events = runtime.events
    sentence_ids: set[str] = set()
    for sentence in runtime.source:
        match = SENTENCE_ID.fullmatch(sentence.id)
        if (
            match is None
            or match["section"] != sentence.section
            or sentence.id in sentence_ids
        ):
            raise EpisodeError(f"sentence IDs: malformed or duplicate ID {sentence.id}")
        sentence_ids.add(sentence.id)
    if any(not chunk.sentence_ids for chunk in events.chunks):
        raise EpisodeError("event positions: chunks cannot be empty")
    affected_sites = [
        site for entry in episode.affected for site in entry.repair + entry.apply
    ]
    chunk_ids = [sid for chunk in events.chunks for sid in chunk.sentence_ids]
    refs = set(chunk_ids)
    refs.update(sid for seed in episode.seeds for sid in seed.sentence_ids)
    refs.update(site.sentence_id for site in affected_sites)
    refs.update(error.sentence_id for error in episode.hidden.planted_errors)
    if missing := refs - sentence_ids:
        raise EpisodeError(f"sentence IDs: unknown reference {sorted(missing)}")
    if len(chunk_ids) != len(sentence_ids) or set(chunk_ids) != sentence_ids:
        raise EpisodeError("sentence IDs: chunks must partition the source sentences")

    brief_ids = {
        clause.id for clause in runtime.brief.clauses + events.brief_change.clauses
    }
    brief_refs = {
        cid for approval in events.approvals for cid in approval.brief_clause_ids
    }
    brief_refs.update(
        cid for pref in episode.hidden.preferences for cid in pref.brief_clause_ids
    )
    if missing := brief_refs - brief_ids:
        raise EpisodeError(f"brief clause IDs: unknown reference {sorted(missing)}")

    executor = events.executor_change_after
    change = events.brief_change.chunk
    positions = [executor, change]
    positions.extend(approval.chunk for approval in events.approvals)
    use_sites = affected_sites + [
        UseSite(chunk=error.chunk, sentence_id=error.sentence_id)
        for error in episode.hidden.planted_errors
    ]
    positions.extend(site.chunk for site in use_sites)
    if max(positions) > len(events.chunks) or executor >= change:
        raise EpisodeError(
            "event positions: expected in-range chunks and executor change before brief change"
        )
    if any(
        site.sentence_id not in events.chunks[site.chunk - 1].sentence_ids
        for site in use_sites
    ):
        raise EpisodeError("event positions: use site is outside its named chunk")

    _coverage(episode)
    return episode


def _coverage(episode: FullEpisode) -> None:
    events = episode.runtime.events
    uses: dict[str, set[int]] = {}
    by_id = {sentence.id: sentence for sentence in episode.runtime.source}
    for index, chunk in enumerate(events.chunks, 1):
        for sentence_id in chunk.sentence_ids:
            for key in by_id[sentence_id].scope_keys:
                uses.setdefault(key, set()).add(index)
    approvals = {approval.decision_id: approval for approval in events.approvals}
    if len(approvals) != len(events.approvals):
        raise EpisodeError("coverage: duplicate approval decision ID")

    def spans(decision_id: str, boundary: int) -> bool:
        approval = approvals.get(decision_id)
        if approval is None:
            return False
        positions = {
            chunk
            for key in approval.scope_keys
            for chunk in uses.get(key, set())
            if chunk > approval.chunk
        }
        return bool(positions) and min(positions) <= boundary < max(positions)

    if sum(spans(key, events.executor_change_after) for key in approvals) < 5:
        raise EpisodeError(
            "coverage: five approved decisions must span the executor change"
        )
    revised = {revision.decision_id for revision in events.brief_change.revisions}
    if len(revised) != len(events.brief_change.revisions):
        raise EpisodeError("coverage: duplicate revised decision ID")
    if any(not spans(key, events.brief_change.chunk) for key in revised):
        raise EpisodeError("coverage: revised decisions must span the brief change")
    kinds = {approvals[key].kind for key in revised}
    if "term" not in kinds or not kinds.intersection({"non_lexical", "scope_limited"}):
        raise EpisodeError(
            "coverage: brief change needs a term and a non-lexical or scope-limited decision"
        )
    affected = {entry.decision_id for entry in episode.affected}
    if affected != revised or len(affected) != len(episode.affected):
        raise EpisodeError("coverage: affected sets must match the revised decisions")

    for entry in episode.affected:
        approval = approvals[entry.decision_id]
        boundary = events.brief_change.chunk
        if (
            not entry.repair
            or not entry.apply
            or any(not approval.chunk < site.chunk <= boundary for site in entry.repair)
            or any(site.chunk <= boundary for site in entry.apply)
        ):
            raise EpisodeError(
                "coverage: affected sites must be on their declared side"
            )
        if any(
            not set(approval.scope_keys).intersection(
                by_id[site.sentence_id].scope_keys
            )
            for site in entry.repair + entry.apply
        ):
            raise EpisodeError(
                "coverage: affected site does not use the decision scope"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("episode", type=Path)
    args = parser.parse_args()
    try:
        validate(args.episode)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{exc}\n")
    print(f"Valid episode: {args.episode}")


if __name__ == "__main__":
    main()
