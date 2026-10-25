"""`pdc_diag` — the BRB run-3 follow-up (written BEFORE the implementation).

Pins the contract of:
- `detect_anomalies(..., rows_col=None, dims=None)`: a primary anomaly whose
  ROW COUNT moved more than `SCOPE_ROWS_FACTOR` (3x) against the period right
  before it — or whose coverage dims changed between those two periods — is a
  change of reporting SCOPE, not a business change: `anomaly = "scope_change"`
  and no `anomaly_up` / `anomaly_is_latest`. New VALUE `rows_ratio`.
- `decompose_change(..., to_period="primary_anomaly", rows_col=None)`: a
  scope change gets NO member attribution (`change_shape = "scope_change"`).
- `rate_by_period`: numerator / denominator per period, first / last finite
  rate and the change in percentage points (`rate_up` / `rate_down`).
- `coverage_check`: the Unknown dim is chosen by its share in the LATEST
  period first; a label of a unit column that ends before the last period
  while the column stays filled (`label_disappeared`).

Offline, synthetic frames. The BRB shape (periods, row counts, NPL totals)
mirrors tests/test_pdc_diag.py; the magnitudes are the retest's, the rows are
invented (each period's metric spread evenly over its rows).
"""
import math

import pandas as pd
import pytest

import pdc_diag

RETURN_KEYS = {"table", "values", "findings"}

BRB_PERIODS = ["2025 Ноябрь", "2025 Декабрь", "2026 Январь", "2026 Февраль", "2026 Март"]
BRB_ROWS = [26605, 12617, 11302, 3792, 33455]
BRB_NPL = [4500.50e9, 602.44e9, 563.70e9, 4.0e6, 4739.29e9]
BRB_LOAN = [25.06e12, 5.90e12, 6.49e12, 0.81e12, 62.96e12]
BRB_RATIO = 33455 / 3792

UZS = "Ўзбек Сўми (UZS)"
OTHER_CCY = "Бошқа Валюталар"
UNKNOWN = "Номаълум"

RATE_COLUMNS = ["period", "numerator", "denominator", "rate"]
RATE_VALUE_KEYS = {"rate_first", "rate_last", "rate_change_pp", "rate_first_period",
                   "rate_last_period", "period_from", "period_to", "n_periods"}


# ---------------------------------------------------------------------------
# helpers (copied from tests/test_pdc_diag.py, not imported)
# ---------------------------------------------------------------------------
def _counted_frame(periods, row_counts, totals, metric="amount") -> pd.DataFrame:
    """Each period has `row_counts[i]` rows whose metric sums to `totals[i]`."""
    parts = [pd.DataFrame({"period": [p] * n, metric: [t / n] * n})
             for p, n, t in zip(periods, row_counts, totals)]
    return pd.concat(parts, ignore_index=True)


def _assert_shape(out):
    assert isinstance(out, dict), type(out)
    assert set(out) == RETURN_KEYS, sorted(out)
    assert isinstance(out["table"], pd.DataFrame)
    assert isinstance(out["values"], dict)
    assert isinstance(out["findings"], dict)


def _iso(n):
    return [f"2025-{m:02d}" for m in range(1, n + 1)]


def _brb_raw(metric="npl", extra_dims=False) -> pd.DataFrame:
    df = _counted_frame(BRB_PERIODS, BRB_ROWS, BRB_NPL, metric=metric)
    if extra_dims:
        n = len(df)
        df["TYPE"] = [("T1", "T2")[i % 2] for i in range(n)]
        df["REGION"] = [("R1", "R2", "R3")[i % 3] for i in range(n)]
        df["SEKTOR"] = UNKNOWN
    return df


def _brb_pre_aggregated() -> pd.DataFrame:
    return pd.DataFrame({"period": BRB_PERIODS, "npl": BRB_NPL, "rows": BRB_ROWS})


# ===========================================================================
# constants
# ===========================================================================
def test_scope_constants_and_registry():
    assert getattr(pdc_diag, "SCOPE_ROWS_FACTOR", None) == 3.0
    assert tuple(pdc_diag.FINDINGS_ENUM)[-2:] == ("scope_change", "label_disappeared")
    for key in ("label_disappeared", "rate_up", "rate_down"):
        assert key in pdc_diag.PDC_DIAG_FINDING_KEYS, key


def test_rate_by_period_is_public_and_guarded():
    import sandbox_guard
    assert callable(getattr(pdc_diag, "rate_by_period", None))
    assert "rate_by_period" in sandbox_guard.PDC_DIAG_PUBLIC
    # a generated block may not define its own rate_by_period
    code = "def rate_by_period(df):\n    return {}\nRESULT = 1"
    assert sandbox_guard.pdc_diag_redefinition(code) == sandbox_guard.PDC_DIAG_REDEFINED_TEXT


