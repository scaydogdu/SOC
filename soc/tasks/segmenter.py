"""Deterministic sentence records from UTF-8 RFC XML v3."""

import argparse
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path
from xml.etree import ElementTree as ET
from xml.parsers import expat

from soc.episode import Sentence

VERBATIM = {"sourcecode", "artwork"}
BLOCK = {"t", "li", "dt", "dd", "blockquote", "figure", "td", "th"}
OPEN_TAG = re.compile(rb"""<(?:[^"'<>]|"[^"]*"|'[^']*')+>""")
END = re.compile(r"""[.!?]+["'”’\)\]]*(?=\s|$)""")
ABBREVIATION = re.compile(
    r"\b(?:e\.g|i\.e|cf|vs|etc|sec|et\s+al|u\.s|no)\.$", re.IGNORECASE
)
BCP14 = re.compile(
    r"\b(?:MUST NOT|SHALL NOT|SHOULD NOT|NOT RECOMMENDED|MUST|REQUIRED|"
    r"SHALL|SHOULD|RECOMMENDED|MAY|OPTIONAL)\b"
)
NUMBER = re.compile(r"\b\d+(?:\.\d+)*\b")


def _verbatim(xml: bytes) -> Iterator[str]:
    parser = expat.ParserCreate()
    starts: list[int | None] = []
    blocks: list[str] = []

    def start(name: str, attrs: dict[str, str]) -> None:
        if name in VERBATIM:
            tag = OPEN_TAG.match(xml, parser.CurrentByteIndex)
            assert tag is not None
            starts.append(None if tag[0].endswith(b"/>") else tag.end())

    def end(name: str) -> None:
        if name in VERBATIM:
            offset = starts.pop()
            blocks.append(
                ""
                if offset is None
                else xml[offset : parser.CurrentByteIndex].decode("utf-8")
            )

    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.Parse(xml, True)
    return iter(blocks)


def _sections(root: ET.Element) -> Iterator[tuple[str, ET.Element]]:
    def visit(
        parent: ET.Element, prefix: str, appendix: bool = False
    ) -> Iterator[tuple[str, ET.Element]]:
        for index, section in enumerate(parent.findall("section"), 1):
            fallback = chr(64 + index) if appendix else f"{prefix}{index}"
            label = section.get("pn", f"section-{fallback}").removeprefix("section-")
            if label.startswith("appendix."):
                label = label.removeprefix("appendix.").upper()
            yield label, section
            yield from visit(section, f"{label}.")

    for location, prefix, appendix in (
        ("front/boilerplate", "boilerplate.", False),
        ("front/toc", "toc.", False),
        ("middle", "", False),
        ("back", "", True),
    ):
        parent = root.find(location)
        if parent is not None:
            yield from visit(parent, prefix, appendix)


def _parts(
    element: ET.Element, raw: dict[ET.Element, str], citations: set[str]
) -> Iterator[tuple[str, str]]:
    if element.text:
        yield "text", element.text
    for child in element:
        if child.tag in VERBATIM:
            yield "verbatim", raw[child]
        elif (
            child.tag == "xref"
            and not "".join(child.itertext()).strip()
            and (label := child.get("derivedContent"))
        ):
            if child.get("target") in citations:
                label = f"[{label}]"
            if number := child.get("section"):
                style = child.get("sectionFormat")
                label = number if style == "bare" else f"Section {number} of {label}"
            yield from _parts(child, raw, citations)
            yield "reference", label
        elif child.tag not in {"name", "section"}:
            if child.tag in BLOCK:
                yield "boundary", ""
            yield from _parts(child, raw, citations)
            if child.tag in BLOCK:
                yield "boundary", ""
        if child.tail:
            yield "text", child.tail


def split_sentences(text: str) -> Iterator[str]:
    """Split prose while retaining every character, including intersentence space."""
    start = 0
    for end in END.finditer(text):
        if ABBREVIATION.search(text, start, end.end()):
            continue
        cursor = end.end()
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        yield text[start:cursor]
        start = cursor
    if start < len(text):
        yield text[start:]


def _prose(parts: list[tuple[str, str]]) -> Iterator[tuple[str, str, bool]]:
    # Map reading-text boundaries back to exact section-text boundaries.
    exact = []
    reading = []
    offsets = [0]
    position = 0
    for kind, text in parts:
        reading.append(text)
        if kind == "reference":
            offsets.extend([position] * len(text))
        else:
            exact.append(text)
            offsets.extend(range(position + 1, position + len(text) + 1))
            position += len(text)
    raw = "".join(exact)
    start = 0
    for sentence in split_sentences("".join(reading)):
        end = start + len(sentence)
        yield raw[offsets[start] : offsets[end]], " ".join(sentence.split()), True
        start = end


def _segments(parts: Iterable[tuple[str, str]]) -> Iterator[tuple[str, str, bool]]:
    buffer: list[tuple[str, str]] = []
    for kind, value in parts:
        if kind in {"text", "reference"}:
            buffer.append((kind, value))
            continue
        if buffer and (kind == "verbatim" or any(text.strip() for _, text in buffer)):
            yield from _prose(buffer)
            buffer.clear()
        if kind == "verbatim":
            yield value, value, False
    yield from _prose(buffer)


def _term_pattern(term: str) -> re.Pattern[str]:
    lower = term.lower()
    if re.search(r"[^aeiou]y$", lower):
        plural = term[:-1] + "ies"
    elif lower.endswith(("s", "x", "z", "sh", "ch")):
        plural = term + "es"
    else:
        plural = term + "s"
    return re.compile(
        rf"(?<!\w)(?:{re.escape(term)}|{re.escape(plural)})(?!\w)", re.IGNORECASE
    )


def segment(xml: bytes, glossary: Iterable[str] = ()) -> Iterator[Sentence]:
    root = ET.fromstring(xml)
    rfc = root.attrib["number"]
    raw = dict(
        zip(
            (node for node in root.iter() if node.tag in VERBATIM),
            _verbatim(xml),
            strict=True,
        )
    )
    terms = {term: _term_pattern(term) for term in glossary if term}
    citations = {
        node.attrib["anchor"]
        for tag in ("reference", "referencegroup")
        for node in root.iter(tag)
        if "anchor" in node.attrib
    }
    for section, element in _sections(root):
        front_matter = section.startswith(("boilerplate.", "toc."))
        for index, (content, reading, prose) in enumerate(
            _segments(_parts(element, raw, citations)), 1
        ):
            keys = set(BCP14.findall(reading)) | set(NUMBER.findall(reading))
            keys.update(
                term for term, pattern in terms.items() if pattern.search(reading)
            )
            yield Sentence(
                id=f"{rfc}-{section}-s{index:02d}",
                section=section,
                text=content,
                reading_text=reading,
                scope_keys=sorted(keys),
                translatable=prose and bool(reading) and not front_matter,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("xml", type=Path)
    parser.add_argument(
        "--glossary", type=Path, help="UTF-8 file with one term per line"
    )
    args = parser.parse_args()
    terms = (
        args.glossary.read_text(encoding="utf-8").splitlines() if args.glossary else ()
    )
    for sentence in segment(args.xml.read_bytes(), terms):
        sys.stdout.write(sentence.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
