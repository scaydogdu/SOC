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
BINDING_STATES: frozenset[State] = frozenset({"ratified", "amended"})
Consistency = Literal["ONE", "QUORUM", "ALL", "SERIAL"]
TRIAD_APPROVAL_COUNTS = {"ONE": 1, "QUORUM": 2, "ALL": 3}
AuthorityClass = Literal["principal", "triad"]
Strength = Literal["must", "should", "may"]
EventType = Literal[
    "Provisional",
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
    "FlagResolved",
    "DecisionDeclared",
    "DecisionCaptured",
    "CrossExamTurn",
    "VoteCast",
    "StaleMarked",
    "StaleDetected",
    "GateDecision",
    "PrincipalAnswered",
    "StandingUpdated",
    "SanctionApplied",
    "HeldResolved",
]
Transition = Literal["Revised", "Superseded", "Revoked"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Actor(Record):
    agent: Text
    model: Text
    role: Text


class DecisionSubject(Record):
    kind: Text
    key: Text


class Commitment(Record):
    id: Text
    operation: Text
    owner: Text
    debtor: Text
    creditor: Text
    subject: DecisionSubject | None = None
    authority: AuthorityClass | None = None


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
    strength: Strength = "must"
    sentence_ids: Annotated[list[Text], Field(min_length=1)] | None = None


class CommitmentVersion(Ruling):
    commitment_id: Text
    version: Version
    valid_from: Version
    valid_until: Version | None = None


class BriefCitation(Record):
    clause_id: Text
    text: Text
    ledger_version: BaseVersion


class VersionRef(Record):
    commitment_id: Text
    version: Version


class DecisionUse(Record):
    ref: VersionRef
    sentence_id: Text


class OperationAuthority(Record):
    operation: Text
    principal: Text


class RulingApproval(Record):
    operation: Text
    triad: list[Text] = Field(default_factory=list)
    approvers: list[Text]


class VersionChange(Record):
    commitment: Commitment
    version: CommitmentVersion
    reason: Text
    brief_citations: list[BriefCitation] = Field(default_factory=list)
    commitment_citations: list[VersionRef] = Field(default_factory=list)
    approval: RulingApproval | None = None


class Conflict(Record):
    expected: BaseVersion
    actual: BaseVersion


class RulingOverlap(Record):
    commitment_id: Text
    version: Version
    sentence_ids: Annotated[list[Text], Field(min_length=1)]


class RulingConflict(Record):
    candidate_id: Text
    subject: DecisionSubject
    overlaps: Annotated[list[RulingOverlap], Field(min_length=1)]


class Tombstone(Record):
    rendering: Text
    scope: list[Text]
    commitment_id: Text
    version: Version
    sentence_ids: list[Text] | None = None


class BriefChanged(Record):
    clauses: dict[Text, Text]
    after_chunk: Version | None = None


class ChunkBuilt(Record):
    ledger_version: BaseVersion
    uses: list[VersionRef]
    decisions: dict[Text, Text]
    build_number: Version = 1
    # Older append-only events predate output references.
    output_ref: Text | None = None
    use_sites: list[DecisionUse] = Field(default_factory=list)


class ExecutorRebound(Record):
    old_model: Text
    new_model: Text
    ledger_version: BaseVersion


class FlagRaised(Record):
    reason: Text
    flag_id: Text | None = None
    sentence_id: Text | None = None


class ResolutionBallot(Record):
    voter: Text
    choice: Literal["first", "second"] | None


class ResolutionTrace(Record):
    sentence_id: Text
    decision_kind: Text
    required: Consistency
    consistency: Consistency
    rendering: Text
    approvers: list[Text]
    ballots: list[ResolutionBallot]
    vote_weights: dict[Text, Text] = Field(default_factory=dict)
    evidence_supporters: list[Text] = Field(default_factory=list)


class FlagResolved(Record):
    flag_id: Text
    trace: ResolutionTrace | None = None


class DecisionDeclared(DecisionUse):
    verified: bool


class DecisionCaptured(DecisionUse):
    excerpt: Text
    start: BaseVersion
    end: Version


class CrossExamTurn(Record):
    question: Text
    answer: Text


class RulingBallot(Record):
    voter: Text
    choice: str | None


class VoteCast(RulingBallot):
    decision_id: Text


class StaleMarked(Record):
    chunk_ids: list[Text]
    overturned: VersionRef | None = None
    replaced_by: VersionRef | None = None


RepairPath = Literal["review", "gate", "final_sweep"]


class StaleDetected(Record):
    path: RepairPath
    build_number: Version


GateOutcome = Literal["block", "warning", "allowed", "unknown", "held", "unverified"]


class GateFinding(Record):
    rule: Text
    detail: Text
    outcome: GateOutcome
    sentence_id: str | None = None
    commitment_id: Text | None = None
    strength: Strength | None = None
    start: BaseVersion | None = None
    end: BaseVersion | None = None
    check_id: str | None = None
    unknown_cause: Literal["configuration", "runtime"] | None = None
    brief_clause_ids: list[str] = Field(default_factory=list)


class GateDecision(Record):
    accepted: bool
    reason: Text
    build_number: Version | None = None
    findings: list[GateFinding] = Field(default_factory=list)

    @property
    def held(self) -> bool:
        return any(row.outcome in {"held", "unverified"} for row in self.findings)


class MeaningEvidence(Record):
    reference: Text
    quote: Text


class HeldResolution(Record):
    check_id: Text
    chunk_id: Text
    build_number: Version
    resolver: Actor
    verdict: Literal["compliant", "violation", "unknown"]
    rationale: Text
    evidence: list[MeaningEvidence]
    input_tokens: BaseVersion = 0
    output_tokens: BaseVersion = 0
    estimated_cost_usd: float = Field(default=0, ge=0, allow_inf_nan=False)


class PrincipalAnswered(Record):
    question_id: Text
    answer: Text
    question_hash: Text | None = None


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
