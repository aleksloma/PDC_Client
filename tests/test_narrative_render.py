"""narrative.py — the template language (DIAGNOSTICS_SPEC A2), written BEFORE
the module exists.

Pins every A2 rule: the grammar (A2.1), slot / section semantics, default
kinds, post-processing order, single-pass substitution, markdown escaping of
string values (A2.2), the caps (A2.3) and the public API names (A2.4). The
rendered text is DISPLAY text: it never reaches the brain (A5.2 R0), which is
why `brain_view` must carry no value and `NARRATIVE_SLOT_MISSING` must log the
slot NAME only.

Runs offline: `.venv/Scripts/python.exe -m pytest tests/test_narrative_render.py -q`.
"""
import logging
import random

import pytest

import narrative
import pdc_diag
from narrative import (CAPTION_MAX, CAPTION_RENDERED_MAX, FINDINGS_MAX_KEYS,
                       FINDINGS_STR_MAX, KINDS, MARKER_MAX, RENDERED_MAX,
                       TEMPLATE_MAX, TemplateError, answer_lang_code, brain_view,
                       clean_captions, clean_template, fallback_text,
                       history_content, parse, plain, render, value_display)

SID = "t_render"
SECRET = "SECRET_VALUE_ZZ_987654321"


def _r(template, values=None, findings=None, **kw):
    return render(template, values or {}, findings or {}, sid=SID, **kw)


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG, logger="datachat")
    return caplog


def _lines(caplog, needle):
    return [r.getMessage() for r in caplog.records if needle in r.getMessage()]


# --------------------------------------------------------------------- constants

def test_public_constants():
    assert KINDS == ("count", "float", "pct", "ratio", "amount", "text", "period")
    assert TEMPLATE_MAX == 4000 and CAPTION_MAX == 600
    assert RENDERED_MAX == 8000 and CAPTION_RENDERED_MAX == 1200
    assert FINDINGS_MAX_KEYS == 24 and FINDINGS_STR_MAX == 64 and MARKER_MAX == 400
    assert issubclass(TemplateError, ValueError)


# ------------------------------------------------------------------------- slots

def test_slot_basic():
    assert _r("Total: {{total}} loans.", {"total": 1234567}) == "Total: 1,234,567 loans."


def test_slot_kinds():
    values = {"p": 0.375, "r": 3.27, "a": 7250000, "c": 1234567.4,
              "f": 1234.5678, "t": "North", "pe": "Q3"}
    out = _r("{{p:pct}}|{{r:ratio}}|{{a:amount}}|{{c:count}}|{{f:float}}|{{t:text}}|{{pe:period}}",
             values)
    assert out == "37.5%|3.3x|7.25 mln|1,234,567|1,234.57|North|Q3"


def test_unknown_kind_uses_default():
    # kind = [a-z]{1,10} parses, but an unrecognised one is the type's default
    assert _r("{{n:bogus}}", {"n": 42}) == "42"
    assert _r("{{f:zzz}}", {"f": 0.5}) == "0.50"
    assert _r("{{s:whatever}}", {"s": "North"}) == "North"


def test_default_kind_per_type():
    # bool -> "true"/"false"; int -> count; float -> float; str -> text; None -> n/a
    out = _r("{{b1}} {{b0}} {{i}} {{f}} {{s}} {{n}}",
             {"b1": True, "b0": False, "i": 1234567, "f": 1234.5678,
              "s": "North", "n": None})
    assert out == "true false 1,234,567 1,234.57 North n/a"


def test_missing_slot_na_logs_name_only(logs):
    out = _r("{{a}} {{a}} {{b}}", {"b": SECRET})
    assert out == "n/a n/a " + SECRET.replace("_", "\\_")
    missing = _lines(logs, "NARRATIVE_SLOT_MISSING")
    assert len(missing) == 1                       # once per NAME per render
    assert "name=a" in missing[0]
    blob = "\n".join(r.getMessage() for r in logs.records)
    assert SECRET not in blob and "987654321" not in blob


