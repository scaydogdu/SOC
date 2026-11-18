"""Inspect and explicitly reconcile one existing campaign spend ledger."""

import argparse
import gzip
import json
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Literal

from soc.providers.client import configured_credentials, ensure_no_credentials
from soc.providers.spend import _NANOS


def _connect(campaign: Path, *, mode: Literal["ro", "rw"]) -> sqlite3.Connection:
    uri = (campaign / "spend.sqlite").resolve().as_uri() + f"?mode={mode}"
    return sqlite3.connect(uri, uri=True)


def _legacy_outcomes(campaign: Path) -> dict[tuple[str, str], list[str]]:
    outcomes: dict[tuple[str, str], list[str]] = defaultdict(list)
    for path in sorted(campaign.glob("*/calls.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row["source"] == "provider" and row["attempt"] is not None:
                    outcomes[(row["run_id"], row["call_id"])].append(row["outcome"])
    return outcomes


def spend_status(campaign: Path) -> dict[str, object]:
    with closing(_connect(campaign, mode="ro")) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        (cap,) = db.execute("SELECT amount FROM cap WHERE id = 1").fetchone()
        charges = [dict(row) for row in db.execute("SELECT * FROM charges ORDER BY id")]
    grouped: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in charges:
        grouped[(row["run_id"], row["call_id"])].append(row)
    if any(
        row["state"] == "reserved" and row.get("outcome") is None for row in charges
    ):
        for key, outcomes in _legacy_outcomes(campaign).items():
            rows = grouped.get(key, [])
            if len(rows) == len(outcomes):
                for row, outcome in zip(rows, outcomes, strict=True):
                    if row.get("outcome") is None:
                        row["outcome"] = outcome
    return {
        "cap_usd": cap / _NANOS,
        "settled_usd": sum(
            row["amount"] for row in charges if row["state"] != "reserved"
        )
        / _NANOS,
        "reserved_usd": sum(
            row["amount"] for row in charges if row["state"] == "reserved"
        )
        / _NANOS,
        "open_reservations": [
            {
                "id": row["id"],
                "model": row["model"],
                "run_id": row["run_id"],
                "call_id": row["call_id"],
                "outcome": row.get("outcome") or "unknown",
                "amount_usd": row["amount"] / _NANOS,
            }
            for row in charges
            if row["state"] == "reserved"
        ],
        "bound_exceeded": any(row["state"] == "bound_exceeded" for row in charges),
    }


def _amount(usd: Decimal) -> int:
    if (
        not usd.is_finite()
        or not 0 <= usd <= 100_000
        or usd != usd.quantize(Decimal("0.000000001"))
    ):
        raise ValueError(
            "USD amount must be finite, between 0 and 100000, with at most 9 decimal places"
        )
    return int(usd * _NANOS)


def _apply_action(
    campaign: Path,
    action: Literal["settle", "set-cap"],
    usd: Decimal,
    *,
    reason: str,
    approved_by: str,
    reservation: int | None = None,
) -> None:
    reason, approved_by = reason.strip(), approved_by.strip()
    if not reason or not approved_by:
        raise ValueError("operator actions require a reason and approver")
    ensure_no_credentials(configured_credentials(), reason, approved_by)
    amount = _amount(usd)
    with closing(_connect(campaign, mode="rw")) as db, db:
        db.execute("BEGIN IMMEDIATE")
        if action == "settle":
            row = db.execute(
                "SELECT amount FROM charges WHERE id = ? AND state = 'reserved'",
                (reservation,),
            ).fetchone()
            if row is None:
                raise ValueError("reservation is not open")
            (before,) = row
            db.execute(
                "UPDATE charges SET amount = ?, state = 'settled' WHERE id = ?",
                (amount, reservation),
            )
        else:
            (before,) = db.execute("SELECT amount FROM cap WHERE id = 1").fetchone()
            (committed,) = db.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM charges"
            ).fetchone()
            if amount <= 0 or amount < committed:
                raise ValueError(
                    "new cap must be positive and cover settled plus reserved spend"
                )
            db.execute("UPDATE cap SET amount = ? WHERE id = 1", (amount,))
        db.execute(
            "CREATE TABLE IF NOT EXISTS operator_actions ("
            "id INTEGER PRIMARY KEY, time TEXT NOT NULL, action TEXT NOT NULL, "
            "reservation INTEGER, before_nanos INTEGER NOT NULL, after_nanos INTEGER NOT NULL, "
            "reason TEXT NOT NULL, approved_by TEXT NOT NULL)"
        )
        for verb in ("UPDATE", "DELETE"):
            db.execute(
                f"CREATE TRIGGER IF NOT EXISTS operator_no_{verb.lower()} BEFORE {verb} ON operator_actions "
                "BEGIN SELECT RAISE(ABORT, 'operator actions are append-only'); END"
            )
        db.execute(
            "INSERT INTO operator_actions (time, action, reservation, before_nanos, after_nanos, reason, approved_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                datetime.now(UTC).isoformat(),
                action,
                reservation,
                before,
                amount,
                reason,
                approved_by,
            ),
        )


def spend_main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m soc.orchestrator.live spend", description=__doc__
    )
    actions = parser.add_subparsers(dest="action", required=True)
    for command in ("status", "settle", "set-cap"):
        action = actions.add_parser(command)
        action.add_argument(
            "--output", type=Path, required=True, help="Existing campaign directory"
        )
        if command != "status":
            action.add_argument("--usd", required=True)
            action.add_argument("--reason", required=True)
            action.add_argument("--approved-by", required=True)
        if command == "settle":
            action.add_argument("--reservation", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if args.action != "status":
            _apply_action(
                args.output,
                args.action,
                Decimal(args.usd),
                reason=args.reason,
                approved_by=args.approved_by,
                reservation=getattr(args, "reservation", None),
            )
        print(json.dumps(spend_status(args.output), indent=2))
    except (InvalidOperation, OSError, sqlite3.Error, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")
    return 0
