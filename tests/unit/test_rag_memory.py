import json
import socket
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from soc.baseline.memory import RAGMemory
from soc.baseline.retrieval import (
    BGE_M3_MODEL,
    BGE_M3_REVISION,
    BGEM3Embedder,
    HashEmbedder,
    HybridIndex,
    RetrievalConfig,
)
from soc.enforce.checker import Checker
from soc.episode import Approval, Brief, Clause, Revision, Sentence
from soc.memory import ChunkInput, ChunkOutput, DecisionRef, PromptContext, Translation

BRIEF = Brief(clauses=[Clause(id="B1", text="Use the approved terminology.")])
UPDATED = Brief(
    clauses=[Clause(id="B2", text="Use erişim belirteci for access token.")]
)


def approval(key, value, scope, *, kind="term", clauses=("B1",)):
    return Approval(
        decision_id=key,
        value=value,
        scope_keys=[scope],
        kind=kind,
        chunk=1,
        brief_clause_ids=list(clauses),
    )


def chunk(position, english, scopes=()):
    return ChunkInput(
        position,
        (
            Sentence(
                id=f"9700-1-s{position:02}",
                section="1",
                text=english,
                reading_text=english,
                scope_keys=list(scopes),
            ),
        ),
    )


def parts(context: PromptContext):
    lines = context.text.splitlines()
    return {
        lines[index]: json.loads(lines[index + 1]) for index in range(0, len(lines), 2)
    }


def test_rag_pins_all_decisions_and_retrieves_relevant_rationale():
    memory = RAGMemory(
        HashEmbedder(), Checker(), retrieval=RetrievalConfig(top_k=1, dense_weight=0)
    )
    memory.record_approval(approval("D1", "erişim jetonu", "access token"), BRIEF)
    memory.record_approval(approval("D2", "yenileme belirteci", "refresh token"), BRIEF)
    memory.record_approval(
        approval("D3", "Korunması gereklidir.", "MUST", kind="non_lexical", clauses=()),
        BRIEF,
    )
    target = chunk(2, "An access token permits access.", ["access token"])
    context = memory.context_for(target, BRIEF)
    values = parts(context)
    assert context.delivered == (
        DecisionRef("D1", 1),
        DecisionRef("D2", 1),
        DecisionRef("D3", 1),
    )
    assert [term["decision_id"] for term in values["Glossary"]] == ["D1", "D2"]
    assert values["Style guide"]["brief"] == [
        {"id": "B1", "text": BRIEF.clauses[0].text}
    ]
    assert values["Style guide"]["decisions"][0]["decision_id"] == "D3"
    (note,) = values["Retrieved notes"]
    assert note["decision_id"] == "D1"
    assert note["rationale"] == BRIEF.clauses[0].text
    assert memory.history("D3")[0].rationale == "Approved by the principal."

    change = memory.apply_revision(
        Revision(decision_id="D1", value="erişim belirteci"), UPDATED
    )
    revised = parts(memory.context_for(target, UPDATED))
    (note,) = revised["Retrieved notes"]
    assert note["value"] == "erişim belirteci" and note["version"] == 2
    assert "Use erişim belirteci" in note["rationale"]
    assert revised["Style guide"]["brief"] == [
        {"id": "B2", "text": UPDATED.clauses[0].text}
    ]
    assert change.current.brief_clause_ids == ()
    assert [old.decision.value for old in memory.history("D1")] == [
        "erişim jetonu",
        "erişim belirteci",
    ]
    assert memory.history("unknown") == ()


def test_revision_search_uses_output_text_without_usage_records():
    memory = RAGMemory(HashEmbedder(), Checker())
    first = chunk(1, "An early sentence.")
    second = chunk(2, "An access token.", ["access token"])
    third = chunk(3, "A refresh token.", ["refresh token"])
    # Old wording can appear before approval and even outside the declared scope.
    for built, text in (
        (third, "Erişim jetonları."),
        (first, "Erişim jetonunu koruyun."),
        (second, "Erişim belirtecini koruyun."),
    ):
        value = ChunkOutput(
            sentences=[Translation(sentence_id=built.sentences[0].id, text=text)]
        )
        memory.after_output(built, value, PromptContext("", ()))
        value.sentences.clear()  # A saved build is independent of the caller's list.
    memory.record_approval(approval("D1", "erişim jetonu", "access token"), BRIEF)
    change = memory.apply_revision(
        Revision(decision_id="D1", value="erişim belirteci"), UPDATED
    )
    assert memory.affected_chunks(change) == (1, 3)
    assert memory.forbidden_for(second) == ()
    handover = memory.executor_change(UPDATED)
    assert handover.delivered == (DecisionRef("D1", 2),)
    values = parts(handover)
    assert values["Current note index"][0]["value"] == "erişim belirteci"
    assert [item["chunk"] for item in values["Partial translation"]] == [1, 2, 3]
    assert (
        values["Partial translation"][0]["sentences"][0]["text"]
        == "Erişim jetonunu koruyun."
    )
    unchanged = memory.apply_revision(
        Revision(decision_id="D1", value="erişim belirteci"), UPDATED
    )
    assert memory.affected_chunks(unchanged) == ()


