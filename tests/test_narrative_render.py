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


# ------------------------------------------------------- drop_na_sentences
# The quality gate (BRB retest follow-up 3): a rendered sentence holding the
# "n/a" token (a missing slot, a None value, or a literal the template typed)
# is dropped before display. `render` itself keeps producing "n/a".

def _drop(text, what="narrative"):
    fn = getattr(narrative, "drop_na_sentences", None)
    assert callable(fn), "narrative.drop_na_sentences is missing"
    return fn(text, sid=SID, what=what)


@pytest.mark.parametrize("text,expected", [
    ("Total is 5. Change was n/a in Q3.", "Total is 5."),
    ("n/a", ""),
    ("- a: 1\n- b: n/a\n- c: 3", "- a: 1\n- c: 3"),
    ("Sales grew! Was it n/a? Yes.", "Sales grew! Yes."),
    ("First N/A here. Second stays.", "Second stays."),
    ("Para one.\n\nPara two is n/a.\n\nPara three.", "Para one.\n\nPara three."),
    ("Was n/a.", ""),
    ("One n/a. Two n/a!", ""),
])
def test_drop_na_sentences_cases(logs, text, expected):
    assert _drop(text) == expected


def test_drop_na_sentences_token_not_inside_a_word(logs):
    for text in ("Ratio of banana/apple is high.", "The kn/ab code is fine.",
                 "Plain sentence. Another one."):
        assert _drop(text) == text
    assert _lines(logs, "NARRATIVE_SENTENCE_DROPPED") == []


def test_drop_na_sentences_text_without_na_is_byte_identical(logs):
    text = "Line  one.\n\n\nLine two.  "
    assert _drop(text) == text
    assert _lines(logs, "NARRATIVE_SENTENCE_DROPPED") == []


def test_drop_na_sentences_decimal_point_is_not_a_sentence_end(logs):
    # rendered numbers carry "." (float / pct / ratio / amount): a sentence
    # holding n/a after one is dropped WHOLE, and a kept sentence's number
    # stays intact
    text = "Ratio was 7.9x while the change was n/a. Total is 1,234.57."
    assert _drop(text) == "Total is 1,234.57."


def test_drop_na_sentences_logs_count_once_without_text(logs):
    text = f"Kept {SECRET}. Dropped {SECRET} n/a. Also n/a here! End."
    out = _drop(text, what="caption")
    assert out == f"Kept {SECRET}. End."
    lines = _lines(logs, "NARRATIVE_SENTENCE_DROPPED")
    assert len(lines) == 1
    assert "reason=missing_slot" in lines[0]
    assert "n=2" in lines[0]
    assert "what=caption" in lines[0]
    blob = "\n".join(r.getMessage() for r in logs.records)
    assert SECRET not in blob and "Dropped" not in blob


def test_drop_na_sentences_on_rendered_slots(logs):
    rendered = _r("Total is {{t}}. Change was {{c}} in {{p}}.", {"t": 5, "p": "Q3"})
    assert rendered == "Total is 5. Change was n/a in Q3."      # render is unchanged
    assert _drop(rendered) == "Total is 5."
    assert "n=1" in _lines(logs, "NARRATIVE_SENTENCE_DROPPED")[0]
    # a None VALUE renders n/a too
    bullets = _r("- a: {{a}}\n- b: {{b}}", {"a": 1, "b": None})
    assert _drop(bullets) == "- a: 1"
    # a slot standing alone
    assert _drop(_r("{{x}}", {})) == ""


@pytest.mark.parametrize("bad", [None, 5, ["n/a"], b"n/a"])
def test_drop_na_sentences_never_raises_on_non_str(logs, bad):
    assert _drop(bad) == ""


# ------------------------------------------------------------- value_display

def test_value_display_uz_ru_labels_pinned():
    assert value_display(5, "uz").startswith("**Natija:**")
    assert value_display(5, "ru").startswith("**Результат:**")


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


# =========================================================================
# BRB retest 2 follow-up (DIAGNOSTICS_SPEC A2, A4, A5.1): section-name
# prefixes, parse-error kinds, locales, the sections a render showed, and the
# fixed FINDING sentences. Every new symbol is read through `getattr` inside
# the test so the file collects before the code exists.
# =========================================================================

import re                                                       # noqa: E402

NBSP = " "
LANGS = ("en", "uz", "ru", "ka")