def test_missing_slot_logged_once_per_name_per_render(logs):
    _r("{{a}} {{c}} {{a}}", {})
    _r("{{a}}", {})
    missing = _lines(logs, "NARRATIVE_SLOT_MISSING")
    assert sorted(m[m.index("name="):] for m in missing) == ["name=a", "name=a", "name=c"]


def test_unused_values_ignored():
    assert _r("x", {"unused": 5, "other": "y"}) == "x"


def test_fmt_delegated_to_pdc_diag():
    # the slot renderer IS pdc_diag.fmt — the two can never disagree
    assert _r("{{v:amount}}", {"v": 2345678901}) == pdc_diag.fmt(2345678901, "amount")
    assert _r("{{v:count}}", {"v": float("nan")}) == "n/a"


# ---------------------------------------------------------------------- sections

def test_section_positive_bool():
    t = "A{{#flag}} shown{{/flag}} B"
    assert _r(t, {}, {"flag": True}) == "A shown B"
    assert _r(t, {}, {"flag": False}) == "A B"
    assert _r(t, {}, {}) == "A B"


def test_section_positive_non_bool():
    # (b) the flag is present with a non-bool value (a column name or a code)
    t = "{{#coverage_dim}}by dimension{{/coverage_dim}}"
    assert _r(t, {}, {"coverage_dim": "CURRENCY"}) == "by dimension"
    assert _r(t, {}, {"coverage_dim": "anomaly_confirmed"}) == "by dimension"


def test_section_enum_code_matches_value():
    # (c) flag in FINDINGS_ENUM and some finding VALUE equals it
    assert "anomaly_confirmed" in pdc_diag.FINDINGS_ENUM
    t = "{{#anomaly_confirmed}}spike{{/anomaly_confirmed}}{{#no_anomaly}}flat{{/no_anomaly}}"
    assert _r(t, {}, {"anomaly": "anomaly_confirmed"}) == "spike"
    assert _r(t, {}, {"anomaly": "no_anomaly"}) == "flat"
    # a value that is not an enum code never opens a section named after it
    assert _r("{{#CURRENCY}}x{{/CURRENCY}}", {}, {"coverage_dim": "CURRENCY"}) == ""


def test_section_negative():
    t = "{{^flag}}no{{/flag}}{{#flag}}yes{{/flag}}"
    assert _r(t, {}, {}) == "no"
    assert _r(t, {}, {"flag": False}) == "no"
    assert _r(t, {}, {"flag": True}) == "yes"
    assert _r(t, {}, {"flag": "COL"}) == "yes"
    assert _r("{{^anomaly_confirmed}}calm{{/anomaly_confirmed}}", {},
              {"anomaly": "anomaly_confirmed"}) == ""


def test_slot_inside_section_rendered():
    out = _r("{{#flag}}n={{n}}{{/flag}}", {"n": 3}, {"flag": True})
    assert out == "n=3"


# ------------------------------------------------------------- parse errors

def test_parse_raises_template_error():
    for bad in ("{{#a}}x{{#b}}y{{/b}}{{/a}}",   # nested
                "x {{/a}} y",                    # close without open
                "{{#a}}x{{/b}}",                 # mismatched close
                "{{#a}}x"):                      # unclosed
        with pytest.raises(TemplateError):
            parse(bad)
    assert isinstance(parse("ok {{x}} {{#a}}y{{/a}}"), list)


def test_nested_section_fallback(logs):
    out = _r("Head {{n}} {{#a}}x{{#b}}y{{/b}}{{/a}} tail", {"n": 1}, {"a": True, "b": True})
    assert out == "Head n/a tail"
    failed = _lines(logs, "NARRATIVE_RENDER_FAILED")
    assert failed and "error=TemplateError" in failed[0]


def test_unclosed_section_fallback(logs):
    assert _r("{{#a}}x {{n}}", {"n": 1}, {"a": True}) == "x n/a"
    assert "error=TemplateError" in _lines(logs, "NARRATIVE_RENDER_FAILED")[0]


