"""P31 Part D — evaluator review fixes (client).

The first version of the identifier rule over-reached in four places; the
fixes pinned here:

1. A VALUE (measure) axis never becomes `category`. Bars whose value axis is
   titled "Revenue per Order", "Sales per account", "Total Number" or
   "clients_number", or that plot a source `loan_amount` of unique integral
   1e6–9e7 values, keep a value axis (grouped unless the name is an
   identifier). An identifier-titled axis the planner gave a `range` stays
   linear with plain-integer ticks ("d"). An identifier on the CATEGORY axis
   (x = personal_number, y = income) is category (or "d") while y and its
   value labels stay grouped.
2. The MEASURE-WORD VETO in `ident_rule`: a name holding a measure word
   (amount, total, revenue, per, count, …, сумма, თანხა) is never an
   identifier — not by name, not by the value rule, not by the year rule —
   so integral currency measures are not identifiers; a price column of
   1950–2050 with fewer than 20 rows is not a year; "no" alone / "Yes/No" is
   not an identifier while "account_no" is. (JS parity for these labels:
   tests/test_ident_display_js.py NAME_CASES.)
3. Narrative: a measure KIND (amount / float / pct / ratio / pvalue) is never
   overridden by the slot name; an identifier-named count / text / no-kind
   slot stays plain digits; a measure-named slot keeps its grouping.
4. Excel (tests/test_ident_display_exports.py): integral identifiers are
   numeric cells with number_format "0"; 16+ digits are text.
5. matplotlib: bar labels keep their grouping when only the CATEGORY axis is
   an identifier.

Offline; pure in-process calls.
"""
import json

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import plotly.express as px  # noqa: E402
import plotly.graph_objects as go  # noqa: E402
import pytest  # noqa: E402

import ident_rule  # noqa: E402
import narrative  # noqa: E402
import pdc_diag  # noqa: E402
import plot_utils  # noqa: E402

PN = [52906123456 + i * 1013 for i in range(30)]
REGIONS = ["North", "South", "East", "West", "Centre"]


def _loans(n=30) -> pd.DataFrame:
    """UZS-style loan amounts: unique, integral, 1e6–9e7 — identifier-LIKE
    values under a MEASURE name."""
    return pd.DataFrame({
        "region": [REGIONS[i % len(REGIONS)] for i in range(n)],
        "loan_amount": [1_000_000 + i * 2_999_999 for i in range(n)],
        "personal_number": PN[:n],
        "income": [[1234567, 2345678, 345678, 45678, 5678][i % 5] for i in range(n)],
    })


def _plotly_layout(html: str) -> dict:
    start = html.index("Plotly.newPlot(") + len("Plotly.newPlot(")
    dec = json.JSONDecoder()
    pos = start
    parts = []
    for _ in range(3):
        while html[pos] in " \t\r\n,":
            pos += 1
        obj, pos = dec.raw_decode(html, pos)
        parts.append(obj)
    return parts[2]


# ---------------------------------------------------------------------------
# 1. a value axis never becomes category
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("measure", ["Revenue per Order", "Sales per account",
                                     "Total Number", "clients_number"])
def test_a_value_axis_never_becomes_category(measure):
    df = pd.DataFrame({"region": REGIONS, measure: [1234567, 2345678, 345678, 45678, 5678]})
    fig = px.bar(df, x="region", y=measure)
    plot_utils._format_plotly_numeric(fig)
    assert fig.layout.yaxis.type != "category", measure


# ("Revenue per Order" / "Sales per account" end in an ORIGINAL identifier
# word, so they are identifiers again — see the re-review tests below.)
@pytest.mark.parametrize("measure", ["Total Number", "ID Count", "Revenue per Client"])
def test_a_measure_named_value_axis_is_still_grouped(measure):
    df = pd.DataFrame({"region": REGIONS, measure: [1234567, 2345678, 345678, 45678, 5678]})
    fig = px.bar(df, x="region", y=measure)
    plot_utils._format_plotly_numeric(fig)
    assert fig.layout.yaxis.tickformat == ",", measure


def test_aggregated_source_loan_amount_bar_keeps_a_grouped_value_axis():
    code = ("fig = px.bar(dfs['loans.csv'].groupby('region', as_index=False)"
            "['loan_amount'].sum(), x='region', y='loan_amount')")
    out = plot_utils._render_in_process(code, {"loans.csv": _loans()}, "t")
    assert out.get("ok") and out.get("is_plotly"), out.get("error")
    yaxis = _plotly_layout(out["plotly_html"]).get("yaxis") or {}
    assert yaxis.get("type") != "category"
    assert yaxis.get("tickformat") == ","


