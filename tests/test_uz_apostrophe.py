"""Uzbek display strings keep the ASCII apostrophe (narrative follow-up, item 5).

Uzbek Latin writes o' / g' / the tutuq belgisi with an apostrophe. The client's
fixed uz strings use the ASCII "'" (U+0027) everywhere; a typographic
look-alike (ʻ U+02BB, ʼ U+02BC, ‘ U+2018, ’ U+2019, ` U+0060, ´ U+00B4) slipped
into one sentence would make the same word look different across sentences
and break text search / copy. This pins every uz display string the client
produces itself:
- `narrative.FINDING_SENTENCES[*]["uz"]` (base and parenthesis),
- `narrative._RESULT_LABEL["uz"]`,
- `pdc_diag._LOCALES["uz"]` (number-format words),
- `narrative.headline_sentence(..., "uz")` (the dropped-template headline),
- a uz finding paragraph rendered by `narrative.add_finding_sentences`.

`pdc_diag.UNKNOWN_TOKENS` is EXCLUDED on purpose: it is a matcher, not display
text, and its U+02BC variant ("nomaʼlum") exists precisely to recognise that
spelling in customer DATA values.
"""
import narrative
import pdc_diag

BAD = {"ʻ": "U+02BB", "ʼ": "U+02BC", "‘": "U+2018", "’": "U+2019",
       "`": "U+0060", "´": "U+00B4"}
WORDS = ("o'lchov", "ma'lumotlardagi", "bo'yicha", "o'zgaradi")

ALL_FINDINGS = {"units": "mixed_units", "coverage": "coverage_change", "thin_period": True,
                "unknown_dominated": True, "metric_relation": "metric_definition_overlap",
                "latest_is_past_period": True}
ALL_VALUES = {"coverage_dim_label": "VALYUTA", "thin_period_label": "2026 Fevral",
              "unknown_dim_label": "SEKTOR", "unknown_share_max": 0.5,
              "overlap_equal_share": 0.976, "latest_period": "2026 Mart"}


def _assert_ascii_apostrophe(text, where):
    assert isinstance(text, str), where
    found = [name for ch, name in BAD.items() if ch in text]
    assert not found, f"{where}: {found} in {text!r}"


def _uz_finding_strings():
    out = []
    for finding, langs in narrative.FINDING_SENTENCES.items():
        base, paren = langs["uz"]
        out.append((f"FINDING_SENTENCES[{finding}].uz base", base))
        out.append((f"FINDING_SENTENCES[{finding}].uz paren", paren))
    return out


def test_uz_finding_sentences_use_ascii_apostrophe():
    strings = _uz_finding_strings()
    assert len(strings) == 2 * len(narrative.FINDING_SENTENCES)
    for where, text in strings:
        _assert_ascii_apostrophe(text, where)


def test_uz_named_words_keep_ascii_apostrophe():
    joined = " ".join(text for _w, text in _uz_finding_strings())
    for word in WORDS:
        assert word in joined, word


def test_uz_result_label_and_locale_words():
    _assert_ascii_apostrophe(narrative._RESULT_LABEL["uz"], "_RESULT_LABEL.uz")
    for key, value in pdc_diag._LOCALES["uz"].items():
        _assert_ascii_apostrophe(value, f"_LOCALES.uz.{key}")


def test_uz_rendered_finding_paragraph():
    text = narrative.add_finding_sentences("Jami: 5.", ALL_FINDINGS, ALL_VALUES, set(), "uz",
                                           sid="t")
    assert text != "Jami: 5."
    _assert_ascii_apostrophe(text, "add_finding_sentences uz")
    for word in WORDS:
        assert word in text, word


def test_uz_headline_sentence():
    fn = getattr(narrative, "headline_sentence", None)
    assert callable(fn), "narrative.headline_sentence is missing"
    text = fn({"uzs_total": 61576335238995.79}, "uz")
    assert text == "Natija: 61,58 trln."
    _assert_ascii_apostrophe(text, "headline_sentence uz")
