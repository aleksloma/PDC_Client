"""narrative.py — the BRB run-3 follow-up (written BEFORE the implementation).

1. Two new FINDING sentences, `scope_change` and `label_disappeared`, each a
   three-part entry `(base, paren, full)`: `full` (inline placeholders) is
   used when every placeholder in it has a value, else `base` with the usual
   parenthesis rule. Order: mixed_units, coverage_change, label_disappeared,
   thin_period, scope_change, unknown_dominated, ...
2. The unknown_dominated sentence names the dim of the FINDING; the VALUES
   label + share are used only when they describe that same dim.
3. `narrative.contains_code` / `narrative.code_in_text`: a planner reply or a
   rendered template that is Python code is never shown as prose.

Offline; display text only (R0), so nothing here reaches a brain stub.
"""
import logging
import re

import pytest

import narrative
import pdc_diag

LANGS = ("en", "uz", "ru", "ka")
SID = "t_run3"
SCOPE_RATIO = 33455 / 3792

# ------------------------------------------------- the contract's sentences
EXPECTED = {
    "scope_change": {
        "en": ("One period{paren} has a very different number of rows than the previous period, "
               "so its totals reflect a change in reporting scope, not a business change; compare "
               "rates or per-row averages instead.",
               "{period}",
               "{period} has {ratio} the rows of the previous period, so its totals reflect a "
               "change in reporting scope, not a business change; compare rates or per-row "
               "averages instead."),
        "uz": ("Bir davrda{paren} qatorlar soni oldingi davrdagidan keskin farq qiladi, shuning "
               "uchun bu davrning jami ko'rsatkichlari biznesdagi o'zgarishni emas, balki hisobot "
               "qamrovining o'zgarishini aks ettiradi; buning o'rniga ulushlarni yoki bir qatorga "
               "to'g'ri keladigan o'rtacha qiymatlarni solishtiring.",
               "{period}",
               "{period}: qatorlar soni oldingi davrga nisbatan {ratio}, shuning uchun bu "
               "davrning jami ko'rsatkichlari biznesdagi o'zgarishni emas, balki hisobot "
               "qamrovining o'zgarishini aks ettiradi; buning o'rniga ulushlarni yoki bir qatorga "
               "to'g'ri keladigan o'rtacha qiymatlarni solishtiring."),
        "ru": ("В одном периоде{paren} число строк резко отличается от предыдущего периода, "
               "поэтому его итоги отражают изменение охвата отчётности, а не изменение в бизнесе; "
               "вместо этого сравнивайте доли или средние значения на одну строку.",
               "{period}",
               "{period}: число строк относительно предыдущего периода — {ratio}, поэтому итоги "
               "этого периода отражают изменение охвата отчётности, а не изменение в бизнесе; "
               "вместо этого сравнивайте доли или средние значения на одну строку."),
        "ka": ("ერთ პერიოდში{paren} ჩანაწერების რაოდენობა მკვეთრად განსხვავდება წინა "
               "პერიოდისგან, ამიტომ მისი ჯამები ასახავს ანგარიშგების ფარგლების ცვლილებას და არა "
               "ბიზნესის ცვლილებას; ამის ნაცვლად შეადარეთ წილები ან ერთ ჩანაწერზე საშუალო "
               "მნიშვნელობები.",
               "{period}",
               "{period}: ჩანაწერების რაოდენობა წინა პერიოდთან შედარებით არის {ratio}, ამიტომ "
               "ამ პერიოდის ჯამები ასახავს ანგარიშგების ფარგლების ცვლილებას და არა ბიზნესის "
               "ცვლილებას; ამის ნაცვლად შეადარეთ წილები ან ერთ ჩანაწერზე საშუალო "
               "მნიშვნელობები."),
    },
    "label_disappeared": {
        "en": ("Rows of one label end before the last period{paren}; later periods carry only "
               "the other label(s), so a 0 for that label in a later period means no rows, not "
               "no activity.",
               "{label}",
               "Rows labelled {label} end after {period}; later periods carry only the other "
               "label(s), so a 0 for {label} in a later period means no rows, not no activity."),
        "uz": ("Belgilardan birining qatorlari oxirgi davrdan oldin tugaydi{paren}; keyingi davrlarda "
               "faqat boshqa belgi(lar) bor, shuning uchun keyingi davrda bu belgi uchun 0 "
               "faoliyat yo'qligini emas, balki qatorlar yo'qligini bildiradi.",
               "{label}",
               "{label} belgili qatorlar {period} davridan keyin tugaydi; keyingi davrlarda "
               "faqat boshqa belgi(lar) bor, shuning uchun keyingi davrda {label} uchun 0 "
               "faoliyat yo'qligini emas, balki qatorlar yo'qligini bildiradi."),
        "ru": ("Строки с одной из меток заканчиваются раньше последнего периода{paren}; в более "
               "поздних периодах есть только другие метки, поэтому 0 для этой метки в более "
               "позднем периоде означает отсутствие строк, а не отсутствие активности.",
               "{label}",
               "Строки с меткой {label} заканчиваются после периода {period}; в более поздних "
               "периодах есть только другие метки, поэтому 0 для {label} в более позднем периоде "
               "означает отсутствие строк, а не отсутствие активности."),
        "ka": ("ერთ-ერთი მნიშვნელობის მქონე ჩანაწერები ბოლო პერიოდამდე მთავრდება{paren}; შემდგომ "
               "პერიოდებში მხოლოდ სხვა მნიშვნელობები გვხვდება, ამიტომ შემდგომ პერიოდში ამ "
               "მნიშვნელობის 0 ნიშნავს, რომ ჩანაწერები არ არის, და არა იმას, რომ აქტივობა არ "
               "ყოფილა.",
               "{label}",
               "{label} მნიშვნელობის მქონე ჩანაწერები მთავრდება {period} პერიოდის შემდეგ; შემდგომ "
               "პერიოდებში მხოლოდ სხვა მნიშვნელობები გვხვდება, ამიტომ შემდგომ პერიოდში "
               "{label}-ის 0 ნიშნავს, რომ ჩანაწერები არ არის, და არა იმას, რომ აქტივობა არ "
               "ყოფილა."),
    },
}

