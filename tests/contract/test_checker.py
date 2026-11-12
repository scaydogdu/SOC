from dataclasses import replace

import pytest

from soc.enforce.checker import Checker, Finding, NormativeRendering
from soc.enforce.lemmatizer import Span, TurkishStemLemmatizer
from soc.episode import Brief, Clause, Sentence
from soc.memory import (
    ChunkInput,
    ChunkOutput,
    Decision,
    DecisionRef,
    ForbiddenRendering,
    Translation,
)

BRIEF = Brief(clauses=[Clause(id="B1", text="Preserve normative force.")])
NORMATIVE = (
    NormativeRendering("MUST", "gereklidir", "B1"),
    NormativeRendering("MUST NOT", "yapılmaması gereklidir", "B1", strength="should"),
    NormativeRendering("REQUIRED", "Gereklidir", "B1"),
    NormativeRendering("SHOULD", "önerilir", "B1"),
    NormativeRendering("SHOULD NOT", "önerilmez", "B1"),
    NormativeRendering("MAY", "isteğe bağlıdır", "B1"),
)
CHECKER = Checker()
ID = "9700-1-s01"
SCOPE = "access token"


def source(text: str, *, translatable: bool = True, scopes=()) -> Sentence:
    return Sentence(
        id=ID,
        section="1",
        text=text,
        reading_text=text.strip(),
        scope_keys=list(scopes),
        translatable=translatable,
    )


def output(*texts: str) -> ChunkOutput:
    return ChunkOutput(
        sentences=[Translation(sentence_id=ID, text=text) for text in texts]
    )


def assert_rule(findings: tuple[Finding, ...], expected: str) -> None:
    assert findings
    assert {finding.rule for finding in findings} == {expected}
    assert {finding.status for finding in findings} == {"violation"}


@pytest.mark.parametrize(
    "ids",
    [
        (),
        (ID, ID),
        (ID, ID, ID),
        ("9700-1-s02",),
        (ID, "9700-1-s02"),
        (ID, ID, "9700-1-s02"),
        (ID + " ",),
        ("",),
        ("9700-1-s02", "9700-1-s02"),
    ],
)
def test_alignment_cases(ids):
    original = source("Translate this.")
    assert CHECKER.check([original], output("Bunu çevirin."), BRIEF) == ()
    broken = ChunkOutput(
        sentences=[
            Translation(sentence_id=identifier, text="Bunu çevirin.")
            for identifier in ids
        ]
    )
    assert_rule(CHECKER.check([original], broken, BRIEF), "ids")


@pytest.mark.parametrize(
    ("exact", "broken"),
    [
        ("  code\n", "code\n"),
        ("a\tb", "a b"),
        ("a\r\nb", "a\nb"),
        ("graph:\n  a-->b\n", "graph:\n  b-->a\n"),
        ("çizim", "c\u0327izim"),
        ("", " "),
        ("line\n", "line"),
        ("   ", ""),
        ("MUST use 3.", "MUST use 4."),
    ],
)
def test_verbatim_cases(exact, broken):
    original = source(exact, translatable=False)
    assert CHECKER.check([original], output(exact), BRIEF) == ()
    assert_rule(CHECKER.check([original], output(broken), BRIEF), "verbatim")


