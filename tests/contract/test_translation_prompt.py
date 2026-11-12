import json
from pathlib import Path

from soc.enforce.checker import Finding
from soc.episode import Sentence
from soc.memory import (
    ChunkInput,
    ChunkOutput,
    Decision,
    DecisionChange,
    DecisionRef,
    PromptContext,
    Translation,
)
from soc.orchestrator.prompts import TranslationPrompts

SNAPSHOT = Path(__file__).resolve().parents[1] / "fixtures" / "translation-prompt.json"


def snapshot_text() -> str:
    sentence = Sentence(
        id="9700-1-s01",
        section="1",
        text="Protect the access token.  ",
        reading_text="Protect the access token.",
        scope_keys=["access token"],
    )
    verbatim = Sentence(
        id="9700-1-s02",
        section="1",
        text="  GET /token\n",
        reading_text="GET /token",
        scope_keys=[],
        translatable=False,
    )
    refs = (DecisionRef("D1", 1), DecisionRef("D1", 2))
    scope = frozenset({"access token"})
    change = DecisionChange(
        Decision(refs[0], "erişim jetonu", scope, "term"),
        Decision(refs[1], "erişim belirteci", scope, "term"),
    )
    messages = TranslationPrompts().render(
        ChunkInput(1, (sentence, verbatim)),
        PromptContext("Glossary\nD1 v2: erişim belirteci\n", (refs[1],)),
        handover=PromptContext("Continue the partial translation.\n", ()),
        previous=ChunkOutput(
            sentences=[
                Translation(sentence_id=sentence.id, text="Erişim jetonunu koruyun."),
                Translation(sentence_id=verbatim.id, text=verbatim.text),
            ]
        ),
        findings=[
            Finding(
                "term", sentence.id, "Use the approved rendering.", decision_id="D1"
            )
        ],
        revisions=[change],
    )
    return (
        json.dumps(
            [message.model_dump() for message in messages], ensure_ascii=False, indent=2
        )
        + "\n"
    )


def test_versioned_translation_prompt_matches_golden_snapshot():
    assert snapshot_text() == SNAPSHOT.read_text(encoding="utf-8")
