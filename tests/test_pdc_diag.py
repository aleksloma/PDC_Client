"""`pdc_diag` — the sandbox diagnostics library (docs/DIAGNOSTICS_SPEC.md §B8,
decision D12): period ordering, `coverage_check`, `detect_anomalies`,
`decompose_change`, `attribute_level`, `check_metric_overlap`, the three
plot helpers, the `prefix` rule, the findings conventions and the leaf-import
pin.

Offline, on SMALL synthetic frames with invented numbers. Every analysis
function returns exactly `{"table", "values", "findings"}`; FINDINGS keys come
from `PDC_DIAG_FINDING_KEYS` and FINDINGS values are a bool, a
`FINDINGS_ENUM` member or one of the caller's column names — never a number,
never a category value (those go to VALUES).
"""
import ast
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import pdc_diag

ROOT = Path(__file__).resolve().parent.parent
RETURN_KEYS = {"table", "values", "findings"}


# ---------------------------------------------------------------------------
# fixtures (invented numbers)
# ---------------------------------------------------------------------------
def _rows(period, n, members, amount=10.0, start=0):
    """`n` rows of one period, cycling through `members`."""
    return pd.DataFrame({
        "period": [period] * n,
        "region": [members[(start + i) % len(members)] for i in range(n)],
        "amount": [amount] * n,
    })


def _stable_frame() -> pd.DataFrame:
    """Three ISO months, 4 rows each, the same 4 members — nothing to flag."""
    return pd.concat([
        _rows("2025-01", 4, ["A", "B", "C", "D"]),
        _rows("2025-02", 4, ["A", "B", "C", "D"]),
        _rows("2025-03", 4, ["A", "B", "C", "D"]),
    ], ignore_index=True)


def _member_change_frame() -> pd.DataFrame:
    """The last period keeps one member of four."""
    return pd.concat([
        _rows("2025-01", 4, ["A", "B", "C", "D"]),
        _rows("2025-02", 4, ["A", "B", "C", "D"]),
        _rows("2025-03", 1, ["A"]),
    ], ignore_index=True)


def _row_jump_frame() -> pd.DataFrame:
    """A 10x row-count jump after the first period, with 5x the members."""
    many = [f"M{i}" for i in range(10)]
    return pd.concat([
        _rows("2025-01", 2, ["M0", "M1"]),
        _rows("2025-02", 20, many),
        _rows("2025-03", 20, many),
    ], ignore_index=True)


def _series_frame(values, periods=None) -> pd.DataFrame:
    periods = periods or [f"2025-{m:02d}" for m in range(1, len(values) + 1)]
    return pd.DataFrame({"period": periods, "amount": list(values),
                         "region": ["A"] * len(values)})


def _two_period_dim_frame(p1: dict, p2: dict) -> pd.DataFrame:
    rows = []
    for member, v in p1.items():
        rows.append({"period": "2025-01", "region": member, "amount": v})
    for member, v in p2.items():
        rows.append({"period": "2025-02", "region": member, "amount": v})
    return pd.DataFrame(rows)


def _attribution_frame(signal: bool, n: int = 300) -> pd.DataFrame:
    rng = np.random.default_rng(12)
    segments = rng.choice(["S1", "S2", "S3"], size=n)
    noise = rng.normal(0.0, 1.0, size=n)
    if signal:
        level = {"S1": 10.0, "S2": 50.0, "S3": 100.0}
        target = np.array([level[s] for s in segments]) + rng.normal(0.0, 1.0, size=n)
    else:
        target = rng.normal(0.0, 1.0, size=n)
    return pd.DataFrame({"segment": segments, "noise": noise, "target": target})


def _overlap_frame(n_equal: int, n_total: int) -> pd.DataFrame:
    a = np.arange(1, n_total + 1, dtype=float) * 100.0
    b = a.copy()
    b[n_equal:] = b[n_equal:] * 3.0 + 7.0
    return pd.DataFrame({"a": a, "b": b})


def _currency_switch_frame() -> pd.DataFrame:
    """Four ISO months, 10 rows each, REGION cycling A/B; CURRENCY half UZS /
    half USD in the first two months, UZS only in the last two — a two-value
    switch neither the size-swing nor the first/last Jaccard rule sees."""
    parts = []
    for i, period in enumerate(["2025-01", "2025-02", "2025-03", "2025-04"]):
        currency = ["UZS", "USD"] * 5 if i < 2 else ["UZS"] * 10
        parts.append(pd.DataFrame({"period": [period] * 10, "REGION": ["A", "B"] * 5,
                                   "CURRENCY": currency, "amount": [10.0] * 10}))
    return pd.concat(parts, ignore_index=True)


def _counts_frame(per_period: list, dim: str = "region") -> pd.DataFrame:
    """One ISO month per entry of `per_period` ({member: row count})."""
    rows = []
    for i, counts in enumerate(per_period):
        for member, k in counts.items():
            rows.extend({"period": f"2025-{i + 1:02d}", dim: member, "amount": 10.0}
                        for _ in range(k))
    return pd.DataFrame(rows)


def _assert_shape(out):
    assert isinstance(out, dict), type(out)
    assert set(out) == RETURN_KEYS, sorted(out)
    assert isinstance(out["table"], pd.DataFrame)
    assert isinstance(out["values"], dict)
    assert isinstance(out["findings"], dict)


def _all_outputs():
    """`(result, input frame)` pairs, one or two per analysis function, on
    frames that trigger findings."""
    concentrated = _two_period_dim_frame({"A": 100, "B": 100, "C": 100, "D": 100},
                                         {"A": 300, "B": 100, "C": 100, "D": 100})
    even = _two_period_dim_frame({"A": 100, "B": 100, "C": 100, "D": 100},
                                 {"A": 200, "B": 200, "C": 200, "D": 200})
    spike = _series_frame([10, 11, 10, 12, 100, 11])
    short = _series_frame([10, 11])
    strong, weak = _attribution_frame(True), _attribution_frame(False, n=1000)
    mostly, same = _overlap_frame(19, 20), _overlap_frame(20, 20)
    units = _stable_frame()
    units["currency"] = ["USD", "UZS"] * 6
    switch = _currency_switch_frame()
    return [
        (pdc_diag.coverage_check(switch, "period", ["REGION"], ["amount"],
                                 unit_cols=["CURRENCY"], as_of="2025-04-15"), switch),
        (pdc_diag.coverage_check(_member_change_frame(), "period", ["region"], ["amount"]),
         _member_change_frame()),
        (pdc_diag.coverage_check(_row_jump_frame(), "period", ["region"], ["amount"],
                                 as_of="2025-03-10"), _row_jump_frame()),
        (pdc_diag.coverage_check(units, "period", ["region"], ["amount"], unit_cols=["currency"],
                                 as_of="2025-06-01"), units),
        (pdc_diag.detect_anomalies(spike, "period", "amount"), spike),
        (pdc_diag.detect_anomalies(short, "period", "amount"), short),
        (pdc_diag.decompose_change(concentrated, "period", "amount", dims=["region"]), concentrated),
        (pdc_diag.decompose_change(even, "period", "amount", dims=["region"]), even),
        (pdc_diag.decompose_change(spike, "period", "amount", to_period="primary_anomaly",
                                   dims=["region"]), spike),
        (pdc_diag.attribute_level(strong, "target", ["segment"], ["noise"]), strong),
        (pdc_diag.attribute_level(weak, "target", ["segment"], ["noise"]), weak),
        (pdc_diag.check_metric_overlap(mostly, "a", "b"), mostly),
        (pdc_diag.check_metric_overlap(same, "a", "b"), same),
    ]


# ---------------------------------------------------------------------------
# B8.2 period ordering
# ---------------------------------------------------------------------------
def test_order_periods_datetime():
    s = pd.Series(pd.to_datetime(["2025-03-01", "2025-01-01", "2025-02-01", "2025-01-01"]))
    ordered, known = pdc_diag._order_periods(s)
    assert known is True
    assert list(ordered) == list(pd.to_datetime(["2025-01-01", "2025-02-01", "2025-03-01"]))