def test_a_planner_range_on_an_identifier_axis_keeps_it_linear():
    # small whole numbers: plotly prints them in full, so the axis is left
    # as it was (no "d", no ",") — re-review item 1
    fig = go.Figure(go.Bar(x=["a", "b", "c"], y=[100, 250, 300]))
    fig.update_layout(yaxis_title="client_id")
    fig.update_yaxes(range=[0, 400])
    plot_utils._format_plotly_numeric(fig)
    assert fig.layout.yaxis.type != "category"
    assert fig.layout.yaxis.tickformat is None
    assert tuple(fig.layout.yaxis.range) == (0, 400)


def test_a_planner_range_on_a_large_identifier_axis_prints_full_digits():
    fig = go.Figure(go.Bar(x=["a", "b", "c"], y=PN[:3]))
    fig.update_layout(yaxis_title="client_id")
    fig.update_yaxes(range=[PN[0] - 10, PN[2] + 10])
    plot_utils._format_plotly_numeric(fig)
    assert fig.layout.yaxis.type != "category"
    assert fig.layout.yaxis.tickformat == "d"


def test_identifier_on_the_category_axis_keeps_the_value_axis_grouped():
    df = _loans(5)
    fig = px.bar(df, x="personal_number", y="income")
    plot_utils._format_plotly_numeric(fig)
    x = fig.layout.xaxis
    assert x.tickformat != ","
    assert x.type == "category" or x.tickformat == "d"
    assert fig.layout.yaxis.type != "category"
    assert fig.layout.yaxis.tickformat == ","


def test_value_labels_stay_grouped_when_only_the_category_axis_is_an_identifier():
    df = _loans(5)
    fig = px.bar(df, x="personal_number", y="income", text_auto=True)
    plot_utils._format_plotly_numeric(fig)
    assert any("," in (tr.texttemplate or "") for tr in fig.data), \
        [tr.texttemplate for tr in fig.data]


# ---------------------------------------------------------------------------
# 2. the measure-word veto
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label", [
    "Total Number", "сумма_счет", "Сумма счет", "თანხა_ნომერი",
    "salary_no", "price_key", "amount_card", "ID Count",
])
def test_a_measure_word_vetoes_the_name_rule(label):
    assert ident_rule.is_identifier_name(label) is False


@pytest.mark.parametrize("label", ["no", "No", "Yes/No", "yes_no"])
def test_no_alone_is_not_an_identifier(label):
    assert ident_rule.is_identifier_name(label) is False


@pytest.mark.parametrize("label", ["account_no", "card_no", "personal_number",
                                   "client_id", "Year", "IBAN"])
def test_identifiers_without_a_measure_word_stay_identifiers(label):
    assert ident_rule.is_identifier_name(label) is True


# Coordinator decision: entity nouns (loan, payment, deposit, principal,
# limit) are not measure words — their ids stay identifiers, while the same
# nouns with a real measure word stay vetoed by that word.
@pytest.mark.parametrize("label", ["loan_id", "payment_id", "deposit_no",
                                   "card_limit_id", "principal_code", "Loan ID"])
def test_entity_noun_identifiers_stay_identifiers(label):
    assert ident_rule.is_identifier_name(label) is True


@pytest.mark.parametrize("label", ["loan_amount", "payment_total", "deposit_sum"])
def test_entity_nouns_with_a_measure_word_stay_vetoed(label):
    assert ident_rule.is_identifier_name(label) is False


# ---------------------------------------------------------------------------
# re-review: the veto never applies when the LAST word is an ORIGINAL word
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("label", [
    "sales_year", "Sales Order", "income_account", "revenue_account", "price_code",
    "rate_code", "Revenue per Order", "Sales per account", "total_id", "amount_code",
    "count_id", "loan_amount_id", "payment_count_id", "Sales Order #",
])
def test_an_original_last_word_overrides_the_measure_veto(label):
    assert ident_rule.is_identifier_name(label) is True


def test_sales_year_is_a_source_identifier_and_keeps_the_year_rule():
    df = pd.DataFrame({"sales_year": [2015 + (i % 8) for i in range(40)]})
    assert ident_rule.source_identifier_columns({"d": df}) == frozenset({"sales_year"})


@pytest.mark.parametrize("name", ["sales_number", "Total Number", "price_key"])
def test_the_veto_still_blocks_the_value_rule_for_new_words(name):
    df = pd.DataFrame({name: [20_000_000 + i * 7_919 for i in range(30)]})
    assert ident_rule.source_identifier_columns({"d": df}) == frozenset()