# ===========================================================================
# detect_anomalies: scope change
# ===========================================================================
def _assert_brb_scope(out):
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert f["anomaly"] == "scope_change"
    assert "anomaly_up" not in f
    assert "anomaly_is_latest" not in f
    assert "anomaly_confirmed" not in f.values()
    assert f.get("period_status") == "data_gap"            # February is still a gap
    assert v["anomaly_period"] == "2026 Март"
    assert v["anomaly_prev_period"] == "2026 Февраль"
    assert v["anomaly_value"] == pytest.approx(BRB_NPL[4])
    assert v["anomaly_prev_value"] == pytest.approx(BRB_NPL[3])
    assert v["rows_ratio"] == pytest.approx(BRB_RATIO, abs=1e-3)
    assert list(t["period"]) == BRB_PERIODS
    assert list(t[t["is_primary"]]["period"]) == ["2026 Март"]
    assert list(t.columns) == ["period", "value", "robust_z", "ratio_prev", "ratio_median",
                               "is_anomaly", "is_primary", "rows", "is_data_gap"]


def test_detect_anomalies_brb_raw_rows_is_scope_change():
    out = pdc_diag.detect_anomalies(_brb_raw(), "period", "npl")
    _assert_brb_scope(out)
    assert [int(x) for x in out["table"]["rows"]] == BRB_ROWS
    assert out["findings"].get("multiple_anomalies") is True   # unchanged rule


def test_detect_anomalies_rows_col_on_pre_aggregated_frame():
    out = pdc_diag.detect_anomalies(_brb_pre_aggregated(), "period", "npl", rows_col="rows")
    _assert_brb_scope(out)
    assert [int(x) for x in out["table"]["rows"]] == BRB_ROWS
    assert out["values"]["data_gap_period"] == "2026 Февраль"


def test_detect_anomalies_pre_aggregated_without_rows_col_keeps_today():
    """One row per period and no rows_col: the row counts are unknown, so no
    scope rule can fire and rows_ratio stays None."""
    out = pdc_diag.detect_anomalies(_brb_pre_aggregated().drop(columns=["rows"]), "period", "npl")
    f, v = out["findings"], out["values"]
    assert f["anomaly"] == "anomaly_confirmed"
    assert f["anomaly"] != "scope_change"
    assert v["anomaly_period"] == "2026 Март"
    assert "rows_ratio" in v
    assert v["rows_ratio"] is None


def test_detect_anomalies_equal_rows_stays_confirmed():
    df = _counted_frame(BRB_PERIODS, [1000] * 5, BRB_NPL, metric="npl")
    out = pdc_diag.detect_anomalies(df, "period", "npl")
    f, v = out["findings"], out["values"]
    assert f["anomaly"] == "anomaly_confirmed"
    assert v["rows_ratio"] == 1.0
    assert isinstance(v["rows_ratio"], float)
    # the BRB shape without its February collapse: March is the primary
    totals = [4500.50e9, 602.44e9, 563.70e9, 580.0e9, 4739.29e9]
    df = _counted_frame(BRB_PERIODS, [1000] * 5, totals, metric="npl")
    out = pdc_diag.detect_anomalies(df, "period", "npl")
    f, v = out["findings"], out["values"]
    assert f["anomaly"] == "anomaly_confirmed"
    assert f.get("anomaly_up") is True
    assert f.get("anomaly_is_latest") is True
    assert v["anomaly_period"] == "2026 Март"
    assert v["rows_ratio"] == 1.0


def test_detect_anomalies_rows_ratio_none_without_primary_or_rows():
    flat = _counted_frame(_iso(5), [10] * 5, [100, 101, 99, 100, 100])
    out = pdc_diag.detect_anomalies(flat, "period", "amount")
    assert out["findings"]["anomaly"] == "no_anomaly"
    assert out["values"]["rows_ratio"] is None
    short = pdc_diag.detect_anomalies(_counted_frame(_iso(2), [10, 10], [1, 2]), "period", "amount")
    assert "rows_ratio" in short["values"]
    assert short["values"]["rows_ratio"] is None
    pref = pdc_diag.detect_anomalies(_brb_raw(), "period", "npl", prefix="q3_")
    assert pref["values"]["q3_rows_ratio"] == pytest.approx(BRB_RATIO, abs=1e-3)
    assert "rows_ratio" not in pref["values"]


