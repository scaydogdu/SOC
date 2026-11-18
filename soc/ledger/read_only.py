"""Verify and read an episode-bound ledger without opening a writer."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from soc.episode import Episode, validate_runtime
from soc.ledger.store import (
    ChunkIdBase,
    LedgerIntegrityError,
    _episode_fingerprint,
    _verified_rows,
)


@dataclass(frozen=True)
class RecordedLedger:
    run_id: str
    chunk_id_base: ChunkIdBase
    events: list[dict[str, JsonValue]]


def read_events(path: Path, episode: Episode) -> RecordedLedger:
    """Verify a consistent read snapshot and retain event types unknown to this reader."""
    episode = validate_runtime(episode)
    uri = path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        has_binding = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'public_episode'"
        ).fetchone()
        binding = (
            db.execute(
                "SELECT run_id, sha256 FROM public_episode WHERE id = 1"
            ).fetchone()
            if has_binding is not None
            else None
        )
        if binding is None:
            raise LedgerIntegrityError(1, "missing public episode binding")
        run_id: str = binding["run_id"]
        rows = db.execute("SELECT * FROM events ORDER BY seq")
        events = list(_verified_rows(rows, run_id))
        if binding["sha256"] == _episode_fingerprint(episode, 1):
            base: ChunkIdBase = 1
        elif binding["sha256"] == _episode_fingerprint(episode, 0):
            base = 0
        else:
            raise LedgerIntegrityError(
                1, "public episode does not match the ledger binding"
            )
    return RecordedLedger(run_id, base, events)
