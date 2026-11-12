"""Create, inspect, and seal episode drafts without using evaluation data at runtime."""

import argparse
import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, PositiveInt

from eval.episode import (
    FILES,
    Affected,
    CoverageCheck,
    EpisodeError,
    FullEpisode,
    Hidden,
    Manifest,
    Seed,
    coverage_checks,
    validate_loaded,
)
from soc.episode import (
    Approval,
    Brief,
    Chunk,
    Clause,
    Record,
    Revision,
    Sentence,
    load_runtime,
    read_lines,
    read_yaml,
)
from soc.tasks.segmenter import BCP14, segment

_RUN = re.compile(r"[a-z][a-z-]+(?: [a-z][a-z-]+)+")
_COMMON = frozenset(
    "a an and are as at be been but by can could does for from had has have if in is it its "
    "may might must not of on or shall should so than that the their them then there these "
    "this those through to use used using was were when where which while will with would".split()
)


class DraftBriefChange(Record):
    chunk: PositiveInt | None = None
    revisions: list[Revision] = Field(default_factory=list)
    clauses: list[Clause] = Field(default_factory=list)


class DraftSchedule(Record):
    chunks: list[Chunk]
    executor_change_after: PositiveInt | None = None
    brief_change: DraftBriefChange = Field(default_factory=DraftBriefChange)
    approvals: list[Approval] = Field(default_factory=list)


class Candidate(Record):
    scope_key: str
    kind: Literal["term", "non_lexical"]
    chunks: list[int]
    sentence_uses: int


def _in_section(label: str, section: str) -> bool:
    return label == section or label.startswith(f"{section}.")


def _select(
    sentences: Sequence[Sentence], first: str, last: str
) -> tuple[Sentence, ...]:
    starts = [
        i
        for i, sentence in enumerate(sentences)
        if _in_section(sentence.section, first)
    ]
    ends = [
        i for i, sentence in enumerate(sentences) if _in_section(sentence.section, last)
    ]
    if not starts or not ends or starts[0] > ends[-1]:
        raise EpisodeError("choose an ordered section range present in the RFC")
    return tuple(sentences[starts[0] : ends[-1] + 1])


def _recurring_terms(source: Sequence[Sentence], limit: int) -> list[str]:
    counts: Counter[str] = Counter()
    sections: dict[str, set[str]] = defaultdict(set)
    for sentence in source:
        if not sentence.translatable:
            continue
        for match in _RUN.finditer(sentence.reading_text.lower()):
            words = match[0].split()
            for size in (2, 3):
                for start in range(len(words) - size + 1):
                    tokens = words[start : start + size]
                    if any(token in _COMMON for token in tokens):
                        continue
                    phrase = " ".join(tokens)
                    counts[phrase] += 1
                    sections[phrase].add(sentence.section)
    recurring = (key for key, places in sections.items() if len(places) >= 2)
    return sorted(recurring, key=lambda key: (-len(sections[key]), -counts[key], key))[
        :limit
    ]


def _proposals(
    source: Sequence[Sentence], positions: dict[str, int], terms: set[str]
) -> tuple[Candidate, ...]:
    occurrences: Counter[str] = Counter()
    chunks: dict[str, set[int]] = defaultdict(set)
    for sentence in source:
        if not sentence.translatable:
            continue
        for key in sentence.scope_keys:
            if key in terms or BCP14.fullmatch(key):
                occurrences[key] += 1
                chunks[key].add(positions[sentence.section])
    return tuple(
        Candidate(
            scope_key=key,
            kind="non_lexical" if BCP14.fullmatch(key) else "term",
            chunks=sorted(chunks[key]),
            sentence_uses=occurrences[key],
        )
        for key in sorted(chunks)
        if len(chunks[key]) >= 2 or BCP14.fullmatch(key)
    )


def _manifest(directory: Path) -> Manifest:
    found = {path.name for path in directory.iterdir()} - {"manifest.json", ".DS_Store"}
    if found != FILES:
        raise EpisodeError("expected the six episode content files")
    hashes = {}
    for name in sorted(FILES):
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise EpisodeError(f"expected a regular episode file: {name}")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return Manifest(sha256=hashes)


def _write_manifest(directory: Path, manifest: Manifest) -> None:
    path = directory / "manifest.json"
    temporary = directory / ".manifest.json.tmp"
    temporary.write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _dump_yaml(path: Path, record: Record) -> None:
    path.write_text(
        yaml.safe_dump(
            record.model_dump(mode="json"), allow_unicode=True, sort_keys=False
        ),
        encoding="utf-8",
    )


