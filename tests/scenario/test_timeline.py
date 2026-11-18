import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from analysis.timeline import export_data, main
from soc.enforce.checker import Checker
from soc.ledger import store
from soc.ledger.models import (
    Actor,
    ChunkBuilt,
    DecisionSubject,
    DecisionUse,
    FlagRaised,
    GateDecision,
    OperationAuthority,
    RulingApproval,
    RulingBallot,
    StaleDetected,
    StaleMarked,
    Tombstone,
    VersionRef,
    VoteCast,
)
from soc.orchestrator.ledger import open_run_ledger
from soc.orchestrator.ledger_runner import run_ledger_episode
from soc.orchestrator.runner import ExperimentSettings
from soc.principal.simulator import PrincipalSimulator
from tests.ledger_examples import IDENTITY, RULING
from tests.scenario.test_ledger_runner import SPEC, TOY, _worker_output

ROOT = Path(__file__).resolve().parents[2]
GOLDEN = ROOT / "tests" / "fixtures" / "commitment-timeline.json"
CLIENT = Actor(agent="client", model="simulator", role="principal")
AUTHORITY = OperationAuthority(operation="toy", principal=CLIENT.agent)
WORKER = Actor(agent="worker", model="script", role="worker")
SITE = "9999-1-s01"


def create_toy_run(directory, monkeypatch):
    """Create the scheduled scripted run with stable persisted event IDs and times."""
    counter = iter(range(1, 1000))

    class FixedTime:
        @staticmethod
        def now(_tz):
            return datetime(2026, 11, 18, tzinfo=UTC)

    monkeypatch.setattr(store, "datetime", FixedTime)
    monkeypatch.setattr(
        store, "uuid4", lambda: f"00000000-0000-4000-8000-{next(counter):012d}"
    )
    directory.mkdir()
    key = directory / "principal.yaml"
    key.write_text("answers: {}\n")
    principal = PrincipalSimulator(
        key, directory / "answers.sqlite", episode_id="toy", actor=CLIENT
    )

    async def worker(_ledger, task):
        return _worker_output(task)

    spec = SPEC.model_copy(
        update={"condition": "scripted timeline", "code_commit": "timeline-fixture"}
    )
    result = asyncio.run(
        run_ledger_episode(
            TOY,
            directory,
            spec=spec,
            experiment=ExperimentSettings(),
            principal=principal,
            checker=Checker(),
            worker=worker,
        )
    )
    assert result.status == "completed"
    manifest = directory / "run.json"
    data = json.loads(manifest.read_text())
    manifest.write_text(json.dumps(data | {"scripted": True}))


@pytest.fixture
def scripted_run(tmp_path, monkeypatch):
    run = tmp_path / "scripted"
    create_toy_run(run, monkeypatch)
    return run


