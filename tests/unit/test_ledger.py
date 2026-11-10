import sqlite3

import pytest

from soc.ledger.models import BriefChanged, ChunkBuilt, ExecutorRebound, VersionRef
from soc.ledger.schema.validation import ShapeError
from soc.ledger.store import AuthorityError, Ledger
from tests.ledger_examples import IDENTITY, OTHER, OWNER, RULING


def test_authority_and_schema_fail_inside_transaction(tmp_path):
    path = tmp_path / "authority.sqlite"
    with Ledger(path, "toy") as ledger:
        with pytest.raises(ShapeError, match="^RatifiedQuoteShape$"):
            ledger.ratify(
                OWNER,
                IDENTITY,
                0,
                RULING.model_copy(update={"source_quote": None}),
                "invalid",
            )
        with pytest.raises(ShapeError, match="^PrincipalBriefChangeShape$"):
            ledger.record(OTHER, BriefChanged(clauses={"B1": "invalid"}))
        assert ledger.snapshot().ledger_version == 0
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        with pytest.raises(AuthorityError, match="owner"):
            ledger.transition("Revised", OTHER, IDENTITY.id, 1, RULING, "unauthorized")
        with pytest.raises(AuthorityError, match="owner"):
            ledger.ratify(OTHER, IDENTITY, 1, RULING, "also unauthorized")
        with pytest.raises(ValueError, match="immutable"):
            ledger.ratify(
                OWNER,
                IDENTITY.model_copy(update={"owner": OTHER.agent}),
                1,
                RULING,
                "transfer",
            )
        assert ledger.snapshot().ledger_version == 1
        ledger.record(OWNER, BriefChanged(clauses={"B1": "valid"}))
        assert ledger.verify() is None


def test_monotonic_reads_usage_and_rebind(tmp_path):
    with Ledger(tmp_path / "read.sqlite", "toy") as ledger:
        reader = ledger.reader()
        assert reader.in_scope("access token", at=0) == {}
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "first")
        initial = reader.in_scope("access token", at=1)
        assert initial[IDENTITY.id].rendering == RULING.rendering
        assert not reader.in_scope("unrelated", at=1)
        with pytest.raises(ValueError, match="go backwards"):
            reader.in_scope("access token", at=0)
        with pytest.raises(ValueError, match="available"):
            reader.in_scope("access token", at=9)
        chunk = ChunkBuilt(
            ledger_version=1,
            uses=[VersionRef(commitment_id=IDENTITY.id, version=1)],
            decisions={IDENTITY.id: RULING.rendering},
        )
        with pytest.raises(ValueError, match="requires an ID"):
            ledger.record(OWNER, chunk)
        with pytest.raises(ValueError, match="existing ledger version"):
            ledger.record(
                OWNER, chunk.model_copy(update={"ledger_version": 99}), chunk_id="bad"
            )
        ledger.record(OWNER, chunk, chunk_id="chunk1")
        with pytest.raises(ValueError, match="already built"):
            ledger.record(OWNER, chunk, chunk_id="chunk1")
        rebind = ExecutorRebound(old_model="old", new_model="new", ledger_version=2)
        before = ledger.snapshot()
        with pytest.raises(ValueError, match="current ledger"):
            ledger.record(OWNER, rebind.model_copy(update={"ledger_version": 0}))
        ledger.record(OWNER, rebind)
        rebound = ledger.snapshot()
        assert rebound.versions == before.versions and rebound.current == before.current
        assert rebound.executor == rebind
        assert rebound.usage == {(IDENTITY.id, 1): ["chunk1"]}
        assert rebound.chunks == {"chunk1": chunk}
        ledger.transition(
            "Superseded",
            OWNER,
            IDENTITY.id,
            1,
            RULING.model_copy(update={"rendering": "new"}),
            "change",
        )
        assert reader.in_scope("access token", at=3)[IDENTITY.id].version == 1
        assert reader.in_scope("access token")[IDENTITY.id].rendering == "new"
        assert ledger.snapshot().versions[IDENTITY.id, 1].valid_until == 4
        assert ledger.is_tombstoned(RULING.rendering, "access token")
        assert not ledger.is_tombstoned(RULING.rendering, "unrelated")
        with pytest.raises(ValueError, match="inactive"):
            ledger.record(
                OWNER, chunk.model_copy(update={"ledger_version": 4}), chunk_id="stale"
            )
        ledger.record(OWNER, chunk, chunk_id="historical")
        assert ledger.snapshot() == ledger.rebuild()


def test_sql_guards_and_first_altered_byte(tmp_path):
    path = tmp_path / "tamper.sqlite"
    with Ledger(path, "toy") as ledger:
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        ledger.record(OWNER, BriefChanged(clauses={"B1": "updated"}))
        assert ledger.verify() is None
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA journal_mode").fetchone() == ("wal",)
            for sql in (
                "UPDATE events SET time = 'x' WHERE seq = 1",
                "DELETE FROM events",
            ):
                with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                    conn.execute(sql)
            copy = """
                INSERT OR REPLACE INTO events
                SELECT 3, ?, run_id, time, type, actor, commitment_id, ?, ?, 3,
                       chunk_id, payload, prev_hash, hash FROM events WHERE seq = 1
            """
            for base, new in [(0, 1), (4, 5)]:
                with pytest.raises(
                    sqlite3.IntegrityError, match="version must advance"
                ):
                    conn.execute(copy, ("different id", base, new))
            with pytest.raises(sqlite3.IntegrityError, match="event identity"):
                conn.execute(copy, (ledger._events()[0].event_id, 1, 2))
            conn.execute("DROP TRIGGER event_no_update")
            conn.execute(
                "UPDATE events SET time = 'x' || substr(time, 2) WHERE seq = 2"
            )
        assert ledger.verify() == 2
    with pytest.raises(ValueError, match="another run"):
        Ledger(path, "wrong")


