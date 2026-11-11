"""SQLite event store, replay, and monotonic reader handles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import JsonValue

from soc.episode import Episode
from soc.ledger.models import (
    Actor,
    BriefChanged,
    ChunkBuilt,
    Commitment,
    CommitmentVersion,
    Conflict,
    Event,
    EventType,
    ExecutorRebound,
    Record,
    Ruling,
    Tombstone,
    Transition,
    VersionChange,
)
from soc.ledger.schema.validation import (
    Node,
    context,
    epoch_nodes,
    event_nodes,
    validate_records,
    version_iri,
    version_nodes,
)

LiteralChange = Literal["Ratified", "Revised", "Superseded", "Revoked"]

GENESIS = "0" * 64
DDL = Path(__file__).with_name("store.sql")
COLUMNS = tuple(Event.model_fields)
INSERT = f"INSERT INTO events ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)})"


class AuthorityError(ValueError):
    """The actor does not own the ruling."""


@dataclass
class Projection:
    ledger_version: int = 0
    identities: dict[str, Commitment] = field(default_factory=dict)
    current: dict[str, CommitmentVersion] = field(default_factory=dict)
    versions: dict[tuple[str, int], CommitmentVersion] = field(default_factory=dict)
    tombstones: list[Tombstone] = field(default_factory=list)
    chunks: dict[str, ChunkBuilt] = field(default_factory=dict)
    usage: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    brief: dict[str, str] = field(default_factory=dict)
    executor: ExecutorRebound | None = None

    def _apply(self, event: Event) -> None:
        self.ledger_version = event.ledger_version
        if event.new_version is not None:
            change = _payload(event, VersionChange)
            commitment = change.commitment
            version = change.version
            if previous := self.current.get(commitment.id):
                closed = previous.model_copy(update={"valid_until": version.valid_from})
                self.versions[commitment.id, previous.version] = closed
            self.identities[commitment.id] = commitment
            self.versions[commitment.id, version.version] = version
            self.current[commitment.id] = version
        elif event.type == "Tombstoned":
            self.tombstones.append(_payload(event, Tombstone))
        elif event.type == "ChunkBuilt":
            assert event.chunk_id is not None
            chunk = _payload(event, ChunkBuilt)
            self.chunks[event.chunk_id] = chunk
            for version_ref in chunk.uses:
                key = (version_ref.commitment_id, version_ref.version)
                self.usage.setdefault(key, []).append(event.chunk_id)
        elif event.type == "BriefChanged":
            self.brief.update(_payload(event, BriefChanged).clauses)
        elif event.type == "ExecutorRebound":
            self.executor = _payload(event, ExecutorRebound)


def _payload[T: Record](event: Event, model: type[T]) -> T:
    return model.model_validate(event.payload["data"])


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(data: dict[str, JsonValue]) -> str:
    return hashlib.sha256(_canonical(data).encode()).hexdigest()


def _unpack(row: sqlite3.Row) -> dict[str, JsonValue]:
    data = dict(row)
    data["actor"] = json.loads(data["actor"])
    data["payload"] = json.loads(data["payload"])
    return data


class Ledger:
    """One SQLite run. Each process opens its own handle."""

    def __init__(self, path: Path, run_id: str, *, episode: Episode | None = None):
        if not run_id.strip():
            raise ValueError("run_id must be non-empty")
        self._run_id = run_id
        self._episode = episode.model_copy(deep=True) if episode is not None else None
        self._live = Projection()
        self.last_validation_ns = 0
        self._db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript(DDL.read_text(encoding="utf-8"))
            row = self._db.execute("SELECT run_id FROM events WHERE seq = 1").fetchone()
            if row is not None and row["run_id"] != run_id:
                raise ValueError("ledger belongs to another run")
        except BaseException:
            self._db.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._db.close()

    def _events(self, after: int = 0) -> list[Event]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE seq > ? ORDER BY seq", (after,)
        )
        return [Event.model_validate(_unpack(row)) for row in rows]

    def verify(self) -> int | None:
        """Return the expected sequence of the first hash-chain break."""
        previous = GENESIS
        for seq, row in enumerate(
            self._db.execute("SELECT * FROM events ORDER BY seq"), 1
        ):
            try:
                data = _unpack(row)
                stored = data.pop("hash")
                if (
                    data["seq"] != seq
                    or data["run_id"] != self._run_id
                    or data["prev_hash"] != previous
                    or row["actor"] != _canonical(data["actor"])
                    or row["payload"] != _canonical(data["payload"])
                    or stored != _digest(data)
                ):
                    return seq
            except ValueError:
                return seq
            previous = stored
        return None

    def rebuild(self) -> Projection:
        """Replay persisted events into a fresh projection."""
        result = Projection()
        for event in self._events():
            result._apply(event)
        return result

    def _sync(self) -> None:
        for event in self._events(after=self._live.ledger_version):
            self._live._apply(event)

    def snapshot(self) -> Projection:
        """Refresh the live projection and return a detached copy."""
        self._sync()
        return deepcopy(self._live)

    def is_tombstoned(self, rendering: str, scope: str) -> bool:
        self._sync()
        return any(
            row.rendering == rendering and scope in row.scope
            for row in self._live.tombstones
        ) and not any(
            ruling.state != "revoked"
            and ruling.rendering == rendering
            and scope in ruling.scope
            for ruling in self._live.current.values()
        )

    def reader(self) -> Reader:
        return Reader(self)

    def ratify(
        self,
        actor: Actor,
        identity: Commitment,
        base_version: int,
        ruling: Ruling,
        reason: str,
    ) -> Event:
        return self._change(
            "Ratified", actor, identity.id, base_version, ruling, reason, identity
        )

    def transition(
        self,
        kind: Transition,
        actor: Actor,
        commitment_id: str,
        base_version: int,
        ruling: Ruling,
        reason: str,
    ) -> Event:
        return self._change(kind, actor, commitment_id, base_version, ruling, reason)

    def _version(
        self, commitment_id: str, version: int | None = None
    ) -> VersionChange | None:
        if version is None:
            row = self._db.execute(
                "SELECT payload FROM events WHERE commitment_id = ? AND new_version IS NOT NULL "
                "ORDER BY new_version DESC LIMIT 1",
                (commitment_id,),
            ).fetchone()
        else:
            row = self._db.execute(
                "SELECT payload FROM events WHERE commitment_id = ? AND new_version = ?",
                (commitment_id, version),
            ).fetchone()
        return (
            None
            if row is None
            else VersionChange.model_validate(json.loads(row["payload"])["data"])
        )

    def _references(self, ruling: Ruling) -> list[Node]:
        nodes: list[Node] = []
        episode = self._episode
        if episode is not None:
            source = {sentence.id: sentence.text for sentence in episode.source}
            if quote := ruling.source_quote:
                if (
                    quote.sentence_id not in source
                    or quote.text not in source[quote.sentence_id]
                ):
                    raise ValueError(
                        "source quote is not verbatim in the named sentence"
                    )
            brief = {clause.id for clause in episode.brief.clauses}
            brief.update(
                row["key"]
                for row in self._db.execute(
                    "SELECT clause.key FROM events, json_each(events.payload, '$.data.clauses') AS clause "
                    "WHERE events.type = 'BriefChanged'"
                )
            )
        for citation in ruling.evidence:
            kind, _, identifier = citation.partition(":")
            if kind == "commitment":
                referenced = self._version(identifier)
                if referenced is None:
                    raise ValueError(f"unknown commitment evidence: {identifier}")
                nodes.extend(version_nodes(referenced.commitment, referenced.version))
            elif episode is not None:
                if kind == "source" and identifier not in source:
                    raise ValueError(f"unknown source evidence: {identifier}")
                if kind == "brief" and identifier not in brief:
                    raise ValueError(f"unknown brief evidence: {identifier}")
        return nodes

    def _change(
        self,
        kind: LiteralChange,
        actor: Actor,
        commitment_id: str,
        base_version: int,
        ruling: Ruling,
        reason: str,
        identity: Commitment | None = None,
    ) -> Event:
        if base_version < 0:
            raise ValueError("base_version must be nonnegative")
        self.last_validation_ns = 0
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            previous = self._version(commitment_id)
            actual = 0 if previous is None else previous.version.version
            if previous is not None:
                if actor.agent != previous.commitment.owner:
                    raise AuthorityError(
                        "only the commitment owner may change a ruling"
                    )
                if identity is not None and identity != previous.commitment:
                    raise ValueError("commitment identity is immutable")
                identity = previous.commitment
            if actual != base_version:
                return self._append(
                    "ConflictRaised",
                    actor,
                    Conflict(expected=base_version, actual=actual),
                    commitment_id=commitment_id,
                    base_version=base_version,
                )
            if identity is None:
                raise ValueError("ratify a commitment before changing it")
            if kind == "Revoked":
                allowed = {"revoked"}
            elif kind == "Revised":
                allowed = {"amended", "ratified"}
            else:
                allowed = {"ratified"}
            if ruling.state not in allowed:
                raise ValueError(f"{kind} requires state in {sorted(allowed)}")
            seq, _ = self._tail()
            version = CommitmentVersion(
                **ruling.model_dump(),
                commitment_id=commitment_id,
                version=actual + 1,
                valid_from=seq + 1,
            )
            nodes = self._references(version)
            links: Node = {}
            previous_id = None
            if previous is not None:
                old = previous.version
                previous_id = version_iri(commitment_id, old.version)
                closed = old.model_copy(update={"valid_until": version.valid_from})
                nodes.extend(version_nodes(identity, closed))
                links["antecedent"] = previous_id
            nodes.extend(version_nodes(identity, version, previous_id))
            links["consequent"] = version_iri(commitment_id, version.version)
            event = self._append(
                kind,
                actor,
                VersionChange(commitment=identity, version=version, reason=reason),
                commitment_id=commitment_id,
                base_version=base_version,
                new_version=version.version,
                nodes=nodes,
                links=links,
            )
            if previous is not None and previous.version.state != "revoked":
                lost_scope = dict.fromkeys(previous.version.scope)
                if (
                    version.state != "revoked"
                    and version.rendering == previous.version.rendering
                ):
                    lost_scope = dict.fromkeys(
                        key for key in lost_scope if key not in version.scope
                    )
                if lost_scope:
                    self._append(
                        "Tombstoned",
                        actor,
                        Tombstone(
                            rendering=previous.version.rendering,
                            scope=list(lost_scope),
                            commitment_id=commitment_id,
                            version=actual,
                        ),
                        commitment_id=commitment_id,
                        base_version=actual,
                        nodes=version_nodes(identity, previous.version),
                    )
            return event

    def record(
        self,
        actor: Actor,
        payload: BriefChanged | ChunkBuilt | ExecutorRebound,
        *,
        chunk_id: str | None = None,
    ) -> Event:
        """Append one supported operation event without changing commitment versions."""
        self.last_validation_ns = 0
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current, _ = self._tail()
            nodes: list[Node] = []
            kind: EventType
            if isinstance(payload, BriefChanged):
                kind = "BriefChanged"
            elif isinstance(payload, ExecutorRebound):
                kind = "ExecutorRebound"
                if payload.ledger_version != current:
                    raise ValueError(
                        "executor must rebind at the current ledger version"
                    )
                previous = self._db.execute(
                    "SELECT seq, payload FROM events WHERE type = 'ExecutorRebound' ORDER BY seq DESC LIMIT 1"
                ).fetchone()
                previous_start = 0
                if previous is not None:
                    rebound = ExecutorRebound.model_validate(
                        json.loads(previous["payload"])["data"]
                    )
                    if rebound.new_model != payload.old_model:
                        raise ValueError("old model must match the current executor")
                    previous_start = previous["seq"]
                nodes = epoch_nodes(
                    self._run_id, actor, payload, current + 1, previous_start
                )
            elif isinstance(payload, ChunkBuilt):
                kind = "ChunkBuilt"
                if not chunk_id or payload.ledger_version > current:
                    raise ValueError(
                        "chunk requires an ID and an existing ledger version"
                    )
                if self._db.execute(
                    "SELECT 1 FROM events WHERE type = 'ChunkBuilt' AND chunk_id = ?",
                    (chunk_id,),
                ).fetchone():
                    raise ValueError("chunk already built")
                for ref in payload.uses:
                    change = self._version(ref.commitment_id, ref.version)
                    following = self._version(ref.commitment_id, ref.version + 1)
                    if (
                        change is None
                        or change.version.state == "revoked"
                        or change.version.valid_from > payload.ledger_version
                        or (
                            following is not None
                            and following.version.valid_from <= payload.ledger_version
                        )
                    ):
                        raise ValueError(
                            "chunk refers to a version inactive at its ledger version"
                        )
                    nodes.extend(version_nodes(change.commitment, change.version))
            else:
                raise TypeError("unsupported M0 operation payload")
            return self._append(kind, actor, payload, chunk_id=chunk_id, nodes=nodes)

    def _tail(self) -> tuple[int, str]:
        row = self._db.execute(
            "SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return (0, GENESIS) if row is None else (row["seq"], row["hash"])

    def _append(
        self,
        kind: EventType,
        actor: Actor,
        payload: Record,
        *,
        commitment_id: str | None = None,
        base_version: int | None = None,
        new_version: int | None = None,
        chunk_id: str | None = None,
        nodes: list[Node] | None = None,
        links: Node | None = None,
    ) -> Event:
        if not self._db.in_transaction:
            raise RuntimeError("append requires the write transaction")
        tail, previous = self._tail()
        seq = tail + 1
        event_id = str(uuid4())
        activity, agent = event_nodes(event_id, kind, seq, actor)
        if links is not None:
            activity.update(links)
        if isinstance(payload, VersionChange):
            if commitment_id != payload.commitment.id:
                raise ValueError("event and ruling must refer to the same commitment")
            existing = self._version(payload.commitment.id)
            if existing is not None and actor.agent != existing.commitment.owner:
                raise AuthorityError("only the commitment owner may change a ruling")
            types: list[JsonValue] = ["prov:Activity", "LedgerEvent"]
            if base_version:
                types.append("RevisionEvent")
            if kind == "Ratified":
                types.append("Ratification")
                activity.update(
                    ratifier=version_nodes(payload.commitment, payload.version)[-1][
                        "ratifier"
                    ],
                    evidence=list(payload.version.evidence),
                    consistency=payload.version.consistency,
                )
            activity["@type"] = types
        graph: list[JsonValue] = [activity, agent, *(nodes or [])]
        self.last_validation_ns += validate_records([activity, agent, *(nodes or [])])
        fields: dict[str, JsonValue] = {
            "seq": seq,
            "event_id": event_id,
            "run_id": self._run_id,
            "time": datetime.now(UTC).isoformat(),
            "type": kind,
            "actor": actor.model_dump(mode="json"),
            "commitment_id": commitment_id,
            "base_version": base_version,
            "new_version": new_version,
            "ledger_version": seq,
            "chunk_id": chunk_id,
            "payload": {
                "data": payload.model_dump(mode="json"),
                "@context": context(),
                "@graph": graph,
            },
            "prev_hash": previous,
        }
        fields["hash"] = _digest(fields)
        event = Event.model_validate(fields)
        self._db.execute(
            INSERT,
            tuple(
                _canonical(fields[name])
                if name in {"payload", "actor"}
                else fields[name]
                for name in COLUMNS
            ),
        )
        return event


class Reader:
    """A monotonically advancing view over a ledger."""

    def __init__(self, ledger: Ledger):
        self._ledger = ledger
        self._version = 0

    def in_scope(
        self, scope: str, at: int | None = None
    ) -> dict[str, CommitmentVersion]:
        self._ledger._sync()
        state = self._ledger._live
        version = state.ledger_version if at is None else at
        if not self._version <= version <= state.ledger_version:
            raise ValueError(
                "reader version must be available and must not go backwards"
            )
        self._version = version
        return {
            identity: entry.model_copy(update={"valid_until": None}, deep=True)
            for (identity, _), entry in state.versions.items()
            if entry.valid_from <= version
            and (entry.valid_until is None or version < entry.valid_until)
            and entry.state != "revoked"
            and scope in entry.scope
        }
