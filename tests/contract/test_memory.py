"""Shared behavior for each runtime memory implementation."""

import pytest

from soc.baseline.memory import RAGMemory
from soc.baseline.retrieval import HashEmbedder
from soc.enforce.checker import Checker
from soc.episode import Approval, Brief, Clause, Revision, Sentence
from soc.memory import ChunkInput, ChunkOutput, DecisionRef, Memory, Translation

BRIEF = Brief(clauses=[Clause(id="B1", text="Use protocol terminology.")])
CHANGED_BRIEF = Brief(
    clauses=[*BRIEF.clauses, Clause(id="B2", text="Use erişim belirteci.")]
)
APPROVAL = Approval(
    decision_id="D1",
    value="erişim jetonu",
    scope_keys=["access token"],
    kind="term",
    chunk=1,
    brief_clause_ids=["B1"],
)
REVISION = Revision(decision_id="D1", value="erişim belirteci")


def chunk(position: int, scope: str = "access token") -> ChunkInput:
    english = f"Protect the {scope}."
    return ChunkInput(
        position,
        (
            Sentence(
                id=f"9700-1-s{position:02}",
                section="1",
                text=english,
                reading_text=english,
                scope_keys=[scope],
            ),
        ),
    )


def output(chunk: ChunkInput, text: str) -> ChunkOutput:
    return ChunkOutput(
        sentences=[Translation(sentence_id=chunk.sentences[0].id, text=text)]
    )


def rag() -> Memory:
    return RAGMemory(HashEmbedder(), Checker())


@pytest.fixture(params=[rag], ids=["rag"])
def memory(request: pytest.FixtureRequest) -> Memory:
    return request.param()


def test_memory_delivers_approved_and_revised_values(memory: Memory):
    target = chunk(2)
    assert not memory.decisions_for(target)
    before = memory.context_for(target, BRIEF)
    assert before.delivered == ()
    assert APPROVAL.value not in before.text
    first = memory.record_approval(APPROVAL, BRIEF)
    assert first.ref == DecisionRef("D1", 1)
    assert first.scope_keys == frozenset({"access token"})
    assert tuple(memory.decisions_for(target)) == (first,)
    assert not memory.decisions_for(chunk(3, "refresh token"))
    context = memory.context_for(target, BRIEF)
    assert context.delivered == (first.ref,)
    assert first.value in context.text

    change = memory.apply_revision(REVISION, CHANGED_BRIEF)
    assert change.previous == first
    assert change.current.ref == DecisionRef("D1", 2)
    assert change.current.scope_keys == first.scope_keys
    assert tuple(memory.decisions_for(target)) == (change.current,)
    current = memory.context_for(target, CHANGED_BRIEF)
    assert current.delivered == (change.current.ref,)
    assert REVISION.value in current.text
    assert APPROVAL.value not in current.text


def test_memory_can_find_and_record_repaired_output(memory: Memory):
    memory.record_approval(APPROVAL, BRIEF)
    target = chunk(2)
    context = memory.context_for(target, BRIEF)
    memory.after_output(target, output(target, "Erişim jetonunu koruyun."), context)
    change = memory.apply_revision(REVISION, CHANGED_BRIEF)
    assert target.position in memory.affected_chunks(change)
    revised_context = memory.context_for(target, CHANGED_BRIEF)
    memory.after_output(
        target, output(target, "Erişim belirtecini koruyun."), revised_context
    )
    assert target.position not in memory.affected_chunks(change)


def test_executor_change_preserves_active_decision(memory: Memory):
    active = memory.record_approval(APPROVAL, BRIEF)
    context = memory.executor_change(BRIEF)
    assert active.ref in context.delivered
    assert active.value in context.text
    assert tuple(memory.decisions_for(chunk(3))) == (active,)