def test_order_periods_numeric_sorted_known():
    ordered, known = pdc_diag._order_periods(pd.Series([2024, 2022, 2023, 2022]))
    assert known is True
    assert list(ordered) == [2022, 2023, 2024]


def test_order_periods_month_names_en_ru_uz_ka():
    labels = [
        "2025 Mart",       # uz Latin, March
        "Июня 2025",       # ru genitive, June
        "Feb 2025",        # en 3-letter, February
        "2025 მაისი",      # ka, May
        "Январь 2025",     # ru nominative, January
        "Апрел 2025",      # uz Cyrillic, April
    ]
    ordered, known = pdc_diag._order_periods(pd.Series(labels))
    assert known is True
    assert ordered == ["Январь 2025", "Feb 2025", "2025 Mart", "Апрел 2025",
                       "2025 მაისი", "Июня 2025"]


def test_order_periods_month_names_year_first_and_last_and_case():
    ordered, known = pdc_diag._order_periods(pd.Series(["Март 2025", "2025 იანვარი", "FEBRUARY 2025"]))
    assert known is True
    assert ordered == ["2025 იანვარი", "FEBRUARY 2025", "Март 2025"]
    # a year change beats the month
    ordered, known = pdc_diag._order_periods(pd.Series(["Yanvar 2025", "Dekabr 2024"]))
    assert known is True
    assert ordered == ["Dekabr 2024", "Yanvar 2025"]


@pytest.mark.parametrize("labels,expected", [
    (["2025-03", "2025-01", "2025-02"], ["2025-01", "2025-02", "2025-03"]),
    (["2025-03-15", "2025-01-02"], ["2025-01-02", "2025-03-15"]),
    (["2025/03", "2025/01"], ["2025/01", "2025/03"]),
    (["03.2025", "01.2025", "12.2024"], ["12.2024", "01.2025", "03.2025"]),
    (["03/2025", "01/2025"], ["01/2025", "03/2025"]),
    (["2025 Q3", "Q1 2025", "2024 Q4"], ["2024 Q4", "Q1 2025", "2025 Q3"]),
])
def test_order_periods_iso_and_quarter_labels(labels, expected):
    ordered, known = pdc_diag._order_periods(pd.Series(labels))
    assert known is True
    assert ordered == expected


def test_order_periods_explicit_order():
    s = pd.Series(["Q1", "Q3", "Q2", "Q1"])
    ordered, known = pdc_diag._order_periods(s, order=["Q3", "Q1", "Q2", "Q4"])
    assert ordered == ["Q3", "Q1", "Q2"]        # Q4 does not occur in the data
    assert known is True
    # an occurring label the order does not name: appended, order NOT known
    s2 = pd.Series(["Q1", "Q9", "Q3", "Q2", "Q8"])
    ordered2, known2 = pdc_diag._order_periods(s2, order=["Q3", "Q1", "Q2"])
    assert ordered2 == ["Q3", "Q1", "Q2", "Q9", "Q8"]
    assert known2 is False


def test_order_periods_unknown_sets_flag():
    ordered, known = pdc_diag._order_periods(pd.Series(["beta", "alpha", "beta", "gamma"]))
    assert known is False
    assert ordered == ["beta", "alpha", "gamma"]     # first-appearance order
    df = pd.DataFrame({"period": ["beta", "alpha", "gamma"] * 4,
                       "region": ["A", "B", "C"] * 4, "amount": [1.0] * 12})
    out = pdc_diag.coverage_check(df, "period", ["region"], ["amount"])
    assert out["findings"].get("period_order_unknown") is True
    assert "latest_is_current_period" not in out["findings"]
    assert "latest_is_past_period" not in out["findings"]
    stable = pdc_diag.coverage_check(_stable_frame(), "period", ["region"], ["amount"])
    assert "period_order_unknown" not in stable["findings"]


# ---------------------------------------------------------------------------
# B8.3 coverage_check
# ---------------------------------------------------------------------------
def test_coverage_check_shape_and_table():
    out = pdc_diag.coverage_check(_stable_frame(), "period", ["region"], ["amount"])
    _assert_shape(out)
    t = out["table"]
    assert list(t["period"]) == ["2025-01", "2025-02", "2025-03"]
    assert list(t["rows"]) == [4, 4, 4]
    assert list(t["region_members"]) == [4, 4, 4]
    assert list(t["amount_null_share"]) == [0.0, 0.0, 0.0]
    assert out["findings"].get("coverage") == "coverage_stable"
    assert "coverage_dim" not in out["findings"]
    assert "thin_period" not in out["findings"]
    assert "period_status" not in out["findings"]
    v = out["values"]
    assert v["n_periods"] == 3
    assert v["first_period"] == "2025-01" and v["latest_period"] == "2025-03"
    assert v["latest_rows"] == 4 and v["median_rows"] == 4
    assert v["coverage_jaccard"] == 1.0


def test_coverage_check_member_set_change():
    out = pdc_diag.coverage_check(_member_change_frame(), "period", ["region"], ["amount"])
    f, v = out["findings"], out["values"]
    assert f["coverage"] == "coverage_change"
    assert f["coverage_dim"] == "region"
    assert v["coverage_dim_label"] == "region"
    assert v["coverage_jaccard"] == pytest.approx(0.25)
    assert v["latest_rows"] == 1


def test_coverage_check_row_count_jump():
    """A 10x row-count jump between periods: the thin period is flagged, and
    since the jump brings 5x the members the coverage changed too."""
    out = pdc_diag.coverage_check(_row_jump_frame(), "period", ["region"], ["amount"])
    f, v = out["findings"], out["values"]
    assert f.get("thin_period") is True
    assert v["thin_period_label"] == "2025-01"
    assert v["median_rows"] == 20
    assert f["coverage"] == "coverage_change"
    assert f["coverage_dim"] == "region"
    # the LAST period is a full one: not partial
    assert "period_status" not in f


def test_coverage_check_partial_latest_period():
    df = pd.concat([
        _rows("2025-01", 10, ["A", "B"]),
        _rows("2025-02", 10, ["A", "B"]),
        _rows("2025-03", 10, ["A", "B"]),
        _rows("2025-04", 2, ["A", "B"]),
    ], ignore_index=True)
    out = pdc_diag.coverage_check(df, "period", ["region"], ["amount"])
    f = out["findings"]
    assert f.get("period_status") == "partial_period"
    assert f.get("thin_period") is True
    assert out["values"]["latest_rows"] == 2
    assert out["values"]["latest_period"] == "2025-04"
    # members unchanged: the coverage itself is stable
    assert f["coverage"] == "coverage_stable"


def test_coverage_check_unknown_dominated():
    p1 = pd.DataFrame({"period": ["2025-01"] * 10, "channel": ["web", "app"] * 5,
                       "amount": [1.0] * 10})
    p2 = pd.DataFrame({"period": ["2025-02"] * 10,
                       "channel": [None, "Unknown", "n/a", "noma'lum", "უცნობი", " - ",
                                   "web", "app", "web", "app"],
                       "amount": [1.0] * 10})
    out = pdc_diag.coverage_check(pd.concat([p1, p2], ignore_index=True),
                                  "period", ["channel"], ["amount"])
    f, v = out["findings"], out["values"]
    assert f.get("unknown_dominated") is True
    assert f.get("unknown_dim") == "channel"
    assert v["unknown_dim_label"] == "channel"
    assert v["unknown_share_max"] == pytest.approx(0.6)
    clean = pdc_diag.coverage_check(_stable_frame(), "period", ["region"], ["amount"])
    assert "unknown_dominated" not in clean["findings"]
    assert "unknown_dim" not in clean["findings"]


def test_coverage_check_mixed_units():
    df = _stable_frame()
    df["currency"] = ["USD", "UZS"] * 6
    out = pdc_diag.coverage_check(df, "period", ["region"], ["amount"], unit_cols=["currency"])
    assert out["findings"].get("units") == "mixed_units"
    df["currency"] = "USD"
    out = pdc_diag.coverage_check(df, "period", ["region"], ["amount"], unit_cols=["currency"])
    assert "units" not in out["findings"]


