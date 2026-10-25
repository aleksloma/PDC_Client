"""Narrative follow-up, item 4 — the COLLAPSE rule of `pdc_diag._data_gaps`
applies only to a pre-aggregated frame (DIAGNOSTICS_SPEC B8.4).

The collapse rule (a value below COLLAPSE_RATIO x every neighbour) exists for
the one-row-per-period frame, where the row-count rule cannot fire. On RAW
rows (several rows per period) the row-count rule is the gap signal: a period
with a normal row count whose value collapsed is a real business change, not
a data gap. So the collapse rule now runs only when max(rows per period) <= 1.
The zero rule (an exact 0 between non-zero neighbours) is unchanged on raw
rows — pinned by tests/test_pdc_diag.py::
test_detect_anomalies_zero_between_nonzero_neighbours_is_a_data_gap — and the
BRB raw / aggregated tests stay green (raw February is thin by rows).
"""
import pandas as pd

import pdc_diag

PERIODS = [f"2025-{m:02d}" for m in range(1, 6)]
COLLAPSED = [100, 100, 0.5, 100, 100]


def _counted_frame(periods, row_counts, totals, metric="amount") -> pd.DataFrame:
    """Each period has `row_counts[i]` rows whose metric sums to `totals[i]`
    (the tests/test_pdc_diag.py helper)."""
    parts = [pd.DataFrame({"period": [p] * n, metric: [t / n] * n})
             for p, n, t in zip(periods, row_counts, totals)]
    return pd.concat(parts, ignore_index=True)


def _gap_flags(table) -> list:
    return [bool(g) for g in table["is_data_gap"]]


def test_data_gaps_collapse_only_with_one_row_per_period():
    # one row per period: the collapse rule fires
    assert pdc_diag._data_gaps(COLLAPSED, [1] * 5, pdc_diag.THIN_PERIOD_RATIO) == [2]
    # raw rows with normal counts: the collapsed value is no data gap
    assert pdc_diag._data_gaps(COLLAPSED, [10] * 5, pdc_diag.THIN_PERIOD_RATIO) == []
    # a single raw period with 2 rows is enough to switch the rule off
    assert pdc_diag._data_gaps(COLLAPSED, [1, 1, 1, 2, 1], pdc_diag.THIN_PERIOD_RATIO) == []


def test_data_gaps_zero_rule_unchanged_on_raw_rows():
    assert pdc_diag._data_gaps([100, 100, 0, 100, 100], [10] * 5,
                               pdc_diag.THIN_PERIOD_RATIO) == [2]


def test_data_gaps_row_rule_unchanged_on_raw_rows():
    # a thin period is a gap whatever its value
    assert pdc_diag._data_gaps([100, 100, 0.5, 100, 100], [10, 10, 2, 10, 10],
                               pdc_diag.THIN_PERIOD_RATIO) == [2]


def test_detect_anomalies_raw_rows_collapse_is_not_a_data_gap():
    df = _counted_frame(PERIODS, [10] * 5, COLLAPSED)
    out = pdc_diag.detect_anomalies(df, "period", "amount")
    t = out["table"]
    assert [int(x) for x in t["rows"]] == [10] * 5
    assert _gap_flags(t) == [False] * 5
    assert out["findings"].get("period_status") != "data_gap"
    assert out["values"]["data_gap_period"] is None


def test_detect_anomalies_one_row_per_period_collapse_is_a_data_gap():
    df = pd.DataFrame({"period": PERIODS, "amount": [float(x) for x in COLLAPSED]})
    out = pdc_diag.detect_anomalies(df, "period", "amount")
    assert [int(x) for x in out["table"]["rows"]] == [1] * 5
    assert _gap_flags(out["table"]) == [False, False, True, False, False]
    assert out["findings"].get("period_status") == "data_gap"
    assert out["values"]["data_gap_period"] == "2025-03"