def test_mismatched_close_fallback(logs):
    assert _r("{{#a}}x{{/b}} {{n}}", {"n": 1}, {"a": True}) == "x n/a"
    assert "error=TemplateError" in _lines(logs, "NARRATIVE_RENDER_FAILED")[0]


def test_close_without_open_fallback(logs):
    assert _r("A {{/a}} B {{n}}", {"n": 1}) == "A B n/a"
    assert "error=TemplateError" in _lines(logs, "NARRATIVE_RENDER_FAILED")[0]


def test_fallback_text_direct():
    t = "Head {{#a}}x {{n}}{{/a}} {{^b}}y{{/b}} {{m:count}} {{/c}} tail"
    assert fallback_text(t) == "Head n/a tail"
    assert fallback_text("") == ""
    assert fallback_text(None) == ""           # any exception -> ""


# ------------------------------------------------------------------ grammar edges

def test_non_grammar_braces_literal():
    assert _r("{{ x }}", {"x": 1}) == "{{ x }}"
    assert _r("{{x:}}", {"x": 1}) == "{{x:}}"
    assert _r("{{1x}}", {"1x": 1}) == "{{1x}}"
    assert _r("{{x:Count}}", {"x": 1}) == "{{x:Count}}"
    assert _r("{ {x} }", {"x": 1}) == "{ {x} }"
    assert _r("{{" + "a" * 41 + "}}", {}) == "{{" + "a" * 41 + "}}"


def test_single_pass_value_with_braces_stays_literal():
    assert _r("{{x}}", {"x": "{{y}}", "y": "Z"}) == "{{y}}"
    # a value holding a section token does not open a section either
    assert _r("{{x}} end", {"x": "{{#f}}"}, {"f": True}) == "{{#f}} end"


# ------------------------------------------------------------- post-processing

def test_control_chars_stripped():
    out = _r("a\x00b\x01c\td\x7fe\x1f", {})
    assert out == "abc\tde"


def test_post_processing_order():
    # 1. CRLF before 4. triple-newline collapse
    assert _r("a\r\n\r\n\r\nb", {}) == "a\n\nb"
    # 2. C0 strip before 3. double-space collapse
    assert _r("a \x00 b", {}) == "a b"
    # 3. runs of spaces/tabs -> one space
    assert _r("a    b\t\tc", {}) == "a b c"
    # 5. strip
    assert _r("  \n x \n ", {}) == "x"
    # a C0 control inside a VALUE is stripped too
    assert _r("{{v}}", {"v": "p\x02q"}) == "pq"


def test_render_caps_cut_at_whitespace():
    full = " ".join(f"w{i}" for i in range(3000))        # > 8000 chars, single spaces
    assert len(full) > RENDERED_MAX
    out = _r(full, {})
    assert len(out) <= RENDERED_MAX
    assert out.endswith("…")
    body = out[:-1]
    assert len(body) <= RENDERED_MAX - 1
    assert full.startswith(body.rstrip())
    assert full[len(body.rstrip())] == " "               # never mid-word


def test_render_caps_max_len_for_captions():
    full = " ".join(f"c{i}" for i in range(600))
    assert len(full) > CAPTION_RENDERED_MAX
    out = _r(full, {}, max_len=CAPTION_RENDERED_MAX)
    assert len(out) <= CAPTION_RENDERED_MAX and out.endswith("…")
    assert full[len(out[:-1].rstrip())] == " "
    assert _r("short", {}, max_len=CAPTION_RENDERED_MAX) == "short"


def test_fallback_text_respects_max_len():
    full = " ".join(f"c{i}" for i in range(600))
    out = fallback_text(full, max_len=CAPTION_RENDERED_MAX)
    assert len(out) <= CAPTION_RENDERED_MAX and out.endswith("…")


# ------------------------------------------------------------ clean_* helpers