def test_coverage_check_metric_gap():
    df = _stable_frame()
    df.loc[df["period"] == "2025-01", "amount"] = np.nan
    out = pdc_diag.coverage_check(df, "period", ["region"], ["amount"])
    assert out["findings"].get("metric_gap") is True
    assert list(out["table"]["amount_null_share"]) == [1.0, 0.0, 0.0]
    clean = pdc_diag.coverage_check(_stable_frame(), "period", ["region"], ["amount"])
    assert "metric_gap" not in clean["findings"]


def test_coverage_check_latest_is_current_period_as_of():
    df = _stable_frame()       # 2025-01 .. 2025-03
    now = pdc_diag.coverage_check(df, "period", ["region"], ["amount"], as_of="2025-03-15")
    assert now["findings"]["latest_is_current_period"] is True
    assert now["findings"].get("latest_is_past_period") is not True
    assert now["values"]["as_of_period"] == "2025-03"
    past = pdc_diag.coverage_check(df, "period", ["region"], ["amount"], as_of="2025-05-01")
    assert past["findings"]["latest_is_current_period"] is False
    assert past["findings"]["latest_is_past_period"] is True
    assert past["values"]["as_of_period"] == "2025-05"
    ts = pdc_diag.coverage_check(df, "period", ["region"], ["amount"],
                                 as_of=pd.Timestamp("2025-03-31"))
    assert ts["findings"]["latest_is_current_period"] is True


def test_coverage_check_explicit_order_argument():
    df = pd.concat([
        _rows("late", 4, ["A", "B", "C", "D"]),
        _rows("early", 4, ["A", "B", "C", "D"]),
    ], ignore_index=True)
    out = pdc_diag.coverage_check(df, "period", ["region"], ["amount"], order=["early", "late"])
    assert list(out["table"]["period"]) == ["early", "late"]
    assert out["values"]["first_period"] == "early"
    assert out["values"]["latest_period"] == "late"
    assert "period_order_unknown" not in out["findings"]


def test_coverage_check_degenerate_inputs_do_not_raise():
    empty = pd.DataFrame({"period": [], "region": [], "amount": []})
    out = pdc_diag.coverage_check(empty, "period", ["region"], ["amount"])
    _assert_shape(out)
    assert len(out["table"]) == 0
    one = pdc_diag.coverage_check(_rows("2025-01", 3, ["A", "B"]), "period", ["region"], ["amount"])
    _assert_shape(one)
    assert one["values"]["n_periods"] == 1


# Follow-up (narrative s1): a member-share rule between CONSECUTIVE periods —
# a member whose share of rows moves by more than `share_shift`, or a member
# holding more than `vanish_share` of the rows that disappears, is a coverage
# change even when the member-set size and the first/last Jaccard look fine.

@pytest.mark.parametrize("kwargs", [
    {"dims": ["REGION"], "unit_cols": ["CURRENCY"]},
    {"dims": ["REGION", "CURRENCY"], "unit_cols": None},
])
def test_coverage_check_two_value_switch_is_a_change(kwargs):
    df = _currency_switch_frame()
    out = pdc_diag.coverage_check(df, "period", kwargs["dims"], ["amount"],
                                  unit_cols=kwargs["unit_cols"])
    f, v = out["findings"], out["values"]
    assert f["coverage"] == "coverage_change"
    assert f["coverage_dim"] == "CURRENCY"
    assert v["coverage_dim_label"] == "CURRENCY"
    # CURRENCY's first/last Jaccard ({UZS, USD} vs {UZS}); REGION is unchanged
    assert v["coverage_jaccard"] == pytest.approx(0.5)


def test_coverage_check_share_shift_rule():
    # same members {A, B} every period; A's share 0.9 -> 0.2 -> 0.2 (shift 0.7)
    moved = _counts_frame([{"A": 9, "B": 1}, {"A": 2, "B": 8}, {"A": 2, "B": 8}])
    out = pdc_diag.coverage_check(moved, "period", ["region"], ["amount"])
    assert out["findings"]["coverage"] == "coverage_change"
    assert out["findings"]["coverage_dim"] == "region"
    assert out["values"]["coverage_jaccard"] == pytest.approx(1.0)
    # A's share 0.7 -> 0.3 -> 0.3 (shift 0.4, not above 0.5): stable
    mild = _counts_frame([{"A": 7, "B": 3}, {"A": 3, "B": 7}, {"A": 3, "B": 7}])
    out = pdc_diag.coverage_check(mild, "period", ["region"], ["amount"])
    assert out["findings"]["coverage"] == "coverage_stable"
    assert "coverage_dim" not in out["findings"]


def test_coverage_check_vanished_minor_member_is_stable():
    # C held 10% of the rows, then vanished: not above vanish_share (0.2)
    minor = _counts_frame([{"A": 4, "B": 5, "C": 1}, {"A": 5, "B": 5}, {"A": 5, "B": 5}])
    out = pdc_diag.coverage_check(minor, "period", ["region"], ["amount"])
    assert out["findings"]["coverage"] == "coverage_stable"
    assert "coverage_dim" not in out["findings"]
    # C held 30% of the rows, then vanished: a coverage change
    major = _counts_frame([{"A": 4, "B": 3, "C": 3}, {"A": 5, "B": 5}, {"A": 5, "B": 5}])
    out = pdc_diag.coverage_check(major, "period", ["region"], ["amount"])
    assert out["findings"]["coverage"] == "coverage_change"
    assert out["findings"]["coverage_dim"] == "region"
    assert out["values"]["coverage_jaccard"] == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# B8.4 detect_anomalies
# ---------------------------------------------------------------------------
def test_detect_anomalies_relative_spike():
    out = pdc_diag.detect_anomalies(_series_frame([10, 11, 10, 12, 100, 11]), "period", "amount")
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert f["anomaly"] == "anomaly_confirmed"
    assert f.get("anomaly_up") is True
    assert "anomaly_is_latest" not in f
    assert "multiple_anomalies" not in f
    assert "too_few_periods" not in f
    assert v["anomaly_period"] == "2025-05"
    assert v["anomaly_value"] == pytest.approx(100.0)
    assert v["baseline_value"] == pytest.approx(11.0)
    assert v["anomaly_ratio_median"] == pytest.approx(100.0 / 11.0)
    assert v["anomaly_prev_period"] == "2025-04"
    assert v["anomaly_prev_value"] == pytest.approx(12.0)
    assert v["anomaly_ratio_prev"] == pytest.approx(100.0 / 12.0)
    assert v["n_periods"] == 6
    assert v["n_anomalies"] == 1
    # `rows` / `is_data_gap` are the additive trailing columns of the BRB
    # retest follow-up (B8.4 gap rule).
    assert list(t.columns) == ["period", "value", "robust_z", "ratio_prev", "ratio_median",
                               "is_anomaly", "is_primary", "rows", "is_data_gap"]
    assert list(t["period"]) == [f"2025-{m:02d}" for m in range(1, 7)]
    assert int(t["is_primary"].sum()) == 1
    assert bool(t.loc[t["period"] == "2025-05", "is_primary"].iloc[0]) is True
    assert int(t["is_anomaly"].sum()) == 1
    assert pd.isna(t["ratio_prev"].iloc[0])


def test_detect_anomalies_latest_and_down():
    out = pdc_diag.detect_anomalies(_series_frame([100, 110, 100, 120, 110, 10]), "period", "amount")
    f = out["findings"]
    assert f["anomaly"] == "anomaly_confirmed"
    assert "anomaly_up" not in f
    assert f.get("anomaly_is_latest") is True
    assert out["values"]["anomaly_period"] == "2025-06"