def _fn(name):
    fn = getattr(narrative, name, None)
    assert callable(fn), f"narrative.{name} is missing"
    return fn


# ------------------------------------------------------- section prefixes

# A prefix resolves only when the block code PASSED it (`prefix="ovl_"`, the
# brain's template-validation rule): the caller hands the passed set in as
# `prefixes`. Reviewer fix: a bare `[a-z0-9]{1,10}_` strip turned
# `{{#no_thin_period}}` into `thin_period` and showed it exactly when the
# author meant the opposite.

def test_prefixed_section_resolves_to_enum_code():
    t = "A{{#ovl_metric_definition_overlap}} stock{{/ovl_metric_definition_overlap}}."
    ovl = {"ovl_"}
    assert _r(t, {}, {"metric_relation": "metric_definition_overlap"}, prefixes=ovl) == "A stock."
    assert _r(t, {}, {"metric_relation": "other"}, prefixes=ovl) == "A."
    assert _r(t, {}, {}, prefixes=ovl) == "A."


def test_prefixed_section_resolves_to_registry_key():
    t = "{{#lg_thin_period}}thin{{/lg_thin_period}}{{^lg_thin_period}}full{{/lg_thin_period}}"
    assert _r(t, {}, {"thin_period": True}, prefixes={"lg_"}) == "thin"
    assert _r(t, {}, {}, prefixes={"lg_"}) == "full"
    # digits are allowed in the prefix after its first letter, up to 10 characters
    assert _r("{{#q3_thin_period}}x{{/q3_thin_period}}", {}, {"thin_period": True},
              prefixes={"q3_"}) == "x"
    assert _r("{{#abcdefghij_thin_period}}x{{/abcdefghij_thin_period}}", {},
              {"thin_period": True}, prefixes=["abcdefghij_"]) == "x"


def test_passed_prefix_resolves_others_do_not():
    t = "{{#ovl_thin_period}}x{{/ovl_thin_period}}"
    f = {"thin_period": True}
    assert _r(t, {}, f, prefixes={"ovl_"}) == "x"
    assert _r(t, {}, f, prefixes={"ovl_", "lg_"}) == "x"
    assert _r(t, {}, f, prefixes={"lg_"}) == ""
    assert _r(t, {}, f, prefixes=None) == ""
    assert _r(t, {}, f, prefixes=set()) == ""
    assert _r(t, {}, f) == ""
    # only `[a-z][a-z0-9]{0,9}_` strings count as passed prefixes
    passed = narrative.passed_prefixes({"3q_", "ovl_", "Ovl_", "abcdefghijk_", "a", 5})
    assert passed == frozenset({"ovl_"})
    assert narrative.passed_prefixes(None) == frozenset()


@pytest.mark.parametrize("name", ["no_thin_period", "not_mixed_units", "has_coverage_change"])
@pytest.mark.parametrize("prefixes", [None, {"ovl_"}])
def test_negating_prefix_never_resolves(name, prefixes, logs):
    findings = {"thin_period": True, "units": "mixed_units", "coverage": "coverage_change"}
    t = "{{#" + name + "}}x{{/" + name + "}}{{^" + name + "}}y{{/" + name + "}}"
    # never shown as the bare code: the author meant the opposite
    assert _r(t, {}, findings, prefixes=prefixes) == "y"
    assert len(_lines(logs, "NARRATIVE_SECTION_UNKNOWN")) == 1


def test_exact_section_name_wins_over_prefix():
    t = "{{#lg_thin_period}}x{{/lg_thin_period}}"
    # the exact name is a findings key (False): the prefix is not consulted
    assert _r(t, {}, {"lg_thin_period": False, "thin_period": True}, prefixes={"lg_"}) == ""
    assert _r(t, {}, {"lg_thin_period": True}) == "x"
    assert _r("{{#thin_period}}x{{/thin_period}}", {}, {"thin_period": True}) == "x"
    assert _r("{{#metric_definition_overlap}}x{{/metric_definition_overlap}}", {},
              {"metric_relation": "metric_definition_overlap"}) == "x"


