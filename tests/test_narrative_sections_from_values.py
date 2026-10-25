"""narrative sections resolve against VALUES booleans too (BRB run-2 follow-up).

The BRB run-2 Uzbek answer wrote `{{#has_uzs}}…{{/has_uzs}}` /
`{{#has_other}}…{{/has_other}}` with `has_uzs` / `has_other` put in VALUES
(a bool the code computed), not in FINDINGS. Before the fix both names were
unknown sections, so both bullets vanished and the answer ended on a colon.

The rule (DIAGNOSTICS_SPEC A2.2, `narrative.render_with_sections` only):
after the unchanged steps (exact FINDINGS key, FINDINGS_ENUM code / registry
key, a PASSED prefix) a name that is a VALUES key whose value is a `bool`
(strictly: `isinstance(v, bool)`) is a section — `{{#x}}` shown when True,
`{{^x}}` shown when False. Non-bool VALUES (1, 0, "true", None, 1.0) never act
as sections: they stay unknown and log `NARRATIVE_SECTION_UNKNOWN`. A
VALUES-bool section is not a finding: it is never in the returned `sections`
list and never logged as unknown. One `NARRATIVE_SECTION_FROM_VALUES n=<count>`
info line per render (distinct names, count only), none in the private
marking pass of `first_sentence_na_slot`. `brain_view(template, findings)` is
unchanged: it has no VALUES, so such a section is false there.

Offline: `.venv/Scripts/python.exe -m pytest tests/test_narrative_sections_from_values.py -q`.
"""
import logging

import pytest

import pdc_diag
from narrative import brain_view, covered_codes, first_sentence_na_slot, render, render_with_sections

SID = "t_sections_values"
FROM_VALUES = "NARRATIVE_SECTION_FROM_VALUES"
UNKNOWN = "NARRATIVE_SECTION_UNKNOWN"


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG, logger="datachat")
    return caplog


