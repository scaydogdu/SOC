import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

from soc.episode import load_runtime
from soc.ledger.models import (
    BriefChanged,
    ChunkBuilt,
    DecisionSubject,
    ExecutorRebound,
    Record,
    Ruling,
    RulingConflict,
    VersionChange,
    VersionRef,
)
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
            output_ref="outputs/chunk1/build-1.json",
        )
        with pytest.raises(ValueError, match="requires an ID"):
            ledger.record(OWNER, chunk)
        with pytest.raises(ValueError, match="existing ledger version"):
            ledger.record(
                OWNER, chunk.model_copy(update={"ledger_version": 99}), chunk_id="bad"
            )
        ledger.record(OWNER, chunk, chunk_id="chunk1")
        with pytest.raises(ValueError, match="expected 0, found 1"):
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


def test_chunk_rebuild_replaces_current_usage_and_preserves_history(tmp_path):
    path = tmp_path / "rebuild.sqlite"
    first = ChunkBuilt(
        ledger_version=1,
        uses=[VersionRef(commitment_id=IDENTITY.id, version=1)],
        decisions={IDENTITY.id: RULING.rendering},
        output_ref="outputs/chunk1/build-1.json",
    )
    updated_ruling = RULING.model_copy(update={"rendering": "updated"})
    second = ChunkBuilt(
        ledger_version=3,
        uses=[VersionRef(commitment_id=IDENTITY.id, version=2)],
        decisions={IDENTITY.id: updated_ruling.rendering},
        build_number=2,
        output_ref="outputs/chunk1/build-2.json",
    )
    with Ledger(path, "toy") as ledger:
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        ledger.record(OWNER, first, chunk_id="chunk1")
        ledger.transition("Revised", OWNER, IDENTITY.id, 1, updated_ruling, "update")
        before = ledger.snapshot()
        with pytest.raises(ValueError, match="advance by one"):
            ledger.record(OWNER, first, chunk_id="chunk1", base_build=1)
        with pytest.raises(ValueError, match="requires an output reference"):
            ledger.record(
                OWNER,
                second.model_copy(update={"output_ref": None}),
                chunk_id="chunk1",
                base_build=1,
            )
        with pytest.raises(ValueError, match="inactive"):
            ledger.record(
                OWNER,
                second.model_copy(update={"uses": first.uses}),
                chunk_id="chunk1",
                base_build=1,
            )
        assert ledger.snapshot() == before
        event = ledger.record(OWNER, second, chunk_id="chunk1", base_build=1)
        assert event.payload["@graph"][0]["buildNumber"] == 2
        assert len(event.payload["@graph"][0]["used"]) == 1
        assert ledger.snapshot().usage == {(IDENTITY.id, 2): ["chunk1"]}

    with Ledger(path, "toy") as ledger:
        view = ledger.snapshot()
        assert view.chunks == {"chunk1": second}
        assert view.chunk_history == {"chunk1": [first, second]}
        assert view.usage == {(IDENTITY.id, 2): ["chunk1"]}
        assert view == ledger.rebuild()
        assert ledger.verify() is None


def test_two_repairs_cannot_replace_the_same_legacy_build(tmp_path):
    class LegacyChunk(Record):
        ledger_version: int = 0
        uses: list[VersionRef] = []
        decisions: dict[str, str] = {}

    path = tmp_path / "race.sqlite"
    with Ledger(path, "toy") as ledger, ledger._db:
        ledger._db.execute("BEGIN IMMEDIATE")
        legacy = ledger._append("ChunkBuilt", OWNER, LegacyChunk(), chunk_id="chunk1")

    start = Barrier(2)

    def repair(output_ref):
        with Ledger(path, "toy") as ledger:
            previous = ledger.snapshot().chunks["chunk1"]
            assert previous.build_number == 1 and previous.output_ref is None
            build = ChunkBuilt(
                ledger_version=1,
                uses=[],
                decisions={},
                build_number=2,
                output_ref=output_ref,
            )
            start.wait(timeout=10)
            try:
                ledger.record(OWNER, build, chunk_id="chunk1", base_build=1)
            except ValueError as error:
                assert str(error) == "chunk build changed: expected 1, found 2"
                return None
            return build

    with ThreadPoolExecutor(2) as workers:
        accepted = [
            build for build in workers.map(repair, ["repair-a", "repair-b"]) if build
        ]
    assert len(accepted) == 1
    with Ledger(path, "toy") as ledger:
        history = ledger.snapshot().chunk_history["chunk1"]
        assert history[0].output_ref is None and history[1:] == accepted
        assert ledger._events()[0] == legacy
        assert ledger.verify() is None