@pytest.mark.parametrize("name,prefix", [
    ("abcdefghijk_thin_period", "abcdefghijk_"),     # 11-character prefix
    ("LG_thin_period", "LG_"),                       # uppercase prefix
    ("lg_thin_periodx", "lg_"),                      # not a code after the prefix
    ("_thin_period", "_"),                           # empty prefix
])
def test_bad_prefix_does_not_resolve(name, prefix, logs):
    # even when "passed": a prefix outside `[a-z][a-z0-9]{0,9}_` never resolves
    t = "{{#" + name + "}}x{{/" + name + "}}"
    assert _r(t, {}, {"thin_period": True}, prefixes={prefix}) == ""


def test_unknown_section_logs_once_per_render(logs):
    t = "{{#zz_flag}}a{{/zz_flag}} b {{#zz_flag}}c{{/zz_flag}} {{^zz_flag}}d{{/zz_flag}}"
    assert _r(t, {}, {}) == "b d"
    lines = _lines(logs, "NARRATIVE_SECTION_UNKNOWN")
    assert len(lines) == 1
    assert "name=zz_flag" in lines[0]
    _r(t, {}, {})
    assert len(_lines(logs, "NARRATIVE_SECTION_UNKNOWN")) == 2


def test_known_section_names_are_not_unknown(logs):
    # a findings key, an enum code, a registry key and a resolvable prefix —
    # absent or present — never log as unknown
    _r("{{#flag}}x{{/flag}}", {}, {"flag": True})
    _r("{{#no_anomaly}}x{{/no_anomaly}}", {}, {})
    _r("{{#thin_period}}x{{/thin_period}}", {}, {})
    _r("{{#lg_thin_period}}x{{/lg_thin_period}}", {}, {}, prefixes={"lg_"})
    _r("{{#ovl_metric_definition_overlap}}x{{/ovl_metric_definition_overlap}}", {}, {},
       prefixes={"ovl_"})
    assert _lines(logs, "NARRATIVE_SECTION_UNKNOWN") == []


def test_prefixed_name_without_passed_prefix_is_unknown(logs):
    assert _r("{{#lg_thin_period}}x{{/lg_thin_period}}", {}, {"thin_period": True}) == ""
    lines = _lines(logs, "NARRATIVE_SECTION_UNKNOWN")
    assert len(lines) == 1 and "name=lg_thin_period" in lines[0]


def test_unknown_section_log_is_escaped_and_capped(logs):
    name = "zz_" + "q" * 37                      # 40 characters, the grammar's maximum
    _r("{{#" + name + "}}x{{/" + name + "}}", {}, {})
    lines = _lines(logs, "NARRATIVE_SECTION_UNKNOWN")
    assert len(lines) == 1 and ("name=" + name) in lines[0]


def test_brain_view_resolves_prefixed_sections():
    t = ("{{#ovl_metric_definition_overlap}}stock {{share:pct}}"
         "{{/ovl_metric_definition_overlap}}{{#lg_thin_period}} thin{{/lg_thin_period}}")
    f = {"metric_relation": "metric_definition_overlap", "thin_period": True}
    out = brain_view(t, f, prefixes={"ovl_", "lg_"})
    assert out == "stock [share] thin"
    assert brain_view(t, {}, prefixes={"ovl_", "lg_"}) == ""
    # no passed prefixes (routes/report.py, the title): prefixed sections are false
    assert brain_view(t, f) == ""
    assert brain_view("{{#no_thin_period}}x{{/no_thin_period}}", {"thin_period": True}) == ""


# ------------------------------------------------------- parse-error kinds

@pytest.mark.parametrize("template,kind", [
    ("{{#a}}x{{#b}}y{{/b}}{{/a}}", "nested"),
    ("{{#a}}x", "unclosed"),
    ("{{#a}}x{{/b}}", "mismatched"),
    ("x {{/a}} y", "mismatched"),               # a close without an open
])
def test_template_error_kind(template, kind):
    with pytest.raises(TemplateError) as err:
        parse(template)
    assert getattr(err.value, "kind", None) == kind
    assert _fn("template_error_kind")(template) == kind


@pytest.mark.parametrize("template", ["", "plain", "a {{x}} {{#f}}y{{/f}} {{^g}}z{{/g}}",
                                      "{{ x }} {{1x}}"])
def test_template_error_kind_none_for_valid(template):
    assert _fn("template_error_kind")(template) is None


