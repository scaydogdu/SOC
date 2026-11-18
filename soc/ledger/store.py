"""SQLite event store, replay, and monotonic reader handles."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import JsonValue

from soc.episode import Brief, Episode, Sentence, validate_runtime
from soc.ledger.models import (
    BINDING_STATES,
    TRIAD_APPROVAL_COUNTS,
    Actor,
    BriefChanged,
    BriefCitation,
    ChunkBuilt,
    Commitment,
    CommitmentVersion,
    Conflict,
    DecisionCaptured,
    DecisionDeclared,
    DecisionUse,
    Event,
    EventType,
    ExecutorRebound,
    GateDecision,
    GateFinding,
    OperationAuthority,
    Record,
    RepairPath,
    Ruling,
    RulingApproval,
    RulingBallot,
    RulingConflict,
    RulingOverlap,
    StaleDetected,
    StaleMarked,
    Tombstone,
    Transition,
    VersionChange,
    VersionRef,
    VoteCast,
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

LiteralChange = Literal["Provisional", "Ratified", "Revised", "Superseded", "Revoked"]
PROVISIONAL_CHUNK_LIMIT = 3
type ChunkIdBase = Literal[0, 1]

GENESIS = "0" * 64
DDL = Path(__file__).with_name("store.sql")
COLUMNS = tuple(Event.model_fields)
INSERT = f"INSERT INTO events ({', '.join(COLUMNS)}) VALUES ({', '.join('?' for _ in COLUMNS)})"


class AuthorityError(ValueError):
    """The actor does not own the ruling."""


@dataclass(frozen=True)
class RevisionHint:
    used: VersionRef
    current: CommitmentVersion


@dataclass(frozen=True)
class RepairRequest:
    chunk_id: str
    path: RepairPath
    build: ChunkBuilt
    ledger_version: int
    hints: tuple[RevisionHint, ...]


@dataclass
class Projection:
    ledger_version: int = 0
    identities: dict[str, Commitment] = field(default_factory=dict)
    current: dict[str, CommitmentVersion] = field(default_factory=dict)
    versions: dict[tuple[str, int], CommitmentVersion] = field(default_factory=dict)
    tombstones: list[Tombstone] = field(default_factory=list)
    chunks: dict[str, ChunkBuilt] = field(default_factory=dict)
    chunk_history: dict[str, list[ChunkBuilt]] = field(default_factory=dict)
    usage: dict[tuple[str, int], list[str]] = field(default_factory=dict)
    overturned: set[tuple[str, int]] = field(default_factory=set)
    stale_chunks: set[str] = field(default_factory=set)
    first_detection: dict[tuple[str, int], RepairPath] = field(default_factory=dict)
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
            self._apply_chunk(event.chunk_id, _payload(event, ChunkBuilt))
        elif event.type == "StaleMarked":
            marker = _payload(event, StaleMarked)
            if marker.overturned is not None:
                self.overturned.add(
                    (marker.overturned.commitment_id, marker.overturned.version)
                )
            self.stale_chunks.update(marker.chunk_ids)
        elif event.type == "StaleDetected":
            assert event.chunk_id is not None
            detected = _payload(event, StaleDetected)
            self.first_detection.setdefault(
                (event.chunk_id, detected.build_number), detected.path
            )
        elif event.type == "BriefChanged":
            self.brief.update(_payload(event, BriefChanged).clauses)
        elif event.type == "ExecutorRebound":
            self.executor = _payload(event, ExecutorRebound)

    def _apply_chunk(self, chunk_id: str, chunk: ChunkBuilt) -> None:
        history = self.chunk_history.setdefault(chunk_id, [])
        previous = history[-1] if history else None
        old_refs = (
            {(ref.commitment_id, ref.version) for ref in previous.uses}
            if previous
            else set()
        )
        new_refs = dict.fromkeys((ref.commitment_id, ref.version) for ref in chunk.uses)
        for key in old_refs - new_refs.keys():
            self.usage[key].remove(chunk_id)
            if not self.usage[key]:
                del self.usage[key]
        for key in new_refs:
            if key not in old_refs:
                self.usage.setdefault(key, []).append(chunk_id)
        history.append(chunk)
        self.chunks[chunk_id] = chunk
        self.stale_chunks.discard(chunk_id)


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

    def __init__(
        self,
        path: Path,
        run_id: str,
        *,
        episode: Episode | None = None,
        authority: OperationAuthority | None = None,
        chunk_id_base: ChunkIdBase = 1,
    ):
        if not run_id.strip():
            raise ValueError("run_id must be non-empty")
        self._run_id = run_id
        if authority is not None and episode is None:
            raise ValueError("operation authority requires a public episode")
        self._authority = authority
        self._episode = (
            validate_runtime(episode.model_copy(deep=True))
            if episode is not None
            else None
        )
        self._episode_hash = None
        self._chunk_sites: dict[str, frozenset[str]] = {}
        self._scope_keys: dict[str, list[str]] = {}
        if self._episode is not None:
            episode_data = self._episode.model_dump(mode="json")
            # Preserve the existing fingerprint for the default convention.
            if chunk_id_base != 1:
                episode_data = {"episode": episode_data, "chunk_id_base": chunk_id_base}
            self._episode_hash = _digest(episode_data)
            self._chunk_sites = {
                f"chunk-{position}": frozenset(chunk.sentence_ids)
                for position, chunk in enumerate(
                    self._episode.events.chunks, chunk_id_base
                )
            }
            self._scope_keys = {
                sentence.id: sentence.scope_keys for sentence in self._episode.source
            }
        self._live = Projection()
        self.last_validation_ns = 0
        self._db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._install_schema()
            with self._db:
                self._db.execute("BEGIN IMMEDIATE")
                row = self._db.execute(
                    "SELECT run_id FROM events WHERE seq = 1"
                ).fetchone()
                if row is not None and row["run_id"] != run_id:
                    raise ValueError("ledger belongs to another run")
                if self._episode_hash is not None:
                    self._db.execute(
                        "INSERT OR IGNORE INTO public_episode VALUES (1, ?, ?)",
                        (run_id, self._episode_hash),
                    )
                self._check_episode_binding()
                if authority is not None:
                    self._db.execute(
                        "INSERT OR IGNORE INTO operation_authority VALUES (1, ?, ?)",
                        (authority.operation, authority.principal),
                    )
                self._check_authority_binding()
        except BaseException:
            self._db.close()
            raise

    def _install_schema(self) -> None:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if self._db.execute("PRAGMA user_version").fetchone()[0] != 0:
                return
            prior = self._db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'events'"
            ).fetchone()
            if prior is not None:
                initial = self._db.execute(
                    "SELECT run_id FROM events WHERE seq = 1"
                ).fetchone()
                if initial is not None and initial["run_id"] != self._run_id:
                    raise ValueError("ledger belongs to another run")
                self._db.execute("DROP INDEX IF EXISTS chunk_build_order")
                for trigger in (
                    "event_no_update",
                    "event_no_delete",
                    "event_insert_order",
                ):
                    self._db.execute(f"DROP TRIGGER IF EXISTS {trigger}")
                self._db.execute(
                    "ALTER TABLE events RENAME TO events_before_provisional"
                )
            statement = ""
            for line in DDL.read_text(encoding="utf-8").splitlines(keepends=True):
                statement += line
                if sqlite3.complete_statement(statement):
                    self._db.execute(statement)
                    statement = ""
            if prior is not None:
                columns = ", ".join(COLUMNS)
                self._db.execute(
                    f"INSERT INTO events ({columns}) "
                    f"SELECT {columns} FROM events_before_provisional ORDER BY seq"
                )
                self._db.execute("DROP TABLE events_before_provisional")
            self._db.execute("PRAGMA user_version = 1")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self._db.close()

    def _check_episode_binding(self) -> None:
        row = self._db.execute(
            "SELECT run_id, sha256 FROM public_episode WHERE id = 1"
        ).fetchone()
        if row is None:
            return
        if row["run_id"] != self._run_id:
            raise ValueError("ledger belongs to another run")
        if self._episode_hash is None:
            raise ValueError("this ledger requires its public episode")
        if row["sha256"] != self._episode_hash:
            raise ValueError("public episode differs from the recorded episode")

    @property
    def authority(self) -> OperationAuthority | None:
        return self._authority

    def initial_brief(self) -> Brief:
        if self._episode is None:
            raise ValueError("this ledger has no public episode")
        return self._episode.brief.model_copy(deep=True)

    @property
    def public_chunk_ids(self) -> tuple[str, ...]:
        return tuple(self._chunk_sites)

    def public_chunks(self) -> tuple[tuple[Sentence, ...], ...]:
        if self._episode is None:
            raise ValueError("this ledger has no public episode")
        source = {sentence.id: sentence for sentence in self._episode.source}
        return deepcopy(
            tuple(
                tuple(source[sentence_id] for sentence_id in chunk.sentence_ids)
                for chunk in self._episode.events.chunks
            )
        )

    def _check_authority_binding(self) -> None:
        row = self._db.execute(
            "SELECT operation, principal FROM operation_authority WHERE id = 1"
        ).fetchone()
        if row is not None and self._authority != OperationAuthority(**dict(row)):
            raise AuthorityError("ledger requires its recorded operation authority")

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

    def is_tombstoned(
        self, rendering: str, scope: str, *, sentence_id: str | None = None
    ) -> bool:
        self._sync()
        return any(
            row.rendering == rendering
            and scope in row.scope
            and _applies_at(row.sentence_ids, sentence_id)
            for row in self._live.tombstones
        ) and not any(
            ruling.state != "revoked"
            and ruling.rendering == rendering
            and scope in ruling.scope
            and _applies_at(ruling.sentence_ids, sentence_id)
            for ruling in self._live.current.values()
        )

    def reader(self) -> Reader:
        return Reader(self)

    def propose(
        self,
        actor: Actor,
        identity: Commitment,
        ruling: Ruling,
        reason: str,
        *,
        approval: RulingApproval,
        ballots: Sequence[RulingBallot] = (),
    ) -> Event:
        """Record a one-vote draft decision with a stable commitment ID."""
        return self._change(
            "Provisional",
            actor,
            identity.id,
            0,
            ruling,
            reason,
            identity,
            approval=approval,
            ballots=ballots,
        )

    def ratify(
        self,
        actor: Actor,
        identity: Commitment,
        base_version: int,
        ruling: Ruling,
        reason: str,
        *,
        approval: RulingApproval | None = None,
        ballots: Sequence[RulingBallot] = (),
    ) -> Event:
        return self._change(
            "Ratified",
            actor,
            identity.id,
            base_version,
            ruling,
            reason,
            identity,
            approval=approval,
            ballots=ballots,
        )

    def transition(
        self,
        kind: Transition,
        actor: Actor,
        commitment_id: str,
        base_version: int,
        ruling: Ruling,
        reason: str,
        *,
        approval: RulingApproval | None = None,
        ballots: Sequence[RulingBallot] = (),
    ) -> Event:
        return self._change(
            kind,
            actor,
            commitment_id,
            base_version,
            ruling,
            reason,
            approval=approval,
            ballots=ballots,
        )

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

    def _references(
        self, ruling: CommitmentVersion
    ) -> tuple[list[Node], list[BriefCitation], list[VersionRef]]:
        nodes: list[Node] = []
        citations: list[BriefCitation] = []
        commitment_citations: list[VersionRef] = []
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
            brief = {
                clause.id: BriefCitation(
                    clause_id=clause.id, text=clause.text, ledger_version=0
                )
                for clause in episode.brief.clauses
            }
            brief.update(
                (
                    row["key"],
                    BriefCitation(
                        clause_id=row["key"],
                        text=row["value"],
                        ledger_version=row["seq"],
                    ),
                )
                for row in self._db.execute(
                    "SELECT events.seq, clause.key, clause.value FROM events, "
                    "json_each(events.payload, '$.data.clauses') AS clause "
                    "WHERE events.type = 'BriefChanged' ORDER BY events.seq"
                )
            )
        for citation in dict.fromkeys(ruling.evidence):
            kind, _, identifier = citation.partition(":")
            if kind == "commitment":
                referenced = self._version(identifier)
                if referenced is None:
                    raise ValueError(f"unknown commitment evidence: {identifier}")
                if self._authority is not None and (
                    identifier == ruling.commitment_id
                    or referenced.commitment.operation != self._authority.operation
                    or referenced.version.state not in BINDING_STATES
                ):
                    raise ValueError(f"ineligible commitment evidence: {identifier}")
                commitment_citations.append(
                    VersionRef(
                        commitment_id=identifier, version=referenced.version.version
                    )
                )
                nodes.extend(
                    version_nodes(
                        referenced.commitment,
                        referenced.version,
                        referenced.brief_citations,
                        approval=referenced.approval,
                    )
                )
            elif episode is not None:
                if kind == "source" and identifier not in source:
                    raise ValueError(f"unknown source evidence: {identifier}")
                if kind == "brief":
                    if identifier not in brief:
                        raise ValueError(f"unknown brief evidence: {identifier}")
                    citations.append(brief[identifier])
        return nodes, citations, commitment_citations

    def _check_sites(self, identity: Commitment, ruling: Ruling) -> None:
        if identity.subject is not None and ruling.sentence_ids is None:
            raise ValueError(
                "a ruling with a decision subject requires sentence use sites"
            )
        if identity.subject is None and ruling.sentence_ids is not None:
            raise ValueError("sentence use sites require a decision subject")
        if ruling.sentence_ids is None or self._episode is None:
            return
        scope_keys = set(ruling.scope)
        applicable = {
            sentence.id
            for sentence in self._episode.source
            if sentence.translatable and scope_keys.intersection(sentence.scope_keys)
        }
        if invalid := set(ruling.sentence_ids) - applicable:
            raise ValueError(f"use sites outside the ruling's scope: {sorted(invalid)}")

    def _ruling_conflict(
        self, identity: Commitment, version: CommitmentVersion
    ) -> RulingConflict | None:
        if identity.subject is None or version.state not in BINDING_STATES:
            return None
        assert version.sentence_ids is not None
        sites = set(version.sentence_ids)
        self._sync()
        overlaps = []
        for other_id, current in sorted(self._live.current.items()):
            other = self._live.identities[other_id]
            if (
                other_id != identity.id
                and other.operation == identity.operation
                and other.subject == identity.subject
                and current.state in BINDING_STATES
                and (shared := sites.intersection(current.sentence_ids or ()))
            ):
                overlaps.append(
                    RulingOverlap(
                        commitment_id=other_id,
                        version=current.version,
                        sentence_ids=sorted(shared),
                    )
                )
        if overlaps:
            return RulingConflict(
                candidate_id=identity.id, subject=identity.subject, overlaps=overlaps
            )
        return None

    def _authorize_ruling(
        self,
        actor: Actor,
        identity: Commitment,
        ruling: Ruling,
        approval: RulingApproval | None,
        previous: VersionChange | None,
        ballots: Sequence[RulingBallot],
    ) -> None:
        policy = self._authority
        if (
            policy is None
            and identity.authority is None
            and approval is None
            and not ballots
        ):
            if previous is not None and actor.agent != previous.commitment.owner:
                raise AuthorityError("only the commitment owner may change a ruling")
            return
        if (
            policy is None
            or identity.authority is None
            or approval is None
            or identity.operation != policy.operation
            or approval.operation != policy.operation
            or ruling.ratifier != actor.agent
        ):
            raise AuthorityError("ruling requires verified approval for its operation")
        choices = {ballot.voter: ballot.choice for ballot in ballots}
        if ballots and (
            len(ballots) != 3 or len(choices) != 3 or policy.principal in choices
        ):
            raise AuthorityError("ballots require three distinct triad voters")
        if identity.authority == "principal":
            if (
                actor.agent != policy.principal
                or actor.role != "principal"
                or identity.owner != policy.principal
                or ruling.consistency != "SERIAL"
                or approval.triad
                or approval.approvers != [policy.principal]
            ):
                raise AuthorityError(
                    "only the operation's principal may issue this ruling"
                )
            return

        triad, approvers = set(approval.triad), set(approval.approvers)
        provisional = ruling.state == "provisional"
        required = TRIAD_APPROVAL_COUNTS.get(ruling.consistency, 0)
        prior_required = (
            0
            if previous is None
            else TRIAD_APPROVAL_COUNTS.get(previous.version.consistency, 0)
        )
        if (
            len(approval.triad) != 3
            or len(triad) != 3
            or policy.principal in triad
            or actor.agent not in triad
            or actor.role == "principal"
            or (previous is None and identity.owner != actor.agent)
            or provisional != (ruling.consistency == "ONE")
            or required < max(1 if provisional else 2, prior_required)
            or not approvers <= triad
            or len(approval.approvers) != len(approvers)
            or len(approvers) < required
        ):
            raise AuthorityError("triad ruling lacks the required distinct approvals")
        supporters = {
            voter for voter, choice in choices.items() if choice == ruling.rendering
        }
        if (
            choices.keys() != triad
            or supporters != approvers
            or any(
                sum(choice == other for other in choices.values()) >= len(supporters)
                for choice in set(choices.values()) - {None, ruling.rendering}
            )
        ):
            raise AuthorityError("triad approvals must match the recorded ballots")

    def _change(
        self,
        kind: LiteralChange,
        actor: Actor,
        commitment_id: str,
        base_version: int,
        ruling: Ruling,
        reason: str,
        identity: Commitment | None = None,
        *,
        approval: RulingApproval | None = None,
        ballots: Sequence[RulingBallot] = (),
    ) -> Event:
        if base_version < 0:
            raise ValueError("base_version must be nonnegative")
        self.last_validation_ns = 0
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            previous = self._version(commitment_id)
            actual = 0 if previous is None else previous.version.version
            if previous is not None:
                self._authorize_ruling(
                    actor, previous.commitment, ruling, approval, previous, ballots
                )
                if identity is not None and identity != previous.commitment:
                    raise ValueError("commitment identity is immutable")
                identity = previous.commitment
            elif identity is not None:
                self._authorize_ruling(
                    actor, identity, ruling, approval, previous, ballots
                )
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
            if kind == "Provisional":
                allowed = {"provisional"}
            elif kind == "Revoked":
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
            self._check_sites(identity, version)
            nodes, brief_citations, commitment_citations = self._references(version)
            if conflict := self._ruling_conflict(identity, version):
                return self._append(
                    "ConflictRaised",
                    actor,
                    conflict,
                    commitment_id=commitment_id,
                    base_version=base_version,
                )
            links: Node = {}
            if previous is not None:
                old = previous.version
                closed = old.model_copy(update={"valid_until": version.valid_from})
                nodes.extend(
                    version_nodes(
                        identity,
                        closed,
                        previous.brief_citations,
                        approval=previous.approval,
                    )
                )
                links["used"] = version_iri(commitment_id, old.version)
            nodes.extend(
                version_nodes(identity, version, brief_citations, approval=approval)
            )
            links["generated"] = version_iri(commitment_id, version.version)
            stale_chunks: list[str] = []
            overturned = previous is not None and _overturned_provisional(
                previous.version, version
            )
            if overturned:
                self._sync()
                stale_chunks = list(self._live.usage.get((commitment_id, actual), ()))
            impacted = (
                self._affected_builds(version)
                if previous is not None and self._authority is not None
                else []
            )
            event = self._append(
                kind,
                actor,
                VersionChange(
                    commitment=identity,
                    version=version,
                    reason=reason,
                    brief_citations=brief_citations,
                    commitment_citations=commitment_citations,
                    approval=approval,
                ),
                commitment_id=commitment_id,
                base_version=base_version,
                new_version=version.version,
                nodes=nodes,
                links=links,
                ballots=ballots,
            )
            if overturned:
                self._mark_overturned(
                    actor,
                    VersionRef(commitment_id=commitment_id, version=actual),
                    stale_chunks,
                )
            if affected := sorted(set(impacted) - set(stale_chunks)):
                self._mark_revised(actor, version, affected)
            if previous is not None and (
                lost := _lost_application(previous.version, version)
            ):
                self._append(
                    "Tombstoned",
                    actor,
                    lost,
                    commitment_id=commitment_id,
                    base_version=actual,
                    nodes=version_nodes(
                        identity,
                        previous.version,
                        previous.brief_citations,
                        approval=previous.approval,
                    ),
                )
            return event

    def record(
        self,
        actor: Actor,
        payload: BriefChanged | ChunkBuilt | ExecutorRebound,
        *,
        chunk_id: str | None = None,
        base_build: int = 0,
    ) -> Event:
        """Append one supported operation event without changing commitment versions."""
        self.last_validation_ns = 0
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            current, _ = self._tail()
            nodes: list[Node] = []
            overturned_refs: list[VersionRef] = []
            revised_refs: list[CommitmentVersion] = []
            links: Node | None = None
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
                if self._episode is not None:
                    chunk_sites = self._chunk_sites.get(chunk_id)
                    if chunk_sites is None:
                        raise ValueError("chunk is not in the public episode")
                    if any(
                        site.sentence_id not in chunk_sites
                        for site in payload.use_sites
                    ):
                        raise ValueError(
                            "chunk use site is outside the scheduled chunk"
                        )
                previous_build = self._db.execute(
                    "SELECT payload FROM events WHERE type = 'ChunkBuilt' AND chunk_id = ? "
                    "ORDER BY seq DESC LIMIT 1",
                    (chunk_id,),
                ).fetchone()
                actual_build = (
                    json.loads(previous_build["payload"])["data"].get("build_number", 1)
                    if previous_build is not None
                    else 0
                )
                if base_build != actual_build:
                    raise ValueError(
                        f"chunk build changed: expected {base_build}, found {actual_build}"
                    )
                if payload.build_number != actual_build + 1:
                    raise ValueError("chunk build number must advance by one")
                if payload.output_ref is None:
                    raise ValueError("chunk build requires an output reference")
                declared = {(ref.commitment_id, ref.version) for ref in payload.uses}
                if any(
                    (site.ref.commitment_id, site.ref.version) not in declared
                    for site in payload.use_sites
                ):
                    raise ValueError(
                        "chunk use sites must identify one of its recorded versions"
                    )
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
                    if change.version.sentence_ids is not None and any(
                        site.ref == ref
                        and site.sentence_id not in change.version.sentence_ids
                        for site in payload.use_sites
                    ):
                        raise ValueError(
                            "chunk use site is outside the recorded ruling's scope"
                        )
                    nodes.extend(
                        version_nodes(
                            change.commitment,
                            change.version,
                            change.brief_citations,
                            approval=change.approval,
                        )
                    )
                    if change.version.state == "provisional":
                        self._sync()
                        key = (ref.commitment_id, ref.version)
                        if key in self._live.overturned and ref not in overturned_refs:
                            overturned_refs.append(ref)
                    if following is not None and self._authority is not None:
                        self._sync()
                        latest = self._live.current[ref.commitment_id]
                        if (
                            ref not in overturned_refs
                            and latest not in revised_refs
                            and _needs_refresh(
                                change.version,
                                latest,
                                payload.use_sites,
                                self._scope_keys,
                            )
                        ):
                            revised_refs.append(latest)
                links = {
                    "@type": ["prov:Activity", "LedgerEvent", "ChunkBuild"],
                    "chunkId": chunk_id,
                    "buildNumber": payload.build_number,
                    "outputRef": payload.output_ref,
                    "ledgerSnapshot": payload.ledger_version,
                    "used": [
                        version_iri(ref.commitment_id, ref.version)
                        for ref in payload.uses
                    ],
                }
            else:
                raise TypeError("unsupported ledger operation payload")
            event = self._append(
                kind, actor, payload, chunk_id=chunk_id, nodes=nodes, links=links
            )
            if chunk_id is not None:
                for ref in overturned_refs:
                    self._mark_overturned(actor, ref, [chunk_id])
                for current_version in revised_refs:
                    self._mark_revised(actor, current_version, [chunk_id])
            return event

    def _mark_overturned(
        self, actor: Actor, ref: VersionRef, chunk_ids: list[str]
    ) -> None:
        self._append(
            "StaleMarked",
            actor,
            StaleMarked(chunk_ids=chunk_ids, overturned=ref),
            commitment_id=ref.commitment_id,
            base_version=ref.version,
        )

    def _mark_revised(
        self, actor: Actor, current: CommitmentVersion, chunk_ids: list[str]
    ) -> None:
        self._append(
            "StaleMarked",
            actor,
            StaleMarked(
                chunk_ids=chunk_ids,
                replaced_by=VersionRef(
                    commitment_id=current.commitment_id, version=current.version
                ),
            ),
            commitment_id=current.commitment_id,
        )

    def _affected_builds(self, current: CommitmentVersion) -> list[str]:
        self._sync()
        return [
            chunk_id
            for (identity, version), chunks in self._live.usage.items()
            if identity == current.commitment_id
            for chunk_id in chunks
            if _needs_refresh(
                self._live.versions[identity, version],
                current,
                self._live.chunks[chunk_id].use_sites,
                self._scope_keys,
            )
        ]

    def revision_hints(
        self, uses: Sequence[VersionRef], *, use_sites: Sequence[DecisionUse] = ()
    ) -> tuple[RevisionHint, ...]:
        """Return current revisions applicable to the versions an in-flight draft used."""
        self._sync()
        return deepcopy(_revision_hints(self._live, uses, use_sites, self._scope_keys))

    def repair_request(
        self, actor: Actor, chunk_id: str, path: RepairPath
    ) -> RepairRequest | None:
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._sync()
            state = self._live
            build = state.chunks.get(chunk_id)
            if build is None:
                raise ValueError("repair requires a recorded chunk build")
            hints = _revision_hints(
                state, build.uses, build.use_sites, self._scope_keys
            )
            if chunk_id not in state.stale_chunks and not (
                self._authority is not None and hints
            ):
                return None
            request = RepairRequest(
                chunk_id,
                path,
                build,
                state.ledger_version,
                hints,
            )
            self._record_detection(actor, chunk_id, build.build_number, path)
            return deepcopy(request)

    def _record_detection(
        self, actor: Actor, chunk_id: str, build_number: int, path: RepairPath
    ) -> None:
        if (chunk_id, build_number) not in self._live.first_detection:
            self._append(
                "StaleDetected",
                actor,
                StaleDetected(path=path, build_number=build_number),
                chunk_id=chunk_id,
            )

    def check_provisional_dependencies(
        self, actor: Actor, chunk_id: str, *, build_number: int
    ) -> Event:
        return self.check_builds(actor, {chunk_id: build_number})[0]

    def check_builds(
        self,
        actor: Actor,
        build_numbers: Mapping[str, int | None],
        *,
        path: RepairPath = "gate",
        evaluate: Callable[[str, Projection, ChunkBuilt], Sequence[GateFinding]]
        | None = None,
    ) -> tuple[Event, ...]:
        """Check the requested current builds against one ledger snapshot."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._sync()
            state = self._live
            events = []
            for chunk_id, build_number in build_numbers.items():
                build = state.chunks.get(chunk_id)
                if build is None or (
                    build_number is not None and build.build_number != build_number
                ):
                    raise ValueError("gate requires the current chunk build")
                stale = chunk_id in state.stale_chunks or (
                    self._authority is not None
                    and bool(
                        _revision_hints(
                            state, build.uses, build.use_sites, self._scope_keys
                        )
                    )
                )
                if stale:
                    self._record_detection(actor, chunk_id, build.build_number, path)
                findings = _dependency_findings(state, build, stale=stale)
                if evaluate is not None:
                    findings.extend(evaluate(chunk_id, state, build))
                events.append(
                    self._append(
                        "GateDecision",
                        actor,
                        GateDecision(
                            accepted=all(item.outcome != "block" for item in findings),
                            reason=", ".join(item.detail for item in findings)
                            if findings
                            else "gate checks passed",
                            build_number=build.build_number,
                            findings=findings,
                        ),
                        chunk_id=chunk_id,
                    )
                )
            return tuple(events)

    def capture_draft(
        self,
        actor: Actor,
        chunk_id: str,
        observe: Callable[[Projection], Sequence[DecisionDeclared | DecisionCaptured]],
    ) -> tuple[Event, ...]:
        """Append a worker's manifest and undeclared uses against one snapshot."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            self._sync()
            return tuple(
                self._append(
                    "DecisionDeclared"
                    if isinstance(item, DecisionDeclared)
                    else "DecisionCaptured",
                    actor,
                    item,
                    chunk_id=chunk_id,
                    commitment_id=item.ref.commitment_id,
                    base_version=item.ref.version,
                )
                for item in observe(self._live)
            )

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
        ballots: Sequence[RulingBallot] = (),
    ) -> Event:
        if not self._db.in_transaction:
            raise RuntimeError("append requires the write transaction")
        if self._episode_hash is None:
            self._check_episode_binding()
        if self._authority is None:
            self._check_authority_binding()
        if (
            kind == "BriefChanged"
            and self._authority is not None
            and (actor.agent != self._authority.principal or actor.role != "principal")
        ):
            raise AuthorityError("only the operation's principal may change the brief")
        tail, previous = self._tail()
        seq = tail + 1
        event_id = str(uuid4())
        activity, agent = event_nodes(event_id, kind, seq, actor)
        if links is not None:
            activity.update(links)
        if isinstance(payload, VersionChange):
            if commitment_id != payload.commitment.id:
                raise ValueError("event and ruling must refer to the same commitment")
            if (kind == "Provisional") != (payload.version.state == "provisional") or (
                kind == "Provisional"
                and (
                    payload.commitment.authority != "triad"
                    or payload.commitment.subject is None
                )
            ):
                raise AuthorityError(
                    "a provisional ruling requires a provisional triad write"
                )
            existing = self._version(payload.commitment.id)
            if existing is not None and payload.commitment != existing.commitment:
                raise ValueError("commitment identity is immutable")
            self._authorize_ruling(
                actor,
                payload.commitment,
                payload.version,
                payload.approval,
                existing,
                ballots,
            )
            if (
                existing is not None
                and payload.commitment.authority == "triad"
                and not _confirmed_provisional(existing.version, payload.version)
                and not _has_new_evidence(existing, payload)
            ):
                raise AuthorityError("a triad revision requires new evidence")
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
        if isinstance(payload, VersionChange):
            for ballot in ballots:
                self._append(
                    "VoteCast",
                    actor,
                    VoteCast(
                        decision_id=version_iri(
                            payload.commitment.id, payload.version.version
                        ),
                        voter=ballot.voter,
                        choice=ballot.choice,
                    ),
                    commitment_id=commitment_id,
                    base_version=base_version,
                )
        return event


def _needs_refresh(
    used: CommitmentVersion,
    current: CommitmentVersion,
    sites: Sequence[DecisionUse],
    scope_keys: Mapping[str, list[str]],
) -> bool:
    if used.version >= current.version:
        return False
    if (
        used.rendering != current.rendering
        or used.rule != current.rule
        or used.strength != current.strength
        or current.state == "revoked"
    ):
        return True
    recorded = {
        site.sentence_id
        for site in sites
        if site.ref.commitment_id == used.commitment_id
        and site.ref.version == used.version
    }
    if used.sentence_ids is not None:
        removed = set(used.sentence_ids) - set(current.sentence_ids or ())
        return bool(removed.intersection(recorded or used.sentence_ids))

    removed_keys = set(used.scope) - set(current.scope)
    if recorded and recorded <= scope_keys.keys():
        return any(removed_keys.intersection(scope_keys[site]) for site in recorded)
    # Without site-level evidence any key in the recorded ruling may have been used.
    return bool(removed_keys)


def _revision_hints(
    state: Projection,
    refs: Sequence[VersionRef],
    sites: Sequence[DecisionUse],
    scope_keys: Mapping[str, list[str]],
) -> tuple[RevisionHint, ...]:
    hints = []
    for identity, version in dict.fromkeys(
        (ref.commitment_id, ref.version) for ref in refs
    ):
        used = state.versions.get((identity, version))
        current = state.current.get(identity)
        if (
            used is not None
            and current is not None
            and (
                (identity, version) in state.overturned
                or _needs_refresh(used, current, sites, scope_keys)
            )
        ):
            hints.append(
                RevisionHint(
                    VersionRef(commitment_id=identity, version=version), current
                )
            )
    return tuple(hints)


def _dependency_findings(
    state: Projection, build: ChunkBuilt, *, stale: bool
) -> list[GateFinding]:
    def block(detail: str, commitment_id: str | None = None) -> GateFinding:
        return GateFinding(
            rule="provisional",
            detail=detail,
            outcome="block",
            commitment_id=commitment_id,
        )

    findings = []
    if stale:
        if any(
            (ref.commitment_id, ref.version) in state.overturned for ref in build.uses
        ):
            findings.append(block("chunk uses an overturned provisional ruling"))
        else:
            findings.append(
                GateFinding(
                    rule="impact",
                    detail="chunk uses a superseded ruling in the affected scope",
                    outcome="block",
                )
            )
    for commitment_id, version in dict.fromkeys(
        (ref.commitment_id, ref.version) for ref in build.uses
    ):
        if (
            state.versions[commitment_id, version].state != "provisional"
            or state.current[commitment_id].state != "provisional"
        ):
            continue
        subject = state.identities[commitment_id].subject
        if subject is not None and subject.kind == "normative":
            findings.append(
                block(f"unresolved normative ruling {commitment_id}", commitment_id)
            )
        uses = len(state.usage[commitment_id, version])
        if uses > PROVISIONAL_CHUNK_LIMIT:
            findings.append(
                block(
                    f"provisional ruling {commitment_id} is used in {uses} chunks",
                    commitment_id,
                )
            )
    return findings


def _has_new_evidence(old: VersionChange, new: VersionChange) -> bool:
    quote = new.version.source_quote
    return bool(
        set(new.version.evidence) - set(old.version.evidence)
        or (
            quote is not None
            and quote != old.version.source_quote
            and f"source:{quote.sentence_id}" in new.version.evidence
        )
        or (
            {(row.clause_id, row.text) for row in new.brief_citations}
            - {(row.clause_id, row.text) for row in old.brief_citations}
        )
        or (
            {(row.commitment_id, row.version) for row in new.commitment_citations}
            - {(row.commitment_id, row.version) for row in old.commitment_citations}
        )
    )


def _confirmed_provisional(old: CommitmentVersion, new: CommitmentVersion) -> bool:
    return (
        old.state == "provisional"
        and new.state in BINDING_STATES
        and old.rendering == new.rendering
        and old.rule == new.rule
        and old.strength == new.strength
        and set(old.scope) == set(new.scope)
        and set(old.sentence_ids or ()) == set(new.sentence_ids or ())
    )


def _overturned_provisional(old: CommitmentVersion, new: CommitmentVersion) -> bool:
    return old.state == "provisional" and (
        new.state == "revoked"
        or old.rendering != new.rendering
        or old.rule != new.rule
        or old.strength != new.strength
        or not set(old.scope) <= set(new.scope)
        or not set(old.sentence_ids or ()) <= set(new.sentence_ids or ())
    )


def _lost_application(
    old: CommitmentVersion, new: CommitmentVersion
) -> Tombstone | None:
    if old.state == "revoked":
        return None
    scope = list(dict.fromkeys(old.scope))
    sites = None if old.sentence_ids is None else sorted(set(old.sentence_ids))
    if new.state != "revoked" and new.rendering == old.rendering:
        if sites is None:
            scope = [key for key in scope if key not in new.scope]
        else:
            assert new.sentence_ids is not None
            sites = sorted(set(sites) - set(new.sentence_ids))
    if not scope or sites == []:
        return None
    return Tombstone(
        rendering=old.rendering,
        scope=scope,
        commitment_id=old.commitment_id,
        version=old.version,
        sentence_ids=sites,
    )


def _applies_at(sites: list[str] | None, sentence_id: str | None) -> bool:
    return sentence_id is None or sites is None or sentence_id in sites


class Reader:
    """A monotonically advancing view over a ledger."""

    def __init__(self, ledger: Ledger):
        self._ledger = ledger
        self._version = 0

    def in_scope(
        self, scope: str, at: int | None = None, *, sentence_id: str | None = None
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
            and _applies_at(entry.sentence_ids, sentence_id)
        }