def test_detect_anomalies_rows_drop_below_a_third_is_scope_change():
    # the spike period holds 120 rows after a 400-row period: 0.3 < 1/3
    df = _counted_frame(_iso(6), [100, 100, 100, 400, 120, 100], [10, 11, 10, 12, 100, 11])
    out = pdc_diag.detect_anomalies(df, "period", "amount")
    f, v = out["findings"], out["values"]
    assert v["anomaly_period"] == "2025-05"
    assert f["anomaly"] == "scope_change"
    assert v["rows_ratio"] == pytest.approx(0.3)
    # 140 rows after 400: 0.35 >= 1/3, a business spike
    df = _counted_frame(_iso(6), [100, 100, 100, 400, 140, 100], [10, 11, 10, 12, 100, 11])
    out = pdc_diag.detect_anomalies(df, "period", "amount")
    assert out["findings"]["anomaly"] == "anomaly_confirmed"
    assert out["values"]["rows_ratio"] == pytest.approx(0.35)


def test_detect_anomalies_rows_col_zero_previous_rows_is_scope_change():
    """rows_col NaN counts as 0 rows; a primary after a 0-row period is a
    scope change and rows_ratio cannot be computed."""
    df = pd.DataFrame({"period": _iso(5), "amount": [100.0, 100.0, 100.0, 100.0, 900.0],
                       "n": [1000, 1000, 1000, float("nan"), 1000]})
    out = pdc_diag.detect_anomalies(df, "period", "amount", rows_col="n")
    f, v, t = out["findings"], out["values"], out["table"]
    assert [int(x) for x in t["rows"]] == [1000, 1000, 1000, 0, 1000]
    assert v["anomaly_period"] == "2025-05"
    assert f["anomaly"] == "scope_change"
    assert v["rows_ratio"] is None


@pytest.mark.parametrize("fill", [float("nan"), 0.0], ids=["all_nan", "all_zero"])
def test_detect_anomalies_rows_col_without_rows_never_fires_the_rows_rule(fill):
    """A rows_col that sums to 0 everywhere knows no rows: the spike stays a
    confirmed anomaly and rows_ratio stays None."""
    df = pd.DataFrame({"period": _iso(5), "amount": [100.0, 100.0, 100.0, 100.0, 900.0],
                       "n": [fill] * 5})
    out = pdc_diag.detect_anomalies(df, "period", "amount", rows_col="n")
    f, v = out["findings"], out["values"]
    assert v["anomaly_period"] == "2025-05"
    assert f["anomaly"] == "anomaly_confirmed"
    assert f.get("anomaly_up") is True
    assert v["rows_ratio"] is None
    assert [int(x) for x in out["table"]["rows"]] == [0] * 5


def test_detect_anomalies_rows_col_zero_previous_and_primary_rows_is_not_scope():
    """With rows only in the first periods, a primary of 0 rows after a
    0-row period is not a scope change from the rows rule."""
    df = pd.DataFrame({"period": _iso(5), "amount": [100.0, 100.0, 100.0, 100.0, 900.0],
                       "n": [1000, 1000, 1000, 1000, 1000]})
    scoped = pdc_diag._scope_change(4, [1000, 1000, 1000, 0, 0], True,
                                    pdc_diag._period_frames(df, "period", _iso(5)), [])
    assert scoped == (False, None)
    scoped = pdc_diag._scope_change(4, [1000, 1000, 1000, 0, 5], True,
                                    pdc_diag._period_frames(df, "period", _iso(5)), [])
    assert scoped == (True, None)


def test_detect_anomalies_rows_col_non_finite_sum_counts_as_zero_rows():
    df = pd.DataFrame({"period": _iso(5), "amount": [100.0, 100.0, 100.0, 100.0, 900.0],
                       "n": [1000, 1000, float("inf"), 1000, 1000]})
    out = pdc_diag.detect_anomalies(df, "period", "amount", rows_col="n")
    assert [int(x) for x in out["table"]["rows"]] == [1000, 1000, 0, 1000, 1000]


def test_detect_anomalies_rows_col_turns_the_collapse_rule_off():
    """One row per period: without rows_col a collapsed value is a data gap;
    with rows_col the counts are known and only the thin-rows and zero rules
    find gaps, so the collapsed period is the anomaly."""
    df = pd.DataFrame({"period": _iso(6), "amount": [100.0, 100.0, 100.0, 0.5, 100.0, 100.0],
                       "n": [1, 1, 1, 1, 1, 1]})
    plain = pdc_diag.detect_anomalies(df, "period", "amount")
    assert plain["findings"].get("period_status") == "data_gap"
    assert plain["values"]["data_gap_period"] == "2025-04"
    counted = pdc_diag.detect_anomalies(df, "period", "amount", rows_col="n")
    assert "period_status" not in counted["findings"]
    assert counted["values"]["data_gap_period"] is None
    assert not counted["table"]["is_data_gap"].any()
    assert counted["values"]["anomaly_period"] == "2025-04"
    assert counted["findings"]["anomaly"] == "anomaly_confirmed"
    assert counted["values"]["rows_ratio"] == 1.0