def test_template_error_kind_crash_is_logged_and_none(monkeypatch, logs):
    """A crash that is not a TemplateError is no verdict on the template: it
    must never spend a brain retry (Article IV: logged, safe fallback)."""
    def boom(_t):
        raise RuntimeError("secret template text")

    monkeypatch.setattr(narrative, "parse", boom)
    assert _fn("template_error_kind")("{{#a}}x{{/a}}") is None
    lines = _lines(logs, "NARRATIVE_TEMPLATE_CHECK_FAILED")
    assert len(lines) == 1 and "error=RuntimeError" in lines[0]
    assert "secret" not in "\n".join(r.getMessage() for r in logs.records)


class _Exploding(str):
    def replace(self, *a, **k):
        raise RuntimeError("secret text")

    def __contains__(self, item):
        raise RuntimeError("secret text")


def test_first_sentence_has_na_crash_is_logged(logs):
    assert narrative.first_sentence_has_na(_Exploding("Total is n/a.")) is False
    lines = _lines(logs, "NARRATIVE_FIRST_SENTENCE_CHECK_FAILED")
    assert len(lines) == 1 and "error=RuntimeError" in lines[0]
    assert "secret" not in "\n".join(r.getMessage() for r in logs.records)


def test_covered_codes_crash_is_logged(logs):
    assert narrative.covered_codes("It counts.", [("units", _Exploding("It counts."))]) == set()
    lines = _lines(logs, "NARRATIVE_COVERED_CHECK_FAILED")
    assert len(lines) == 1 and "error=RuntimeError" in lines[0]
    assert "secret" not in "\n".join(r.getMessage() for r in logs.records)


# ------------------------------------------------------------------ locale

def test_locale_for():
    locale_for = _fn("locale_for")
    assert locale_for("uz") == "uz"
    assert locale_for("ru") == "ru"
    for lang in ("en", "ka", "xx", "", None):
        assert locale_for(lang) is None


def test_render_with_locale():
    out = render("{{a:amount}} | {{c:count}}", {"a": 2345678901, "c": 1234567}, {},
                 sid=SID, locale="uz")
    assert out == f"2,35 mlrd | 1{NBSP}234{NBSP}567"
    ru = render("{{a:amount}} {{p:pct}}", {"a": 2345678901, "p": 0.375}, {}, sid=SID, locale="ru")
    assert ru == "2,35 млрд 37,5%"
    # no locale: the default, unchanged
    assert render("{{c:count}}", {"c": 1234567}, {}, sid=SID) == "1,234,567"
    assert render("{{c:count}}", {"c": 1234567}, {}, sid=SID, locale=None) == "1,234,567"


def test_value_display_uses_language_locale():
    assert value_display(1234567, "ru") == f"**Результат:** 1{NBSP}234{NBSP}567"
    assert value_display(1234567, "uz") == f"**Natija:** 1{NBSP}234{NBSP}567"
    assert value_display(1234567, "en") == "**Result:** 1,234,567"
    assert value_display(1234567, "ka") == "**შედეგი:** 1,234,567"
    assert value_display({"a": 2345678901.0}, "ru") == f"- a: 2{NBSP}345{NBSP}678{NBSP}901,00"


# ------------------------------------------------------- render_with_sections

def test_render_with_sections_reports_shown_sections():
    rws = _fn("render_with_sections")
    t = ("Total {{t}}. {{#metric_definition_overlap}}It counts a stock.{{/metric_definition_overlap}}"
         " {{#lg_thin_period}}One month is thin.{{/lg_thin_period}}")
    findings = {"metric_relation": "metric_definition_overlap", "thin_period": True}
    text, sections = rws(t, {"t": 5}, findings, sid=SID, prefixes={"lg_"})
    assert text == "Total 5. It counts a stock. One month is thin."
    assert text == render(t, {"t": 5}, findings, sid=SID, prefixes={"lg_"})
    shown = {(code, body.strip()) for code, body in sections}
    assert ("metric_definition_overlap", "It counts a stock.") in shown
    assert ("thin_period", "One month is thin.") in shown      # the prefix resolved


def test_render_with_sections_prefixed_body_renders_slots():
    rws = _fn("render_with_sections")
    t = "{{#ovl_metric_definition_overlap}}Equal in {{s:pct}} of rows.{{/ovl_metric_definition_overlap}}"
    text, sections = rws(t, {"s": 0.976}, {"metric_relation": "metric_definition_overlap"},
                         sid=SID, prefixes={"ovl_"})
    assert text == "Equal in 97.6% of rows."
    assert [(c, b.strip()) for c, b in sections] == [("metric_definition_overlap",
                                                      "Equal in 97.6% of rows.")]


