"""`pdc_diag.fmt(value, kind, locale=None)` — the slot formatter of the
narrative template (docs/DIAGNOSTICS_SPEC.md §A4, decision D5).

Pure unit tests, offline. Every magnitude is invented. The example table of
§A4 is pinned row by row; each numbered rule of §A4 that the table does not
exercise on its own has a test of its own below.
"""
import numpy as np
import pytest

import pdc_diag
from pdc_diag import fmt

# §A4 example table, verbatim.
EXAMPLES = [
    (1234567, "count", "1,234,567"),
    (1234567.4, "count", "1,234,567"),
    (2.5, "count", "2"),
    (3.5, "count", "4"),
    (-1234, "count", "-1,234"),
    (0, "count", "0"),
    (1234.5678, "float", "1,234.57"),
    (3, "float", "3.00"),
    (5e-7, "float", "0.00"),
    (-0.001, "float", "0.00"),
    (0.375, "pct", "37.5%"),
    (0.1234, "pct", "12.3%"),
    (1, "pct", "100.0%"),
    (-0.031, "pct", "-3.1%"),
    (12.5, "pct", "1,250.0%"),
    (3.27, "ratio", "3.3x"),
    (12, "ratio", "12.0x"),
    (0.48, "ratio", "0.5x"),
    (6.6e-6, "ratio", "<0.1x"),
    (1e9, "ratio", ">1000x"),
    (2345678901, "amount", "2.35 bln"),
    (7250000, "amount", "7.25 mln"),
    (999999, "amount", "999,999"),
    (1500.6, "amount", "1,501"),
    (-3700000000, "amount", "-3.70 bln"),
    (5e12, "amount", "5.00 trln"),
    (7.5e15, "amount", "7,500.00 trln"),
    ("North", "text", "North"),
    ("x" * 250, "text", "x" * 200),
    ("Q3", "period", "Q3"),
    (7, "text", "7"),
    (0.25, "period", "0.25"),
    (None, "count", "n/a"),
    (float("nan"), "count", "n/a"),
    (float("inf"), "amount", "n/a"),
    ("abc", "count", "abc"),
    (True, "count", "true"),
    (False, "text", "false"),
    (np.int64(42), "count", "42"),
    (np.float64(0.25), "pct", "25.0%"),
    (1e40, "count", "n/a"),
    (123456789012345678, "count", "123,456,789,012,345,678"),
    (42, "bogus", "42"),
    (0.5, None, "0.50"),
]


@pytest.mark.parametrize("value,kind,expected", EXAMPLES,
                         ids=[f"{i}:{k}" for i, (_, k, _e) in enumerate(EXAMPLES)])
def test_fmt_examples_table(value, kind, expected):
    out = fmt(value, kind)
    assert isinstance(out, str)
    assert out == expected


# --- rule 8: never scientific -------------------------------------------------

@pytest.mark.parametrize("value,kind", [
    (5e-7, "float"),
    (1e-12, "pct"),
    (1e20, "count"),
    (1e20, "float"),
    (123456789.0, "count"),
    (1e-9, "amount"),
    (1e15, "amount"),
    (2.5e-5, "ratio"),
    (1e22, "count"),
])
def test_fmt_never_scientific(value, kind):
    out = fmt(value, kind)
    # Either a plain fixed-point rendering or "n/a" (over the length cap) —
    # never Python's "1e+20" / "5e-07" forms. ("n/a", "mln", "bln", "trln"
    # and "%"/"x" carry no "e", so the check is exact for these kinds.)
    assert "e" not in out.lower(), out
    assert "+" not in out, out


# --- ratio edges (BRB retest follow-up: a 6.6e-6 ratio rendered "0.0x") -------

@pytest.mark.parametrize("value,expected", [
    (0.0, "<0.1x"),
    (6.6e-6, "<0.1x"),
    (0.049, "<0.1x"),
    (0.05, "0.1x"),
    (0.48, "0.5x"),
    (3.27, "3.3x"),
    (1000, "1,000.0x"),
    (1000.5, ">1000x"),
    (1e9, ">1000x"),
    (np.float64(6.6e-6), "<0.1x"),
    (np.int64(5000), ">1000x"),
])
def test_fmt_ratio_edges(value, expected):
    assert fmt(value, "ratio") == expected


def test_fmt_ratio_negative_keeps_fixed_point_rule():
    # the edge rule covers 0 <= v < 0.05 and v > 1000 only; a negative ratio
    # keeps ",.1f" + "x" with the negative-zero rule (rule 9)
    assert fmt(-0.04, "ratio") == "0.0x"
    assert fmt(-1.5, "ratio") == "-1.5x"
    assert fmt(-5000, "ratio") == "-5,000.0x"