def test_rulings_conflict_by_subject_and_site_within_the_operation(tmp_path):
    first_site, second_site = "9999-1-s01", "9999-2-s01"
    subject = DecisionSubject(kind="term", key="access token")
    identity = IDENTITY.model_copy(update={"subject": subject})
    broad = RULING.model_copy(update={"sentence_ids": [first_site, second_site]})
    first = broad.model_copy(update={"sentence_ids": [first_site]})
    second = broad.model_copy(update={"sentence_ids": [second_site]})
    alternate = first.model_copy(update={"scope": ["client"], "rendering": "alternate"})
    episode = load_runtime(Path("data/episodes/toy"))
    with Ledger(tmp_path / "rulings.sqlite", "toy", episode=episode) as ledger:
        with pytest.raises(ValueError, match="requires sentence use sites"):
            ledger.ratify(OWNER, identity, 0, RULING, "missing sites")
        with pytest.raises(ValueError, match="outside the ruling's scope"):
            ledger.ratify(
                OWNER,
                identity,
                0,
                first.model_copy(update={"sentence_ids": ["9999-1-s02"]}),
                "inapplicable site",
            )
        assert ledger.ratify(OWNER, identity, 0, first, "first").type == "Ratified"
        disjoint = identity.model_copy(update={"id": "disjoint"})
        assert ledger.ratify(OWNER, disjoint, 0, second, "exception").type == "Ratified"
        numeric = identity.model_copy(
            update={
                "id": "numeric",
                "subject": DecisionSubject(kind="numeric", key=subject.key),
            }
        )
        assert (
            ledger.ratify(OWNER, numeric, 0, first, "another kind").type == "Ratified"
        )
        other_operation = identity.model_copy(
            update={"id": "separate", "operation": "separate"}
        )
        assert (
            ledger.ratify(
                OWNER, other_operation, 0, alternate, "another operation"
            ).type
            == "Ratified"
        )

        candidate = identity.model_copy(update={"id": "candidate"})
        blocked = ledger.ratify(
            OWNER,
            candidate,
            0,
            alternate.model_copy(update={"sentence_ids": [second_site, first_site]}),
            "same sites using a different scope key",
        )
        assert blocked.type == "ConflictRaised" and blocked.new_version is None
        conflict = RulingConflict.model_validate(blocked.payload["data"])
        assert conflict.candidate_id == candidate.id and conflict.subject == subject
        assert [
            (item.commitment_id, item.version, item.sentence_ids)
            for item in conflict.overlaps
        ] == [(identity.id, 1, [first_site]), (disjoint.id, 1, [second_site])]
        assert candidate.id not in ledger.snapshot().current
        assert (
            ledger.transition("Revised", OWNER, identity.id, 1, broad, "widen").type
            == "ConflictRaised"
        )
        assert ledger.snapshot().current[identity.id].version == 1
        assert set(
            ledger.reader().in_scope("access token", sentence_id=second_site)
        ) == {disjoint.id}
        assert ledger.verify() is None


