"""Replay public runtime evidence into an append-only cooperation projection."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from pathlib import Path
from typing import Annotated

from pydantic import Field

from soc.cooperation.prompts import PromptPolicy
from soc.cooperation.standing import (
    CheckKind,
    CheckOutcome,
    CooperationPolicy,
    RuntimeCheck,
    Standing,
    StandingTracker,
    Visibility,
)
from soc.ledger.models import (
    Actor,
    ChunkBuilt,
    DecisionCaptured,
    DecisionDeclared,
    Event,
    ExecutorRebound,
    FlagRaised,
    FlagResolved,
    GateDecision,
    Record,
)
from soc.ledger.store import Ledger


class CooperationSettings(Record):
    episode_position: Annotated[int, Field(ge=1, le=12)]
    visibility: Visibility
    policy: CooperationPolicy = Field(default_factory=CooperationPolicy)
    prompts: PromptPolicy | None = None


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def actor_key(actor: Actor) -> str:
    """A durable agent keeps its ID across replacement; its model does not."""
    return _json([actor.agent, actor.model])


def _standing(state: Standing) -> dict[str, object]:
    return {
        **asdict(state),
        "score": str(state.score),
        "vote_weight": str(state.vote_weight),
    }


class _Projection:
    def __init__(
        self, settings: CooperationSettings, participants: tuple[Actor, ...]
    ) -> None:
        self.settings = settings
        self.tracker = StandingTracker(settings.policy)
        self.actors = {actor.agent: actor for actor in participants}
        self.identities = {actor_key(actor) for actor in participants}
        self.builds: dict[tuple[str | None, int], Actor] = {}
        self.build_numbers: dict[str | None, int] = {}
        self.rule_epoch = 0
        self.build_epochs: dict[tuple[str | None, int], int] = {}
        self.gated: set[tuple[str | None, int]] = set()
        self.declarations: set[tuple[str, str]] = set()
        self.flags: dict[str, Actor] = {}
        self.pending_reviews: dict[str | None, tuple[Actor, set[str]]] = {}
        self.build_reviews: dict[tuple[str | None, int], tuple[Actor, set[str]]] = {}
        self.sequence = 0

    def rows(self, event: Event) -> list[dict[str, object]]:
        rows: list[dict[str, object]] = []

        def check(
            actor: Actor, kind: CheckKind, outcome: CheckOutcome, reference: str
        ) -> None:
            key = actor_key(actor)
            if key not in self.identities:
                raise ValueError("runtime check names an unauthenticated participant")
            before = self.tracker.snapshot(key)
            self.sequence += 1
            observation = RuntimeCheck(
                check_id=f"runtime-{event.seq}-{len(rows)}",
                sequence=self.sequence,
                agent=key,
                kind=kind,
                outcome=outcome,
                runtime_ref=reference,
                observer="runtime-orchestrator",
            )
            self.tracker.record(observation)
            after = self.tracker.snapshot(key)
            rows.append(
                {
                    "kind": "StandingUpdated",
                    "check": observation.model_dump(mode="json"),
                    "state": _standing(after),
                }
            )
            if before.sanction != after.sanction:
                rows.append(
                    {
                        "kind": "SanctionApplied",
                        "agent": key,
                        "before": before.sanction,
                        "after": after.sanction,
                    }
                )

        data = event.payload["data"]
        if event.type in {
            "Provisional",
            "Ratified",
            "Revised",
            "Superseded",
            "Revoked",
            "BriefChanged",
        }:
            self.rule_epoch = event.seq
        elif event.type in {"DecisionDeclared", "DecisionCaptured"}:
            entry = (
                DecisionDeclared.model_validate(data)
                if event.type == "DecisionDeclared"
                else DecisionCaptured.model_validate(data)
            )
            reference = _json(
                [
                    "draft",
                    event.chunk_id,
                    self.build_numbers.get(event.chunk_id, 0),
                    entry.ref.commitment_id,
                    entry.ref.version,
                    entry.sentence_id,
                ]
            )
            identity = (actor_key(event.actor), reference)
            if identity not in self.declarations:
                self.declarations.add(identity)
                check(
                    event.actor,
                    "declaration",
                    "pass"
                    if isinstance(entry, DecisionDeclared) and entry.verified
                    else "fail",
                    reference,
                )
        elif event.type == "ChunkBuilt":
            build = ChunkBuilt.model_validate(data)
            self.build_numbers[event.chunk_id] = build.build_number
            self.builds[event.chunk_id, build.build_number] = event.actor
            self.build_epochs[event.chunk_id, build.build_number] = (
                self.rule_epoch if self.rule_epoch <= build.ledger_version else -1
            )
            if event.chunk_id in self.pending_reviews:
                self.build_reviews[event.chunk_id, build.build_number] = (
                    self.pending_reviews.pop(event.chunk_id)
                )
        elif event.type == "GateDecision":
            verdict = GateDecision.model_validate(data)
            key = (event.chunk_id, verdict.build_number or 1)
            if key not in self.gated:
                self.gated.add(key)
                actor = self.builds.get(key)
                if actor is None:
                    raise ValueError("gate check has no authored build")
                # Revision/provisional/artifact failures cannot establish worker defection.
                content_rules = {
                    "ids",
                    "verbatim",
                    "bcp14",
                    "numbers",
                    "term",
                    "forbidden",
                }
                outcome: CheckOutcome
                if self.build_epochs[key] != self.rule_epoch or any(
                    finding.rule not in content_rules
                    or finding.outcome in {"unknown", "held", "unverified"}
                    for finding in verdict.findings
                ):
                    outcome = "unknown"
                else:
                    outcome = (
                        "fail"
                        if any(
                            finding.outcome in {"block", "warning"}
                            for finding in verdict.findings
                        )
                        else "pass"
                    )
                check(actor, "gate", outcome, _json(["build", *key]))
                if key in self.build_reviews:
                    moderator, sites = self.build_reviews[key]
                    review_outcome: CheckOutcome = "unknown"
                    if outcome == "pass":
                        review_outcome = "pass"
                    elif outcome == "fail" and any(
                        finding.sentence_id in sites
                        and finding.outcome in {"block", "warning"}
                        for finding in verdict.findings
                    ):
                        review_outcome = "fail"
                    check(moderator, "review", review_outcome, _json(["review", *key]))
        elif event.type == "FlagRaised":
            flag = FlagRaised.model_validate(data)
            if flag.flag_id is not None:
                self.flags[flag.flag_id] = event.actor
        elif event.type == "FlagResolved":
            resolution = FlagResolved.model_validate(data)
            trace = resolution.trace
            if trace is not None:
                previous = self.pending_reviews.get(event.chunk_id)
                sites = set() if previous is None else previous[1]
                sites.add(trace.sentence_id)
                self.pending_reviews[event.chunk_id] = (event.actor, sites)
                actor = self.flags[resolution.flag_id]
                check(
                    actor,
                    "flag_ruling",
                    "unknown" if trace.consistency == "SERIAL" else "pass",
                    f"flag:{resolution.flag_id}",
                )
                for supporter in sorted(
                    set(trace.evidence_supporters) & set(trace.approvers)
                ):
                    if supporter in self.actors:
                        check(
                            self.actors[supporter],
                            "evidence_ruling",
                            "pass",
                            f"evidence:{resolution.flag_id}:{supporter}",
                        )
        elif event.type == "ExecutorRebound":
            rebound = ExecutorRebound.model_validate(data)
            predecessor = self.actors.get(event.actor.agent)
            if (
                predecessor is None
                or predecessor.model != rebound.old_model
                or event.actor.model != rebound.new_model
            ):
                raise ValueError(
                    "executor transfer differs from authenticated participants"
                )
            old, new = actor_key(predecessor), actor_key(event.actor)
            self.tracker.rebind(
                old,
                new,
                episode=self.settings.episode_position,
                arm=self.settings.visibility,
            )
            self.actors[event.actor.agent] = event.actor
            self.identities.add(new)
            rows.append(
                {
                    "kind": "SuccessorStanding",
                    "predecessor": old,
                    "successor": new,
                    "state": _standing(self.tracker.snapshot(new)),
                }
            )
        return rows


class CooperationJournal:
    """Derive state under the runner lock; pin every source event's hash.

    Each sync recomputes the projection from verified public ledger history and
    compares stored rows before appending. A crash can leave this sidecar behind
    the ledger; the next sync catches up atomically. It may never run ahead or
    silently accept a fork, changed configuration, or edited projection.
    """

    def __init__(
        self,
        path: Path,
        run_id: str,
        settings: CooperationSettings,
        participants: tuple[Actor, ...],
    ) -> None:
        if not participants or len({actor.agent for actor in participants}) != len(
            participants
        ):
            raise ValueError("cooperation requires distinct authenticated participants")
        self.path, self.settings, self.participants = path, settings, participants
        self.run_id = run_id
        self.binding = _json(
            [
                run_id,
                settings.model_dump(mode="json"),
                [actor.model_dump(mode="json") for actor in participants],
            ]
        )

    def sync(self, ledger: Ledger) -> StandingTracker:
        if ledger.run_id != self.run_id:
            raise ValueError("cooperation configuration belongs to another run")
        events = ledger.history()
        projection = _Projection(self.settings, self.participants)
        derived = [
            (event.seq, event.hash, _json(projection.rows(event))) for event in events
        ]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), value TEXT NOT NULL) STRICT;
                CREATE TABLE IF NOT EXISTS source_events (sequence INTEGER PRIMARY KEY, source_hash TEXT NOT NULL, records TEXT NOT NULL CHECK(json_valid(records))) STRICT;
                CREATE TRIGGER IF NOT EXISTS source_no_update BEFORE UPDATE ON source_events BEGIN SELECT RAISE(ABORT, 'cooperation is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS source_no_delete BEFORE DELETE ON source_events BEGIN SELECT RAISE(ABORT, 'cooperation is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS binding_no_update BEFORE UPDATE ON binding BEGIN SELECT RAISE(ABORT, 'cooperation is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS binding_no_delete BEFORE DELETE ON binding BEGIN SELECT RAISE(ABORT, 'cooperation is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS source_order BEFORE INSERT ON source_events WHEN NEW.sequence != (SELECT coalesce(max(sequence),0)+1 FROM source_events) BEGIN SELECT RAISE(ABORT, 'cooperation sequence must advance'); END;
            """)
            with db:
                db.execute("BEGIN IMMEDIATE")
                binding = db.execute("SELECT value FROM binding WHERE id=1").fetchone()
                if binding is None:
                    db.execute("INSERT INTO binding VALUES (1, ?)", (self.binding,))
                elif binding[0] != self.binding:
                    raise ValueError("cooperation configuration changed")
                stored = db.execute(
                    "SELECT sequence, source_hash, records FROM source_events ORDER BY sequence"
                ).fetchall()
                if len(stored) > len(derived) or any(
                    row[:2] != expected[:2]
                    for row, expected in zip(stored, derived, strict=False)
                ):
                    raise ValueError("cooperation source ledger prefix changed")
                if any(
                    row[2] != expected[2]
                    for row, expected in zip(stored, derived, strict=False)
                ):
                    raise ValueError("cooperation projection was changed")
                db.executemany(
                    "INSERT INTO source_events VALUES (?, ?, ?)", derived[len(stored) :]
                )
        return projection.tracker

    def freeze(
        self,
        ledger: Ledger,
        chunk_id: str,
        round_number: int,
        actors: tuple[Actor, ...],
    ) -> dict[str, Standing]:
        """Bind an attempt to its original public evidence, including on resume."""
        self.sync(ledger)
        events = ledger.history()
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS attempts (chunk TEXT NOT NULL, round INTEGER NOT NULL, source_count INTEGER NOT NULL, source_hash TEXT NOT NULL, snapshot TEXT NOT NULL CHECK(json_valid(snapshot)), PRIMARY KEY(chunk,round)) STRICT;
                CREATE TRIGGER IF NOT EXISTS attempts_no_update BEFORE UPDATE ON attempts BEGIN SELECT RAISE(ABORT, 'cooperation is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS attempts_no_delete BEFORE DELETE ON attempts BEGIN SELECT RAISE(ABORT, 'cooperation is append-only'); END;
            """)
            with db:
                db.execute("BEGIN IMMEDIATE")
                stored = db.execute(
                    "SELECT source_count,source_hash,snapshot FROM attempts WHERE chunk=? AND round=?",
                    (chunk_id, round_number),
                ).fetchone()
                count = len(events) if stored is None else stored[0]
                if not 0 <= count <= len(events):
                    raise ValueError("cooperation attempt source prefix changed")
                source_hash = events[count - 1].hash if count else ""
                projection = _Projection(self.settings, self.participants)
                for event in events[:count]:
                    projection.rows(event)
                if (
                    len(actors) != len(projection.actors)
                    or {actor.agent: actor for actor in actors} != projection.actors
                ):
                    raise ValueError(
                        "cooperation attempt snapshot changed: participants differ"
                    )
                states = {
                    actor.agent: projection.tracker.snapshot(actor_key(actor))
                    for actor in actors
                }
                snapshot = _json(
                    [
                        [actor.model_dump(mode="json"), _standing(states[actor.agent])]
                        for actor in actors
                    ]
                )
                if stored is None:
                    db.execute(
                        "INSERT INTO attempts VALUES (?,?,?,?,?)",
                        (chunk_id, round_number, count, source_hash, snapshot),
                    )
                elif stored[1:] != (source_hash, snapshot):
                    raise ValueError("cooperation attempt snapshot changed")
        return states
