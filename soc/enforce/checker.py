"""Deterministic rules shared by the translation workflows."""

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from soc.enforce.lemmatizer import (
    Lemmatizer,
    Span,
    TurkishStemLemmatizer,
    exact_matches,
    phrase_tokens,
)
from soc.episode import Brief, Sentence
from soc.memory import ChunkOutput, Decision, ForbiddenRendering, Strength
from soc.tasks.segmenter import BCP14

_NUMBER = re.compile(r"(?<!\w)[+−-]?(?>0[xX][0-9A-Fa-f]+|\d+(?:[.,]\d+)*)(?!\w)")

type RuleID = Literal["ids", "verbatim", "bcp14", "numbers", "term", "forbidden"]


@dataclass(frozen=True)
class Finding:
    rule: RuleID
    sentence_id: str
    detail: str
    span: Span | None = None
    decision_id: str | None = None
    brief_clause_ids: tuple[str, ...] = ()
    strength: Strength = "must"
    status: Literal["violation", "unknown"] = "violation"


@dataclass(frozen=True)
class NormativeRendering:
    keyword: str
    value: str
    brief_clause_id: str
    strength: Strength = "must"


def _numbers(sentence: Sentence, translated: str) -> list[Finding]:
    expected = Counter(match[0] for match in _NUMBER.finditer(sentence.reading_text))
    actual = Counter(match[0] for match in _NUMBER.finditer(translated))
    surplus = actual - expected
    wrong_spans = iter(
        Span(*match.span())
        for match in _NUMBER.finditer(translated)
        if surplus[match[0]]
    )
    return [
        Finding(
            "numbers",
            sentence.id,
            f"Missing {count} occurrence(s) of {number!r}",
            span=next(wrong_spans, None),
        )
        for number, count in (expected - actual).items()
    ]


def _normative(
    sentence: Sentence,
    translated: str,
    renderings: dict[str, NormativeRendering],
) -> list[Finding]:
    expected = Counter(match[0] for match in BCP14.finditer(sentence.reading_text))
    if not expected:
        return []
    phrases = {
        keyword: phrase_tokens(rule.value) for keyword, rule in renderings.items()
    }
    # Reserve longer phrases first. A positive marker nested inside a negative
    # phrase cannot satisfy a separate positive obligation.
    occurrences = [
        (span, phrase)
        for phrase in set(phrases.values())
        for span in exact_matches(" ".join(phrase), translated)
    ]
    occurrences.sort(
        key=lambda item: (-(item[0].end - item[0].start), item[0].start, item[1])
    )
    used: list[Span] = []
    found: Counter[tuple[str, ...]] = Counter()
    for span, phrase in occurrences:
        if all(span.end <= taken.start or span.start >= taken.end for taken in used):
            found[phrase] += 1
            used.append(span)

    findings = []
    for keyword, count in expected.items():
        rendering = renderings.get(keyword)
        if rendering is None:
            findings.append(
                Finding(
                    "bcp14",
                    sentence.id,
                    f"No approved rendering for {keyword}",
                    status="unknown",
                )
            )
            continue
        phrase = phrases[keyword]
        matched = min(found[phrase], count)
        found[phrase] -= matched
        if matched < count:
            findings.append(
                Finding(
                    "bcp14",
                    sentence.id,
                    f"Expected {count} rendering(s) of {keyword} as {rendering.value!r}; found {matched}",
                    brief_clause_ids=(rendering.brief_clause_id,),
                    strength=rendering.strength,
                )
            )
    return findings


class Checker:
    def __init__(self, lemmatizer: Lemmatizer | None = None) -> None:
        self.lemmatizer = (
            lemmatizer if lemmatizer is not None else TurkishStemLemmatizer()
        )

    def find_rendering(self, phrase: str, text: str) -> tuple[Span, ...]:
        return self.lemmatizer.find(phrase, text)

    def check(
        self,
        source: Sequence[Sentence],
        output: ChunkOutput,
        brief: Brief,
        *,
        decisions: Sequence[Decision] = (),
        forbidden: Sequence[ForbiddenRendering] = (),
        normative: Sequence[NormativeRendering] = (),
    ) -> tuple[Finding, ...]:
        """Check aligned sentences. Missing output spans are represented by None."""
        source_ids = {sentence.id for sentence in source}
        translated: dict[str, list[str]] = defaultdict(list)
        findings: list[Finding] = []
        for item in output.sentences:
            translated[item.sentence_id].append(item.text)
            if item.sentence_id not in source_ids:
                findings.append(
                    Finding(
                        "ids",
                        item.sentence_id,
                        "Unexpected sentence ID",
                        Span(0, len(item.text)),
                    )
                )
        clauses = {clause.id for clause in brief.clauses}
        candidates: dict[str, set[NormativeRendering]] = defaultdict(set)
        for rule in normative:
            if rule.brief_clause_id in clauses and rule.value.strip():
                candidates[rule.keyword].add(rule)
        approved = {
            keyword: next(iter(rules))
            for keyword, rules in candidates.items()
            if len(rules) == 1
        }
        for sentence in source:
            texts = translated[sentence.id]
            if len(texts) != 1:
                detail = "Missing sentence ID" if not texts else "Duplicate sentence ID"
                span = Span(0, len(texts[1])) if texts else None
                findings.append(Finding("ids", sentence.id, detail, span))
                continue
            text = texts[0]
            if not sentence.translatable:
                if text != sentence.text:
                    findings.append(
                        Finding(
                            "verbatim",
                            sentence.id,
                            "Changed verbatim text",
                            Span(0, len(text)),
                        )
                    )
                continue
            findings.extend(_normative(sentence, text, approved))
            findings.extend(_numbers(sentence, text))
            findings.extend(self._renderings(sentence, text, decisions, forbidden))
        return tuple(findings)

    def _renderings(
        self,
        sentence: Sentence,
        text: str,
        decisions: Sequence[Decision],
        forbidden: Sequence[ForbiddenRendering],
    ) -> list[Finding]:
        scopes = set(sentence.scope_keys)
        findings = []
        for decision in decisions:
            applicable = decision.scope_keys & scopes
            if not applicable:
                continue
            # Normative interpretations attached only to BCP 14 keywords are
            # enforced by their approved modal rendering in the brief.
            if decision.kind == "non_lexical" and all(
                BCP14.fullmatch(key) for key in applicable
            ):
                continue
            if not self.find_rendering(decision.value, text):
                findings.append(
                    Finding(
                        "term",
                        sentence.id,
                        f"Missing approved rendering {decision.value!r}",
                        decision_id=decision.ref.decision_id,
                        brief_clause_ids=decision.brief_clause_ids,
                        strength=decision.strength,
                    )
                )
        for rule in forbidden:
            if rule.scope_keys & scopes:
                findings.extend(
                    Finding(
                        "forbidden",
                        sentence.id,
                        f"Forbidden rendering {rule.rendering!r}",
                        span=span,
                        decision_id=rule.decision_id,
                        strength=rule.strength,
                    )
                    for span in self.find_rendering(rule.rendering, text)
                )
        return findings