def test_concurrent_rulings_leave_one_binding_then_can_resolve(tmp_path):
    path = tmp_path / "overlap.sqlite"
    subject = DecisionSubject(kind="term", key="access token")
    identities = [
        IDENTITY.model_copy(update={"id": key, "subject": subject})
        for key in ("a", "b")
    ]
    first_site, second_site = "9999-1-s01", "9999-2-s01"
    ruling = RULING.model_copy(update={"sentence_ids": [first_site, second_site]})
    start = Barrier(2)
    with Ledger(path, "toy"):
        pass

    def attempt(identity):
        with Ledger(path, "toy") as ledger:
            start.wait(timeout=10)
            return ledger.ratify(OWNER, identity, 0, ruling, "concurrent")

    with ThreadPoolExecutor(2) as workers:
        events = list(workers.map(attempt, identities))
    assert sorted(event.type for event in events) == ["ConflictRaised", "Ratified"]
    with Ledger(path, "toy") as ledger:
        (winner,) = ledger.snapshot().current
        loser = next(identity for identity in identities if identity.id != winner)
        narrowed = ruling.model_copy(update={"sentence_ids": [first_site]})
        ledger.transition("Revised", OWNER, winner, 1, narrowed, "restrict the scope")
        assert not ledger.is_tombstoned(
            ruling.rendering, "access token", sentence_id=first_site
        )
        assert ledger.is_tombstoned(
            ruling.rendering, "access token", sentence_id=second_site
        )
        replacement = ruling.model_copy(
            update={"sentence_ids": [second_site], "rendering": "new"}
        )
        assert (
            ledger.ratify(OWNER, loser, 0, replacement, "disjoint exception").type
            == "Ratified"
        )
        assert set(
            ledger.reader().in_scope("access token", sentence_id=second_site)
        ) == {loser.id}
        assert ledger.rebuild() == ledger.snapshot()
        assert ledger.verify() is None


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


def test_revision_graph_keeps_commitment_roles_and_prov_links(tmp_path):
    revised_ruling = RULING.model_copy(
        update={"scope": ["access token", "session"], "rendering": "replacement"}
    )
    with Ledger(tmp_path / "revision.sqlite", "toy") as ledger:
        initial = ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        revision = ledger.transition(
            "Revised", OWNER, IDENTITY.id, 1, revised_ruling, "new ruling"
        )
        third = ledger.ratify(OWNER, IDENTITY, 2, revised_ruling, "ratify again")

    initial_activity = initial.payload["@graph"][0]
    activity = revision.payload["@graph"][0]
    assert initial_activity["generated"] == activity["used"]
    assert "used" not in initial_activity
    assert "antecedent" not in activity and "consequent" not in activity
    new_id = activity["generated"]
    new_version = next(
        node for node in revision.payload["@graph"] if node["@id"] == new_id
    )
    assert new_version["antecedent"] == revised_ruling.scope
    assert new_version["consequent"] == revised_ruling.rendering
    assert new_version["wasRevisionOf"] == activity["used"]
    assert third.payload["@graph"][0]["used"] == new_id
    closed_version = next(
        node for node in third.payload["@graph"] if node["@id"] == new_id
    )
    assert closed_version["wasRevisionOf"] == activity["used"]


def test_episode_citations_are_checked_inside_write(tmp_path):
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
        initial = ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        change = ledger.record(
            OWNER,
            BriefChanged(clauses={"B1": "replacement clause", "B2": "later clause"}),
        )
        evidence = RULING.model_copy(
            update={"evidence": ["brief:B1", "brief:B2", "commitment:D1"]}
        )
        supported = ledger.ratify(
            OWNER, IDENTITY.model_copy(update={"id": "D2"}), 0, evidence, "supported"
        )
        (before,) = VersionChange.model_validate(
            initial.payload["data"]
        ).brief_citations
        assert (before.clause_id, before.text, before.ledger_version) == (
            "B1",
            episode.brief.clauses[0].text,
            0,
        )
        resolved = VersionChange.model_validate(
            supported.payload["data"]
        ).brief_citations
        assert [
            (item.clause_id, item.text, item.ledger_version) for item in resolved
        ] == [
            ("B1", "replacement clause", change.seq),
            ("B2", "later clause", change.seq),
        ]
        version = next(
            node
            for node in supported.payload["@graph"]
            if node["@id"] == supported.payload["@graph"][0]["generated"]
        )
        assert [item["clauseText"] for item in version["briefCitation"]] == [
            "replacement clause",
            "later clause",
        ]
        assert ledger.snapshot().ledger_version == 3
        assert ledger.verify() is None


def test_scope_narrowing_tombstones_only_the_lost_scope(tmp_path):
    with Ledger(tmp_path / "narrow.sqlite", "toy") as ledger:
        original = RULING.model_copy(update={"scope": ["sec-A", "sec-B"]})
        ledger.ratify(OWNER, IDENTITY, 0, original, "initial")
        replacement = original.model_copy(update={"scope": ["sec-A"]})
        ledger.transition("Superseded", OWNER, IDENTITY.id, 1, replacement, "narrow")
        assert not ledger.is_tombstoned(original.rendering, "sec-A")
        assert ledger.is_tombstoned(original.rendering, "sec-B")
        assert [item.scope for item in ledger.snapshot().tombstones] == [["sec-B"]]