# --- rule 3: bool before int ---------------------------------------------------

@pytest.mark.parametrize("value,kind,expected", [
    (True, "count", "true"),
    (False, "count", "false"),
    (True, "float", "true"),
    (True, "pct", "true"),
    (False, "ratio", "false"),
    (True, "amount", "true"),
    (False, "text", "false"),
    (True, "period", "true"),
    (True, None, "true"),
    (True, "bogus", "true"),
    (np.bool_(True), "count", "true"),
    (np.bool_(False), "pct", "false"),
])
def test_fmt_bool_before_int(value, kind, expected):
    # `bool` is an `int` in Python: without the ordering rule True would
    # render as "1" (count) or "100.0%" (pct).
    assert fmt(value, kind) == expected


# --- rules 1 and 7: None / NaN / ±inf ------------------------------------------

@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), float("-inf"),
                                   np.float64("nan"), np.float64("inf")])
@pytest.mark.parametrize("kind", ["count", "float", "pct", "ratio", "amount", None, "bogus"])
def test_fmt_na_inputs(value, kind):
    assert fmt(value, kind) == "n/a"


def test_fmt_none_is_na_for_text_kinds_too():
    assert fmt(None, "text") == "n/a"
    assert fmt(None, "period") == "n/a"


# --- rule 11: FMT_MAX_LEN -----------------------------------------------------

def test_fmt_long_number_na():
    assert fmt(1e40, "count") == "n/a"
    assert fmt(1e40, "float") == "n/a"
    assert fmt(1e35, "amount") == "n/a"          # 1e23 trln, far over the cap
    assert fmt(10 ** 24, "count") == "n/a"       # 25 digits + 8 commas = 33 chars
    assert fmt(1e40, "pct") == "n/a"


def test_fmt_max_len_constant_and_boundary():
    assert pdc_diag.FMT_MAX_LEN == 32
    # 24 digits + 7 commas = 31 chars: kept, exact (no float conversion).
    assert fmt(10 ** 23, "count") == "100,000,000,000,000,000,000,000"
    assert len(fmt(10 ** 23, "count")) == 31
    # "-" + 24 digits + 7 commas = exactly 32 chars: still kept.
    exact = fmt(-(10 ** 23), "count")
    assert len(exact) == 32, exact
    assert exact == format(-(10 ** 23), ",")
    # one more digit crosses the cap
    assert fmt(10 ** 24, "count") == "n/a"


# --- rule 9: negative zero -----------------------------------------------------

@pytest.mark.parametrize("value,kind,expected", [
    (-0.001, "float", "0.00"),
    (-0.0, "count", "0"),
    (-0.0, "float", "0.00"),
    (-0.4, "count", "0"),
    (-0.0001, "pct", "0.0%"),
    (-0.04, "ratio", "0.0x"),
    (-0.3, "amount", "0"),
    (-1e-9, "amount", "0"),
    (np.float64(-0.0), "float", "0.00"),
])
def test_fmt_negative_zero(value, kind, expected):
    out = fmt(value, kind)
    assert out == expected
    assert not out.startswith("-")


def test_fmt_negative_zero_rule_keeps_real_negatives():
    assert fmt(-0.01, "float") == "-0.01"
    assert fmt(-1, "count") == "-1"
    assert fmt(-0.001, "pct") == "-0.1%"
    assert fmt(-1000000, "amount") == "-1.00 mln"


# --- rule 10: locale -----------------------------------------------------------

def test_fmt_unknown_locale_is_default():
    assert fmt(1234.5, "float", locale="xx") == "1,234.50"
    assert fmt(1234567, "count", locale="") == "1,234,567"
    assert fmt(2345678901, "amount", locale="no-such-locale") == "2.35 bln"
    assert fmt(0.375, "pct", locale="??") == "37.5%"
    assert fmt(1234.5, "float", locale=None) == "1,234.50"


def test_fmt_default_locale_entry_exists():
    assert "default" in pdc_diag._LOCALES
    d = pdc_diag._LOCALES["default"]
    assert d["thousands"] == "," and d["decimal"] == "."
    assert (d["mln"], d["bln"], d["trln"]) == ("mln", "bln", "trln")
    assert fmt(1234.5, "float", locale="default") == "1,234.50"


