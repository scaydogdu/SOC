import multiprocessing
import sqlite3

from soc.ledger.models import VersionChange
from soc.ledger.store import Ledger
from tests.ledger_examples import IDENTITY, OWNER, RULING


def _writer(path, barrier, index):
    with Ledger(path, "concurrency") as ledger:
        for round_number in range(125):
            visible = ledger.snapshot().current.get(IDENTITY.id)
            base = visible.version if visible is not None else 0
            barrier.wait(timeout=30)
            reason = f"writer {index} attempt {round_number}"
            event = ledger.ratify(OWNER, IDENTITY, base, RULING, reason)
            assert event.type in {"Ratified", "ConflictRaised"}
            barrier.wait(timeout=30)


def _kill_target(path, ready):
    with Ledger(path, "crash") as ledger:
        append = ledger._append

        def pause_after_insert(*args, **kwargs):
            event = append(*args, **kwargs)
            assert event.type == "Superseded" and ledger._db.in_transaction
            ready.set()
            # Stay inside the transaction, before the second append and commit.
            import time

            time.sleep(60)
            return event

        ledger._append = pause_after_insert
        ledger.transition(
            "Superseded",
            OWNER,
            IDENTITY.id,
            1,
            RULING.model_copy(update={"rendering": "replacement"}),
            "replace",
        )


def test_eight_writer_processes(tmp_path):
    path = tmp_path / "race.sqlite"
    with Ledger(path, "concurrency") as ledger:
        assert ledger.verify() is None
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(8)
    workers = [
        context.Process(target=_writer, args=(path, barrier, index))
        for index in range(8)
    ]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=50)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.kill()
                worker.join()

    with Ledger(path, "concurrency") as ledger:
        events = ledger._events()
        wins = [event for event in events if event.type == "Ratified"]
        losses = [event for event in events if event.type == "ConflictRaised"]
        assert len(events) == 1000 and len(wins) == 125 and len(losses) == 875
        assert [event.new_version for event in wins] == list(range(1, 126))
        assert len({event.event_id for event in events}) == 1000
        assert (
            len(
                {
                    VersionChange.model_validate(event.payload["data"]).reason
                    for event in wins
                }
            )
            == 125
        )
        for round_number in range(125):
            winner, *conflicts = events[round_number * 8 : (round_number + 1) * 8]
            assert winner.type == "Ratified" and winner.base_version == round_number
            assert all(
                event.type == "ConflictRaised"
                and event.payload["data"]
                == {"expected": round_number, "actual": round_number + 1}
                for event in conflicts
            )
        assert ledger.verify() is None
        assert ledger.snapshot() == ledger.rebuild()
    print(
        "8 processes, 1,000 attempts, 125 ratifications, 875 conflicts, chain and replay verified"
    )


def test_killed_mid_transaction_then_reopen(tmp_path):
    path = tmp_path / "crash.sqlite"
    with Ledger(path, "crash") as ledger:
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "first")
        before = ledger.snapshot()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    writer = context.Process(target=_kill_target, args=(path, ready))
    writer.start()
    try:
        assert ready.wait(timeout=20)
        with sqlite3.connect(path) as conn:
            assert conn.execute("SELECT count(*) FROM events").fetchone() == (1,)
    finally:
        writer.kill()
        writer.join(timeout=10)
    assert writer.exitcode == -9
    with Ledger(path, "crash") as ledger:
        assert ledger.verify() is None
        assert ledger.snapshot() == before
        assert not ledger.is_tombstoned(RULING.rendering, "access token")
        event = ledger.transition(
            "Superseded",
            OWNER,
            IDENTITY.id,
            1,
            RULING.model_copy(update={"rendering": "replacement"}),
            "retry",
        )
        assert event.seq == 2
        assert ledger.snapshot().ledger_version == 3
        assert ledger.verify() is None
        assert ledger.is_tombstoned(RULING.rendering, "access token")