def test_detect_anomalies_aggregates_rows_per_period():
    df = pd.DataFrame({"period": ["2025-01"] * 2 + ["2025-02"] * 2 + ["2025-03"] * 2 + ["2025-04"] * 2,
                       "amount": [5, 5, 6, 5, 5, 6, 50, 50]})
    out = pdc_diag.detect_anomalies(df, "period", "amount", agg="sum")
    assert out["findings"]["anomaly"] == "anomaly_confirmed"
    assert out["values"]["anomaly_value"] == pytest.approx(100.0)
    cnt = pdc_diag.detect_anomalies(df, "period", "amount", agg="count")
    assert cnt["findings"]["anomaly"] == "no_anomaly"
    with pytest.raises(ValueError):
        pdc_diag.detect_anomalies(df, "period", "amount", agg="max")


def test_detect_anomalies_flat_no_anomaly():
    out = pdc_diag.detect_anomalies(_series_frame([10, 11, 10, 12, 11, 10]), "period", "amount")
    f = out["findings"]
    assert f["anomaly"] == "no_anomaly"
    for key in ("anomaly_up", "anomaly_is_latest", "multiple_anomalies", "too_few_periods"):
        assert key not in f
    assert out["values"]["n_anomalies"] == 0
    assert int(out["table"]["is_anomaly"].sum()) == 0


def test_detect_anomalies_too_few_periods():
    out = pdc_diag.detect_anomalies(_series_frame([10, 11]), "period", "amount")
    _assert_shape(out)
    assert out["findings"].get("too_few_periods") is True
    assert "anomaly" not in out["findings"]
    assert out["values"]["n_periods"] == 2
    one = pdc_diag.detect_anomalies(_series_frame([10]), "period", "amount")
    assert one["findings"].get("too_few_periods") is True


def test_detect_anomalies_multiple():
    out = pdc_diag.detect_anomalies(_series_frame([10, 100, 10, 11, 90, 10, 11]), "period", "amount")
    f = out["findings"]
    assert f["anomaly"] == "anomaly_confirmed"
    assert f.get("multiple_anomalies") is True
    assert out["values"]["n_anomalies"] == 2
    # primary = the largest |log(ratio_median)|: the 100
    assert out["values"]["anomaly_period"] == "2025-02"


# --- degenerate periods (BRB retest follow-up: thin February) ---------------
ANOMALY_VALUE_KEYS = {"anomaly_period", "anomaly_value", "baseline_value",
                      "anomaly_ratio_median", "anomaly_prev_period", "anomaly_prev_value",
                      "anomaly_ratio_prev", "n_periods", "n_anomalies"}


def _counted_frame(periods, row_counts, totals, metric="amount") -> pd.DataFrame:
    """Each period has `row_counts[i]` rows whose metric sums to `totals[i]`."""
    parts = [pd.DataFrame({"period": [p] * n, metric: [t / n] * n})
             for p, n, t in zip(periods, row_counts, totals)]
    return pd.concat(parts, ignore_index=True)


def _gap_flags(table) -> dict:
    return {str(p): bool(g) for p, g in zip(table["period"], table["is_data_gap"])}


def test_thin_period_ratio_constant_shared_with_coverage_check():
    import inspect
    assert getattr(pdc_diag, "THIN_PERIOD_RATIO", None) == 0.5
    cov = inspect.signature(pdc_diag.coverage_check).parameters["ratio_low"].default
    assert cov == pdc_diag.THIN_PERIOD_RATIO
    det = inspect.signature(pdc_diag.detect_anomalies).parameters
    assert "thin_ratio" in det
    assert det["thin_ratio"].default == pdc_diag.THIN_PERIOD_RATIO


def test_detect_anomalies_collapsed_month_is_a_data_gap():
    periods = [f"2025-{m:02d}" for m in range(1, 6)]
    df = _counted_frame(periods, [1000, 1000, 1, 1000, 1000], [1000, 1000, 1, 1000, 1000])
    out = pdc_diag.detect_anomalies(df, "period", "amount")
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert f.get("period_status") == "data_gap"
    assert f["anomaly"] == "no_anomaly"
    for key in ("anomaly_up", "anomaly_is_latest", "multiple_anomalies"):
        assert key not in f
    assert v["n_anomalies"] == 0
    assert v["anomaly_period"] is None
    assert list(t.columns)[-2:] == ["rows", "is_data_gap"]
    assert [int(x) for x in t["rows"]] == [1000, 1000, 1, 1000, 1000]
    assert _gap_flags(t) == {p: p == "2025-03" for p in periods}
    assert not bool(t.loc[t["period"] == "2025-03", "is_anomaly"].iloc[0])
    assert int(t["is_primary"].sum()) == 0
    assert set(v) == ANOMALY_VALUE_KEYS


def test_detect_anomalies_zero_between_nonzero_neighbours_is_a_data_gap():
    periods = [f"2025-{m:02d}" for m in range(1, 6)]
    df = _counted_frame(periods, [10] * 5, [100, 100, 0, 100, 100])
    out = pdc_diag.detect_anomalies(df, "period", "amount")
    f, t = out["findings"], out["table"]
    assert f.get("period_status") == "data_gap"
    assert f["anomaly"] == "no_anomaly"
    assert out["values"]["n_anomalies"] == 0
    assert _gap_flags(t) == {p: p == "2025-03" for p in periods}
    assert [int(x) for x in t["rows"]] == [10] * 5


def test_detect_anomalies_real_spike_with_normal_rows_unchanged():
    out = pdc_diag.detect_anomalies(_series_frame([10, 11, 10, 12, 100, 11]), "period", "amount")
    f, t = out["findings"], out["table"]
    assert f["anomaly"] == "anomaly_confirmed"
    assert "period_status" not in f
    assert not any(bool(g) for g in t["is_data_gap"])
    assert [int(x) for x in t["rows"]] == [1] * 6
    assert t["is_data_gap"].dtype == bool


def test_detect_anomalies_brb_thin_february():
    """The Uzbek Q3 shape: February holds a fraction of the rows (3,792 of a
    median 12,617) and a near-zero total. It is a gap, never the anomaly; the
    primary anomaly is the March spike. Magnitudes invented."""
    periods = ["2025 Ноябрь", "2025 Декабрь", "2026 Январь", "2026 Февраль", "2026 Март"]
    rows = [26605, 12617, 11302, 3792, 33455]
    totals = [4500.0, 602.0, 564.0, 0.004, 4739.0]
    df = _counted_frame(periods, rows, totals, metric="npl")
    out = pdc_diag.detect_anomalies(df, "period", "npl")
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert list(t["period"]) == periods
    assert [int(x) for x in t["rows"]] == rows
    assert _gap_flags(t) == {p: p == "2026 Февраль" for p in periods}
    assert f.get("period_status") == "data_gap"
    assert f["anomaly"] == "anomaly_confirmed"
    assert f.get("anomaly_up") is True
    assert f.get("anomaly_is_latest") is True
    assert f.get("multiple_anomalies") is True          # November and March
    assert v["anomaly_period"] == "2026 Март"
    assert v["n_anomalies"] == 2
    assert list(t[t["is_primary"]]["period"]) == ["2026 Март"]
    feb = t[t["period"] == "2026 Февраль"].iloc[0]
    assert not bool(feb["is_anomaly"]) and not bool(feb["is_primary"])
    assert set(v) == ANOMALY_VALUE_KEYS                  # no new VALUES keys


def test_detect_anomalies_thin_ratio_kwarg():
    periods = [f"2025-{m:02d}" for m in range(1, 6)]
    # 300 rows of a median 1000: thin at the default 0.5, not thin at 0.2
    df = _counted_frame(periods, [1000, 1000, 300, 1000, 1000], [1000, 1000, 1, 1000, 1000])
    default = pdc_diag.detect_anomalies(df, "period", "amount")
    assert default["findings"].get("period_status") == "data_gap"
    loose = pdc_diag.detect_anomalies(df, "period", "amount", thin_ratio=0.2)
    assert "period_status" not in loose["findings"]
    assert loose["findings"]["anomaly"] == "anomaly_confirmed"
    assert loose["values"]["anomaly_period"] == "2025-03"