def test_fmt_locale_swaps_separators_through_placeholder(monkeypatch):
    """A locale whose thousands separator is the default's DECIMAL separator
    (and vice versa) must swap through a placeholder: a naive two-step
    `replace` would turn every "," into "." and then every "." back into ","."""
    monkeypatch.setitem(pdc_diag._LOCALES, "tt", {
        "thousands": ".", "decimal": ",", "mln": "mln.", "bln": "mlrd", "trln": "trln.",
    })
    assert fmt(1234567.891, "float", locale="tt") == "1.234.567,89"
    assert fmt(1234567, "count", locale="tt") == "1.234.567"
    assert fmt(0.375, "pct", locale="tt") == "37,5%"
    assert fmt(3.27, "ratio", locale="tt") == "3,3x"
    assert fmt(2345678901, "amount", locale="tt") == "2,35 mlrd"
    assert fmt(7250000, "amount", locale="tt") == "7,25 mln."
    assert fmt(5e12, "amount", locale="tt") == "5,00 trln."
    assert fmt(999999, "amount", locale="tt") == "999.999"
    # text and n/a are untouched by the locale
    assert fmt("North", "text", locale="tt") == "North"
    assert fmt(None, "count", locale="tt") == "n/a"


def test_fmt_locale_word_swap_without_separator_change(monkeypatch):
    monkeypatch.setitem(pdc_diag._LOCALES, "ww", {
        "thousands": ",", "decimal": ".", "mln": "M", "bln": "B", "trln": "T",
    })
    assert fmt(7250000, "amount", locale="ww") == "7.25 M"
    assert fmt(2345678901, "amount", locale="ww") == "2.35 B"
    assert fmt(5e12, "amount", locale="ww") == "5.00 T"
    assert fmt(1234.5, "float", locale="ww") == "1,234.50"


# --- rule 8 amount: trln --------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (1e12, "1.00 trln"),
    (5e12, "5.00 trln"),
    (1.5e13, "15.00 trln"),
    (-2e12, "-2.00 trln"),
    (999999999999, "1,000.00 bln"),       # just under 1e12 stays bln
    (1e9, "1.00 bln"),
    (999999999, "1,000.00 mln"),          # just under 1e9 stays mln
    (1e6, "1.00 mln"),
    (999999.4, "999,999"),                # under 1e6: the count rule
])
def test_fmt_amount_thresholds_and_trln(value, expected):
    assert fmt(value, "amount") == expected


# --- rule 5: text / period caps at 200 ---------------------------------------

@pytest.mark.parametrize("kind", ["text", "period"])
def test_fmt_text_kind_caps_200(kind):
    assert fmt("y" * 250, kind) == "y" * 200
    assert fmt("y" * 200, kind) == "y" * 200
    assert fmt("y" * 199, kind) == "y" * 199
    assert fmt("", kind) == ""


@pytest.mark.parametrize("kind", ["text", "period"])
def test_fmt_text_kind_with_number_uses_type_default(kind):
    # rule 5: an int / float under a text kind takes its type's default kind
    assert fmt(1234567, kind) == "1,234,567"
    assert fmt(1234.5678, kind) == "1,234.57"
    assert fmt(np.int64(1234567), kind) == "1,234,567"


# --- rule 6: a numeric kind with a non-numeric value -> the text rule ---------

@pytest.mark.parametrize("kind", ["count", "float", "pct", "ratio", "amount"])
def test_fmt_numeric_kind_with_string_uses_text_rule(kind):
    assert fmt("abc", kind) == "abc"
    assert fmt("z" * 250, kind) == "z" * 200
    assert fmt("12,5", kind) == "12,5"       # not parsed, not reformatted


# --- rule 4: unknown kind / None -> the type's default kind -------------------

@pytest.mark.parametrize("kind", [None, "bogus", "", "COUNT", "percent"])
def test_fmt_unknown_kind_uses_type_default(kind):
    assert fmt(42, kind) == "42"             # int -> count
    assert fmt(1234567, kind) == "1,234,567"
    assert fmt(0.5, kind) == "0.50"          # float -> float
    assert fmt(2.5, kind) == "2.50"
    assert fmt("s", kind) == "s"             # str -> text
    assert fmt(np.int64(7), kind) == "7"
    assert fmt(np.float64(0.5), kind) == "0.50"


# --- rule 2: numpy scalars ------------------------------------------------------

def test_fmt_numpy_scalars_item():
    assert fmt(np.int32(1234), "count") == "1,234"
    assert fmt(np.int64(1234567), "amount") == "1.23 mln"
    assert fmt(np.float32(0.5), "ratio") == "0.5x"
    assert fmt(np.float64(1234.5678), "float") == "1,234.57"
    assert fmt(np.float64("nan"), "pct") == "n/a"
    assert fmt(np.int64(3), "text") == "3"


