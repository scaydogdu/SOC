import re
from collections import defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET

from soc.episode import SENTENCE_ID
from soc.tasks.segmenter import main, segment, split_sentences

RFC = Path("tests/fixtures/rfc9700.xml")
RAW = re.compile(rb"<(sourcecode|artwork)\b[^>]*>(.*?)</\1>", re.DOTALL)


def test_rfc9700_round_trip():
    xml = RFC.read_bytes()
    root = ET.fromstring(xml)
    raw = {
        node: match[2].decode()
        for node, match in zip(
            root.findall(".//sourcecode") + root.findall(".//artwork"),
            sorted(
                RAW.finditer(xml),
                key=lambda item: (item[1] != b"sourcecode", item.start()),
            ),
            strict=True,
        )
    }
    expected = {}
    for section in root.findall(".//section"):
        label = section.attrib["pn"].removeprefix("section-")
        if label.startswith("appendix."):
            label = label.removeprefix("appendix.").upper()
        for child in list(section):
            if child.tag not in {"name", "section"}:
                continue
            previous = list(section).index(child) - 1
            if previous >= 0:
                neighbor = section[previous]
                neighbor.tail = (neighbor.tail or "") + (child.tail or "")
            else:
                section.text = (section.text or "") + (child.tail or "")
            section.remove(child)
        for node in section.iter():
            if node in raw:
                node.text = raw[node]
        expected[label] = "".join(section.itertext())
    actual = defaultdict(str)
    sentences = list(segment(xml))
    assert sentences == list(segment(xml))
    assert len({sentence.id for sentence in sentences}) == len(sentences)
    for sentence in sentences:
        assert SENTENCE_ID.fullmatch(sentence.id)
        actual[sentence.section] += sentence.text
    assert actual == expected
    assert all(
        s.reading_text and s.reading_text == " ".join(s.reading_text.split())
        for s in sentences
        if s.translatable
    )
    assert all(
        not s.translatable
        for s in sentences
        if s.section.startswith(("toc.", "boilerplate."))
    )
    # Literal reference checked independently against the RFC Editor's text edition.
    known = next(s for s in sentences if s.id == "9700-1.1-s02")
    assert known.reading_text == "Section 3 presents the updated OAuth attacker model."
    assert "Section 3" not in known.text
    assert any("according to [BCP195]" in s.reading_text for s in sentences)

    assert [
        s.text for s in sentences if not s.translatable and s.text in raw.values()
    ] == [match[2].decode() for match in RAW.finditer(xml)]


def test_split_traps():
    prose = (
        "Use e.g. example.com, i.e. the URL https://example.com/v1.2. "
        "See Section 4.2. (It works.) This uses version 1.2.3."
    )
    assert list(split_sentences(prose)) == [
        "Use e.g. example.com, i.e. the URL https://example.com/v1.2. ",
        "See Section 4.2. ",
        "(It works.) ",
        "This uses version 1.2.3.",
    ]


def test_scope_and_unrelated_section_edits():
    xml = (
        b'<rfc number="1"><middle><section><t>First.</t></section>'
        b"<section><t>An Access Token MUST NOT change. May it last 2.5 hours?</t>"
        b'<sourcecode data="x>y">a&amp;b\r\n</sourcecode><artwork/>'
        b"</section></middle></rfc>"
    )
    first = list(segment(xml, ["access token", "tokenizer", ""]))
    second = list(
        segment(xml.replace(b"First.", b"First. More."), ["access token", "tokenizer"])
    )
    section = [item for item in first if item.section == "2"]
    assert section == [item for item in second if item.section == "2"]
    assert [item.scope_keys for item in section] == [
        ["MUST NOT", "access token"],
        ["2.5"],
        [],
        [],
    ]
    assert section[-2].text == "a&amp;b\r\n"
    assert not section[-1].translatable


def test_cli(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["segmenter", str(RFC)])
    main()
    assert '"id":"9700-boilerplate.1-s01"' in capsys.readouterr().out


def test_common_abbreviations():
    text = "See Sec. 4. Hardt et al. describe changes in the U.S. only. Read No. 5."
    assert list(split_sentences(text)) == [
        "See Sec. 4. ",
        "Hardt et al. describe changes in the U.S. only. ",
        "Read No. 5.",
    ]


def test_glossary_forms():
    xml = """<rfc number="1"><middle><section>
    <t>These access tokens’ values use a policy's rules and several policies.
    The boxes and the box’s contents are relevant.</t>
    </section></middle></rfc>"""
    sentences = [
        s
        for s in segment(xml.encode(), ["access token", "policy", "box"])
        if s.translatable
    ]
    assert [s.scope_keys for s in sentences] == [["access token", "policy"], ["box"]]


def test_rfc9700_plural_count():
    plural = [
        s
        for s in segment(RFC.read_bytes(), ["access token"])
        if s.translatable and re.search(r"\baccess tokens\b", s.reading_text, re.I)
    ]
    assert len(plural) == 53
    assert all("access token" in s.scope_keys for s in plural)