def test_render_with_sections_hidden_section_not_reported():
    rws = _fn("render_with_sections")
    t = "A. {{#metric_definition_overlap}}Stock.{{/metric_definition_overlap}}"
    text, sections = rws(t, {}, {}, sid=SID)
    assert text == "A."
    assert all(code != "metric_definition_overlap" for code, _b in sections)


def test_render_with_sections_locale():
    rws = _fn("render_with_sections")
    text, _sections = rws("{{a:amount}}", {"a": 7250000}, {}, sid=SID, locale="ru")
    assert text == "7,25 млн"


# --------------------------------------------------------- FINDING sentences
# The English wording is pinned exactly (the plan's six sentences); the uz,
# ru and ka wording is the code writer's, so only its STRUCTURE is pinned:
# the formatted values inside "(…)", no "(" and no "n/a" without values.

EN_WITH = {
    "mixed_units": "Amounts carry more than one currency or unit label, so they are "
                   "reported per label and never added together.",
    "coverage_change": "The set of values in one dimension changes between periods (CURRENCY), "
                       "so totals are not comparable across periods.",
    "thin": "One period looks incomplete (2026 Февраль): it has far fewer rows or almost no "
            "value compared with the other periods, so it is treated as a data gap, not a "
            "business change.",
    "unknown_dominated": "Many rows have no value in one dimension (SEKTOR; up to 100.0% "
                         "Unknown), so that dimension cannot explain any change.",
    "metric_definition_overlap": "In most rows this metric equals another metric of the data "
                                 "(97.6% of rows), so it counts a stock, not new items.",
    "latest_is_past_period": "The data ends before the current month (latest period: 2026 Март).",
}
EN_WITHOUT = {k: re.sub(r" \([^)]*\)", "", v) for k, v in EN_WITH.items()}

# (findings that fire it, VALUES with every slot, the expected formatted
# parts as (value, kind) for the parenthesis)
CASES = {
    "mixed_units": ({"units": "mixed_units"}, {}, []),
    "coverage_change": ({"coverage": "coverage_change"}, {"coverage_dim_label": "CURRENCY"},
                        [("CURRENCY", "text")]),
    "thin": ({"thin_period": True}, {"thin_period_label": "2026 Февраль"},
             [("2026 Февраль", "text")]),
    "unknown_dominated": ({"unknown_dominated": True},
                          {"unknown_dim_label": "SEKTOR", "unknown_share_max": 1.0},
                          [("SEKTOR", "text"), (1.0, "pct")]),
    "metric_definition_overlap": ({"metric_relation": "metric_definition_overlap"},
                                  {"overlap_equal_share": 0.976}, [(0.976, "pct")]),
    "latest_is_past_period": ({"latest_is_past_period": True}, {"latest_period": "2026 Март"},
                              [("2026 Март", "text")]),
}
# the base each case is added to: one sentence, so the new paragraph goes last
BASE = {"en": "Total is 5.", "uz": "Jami 5.", "ru": "Итого 5.", "ka": "სულ 5."}


def _add(text, findings, values, covered=(), lang="en"):
    return _fn("add_finding_sentences")(text, findings, values, set(covered), lang, sid=SID)


def _added(lang, findings, values, covered=()):
    base = BASE[lang]
    out = _add(base, findings, values, covered, lang)
    assert out.startswith(base), out
    return out[len(base):].strip()


def _locale(lang):
    return _fn("locale_for")(lang)


def test_finding_sentences_table_has_every_finding_and_language():
    table = getattr(narrative, "FINDING_SENTENCES", None)
    assert isinstance(table, dict), "narrative.FINDING_SENTENCES is missing"
    # 6 entries + the BRB run-3 follow-up's scope_change / label_disappeared
    assert len(table) == 8
    for key in ("mixed_units", "coverage_change", "unknown_dominated",
                "metric_definition_overlap", "latest_is_past_period",
                "scope_change", "label_disappeared"):
        assert key in table, key
    assert {"thin_period", "data_gap"} & set(table), sorted(table)
    for key, by_lang in table.items():
        assert set(LANGS) <= set(by_lang), (key, sorted(by_lang))
        for lang in LANGS:
            entry = by_lang[lang]
            # (base, paren) or, since the run-3 follow-up, (base, paren, full)
            assert isinstance(entry, tuple) and len(entry) in (2, 3), (key, lang)
            assert all(isinstance(part, str) for part in entry), (key, lang)
            assert entry[0].strip(), (key, lang)


