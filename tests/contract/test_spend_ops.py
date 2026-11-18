import gzip
import hashlib
import json
import sqlite3
from decimal import Decimal

import pytest

from soc.orchestrator.spend_ops import spend_main, spend_status
from soc.providers.models import Outcome, Reply
from soc.providers.spend import SpendGuard, SpendRate

RATE = SpendRate(
    max_input_tokens=1000,
    max_output_tokens=1000,
    input_per_million=1000,
    output_per_million=1000,
)


def test_spend_status_on_legacy_database_is_read_only(tmp_path):
    path = tmp_path / "spend.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE cap (id INTEGER PRIMARY KEY, amount INTEGER);
            INSERT INTO cap VALUES (1, 40000000000);
            CREATE TABLE charges (id INTEGER PRIMARY KEY, run_id TEXT, call_id TEXT,
                model TEXT, amount INTEGER, state TEXT);
            INSERT INTO charges VALUES (1, 'old-run', 'probe-low', 'gemini', 100000000, 'settled');
            INSERT INTO charges VALUES (2, 'old-run', 'probe-low', 'gemini', 900000000, 'reserved');
            INSERT INTO charges VALUES (3, 'old-run', 'no-log', 'gemini', 400000000, 'reserved');
        """)
    probes = tmp_path / "probes"
    probes.mkdir()
    with gzip.open(probes / "calls.jsonl.gz", "wt") as stream:
        for source, attempt, outcome in [
            ("provider", 1, "ok"),
            ("provider", 2, "rate_limited"),
            ("cache", None, "ok"),
        ]:
            stream.write(
                json.dumps(
                    dict(
                        source=source,
                        attempt=attempt,
                        outcome=outcome,
                        run_id="old-run",
                        call_id="probe-low",
                    )
                )
                + "\n"
            )
    before = hashlib.sha256(path.read_bytes()).digest()
    report = spend_status(tmp_path)
    assert report["cap_usd"] == 40 and report["settled_usd"] == 0.1
    assert [r["outcome"] for r in report["open_reservations"]] == [
        "rate_limited",
        "unknown",
    ]
    assert hashlib.sha256(path.read_bytes()).digest() == before


def test_spend_operator_actions_are_atomic_audited_and_require_matching_cap(
    tmp_path, capsys
):
    path = tmp_path / "spend.sqlite"
    guard = SpendGuard(path, Decimal("2"), {"m": RATE})
    first = guard.reserve("run", "first", "m", input_bound=300, output_bound=400)
    second = guard.reserve("run", "second", "m", input_bound=300, output_bound=400)
    guard.settle(first, "m", Reply(outcome=Outcome.RATE_LIMITED))
    assert spend_status(tmp_path)["open_reservations"][0]["outcome"] == "rate_limited"

    def run(
        command, usd, *params, reason="confirmed in billing console", approver="Can"
    ):
        return spend_main(
            [
                command,
                "--output",
                str(tmp_path),
                "--usd",
                usd,
                "--reason",
                reason,
                "--approved-by",
                approver,
                *params,
            ]
        )

    with pytest.raises(SystemExit):
        run("set-cap", "1.2")
    assert "cover settled plus reserved" in capsys.readouterr().err
    assert run("settle", "0", "--reservation", str(first)) == 0
    assert json.loads(capsys.readouterr().out)["reserved_usd"] == 0.7
    with pytest.raises(SystemExit):
        run("settle", "0", "--reservation", str(first))
    assert "reservation is not open" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run("settle", "0", "--reservation", str(second), reason=" ")
    assert "require a reason and approver" in capsys.readouterr().err
    assert run("set-cap", "3.25") == 0
    assert json.loads(capsys.readouterr().out)["cap_usd"] == 3.25
    with pytest.raises(ValueError, match="spend set-cap"):
        guard.reserve("run", "third", "m", input_bound=100, output_bound=100)
    with pytest.raises(ValueError, match="spend set-cap"):
        SpendGuard(path, Decimal("2"), {"m": RATE})
    SpendGuard(path, Decimal("3.25"), {"m": RATE})

    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT action, reservation, before_nanos, after_nanos, reason, approved_by "
            "FROM operator_actions ORDER BY id"
        ).fetchall()
        assert rows == [
            ("settle", first, 700000000, 0, "confirmed in billing console", "Can"),
            (
                "set-cap",
                None,
                2000000000,
                3250000000,
                "confirmed in billing console",
                "Can",
            ),
        ]
        for statement in (
            "UPDATE operator_actions SET approved_by = 'x'",
            "DELETE FROM operator_actions",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(statement)


def test_spend_status_refuses_missing_campaign_without_creating_one(tmp_path, capsys):
    missing = tmp_path / "missing"
    with pytest.raises(SystemExit):
        spend_main(["status", "--output", str(missing)])
    assert "unable to open" in capsys.readouterr().err
    assert not missing.exists()