SCOPE_FINDINGS = {"anomaly": "scope_change"}
SCOPE_VALUES = {"anomaly_period": "2026 Март", "rows_ratio": SCOPE_RATIO}
LABEL_FINDINGS = {"label_disappeared": True}
LABEL_VALUES = {"disappeared_label": "UZS", "disappeared_after_period": "2025 Dekabr"}


def _add(findings, values, covered=(), lang="en", text=""):
    return narrative.add_finding_sentences(text, findings, values, set(covered), lang, sid=SID)


def _ratio(lang):
    return pdc_diag.fmt(SCOPE_RATIO, "ratio", narrative.locale_for(lang))


def _fill(template, **parts):
    return re.sub(r"\{(dim|share|period|ratio|label)\}", lambda m: parts[m.group(1)], template)


def _base(finding, lang, paren=""):
    base = EXPECTED[finding][lang][0]
    return base.replace("{paren}", f" ({paren})" if paren else "")


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG, logger="datachat")
    return caplog


def _lines(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


# ===========================================================================
# the table
# ===========================================================================
@pytest.mark.parametrize("finding", list(EXPECTED))
@pytest.mark.parametrize("lang", LANGS)
def test_new_entries_verbatim(finding, lang):
    table = narrative.FINDING_SENTENCES
    assert finding in table, sorted(table)
    assert table[finding][lang] == EXPECTED[finding][lang]


def test_finding_order_and_coverage_maps():
    assert narrative._FINDING_ORDER == (
        "mixed_units", "coverage_change", "label_disappeared", "thin_period", "scope_change",
        "unknown_dominated", "metric_definition_overlap", "latest_is_past_period")
    assert set(narrative._FINDING_COVERED_BY["scope_change"]) == {"scope_change"}
    assert set(narrative._FINDING_COVERED_BY["label_disappeared"]) == {"label_disappeared"}


def test_ratio_format_per_locale():
    # what the full sentences quote: 8.8x, and 8,8x in the uz / ru locales
    assert _ratio("en") == "8.8x" and _ratio("ka") == "8.8x"
    assert _ratio("uz") == "8,8x" and _ratio("ru") == "8,8x"


# ===========================================================================
# scope_change
# ===========================================================================
@pytest.mark.parametrize("lang", LANGS)
def test_scope_change_full_sentence(lang):
    expected = _fill(EXPECTED["scope_change"][lang][2], period="2026 Март", ratio=_ratio(lang))
    assert _add(SCOPE_FINDINGS, SCOPE_VALUES, lang=lang) == expected


@pytest.mark.parametrize("lang", LANGS)
def test_scope_change_value_free_fallbacks(lang):
    assert _add(SCOPE_FINDINGS, {}, lang=lang) == _base("scope_change", lang)
    # only the period: the base with the period in the parenthesis
    assert (_add(SCOPE_FINDINGS, {"anomaly_period": "2026 Март"}, lang=lang)
            == _base("scope_change", lang, "2026 Март"))
    # only the ratio: the base sentence, no parenthesis
    assert (_add(SCOPE_FINDINGS, {"rows_ratio": SCOPE_RATIO}, lang=lang)
            == _base("scope_change", lang))


def test_scope_change_fires_on_change_shape_with_change_to_period():
    findings = {"change_shape": "scope_change"}
    assert (_add(findings, {"change_to_period": "2026 Март"})
            == _base("scope_change", "en", "2026 Март"))
    full = _add(findings, {"change_to_period": "2026 Март", "rows_ratio": SCOPE_RATIO})
    assert full == _fill(EXPECTED["scope_change"]["en"][2], period="2026 Март", ratio="8.8x")
    # anomaly_period wins over change_to_period
    both = _add({"anomaly": "scope_change", "change_shape": "scope_change"},
                {"anomaly_period": "2026 Март", "change_to_period": "2025 Ноябрь"})
    assert both == _base("scope_change", "en", "2026 Март")
    assert both.count("very different number of rows") == 1


def test_scope_change_prefixed_values():
    out = _add(SCOPE_FINDINGS, {"q3_anomaly_period": "2026 Март", "q3_rows_ratio": SCOPE_RATIO})
    assert out == _fill(EXPECTED["scope_change"]["en"][2], period="2026 Март", ratio="8.8x")


@pytest.mark.parametrize("findings", [{"anomaly": "anomaly_confirmed"},
                                      {"change_shape": "concentrated_in_one_dim"},
                                      {"anomaly": "no_anomaly"}, {}])
def test_scope_change_not_fired(findings):
    assert _add(findings, SCOPE_VALUES, text="Total is 5.") == "Total is 5."


def test_scope_change_covered_by_section():
    assert _add(SCOPE_FINDINGS, SCOPE_VALUES, covered=["scope_change"],
                text="Total is 5.") == "Total is 5."


# ===========================================================================
# label_disappeared
# ===========================================================================
@pytest.mark.parametrize("lang", LANGS)
def test_label_disappeared_full_sentence(lang):
    expected = _fill(EXPECTED["label_disappeared"][lang][2], label="UZS", period="2025 Dekabr")
    assert _add(LABEL_FINDINGS, LABEL_VALUES, lang=lang) == expected


@pytest.mark.parametrize("lang", LANGS)
def test_label_disappeared_value_free_fallbacks(lang):
    assert _add(LABEL_FINDINGS, {}, lang=lang) == _base("label_disappeared", lang)
    assert (_add(LABEL_FINDINGS, {"disappeared_label": "UZS"}, lang=lang)
            == _base("label_disappeared", lang, "UZS"))
    # only the period: the full form needs the label, the parenthesis too
    assert (_add(LABEL_FINDINGS, {"disappeared_after_period": "2025 Dekabr"}, lang=lang)
            == _base("label_disappeared", lang))


def test_label_disappeared_fires_only_on_true():
    for findings in ({"label_disappeared": False}, {"label_disappeared": "yes"}, {}):
        assert _add(findings, LABEL_VALUES, text="Total is 5.") == "Total is 5."


def test_label_disappeared_covered_by_section():
    assert _add(LABEL_FINDINGS, LABEL_VALUES, covered=["label_disappeared"],
                text="Total is 5.") == "Total is 5."


def test_label_disappeared_string_value_is_markdown_escaped():
    out = _add(LABEL_FINDINGS, {"disappeared_label": "UZS_cash",
                                "disappeared_after_period": "2025 Dekabr"})
    assert "UZS\\_cash" in out
    assert out == _fill(EXPECTED["label_disappeared"]["en"][2], label="UZS\\_cash",
                        period="2025 Dekabr")


def test_new_sentences_in_table_order():
    findings = {"units": "mixed_units", "coverage": "coverage_change",
                "label_disappeared": True, "thin_period": True, "anomaly": "scope_change"}
    out = _add(findings, {}, text="Total is 5.")
    paragraphs = out.split("\n\n")
    assert paragraphs[0] == "Total is 5." and len(paragraphs) == 2, out
    added = paragraphs[1]
    fragments = ["Amounts carry more than one currency",
                 "The set of values in one dimension changes",
                 "Rows of one label end before the last period",
                 "One period looks incomplete",
                 "One period has a very different number of rows"]
    positions = [added.find(f) for f in fragments]
    assert all(p >= 0 for p in positions), (positions, added)
    assert positions == sorted(positions)


def test_log_carries_count_not_values(logs):
    _add({**SCOPE_FINDINGS, **LABEL_FINDINGS},
         {"anomaly_period": "SECRETPERIOD77", "rows_ratio": SCOPE_RATIO,
          "disappeared_label": "SECRETLABEL55", "disappeared_after_period": "2025 Dekabr"})
    lines = _lines(logs, "FINDING_SENTENCES_ADDED")
    assert len(lines) == 1 and "n=2" in lines[0]
    blob = "\n".join(r.getMessage() for r in logs.records)
    assert "SECRETPERIOD77" not in blob and "SECRETLABEL55" not in blob


# ===========================================================================
# unknown_dominated: the sentence names the finding's dim
# ===========================================================================
UNKNOWN_BASE = "Many rows have no value in one dimension{paren}, so that dimension cannot explain any change."


def test_unknown_sentence_label_of_another_dim_is_ignored():
    out = _add({"unknown_dominated": True, "unknown_dim": "SEKTOR"},
               {"unknown_dim_label": "SEGMENT", "unknown_share_max": 0.581})
    assert out == UNKNOWN_BASE.replace("{paren}", " (SEKTOR)")
    assert "SEGMENT" not in out
    assert "58.1" not in out and "%" not in out


def test_unknown_sentence_label_equal_to_finding_keeps_share():
    out = _add({"unknown_dominated": True, "unknown_dim": "SEKTOR"},
               {"unknown_dim_label": "SEKTOR", "unknown_share_max": 1.0})
    share = pdc_diag.fmt(1.0, "pct")
    assert out == UNKNOWN_BASE.replace("{paren}", f" (SEKTOR; up to {share} Unknown)")


def test_unknown_sentence_without_finding_dim_uses_values():
    out = _add({"unknown_dominated": True},
               {"unknown_dim_label": "SEGMENT", "unknown_share_max": 0.581})
    assert out == UNKNOWN_BASE.replace("{paren}", " (SEGMENT; up to 58.1% Unknown)")


@pytest.mark.parametrize("lang", LANGS)
def test_unknown_sentence_mismatch_every_language(lang):
    out = _add({"unknown_dominated": True, "unknown_dim": "SEKTOR"},
               {"unknown_dim_label": "SEGMENT", "unknown_share_max": 0.581}, lang=lang)
    assert "SEKTOR" in out
    assert "SEGMENT" not in out
    assert "58" not in out


# ===========================================================================
# contains_code / code_in_text
# ===========================================================================
Q4_CODE = """# answer_language: English
import pandas as pd
import re

df = dfs['sales.csv']
MONTHS = {
    'january': 1, 'february': 2, 'march': 3,
    'april': 4, 'may': 5, 'june': 6,
}

def parse_month(s):
    m = re.match(r'(\\w+)', str(s).lower())
    return MONTHS.get(m.group(1)) if m else None

df['month_num'] = df['month'].map(parse_month)
VALUES['new_items'] = int((df['status'] == 'new').sum())
RESULT = df.groupby('region')['amount'].sum()"""

Q4_REPLY = "```python\n" + Q4_CODE + "\n```"

PROSE = ("The total loan portfolio grew by 12% in March.\n"
         "Most of the growth came from the Tashkent region.\n"
         "The chart below shows the monthly trend by region.\n"
         "Smaller regions stayed flat over the quarter.")

PLACEHOLDERS = {"dim": "SEKTOR", "share": "50%", "period": "2026 Mart", "ratio": "8,8x",
                "label": "UZS"}


def _contains(text):
    fn = getattr(narrative, "contains_code", None)
    assert callable(fn), "narrative.contains_code is missing"
    return fn(text)


@pytest.mark.parametrize("text", [
    Q4_REPLY,
    Q4_CODE,
    "import pandas as pd\nx = 1\nprint(x)",
    "Here is the code:\n```\nRESULT = 1\n```",
    "for i in range(3):\n    total = i\nprint(total)",
    "\n\nimport pandas as pd\n\n\nx = 1\n\nprint(x)\n",
], ids=["q4_fenced", "q4_unfenced", "three_lines", "bare_fence", "for_loop", "blank_lines"])
def test_contains_code_true(text):
    assert _contains(text) is True


def _finding_texts(langs):
    """Every FINDING_SENTENCES string of `langs`, placeholders filled, the
    parenthesis put in place — and per language all of them joined one per
    line, so a 3-line window exists."""
    out = []
    for finding, by_lang in narrative.FINDING_SENTENCES.items():
        for lang in langs:
            entry = by_lang[lang]
            base, paren = entry[0], entry[1]
            filled_paren = _fill(paren, **PLACEHOLDERS) if paren else ""
            out.append(base.replace("{paren}", f" ({filled_paren})" if filled_paren else ""))
            if len(entry) > 2:
                out.append(_fill(entry[2], **PLACEHOLDERS))
    return out


@pytest.mark.parametrize("lang", ["uz", "ru", "ka", "en"])
def test_contains_code_false_for_finding_sentences(lang):
    texts = _finding_texts([lang])
    for text in texts:
        assert _contains(text) is False, text
    assert _contains("\n".join(texts)) is False


@pytest.mark.parametrize("text", [
    PROSE,
    "- **0** in UZS\n- **760,477** in other\n- note",
    "Result: 5\nTotal: 7\nShare: 0.5",
    "760,477\n1,687\n2,037",
    "Summary\nTotal\nEnd",
    "Total\n(1, 2)\n3",
    "",
    "   \n\n  ",
    "x = 1",
    "Which region do you mean?",
], ids=["prose", "md_bullets", "annassign", "numbers", "words", "tuple", "empty", "blank",
        "one_line", "question"])
def test_contains_code_false(text):
    assert _contains(text) is False


@pytest.mark.parametrize("text", [
    "Revenue (USD)\nCost (USD)\nMargin",
    "Ташкент (город)\nСамарканд (область)\nБухара (область)",
    "შემოსავალი (ლარი)\nხარჯი\nმოგება",
    "Margin = Revenue - Cost\nShare = Margin / Revenue\nGrowth = Share * 100",
    "Q1 = 5\nQ2 = 7\nQ3 = 9",
    "Итого = 5\nПлан = 7\nФакт = 9",
    "file.csv\nreport.pdf\ndata.xlsx",
], ids=["unit_headings", "ru_places", "ka_headings", "formulas", "assignments",
        "ru_assignments", "file_names"])
def test_contains_code_false_without_a_strong_construct(text):
    """Every window parses (calls of plain names, assignments, attributes)
    but holds no STRONG construct, so it is prose, not code."""
    assert _contains(text) is False


@pytest.mark.parametrize("text", [
    "x = df.groupby('a')\ny = 1\nz = 2",
    "total = sum(v for v in rows)\ny = 1\nz = 2",
    "x = dfs['sales.csv']\ny = 1\nz = 2",
    "fig = px.bar(d, x='a')\ny = 1\nz = 2",
    "m = {'a': 1}\ny = 1\nz = 2",
    "f = lambda v: v\ny = 1\nz = 2",
    "while True:\n    break\ny = 1",
    "with open('a') as h:\n    pass\ny = 1",
    "try:\n    y = 1\nfinally: pass",
], ids=["method_call", "generator", "str_subscript", "keyword_args", "dict", "lambda",
        "while", "with", "try"])
def test_contains_code_true_with_a_strong_construct(text):
    assert _contains(text) is True


def test_contains_code_failure_logs_under_the_given_sid(monkeypatch, logs):
    def boom(*a, **k):
        raise RuntimeError("no")
    monkeypatch.setattr(narrative, "_window_is_code", boom)
    assert narrative.contains_code("a\nb\nc", sid="sid-x") is False
    lines = _lines(logs, "NARRATIVE_CODE_CHECK_FAILED")
    assert len(lines) == 1 and "RuntimeError" in lines[0]


@pytest.mark.parametrize("value", [None, 42, 3.5, b"import os\nx = 1\nprint(x)",
                                   ["import os", "x = 1", "print(x)"]])
def test_contains_code_non_str_is_false(value):
    assert _contains(value) is False


def test_contains_code_never_raises_on_deep_nesting():
    deep = "x = " + "(" * 5000 + "1" + ")" * 5000
    assert _contains(deep + "\n" + deep + "\n" + deep) in (True, False)


def test_code_in_text_logs_once_without_the_text(logs):
    fn = getattr(narrative, "code_in_text", None)
    assert callable(fn), "narrative.code_in_text is missing"
    secret = "import pandas as pd\nSECRET_TOKEN_ZZ9 = 1\nprint(SECRET_TOKEN_ZZ9)"
    assert fn(secret, sid=SID, what="answer") is True
    lines = _lines(logs, "NARRATIVE_CODE_IN_TEXT")
    assert len(lines) == 1, lines
    assert "what=answer" in lines[0]
    blob = "\n".join(r.getMessage() for r in logs.records)
    assert "SECRET_TOKEN_ZZ9" not in blob
    assert "import pandas" not in blob


def test_code_in_text_prose_is_false_and_silent(logs):
    fn = getattr(narrative, "code_in_text", None)
    assert callable(fn), "narrative.code_in_text is missing"
    assert fn(PROSE, sid=SID, what="narrative") is False
    assert fn(None, sid=SID, what="caption") is False
    assert _lines(logs, "NARRATIVE_CODE_IN_TEXT") == []