def _lines(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


def _rs(template, values=None, findings=None, **kw):
    return render_with_sections(template, values or {}, findings or {}, sid=SID, **kw)


def _r(template, values=None, findings=None, **kw):
    return render(template, values or {}, findings or {}, sid=SID, **kw)


BOTH = "{{#zq_flag}}A{{/zq_flag}}{{^zq_flag}}B{{/zq_flag}}"


# ------------------------------------------------------------ bool VALUES

def test_values_true_shows_section(logs):
    text, sections = _rs("x {{#zq_flag}}shown{{/zq_flag}} y", {"zq_flag": True})
    assert text == "x shown y"
    assert _lines(logs, UNKNOWN) == []


def test_values_false_hides_section(logs):
    text, _sections = _rs("x {{#zq_flag}}shown{{/zq_flag}} y", {"zq_flag": False})
    assert text == "x y"
    assert _lines(logs, UNKNOWN) == []


def test_values_bool_inverse_section():
    assert _r(BOTH, {"zq_flag": True}) == "A"
    assert _r(BOTH, {"zq_flag": False}) == "B"
    assert _r("{{^zq_flag}}none{{/zq_flag}}", {"zq_flag": True}) == ""
    assert _r("{{^zq_flag}}none{{/zq_flag}}", {"zq_flag": False}) == "none"


def test_render_uses_values_bool_sections_too():
    # `render` is `render_with_sections(...)[0]`
    assert _r("{{#zq_flag}}yes{{/zq_flag}}", {"zq_flag": True}) == "yes"


@pytest.mark.parametrize("value", [1, 0, "true", None, 1.0],
                         ids=["int1", "int0", "str_true", "none", "float1"])
def test_non_bool_values_never_act_as_sections(value, logs):
    text, sections = _rs(BOTH, {"zq_flag": value})
    # unknown as today: `#` hidden, `^` shown
    assert text == "B"
    assert sections == []
    lines = _lines(logs, UNKNOWN)
    assert len(lines) == 1 and "name=zq_flag" in lines[0]
    assert _lines(logs, FROM_VALUES) == []


# --------------------------------------------------------- resolution order

def test_findings_key_wins_over_values_bool(logs):
    # findings bool False -> `findings.get(flag) is True` is false -> hidden,
    # even though VALUES says True
    assert _r(BOTH, {"zq_flag": True}, {"zq_flag": False}) == "B"
    # and the other way round
    assert _r(BOTH, {"zq_flag": False}, {"zq_flag": True}) == "A"
    assert _lines(logs, FROM_VALUES) == []
    assert _lines(logs, UNKNOWN) == []


def test_enum_code_resolved_by_findings_not_values(logs):
    t = "{{#anomaly_confirmed}}A{{/anomaly_confirmed}}{{^anomaly_confirmed}}B{{/anomaly_confirmed}}"
    # an enum code is itself: decided by FINDINGS (no finding value equals it)
    assert _r(t, {"anomaly_confirmed": True}, {}) == "B"
    assert _r(t, {"anomaly_confirmed": False}, {"anomaly": "anomaly_confirmed"}) == "A"
    assert _lines(logs, FROM_VALUES) == []


def test_registry_key_resolved_by_findings_not_values(logs):
    t = "{{#thin_period}}A{{/thin_period}}{{^thin_period}}B{{/thin_period}}"
    assert "thin_period" in pdc_diag.PDC_DIAG_FINDING_KEYS
    assert _r(t, {"thin_period": True}, {}) == "B"
    assert _lines(logs, FROM_VALUES) == []


def test_passed_prefix_wins_over_values_bool(logs):
    t = "{{#lg_thin_period}}A{{/lg_thin_period}}{{^lg_thin_period}}B{{/lg_thin_period}}"
    # `lg_` passed -> resolves to `thin_period` (absent from FINDINGS -> false)
    assert _r(t, {"lg_thin_period": True}, {}, prefixes={"lg_"}) == "B"
    assert _lines(logs, FROM_VALUES) == []


# ------------------------------------------------------------------- logging

def test_from_values_line_counts_distinct_names_once_per_render(logs):
    t = ("{{#zq_alpha}}a{{/zq_alpha}} {{#zq_beta}}b{{/zq_beta}} "
         "{{#zq_alpha}}c{{/zq_alpha}}")
    text, _sections = _rs(t, {"zq_alpha": True, "zq_beta": True})
    assert text == "a b c"
    lines = _lines(logs, FROM_VALUES)
    assert len(lines) == 1
    assert lines[0].endswith("NARRATIVE_SECTION_FROM_VALUES n=2")
    # count only: no section name in the line
    assert "zq_alpha" not in lines[0] and "zq_beta" not in lines[0]
    assert _lines(logs, UNKNOWN) == []
    # one line per render
    _rs(t, {"zq_alpha": True, "zq_beta": True})
    assert len(_lines(logs, FROM_VALUES)) == 2


def test_from_values_counts_inverse_and_false_sections(logs):
    # a name resolved from VALUES counts whatever its value or section kind
    t = "{{#zq_alpha}}a{{/zq_alpha}}{{^zq_beta}}b{{/zq_beta}}"
    assert _r(t, {"zq_alpha": False, "zq_beta": False}) == "b"
    lines = _lines(logs, FROM_VALUES)
    assert len(lines) == 1 and lines[0].endswith("n=2")


def test_no_from_values_line_without_values_sections(logs):
    _r("{{#flag}}x{{/flag}} {{total}}", {"total": 3}, {"flag": True})
    _r("{{#zq_flag}}x{{/zq_flag}}", {}, {})
    assert _lines(logs, FROM_VALUES) == []


def test_values_bool_mixed_with_unknown_logs_only_the_unknown(logs):
    t = "{{#zq_known}}k{{/zq_known}}{{#zq_missing}}m{{/zq_missing}}"
    assert _r(t, {"zq_known": True}) == "k"
    unknown = _lines(logs, UNKNOWN)
    assert len(unknown) == 1 and "name=zq_missing" in unknown[0]
    from_values = _lines(logs, FROM_VALUES)
    assert len(from_values) == 1 and from_values[0].endswith("n=1")


def test_marking_pass_writes_no_section_lines(logs):
    t = ("{{#zq_flag}}Total {{zq_missing_slot}}.{{/zq_flag}} "
         "{{#zq_unknown}}u{{/zq_unknown}} Done.")
    first_sentence_na_slot(t, {"zq_flag": True}, {})
    assert _lines(logs, FROM_VALUES) == []
    assert _lines(logs, UNKNOWN) == []


def test_marking_pass_resolves_values_bool_sections():
    # same sections as the render: the shown VALUES-bool section's missing
    # slot is the first sentence's n/a slot
    t = "{{#zq_flag}}Total {{zq_missing_slot}}.{{/zq_flag}} Done."
    assert first_sentence_na_slot(t, {"zq_flag": True}, {}) == ("zq_missing_slot", "missing")
    assert first_sentence_na_slot(t, {"zq_flag": False}, {}) is None


# ----------------------------------------------------- not a finding

def test_values_bool_section_not_in_sections_list():
    t = "{{#zq_flag}}Shown body.{{/zq_flag}} {{#thin_period}}Thin body.{{/thin_period}}"
    text, sections = _rs(t, {"zq_flag": True}, {"thin_period": True})
    assert text == "Shown body. Thin body."
    assert [code for code, _body in sections] == ["thin_period"]
    assert covered_codes(text, sections) == {"thin_period"}


def test_values_bool_section_covers_no_code():
    text, sections = _rs("{{#zq_flag}}A sentence.{{/zq_flag}}", {"zq_flag": True})
    assert text == "A sentence."
    assert sections == []
    assert covered_codes(text, sections) == set()


def test_brain_view_unchanged_values_bool_is_false():
    t = "{{#zq_flag}}yes {{total}}{{/zq_flag}}{{^zq_flag}}no{{/zq_flag}}"
    # brain_view takes no VALUES: such a section is false there
    assert brain_view(t, {}) == "no"
    assert brain_view(t, {"zq_flag": True}) == "yes [total]"


# ---------------------------------------------------- the real BRB template

BRB_TEMPLATE = (
    "Jami {{n_periods:count}} davr mobaynida ({{period_from:period}} dan "
    "{{period_to:period}} gacha) yangi kreditlar sifatida quyidagi miqdorlar ajratilgan:\n"
    "{{#has_uzs}}- **{{uzs_total:amount}}** O'zbek so'mi{{/has_uzs}}\n"
    "{{#has_other}}- **{{other_total:amount}}** boshqa valyutalarda{{/has_other}}")

BRB_VALUES = {"n_periods": 5, "period_from": "2025-11", "period_to": "2026-03",
              "has_uzs": True, "has_other": True,
              "uzs_total": 4696464437, "other_total": 97613916711}


def _brb_lines():
    first = (f"Jami {pdc_diag.fmt(5, 'count', 'uz')} davr mobaynida "
             f"({pdc_diag.fmt('2025-11', 'period', 'uz')} dan "
             f"{pdc_diag.fmt('2026-03', 'period', 'uz')} gacha) yangi kreditlar sifatida "
             "quyidagi miqdorlar ajratilgan:")
    uzs = f"- **{pdc_diag.fmt(4696464437, 'amount', 'uz')}** O'zbek so'mi"
    other = f"- **{pdc_diag.fmt(97613916711, 'amount', 'uz')}** boshqa valyutalarda"
    return first, uzs, other


def test_brb_run2_template_renders_both_bullets(logs):
    first, uzs, other = _brb_lines()
    text, sections = _rs(BRB_TEMPLATE, dict(BRB_VALUES), {}, locale="uz")
    assert "O'zbek so'mi" in text and "boshqa valyutalarda" in text
    assert "n/a" not in text
    assert text == f"{first}\n{uzs}\n{other}"
    assert sections == []
    lines = _lines(logs, FROM_VALUES)
    assert len(lines) == 1 and lines[0].endswith("n=2")
    assert _lines(logs, UNKNOWN) == []


def test_brb_run2_template_other_false_keeps_only_uzs_bullet():
    first, uzs, _other = _brb_lines()
    values = dict(BRB_VALUES, has_other=False)
    text = _r(BRB_TEMPLATE, values, {}, locale="uz")
    assert text == f"{first}\n{uzs}"
    assert "boshqa valyutalarda" not in text
    assert "n/a" not in text