def test_template_over_cap_rejected(logs):
    good = "x " * 100
    assert clean_template(good, max_len=TEMPLATE_MAX, sid=SID, what="narrative") == good
    assert clean_template("y" * (TEMPLATE_MAX + 1), max_len=TEMPLATE_MAX,
                          sid=SID, what="narrative") is None
    rej = _lines(logs, "NARRATIVE_TEMPLATE_REJECTED")
    assert rej and "reason=too_long" in rej[0]
    assert "yyyy" not in rej[0]
    assert clean_template(5, max_len=TEMPLATE_MAX, sid=SID, what="narrative") is None
    assert clean_template(None, max_len=TEMPLATE_MAX, sid=SID, what="narrative") is None
    assert clean_template("", max_len=TEMPLATE_MAX, sid=SID, what="narrative") is None
    assert clean_template("   ", max_len=TEMPLATE_MAX, sid=SID, what="narrative") is None


def test_clean_captions_maps_keys_and_drops_bad():
    out = clean_captions({"1": "t", "x": "bad", "13": "..", "2": 5}, sid=SID)
    assert out == {1: "t"}
    assert clean_captions(None, sid=SID) == {}
    assert clean_captions("nope", sid=SID) == {}
    assert clean_captions({"1": "a", "2": "b"}, sid=SID) == {1: "a", 2: "b"}


def test_clean_captions_over_cap_dropped(logs):
    out = clean_captions({"1": "ok", "2": "c" * (CAPTION_MAX + 1)}, sid=SID)
    assert out == {1: "ok"}
    rej = _lines(logs, "CAPTION_REJECTED")
    assert rej and "index=2" in rej[0] and "reason=too_long" in rej[0]


# ---------------------------------------------------------- markdown escaping

def test_text_value_markdown_escaped():
    assert _r("{{v}}", {"v": "a*b_c"}) == "a\\*b\\_c"
    assert _r("{{v:text}}", {"v": "a*b"}) == "a\\*b"
    assert _r("{{v:period}}", {"v": "Q3_2025"}) == "Q3\\_2025"
    assert _r("x: {{v}}", {"v": "- lead"}) == "x: \\- lead"
    assert _r("{{v}}", {"v": " - lead"}) == "\\- lead"
    # a dash that is not leading is untouched
    assert _r("{{v}}", {"v": "a - b"}) == "a - b"
    # numbers are never escaped, whatever the kind
    assert _r("{{n:text}}", {"n": -1234}) == "-1,234"
    assert _r("{{n}}", {"n": -1234}) == "-1,234"
    assert _r("{{n:float}}", {"n": -0.5}) == "-0.50"
    # the template's own markdown is left alone
    assert _r("**bold** _it_ - item {{v}}", {"v": "x"}) == "**bold** _it_ - item x"


def test_plain_removes_escapes():
    assert plain("a\\*b\\_c \\- d") == "a*b_c - d"
    assert plain("no escapes") == "no escapes"
    assert plain("") == ""


# ----------------------------------------------------------------- brain_view

def test_brain_view_brackets_slots_resolves_sections():
    t = ("Total {{total_loans:count}} {{#anomaly_confirmed}}spike{{/anomaly_confirmed}}"
         "{{^anomaly_confirmed}}calm{{/anomaly_confirmed}} {{share:pct}}")
    assert brain_view(t, {"anomaly": "anomaly_confirmed"}) == "Total [total loans] spike [share]"
    assert brain_view(t, {}) == "Total [total loans] calm [share]"
    # a parse error -> fallback with "[value]"
    assert brain_view("{{#a}}x {{n}}", {"a": True}) == "x [value]"
    assert brain_view(None, {}) == ""


def test_brain_view_carries_no_value():
    out = brain_view("{{v}} {{#f}}{{w}}{{/f}}", {"f": True})
    assert out == "[v] [w]"


# ------------------------------------------------------------- value_display

def test_value_display_scalar(logs):
    assert value_display(1234, "en") == "**Result:** 1,234"
    assert value_display(0.5, "en") == "**Result:** 0.50"
    assert value_display("North", "en") == "**Result:** North"
    assert value_display(True, "en") == "**Result:** true"
    assert any("VALUE_DISPLAY_APPENDED kind=scalar" in m for m in _lines(logs, "VALUE_DISPLAY_APPENDED"))


