"""Glossary, style guide, and note retrieval for the RAG workflow."""

import json
from dataclasses import dataclass, replace

from soc.baseline.retrieval import Embedder, HybridIndex, RetrievalConfig
from soc.enforce.checker import Checker
from soc.episode import Approval, Brief, Episode, Revision, approval_sites
from soc.memory import (
    ChunkInput,
    ChunkOutput,
    Decision,
    DecisionChange,
    DecisionRef,
    ForbiddenRendering,
    PromptContext,
    TermNote,
    Translation,
)


@dataclass(frozen=True)
class Note:
    decision: Decision
    rationale: str

    def content(self) -> str:
        return "\n".join(
            (*sorted(self.decision.scope_keys), self.decision.value, self.rationale)
        )


class RAGMemory:
    def __init__(
        self,
        embedder: Embedder,
        checker: Checker,
        *,
        retrieval: RetrievalConfig | None = None,
        episode: Episode | None = None,
    ) -> None:
        self._index = HybridIndex(
            embedder, retrieval if retrieval is not None else RetrievalConfig()
        )
        self._checker = checker
        self._episode = episode
        self._notes: dict[str, list[Note]] = {}
        self._outputs: dict[int, tuple[Translation, ...]] = {}

    def history(self, decision_id: str) -> tuple[Note, ...]:
        return tuple(self._notes.get(decision_id, ()))

    def _current(self) -> tuple[Note, ...]:
        return tuple(self._notes[key][-1] for key in sorted(self._notes))

    def _append(self, note: Note) -> None:
        key = note.decision.ref.decision_id
        self._index.upsert(key, note.content())
        self._notes.setdefault(key, []).append(note)

    def remember_term(self, note: TermNote) -> None:
        if note.decision_id in self._notes:
            return
        sites = frozenset(note.sentence_ids)
        for existing in self._current():
            decision = existing.decision
            if note.scope_key in decision.scope_keys and (
                decision.sentence_ids is None or sites & decision.sentence_ids
            ):
                return
        decision = Decision(
            DecisionRef(note.decision_id, 1),
            note.rendering,
            frozenset({note.scope_key}),
            "term",
            sentence_ids=sites,
        )
        self._append(Note(decision, "Term agreed by the translation team."))

    def record_approval(self, approval: Approval, brief: Brief) -> Decision:
        key = approval.decision_id
        if key in self._notes:
            raise ValueError(f"decision already approved: {key}")
        clauses = {clause.id: clause.text for clause in brief.clauses}
        unknown = set(approval.brief_clause_ids) - clauses.keys()
        if unknown:
            raise ValueError(
                f"approval cites unknown brief clauses: {', '.join(sorted(unknown))}"
            )
        decision = Decision(
            DecisionRef(key, 1),
            approval.value,
            frozenset(approval.scope_keys),
            approval.kind,
            tuple(approval.brief_clause_ids),
            sentence_ids=None
            if self._episode is None
            else frozenset(
                sentence.id for sentence in approval_sites(self._episode, approval)
            ),
        )
        rationale = " ".join(
            clauses[clause_id] for clause_id in decision.brief_clause_ids
        )
        self._append(Note(decision, rationale or "Approved by the principal."))
        return decision

    def apply_revision(self, revision: Revision, brief: Brief) -> DecisionChange:
        key = revision.decision_id
        if key not in self._notes:
            raise ValueError(f"cannot revise an unapproved decision: {key}")
        previous = self._notes[key][-1].decision
        active = {clause.id for clause in brief.clauses}
        current = replace(
            previous,
            ref=DecisionRef(key, previous.ref.version + 1),
            value=revision.value,
            brief_clause_ids=tuple(
                clause_id
                for clause_id in previous.brief_clause_ids
                if clause_id in active
            ),
        )
        rationale = " ".join(
            clause.text
            for clause in brief.clauses
            if self._checker.find_rendering(current.value, clause.text)
        )
        self._append(
            Note(current, rationale or "Approved revision under the active brief.")
        )
        return DecisionChange(previous, current)

    def decisions_for(self, chunk: ChunkInput) -> tuple[Decision, ...]:
        scope_keys = chunk.scope_keys
        return tuple(
            note.decision
            for note in self._current()
            if note.decision.scope_keys & scope_keys
        )

    def forbidden_for(self, chunk: ChunkInput) -> tuple[ForbiddenRendering, ...]:
        return ()

    def context_for(self, chunk: ChunkInput, brief: Brief) -> PromptContext:
        current = self._current()
        query = "\n".join(sentence.reading_text for sentence in chunk.sentences)
        retrieved = [self._notes[key][-1] for key in self._index.search(query)]
        text = _pinned(current, brief)
        text += _section("Retrieved notes", [_note_data(note) for note in retrieved])
        return PromptContext(text, tuple(note.decision.ref for note in current))

    def after_output(
        self, chunk: ChunkInput, output: ChunkOutput, context: PromptContext
    ) -> None:
        self._outputs[chunk.position] = tuple(output.sentences)

    def affected_chunks(self, change: DecisionChange) -> tuple[int, ...]:
        old = change.previous.value
        if old == change.current.value:
            return ()
        return tuple(
            position
            for position, sentences in sorted(self._outputs.items())
            if any(self._checker.find_rendering(old, item.text) for item in sentences)
        )

    def executor_change(self, brief: Brief) -> PromptContext:
        current = self._current()
        text = _pinned(current, brief)
        text += _section("Current note index", [_note_data(note) for note in current])
        text += _section(
            "Partial translation",
            [
                {
                    "chunk": position,
                    "sentences": [item.model_dump() for item in sentences],
                }
                for position, sentences in sorted(self._outputs.items())
            ],
        )
        return PromptContext(text, tuple(note.decision.ref for note in current))


def _decision_data(decision: Decision) -> dict[str, object]:
    data: dict[str, object] = {
        "decision_id": decision.ref.decision_id,
        "version": decision.ref.version,
        "value": decision.value,
        "scope_keys": sorted(decision.scope_keys),
        "kind": decision.kind,
        "strength": decision.strength,
    }
    if decision.sentence_ids is not None:
        data["sentence_ids"] = sorted(decision.sentence_ids)
    return data


def _note_data(note: Note) -> dict[str, object]:
    return _decision_data(note.decision) | {"rationale": note.rationale}


def _section(title: str, data: object) -> str:
    return f"{title}\n{json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n"


def _pinned(notes: tuple[Note, ...], brief: Brief) -> str:
    terms = []
    guidance = []
    for note in notes:
        decision = note.decision
        if decision.kind == "term":
            terms.append(_decision_data(decision))
        else:
            guidance.append(_decision_data(decision))
    return _section("Glossary", terms) + _section(
        "Style guide",
        {
            "brief": [clause.model_dump() for clause in brief.clauses],
            "decisions": guidance,
        }
        | (
            {"normative": brief.public_data()["normative"]}
            if "normative" in brief.public_data()
            else {}
        ),
    )