# ---------------------------------------------------------------------------
# B8.5 decompose_change
# ---------------------------------------------------------------------------
def test_decompose_change_concentrated():
    df = _two_period_dim_frame({"A": 100, "B": 100, "C": 100, "D": 100},
                               {"A": 300, "B": 100, "C": 100, "D": 100})
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["region"])
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert f["change_shape"] == "concentrated_in_one_dim"
    assert f["change_dim"] == "region"
    assert f.get("change_up") is True
    assert "change_large" not in f
    assert v["change_from_period"] == "2025-01"
    assert v["change_to_period"] == "2025-02"
    assert v["change_from_value"] == pytest.approx(400.0)
    assert v["change_to_value"] == pytest.approx(600.0)  # A=300 + B=C=D=100
    assert v["change_abs"] == pytest.approx(200.0)
    assert v["change_ratio"] == pytest.approx(1.5)
    assert v["change_pct"] == pytest.approx(0.5)
    assert v["change_top_member"] == "A"
    assert v["change_top_member_share"] == pytest.approx(1.0)
    assert v["change_top_member_delta"] == pytest.approx(200.0)
    assert v["change_dim_label"] == "region"
    assert list(t.columns) == ["dim", "member", "from_value", "to_value", "delta",
                               "share_of_delta", "share_of_base"]
    assert set(t["dim"]) == {"region"}
    assert "(other)" in set(t["member"])
    a = t[t["member"] == "A"].iloc[0]
    assert a["delta"] == pytest.approx(200.0)
    assert a["share_of_delta"] == pytest.approx(1.0)
    assert a["share_of_base"] == pytest.approx(0.25)


def test_decompose_change_spread_evenly():
    df = _two_period_dim_frame({"A": 100, "B": 100, "C": 100, "D": 100},
                               {"A": 200, "B": 200, "C": 200, "D": 200})
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["region"])
    f, v = out["findings"], out["values"]
    assert f["change_shape"] == "spread_evenly"
    assert "change_dim" not in f
    assert f.get("change_up") is True
    assert f.get("change_large") is True           # 800 / 400 = 2.0 >= large_ratio
    assert v["change_ratio"] == pytest.approx(2.0)
    assert v["change_pct"] == pytest.approx(1.0)


def test_decompose_change_neither_shape_absent_and_down():
    # a moderate, uneven change: not concentrated, not even
    df = _two_period_dim_frame({"A": 100, "B": 100, "C": 100, "D": 100},
                               {"A": 60, "B": 90, "C": 100, "D": 100})
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["region"])
    f = out["findings"]
    assert "change_shape" not in f or f["change_shape"] in pdc_diag.FINDINGS_ENUM
    assert "change_up" not in f                    # the total went down
    assert out["values"]["change_abs"] == pytest.approx(-50.0)


def test_decompose_change_default_last_two():
    df = pd.DataFrame({
        "period": ["2025-01", "2025-02", "2025-03"] * 2,
        "region": ["A"] * 3 + ["B"] * 3,
        "amount": [10, 20, 40, 10, 20, 50],
    })
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["region"])
    v = out["values"]
    assert v["change_from_period"] == "2025-02"
    assert v["change_to_period"] == "2025-03"
    assert v["change_from_value"] == pytest.approx(40.0)
    assert v["change_to_value"] == pytest.approx(90.0)
    explicit = pdc_diag.decompose_change(df, "period", "amount", from_period="2025-01",
                                         to_period="2025-03", dims=["region"])
    assert explicit["values"]["change_from_value"] == pytest.approx(20.0)
    assert explicit["values"]["change_to_value"] == pytest.approx(90.0)
    one = pdc_diag.decompose_change(df[df["period"] == "2025-01"], "period", "amount", dims=["region"])
    _assert_shape(one)
    assert one["findings"].get("too_few_periods") is True


def test_decompose_change_primary_anomaly_target():
    df = pd.DataFrame({
        "period": [f"2025-{m:02d}" for m in range(1, 7)] * 2,
        "region": ["A"] * 6 + ["B"] * 6,
        "amount": [5, 5, 5, 5, 90, 5, 5, 6, 5, 7, 10, 6],
    })
    out = pdc_diag.decompose_change(df, "period", "amount", to_period="primary_anomaly",
                                    dims=["region"])
    v = out["values"]
    assert v["change_to_period"] == "2025-05"
    assert v["change_from_period"] == "2025-04"
    assert out["findings"]["change_shape"] == "concentrated_in_one_dim"
    assert v["change_top_member"] == "A"
    flat = pdc_diag.decompose_change(_series_frame([10, 11, 10, 12, 11, 10]), "period", "amount",
                                     to_period="primary_anomaly", dims=["region"])
    assert flat["findings"].get("anomaly") == "no_anomaly"
    assert flat["values"]["change_to_period"] == "2025-06"


def test_decompose_change_missing_member_counts_as_zero_and_top_k():
    p1 = {f"M{i}": 10 for i in range(8)}
    p2 = {f"M{i}": 10 for i in range(8)}
    p2["M3"] = 90
    p2.pop("M7")             # vanished member: to_v = 0
    p2["NEW"] = 25           # new member: from_v = 0
    out = pdc_diag.decompose_change(_two_period_dim_frame(p1, p2), "period", "amount",
                                    dims=["region"], top_k=3)
    t = out["table"]
    assert len(t) == 4                          # top_k members + "(other)"
    m7 = t[t["member"] == "M7"]
    if len(m7):
        assert m7.iloc[0]["to_value"] == pytest.approx(0.0)
    new = t[t["member"] == "NEW"]
    if len(new):
        assert new.iloc[0]["from_value"] == pytest.approx(0.0)
    assert out["values"]["change_top_member"] == "M3"


# --- Unknown-dominated dimensions (BRB retest follow-up: SEKTOR) -----------
def _sektor_region_frame(p1: dict, p2: dict, sektor_from, sektor_to) -> pd.DataFrame:
    """Two periods by REGION; each row's SEKTOR cycles through the list."""
    rows = []
    for period, members, sektors in (("2025-01", p1, sektor_from), ("2025-02", p2, sektor_to)):
        for i, (member, v) in enumerate(members.items()):
            rows.append({"period": period, "REGION": member,
                         "SEKTOR": sektors[i % len(sektors)], "amount": v})
    return pd.DataFrame(rows)


EVEN_BASE = {"A": 100, "B": 100, "C": 100, "D": 100}


def test_decompose_change_skips_unknown_dominated_dim_concentrated():
    df = _sektor_region_frame(EVEN_BASE, {"A": 300, "B": 100, "C": 100, "D": 100},
                              ["Номаълум"], ["Номаълум"])
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["SEKTOR", "REGION"])
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert f.get("unknown_dominated") is True
    assert f.get("unknown_dim") == "SEKTOR"
    assert f["change_shape"] == "concentrated_in_one_dim"
    assert f["change_dim"] == "REGION"
    assert v["change_dim_label"] == "REGION"
    assert v["change_top_member"] == "A"
    assert set(t["dim"]) == {"REGION"}                  # no SEKTOR rows


def test_decompose_change_unknown_dim_never_becomes_the_top_member():
    """The BRB shape: REGION's change is uneven but not concentrated, so the
    all-Unknown SEKTOR (one member, 100 % of the delta) used to win the
    scoring and give `change_top_member = Номаълум` with no dim label."""
    df = _sektor_region_frame(EVEN_BASE, {"A": 150, "B": 140, "C": 100, "D": 100},
                              ["Номаълум"], ["Номаълум"])
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["SEKTOR", "REGION"])
    f, v, t = out["findings"], out["values"], out["table"]
    assert f.get("unknown_dominated") is True
    assert f.get("unknown_dim") == "SEKTOR"
    assert v["change_top_member"] == "A"
    assert v["change_dim_label"] == "REGION"
    assert "SEKTOR" not in set(t["dim"])