def _switch_frame(switch: bool) -> pd.DataFrame:
    """Six ISO months, 10 rows each, a spike in month 5. BRANCH holds A/B in
    every month except month 5, which holds X/Y when `switch`."""
    parts = []
    for i, (period, total) in enumerate(zip(_iso(6), [10, 11, 10, 12, 100, 11])):
        members = ["X", "Y"] if (switch and i == 4) else ["A", "B"]
        parts.append(pd.DataFrame({"period": [period] * 10,
                                   "BRANCH": [members[j % 2] for j in range(10)],
                                   "amount": [total / 10] * 10}))
    return pd.concat(parts, ignore_index=True)


def test_detect_anomalies_dims_member_switch_is_scope_change():
    df = _switch_frame(True)
    out = pdc_diag.detect_anomalies(df, "period", "amount", dims=["BRANCH"])
    f, v = out["findings"], out["values"]
    assert v["anomaly_period"] == "2025-05"
    assert f["anomaly"] == "scope_change"
    assert "anomaly_up" not in f
    assert v["rows_ratio"] == 1.0
    plain = pdc_diag.detect_anomalies(df, "period", "amount")
    assert plain["findings"]["anomaly"] == "anomaly_confirmed"
    assert plain["findings"].get("anomaly_up") is True
    # the same members in the spike period: a business spike
    same = pdc_diag.detect_anomalies(_switch_frame(False), "period", "amount", dims=["BRANCH"])
    assert same["findings"]["anomaly"] == "anomaly_confirmed"


def test_detect_anomalies_dims_share_shift_is_scope_change():
    """Same member set, but one member's row share moves by more than 0.5."""
    parts = []
    for i, (period, total) in enumerate(zip(_iso(6), [10, 11, 10, 12, 100, 11])):
        members = ["A"] * 9 + ["B"] if i == 4 else ["A", "B"] * 5
        if i == 3:
            members = ["B"] * 9 + ["A"]
        parts.append(pd.DataFrame({"period": [period] * 10, "BRANCH": members,
                                   "amount": [total / 10] * 10}))
    df = pd.concat(parts, ignore_index=True)
    out = pdc_diag.detect_anomalies(df, "period", "amount", dims=["BRANCH"])
    assert out["values"]["anomaly_period"] == "2025-05"
    assert out["findings"]["anomaly"] == "scope_change"


@pytest.mark.parametrize("kwargs,param,column", [
    ({"rows_col": "ghost"}, "rows_col", "ghost"),
    ({"rows_col": "label"}, "rows_col", "label"),
    ({"dims": ["ghost"]}, "dims", "ghost"),
])
def test_detect_anomalies_rows_col_and_dims_errors(kwargs, param, column):
    df = pd.DataFrame({"period": _iso(4), "amount": [1.0, 2.0, 3.0, 4.0],
                       "label": ["a", "b", "c", "d"]})
    with pytest.raises(ValueError) as info:
        pdc_diag.detect_anomalies(df, "period", "amount", **kwargs)
    text = str(info.value)
    assert param in text, text
    assert column in text, text


def test_detect_anomalies_rows_col_not_numeric_message():
    df = pd.DataFrame({"period": _iso(4), "amount": [1.0, 2.0, 3.0, 4.0],
                       "label": ["a", "b", "c", "d"]})
    with pytest.raises(ValueError) as info:
        pdc_diag.detect_anomalies(df, "period", "amount", rows_col="label")
    assert "not numeric" in str(info.value)


# ===========================================================================
# decompose_change: no attribution on a scope change
# ===========================================================================
DECOMPOSE_COLUMNS = ["dim", "member", "from_value", "to_value", "delta", "share_of_delta",
                     "share_of_base"]