@pytest.mark.parametrize(
    ("english", "correct", "broken"),
    [
        ("It MUST run.", "Gereklidir.", "Yapılabilir."),
        ("It MUST NOT run.", "Yapılmaması gereklidir.", "Gereklidir."),
        ("This is REQUIRED.", "GEREKLİDİR.", "Önerilir."),
        ("It SHOULD run.", "Önerilir.", "Önerilmez."),
        ("It SHOULD NOT run.", "Önerilmez.", "Önerilir."),
        ("It MAY run.", "İsteğe bağlıdır.", "İsteğe bağlıdırmış."),
        (
            "It MUST run and MUST restart.",
            "Gereklidir. Gereklidir.",
            "Gereklidir.",
        ),
        (
            "It MUST run but MUST NOT stop.",
            "Gereklidir. Yapılmaması gereklidir.",
            "Yapılmaması gereklidir.",
        ),
        (
            "It MUST run and is REQUIRED.",
            "Gereklidir. Gereklidir.",
            "Gereklidir.",
        ),
    ],
)
def test_normative_cases(english, correct, broken):
    original = source(english)
    assert CHECKER.check([original], output(correct), BRIEF, normative=NORMATIVE) == ()
    assert_rule(
        CHECKER.check([original], output(broken), BRIEF, normative=NORMATIVE), "bcp14"
    )


@pytest.mark.parametrize(
    ("english", "correct", "broken"),
    [
        ("Wait 3 seconds.", "3 saniye bekleyin.", "13 saniye bekleyin."),
        ("Use 1.25.", "1.25 kullanın.", "1,25 kullanın."),
        ("Use -7.", "-7 kullanın.", "7 kullanın."),
        ("Use +7.", "+7 kullanın.", "-7 kullanın."),
        ("Use 0xFF.", "0xFF kullanın.", "0xFE kullanın."),
        ("Read Section 2.1.3.", "2.1.3 bölümünü okuyun.", "2.1.4 bölümünü okuyun."),
        ("Keep 1,000 entries.", "1,000 kayıt tutun.", "1 ile 000 kayıt tutun."),
        ("Wait 4 and 4.", "4 ve 4 bekleyin.", "4 bekleyin."),
        ("Wait 99.", "99 bekleyin.", "Bekleyin."),
    ],
)
def test_numeric_cases(english, correct, broken):
    original = source(english)
    assert CHECKER.check([original], output(correct), BRIEF) == ()
    assert_rule(CHECKER.check([original], output(broken), BRIEF), "numbers")


@pytest.mark.parametrize(
    ("approved", "correct", "broken"),
    [
        ("belirteç", "Belirteci koruyun.", "Belirteçlik koruyun."),
        ("belirteç", "Belirteçler geçerlidir.", "Altbelirteçler geçerlidir."),
        ("belirteç", "Belirtecin süresi doldu.", "Jetonun süresi doldu."),
        ("belirteç", "Belirteçte bulunur.", "Belirteçsiz bulunur."),
        ("erişim belirteci", "Erişim belirtecini koruyun.", "Erişim jetonunu koruyun."),
        (
            "erişim belirteci",
            "Erişim belirteçlerini koruyun.",
            "Erişim belirteçsizlik oluştu.",
        ),
        ("istemci", "İstemcilerimiz hazır.", "İstemcilik hazır."),
        ("görüş", "go\u0308ru\u0308şü kaydedin.", "Gürültüyü kaydedin."),
        ("işlem", "İŞLEM yapın.", "IŞLEM yapın."),
    ],
)
def test_term_cases(approved, correct, broken):
    original = source("Use the approved term.", scopes=[SCOPE])
    decision = Decision(DecisionRef("D1", 2), approved, frozenset({SCOPE}), "term")
    assert CHECKER.check([original], output(correct), BRIEF, decisions=[decision]) == ()
    assert_rule(
        CHECKER.check([original], output(broken), BRIEF, decisions=[decision]), "term"
    )