@pytest.mark.parametrize(
    ("weight", "first"),
    [(0, "lexical"), (0.25, "lexical"), (0.75, "semantic"), (1, "semantic")],
)
def test_hybrid_mix_can_rank_dense_and_sparse_matches(weight, first):
    class CrossLanguage:
        def embed(self, text):
            return (3.0, 0.0) if text in {"erişim", "access"} else (0.0, 5.0)

    index = HybridIndex(CrossLanguage(), RetrievalConfig(top_k=1, dense_weight=weight))
    assert index.search("access") == ()
    index.upsert("lexical", "access word")
    index.upsert("semantic", "erişim")
    assert index.search("access") == (first,)


def test_index_replaces_text_and_breaks_ties_by_id():
    index = HybridIndex(HashEmbedder(), RetrievalConfig(top_k=2, dense_weight=0))
    index.upsert("D2", "Jetonu")
    index.upsert("D1", "Belirteci")
    assert index.search("jetonu") == ("D2", "D1")
    index.upsert("D2", "Belirteci")
    assert index.search("jetonu") == ("D1", "D2")
    index.upsert("D3", "")
    assert index.search("") == ("D1", "D2")
    disabled = HybridIndex(HashEmbedder(), RetrievalConfig(top_k=0))
    disabled.upsert("D1", "content")
    assert disabled.search("content") == ()


def test_invalid_revision_leaves_index_and_history_unchanged():
    class InvalidEmbedding:
        def embed(self, text):
            if "dimensions" in text:
                return (1.0, 0.0, 0.0)
            if "nan" in text:
                return (float("nan"), 0.0)
            return (1.0, 0.0)

    memory = RAGMemory(InvalidEmbedding(), Checker())
    revision = Revision(decision_id="D1", value="updated")
    with pytest.raises(ValueError, match="unapproved"):
        memory.apply_revision(revision, BRIEF)
    with pytest.raises(ValueError, match="unknown brief"):
        memory.record_approval(
            approval("D1", "start", "token", clauses=("bad",)), BRIEF
        )
    original = approval("D1", "start", "token")
    memory.record_approval(original, BRIEF)
    with pytest.raises(ValueError, match="already approved"):
        memory.record_approval(original, BRIEF)
    for value, message in (("dimensions", "dimensions"), ("nan", "finite")):
        with pytest.raises(ValueError, match=message):
            memory.apply_revision(Revision(decision_id="D1", value=value), BRIEF)
    context = memory.context_for(chunk(2, "token"), BRIEF)
    assert context.delivered == (DecisionRef("D1", 1),)
    assert len(memory.history("D1")) == 1
    assert parts(context)["Retrieved notes"][0]["value"] == "start"
    with pytest.raises(ValidationError):
        RetrievalConfig(dense_weight=1.1)


@pytest.mark.parametrize("local_only", [True, False])
def test_bge_adapter_loads_exact_model_revision(monkeypatch, local_only):
    cached = "/tmp/bge-m3-snapshot"

    def snapshot_download(**options):
        assert local_only
        assert options == {
            "repo_id": BGE_M3_MODEL,
            "revision": BGE_M3_REVISION,
            "local_files_only": True,
        }
        return cached

    class Encoder:
        def __init__(self, model, **options):
            assert model == (cached if local_only else BGE_M3_MODEL)
            assert options == {
                "revision": None if local_only else BGE_M3_REVISION,
                "device": "cpu",
                "local_files_only": local_only,
                "trust_remote_code": False,
                "model_kwargs": {"weights_only": True, "use_safetensors": False},
            }

        def encode(self, text, **options):
            assert text == "erişim belirteci"
            assert options == {"normalize_embeddings": True, "show_progress_bar": False}
            return [0.6, 0.8]

    libraries = {
        "sentence_transformers": SimpleNamespace(SentenceTransformer=Encoder),
        "huggingface_hub": SimpleNamespace(snapshot_download=snapshot_download),
    }
    monkeypatch.setattr("soc.baseline.retrieval.import_module", libraries.__getitem__)
    assert BGEM3Embedder(local_files_only=local_only).embed("erişim belirteci") == (
        0.6,
        0.8,
    )


@pytest.mark.nightly
def test_local_bge_model_returns_normalized_vectors(monkeypatch):
    from math import hypot

    lookups = []

    def unexpected_lookup(*args, **kwargs):
        lookups.append(args)
        raise AssertionError("local checkpoint attempted DNS")

    monkeypatch.setattr(socket, "getaddrinfo", unexpected_lookup)
    monkeypatch.setattr(socket, "gethostbyname", unexpected_lookup)
    embedder = BGEM3Embedder()
    vector = embedder.embed("access token")
    assert len(vector) == 1024
    assert hypot(*vector) == pytest.approx(1.0, abs=1e-6)
    assert embedder.embed("erişim belirteci") != vector
    assert lookups == []
