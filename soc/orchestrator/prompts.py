"""Render and fingerprint the versioned translation templates."""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from string import Template

from soc.enforce.checker import Finding
from soc.memory import ChunkInput, ChunkOutput, DecisionChange, PromptContext
from soc.providers.models import Message

DEFAULT_PROMPTS = Path(__file__).resolve().parents[2] / "prompts"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def json_text(data: object) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class TranslationPrompts:
    def __init__(self, directory: Path = DEFAULT_PROMPTS) -> None:
        names = ("system", "user", "repair")
        self._templates = {
            name: (directory / f"translation.{name}.txt").read_text(encoding="utf-8")
            for name in names
        }

    def hashes(self) -> dict[str, str]:
        return {name: digest(value) for name, value in self._templates.items()}

    def render(
        self,
        chunk: ChunkInput,
        context: PromptContext,
        *,
        handover: PromptContext | None = None,
        previous: ChunkOutput | None = None,
        findings: Sequence[Finding] = (),
        revisions: Sequence[DecisionChange] = (),
    ) -> tuple[Message, ...]:
        source = [
            {
                "sentence_id": sentence.id,
                "text": sentence.reading_text
                if sentence.translatable
                else sentence.text,
                "translatable": sentence.translatable,
            }
            for sentence in chunk.sentences
        ]
        repair = ""
        if previous is not None:
            repair = Template(self._templates["repair"]).substitute(
                previous=previous.model_dump_json(),
                findings=json_text([asdict(finding) for finding in findings]),
                revisions=json_text(
                    [
                        {
                            "decision_id": change.current.ref.decision_id,
                            "previous": change.previous.value,
                            "current": change.current.value,
                            "scope_keys": sorted(change.current.scope_keys),
                        }
                        for change in revisions
                    ]
                ),
            )
        user = Template(self._templates["user"]).substitute(
            memory=context.text,
            handover=f"Executor handover\n{handover.text}" if handover else "",
            source=json_text(source),
            repair=repair,
        )
        return (
            Message(role="system", content=self._templates["system"]),
            Message(role="user", content=user),
        )
