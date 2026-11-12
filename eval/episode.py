"""Complete episode loader and offline package validator."""

import argparse
import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import PositiveInt

from soc.episode import (
    SENTENCE_ID,
    Approval,
    Chunk,
    Episode,
    NonBlank,
    Record,
    Sentence,
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
    return validate_loaded(load_full(directory))


def validate_loaded(episode: FullEpisode) -> FullEpisode:
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


@dataclass(frozen=True)
class CoverageCheck:
    rule: str
    passed: bool
    requirement: str
    observed: object = None


def coverage_checks(
    source: Sequence[Sentence],
    chunks: Sequence[Chunk],
    approved: Sequence[Approval],
    revisions: Sequence[str],
    affected: Sequence[Affected],
    *,
    executor: int | None,
    change: int | None,
) -> tuple[CoverageCheck, ...]:
    uses: dict[str, set[int]] = {}
    by_id = {sentence.id: sentence for sentence in source}
    for index, chunk in enumerate(chunks, 1):
        for sentence_id in chunk.sentence_ids:
            sentence = by_id.get(sentence_id)
            if sentence is not None:
                for key in sentence.scope_keys:
                    uses.setdefault(key, set()).add(index)
    approvals = {approval.decision_id: approval for approval in approved}

    def spans(decision_id: str, boundary: int | None) -> bool:
        approval = approvals.get(decision_id)
        if approval is None or boundary is None:
            return False
        positions = {
            chunk
            for key in approval.scope_keys
            for chunk in uses.get(key, set())
            if chunk > approval.chunk
        }
        return bool(positions) and min(positions) <= boundary < max(positions)

    spanning_executor = sorted(key for key in approvals if spans(key, executor))
    revised = set(revisions)
    missing_spans = sorted(key for key in revised if not spans(key, change))
    kinds = {approvals[key].kind for key in revised if key in approvals}
    affected_ids = {entry.decision_id for entry in affected}
    checks = [
        CoverageCheck(
            "approval_ids",
            len(approvals) == len(approved),
            "duplicate approval decision ID",
        ),
        CoverageCheck(
            "executor_span",
            len(spanning_executor) >= 5,
            "five approved decisions must span the executor change",
            spanning_executor,
        ),
        CoverageCheck(
            "revision_ids",
            len(revised) == len(revisions),
            "duplicate revised decision ID",
        ),
        CoverageCheck(
            "brief_span",
            not missing_spans and change is not None,
            "revised decisions must span the brief change",
            missing_spans,
        ),
        CoverageCheck(
            "revision_kinds",
            "term" in kinds
            and bool(kinds.intersection({"non_lexical", "scope_limited"})),
            "brief change needs a term and a non-lexical or scope-limited decision",
            sorted(kinds),
        ),
        CoverageCheck(
            "affected_decisions",
            affected_ids == revised and len(affected_ids) == len(affected),
            "affected sets must match the revised decisions",
            {
                "missing": sorted(revised - affected_ids),
                "extra": sorted(affected_ids - revised),
            },
        ),
    ]
    for entry in affected:
        approval = approvals.get(entry.decision_id)
        valid_sides = (
            approval is not None
            and change is not None
            and bool(entry.repair)
            and bool(entry.apply)
            and all(approval.chunk < site.chunk <= change for site in entry.repair)
            and all(site.chunk > change for site in entry.apply)
        )
        checks.append(
            CoverageCheck(
                "affected_sides",
                valid_sides,
                "affected sites must be on their declared side",
                entry.decision_id,
            )
        )
        valid_scope = approval is not None and all(
            site.sentence_id in by_id
            and bool(
                set(approval.scope_keys).intersection(
                    by_id[site.sentence_id].scope_keys
                )
            )
            for site in entry.repair + entry.apply
        )
        checks.append(
            CoverageCheck(
                "affected_scope",
                valid_scope,
                "affected site does not use the decision scope",
                entry.decision_id,
            )
        )
    return tuple(checks)


def _coverage(episode: FullEpisode) -> None:
    events = episode.runtime.events
    for check in coverage_checks(
        episode.runtime.source,
        events.chunks,
        events.approvals,
        [revision.decision_id for revision in events.brief_change.revisions],
        episode.affected,
        executor=events.executor_change_after,
        change=events.brief_change.chunk,
    ):
        if not check.passed:
            raise EpisodeError(f"coverage: {check.requirement}")


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
