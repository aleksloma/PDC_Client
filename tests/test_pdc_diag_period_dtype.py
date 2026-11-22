"""`pdc_diag` with a `pd.Period` period column (C1 follow-up).

`df["ts"].dt.to_period("M")` WITHOUT `.astype(str)` gives a PeriodDtype
column. Before the fix pdc_diag ordered it by first appearance (and set
`period_order_unknown`), and every period VALUE came back None (`_label_str`
could not turn a Period into text). The contract pinned here:

* a Period column is ordered chronologically by period start;
* its labels are `str(period)` ("2023-05", "2024-03-04",
  "2024-01-01/2024-01-07");
* for freq M and D the four functions give the SAME table order, VALUES and
  FINDINGS as the equivalent `.astype(str)` column; for freq W (whose string
  labels "a/b" pdc_diag cannot parse back into dates) the order and VALUES
  are those of the chronological `str(period)` labels;
* never `period_order_unknown`.

Rows are SHUFFLED so first appearance is never chronological. Offline,
invented numbers.
"""
import numpy as np
import pandas as pd
import pytest

import pdc_diag

N_PERIODS = 8
SPIKE = 5                       # the period index holding the planted spike
_START = {"M": ("2023-01-01", "MS"), "D": ("2024-03-01", "D"), "W": ("2024-01-01", "W-MON")}


def _frame(freq: str) -> pd.DataFrame:
    """N_PERIODS periods x 4 rows, one planted spike, rows shuffled; `per` is
    the Period column, `lbl` its `.astype(str)` twin."""
    start, step = _START[freq]
    starts = pd.date_range(start, periods=N_PERIODS, freq=step)
    rows = []
    for i, stamp in enumerate(starts):
        for r in range(4):
            rows.append({
                "ts": stamp + pd.Timedelta(hours=r),
                "region": ("North", "South")[r % 2],
                "amount": 100.0 + 3.0 * i + r + (900.0 if i == SPIKE else 0.0),
                "loans": 4.0 + r + (i % 3),
            })
    df = pd.DataFrame(rows).sample(frac=1, random_state=7).reset_index(drop=True)
    df["per"] = df["ts"].dt.to_period(freq)
    df["lbl"] = df["per"].astype(str)
    assert isinstance(df["per"].dtype, pd.PeriodDtype)
    # the shuffle really scrambles first appearance
    first_seen = list(pd.unique(df["per"]))
    assert first_seen != sorted(first_seen)
    return df


def _expected_labels(df: pd.DataFrame) -> list:
    return [str(p) for p in sorted(pd.unique(df["per"]))]


def _labels_of(table: pd.DataFrame) -> list:
    return [str(v) for v in table["period"].tolist()]


def _norm(table: pd.DataFrame) -> pd.DataFrame:
    out = table.reset_index(drop=True).copy()
    if "period" in out.columns:
        out["period"] = out["period"].map(str)
    return out


def _call(name: str, df: pd.DataFrame, col: str, **kw):
    if name == "coverage_check":
        return pdc_diag.coverage_check(df, col, ["region"], ["amount"], **kw)
    if name == "detect_anomalies":
        return pdc_diag.detect_anomalies(df, col, "amount", **kw)
    if name == "decompose_change":
        return pdc_diag.decompose_change(df, col, "amount", dims=["region"], **kw)
    if name == "rate_by_period":
        return pdc_diag.rate_by_period(df, col, "amount", "loans", **kw)
    raise AssertionError(name)


FUNCS = ("coverage_check", "detect_anomalies", "decompose_change", "rate_by_period")


# ------------------------------------------- M and D: identical to .astype(str)

@pytest.mark.parametrize("freq", ["M", "D"])
@pytest.mark.parametrize("name", FUNCS)
def test_period_column_matches_its_astype_str_twin(name, freq):
    df = _frame(freq)
    by_period = _call(name, df, "per")
    by_text = _call(name, df, "lbl")
    assert not by_period["findings"].get("period_order_unknown"), by_period["findings"]
    assert not by_text["findings"].get("period_order_unknown"), by_text["findings"]
    assert by_period["values"] == by_text["values"]
    assert by_period["findings"] == by_text["findings"]
    pd.testing.assert_frame_equal(_norm(by_period["table"]), _norm(by_text["table"]))


# ------------------------------------------------ every freq: chronological