@pytest.mark.parametrize("finding", list(EN_WITH))
def test_finding_sentence_english_wording_exact(finding):
    findings, values, _parts = CASES[finding]
    assert _added("en", findings, values) == EN_WITH[finding]
    assert _added("en", findings, {}) == EN_WITHOUT[finding]


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("finding", list(CASES))
def test_finding_sentence_with_values_has_formatted_parenthesis(finding, lang):
    findings, values, parts = CASES[finding]
    added = _added(lang, findings, values)
    assert added and "n/a" not in added
    if not parts:
        assert "(" not in added
        assert added == _added(lang, findings, {})       # no slots: same sentence
        return
    groups = re.findall(r"\(([^)]*)\)", added)
    assert groups, added
    for value, kind in parts:
        shown = pdc_diag.fmt(value, kind, _locale(lang))
        assert any(shown in g for g in groups), (finding, lang, shown, added)


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("finding", list(CASES))
def test_finding_sentence_without_values_has_no_parenthesis(finding, lang):
    findings, _values, _parts = CASES[finding]
    added = _added(lang, findings, {})
    assert added
    assert "(" not in added and ")" not in added
    assert "n/a" not in added.lower()


def test_finding_sentences_uz_and_ru_use_their_locale():
    _f, values, _p = CASES["metric_definition_overlap"]
    findings = CASES["metric_definition_overlap"][0]
    assert "97,6%" in _added("uz", findings, values)
    assert "97,6%" in _added("ru", findings, values)
    assert "97.6%" in _added("ka", findings, values)
    assert "97.6%" in _added("en", findings, values)


@pytest.mark.parametrize("findings", [
    {"units": "single_unit"}, {"coverage": "coverage_stable"}, {"thin_period": False},
    {"period_status": "partial_period"}, {"unknown_dominated": False},
    {"metric_relation": "other"}, {"latest_is_past_period": False},
    {"latest_is_current_period": True}, {},
])
def test_finding_sentences_not_fired(findings, logs):
    text = "Total is 5. The chart shows it."
    assert _add(text, findings, {"coverage_dim_label": "CURRENCY"}) == text


def test_thin_fires_on_data_gap_status_and_falls_back_to_gap_period():
    added = _added("en", {"period_status": "data_gap"}, {"data_gap_period": "2026 Февраль"})
    assert added == EN_WITH["thin"]
    # thin_period_label wins over data_gap_period
    added = _added("en", {"thin_period": True, "period_status": "data_gap"},
                   {"thin_period_label": "2026 Февраль", "data_gap_period": "2025 Ноябрь"})
    assert added == EN_WITH["thin"]
    # both fire on one finding sentence, never two
    assert added.count("looks incomplete") == 1


def test_coverage_falls_back_to_coverage_dim_finding():
    added = _added("en", {"coverage": "coverage_change", "coverage_dim": "CURRENCY"}, {})
    assert added == EN_WITH["coverage_change"]


def test_unknown_falls_back_to_unknown_dim_finding_without_share():
    added = _added("en", {"unknown_dominated": True, "unknown_dim": "SEKTOR"},
                   {"unknown_share_max": 1.0})
    assert added == ("Many rows have no value in one dimension (SEKTOR), so that dimension "
                     "cannot explain any change.")
    assert "%" not in added


def test_prefixed_value_key_is_found():
    added = _added("en", {"metric_relation": "metric_definition_overlap"},
                   {"ovl_overlap_equal_share": 0.976})
    assert added == EN_WITH["metric_definition_overlap"]
    added = _added("en", {"latest_is_past_period": True}, {"cov_latest_period": "2026 Март"})
    assert added == EN_WITH["latest_is_past_period"]


def test_none_or_na_value_drops_the_part():
    findings = {"unknown_dominated": True}
    added = _added("en", findings, {"unknown_dim_label": "SEKTOR", "unknown_share_max": None})
    assert added == ("Many rows have no value in one dimension (SEKTOR), so that dimension "
                     "cannot explain any change.")
    added = _added("en", {"latest_is_past_period": True}, {"latest_period": None})
    assert added == EN_WITHOUT["latest_is_past_period"]
    added = _added("en", {"coverage": "coverage_change"}, {"coverage_dim_label": "n/a"})
    assert added == EN_WITHOUT["coverage_change"]


