"""P31 Part D §4 — identifiers in the narrative (`narrative.py` + `pdc_diag.fmt`).

A slot whose NAME passes `ident_rule.is_identifier_name`, or whose kind is
`text`, and that holds an INTEGRAL number renders `str(int(v))` — never
grouped, never decimals. `pdc_diag.fmt` rule 5 changes accordingly for
`text` (an integral number under `text` is plain digits); `period` keeps its
behaviour. `value_display` of a flat dict renders identifier-named keys'
integral values plainly. `count` / `amount` / `float` slots with
non-identifier names are unchanged.

Reading where the spec leaves room: a NON-integral number under `text`
keeps today's rule (the float default, "1,234.57") — the spec changes the
integral case only.
"""
import numpy as np
import pytest

import narrative
import pdc_diag

SID = "t"
PN = 52906123456


def _r(template, values, **kw):
    return narrative.render(template, values, {}, sid=SID, **kw)


# ---------------------------------------------------------------------------
# pdc_diag.fmt rule 5 (text) / period unchanged
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value,expected", [
    (PN, "52906123456"),
    (np.int64(PN), "52906123456"),
    (float(PN), "52906123456"),
    (np.float64(PN), "52906123456"),
    (1234567, "1234567"),
    (-1234, "-1234"),
    (7, "7"),
    (np.int64(3), "3"),            # the existing pin
    (0, "0"),
])
def test_fmt_text_kind_integral_number_is_plain_digits(value, expected):
    assert pdc_diag.fmt(value, "text") == expected


def test_fmt_text_kind_non_integral_number_keeps_the_float_default():
    assert pdc_diag.fmt(1234.5678, "text") == "1,234.57"


def test_fmt_text_kind_string_and_bool_unchanged():
    assert pdc_diag.fmt("North", "text") == "North"
    assert pdc_diag.fmt(False, "text") == "false"
    assert pdc_diag.fmt(None, "text") == "n/a"


def test_fmt_period_kind_keeps_its_behaviour():
    assert pdc_diag.fmt(1234567, "period") == "1,234,567"
    assert pdc_diag.fmt(0.25, "period") == "0.25"
    assert pdc_diag.fmt("Q3", "period") == "Q3"


def test_fmt_numeric_kinds_unchanged():
    assert pdc_diag.fmt(50000, "count") == "50,000"
    assert pdc_diag.fmt(7250000, "amount") == "7.25 mln"
    assert pdc_diag.fmt(1234.5678, "float") == "1,234.57"


def test_fmt_text_never_scientific_for_a_long_integral_float():
    out = pdc_diag.fmt(1234567890123.0, "text")
    assert out == "1234567890123"
    assert "e" not in out.lower()


# ---------------------------------------------------------------------------
# narrative slots
# ---------------------------------------------------------------------------
def test_text_slot_with_a_personal_number_renders_plain():
    assert _r("Top client: {{personal_number:text}}", {"personal_number": PN}) \
        == "Top client: 52906123456"


@pytest.mark.parametrize("kind", ["count", "text", None])
def test_identifier_named_slot_renders_plain_for_count_text_and_no_kind(kind):
    slot = "{{client_pin}}" if kind is None else "{{client_pin:%s}}" % kind
    assert _r(f"PIN {slot}", {"client_pin": PN}) == "PIN 52906123456"


# Evaluator review (deliberate update): an explicit MEASURE kind is the
# planner's statement that the slot is a measure — amount / float / pct /
# ratio / pvalue are never overridden by the identifier name.
@pytest.mark.parametrize("kind,value", [
    ("amount", PN), ("float", PN), ("pct", 0.25), ("ratio", 3.27), ("pvalue", 0.042),
])
def test_identifier_named_slot_with_a_measure_kind_is_not_overridden(kind, value):
    assert _r("{{client_pin:%s}}" % kind, {"client_pin": value}) == pdc_diag.fmt(value, kind)


@pytest.mark.parametrize("name", ["personal_number", "personalNumber", "client_id",
                                  "card_no", "account"])
def test_identifier_named_count_slot_renders_plain(name):
    assert _r("{{%s:count}}" % name, {name: np.int64(PN)}) == "52906123456"


def test_identifier_named_slot_with_an_integral_float_has_no_decimals():
    assert _r("{{personal_number:count}}", {"personal_number": float(PN)}) == "52906123456"
    assert _r("{{personal_number}}", {"personal_number": float(PN)}) == "52906123456"


def test_non_identifier_count_slot_still_groups():
    assert _r("{{total_clients:count}}", {"total_clients": 50000}) == "50,000"
    assert _r("{{number_of_orders:count}}", {"number_of_orders": 1234567}) == "1,234,567"
    assert _r("{{loan_amount:amount}}", {"loan_amount": 7250000}) == "7.25 mln"


def test_mixed_template_formats_each_slot_by_its_own_rule():
    out = _r("Client {{personal_number:text}} holds {{loan_amount:count}} GEL "
             "of {{total_clients:count}} clients.",
             {"personal_number": PN, "loan_amount": 1234567, "total_clients": 50000})
    assert out == "Client 52906123456 holds 1,234,567 GEL of 50,000 clients."


# ---------------------------------------------------------------------------
# value_display
# ---------------------------------------------------------------------------
def test_value_display_flat_dict_renders_identifier_keys_plainly():
    out = narrative.value_display({"personal_number": PN, "total": 50000}, "en")
    assert out is not None
    lines = out.splitlines()
    assert any(l.endswith(": 52906123456") for l in lines), lines
    assert any(l.endswith(": 50,000") for l in lines), lines
    assert "52,906,123,456" not in out


def test_value_display_integral_float_identifier_has_no_decimals():
    out = narrative.value_display({"client_id": float(PN)}, "en")
    assert out is not None and out.endswith(": 52906123456"), out


def test_value_display_scalar_unchanged():
    assert narrative.value_display(1234, "en") == "**Result:** 1,234"