def generate_skeleton(
    xml_path: Path,
    output: Path,
    *,
    first: str,
    last: str,
    glossary: Sequence[str] = (),
    proposal_limit: int = 30,
) -> tuple[Candidate, ...]:
    if proposal_limit < 0:
        raise ValueError("proposal limit must be nonnegative")
    xml = xml_path.read_bytes()
    draft = _select(tuple(segment(xml)), first, last)
    terms = set(_recurring_terms(draft, proposal_limit))
    terms.update(term.strip().lower() for term in glossary if term.strip())
    source = _select(tuple(segment(xml, sorted(terms))), first, last)
    sections: dict[str, list[str]] = defaultdict(list)
    for sentence in source:
        sections[sentence.section].append(sentence.id)
    positions = {section: index for index, section in enumerate(sections, 1)}
    schedule = DraftSchedule(
        chunks=[Chunk(sentence_ids=ids) for ids in sections.values()]
    )
    output.mkdir(parents=True)
    (output / "source.jsonl").write_text(
        "".join(sentence.model_dump_json() + "\n" for sentence in source),
        encoding="utf-8",
    )
    _dump_yaml(output / "brief.yaml", Brief(clauses=[]))
    _dump_yaml(output / "events.yaml", schedule)
    _dump_yaml(output / "hidden.yaml", Hidden(preferences=[], planted_errors=[]))
    for name in ("seeds.jsonl", "affected.jsonl"):
        (output / name).write_text("", encoding="utf-8")
    _write_manifest(output, _manifest(output))
    return _proposals(source, positions, terms)


def draft_coverage(directory: Path) -> tuple[CoverageCheck, ...]:
    source = read_lines(directory / "source.jsonl", Sentence)
    schedule = read_yaml(directory / "events.yaml", DraftSchedule)
    affected = read_lines(directory / "affected.jsonl", Affected)
    executor = schedule.executor_change_after
    change = schedule.brief_change.chunk
    source_ids = {sentence.id for sentence in source}
    chunk_ids = [sid for chunk in schedule.chunks for sid in chunk.sentence_ids]
    count = len(schedule.chunks)
    structural = (
        CoverageCheck(
            "chunk_partition",
            bool(schedule.chunks)
            and all(chunk.sentence_ids for chunk in schedule.chunks)
            and len(chunk_ids) == len(source) == len(source_ids)
            and set(chunk_ids) == source_ids,
            "nonempty chunks must partition the source sentences",
        ),
        CoverageCheck(
            "executor_position",
            executor is not None and executor <= count,
            "set an in-range executor change",
            executor,
        ),
        CoverageCheck(
            "brief_position",
            change is not None and change <= count,
            "set an in-range brief change",
            change,
        ),
        CoverageCheck(
            "event_order",
            executor is not None and change is not None and executor < change,
            "schedule the executor change before the brief change",
        ),
        CoverageCheck(
            "approval_positions",
            all(approval.chunk <= count for approval in schedule.approvals),
            "schedule approvals within the chunk range",
        ),
        CoverageCheck(
            "affected_positions",
            all(
                site.chunk <= count
                and site.sentence_id in schedule.chunks[site.chunk - 1].sentence_ids
                for entry in affected
                for site in (*entry.repair, *entry.apply)
            ),
            "affected sites must belong to the named chunk",
        ),
    )
    return structural + coverage_checks(
        source,
        schedule.chunks,
        schedule.approvals,
        [revision.decision_id for revision in schedule.brief_change.revisions],
        affected,
        executor=executor,
        change=change,
    )


def seal(directory: Path) -> FullEpisode:
    manifest = _manifest(directory)
    episode = validate_loaded(
        FullEpisode(
            runtime=load_runtime(directory),
            hidden=read_yaml(directory / "hidden.yaml", Hidden),
            seeds=read_lines(directory / "seeds.jsonl", Seed),
            affected=read_lines(directory / "affected.jsonl", Affected),
            manifest=manifest,
        )
    )
    _write_manifest(directory, manifest)
    return episode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    skeleton = commands.add_parser("skeleton")
    skeleton.add_argument("rfc_xml", type=Path)
    skeleton.add_argument("directory", type=Path)
    skeleton.add_argument("--first", required=True)
    skeleton.add_argument("--last", required=True)
    skeleton.add_argument("--glossary", type=Path)
    skeleton.add_argument("--proposal-limit", type=int, default=30)
    for name in ("coverage", "seal"):
        commands.add_parser(name).add_argument("directory", type=Path)
    args = parser.parse_args()
    if args.command == "skeleton":
        glossary = (
            args.glossary.read_text(encoding="utf-8").splitlines()
            if args.glossary
            else ()
        )
        candidates = generate_skeleton(
            args.rfc_xml,
            args.directory,
            first=args.first,
            last=args.last,
            glossary=glossary,
            proposal_limit=args.proposal_limit,
        )
        print(
            json.dumps(
                [candidate.model_dump() for candidate in candidates],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if args.command == "coverage":
        report = draft_coverage(args.directory)
        print(
            json.dumps(
                [asdict(check) for check in report], ensure_ascii=False, indent=2
            )
        )
        return int(not all(check.passed for check in report))
    seal(args.directory)
    print(f"Sealed episode: {args.directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
