"""Persistent USD reservations for all physical requests in a live campaign."""

import sqlite3
from contextlib import closing
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from soc.providers.models import ProviderRequestError, Reply

_NANOS = 1_000_000_000


class SpendBoundError(ProviderRequestError):
    """Provider-reported usage exceeded the configured billing envelope."""


class SpendRate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_input_tokens: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0)
    input_per_million: Decimal = Field(gt=0, le=10_000)
    output_per_million: Decimal = Field(gt=0, le=10_000)

    def charge(self, input_tokens: int, output_tokens: int) -> int:
        dollars = (
            input_tokens * self.input_per_million
            + output_tokens * self.output_per_million
        ) / 1_000_000
        return int((dollars * _NANOS).to_integral_value(rounding=ROUND_CEILING))


class SpendGuard:
    """Reserve the documented worst case before sending a paid request."""

    def __init__(
        self, path: Path, cap_usd: Decimal, rates: dict[str, SpendRate]
    ) -> None:
        if not cap_usd.is_finite() or not Decimal(0) < cap_usd <= Decimal(100_000):
            raise ValueError("spend cap must be a positive finite USD amount")
        self.path = path
        self.cap = int((cap_usd * _NANOS).to_integral_value(rounding=ROUND_FLOOR))
        self._rates = rates.copy()
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "CREATE TABLE IF NOT EXISTS cap (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "amount INTEGER NOT NULL)"
            )
            db.execute("INSERT OR IGNORE INTO cap VALUES (1, ?)", (self.cap,))
            (stored_cap,) = db.execute("SELECT amount FROM cap WHERE id = 1").fetchone()
            self._require_cap(stored_cap)
            db.execute(
                "CREATE TABLE IF NOT EXISTS charges (id INTEGER PRIMARY KEY, "
                "run_id TEXT NOT NULL, call_id TEXT NOT NULL, model TEXT NOT NULL, "
                "amount INTEGER NOT NULL, state TEXT NOT NULL, "
                "input_bound INTEGER, output_bound INTEGER, outcome TEXT)"
            )
            columns = {column[1] for column in db.execute("PRAGMA table_info(charges)")}
            for name, sql_type in (
                ("input_bound", "INTEGER"),
                ("output_bound", "INTEGER"),
                ("outcome", "TEXT"),
            ):
                if name not in columns:
                    db.execute(f"ALTER TABLE charges ADD COLUMN {name} {sql_type}")

    def _require_cap(self, stored: int) -> None:
        if stored != self.cap:
            raise ValueError(
                "campaign config differs from the stored spend cap; use spend set-cap "
                "for an approved change and match the config to the stored cap"
            )

    def reserve(
        self,
        run_id: str,
        call_id: str,
        model: str,
        *,
        input_bound: int,
        output_bound: int,
    ) -> int | None:
        rate = self._rates[model]
        if not (
            0 < input_bound <= rate.max_input_tokens
            and 0 < output_bound <= rate.max_output_tokens
        ):
            raise SpendBoundError(
                f"request bounds exceed the positive configured token limits for {model}"
            )
        amount = rate.charge(input_bound, output_bound)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            self._require_cap(
                db.execute("SELECT amount FROM cap WHERE id = 1").fetchone()[0]
            )
            spent, breached = db.execute(
                "SELECT COALESCE(SUM(amount), 0), "
                "COALESCE(MAX(state = 'bound_exceeded'), 0) FROM charges"
            ).fetchone()
            if breached or spent + amount > self.cap:
                return None
            cursor = db.execute(
                "INSERT INTO charges (run_id, call_id, model, amount, state, input_bound, output_bound) "
                "VALUES (?, ?, ?, ?, 'reserved', ?, ?)",
                (run_id, call_id, model, amount, input_bound, output_bound),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def settle(self, reservation: int, model: str, reply: Reply) -> None:
        rate = self._rates[model]
        amount = rate.charge(reply.input_tokens, reply.output_tokens)
        state = "reserved"
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT amount, input_bound, output_bound FROM charges "
                "WHERE id = ? AND model = ? AND state = 'reserved'",
                (reservation, model),
            ).fetchone()
            if row is None:
                raise RuntimeError("invalid or already settled spend reservation")
            reserved, input_bound, output_bound = row
            if reply.usage_reported:
                exceeded = (
                    reply.input_tokens > (input_bound or rate.max_input_tokens)
                    or reply.output_tokens > (output_bound or rate.max_output_tokens)
                    or amount > reserved
                )
                state = "bound_exceeded" if exceeded else "settled"
            db.execute(
                "UPDATE charges SET amount = ?, state = ?, outcome = ? WHERE id = ?",
                (
                    reserved if state == "reserved" else amount,
                    state,
                    reply.outcome,
                    reservation,
                ),
            )
        if state == "bound_exceeded":
            raise SpendBoundError(
                f"provider usage exceeded the configured bound for {model}"
            )

    def summary(self) -> dict[str, float | int | bool]:
        with closing(sqlite3.connect(self.path)) as db:
            metered, held, count, breached = db.execute(
                "SELECT COALESCE(SUM(CASE WHEN state != 'reserved' THEN amount END), 0), "
                "COALESCE(SUM(CASE WHEN state = 'reserved' THEN amount END), 0), "
                "COUNT(*), COALESCE(MAX(state = 'bound_exceeded'), 0) FROM charges"
            ).fetchone()
        return {
            "cap_usd": self.cap / _NANOS,
            "metered_upper_estimate_usd": metered / _NANOS,
            "unsettled_reservations_usd": held / _NANOS,
            "reserved_attempts": count,
            "bound_exceeded": bool(breached),
        }