# ---------------------------------------------------------------------------
# re-review: a year axis is never touched; "d" only for big whole numbers
# ---------------------------------------------------------------------------
def test_a_year_axis_is_left_exactly_as_before():
    fig = px.line(pd.DataFrame({"Year": [2019, 2020, 2021], "loans": [5, 7, 9]}),
                  x="Year", y="loans")
    plot_utils._format_plotly_numeric(fig)
    assert fig.layout.xaxis.tickformat is None
    assert fig.layout.xaxis.type is None


def test_a_fractional_identifier_titled_axis_gets_no_d():
    fig = go.Figure(go.Bar(x=["a", "b", "c"], y=[0.2, 0.45, 0.7]))
    fig.update_layout(yaxis_title="Share by account")
    plot_utils._format_plotly_numeric(fig)
    assert fig.layout.yaxis.tickformat != "d"
    assert fig.layout.yaxis.tickformat != ","
    assert fig.layout.yaxis.type != "category"


def test_eight_digit_integral_revenues_are_not_source_identifiers():
    df = pd.DataFrame({"revenue": [10_000_000 + i * 123_457 for i in range(30)],
                       "region": [REGIONS[i % 5] for i in range(30)]})
    assert ident_rule.source_identifier_columns({"d": df}) == frozenset()


def test_unique_uzs_loan_amounts_are_not_source_identifiers():
    out = ident_rule.source_identifier_columns({"loans.csv": _loans()})
    assert "loan_amount" not in out
    assert "income" not in out
    assert "personal_number" in out


@pytest.mark.parametrize("name", ["Сумма", "თანხა", "total_revenue", "amount", "revenue_per_client"])
def test_measure_named_identifier_like_values_are_not_source_identifiers(name):
    df = pd.DataFrame({name: [20_000_000 + i * 7_919 for i in range(30)]})
    assert ident_rule.source_identifier_columns({"d": df}) == frozenset()


def test_measure_named_year_like_values_are_not_years():
    df = pd.DataFrame({"total": [1995 + (i % 30) for i in range(40)]})
    assert ident_rule.source_identifier_columns({"d": df}) == frozenset()


def test_a_short_price_column_in_the_year_range_is_not_a_year():
    df = pd.DataFrame({"price": [1950 + i * 7 for i in range(15)]})
    assert max(df["price"]) <= 2050
    assert ident_rule.source_identifier_columns({"d": df}) == frozenset()


def test_client_since_is_still_a_source_year():
    df = pd.DataFrame({"client_since": [1995 + (i % 30) for i in range(40)]})
    assert ident_rule.source_identifier_columns({"d": df}) == frozenset({"client_since"})


# ---------------------------------------------------------------------------
# 3. narrative: a measure kind is never overridden
# ---------------------------------------------------------------------------
def test_measure_named_amount_slot_keeps_its_magnitude_words():
    assert narrative._fmt_value(1500000, "amount", name="revenue_per_order") == "1.50 mln"


@pytest.mark.parametrize("kind", ["count", "text", None])
def test_identifier_named_count_text_and_no_kind_stay_plain(kind):
    assert narrative._fmt_value(PN[0], kind, name="personal_number") == "52906123456"


@pytest.mark.parametrize("kind,value", [
    ("amount", PN[0]), ("float", PN[0]), ("pct", 0.25), ("ratio", 3.27), ("pvalue", 0.042),
])
def test_identifier_named_slot_with_a_measure_kind_is_not_overridden(kind, value):
    assert narrative._fmt_value(value, kind, name="personal_number") == pdc_diag.fmt(value, kind)


@pytest.mark.parametrize("name", ["revenue_per_client", "Total Number", "order_count"])
def test_measure_named_count_slot_keeps_grouping(name):
    assert narrative._fmt_value(1500000, "count", name=name) == "1,500,000"


def test_render_with_a_measure_named_amount_slot():
    out = narrative.render("Revenue per order: {{revenue_per_order:amount}}",
                           {"revenue_per_order": 1500000}, {}, sid="t")
    assert out == "Revenue per order: 1.50 mln"


# ---------------------------------------------------------------------------
# 5. matplotlib: value labels with an identifier CATEGORY axis
# ---------------------------------------------------------------------------
def test_matplotlib_bar_labels_keep_grouping_when_only_the_category_axis_is_an_identifier():
    fig, ax = plt.subplots()
    try:
        bars = ax.bar([str(v) for v in PN[:3]], [1234567, 2345678, 3456789])
        ax.set_xlabel("personal_number")
        ax.set_ylabel("income")
        ax.bar_label(bars, fmt="%d")       # "%g" would print 1.23457e+06
        plot_utils._format_matplotlib_numeric(fig)
        texts = [t.get_text() for t in ax.texts]
        assert texts == ["1,234,567", "2,345,678", "3,456,789"], texts
    finally:
        plt.close(fig)
