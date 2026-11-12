"""Runtime contract shared by the RAG workflow and the future ledger adapter."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, PositiveInt

from soc.episode import Approval, Brief, Revision, Sentence

type Strength = Literal["must", "should", "may"]
type DecisionKind = Literal["term", "non_lexical", "scope_limited"]


class Translation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    sentence_id: str
    text: str


class ChunkOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    sentences: list[Translation]


@dataclass(frozen=True)
class ChunkInput:
    position: PositiveInt
    sentences: tuple[Sentence, ...]

    @property
    def scope_keys(self) -> frozenset[str]:
        return frozenset(
            key for sentence in self.sentences for key in sentence.scope_keys
        )


@dataclass(frozen=True)
class DecisionRef:
    decision_id: str
    version: PositiveInt


@dataclass(frozen=True)
class Decision:
    ref: DecisionRef
    value: str
    scope_keys: frozenset[str]
    kind: DecisionKind
    brief_clause_ids: tuple[str, ...] = ()
    strength: Strength = "must"


@dataclass(frozen=True)
class DecisionChange:
    previous: Decision
    current: Decision


@dataclass(frozen=True)
class ForbiddenRendering:
    rendering: str
    scope_keys: frozenset[str]
    decision_id: str
    strength: Strength = "must"


@dataclass(frozen=True)
class PromptContext:
    text: str
    delivered: tuple[DecisionRef, ...]


class Memory(Protocol):
    def record_approval(self, approval: Approval, brief: Brief) -> Decision: ...

    def apply_revision(self, revision: Revision, brief: Brief) -> DecisionChange: ...

    def context_for(self, chunk: ChunkInput, brief: Brief) -> PromptContext: ...

    def decisions_for(self, chunk: ChunkInput) -> Sequence[Decision]: ...

    def forbidden_for(self, chunk: ChunkInput) -> Sequence[ForbiddenRendering]: ...

    def after_output(
        self, chunk: ChunkInput, output: ChunkOutput, context: PromptContext
    ) -> None: ...

    def affected_chunks(self, change: DecisionChange) -> Sequence[int]: ...

    def executor_change(self, brief: Brief) -> PromptContext: ...
