"""Public episode records and the runtime loader."""

import json
import re
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, StringConstraints

SENTENCE_ID = re.compile(
    r"[1-9][0-9]*-(?P<section>(?:[1-9][0-9]*|[A-Z]|boilerplate|toc)(?:\.[1-9][0-9]*)*)-s(?:0[1-9]|[1-9][0-9]+)"
)


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Sentence(Record):
    id: str
    section: str
    text: str
    reading_text: str
    scope_keys: list[str]
    translatable: bool = True


class Clause(Record):
    id: str
    text: str


class Brief(Record):
    clauses: list[Clause]


class Chunk(Record):
    sentence_ids: list[str]


class Approval(Record):
    decision_id: NonBlank
    value: NonBlank
    scope_keys: Annotated[list[NonBlank], Field(min_length=1)]
    kind: Literal["term", "non_lexical", "scope_limited"]
    chunk: PositiveInt
    brief_clause_ids: list[str]


class Revision(Record):
    decision_id: NonBlank
    value: NonBlank


class BriefChange(Record):
    chunk: PositiveInt
    revisions: list[Revision]
    clauses: list[Clause]


class Schedule(Record):
    chunks: list[Chunk]
    executor_change_after: PositiveInt
    brief_change: BriefChange
    approvals: list[Approval]


class Episode(Record):
    source: list[Sentence]
    brief: Brief
    events: Schedule


class EpisodeError(ValueError):
    """A package violates a named validation rule."""


def validate_runtime(episode: Episode) -> Episode:
    """Check the relationships among the three public files."""
    events = episode.events
    sentence_ids: set[str] = set()
    for sentence in episode.source:
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
    chunk_ids = [sid for chunk in events.chunks for sid in chunk.sentence_ids]
    scheduled_ids = set(chunk_ids)
    if missing := scheduled_ids - sentence_ids:
        raise EpisodeError(f"sentence IDs: unknown reference {sorted(missing)}")
    if len(chunk_ids) != len(sentence_ids) or scheduled_ids != sentence_ids:
        raise EpisodeError("sentence IDs: chunks must partition the source sentences")

    brief_ids: set[str] = set()
    for clauses in (episode.brief.clauses, events.brief_change.clauses):
        ids = {clause.id for clause in clauses}
        if len(ids) != len(clauses) or any(
            not clause.id.strip() or not clause.text.strip() for clause in clauses
        ):
            raise EpisodeError(
                "brief clause IDs: expected unique IDs and nonblank clauses"
            )
        brief_ids.update(ids)
    brief_refs = {
        cid for approval in events.approvals for cid in approval.brief_clause_ids
    }
    if missing := brief_refs - brief_ids:
        raise EpisodeError(f"brief clause IDs: unknown reference {sorted(missing)}")

    executor = events.executor_change_after
    change = events.brief_change.chunk
    positions = [executor, change, *(approval.chunk for approval in events.approvals)]
    if max(positions) > len(events.chunks) or executor >= change:
        raise EpisodeError(
            "event positions: expected in-range chunks and executor change before brief change"
        )
    return episode


def read_lines[T: Record](path: Path, model: type[T]) -> list[T]:
    return [
        model.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def read_yaml[T: Record](path: Path, model: type[T]) -> T:
    return model.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def load_runtime(directory: Path) -> Episode:
    return Episode(
        source=read_lines(directory / "source.jsonl", Sentence),
        brief=read_yaml(directory / "brief.yaml", Brief),
        events=read_yaml(directory / "events.yaml", Schedule),
    )


def reusable_term_sites(
    episode: Episode, sentence_id: str, scope_key: str
) -> tuple[str, ...]:
    """Find the originating and later uses before scheduled principal ownership."""
    schedule = episode.events
    source = {sentence.id: sentence for sentence in episode.source}
    origin = next(
        (
            index
            for index, chunk in enumerate(schedule.chunks)
            if sentence_id in chunk.sentence_ids
        ),
        None,
    )
    if origin is None or scope_key not in source[sentence_id].scope_keys:
        raise ValueError("term must match a scope key at its source sentence")
    # A scheduled approval claims matching sites after its boundary.
    end = min(
        (
            approval.chunk
            for approval in schedule.approvals
            if scope_key in approval.scope_keys
        ),
        default=len(schedule.chunks),
    )
    return tuple(
        sid
        for index, chunk in enumerate(schedule.chunks[origin:end], origin)
        for sid in chunk.sentence_ids
        if (index > origin or sid == sentence_id)
        and source[sid].translatable
        and scope_key in source[sid].scope_keys
    )


def approval_sites(episode: Episode, approval: Approval) -> tuple[Sentence, ...]:
    """Return the public sites to which a scheduled approval applies."""
    later = {
        sentence_id
        for chunk in episode.events.chunks[approval.chunk :]
        for sentence_id in chunk.sentence_ids
    }
    scopes = set(approval.scope_keys)
    return tuple(
        sentence
        for sentence in episode.source
        if sentence.id in later
        and sentence.translatable
        and scopes.intersection(sentence.scope_keys)
    )