@pytest.mark.parametrize(
    ("stale", "correct", "broken"),
    [
        ("belirteç", "Jeton kullanın.", "Belirteci kullanın."),
        ("belirteç", "Belirteçlik bir sözcük.", "Belirteçleri silin."),
        ("erişim jetonu", "Erişim belirtecini koruyun.", "Erişim jetonunu koruyun."),
        ("erişim jetonu", "Yenileme jetonunu koruyun.", "ERİŞİM JETONUNU koruyun."),
        ("jeton", "Bir mikrojeton var.", "Bir jeton var."),
        ("kanat", "Parçayı alın.", "Kanadı alın."),
        ("kitap", "Dosya var.", "Kitabı var."),
        ("kulak", "Ses açık.", "Kulağı açık."),
        ("genel istemciler", "Yalnızca sunucular.", "Genel istemcilerin durumu."),
    ],
)
def test_forbidden_cases(stale, correct, broken):
    original = source("Use a current rendering.", scopes=[SCOPE])
    rule = ForbiddenRendering(stale, frozenset({SCOPE}), "D1", "may")
    assert CHECKER.check([original], output(correct), BRIEF, forbidden=[rule]) == ()
    findings = CHECKER.check([original], output(broken), BRIEF, forbidden=[rule])
    assert_rule(findings, "forbidden")
    assert findings[0].strength == "may"
    assert findings[0].decision_id == "D1"
    span = findings[0].span
    assert span is not None
    assert TurkishStemLemmatizer().find(stale, broken[span.start : span.end])


def test_unknown_normative_mapping_and_scope():
    original = source("The client MUST act, but it may wait.", scopes=["MUST"])
    interpretation = Decision(
        DecisionRef("D4", 1), "Bağlayıcı yükümlülük", frozenset({"MUST"}), "non_lexical"
    )
    options = (
        (),
        (NormativeRendering("MUST", "gereklidir", "old"),),
        (NormativeRendering("MUST", "  ", "B1"),),
        (NORMATIVE[0], replace(NORMATIVE[0], value="zorunludur")),
    )
    for normative in options:
        (finding,) = CHECKER.check(
            [original],
            output("Gereklidir."),
            BRIEF,
            decisions=[interpretation],
            normative=normative,
        )
        assert finding.rule == "bcp14" and finding.status == "unknown"
    assert (
        CHECKER.check(
            [original],
            output("Gereklidir."),
            BRIEF,
            decisions=[interpretation],
            normative=NORMATIVE,
        )
        == ()
    )


def test_scoped_renderings_and_brief_attribution():
    original = source("It MUST NOT run.", scopes=[SCOPE])
    unrelated = frozenset({"unrelated"})
    forbidden = ForbiddenRendering("jeton", unrelated, "old")
    decision = Decision(DecisionRef("D7", 2), "belirteç", unrelated, "term")
    chunk = ChunkInput(1, (original, source("Other.", scopes=["MUST NOT"])))
    assert chunk.scope_keys == {SCOPE, "MUST NOT"}
    (finding,) = CHECKER.check(
        [original],
        output("Gereklidir. Jeton."),
        BRIEF,
        decisions=[decision],
        forbidden=[forbidden],
        normative=NORMATIVE,
    )
    assert finding.brief_clause_ids == ("B1",)
    assert finding.strength == "should"


def test_checker_accepts_a_replacement_lemmatizer():
    class NoMatches:
        def find(self, phrase, text):
            return ()

    decision = Decision(
        DecisionRef("D1", 2),
        "X.1",
        frozenset({SCOPE}),
        "scope_limited",
        ("B1",),
        "should",
    )
    original = source("A scoped requirement.", scopes=[SCOPE])
    (finding,) = Checker(NoMatches()).check(
        [original],
        output("X.1"),
        BRIEF,
        decisions=[decision],
    )
    assert finding.decision_id == "D1"
    assert finding.brief_clause_ids == ("B1",)
    assert finding.strength == "should"
    assert CHECKER.check([original], output("X.1"), BRIEF, decisions=[decision]) == ()


def test_span_offsets_preserve_original_turkish_output():
    lemmatizer = TurkishStemLemmatizer()
    text = "Bir go\u0308ru\u0308şü kaydedin."
    assert lemmatizer.find("görüş", text) == (Span(4, 12),)
    assert lemmatizer.find("Ankara", "Ankara’nın") == (Span(0, 10),)
    assert lemmatizer.find(" ", "Ankara") == ()