def test_timeline_scripted_toy_golden_round_trip_and_read_only(
    scripted_run, tmp_path, monkeypatch
):
    path = scripted_run / "ledger.sqlite"
    before = hashlib.sha256(path.read_bytes()).digest()
    connect = sqlite3.connect
    opened = []

    def readonly(database, **kwargs):
        opened.append(database)
        assert database.endswith("?mode=ro")
        assert kwargs["uri"] is True
        return connect(database, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", readonly)
    page, data = tmp_path / "timeline.html", tmp_path / "timeline.json"
    args = [str(scripted_run), str(TOY), "--out", str(page), "--json", str(data)]
    main(args)
    html, encoded = page.read_bytes(), data.read_bytes()
    main(args)
    assert len(opened) == 2
    assert hashlib.sha256(path.read_bytes()).digest() == before
    assert (
        html
        == page.read_bytes()
        == (ROOT / "docs/examples/commitment-timeline.html").read_bytes()
    )
    assert encoded == data.read_bytes() == GOLDEN.read_bytes()
    payload = html.split(b'<script type="application/json" id="data">', 1)[1].split(
        b"</script>", 1
    )[0]
    assert json.loads(payload) == json.loads(encoded)
    assert b"http://" not in html and b"https://" not in html
    exported = json.loads(encoded)
    assert exported["run"]["label"] == "Scripted fixture"
    events = exported["events"]
    assert exported["summary"] == {
        "decisions": len({e["commitment_id"] for e in events if e["new_version"]}),
        "chunks": 4,
        "events": len(events),
        "revisions": sum(e["type"] == "Revised" for e in events),
        "stale_builds": len(
            {
                (
                    chunk,
                    max(
                        b["seq"]
                        for b in events
                        if b["type"] == "ChunkBuilt"
                        and b["chunk_id"] == chunk
                        and b["seq"] < e["seq"]
                    ),
                )
                for e in events
                if e["type"] == "StaleMarked"
                for chunk in e["payload"]["data"]["chunk_ids"]
            }
        ),
        "gate_blocks": sum(
            e["type"] == "GateDecision" and not e["payload"]["data"]["accepted"]
            for e in events
        ),
        "final_stale": 0,
        "tombstones": sum(e["type"] == "Tombstoned" for e in events),
    }
    assert all("@graph" not in event["payload"] for event in events)


def _run_with_ruling(path, *, base=1, authority=AUTHORITY):
    return open_run_ledger(TOY, path, "case", authority=authority, chunk_id_base=base)


def _ratify(ledger):
    identity = IDENTITY.model_copy(
        update={
            "authority": "principal",
            "owner": CLIENT.agent,
            "subject": DecisionSubject(kind="term", key="access token"),
        }
    )
    original = RULING.model_copy(
        update={
            "ratifier": CLIENT.agent,
            "consistency": "SERIAL",
            "sentence_ids": [SITE],
        }
    )
    approval = RulingApproval(operation="toy", approvers=[CLIENT.agent])
    event = ledger.ratify(
        CLIENT, identity, 0, original, "first version", approval=approval
    )
    return original, approval, event.seq


def _build(ledger, sequence, version, number, chunk="chunk-1"):
    use = VersionRef(commitment_id=IDENTITY.id, version=version)
    return ledger.record(
        WORKER,
        ChunkBuilt(
            ledger_version=sequence,
            uses=[use],
            decisions={},
            use_sites=[DecisionUse(ref=use, sentence_id=SITE)],
            build_number=number,
            output_ref=f"build-{number}.json",
        ),
        chunk_id=chunk,
        base_build=number - 1,
    )


@pytest.mark.parametrize(
    ("stage", "statuses", "stale"),
    [
        ("evidence", ["current"], []),
        ("rendering", ["stale"], ["chunk-1"]),
        ("blocked", ["stale", "blocked"], ["chunk-1"]),
        ("repaired", ["stale", "blocked", "current"], []),
    ],
)
def test_timeline_cell_status_follows_recorded_build_history(
    tmp_path, stage, statuses, stale
):
    with _run_with_ruling(tmp_path / "ledger.sqlite") as (_, ledger):
        original, approval, observed = _ratify(ledger)
        old = _build(ledger, observed, 1, 1)
        assert (
            ledger.check_provisional_dependencies(WORKER, "chunk-1", build_number=1).seq
            > old.seq
        )
        update = {"evidence": [*original.evidence, "source:9999-1-s03"]}
        if stage != "evidence":
            update["rendering"] = "erişim belirteci"
        revised = ledger.transition(
            "Revised",
            CLIENT,
            IDENTITY.id,
            1,
            original.model_copy(update=update),
            "second version",
            approval=approval,
        )
        if stage in {"blocked", "repaired"}:
            _build(ledger, observed, 1, 2)
            ledger.check_provisional_dependencies(WORKER, "chunk-1", build_number=2)
        if stage == "repaired":
            _build(ledger, revised.seq, 2, 3)
            ledger.check_provisional_dependencies(WORKER, "chunk-1", build_number=3)
    data = export_data(tmp_path, TOY)
    builds = data["chunks"][0]["builds"]
    assert [item["statuses"][IDENTITY.id] for item in builds] == statuses
    assert data["final_stale"] == stale
    assert (
        data["commitments"][0]["versions"][0]["version"]["valid_until"] == revised.seq
    )
    assert (
        data["tombstones"] == []
        if stage == "evidence"
        else len(data["tombstones"]) == 1
    )


def _mutate(path, seq, **changes):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        db.execute("DROP TRIGGER event_no_update")
        row = store._unpack(
            db.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        )
        row.update(changes)
        row.pop("hash")
        db.execute(
            "UPDATE events SET type = ?, payload = ?, hash = ? WHERE seq = ?",
            (row["type"], store._canonical(row["payload"]), store._digest(row), seq),
        )


def test_timeline_rejects_corrupted_chain_and_mismatched_public_episode(
    tmp_path, capsys
):
    run = tmp_path / "run"
    run.mkdir()
    db = run / "ledger.sqlite"
    with _run_with_ruling(db) as (_, ledger):
        _ratify(ledger)
        ledger.record(CLIENT, store.BriefChanged(clauses={"B1": "new clause"}))
    page = tmp_path / "out.html"

    def args(episode):
        return [str(run), str(episode), "--out", str(page)]

    other = tmp_path / "other"
    other.mkdir()
    for name in ("source.jsonl", "brief.yaml", "events.yaml"):
        shutil.copyfile(TOY / name, other / name)
    (other / "brief.yaml").write_text('clauses: [{id: B1, text: "Different brief"}]\n')
    with pytest.raises(SystemExit) as failure:
        main(args(other))
    assert failure.value.code == 1
    assert "ledger sequence 1: public episode does not match" in capsys.readouterr().err
    assert not page.exists()

    # Change one byte in the retained event hash without changing any other cell.
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER event_no_update")
        conn.execute(
            "UPDATE events SET hash = CASE substr(hash, 1, 1) WHEN '0' THEN '1' ELSE '0' END || substr(hash, 2) WHERE seq=2"
        )
    with pytest.raises(SystemExit) as failure:
        main(args(TOY))
    assert failure.value.code == 1
    assert (
        "ledger sequence 2: hash chain verification failed" in capsys.readouterr().err
    )
    assert not page.exists()


def test_timeline_keeps_unknown_and_unplaced_events_and_escapes_embedded_data(tmp_path):
    path = tmp_path / "ledger.sqlite"
    with _run_with_ruling(path, base=0) as (_, ledger):
        with ledger._db:
            ledger._db.execute("BEGIN IMMEDIATE")
            gate = ledger._append(
                "GateDecision",
                WORKER,
                GateDecision(accepted=False, reason="missing build", build_number=9),
                chunk_id="chunk-0",
            )
            ledger._append(
                "StaleDetected",
                WORKER,
                StaleDetected(path="review", build_number=9),
                chunk_id="ghost",
            )
            ledger._append(
                "StaleMarked", WORKER, StaleMarked(chunk_ids=["chunk-2", "ghost"])
            )
            future = ledger._append(
                "FlagRaised", WORKER, FlagRaised(reason="</script>")
            )
    _mutate(path, future.seq, type="FutureEvent")
    page = tmp_path / "out.html"
    main(
        [
            str(tmp_path),
            str(TOY),
            "--out",
            str(page),
            "--label",
            "Scripted </script> fixture",
        ]
    )
    data = export_data(tmp_path, TOY)
    assert data["run"]["chunk_id_base"] == 0
    assert len(data["run"]["episode_id"]) == 12
    assert data["chunks"][0]["id"] == "chunk-0"
    assert data["events"][-1]["type"] == "FutureEvent"
    assert data["unplaced_events"][0]["seq"] == gate.seq
    assert [row["seq"] for row in data["unplaced_events"]] == [1, 2, 3, 3, 4]
    assert data["summary"]["gate_blocks"] == 1
    assert "Scripted <\\/script> fixture" in page.read_text()


def test_timeline_ballots_and_legacy_gate_attach_to_their_versions_and_builds(tmp_path):
    actor = Actor(agent="reviewer", model="script", role="reviewer")
    identity = IDENTITY.model_copy(
        update={
            "authority": "triad",
            "owner": actor.agent,
            "subject": DecisionSubject(kind="term", key="access token"),
        }
    )
    ruling = RULING.model_copy(
        update={
            "ratifier": actor.agent,
            "consistency": "QUORUM",
            "sentence_ids": [SITE],
        }
    )
    triad = [actor.agent, "first", "second"]
    approval = RulingApproval(operation="toy", triad=triad, approvers=triad[:2])
    supports = [
        RulingBallot(voter=name, choice=ruling.rendering) for name in approval.approvers
    ]
    with _run_with_ruling(tmp_path / "ledger.sqlite") as (_, ledger):
        first = ledger.ratify(
            actor,
            identity,
            0,
            ruling,
            "vote",
            approval=approval,
            ballots=[*supports, RulingBallot(voter="second", choice="other")],
        )
        _build(ledger, first.seq, 1, 1)
        revised = ruling.model_copy(
            update={"evidence": [*ruling.evidence, "source:9999-1-s03"]}
        )
        ledger.transition(
            "Revised",
            actor,
            identity.id,
            1,
            revised,
            "more evidence",
            approval=approval,
            ballots=[*supports, RulingBallot(voter="second", choice=None)],
        )
        with ledger._db:
            ledger._db.execute("BEGIN IMMEDIATE")
            ledger._append(
                "GateDecision",
                WORKER,
                GateDecision(accepted=True, reason="legacy record"),
                chunk_id="chunk-1",
            )
            ledger._append(
                "VoteCast",
                actor,
                VoteCast(voter="reviewer", choice=None, decision_id="missing"),
            )
            ledger._append(
                "ChunkBuilt",
                WORKER,
                ChunkBuilt(
                    ledger_version=first.seq,
                    uses=[VersionRef(commitment_id="unknown", version=1)],
                    decisions={},
                ),
                chunk_id="chunk-4",
            )
            ledger._append(
                "Tombstoned",
                actor,
                Tombstone(
                    rendering="other",
                    scope=["access token"],
                    commitment_id="unknown",
                    version=1,
                ),
            )
    result = export_data(tmp_path, TOY)
    votes = result["commitments"][0]["versions"]
    assert [row["status"] for row in votes[0]["ballots"]] == [
        "support",
        "support",
        "dissent",
    ]
    assert [row["status"] for row in votes[1]["ballots"]] == [
        "support",
        "support",
        "abstention",
    ]
    gate = result["chunks"][0]["builds"][0]["gate_decisions"][0]
    assert gate["accepted"] and gate["build_number"] is None
    assert [row["type"] for row in result["unplaced_events"]] == [
        "VoteCast",
        "ChunkBuilt",
        "Tombstoned",
    ]


def test_timeline_empty_and_unsafe_output_paths(tmp_path, capsys):
    path = tmp_path / "ledger.sqlite"
    with _run_with_ruling(path):
        pass
    result = export_data(tmp_path, TOY)
    assert (
        result["run"]["final_sequence"] == 0 and result["run"]["head_hash"] == "0" * 64
    )
    assert result["summary"]["events"] == 0 and result["commitments"] == []

    first = tmp_path / "page.html"
    hardlink = tmp_path / "linked-db"
    hardlink.hardlink_to(path)
    for output, additional in (
        (path, []),
        (hardlink, []),
        (first, ["--json", str(first)]),
    ):
        with pytest.raises(SystemExit) as failure:
            main([str(tmp_path), str(TOY), "--out", str(output), *additional])
        assert failure.value.code == 1
        assert "distinct export paths" in capsys.readouterr().err

    manifest = tmp_path / "run.json"
    manifest.write_text('{"run_id":"different","episode_id":"toy"}')
    with pytest.raises(ValueError, match="run metadata names another run"):
        export_data(tmp_path, TOY)
    manifest.unlink()
    unbound = tmp_path / "unbound"
    unbound.mkdir()
    with store.Ledger(unbound / "ledger.sqlite", "old"):
        pass
    with pytest.raises(ValueError, match="missing public episode binding"):
        export_data(unbound, TOY)


@pytest.mark.nightly
def test_timeline_chrome_phone_interactions_without_script_errors(
    tmp_path, scripted_run
):
    chrome = (
        os.environ.get("CHROME_BIN")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
    )
    mac = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if chrome is None and mac.is_file():
        chrome = str(mac)
    if chrome is None:
        pytest.skip("set CHROME_BIN to a local Chromium or Chrome binary")
    path = scripted_run / "ledger.sqlite"
    with open_run_ledger(TOY, path, SPEC.run_id, authority=AUTHORITY) as (_, ledger):
        with ledger._db:
            ledger._db.execute("BEGIN IMMEDIATE")
            ledger._append(
                "VoteCast",
                WORKER,
                VoteCast(
                    voter=WORKER.agent, choice=None, decision_id="urn:soc:version:D1/1"
                ),
                commitment_id="D1",
            )
            future = ledger._append(
                "FlagRaised", WORKER, FlagRaised(reason="future entry")
            )
    _mutate(path, future.seq, type="FutureEvent")
    page = tmp_path / "browser.html"
    main([str(scripted_run), str(TOY), "--out", str(page)])
    html = page.read_text()
    error_hook = '<script>window.timelineErrors=[]; window.addEventListener("error", e => timelineErrors.push(e.message));</script>'
    checks = """<script>
    try {
      const verify = (truth, reason) => { if (!truth) throw new Error(reason); };
      verify(timelineErrors.length === 0, timelineErrors.join(" | "));
      verify(document.querySelectorAll("#strip .tick").length === D.events.length, "strip size");
      const initialRows = document.querySelectorAll("#log tr").length;
      const votesFilter = [...document.querySelectorAll("#filters button")].find(e => e.textContent === "VoteCast");
      verify(votesFilter.getAttribute("aria-pressed") === "false", "votes off by default");
      votesFilter.click();
      verify(document.querySelectorAll("#log tr").length === initialRows + 1, "vote filter");
      votesFilter.click();
      verify(document.querySelector("#unplaced").textContent.includes("FutureEvent"), "unknown event in note");
      verify(document.querySelector("#log").textContent.includes("FutureEvent"), "unknown event in log");
      document.querySelector("#matrix tbody tr:nth-child(2) td button").click();
      verify(!document.getElementById("scope").hidden, "scoped log");
      verify(document.querySelectorAll("#log tr").length < initialRows, "scope reduces rows");
      document.querySelector("#scope .clear").click();
      verify(document.querySelectorAll("#log tr").length === initialRows, "show all clears selection");
      document.querySelector("#matrix thead th:nth-child(2) button").click();
      verify(document.getElementById("detail").textContent.includes("Chunk 1"), "column details");
      document.querySelector("#matrix tbody tr:nth-child(2) th button").click();
      verify(document.getElementById("detail").textContent.includes("Valid sequence"), "decision details");
      verify(document.querySelector(".work .scroll").scrollWidth > document.querySelector(".work .scroll").clientWidth, "matrix scroll");
      verify(document.documentElement.scrollWidth <= window.innerWidth, "body must fit phone width");
      document.body.dataset.timelineStatus = "passed";
    } catch (error) {
      document.body.dataset.timelineStatus = error.message;
    }
    </script>"""
    page.write_text(
        html.replace("</head>", error_hook + "</head>").replace(
            "</body>", checks + "</body>"
        )
    )
    browser = subprocess.run(
        [
            chrome,
            "--headless=new",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=390,844",
            f"--user-data-dir={tmp_path / 'chrome-profile'}",
            "--dump-dom",
            page.as_uri(),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=45,
    )
    assert 'data-timeline-status="passed"' in browser.stdout, next(
        (line for line in browser.stdout.splitlines() if "<body" in line),
        browser.stderr[-3000:],
    )