# --- rule 8 rounding: fixed-point, half-to-even on the binary value ----------

def test_fmt_rounding_is_format_based():
    assert fmt(2.5, "count") == "2"
    assert fmt(3.5, "count") == "4"
    assert fmt(0.125, "float") == format(0.125, ",.2f")
    assert fmt(0.12345, "pct") == "12.3%"
    assert fmt(2.675, "float") == format(2.675, ",.2f")
    assert fmt(1234567.4, "count") == "1,234,567"
    assert fmt(1234567.5, "count") == format(1234567.5, ",.0f")


def test_fmt_count_int_is_exact_without_float_conversion():
    big = 123456789012345678
    assert fmt(big, "count") == "123,456,789,012,345,678"
    assert fmt(big, "count") == format(big, ",")
    # a float of the same magnitude has already lost precision: the int path
    # must not go through it
    assert format(float(big), ",.0f") != format(big, ",")
    assert fmt(big + 1, "count") == "123,456,789,012,345,679"


# --- never raises ---------------------------------------------------------------

@pytest.mark.parametrize("value", [object(), [1, 2], {"a": 1}, b"bytes", complex(1, 2),
                                   type, lambda: 0])
@pytest.mark.parametrize("kind", ["count", "text", None])
def test_fmt_never_raises_on_odd_input(value, kind):
    out = fmt(value, kind)
    assert isinstance(out, str)
    assert len(out) <= 200


def test_fmt_result_always_a_str_and_bounded():
    for value, kind, _ in EXAMPLES:
        out = fmt(value, kind)
        assert isinstance(out, str)
        assert len(out) <= 200
        if kind not in ("text", "period") and not isinstance(value, (str, bool)):
            assert len(out) <= pdc_diag.FMT_MAX_LEN or out == "n/a"


# --- BRB retest 2 follow-up: the uz / ru locales (A4, decision F20) -----------
# Thousands separator: a NO-BREAK space (U+00A0); decimal separator: a comma;
# magnitude words in the language. The default locale is unchanged.
NBSP = " "

LOCALE_EXAMPLES = [
    ("uz", 1234567, "count", f"1{NBSP}234{NBSP}567"),
    ("uz", 2345678901, "amount", "2,35 mlrd"),
    ("uz", 7250000, "amount", "7,25 mln"),
    ("uz", 5e12, "amount", "5,00 trln"),
    ("uz", 1234.5678, "float", f"1{NBSP}234,57"),
    ("ru", 1234567, "count", f"1{NBSP}234{NBSP}567"),
    ("ru", 2345678901, "amount", "2,35 млрд"),
    ("ru", 7250000, "amount", "7,25 млн"),
    ("ru", 5e12, "amount", "5,00 трлн"),
    ("ru", 0.375, "pct", "37,5%"),
    ("ru", 3.27, "ratio", "3,3x"),
    ("ru", 6.6e-6, "ratio", "<0,1x"),
]


@pytest.mark.parametrize("locale,value,kind,expected", LOCALE_EXAMPLES,
                         ids=[f"{loc}:{k}:{i}" for i, (loc, _v, k, _e) in enumerate(LOCALE_EXAMPLES)])
def test_fmt_uz_ru_locale_rows(locale, value, kind, expected):
    assert fmt(value, kind, locale=locale) == expected


def test_fmt_uz_ru_locale_entries():
    assert pdc_diag._LOCALES.get("uz") == {"thousands": NBSP, "decimal": ",", "mln": "mln",
                                           "bln": "mlrd", "trln": "trln"}
    assert pdc_diag._LOCALES.get("ru") == {"thousands": NBSP, "decimal": ",", "mln": "млн",
                                           "bln": "млрд", "trln": "трлн"}


def test_fmt_default_locale_unchanged_by_uz_ru():
    d = pdc_diag._LOCALES["default"]
    assert d == {"thousands": ",", "decimal": ".", "mln": "mln", "bln": "bln", "trln": "trln"}
    assert fmt(1234567, "count") == "1,234,567"
    assert fmt(2345678901, "amount") == "2.35 bln"
    assert fmt(0.375, "pct", locale=None) == "37.5%"
    # text, n/a and booleans are never localized
    for loc in ("uz", "ru"):
        assert fmt("2026 Март", "text", locale=loc) == "2026 Март"
        assert fmt(None, "count", locale=loc) == "n/a"
        assert fmt(True, "count", locale=loc) == "true"
