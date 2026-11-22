"""`pdc_diag` period-column guard (C1, F13 item 6).

A raw timestamp passed as `period_col` made `coverage_check` take 81 s and
run out of memory in the 2 GB sandbox (189,018 distinct values instead of 31
days). Every public function that takes `period_col` — `coverage_check`,
`detect_anomalies`, `decompose_change`, `rate_by_period` — now counts the
distinct NON-NULL values of `df[period_col]` right after its column checks
and, above `pdc_diag.MAX_PERIODS`, raises
`ValueError("period_col: column <name> has <n> distinct values; ...")`.
`order=` does not bypass it.

`MAX_PERIODS` is read from the module (its value is chosen by measurement in
the implementation). Frames hold ONE row per period so the at-the-limit
calls stay fast. Offline, invented numbers.
"""
import re
import time

import numpy as np
import pandas as pd
import pytest

import pdc_diag

FUNCS = ("coverage_check", "detect_anomalies", "decompose_change", "rate_by_period")


def _limit() -> int:
    limit = getattr(pdc_diag, "MAX_PERIODS", None)
    assert isinstance(limit, int) and not isinstance(limit, bool), \
        "pdc_diag.MAX_PERIODS (an int) is missing"
    assert limit >= 366, limit            # a year of daily data must always pass
    return limit


def _frame(n_periods: int, *, start="2000-01-01", freq="D") -> pd.DataFrame:
    """One row per period: a datetime period column, a 2-member dim, a
    positive metric and a positive denominator."""
    days = pd.date_range(start, periods=n_periods, freq=freq)
    return pd.DataFrame({
        "booked_at": days,
        "region": np.where(np.arange(n_periods) % 2 == 0, "North", "South"),
        "amount": np.arange(n_periods, dtype=float) % 97.0 + 10.0,
        "loans": np.full(n_periods, 4.0),
    })


def _call(name: str, df: pd.DataFrame, **kw):
    """Each function with valid arguments apart from the period count."""
    if name == "coverage_check":
        return pdc_diag.coverage_check(df, "booked_at", ["region"], ["amount"], **kw)
    if name == "detect_anomalies":
        return pdc_diag.detect_anomalies(df, "booked_at", "amount", **kw)
    if name == "decompose_change":
        return pdc_diag.decompose_change(df, "booked_at", "amount", dims=["region"], **kw)
    if name == "rate_by_period":
        return pdc_diag.rate_by_period(df, "booked_at", "amount", "loans", **kw)
    raise AssertionError(name)


def _count_texts(n: int) -> tuple:
    return (str(n), f"{n:,}", f"{n:_}")


def _assert_guard_message(msg: str, n: int, column: str = "booked_at"):
    assert msg.startswith("period_col:"), msg
    assert any(t in msg for t in _count_texts(n)), msg          # names the count
    assert column in msg, msg                                   # names the column
    assert any(t in msg for t in _count_texts(_limit())), msg   # names the limit
    # the advice survives the retry scrub (not only inside a quoted literal)
    assert "dt.date" in msg or "to_period" in msg, msg
    # Article II: the message reaches the brain's retry prompt — no cell value
    assert "2000-01-01" not in msg and "2024-03-01" not in msg, msg
    assert "North" not in msg, msg


def test_max_periods_is_a_module_int():
    _limit()


# ------------------------------------------------------------- the limit passes

@pytest.mark.parametrize("name", FUNCS)
def test_exactly_max_periods_passes(name):
    limit = _limit()
    df = _frame(limit)
    assert df["booked_at"].nunique() == limit
    out = _call(name, df)
    assert set(out) == {"table", "values", "findings"}, sorted(out)
    if name in ("coverage_check", "rate_by_period"):
        assert out["values"]["n_periods"] == limit, out["values"]["n_periods"]
    if name == "detect_anomalies":
        assert len(out["table"]) == limit


@pytest.mark.parametrize("name", ["coverage_check", "rate_by_period"])
def test_nulls_are_not_counted_as_periods(name):
    limit = _limit()
    df = _frame(limit)
    gaps = df.head(3).copy()
    gaps["booked_at"] = pd.NaT
    df = pd.concat([df, gaps], ignore_index=True)
    assert df["booked_at"].nunique(dropna=False) == limit + 1
    out = _call(name, df)
    assert out["values"]["n_periods"] == limit