def _assert_scope_decompose(out):
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert f["change_shape"] == "scope_change"
    assert f["anomaly"] == "scope_change"
    assert "change_dim" not in f
    assert "unknown_dominated" not in f
    assert "unknown_dim" not in f
    for key in ("change_top_member", "change_top_member_share", "change_top_member_delta",
                "change_dim_label"):
        assert v[key] is None, key
    assert v["change_to_period"] == "2026 Март"
    assert v["change_from_period"] == "2026 Февраль"
    assert v["change_from_value"] == pytest.approx(BRB_NPL[3])
    assert v["change_to_value"] == pytest.approx(BRB_NPL[4])
    assert v["change_abs"] == pytest.approx(BRB_NPL[4] - BRB_NPL[3])
    assert f.get("change_up") is True
    assert f.get("change_large") is True
    assert len(t) == 0
    assert list(t.columns) == DECOMPOSE_COLUMNS
    assert t.attrs.get("pdc_from_period") == "2026 Февраль"
    assert t.attrs.get("pdc_to_period") == "2026 Март"
    assert t.attrs.get("pdc_metric") == "npl"
    assert "pdc_change_dim" not in t.attrs


def test_decompose_change_brb_raw_primary_anomaly_is_scope_change():
    df = _brb_raw(extra_dims=True)
    out = pdc_diag.decompose_change(df, "period", "npl", to_period="primary_anomaly",
                                    dims=["TYPE", "REGION", "SEKTOR"])
    _assert_scope_decompose(out)


def test_decompose_change_rows_col_pre_aggregated_is_scope_change():
    df = _brb_pre_aggregated()
    df["TYPE"] = "T1"
    out = pdc_diag.decompose_change(df, "period", "npl", to_period="primary_anomaly",
                                    dims=["TYPE"], rows_col="rows")
    _assert_scope_decompose(out)


def test_decompose_change_normal_spike_keeps_attribution():
    """Equal row counts: the primary-anomaly spike is attributed as today."""
    rows = []
    for period, a, b in zip(_iso(6), [5, 5, 5, 5, 90, 5], [5, 6, 5, 7, 10, 6]):
        for member, total in (("A", a), ("B", b)):
            rows.extend({"period": period, "region": member, "amount": total / 5}
                        for _ in range(5))
    df = pd.DataFrame(rows)
    out = pdc_diag.decompose_change(df, "period", "amount", to_period="primary_anomaly",
                                    dims=["region"])
    f, v = out["findings"], out["values"]
    assert f["change_shape"] != "scope_change"
    assert f.get("anomaly") != "scope_change"
    assert f["change_shape"] == "concentrated_in_one_dim"
    assert v["change_top_member"] == "A"
    assert v["change_to_period"] == "2025-05"


def test_decompose_change_rows_col_is_keyword_only():
    import inspect
    param = inspect.signature(pdc_diag.decompose_change).parameters.get("rows_col")
    assert param is not None
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is None


# ===========================================================================
# rate_by_period
# ===========================================================================
def _rate_frame(nums, dens, periods=None) -> pd.DataFrame:
    """Two rows per period splitting each period's numerator / denominator."""
    periods = periods or _iso(len(nums))
    rows = []
    for p, n, d in zip(periods, nums, dens):
        rows.append({"period": p, "npl": n * 0.25, "loan": d * 0.5})
        rows.append({"period": p, "npl": n * 0.75, "loan": d * 0.5})
    return pd.DataFrame(rows)


def test_rate_by_period_values_and_table():
    df = _rate_frame([50, 48, 47, 44], [1000, 1000, 1000, 1000])
    out = pdc_diag.rate_by_period(df, "period", "npl", "loan")
    _assert_shape(out)
    f, v, t = out["findings"], out["values"], out["table"]
    assert list(t.columns) == RATE_COLUMNS
    assert list(t["period"]) == _iso(4)
    assert list(t["numerator"]) == pytest.approx([50, 48, 47, 44])
    assert list(t["denominator"]) == pytest.approx([1000] * 4)
    assert list(t["rate"]) == pytest.approx([0.05, 0.048, 0.047, 0.044])
    assert t.attrs.get("pdc_numerator") == "npl"
    assert t.attrs.get("pdc_denominator") == "loan"
    assert set(v) == RATE_VALUE_KEYS
    assert v["rate_first"] == pytest.approx(0.05)
    assert v["rate_last"] == pytest.approx(0.044)
    assert v["rate_change_pp"] == pytest.approx(-0.6)
    assert v["rate_first_period"] == "2025-01"
    assert v["rate_last_period"] == "2025-04"
    assert v["period_from"] == "2025-01"
    assert v["period_to"] == "2025-04"
    assert v["n_periods"] == 4
    assert f.get("rate_down") is True
    assert "rate_up" not in f
    assert "too_few_periods" not in f
    assert set(f) <= pdc_diag.PDC_DIAG_FINDING_KEYS
    for value in v.values():
        assert value is None or isinstance(value, (bool, int, float, str)), type(value)


@pytest.mark.parametrize("last,expected", [
    (56, "rate_up"), (44, "rate_down"), (54, None), (46, None), (50, None)])