@pytest.mark.parametrize("finding,codes", [
    ("mixed_units", ["mixed_units", "units"]),
    ("coverage_change", ["coverage_change"]),
    ("thin", ["thin_period", "data_gap"]),
    ("unknown_dominated", ["unknown_dominated"]),
    ("metric_definition_overlap", ["metric_definition_overlap", "metrics_identical",
                                   "metric_relation"]),
    ("latest_is_past_period", ["latest_is_past_period"]),
])
def test_covered_finding_gets_no_sentence(finding, codes):
    findings, values, _parts = CASES[finding]
    base = "Total is 5."
    for code in codes:
        assert _add(base, findings, values, [code]) == base, code


def test_all_six_in_table_order_one_paragraph():
    findings, values = {}, {}
    for f, v, _p in CASES.values():
        findings.update(f)
        values.update(v)
    out = _add("Total is 5.", findings, values)
    paragraphs = out.split("\n\n")
    assert paragraphs[0] == "Total is 5."
    assert len(paragraphs) == 2, out
    added = paragraphs[1]
    order = ["mixed_units", "coverage_change", "thin", "unknown_dominated",
             "metric_definition_overlap", "latest_is_past_period"]
    positions = [added.find(EN_WITH[k]) for k in order]
    assert all(p >= 0 for p in positions), (positions, added)
    assert positions == sorted(positions)


MIXED = EN_WITH["mixed_units"]


def test_placement_before_reading_guide():
    head = "Total is 5. Most loans are in Tashkent."
    guide = "The table below shows the totals by region."
    out = _add(f"{head} {guide}", {"units": "mixed_units"}, {})
    assert out.split("\n\n") == [head, MIXED, guide]


@pytest.mark.parametrize("guide", ["The chart shows the monthly trend.",
                                   "See the bar for each branch.",
                                   "The waterfall breaks the change down.",
                                   "The graph compares both years."])
def test_placement_reading_guide_words(guide):
    out = _add(f"Total is 5. {guide}", {"units": "mixed_units"}, {})
    assert out.split("\n\n") == ["Total is 5.", MIXED, guide]


@pytest.mark.parametrize("lang,head,guide", [
    ("uz", "Jami 5.", "Quyidagi jadval hududlar kesimida jamini ko'rsatadi."),
    ("uz", "Jami 5.", "Oylik o'zgarish grafikda ko'rsatilgan."),
    ("ru", "Итого 5.", "Ниже в таблице показаны итоги по регионам."),
    ("ru", "Итого 5.", "На графике показана динамика."),
    ("ka", "სულ 5.", "ცხრილში ნაჩვენებია ჯამები რეგიონების მიხედვით."),
])
def test_placement_reading_guide_other_languages(lang, head, guide):
    out = _add(f"{head} {guide}", {"units": "mixed_units"}, {}, lang=lang)
    paragraphs = out.split("\n\n")
    assert len(paragraphs) == 3, out
    assert paragraphs[0] == head and paragraphs[2] == guide
    assert paragraphs[1] == _added(lang, {"units": "mixed_units"}, {})


def test_placement_at_end_without_reading_guide():
    text = "Total is 5. Most loans are in Tashkent."
    assert _add(text, {"units": "mixed_units"}, {}).split("\n\n") == [text, MIXED]


def test_placement_single_sentence_reading_guide_appends_after():
    text = "The table below shows the totals by region."
    assert _add(text, {"units": "mixed_units"}, {}).split("\n\n") == [text, MIXED]


def test_finding_sentences_log_count_without_values(logs):
    secret_dim = "SECRETDIM987"
    secret_period = "SECRETPERIOD654"
    findings = {"coverage": "coverage_change", "latest_is_past_period": True}
    values = {"coverage_dim_label": secret_dim, "latest_period": secret_period}
    out = _add("Total is 5.", findings, values, lang="uz")
    assert secret_dim in out and secret_period in out
    lines = _lines(logs, "FINDING_SENTENCES_ADDED")
    assert len(lines) == 1
    assert "n=2" in lines[0] and "lang=uz" in lines[0]
    blob = "\n".join(r.getMessage() for r in logs.records)
    assert secret_dim not in blob and secret_period not in blob