def test_decompose_change_all_dims_unknown_dominated():
    rows = []
    for period, vals in (("2025-01", [100, 100, 100]), ("2025-02", [300, 100, 100])):
        for v in vals:
            rows.append({"period": period, "SEKTOR": "Номаълум", "BRANCH": None,
                         "amount": v})
    df = pd.DataFrame(rows)
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["SEKTOR", "BRANCH"])
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert f.get("unknown_dominated") is True
    assert f.get("unknown_dim") == "SEKTOR"              # first in argument order
    assert "change_dim" not in f
    for key in ("change_dim_label", "change_top_member", "change_top_member_share",
                "change_top_member_delta"):
        assert v[key] is None, key
    assert len(t) == 0
    assert list(t.columns) == ["dim", "member", "from_value", "to_value", "delta",
                               "share_of_delta", "share_of_base"]
    assert v["change_abs"] == pytest.approx(200.0)       # the totals are still reported


def test_decompose_change_unknown_only_in_to_period_is_skipped():
    df = _sektor_region_frame(EVEN_BASE, {"A": 300, "B": 100, "C": 100, "D": 100},
                              ["retail", "corp"], ["unknown", "unknown", "unknown", "corp"])
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["SEKTOR", "REGION"])
    f, t = out["findings"], out["table"]
    assert f.get("unknown_dominated") is True
    assert f.get("unknown_dim") == "SEKTOR"
    assert "SEKTOR" not in set(t["dim"])
    assert out["values"]["change_dim_label"] == "REGION"


def test_decompose_change_exactly_half_unknown_is_not_skipped():
    df = _sektor_region_frame(EVEN_BASE, {"A": 300, "B": 100, "C": 100, "D": 100},
                              ["unknown", "retail"], ["unknown", "retail"])
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["SEKTOR", "REGION"])
    f, t = out["findings"], out["table"]
    assert "unknown_dominated" not in f
    assert "unknown_dim" not in f
    assert "SEKTOR" in set(t["dim"])


def test_decompose_change_unknown_share_kwarg():
    # 3 of 4 rows Unknown in both periods: a share of 0.75
    sektors = ["n/a", " N/A ", "-", "corp"]
    df = _sektor_region_frame(EVEN_BASE, {"A": 300, "B": 100, "C": 100, "D": 100},
                              sektors, sektors)
    default = pdc_diag.decompose_change(df, "period", "amount", dims=["SEKTOR", "REGION"])
    assert default["findings"].get("unknown_dim") == "SEKTOR"
    assert "SEKTOR" not in set(default["table"]["dim"])
    loose = pdc_diag.decompose_change(df, "period", "amount", dims=["SEKTOR", "REGION"],
                                      unknown_share=0.8)
    assert "unknown_dominated" not in loose["findings"]
    assert "SEKTOR" in set(loose["table"]["dim"])


def test_decompose_change_dim_label_set_when_spread_evenly():
    df = _two_period_dim_frame(EVEN_BASE, {"A": 200, "B": 200, "C": 200, "D": 200})
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["region"])
    f, v = out["findings"], out["values"]
    assert f["change_shape"] == "spread_evenly"
    assert v["change_top_member"] is not None
    assert v["change_dim_label"] == "region"


def test_decompose_change_dim_label_set_in_neither_branch():
    df = _two_period_dim_frame(EVEN_BASE, {"A": 150, "B": 140, "C": 100, "D": 100})
    out = pdc_diag.decompose_change(df, "period", "amount", dims=["region"])
    v = out["values"]
    assert "change_dim" not in out["findings"]           # not concentrated
    assert v["change_top_member"] == "A"
    assert v["change_dim_label"] == "region"


# ---------------------------------------------------------------------------
# B8.6 attribute_level
# ---------------------------------------------------------------------------
def test_attribute_level_dominant_feature():
    out = pdc_diag.attribute_level(_attribution_frame(True), "target", ["segment"], ["noise"])
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert f["attribution"] == "dominant_feature"
    assert f["top_feature"] == "segment"
    assert "second_feature" not in f               # noise explains ~nothing
    assert "method_fallback" not in f
    assert v["top_feature_label"] == "segment"
    assert v["top_feature_share"] > 0.9
    assert v["second_feature_share"] < 0.05
    assert v["n_rows_used"] == 300
    # B8.6: top_member = the member with the largest |mean - overall|; with
    # S1~10, S2~50, S3~100 and overall~56.5 that is S1 (|-46.5| > |43.5|).
    assert v["top_member"] == "S1"
    assert v["top_member_target"] == pytest.approx(10.0, abs=2.0)
    assert v["overall_target"] == pytest.approx(float(_attribution_frame(True)["target"].mean()), abs=1.0)
    assert list(t.columns) == ["feature", "kind", "importance", "top_member", "top_member_target"]
    assert set(t["feature"]) == {"segment", "noise"}
    assert set(t["kind"]) == {"dimension", "numeric"}
    assert t.iloc[0]["feature"] == "segment"       # sorted by importance


def test_attribute_level_weak_signal():
    out = pdc_diag.attribute_level(_attribution_frame(False, n=1000), "target", ["segment"], ["noise"])
    f, v = out["findings"], out["values"]
    assert f["attribution"] == "weak_signal"
    assert v["top_feature_share"] < 0.05
    assert f.get("top_feature") in (None, "segment", "noise")
    assert v["n_rows_used"] == 1000


def test_attribute_level_variance_and_forest_methods():
    df = _attribution_frame(True)
    var = pdc_diag.attribute_level(df, "target", ["segment"], ["noise"], method="variance")
    assert var["findings"]["top_feature"] == "segment"
    assert "method_fallback" not in var["findings"]
    pytest.importorskip("sklearn")
    forest = pdc_diag.attribute_level(df, "target", ["segment"], ["noise"], method="forest")
    _assert_shape(forest)
    assert forest["findings"]["top_feature"] == "segment"
    assert "method_fallback" not in forest["findings"]


def test_attribute_level_shap_fallback_flag(monkeypatch):
    # `shap` is deliberately not a dependency (decision F7); make sure the
    # import fails here even on a box that happens to have it.
    monkeypatch.setitem(sys.modules, "shap", None)
    out = pdc_diag.attribute_level(_attribution_frame(True), "target", ["segment"], ["noise"],
                                   method="shap")
    _assert_shape(out)
    assert out["findings"].get("method_fallback") is True
    assert out["findings"]["top_feature"] == "segment"
    assert out["findings"]["attribution"] == "dominant_feature"


def test_attribute_level_max_rows_sampling_and_null_targets():
    df = _attribution_frame(True)
    df.loc[:9, "target"] = np.nan
    out = pdc_diag.attribute_level(df, "target", ["segment"], ["noise"], max_rows=100)
    assert out["values"]["n_rows_used"] == 100
    assert out["findings"]["top_feature"] == "segment"


# ---------------------------------------------------------------------------
# B8.7 check_metric_overlap
# ---------------------------------------------------------------------------
def test_check_metric_overlap_identical():
    out = pdc_diag.check_metric_overlap(_overlap_frame(20, 20), "a", "b")
    _assert_shape(out)
    f, v = out["findings"], out["values"]
    assert f["metric_relation"] == "metric_definition_overlap"
    assert f.get("metrics_identical") is True
    assert v["overlap_equal_share"] == pytest.approx(1.0)
    assert v["overlap_sum_ratio"] == pytest.approx(1.0)
    assert v["overlap_rows"] == 20
    assert len(out["table"]) == 1


def test_check_metric_overlap_95_percent():
    out = pdc_diag.check_metric_overlap(_overlap_frame(19, 20), "a", "b")
    f, v = out["findings"], out["values"]
    assert f["metric_relation"] == "metric_definition_overlap"
    assert "metrics_identical" not in f
    assert v["overlap_equal_share"] == pytest.approx(0.95)
    assert v["overlap_rows"] == 20


def test_check_metric_overlap_distinct_metrics():
    out = pdc_diag.check_metric_overlap(_overlap_frame(6, 20), "a", "b")
    assert "metric_relation" not in out["findings"]
    assert "metrics_identical" not in out["findings"]
    assert out["values"]["overlap_equal_share"] == pytest.approx(0.3)