def test_invalid_transitions_and_revoke(tmp_path):
    with pytest.raises(ValueError, match="non-empty"):
        Ledger(tmp_path / "bad.sqlite", " ")
    with Ledger(tmp_path / "states.sqlite", "toy") as ledger:
        with pytest.raises(ValueError, match="nonnegative"):
            ledger.ratify(OWNER, IDENTITY, -1, RULING, "invalid base")
        with pytest.raises(ValueError, match="ratify"):
            ledger.transition("Revoked", OWNER, IDENTITY.id, 0, RULING, "missing")
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        with pytest.raises(ValueError, match="requires state"):
            ledger.transition("Revoked", OWNER, IDENTITY.id, 1, RULING, "wrong state")
        ledger.transition(
            "Revoked",
            OWNER,
            IDENTITY.id,
            1,
            RULING.model_copy(update={"state": "revoked"}),
            "revoke",
        )
        assert ledger.reader().in_scope("access token") == {}
        assert ledger.snapshot().current[IDENTITY.id].state == "revoked"
        assert ledger.snapshot().versions[IDENTITY.id, 1].valid_until == 2


def test_byte_change_to_stored_json_is_detected(tmp_path):
    path = tmp_path / "altered.sqlite"
    with Ledger(path, "toy") as ledger:
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TRIGGER event_no_update")
            conn.execute("UPDATE events SET payload = ' ' || payload WHERE seq = 1")
        assert ledger.verify() == 1


def test_low_level_version_append_enforces_owner(tmp_path):
    from soc.ledger.models import VersionChange

    with Ledger(tmp_path / "protected.sqlite", "toy") as ledger:
        first = ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        original = VersionChange.model_validate(first.payload["data"])
        replacement = original.model_copy(
            update={
                "version": original.version.model_copy(
                    update={"version": 2, "valid_from": 2}
                )
            }
        )
        with pytest.raises(RuntimeError, match="write transaction"):
            ledger._append("Revised", OTHER, replacement, commitment_id=IDENTITY.id)
        with pytest.raises(AuthorityError, match="owner"), ledger._db:
            ledger._db.execute("BEGIN IMMEDIATE")
            ledger._append(
                "Revised",
                OTHER,
                replacement,
                commitment_id=IDENTITY.id,
                base_version=1,
                new_version=2,
            )
        assert ledger.snapshot().ledger_version == 1


def test_executor_epoch_closes_without_touching_rulings(tmp_path):
    with Ledger(tmp_path / "epochs.sqlite", "toy") as ledger:
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        before = ledger.snapshot().versions
        first = ledger.record(
            OWNER, ExecutorRebound(old_model="a", new_model="b", ledger_version=1)
        )
        with pytest.raises(ValueError, match="current executor"):
            ledger.record(
                OWNER, ExecutorRebound(old_model="a", new_model="c", ledger_version=2)
            )
        second = ledger.record(
            OWNER, ExecutorRebound(old_model="b", new_model="c", ledger_version=2)
        )

        def intervals(event):
            return [
                (node["validFrom"], node.get("validUntil"))
                for node in event.payload["@graph"]
                if node.get("@type") == "ExecutorEpoch"
            ]

        assert intervals(first) == [(0, 2), (2, None)]
        assert intervals(second) == [(2, 3), (3, None)]
        assert ledger.snapshot().versions == before


def test_episode_citations_are_checked_inside_write(tmp_path):
    from pathlib import Path

    from soc.episode import load_runtime
    from soc.ledger.models import Quote

    episode = load_runtime(Path("data/episodes/toy"))
    with Ledger(tmp_path / "citations.sqlite", "toy", episode=episode) as ledger:
        with pytest.raises(ValueError, match="verbatim"):
            ledger.ratify(
                OWNER,
                IDENTITY,
                0,
                RULING.model_copy(
                    update={
                        "source_quote": Quote(
                            sentence_id="9999-1-s01", text="invented quote"
                        )
                    }
                ),
                "invalid",
            )
        for reference, message in [
            ("source:missing", "unknown source"),
            ("brief:B2", "unknown brief"),
            ("commitment:missing", "unknown commitment"),
        ]:
            with pytest.raises(ValueError, match=message):
                ledger.ratify(
                    OWNER,
                    IDENTITY,
                    0,
                    RULING.model_copy(update={"evidence": [reference]}),
                    "invalid",
                )
        assert ledger.snapshot().ledger_version == 0
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        ledger.record(OWNER, BriefChanged(clauses={"B2": "later clause"}))
        evidence = RULING.model_copy(update={"evidence": ["brief:B2", "commitment:D1"]})
        ledger.ratify(
            OWNER, IDENTITY.model_copy(update={"id": "D2"}), 0, evidence, "supported"
        )
        assert ledger.snapshot().ledger_version == 3
