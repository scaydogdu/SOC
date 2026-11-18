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
            if stored_cap != self.cap:
                raise ValueError(
                    "campaign database was created with a different spend cap"
                )
            db.execute(
                "CREATE TABLE IF NOT EXISTS charges (id INTEGER PRIMARY KEY, "
                "run_id TEXT NOT NULL, call_id TEXT NOT NULL, model TEXT NOT NULL, "
                "amount INTEGER NOT NULL, state TEXT NOT NULL)"
            )

    def reserve(
        self, run_id: str, call_id: str, model: str, *, output_bound: int | None = None
    ) -> int | None:
        rate = self._rates[model]
        if output_bound is not None and output_bound <= 0:
            raise ValueError("output bound must be positive")
        max_output = rate.max_output_tokens
        if output_bound is not None:
            max_output = min(max_output, output_bound)
        amount = rate.charge(rate.max_input_tokens, max_output)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            spent, breached = db.execute(
                "SELECT COALESCE(SUM(amount), 0), "
                "COALESCE(MAX(state = 'bound_exceeded'), 0) FROM charges"
            ).fetchone()
            if breached or spent + amount > self.cap:
                return None
            cursor = db.execute(
                "INSERT INTO charges (run_id, call_id, model, amount, state) "
                "VALUES (?, ?, ?, ?, 'reserved')",
                (run_id, call_id, model, amount),
            )
            assert cursor.lastrowid is not None
            return cursor.lastrowid

    def settle(self, reservation: int, model: str, reply: Reply) -> None:
        if not reply.usage_reported:
            return
        rate = self._rates[model]
        amount = rate.charge(reply.input_tokens, reply.output_tokens)
        exceeded = (
            reply.input_tokens > rate.max_input_tokens
            or reply.output_tokens > rate.max_output_tokens
        )
        with closing(sqlite3.connect(self.path)) as db, db:
            updated = db.execute(
                "UPDATE charges SET state = CASE WHEN ? OR ? > amount "
                "THEN 'bound_exceeded' ELSE 'settled' END, amount = ? "
                "WHERE id = ? AND model = ? AND state = 'reserved' RETURNING state",
                (
                    exceeded,
                    amount,
                    amount,
                    reservation,
                    model,
                ),
            ).fetchone()
            if updated is None:
                raise RuntimeError("invalid or already settled spend reservation")
        if updated[0] == "bound_exceeded":
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