def test_check_metric_overlap_nulls_and_tolerance():
    df = _overlap_frame(20, 20)
    df.loc[0, "a"] = np.nan
    df.loc[1, "b"] = np.nan
    df.loc[2, "b"] = df.loc[2, "a"] * (1 + 1e-12)      # within rel_tol: equal
    out = pdc_diag.check_metric_overlap(df, "a", "b")
    assert out["values"]["overlap_rows"] == 18
    assert out["values"]["overlap_equal_share"] == pytest.approx(1.0)
    strict = pdc_diag.check_metric_overlap(df, "a", "b", rel_tol=1e-15)
    assert strict["values"]["overlap_equal_share"] == pytest.approx(17 / 18)


# Follow-up (narrative s1): rows where both metrics are 0 are not compared;
# below `min_compared_share` of the non-null rows the share is not judged.

def _zero_padded_overlap(n_zero: int, a_tail, b_tail) -> pd.DataFrame:
    return pd.DataFrame({"a": [0.0] * n_zero + [float(x) for x in a_tail],
                         "b": [0.0] * n_zero + [float(x) for x in b_tail]})


def test_check_metric_overlap_ignores_both_zero_rows():
    # 98 both-zero rows, the 2 others differ: 2 of 100 compared — not judged
    df = _zero_padded_overlap(98, [1, 2], [3, 4])
    out = pdc_diag.check_metric_overlap(df, "a", "b")
    f, v = out["findings"], out["values"]
    assert "metric_relation" not in f
    assert "metrics_identical" not in f
    assert v["overlap_equal_share"] is None
    assert v["overlap_rows"] == 2
    assert v["overlap_nonnull_rows"] == 100

    # 80 both-zero rows + 20 non-zero rows, 19 of them equal: 0.95 over 20
    tail_a = [100 * k for k in range(1, 21)]
    tail_b = list(tail_a)
    tail_b[-1] = tail_b[-1] * 3 + 7
    df = _zero_padded_overlap(80, tail_a, tail_b)
    out = pdc_diag.check_metric_overlap(df, "a", "b")
    f, v = out["findings"], out["values"]
    assert f["metric_relation"] == "metric_definition_overlap"
    assert "metrics_identical" not in f
    assert v["overlap_equal_share"] == pytest.approx(0.95)
    assert v["overlap_rows"] == 20
    assert v["overlap_nonnull_rows"] == 100