def test_rate_by_period_half_point_threshold(last, expected):
    out = pdc_diag.rate_by_period(_rate_frame([50, 52, last], [1000, 1000, 1000]),
                                  "period", "npl", "loan")
    f = out["findings"]
    if expected is None:
        assert "rate_up" not in f and "rate_down" not in f
    else:
        assert f.get(expected) is True
        other = "rate_down" if expected == "rate_up" else "rate_up"
        assert other not in f
    assert out["values"]["rate_change_pp"] == pytest.approx((last - 50) / 10.0)


def test_rate_by_period_zero_denominator_in_the_middle():
    out = pdc_diag.rate_by_period(_rate_frame([50, 7, 44], [1000, 0, 1000]),
                                  "period", "npl", "loan")
    t, v = out["table"], out["values"]
    assert math.isnan(float(t["rate"].iloc[1]))
    assert v["rate_first"] == pytest.approx(0.05)
    assert v["rate_last"] == pytest.approx(0.044)
    assert v["rate_first_period"] == "2025-01"
    assert v["rate_last_period"] == "2025-03"
    assert v["n_periods"] == 3


def test_rate_by_period_zero_denominator_first_period():
    out = pdc_diag.rate_by_period(_rate_frame([5, 50, 44], [0, 1000, 1000]),
                                  "period", "npl", "loan")
    v = out["values"]
    assert math.isnan(float(out["table"]["rate"].iloc[0]))
    assert v["rate_first"] == pytest.approx(0.05)
    assert v["rate_first_period"] == "2025-02"
    assert v["period_from"] == "2025-01"
    assert v["rate_change_pp"] == pytest.approx(-0.6)
    assert out["findings"].get("rate_down") is True


def test_rate_by_period_fewer_than_two_finite_rates():
    out = pdc_diag.rate_by_period(_rate_frame([5, 50, 3], [0, 1000, 0]),
                                  "period", "npl", "loan")
    f, v = out["findings"], out["values"]
    assert f.get("too_few_periods") is True
    assert "rate_up" not in f and "rate_down" not in f
    assert v["rate_change_pp"] is None
    assert v["rate_first"] == pytest.approx(0.05)
    assert v["rate_last"] == pytest.approx(0.05)
    assert v["rate_first_period"] == "2025-02"
    assert v["rate_last_period"] == "2025-02"
    assert v["n_periods"] == 3
    one = pdc_diag.rate_by_period(_rate_frame([5], [100]), "period", "npl", "loan")
    _assert_shape(one)
    assert one["findings"].get("too_few_periods") is True
    assert one["values"]["rate_change_pp"] is None
    none = pdc_diag.rate_by_period(_rate_frame([5, 6], [0, 0]), "period", "npl", "loan")
    assert none["findings"].get("too_few_periods") is True
    assert none["values"]["rate_first"] is None and none["values"]["rate_last"] is None


def test_rate_by_period_prefix_applies_to_values_only():
    df = _rate_frame([50, 48, 44], [1000, 1000, 1000])
    plain = pdc_diag.rate_by_period(df, "period", "npl", "loan")
    pref = pdc_diag.rate_by_period(df, "period", "npl", "loan", prefix="npl_")
    assert pref["findings"] == plain["findings"]
    assert set(pref["values"]) == {"npl_" + k for k in plain["values"]}
    assert pref["values"]["npl_rate_change_pp"] == pytest.approx(plain["values"]["rate_change_pp"])
    with pytest.raises(ValueError):
        pdc_diag.rate_by_period(df, "period", "npl", "loan", prefix="Bad")


def test_rate_by_period_missing_values_key_names_available_keys():
    out = pdc_diag.rate_by_period(_rate_frame([50, 48], [1000, 1000]), "period", "npl", "loan")
    with pytest.raises(KeyError) as info:
        out["values"]["rate_from"]
    text = str(info.value)
    assert "rate_by_period values" in text
    assert "rate_first" in text and "rate_change_pp" in text
    with pytest.raises(KeyError) as info:
        out["findings"]["npl_rate"]
    assert "rate_by_period findings" in str(info.value)


@pytest.mark.parametrize("args,param,column", [
    (("nope", "npl", "loan"), "period_col", "nope"),
    (("period", "ghost", "loan"), "numerator", "ghost"),
    (("period", "npl", "ghost"), "denominator", "ghost"),
    (("period", "label", "loan"), "numerator", "label"),
    (("period", "npl", "label"), "denominator", "label"),
])
def test_rate_by_period_value_errors(args, param, column):
    df = _rate_frame([50, 48], [1000, 1000])
    df["label"] = ["a", "b", "c", "d"]
    with pytest.raises(ValueError) as info:
        pdc_diag.rate_by_period(df, *args)
    text = str(info.value)
    assert param in text, text
    assert column in text, text