def test_revision_tombstones_old_rendering_until_readopted(tmp_path):
    with Ledger(tmp_path / "readopt.sqlite", "toy") as ledger:
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        replacement = RULING.model_copy(
            update={"rendering": "jeton", "state": "amended"}
        )
        ledger.transition("Revised", OWNER, IDENTITY.id, 1, replacement, "new term")
        assert ledger.is_tombstoned(RULING.rendering, "access token")
        ledger.transition(
            "Superseded", OWNER, IDENTITY.id, 2, RULING, "return to original"
        )
        assert not ledger.is_tombstoned(RULING.rendering, "access token")
        assert ledger.is_tombstoned(replacement.rendering, "access token")
        assert (
            ledger.reader().in_scope("access token", at=1)[IDENTITY.id].valid_until
            is None
        )


def test_other_current_commitment_can_keep_rendering_allowed(tmp_path):
    with Ledger(tmp_path / "overlap.sqlite", "toy") as ledger:
        other = IDENTITY.model_copy(update={"id": "D2"})
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        ledger.ratify(OWNER, other, 0, RULING, "overlap")
        replacement = RULING.model_copy(update={"rendering": "jeton"})
        ledger.ratify(OWNER, IDENTITY, 1, replacement, "new term")
        assert not ledger.is_tombstoned(RULING.rendering, "access token")
        revoked = RULING.model_copy(update={"state": "revoked"})
        ledger.transition("Revoked", OWNER, other.id, 1, revoked, "withdraw")
        assert ledger.is_tombstoned(RULING.rendering, "access token")


def test_failed_tombstone_insert_rolls_back_supersession(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite"
    with Ledger(path, "toy") as ledger:
        ledger.ratify(OWNER, IDENTITY, 0, RULING, "initial")
        append = ledger._append

        def fail_on_tombstone(kind, *args, **kwargs):
            if kind == "Tombstoned":
                with sqlite3.connect(path) as observer:
                    assert observer.execute(
                        "SELECT type FROM events ORDER BY seq"
                    ).fetchall() == [("Ratified",)]
                raise RuntimeError("tombstone append failed")
            return append(kind, *args, **kwargs)

        monkeypatch.setattr(ledger, "_append", fail_on_tombstone)
        replacement = RULING.model_copy(update={"rendering": "jeton"})
        with pytest.raises(RuntimeError, match="tombstone append failed"):
            ledger.transition(
                "Superseded", OWNER, IDENTITY.id, 1, replacement, "replace"
            )

    with Ledger(path, "toy") as reopened:
        assert reopened.snapshot().ledger_version == 1
        assert reopened.snapshot().current[IDENTITY.id].rendering == RULING.rendering
        assert reopened.verify() is None


def test_strength_defaults_and_round_trips_as_version_data(tmp_path):
    original = Ruling.model_validate(RULING.model_dump(exclude={"strength"}))
    assert original.strength == "must"
    with Ledger(tmp_path / "strength.sqlite", "toy") as ledger:
        for base, strength in enumerate(("must", "should", "may")):
            ruling = Ruling.model_validate(
                dict(original.model_dump(), strength=strength)
            )
            event = ledger.ratify(OWNER, IDENTITY, base, ruling, "set strength")
            assert event.payload["data"]["version"]["strength"] == strength
            version_node = next(
                node
                for node in event.payload["@graph"]
                if node.get("@type") == "CommitmentVersion"
                and node["version"] == base + 1
            )
            assert version_node["strength"] == strength

        replay = ledger.rebuild()
        assert ledger.snapshot() == replay
        assert [item.strength for item in replay.versions.values()] == [
            "must",
            "should",
            "may",
        ]
        assert replay.current[IDENTITY.id].consistency == original.consistency
        assert replay.current[IDENTITY.id].state == original.state
        assert not replay.tombstones
        assert ledger.verify() is None


@pytest.mark.parametrize("invalid", ["MUST", "required", None])
def test_ruling_rejects_invalid_strength(invalid):
    with pytest.raises(ValidationError, match="strength"):
        Ruling.model_validate(dict(RULING.model_dump(), strength=invalid))