def test_check_metric_overlap_min_compared_share_kwarg():
    # 95 both-zero rows + 5 equal non-zero rows
    df = _zero_padded_overlap(95, [1, 2, 3, 4, 5], [1, 2, 3, 4, 5])
    out = pdc_diag.check_metric_overlap(df, "a", "b")
    assert "metric_relation" not in out["findings"]
    assert out["values"]["overlap_equal_share"] is None
    loose = pdc_diag.check_metric_overlap(df, "a", "b", min_compared_share=0.0)
    assert loose["findings"]["metric_relation"] == "metric_definition_overlap"
    assert loose["values"]["overlap_equal_share"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# prefix, registry, value conventions, errors
# ---------------------------------------------------------------------------
def test_prefix_applies_to_values_only():
    series = _series_frame([10, 11, 10, 12, 100, 11])
    plain = pdc_diag.detect_anomalies(series, "period", "amount")
    pref = pdc_diag.detect_anomalies(series, "period", "amount", prefix="q3_")
    assert pref["findings"] == plain["findings"]
    assert set(pref["values"]) == {"q3_" + k for k in plain["values"]}
    assert pref["values"]["q3_anomaly_period"] == plain["values"]["anomaly_period"]
    cov_plain = pdc_diag.coverage_check(_member_change_frame(), "period", ["region"], ["amount"])
    cov_pref = pdc_diag.coverage_check(_member_change_frame(), "period", ["region"], ["amount"],
                                       prefix="a1_")
    assert cov_pref["findings"] == cov_plain["findings"]
    assert set(cov_pref["values"]) == {"a1_" + k for k in cov_plain["values"]}
    assert all(k.startswith("a1_") for k in cov_pref["values"])
    dec = pdc_diag.decompose_change(
        _two_period_dim_frame({"A": 100, "B": 100}, {"A": 300, "B": 100}),
        "period", "amount", dims=["region"], prefix="x_")
    assert all(k.startswith("x_") for k in dec["values"])
    assert not any(k.startswith("x_") for k in dec["findings"])
    ov = pdc_diag.check_metric_overlap(_overlap_frame(20, 20), "a", "b", prefix="m2_")
    assert set(ov["values"]) == {"m2_overlap_equal_share", "m2_overlap_sum_ratio", "m2_overlap_rows",
                                 "m2_overlap_nonnull_rows"}
    at = pdc_diag.attribute_level(_attribution_frame(True), "target", ["segment"], ["noise"], prefix="p_")
    assert all(k.startswith("p_") for k in at["values"])
    assert "top_feature" in at["findings"]


@pytest.mark.parametrize("bad", ["Q3_", "q3", "3q_", "abcdefghijk_", "q-3_", "q3__", "_", "q 3_"])
def test_prefix_rejected_with_value_error(bad):
    with pytest.raises(ValueError):
        pdc_diag.detect_anomalies(_series_frame([1, 2, 3]), "period", "amount", prefix=bad)
    with pytest.raises(ValueError):
        pdc_diag.coverage_check(_stable_frame(), "period", ["region"], ["amount"], prefix=bad)


@pytest.mark.parametrize("good", ["", "q3_", "a_", "abcdefghij_", "p2x_"])
def test_prefix_accepted(good):
    out = pdc_diag.detect_anomalies(_series_frame([1, 2, 3]), "period", "amount", prefix=good)
    _assert_shape(out)


def test_constants():
    assert pdc_diag.PDC_DIAG_VERSION == 1
    assert isinstance(pdc_diag.PDC_DIAG_FINDING_KEYS, frozenset)
    assert pdc_diag.PDC_DIAG_FINDING_KEYS == frozenset({
        "coverage", "coverage_dim", "thin_period", "period_status", "unknown_dominated", "unknown_dim",
        "units", "metric_gap", "period_order_unknown", "latest_is_current_period", "latest_is_past_period",
        "anomaly", "anomaly_up", "anomaly_is_latest", "multiple_anomalies", "too_few_periods",
        "change_shape", "change_dim", "change_up", "change_large",
        "attribution", "top_feature", "second_feature", "method_fallback",
        "metric_relation", "metrics_identical"})
    assert tuple(pdc_diag.FINDINGS_ENUM) == (
        "coverage_change", "coverage_stable", "partial_period", "metric_definition_overlap",
        "mixed_units", "concentrated_in_one_dim", "spread_evenly", "weak_signal",
        "dominant_feature", "anomaly_confirmed", "no_anomaly",
        "data_gap", "outlier_driven", "seasonal_pattern")
    assert "needs_phase2" not in pdc_diag.FINDINGS_ENUM
    assert "needs_phase2" not in pdc_diag.PDC_DIAG_FINDING_KEYS
    assert "unknown" in pdc_diag.UNKNOWN_TOKENS and "" in pdc_diag.UNKNOWN_TOKENS
    assert "უცნობი" in pdc_diag.UNKNOWN_TOKENS and "noma'lum" in pdc_diag.UNKNOWN_TOKENS


def test_findings_keys_in_registry():
    for out, _df in _all_outputs():
        _assert_shape(out)
        unknown = set(out["findings"]) - pdc_diag.PDC_DIAG_FINDING_KEYS
        assert unknown == set(), unknown


def test_findings_values_bool_enum_or_column_only():
    for out, df in _all_outputs():
        columns = {str(c) for c in df.columns}
        for key, value in out["findings"].items():
            if isinstance(value, bool):
                continue
            assert isinstance(value, str), (key, type(value))
            assert value in pdc_diag.FINDINGS_ENUM or value in columns, (key, value)
            # never a number, never a category value
            assert value not in {"A", "B", "C", "D", "S1", "S3", "M3", "2025-05"}, (key, value)


def test_findings_bools_emitted_only_when_true():
    for out, _df in _all_outputs():
        for key, value in out["findings"].items():
            if isinstance(value, bool) and key != "latest_is_current_period":
                assert value is True, (key, value)


def test_values_are_plain_scalars():
    for out, _df in _all_outputs():
        for key, value in out["values"].items():
            assert isinstance(key, str) and key.isidentifier(), key
            assert value is None or isinstance(value, (bool, int, float, str)), (key, type(value))
            if isinstance(value, str):
                assert len(value) <= 200, key
        for key in ("coverage_dim_label", "unknown_dim_label", "change_dim_label",
                    "top_feature_label", "thin_period_label", "latest_period"):
            if key in out["values"] and out["values"][key] is not None:
                assert isinstance(out["values"][key], str), key


@pytest.mark.parametrize("call,param,column", [
    (lambda df: pdc_diag.coverage_check(df, "nope", ["region"], ["amount"]), "period_col", "nope"),
    (lambda df: pdc_diag.coverage_check(df, "period", ["ghost"], ["amount"]), "dims", "ghost"),
    (lambda df: pdc_diag.coverage_check(df, "period", ["region"], ["ghost"]), "metrics", "ghost"),
    (lambda df: pdc_diag.coverage_check(df, "period", ["region"], ["amount"], unit_cols=["ccy"]),
     "unit_cols", "ccy"),
    (lambda df: pdc_diag.detect_anomalies(df, "nope", "amount"), "period_col", "nope"),
    (lambda df: pdc_diag.detect_anomalies(df, "period", "ghost"), "metric", "ghost"),
    (lambda df: pdc_diag.detect_anomalies(df, "period", "region"), "metric", "region"),
    (lambda df: pdc_diag.decompose_change(df, "period", "ghost", dims=["region"]), "metric", "ghost"),
    (lambda df: pdc_diag.decompose_change(df, "period", "amount", dims=["ghost"]), "dims", "ghost"),
    (lambda df: pdc_diag.attribute_level(df, "ghost", ["region"], []), "target", "ghost"),
    (lambda df: pdc_diag.attribute_level(df, "amount", ["ghost"], []), "dims", "ghost"),
    (lambda df: pdc_diag.attribute_level(df, "amount", ["region"], ["ghost"]), "numeric_cols", "ghost"),
    (lambda df: pdc_diag.check_metric_overlap(df, "amount", "ghost"), "metric_b", "ghost"),
    (lambda df: pdc_diag.check_metric_overlap(df, "ghost", "amount"), "metric_a", "ghost"),
    (lambda df: pdc_diag.check_metric_overlap(df, "region", "amount"), "metric_a", "region"),
])
def test_missing_column_raises_value_error(call, param, column):
    with pytest.raises(ValueError) as info:
        call(_stable_frame())
    text = str(info.value)
    assert param in text, text
    assert column in text, text


# ---------------------------------------------------------------------------
# B8.8 plot helpers
# ---------------------------------------------------------------------------
def _is_plotly_white(fig):
    import plotly.io as pio
    return fig.layout.template.to_plotly_json() == pio.templates["plotly_white"].to_plotly_json()


def test_plot_helpers_return_one_plotly_figure():
    go = pytest.importorskip("plotly.graph_objects")
    series = pdc_diag.detect_anomalies(_series_frame([10, 11, 10, 12, 100, 11]), "period", "amount")
    fig1 = pdc_diag.plot_highlighted_series(series["table"])
    assert isinstance(fig1, go.Figure)
    assert len(fig1.data) >= 1
    assert _is_plotly_white(fig1)
    assert fig1.layout.title.text == "value by period — highlighted period"
    fig1b = pdc_diag.plot_highlighted_series(series["table"], title="T", y_title="Amount")
    assert fig1b.layout.title.text == "T"
    assert fig1b.layout.yaxis.title.text == "Amount"

    change = pdc_diag.decompose_change(
        _two_period_dim_frame({"A": 100, "B": 100, "C": 100, "D": 100},
                              {"A": 300, "B": 100, "C": 100, "D": 100}),
        "period", "amount", dims=["region"])
    fig2 = pdc_diag.plot_waterfall(change["table"])
    assert isinstance(fig2, go.Figure)
    assert len(fig2.data) >= 1
    assert _is_plotly_white(fig2)
    assert "What changed" in (fig2.layout.title.text or "")
    fig2b = pdc_diag.plot_waterfall(change["table"], dim="region", title="W")
    assert fig2b.layout.title.text == "W"

    imp = pdc_diag.attribute_level(_attribution_frame(True), "target", ["segment"], ["noise"])
    fig3 = pdc_diag.plot_importance(imp["table"])
    assert isinstance(fig3, go.Figure)
    assert len(fig3.data) >= 1
    assert _is_plotly_white(fig3)
    assert "What explains" in (fig3.layout.title.text or "")
    assert fig3.layout.xaxis.title.text == "Share of variation explained"
    assert fig3.data[0].orientation == "h"


def test_plot_helpers_palette_and_bad_table():
    pytest.importorskip("plotly.graph_objects")
    assert pdc_diag._PALETTE == {"base": "#4C78A8", "highlight": "#E45756", "up": "#54A24B",
                                 "down": "#E45756", "total": "#4C78A8"}
    bad = pd.DataFrame({"x": [1, 2]})
    with pytest.raises(ValueError):
        pdc_diag.plot_highlighted_series(bad)
    with pytest.raises(ValueError):
        pdc_diag.plot_waterfall(bad)
    with pytest.raises(ValueError):
        pdc_diag.plot_importance(bad)


# ---------------------------------------------------------------------------
# leaf module (AST)
# ---------------------------------------------------------------------------
def _module_scope_import_roots(tree) -> set:
    roots = set()

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
                continue
            if isinstance(child, ast.Import):
                for alias in child.names:
                    roots.add(alias.name.split(".")[0])
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and child.module:
                roots.add(child.module.split(".")[0])
            walk(child)

    walk(tree)
    return roots


def _all_import_roots(tree) -> set:
    roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _called_names(tree) -> set:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def test_pdc_diag_top_level_imports_are_leaf():
    path = ROOT / "pdc_diag.py"
    assert path.is_file(), "pdc_diag.py must live at the client root"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    stdlib = set(sys.stdlib_module_names)
    top = _module_scope_import_roots(tree)
    assert top, "no module-level imports at all?"
    outside = top - stdlib - {"numpy", "pandas"}
    assert outside == set(), sorted(outside)
    # plotly / sklearn / shap only lazily, and never anything of the client
    everything = _all_import_roots(tree)
    for forbidden in ("logger_utils", "logging", "settings", "local_store", "brain_client",
                      "exec_transport", "code_exec", "plot_utils", "run_chat_local",
                      "executor_client", "narrative", "db_connector", "db_sources",
                      "roles_store", "sso_store", "httpx", "requests"):
        assert forbidden not in everything, forbidden
    called = _called_names(tree)
    assert "exec" not in called, "no exec() anywhere in pdc_diag.py"
    assert "eval" not in called
    assert "print" not in called, "pdc_diag never prints (it runs in the sandbox)"
    assert "log_with_sid" not in called
    for noisy in ("info", "warning", "error", "debug"):
        # no logger calls either
        assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                       and n.func.attr == noisy and isinstance(n.func.value, ast.Name)
                       and n.func.value.id in ("logging", "logger", "log")
                       for n in ast.walk(tree)), noisy
    assert "exec(" not in path.read_text(encoding="utf-8")


def test_pdc_diag_public_surface():
    for name in ("fmt", "coverage_check", "detect_anomalies", "decompose_change",
                 "attribute_level", "check_metric_overlap", "plot_highlighted_series",
                 "plot_waterfall", "plot_importance", "_order_periods", "_parse_period_label"):
        assert callable(getattr(pdc_diag, name, None)), name