# ------------------------------------------------------------- limit + 1 fails

@pytest.mark.parametrize("name", FUNCS)
def test_max_periods_plus_one_raises(name):
    limit = _limit()
    df = _frame(limit + 1)
    with pytest.raises(ValueError) as err:
        _call(name, df)
    _assert_guard_message(str(err.value), limit + 1)


@pytest.mark.parametrize("name", FUNCS)
def test_order_argument_does_not_bypass_the_guard(name):
    limit = _limit()
    df = _frame(limit + 1)
    full_order = list(df["booked_at"])
    with pytest.raises(ValueError) as err:
        _call(name, df, order=full_order)
    _assert_guard_message(str(err.value), limit + 1)
    # an order naming only a few periods does not bypass it either
    with pytest.raises(ValueError) as err:
        _call(name, df, order=full_order[:3])
    _assert_guard_message(str(err.value), limit + 1)


def test_guard_message_advises_to_period_astype_str_and_dt_date():
    # C1 follow-up: a bare `.dt.to_period(...)` column holds pd.Period objects;
    # the advice names the `.astype(str)` step right after it, and `.dt.date`
    # for days
    limit = _limit()
    df = _frame(limit + 1)
    with pytest.raises(ValueError) as err:
        pdc_diag.coverage_check(df, "booked_at", ["region"], ["amount"])
    msg = str(err.value)
    assert ".astype(str)" in msg, msg
    assert re.search(r"to_period\([^)]{0,20}\)\.astype\(str\)", msg), msg
    assert "dt.date" in msg, msg


def test_period_dtype_column_over_the_limit_is_refused():
    limit = _limit()
    df = _frame(limit + 1)
    df["booked_at"] = df["booked_at"].dt.to_period("D")
    assert isinstance(df["booked_at"].dtype, pd.PeriodDtype)
    with pytest.raises(ValueError) as err:
        pdc_diag.rate_by_period(df, "booked_at", "amount", "loans")
    _assert_guard_message(str(err.value), limit + 1)


def test_string_period_labels_are_counted_too():
    limit = _limit()
    df = _frame(limit + 1)
    df["booked_at"] = [f"P{i:06d}" for i in range(limit + 1)]
    with pytest.raises(ValueError) as err:
        pdc_diag.rate_by_period(df, "booked_at", "amount", "loans")
    _assert_guard_message(str(err.value), limit + 1)


def test_decompose_change_primary_anomaly_is_guarded():
    limit = _limit()
    df = _frame(limit + 1)
    with pytest.raises(ValueError) as err:
        pdc_diag.decompose_change(df, "booked_at", "amount", to_period="primary_anomaly",
                                  dims=["region"])
    _assert_guard_message(str(err.value), limit + 1)


def test_column_checks_still_come_first():
    # the guard runs right AFTER the existing column checks: a missing column
    # still answers its own error on a frame that is also over the limit
    limit = _limit()
    df = _frame(limit + 1)
    with pytest.raises(ValueError) as err:
        pdc_diag.coverage_check(df, "booked_at", ["region"], ["no_such_metric"])
    assert str(err.value).startswith("metrics:"), str(err.value)
    with pytest.raises(ValueError) as err:
        pdc_diag.coverage_check(df, "no_such_period", ["region"], ["amount"])
    assert str(err.value).startswith("period_col:"), str(err.value)
    assert "not found" in str(err.value), str(err.value)


# ------------------------------------------------- a raw timestamp is refused fast

@pytest.mark.parametrize("name", FUNCS)
def test_raw_timestamp_column_is_refused_quickly(name):
    limit = _limit()
    n_rows = max(3000, limit + 1000)
    # event timestamps a few seconds apart: every row its own "period"
    df = _frame(n_rows, start="2024-03-01 08:00:00", freq="7s")
    assert df["booked_at"].nunique() == n_rows
    started = time.perf_counter()
    with pytest.raises(ValueError) as err:
        _call(name, df)
    elapsed = time.perf_counter() - started
    _assert_guard_message(str(err.value), n_rows)
    assert elapsed < 2.0, elapsed
