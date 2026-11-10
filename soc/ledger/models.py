"""Typed commitment records and ledger event payloads."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints

Text = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
Version = Annotated[int, Field(ge=1)]
BaseVersion = Annotated[int, Field(ge=0)]
State = Literal[
    "proposed",
    "provisional",
    "ratified",
    "contested",
    "amended",
    "superseded",
    "revoked",
]
Consistency = Literal["ONE", "QUORUM", "ALL", "SERIAL"]
EventType = Literal[
    "Ratified",
    "ConflictRaised",
    "Revised",
    "Superseded",
    "Revoked",
    "Tombstoned",
    "BriefChanged",
    "ChunkBuilt",
    "ExecutorRebound",
    "FlagRaised",
    "DecisionDeclared",
    "DecisionCaptured",
    "CrossExamTurn",
    "VoteCast",
    "StaleMarked",
    "GateDecision",
    "PrincipalAnswered",
    "StandingUpdated",
    "SanctionApplied",
]
Transition = Literal["Revised", "Superseded", "Revoked"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Actor(Record):
    agent: Text
    model: Text
    role: Text


class Commitment(Record):
    id: Text
    operation: Text
    owner: Text
    debtor: Text
    creditor: Text


class Quote(Record):
    sentence_id: Text
    text: Text


class Rule(Record):
    kind: Text
    parameters: dict[str, JsonValue]


class Ruling(Record):
    scope: Annotated[list[Text], Field(min_length=1)]
    rendering: Text
    source_quote: Quote | None
    rule: Rule | None
    rationale: Text
    evidence: list[
        Annotated[str, StringConstraints(pattern=r"^(source|brief|commitment):\S+$")]
    ]
    proposer: Text
    ratifier: Text
    consistency: Consistency
    state: State


class CommitmentVersion(Ruling):
    commitment_id: Text
    version: Version
    valid_from: Version
    valid_until: Version | None = None


class VersionChange(Record):
    commitment: Commitment
    version: CommitmentVersion
    reason: Text


class Conflict(Record):
    expected: BaseVersion
    actual: BaseVersion


class Tombstone(Record):
    rendering: Text
    scope: list[Text]
    commitment_id: Text
    version: Version


class BriefChanged(Record):
    clauses: dict[Text, Text]


class VersionRef(Record):
    commitment_id: Text
    version: Version


class ChunkBuilt(Record):
    ledger_version: BaseVersion
    uses: list[VersionRef]
    decisions: dict[Text, Text]


class ExecutorRebound(Record):
    old_model: Text
    new_model: Text
    ledger_version: BaseVersion


class FlagRaised(Record):
    reason: Text


class DecisionDeclared(Record):
    ruling: Ruling


class DecisionCaptured(Record):
    ruling: Ruling
    evidence: list[Text]


class CrossExamTurn(Record):
    question: Text
    answer: Text


class VoteCast(Record):
    decision_id: Text
    choice: Text


class StaleMarked(Record):
    chunk_ids: list[Text]


class GateDecision(Record):
    accepted: bool
    reason: Text


class PrincipalAnswered(Record):
    question_id: Text
    answer: Text


class StandingUpdated(Record):
    agent: Text
    standing: float


class SanctionApplied(Record):
    agent: Text
    reason: Text


class Event(Record):
    seq: Version
    event_id: Text
    run_id: Text
    time: Text
    type: EventType
    actor: Actor
    commitment_id: Text | None
    base_version: BaseVersion | None
    new_version: Version | None
    ledger_version: Version
    chunk_id: Text | None
    payload: dict[str, JsonValue]
    prev_hash: str
    hash: str
