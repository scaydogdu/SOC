"""Phrase matching with Turkish inflection and original output offsets."""

import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

_WORD = r"[\w\u0300-\u036f]+"
_TOKEN = re.compile(rf"{_WORD}(?:['’]{_WORD})?|[^\w\s]")
_VOWELS = "aeıioöuü"
_SOFTENED = {"p": "b", "t": "d", "ç": "c", "k": "ğg"}
_INFLECTION = re.compile(
    r"(?:l[ae]r)?"
    r"(?:[ıiuü]m(?:[ıiuü]z)?|[ıiuü]n(?:[ıiuü]z)?|m(?:[ıiuü]z)?|n(?:[ıiuü]z)?"
    r"|s?[ıiuü]|l[ae]r[ıi])?"
    r"(?:[yn]?[aeıiuü]|[dt][ae]n?|n?[ıiuü]n|y?l[ae])?"
)
_COMPOUND_PLURAL = re.compile(r"l[ae]r[ıi](?:n[aeıiuü]|n[dt][ae]n?|n[ıiuü]n|yl[ae])?")
_TURKISH_CASE = str.maketrans({"I": "ı", "İ": "i"})


@dataclass(frozen=True)
class Span:
    start: int
    end: int


class Lemmatizer(Protocol):
    def find(self, phrase: str, text: str) -> tuple[Span, ...]: ...


def _normalize(word: str) -> str:
    return (
        unicodedata.normalize("NFC", word)
        .translate(_TURKISH_CASE)
        .lower()
        .replace("’", "'")
    )


def _tokens(text: str) -> Iterator[tuple[str, Span]]:
    for token in _TOKEN.finditer(text):
        yield _normalize(token[0]), Span(*token.span())


def phrase_tokens(text: str) -> tuple[str, ...]:
    return tuple(word for word, _ in _tokens(text))


def _softenings(word: str) -> Iterator[str]:
    for consonant in _SOFTENED.get(word[-1], ""):
        yield word[:-1] + consonant


def _inflected(base: str, word: str, *, compound: bool) -> bool:
    if base == word:
        return True
    if len(base) < 2 or not base.isalpha():
        return False
    plain = word
    if plain.startswith(base):
        suffix = plain[len(base) :].removeprefix("'")
        if suffix and _INFLECTION.fullmatch(suffix):
            return True
    for stem in _softenings(base):
        if plain.startswith(stem):
            suffix = plain[len(stem) :]
            if suffix and suffix[0] in _VOWELS and _INFLECTION.fullmatch(suffix):
                return True
    # A noun compound may pluralize the head before its possessive ending.
    if compound and len(base) >= 4 and base[-1] in "ıiuü":
        head = base[:-1]
        stems = {head} | {
            head[:-1] + hard for hard, soft in _SOFTENED.items() if head[-1] in soft
        }
        return any(
            plain.startswith(stem) and _COMPOUND_PLURAL.fullmatch(plain[len(stem) :])
            for stem in stems
        )
    return False


def exact_matches(phrase: str, text: str) -> tuple[Span, ...]:
    return _find(phrase, text, inflect=False)


def _find(phrase: str, text: str, *, inflect: bool) -> tuple[Span, ...]:
    expected = phrase_tokens(phrase)
    if not expected:
        return ()
    observed = tuple(_tokens(text))
    count = len(expected)
    head = next(
        (index for index in range(count - 1, -1, -1) if expected[index][0].isalnum()),
        -1,
    )
    matches = []
    for start in range(len(observed) - count + 1):
        window = observed[start : start + count]
        if all(
            _inflected(base, word, compound=head > 0)
            if inflect and index == head
            else base == word
            for index, (base, (word, _)) in enumerate(
                zip(expected, window, strict=True)
            )
        ):
            matches.append(Span(window[0][1].start, window[-1][1].end))
    return tuple(matches)


class TurkishStemLemmatizer:
    def find(self, phrase: str, text: str) -> tuple[Span, ...]:
        return _find(phrase, text, inflect=True)