def test_rate_by_period_brb_npl_share_falls():
    rows = []
    for p, n, d in zip(BRB_PERIODS, BRB_NPL, BRB_LOAN):
        for share in (0.2, 0.3, 0.5):
            rows.append({"period": p, "NPL": n * share, "LOAN": d * share})
    df = pd.DataFrame(rows)
    out = pdc_diag.rate_by_period(df, "period", "NPL", "LOAN")
    f, v, t = out["findings"], out["values"], out["table"]
    assert list(t["period"]) == BRB_PERIODS
    assert v["rate_first"] == pytest.approx(4500.50e9 / 25.06e12)
    assert v["rate_last"] == pytest.approx(4739.29e9 / 62.96e12)
    assert v["rate_change_pp"] == pytest.approx((4739.29e9 / 62.96e12 - 4500.50e9 / 25.06e12) * 100)
    assert v["rate_first_period"] == "2025 Ноябрь"
    assert v["rate_last_period"] == "2026 Март"
    assert f.get("rate_down") is True
    assert "rate_up" not in f


# ===========================================================================
# coverage_check: the Unknown dim
# ===========================================================================
def _unknown_frame(shares: dict, n: int = 200, periods=None) -> pd.DataFrame:
    """`shares` = {dim: [Unknown share per period]}; REGION never Unknown."""
    periods = periods or BRB_PERIODS
    parts = []
    for i, period in enumerate(periods):
        part = {"period": [period] * n, "amount": [1.0] * n,
                "REGION": [("R1", "R2")[j % 2] for j in range(n)]}
        for dim, per_period in shares.items():
            k = int(round(per_period[i] * n))
            part[dim] = [UNKNOWN] * k + [f"{dim}_{j % 3}" for j in range(n - k)]
        parts.append(pd.DataFrame(part))
    return pd.concat(parts, ignore_index=True)


BRB_UNKNOWN = {"SEKTOR": [0.655, 0.715, 1.0, 1.0, 1.0],
               "SEGMENT": [0.257, 0.255, 0.449, 0.581, 0.420]}


@pytest.mark.parametrize("dims", [["SEGMENT", "SEKTOR", "REGION"],
                                  ["SEKTOR", "REGION", "SEGMENT"]])
def test_coverage_check_brb_unknown_dim_is_sektor(dims):
    out = pdc_diag.coverage_check(_unknown_frame(BRB_UNKNOWN), "period", dims, ["amount"])
    f, v = out["findings"], out["values"]
    assert f.get("unknown_dominated") is True
    assert f.get("unknown_dim") == "SEKTOR"
    assert v["unknown_dim_label"] == "SEKTOR"
    assert v["unknown_share_max"] == pytest.approx(1.0)


def test_coverage_check_unknown_dim_prefers_the_latest_period_share():
    """EARLY was 90 % Unknown in the first period only; LATE is 60 % Unknown
    in the latest period. The latest share decides."""
    shares = {"EARLY": [0.9, 0.0, 0.0, 0.0, 0.0], "LATE": [0.0, 0.0, 0.0, 0.0, 0.6]}
    out = pdc_diag.coverage_check(_unknown_frame(shares), "period", ["EARLY", "LATE"], ["amount"])
    f, v = out["findings"], out["values"]
    assert f.get("unknown_dim") == "LATE"
    assert v["unknown_dim_label"] == "LATE"
    assert v["unknown_share_max"] == pytest.approx(0.6)


def test_coverage_check_unknown_dim_only_earlier_candidate_is_chosen():
    shares = {"EARLY": [0.9, 0.0, 0.0, 0.0, 0.0], "LATE": [0.0, 0.0, 0.0, 0.0, 0.3]}
    out = pdc_diag.coverage_check(_unknown_frame(shares), "period", ["LATE", "EARLY"], ["amount"])
    f, v = out["findings"], out["values"]
    assert f.get("unknown_dominated") is True
    assert f.get("unknown_dim") == "EARLY"
    assert v["unknown_dim_label"] == "EARLY"
    assert v["unknown_share_max"] == pytest.approx(0.9)


def test_coverage_check_unknown_no_candidate():
    shares = {"A": [0.1, 0.3, 0.0, 0.0, 0.2], "B": [0.0, 0.0, 0.0, 0.0, 0.25]}
    out = pdc_diag.coverage_check(_unknown_frame(shares), "period", ["A", "B"], ["amount"])
    f, v = out["findings"], out["values"]
    assert "unknown_dominated" not in f and "unknown_dim" not in f
    assert v["unknown_dim_label"] is None
    assert v["unknown_share_max"] == pytest.approx(0.3)
    none = pdc_diag.coverage_check(_unknown_frame({}), "period", [], ["amount"])
    assert none["values"]["unknown_share_max"] == 0.0


