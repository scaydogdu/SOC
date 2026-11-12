"""Durable worker checkpoints and single-writer ownership of a run directory."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from soc.memory import ChunkOutput
from soc.providers.models import Outcome


class Attempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    outcome: Outcome
    output: ChunkOutput | None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    estimated_cost_usd: float = Field(ge=0)

    @model_validator(mode="after")
    def consistent_output(self) -> Self:
        if (self.outcome is Outcome.OK) != (self.output is not None):
            raise ValueError("only successful attempts carry translated output")
        return self


class Journal:
    def __init__(self, db: sqlite3.Connection, fingerprint: str) -> None:
        self._db = db
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS run (fingerprint TEXT NOT NULL)")
            row = db.execute("SELECT fingerprint FROM run").fetchone()
            if row is None:
                db.execute("INSERT INTO run VALUES (?)", (fingerprint,))
            elif row[0] != fingerprint:
                raise ValueError("run configuration, episode, or templates changed")
            db.execute(
                "CREATE TABLE IF NOT EXISTS attempts ("
                "call_id TEXT PRIMARY KEY, prompt_hash TEXT NOT NULL, result TEXT)"
            )

    def prepare(self, call_id: str, prompt_hash: str) -> tuple[bool, Attempt | None]:
        row = self._db.execute(
            "SELECT prompt_hash, result FROM attempts WHERE call_id = ?", (call_id,)
        ).fetchone()
        if row is not None:
            if row[0] != prompt_hash:
                raise ValueError(f"prompt changed for recorded call {call_id}")
            return True, Attempt.model_validate_json(row[1]) if row[
                1
            ] is not None else None
        with self._db:
            self._db.execute(
                "INSERT INTO attempts (call_id, prompt_hash) VALUES (?, ?)",
                (call_id, prompt_hash),
            )
        return False, None

    def complete(self, call_id: str, result: Attempt) -> None:
        with self._db:
            self._db.execute(
                "UPDATE attempts SET result = ? WHERE call_id = ?",
                (result.model_dump_json(), call_id),
            )


@contextmanager
def run_lock(directory: Path) -> Iterator[None]:
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".run.lock").open("a+b") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("another process owns this run directory") from exc
        yield


@contextmanager
def open_journal(directory: Path, fingerprint: str) -> Iterator[Journal]:
    with run_lock(directory):
        with closing(sqlite3.connect(directory / "journal.sqlite")) as db:
            yield Journal(db, fingerprint)
