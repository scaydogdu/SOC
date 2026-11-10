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
