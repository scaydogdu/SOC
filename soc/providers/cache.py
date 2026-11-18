"""Content-addressed storage for completed logical calls."""

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from soc.providers.models import Outcome, ProviderRequest, RecordedReply

RETRYABLE_OUTCOMES = (
    Outcome.BUDGET_EXHAUSTED,
    Outcome.RATE_LIMITED,
    Outcome.SERVER_ERROR,
)


def cache_key(request: ProviderRequest, replicate: int) -> str:
    data = {"request": request.model_dump(mode="json"), "replicate": replicate}
    canonical = json.dumps(
        data, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS responses "
                "(key TEXT PRIMARY KEY, reply TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS calls ("
                "run_id TEXT NOT NULL, call_id TEXT NOT NULL, key TEXT NOT NULL, "
                "reply TEXT NOT NULL, PRIMARY KEY (run_id, call_id, key))"
            )

    def read(self, key: str) -> RecordedReply | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT reply FROM responses WHERE key = ?", (key,)
            ).fetchone()
        return None if row is None else RecordedReply.model_validate_json(row[0])

    def read_call(self, run_id: str, call_id: str, key: str) -> RecordedReply | None:
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT reply FROM calls WHERE run_id = ? AND call_id = ? AND key = ?",
                (run_id, call_id, key),
            ).fetchone()
        return None if row is None else RecordedReply.model_validate_json(row[0])

    def write(self, run_id: str, call_id: str, key: str, reply: RecordedReply) -> None:
        data = reply.model_dump_json()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO calls VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id, call_id, key) DO UPDATE SET reply = excluded.reply",
                (run_id, call_id, key, data),
            )
            if reply.outcome not in RETRYABLE_OUTCOMES:
                db.execute(
                    "INSERT INTO responses VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET reply = excluded.reply "
                    "WHERE json_extract(responses.reply, '$.outcome') IN (?, ?, ?)",
                    (key, data, *RETRYABLE_OUTCOMES),
                )