# ===========================================================================
# coverage_check: a unit label that disappears
# ===========================================================================
def _currency_frame(per_period: list, periods=None) -> pd.DataFrame:
    """`per_period[i]` = {currency label or None: row count}."""
    periods = periods or BRB_PERIODS
    rows = []
    for period, counts in zip(periods, per_period):
        for i, (label, k) in enumerate(counts.items()):
            rows.extend({"period": period, "REGION": ("R1", "R2")[j % 2], "CURRENCY": label,
                         "amount": 1.0} for j in range(k))
    return pd.DataFrame(rows)


def test_coverage_check_brb_uzs_disappears_after_december():
    both = {UZS: 5, OTHER_CCY: 10}
    other = {OTHER_CCY: 10}
    df = _currency_frame([both, both, other, other, other])
    out = pdc_diag.coverage_check(df, "period", ["REGION"], ["amount"], unit_cols=["CURRENCY"])
    _assert_shape(out)
    f, v = out["findings"], out["values"]
    assert f.get("label_disappeared") is True
    assert v["disappeared_label"] == UZS
    assert v["disappeared_after_period"] == "2025 Декабрь"


def test_coverage_check_label_that_returns_is_not_disappeared():
    both = {UZS: 5, OTHER_CCY: 10}
    other = {OTHER_CCY: 10}
    df = _currency_frame([both, both, other, other, both])
    out = pdc_diag.coverage_check(df, "period", ["REGION"], ["amount"], unit_cols=["CURRENCY"])
    assert "label_disappeared" not in out["findings"]
    assert out["values"]["disappeared_label"] is None
    assert out["values"]["disappeared_after_period"] is None


def test_coverage_check_unit_column_empty_later_is_not_disappeared():
    both = {UZS: 5, OTHER_CCY: 10}
    empty = {None: 10}
    df = _currency_frame([both, both, empty, empty, empty])
    out = pdc_diag.coverage_check(df, "period", ["REGION"], ["amount"], unit_cols=["CURRENCY"])
    assert "label_disappeared" not in out["findings"]
    assert out["values"]["disappeared_label"] is None


def test_coverage_check_two_disappearing_labels_more_rows_wins():
    periods = _iso(4)
    df = _currency_frame([{"X": 3, "Y": 10, "Z": 4}, {"X": 3, "Z": 4}, {"Z": 4}, {"Z": 4}],
                         periods=periods)
    out = pdc_diag.coverage_check(df, "period", ["REGION"], ["amount"], unit_cols=["CURRENCY"])
    f, v = out["findings"], out["values"]
    assert f.get("label_disappeared") is True
    assert v["disappeared_label"] == "Y"
    assert v["disappeared_after_period"] == "2025-01"


def test_coverage_check_disappearing_label_tie_first_appearance():
    periods = _iso(3)
    # X and Y both hold 6 rows and both end early; X appears first
    df = _currency_frame([{"X": 3, "Y": 6, "Z": 2}, {"X": 3, "Z": 2}, {"Z": 2}],
                         periods=periods)
    out = pdc_diag.coverage_check(df, "period", ["REGION"], ["amount"], unit_cols=["CURRENCY"])
    assert out["values"]["disappeared_label"] == "X"
    assert out["values"]["disappeared_after_period"] == "2025-02"


def test_coverage_check_disappearance_needs_unit_cols():
    both = {UZS: 5, OTHER_CCY: 10}
    other = {OTHER_CCY: 10}
    df = _currency_frame([both, both, other, other, other])
    out = pdc_diag.coverage_check(df, "period", ["REGION", "CURRENCY"], ["amount"])
    assert "label_disappeared" not in out["findings"]
    assert "disappeared_label" in out["values"]
    assert out["values"]["disappeared_label"] is None
    assert out["values"]["disappeared_after_period"] is None


def test_coverage_check_disappearance_values_prefixed():
    both = {UZS: 5, OTHER_CCY: 10}
    other = {OTHER_CCY: 10}
    df = _currency_frame([both, both, other, other, other])
    out = pdc_diag.coverage_check(df, "period", ["REGION"], ["amount"], unit_cols=["CURRENCY"],
                                  prefix="cov_")
    assert out["values"]["cov_disappeared_label"] == UZS
    assert out["findings"].get("label_disappeared") is True