def test_value_display_lang_labels():
    assert value_display(1, "ka") == "**შედეგი:** 1"
    assert value_display(1, "ru") == "**Результат:** 1"
    assert value_display(1, "uz") == "**Natija:** 1"
    assert value_display(1, "xx") == "**Result:** 1"


def test_value_display_flat_dict(logs):
    assert value_display({"a": 1, "b": 2.5, "c": "x"}, "en") == "- a: 1\n- b: 2.50\n- c: x"
    many = {f"k{i:02d}": i for i in range(25)}
    out = value_display(many, "en")
    lines = out.split("\n")
    assert len(lines) == 21 and lines[-1] == "- …"
    assert lines[0] == "- k00: 0" and lines[19] == "- k19: 19"
    assert any("kind=dict" in m for m in _lines(logs, "VALUE_DISPLAY_APPENDED"))


def test_value_display_key_cut_and_none():
    out = value_display({"k" * 100: 1}, "en")
    assert out == "- " + "k" * 80 + ": 1"
    assert value_display(None, "en") is None
    assert value_display({}, "en") is None
    assert value_display([1, 2], "en") is None


# ---------------------------------------------------------- answer_lang_code

def test_answer_lang_code_mapping():
    assert answer_lang_code({"answer_language": "English"}, "კითხვა") == "en"
    assert answer_lang_code({"answer_language": "Georgian"}, "q") == "ka"
    assert answer_lang_code({"answer_language": "Russian"}, "q") == "ru"
    assert answer_lang_code({"answer_language": "Uzbek"}, "q") == "uz"
    # unknown / absent -> detection from the question (en/ka/ru only)
    assert answer_lang_code({"answer_language": "Klingon"}, "რა არის") == "ka"
    assert answer_lang_code(None, "что это") == "ru"
    assert answer_lang_code({}, "what") == "en"
    assert answer_lang_code(None, "") == "en"
    assert answer_lang_code("junk", "what") == "en"


def test_answer_lang_code_uzbek_only_from_context_decision():
    assert answer_lang_code(None, "Qancha kredit berilgan?") == "en"    # Latin -> en
    assert answer_lang_code(None, "Қанча кредит берилган?") == "ru"     # Cyrillic -> ru


# ----------------------------------------------------------- history_content

def test_history_content_with_and_without_marker():
    t = "Total {{total:count}}"
    assert history_content(t, {}) == t
    out = history_content(t, {"coverage_dim": "CURRENCY", "needs_phase2": True})
    assert out == "[Findings: coverage_dim=CURRENCY; needs_phase2=true]\n" + t


# ------------------------------------------------------------------- fuzz

def test_render_never_raises_fuzz():
    rng = random.Random(20261023)
    atoms = ["{{", "}}", "{{#", "{{^", "{{/", "a", "b", "flag", "x:count", "y:pct", ":",
             " ", "\n", "\r\n", "*", "_", "-", "\x00", "{", "}", "n/a", "1x", "#", "/",
             "{{x}}", "{{#flag}}", "{{/flag}}", "{{^flag}}", "{{y:amount}}", "{{z:bogus}}"]
    pool = [0, 1, -1, 2.5, float("nan"), float("inf"), True, False, None, "s*t_u",
            "- lead", "{{x}}", "x" * 300, 10 ** 20]
    for _ in range(500):
        t = "".join(rng.choice(atoms) for _ in range(rng.randint(0, 40)))
        values = {k: rng.choice(pool) for k in rng.sample(["x", "y", "z", "a", "flag"], rng.randint(0, 5))}
        findings = {k: rng.choice([True, False, "COL", "anomaly_confirmed", "x"])
                    for k in rng.sample(["flag", "a", "b", "anomaly", "anomaly_confirmed"],
                                        rng.randint(0, 5))}
        out = render(t, values, findings, sid=SID)
        assert isinstance(out, str) and len(out) <= RENDERED_MAX
        assert "\x00" not in out and "\r" not in out
        bv = brain_view(t, findings)
        assert isinstance(bv, str)
        assert isinstance(fallback_text(t), str)