@pytest.mark.parametrize("freq", ["M", "D", "W"])
def test_rate_by_period_orders_periods_and_fills_values(freq):
    df = _frame(freq)
    labels = _expected_labels(df)
    out = pdc_diag.rate_by_period(df, "per", "amount", "loans")
    assert _labels_of(out["table"]) == labels
    v = out["values"]
    assert v["period_from"] == labels[0] and v["period_to"] == labels[-1], v
    assert v["rate_first_period"] == labels[0], v
    assert v["rate_last_period"] == labels[-1], v
    assert v["n_periods"] == N_PERIODS
    assert not out["findings"].get("period_order_unknown")


@pytest.mark.parametrize("freq", ["M", "D", "W"])
def test_coverage_check_orders_periods_and_fills_values(freq):
    df = _frame(freq)
    labels = _expected_labels(df)
    out = pdc_diag.coverage_check(df, "per", ["region"], ["amount"])
    assert _labels_of(out["table"]) == labels
    v = out["values"]
    assert v["first_period"] == labels[0] and v["latest_period"] == labels[-1], v
    assert v["period_from"] == labels[0] and v["period_to"] == labels[-1], v
    assert v["n_periods"] == N_PERIODS
    assert not out["findings"].get("period_order_unknown"), out["findings"]


@pytest.mark.parametrize("freq", ["M", "D", "W"])
def test_detect_anomalies_names_the_spike_period(freq):
    df = _frame(freq)
    labels = _expected_labels(df)
    out = pdc_diag.detect_anomalies(df, "per", "amount")
    assert _labels_of(out["table"]) == labels
    assert out["findings"].get("anomaly") == "anomaly_confirmed", out["findings"]
    assert out["values"]["anomaly_period"] == labels[SPIKE], out["values"]
    assert not out["findings"].get("period_order_unknown")


@pytest.mark.parametrize("freq", ["M", "D", "W"])
def test_decompose_change_default_is_last_two_periods(freq):
    df = _frame(freq)
    labels = _expected_labels(df)
    out = pdc_diag.decompose_change(df, "per", "amount", dims=["region"])
    v = out["values"]
    assert v["change_from_period"] == labels[-2], v
    assert v["change_to_period"] == labels[-1], v
    assert v["change_from_value"] is not None and v["change_to_value"] is not None


@pytest.mark.parametrize("freq", ["M", "D", "W"])
def test_decompose_change_primary_anomaly(freq):
    df = _frame(freq)
    labels = _expected_labels(df)
    out = pdc_diag.decompose_change(df, "per", "amount", to_period="primary_anomaly",
                                    dims=["region"])
    v = out["values"]
    assert v["change_to_period"] == labels[SPIKE], v
    assert v["change_from_period"] == labels[SPIKE - 1], v
    if freq in ("M", "D"):
        twin = pdc_diag.decompose_change(df, "lbl", "amount", to_period="primary_anomaly",
                                         dims=["region"])
        assert v == twin["values"]
        assert out["findings"] == twin["findings"]


@pytest.mark.parametrize("freq", ["M", "D", "W"])
@pytest.mark.parametrize("as_period", [False, True], ids=["str_args", "period_args"])
def test_decompose_change_explicit_periods(freq, as_period):
    df = _frame(freq)
    periods = sorted(pd.unique(df["per"]))
    labels = [str(p) for p in periods]
    frm, to = (periods[1], periods[6]) if as_period else (labels[1], labels[6])
    out = pdc_diag.decompose_change(df, "per", "amount", from_period=frm, to_period=to,
                                    dims=["region"])
    v = out["values"]
    assert v["change_from_period"] == labels[1], v
    assert v["change_to_period"] == labels[6], v
    expected_from = float(df.loc[df["per"] == periods[1], "amount"].sum())
    expected_to = float(df.loc[df["per"] == periods[6], "amount"].sum())
    assert v["change_from_value"] == pytest.approx(expected_from)
    assert v["change_to_value"] == pytest.approx(expected_to)
    if freq in ("M", "D"):
        twin = pdc_diag.decompose_change(df, "lbl", "amount", from_period=labels[1],
                                         to_period=labels[6], dims=["region"])
        assert v == twin["values"]
        pd.testing.assert_frame_equal(_norm(out["table"]), _norm(twin["table"]))


def test_period_values_are_plain_strings():
    # VALUES cross to the narrative / brain as plain text, never Period objects
    df = _frame("M")
    for name in FUNCS:
        out = _call(name, df, "per")
        for key, value in out["values"].items():
            assert not isinstance(value, pd.Period), (name, key, value)
