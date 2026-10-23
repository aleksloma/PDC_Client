"""dataset_profile.compute_profile — pure units (Prompt 13 Part A).

Pins the profile shape the brain's injection relies on: table stats,
per-column flags, bounded grain detection, deterministic warnings, sampling,
and JSON round-trip through local_store._json_safe.
"""
import json

import numpy as np
import pandas as pd
import pytest

import dataset_profile as dp
import local_store


def test_constant_column_flag_and_warning():
    df = pd.DataFrame({"Quantity": [1, 1, 1, 1], "city": ["a", "b", "c", "d"]})
    prof = dp.compute_profile(df)
    assert prof["columns"]["Quantity"]["constant"] is True
    assert prof["columns"]["city"]["constant"] is False
    assert "Quantity is constant: every value = 1" in prof["warnings"]


def test_null_pct_warning_over_30():
    df = pd.DataFrame({"x": [1, None, None, None], "y": [1, 2, 3, 4]})
    prof = dp.compute_profile(df)
    assert prof["columns"]["x"]["null_pct"] == 75.0
    assert any(w.startswith("x is 75.0% empty") for w in prof["warnings"])
    assert not any(w.startswith("y is") for w in prof["warnings"])


def test_duplicates_and_all_rows_duplicated_warning():
    df = pd.DataFrame({"a": [1, 1, 2, 2], "b": ["x", "x", "y", "y"]})
    prof = dp.compute_profile(df)
    assert prof["duplicate_row_count"] == 2          # keep='first' semantics
    assert "All rows are duplicated at least once" in prof["warnings"]
    df2 = pd.DataFrame({"a": [1, 1, 2], "b": ["x", "x", "y"]})
    assert "All rows are duplicated at least once" not in dp.compute_profile(df2)["warnings"]


def test_all_unique_flag_and_min_max_scope():
    ts = pd.to_datetime(["2026-01-01", "2026-02-01", "2026-03-01"])
    df = pd.DataFrame({"id": [1, 2, 3], "when": ts, "name": ["a", "b", "c"]})
    prof = dp.compute_profile(df)
    assert prof["columns"]["id"]["all_unique"] is True
    assert prof["columns"]["id"]["min"] == 1 and prof["columns"]["id"]["max"] == 3
    assert "min" in prof["columns"]["when"] and "max" in prof["columns"]["when"]
    assert "min" not in prof["columns"]["name"]      # text: no min/max


def test_top_values_cap_and_truncation():
    long_val = "v" * 100
    df = pd.DataFrame({"c": [long_val] * 3 + ["a", "b", "d", "e", "f", "g"]})
    prof = dp.compute_profile(df)
    tv = prof["columns"]["c"]["top_values"]
    assert len(tv) <= dp.TOP_VALUES_N
    assert tv[0][0] == "v" * dp.TOP_VALUE_MAXLEN and tv[0][1] == 3
    # high-cardinality numeric column gets no top_values
    dfn = pd.DataFrame({"n": range(100)})
    assert "top_values" not in dp.compute_profile(dfn)["columns"]["n"]


def test_single_column_grain():
    df = pd.DataFrame({"order_id": [10, 11, 12], "v": [1, 1, 2]})
    grain = dp.compute_profile(df)["grain"]
    assert grain == {"columns": ["order_id"], "kind": "single",
                     "text": "one row per (order_id)"}


def test_pair_grain_key_like_only_and_catalog_warning():
    # 3 products × 4 cities link table: every Quantity = 1
    rows = [(p, c) for p in (1, 2, 3) for c in (10, 20, 30, 40)]
    df = pd.DataFrame({"product_id": [r[0] for r in rows],
                       "city_id": [r[1] for r in rows],
                       "Quantity": [1] * len(rows)})
    prof = dp.compute_profile(df)
    assert prof["grain"]["kind"] == "pair"
    assert set(prof["grain"]["columns"]) == {"product_id", "city_id"}
    assert any(w.startswith("looks like a catalog/link table: one row per (")
               and "no varying numeric measure" in w for w in prof["warnings"])


def test_pair_grain_not_from_non_key_columns():
    # unique pair exists but neither column is key-like (no id/code suffix,
    # not low-cardinality categorical) → no pair grain claimed
    df = pd.DataFrame({"m1": [1.5, 2.5, 1.5, 2.5], "m2": [1, 1, 2, 2]})
    assert dp.compute_profile(df)["grain"] is None


def test_pair_scan_skipped_over_row_threshold(monkeypatch):
    monkeypatch.setattr(dp, "PAIR_SCAN_MAX_ROWS", 3)
    rows = [(p, c) for p in (1, 2) for c in (10, 20)]
    df = pd.DataFrame({"product_id": [r[0] for r in rows],
                       "city_id": [r[1] for r in rows]})
    assert dp.compute_profile(df)["grain"] is None   # 4 rows > cap 3


def test_candidate_cap_respected(monkeypatch):
    monkeypatch.setattr(dp, "MAX_GRAIN_CANDIDATE_COLS", 1)
    rows = [(p, c) for p in (1, 2) for c in (10, 20)]
    df = pd.DataFrame({"product_id": [r[0] for r in rows],
                       "city_id": [r[1] for r in rows]})
    # only one candidate survives the cap → no pair to test
    assert dp.compute_profile(df)["grain"] is None


def test_warnings_capped_at_six():
    df = pd.DataFrame({f"k{i}": [7] * 3 for i in range(10)})
    assert len(dp.compute_profile(df)["warnings"]) == dp.MAX_WARNINGS


def test_sampling_flag_keeps_true_row_count(monkeypatch):
    monkeypatch.setattr(dp, "SAMPLE_THRESHOLD", 10)
    monkeypatch.setattr(dp, "SAMPLE_ROWS", 5)
    df = pd.DataFrame({"n": range(20)})
    prof = dp.compute_profile(df)
    assert prof["sampled"] is True
    assert prof["rows"] == 20                        # TRUE count
    assert prof["columns"]["n"]["nunique"] == 5      # stats on the sample


def test_profile_json_round_trips_through_json_safe():
    df = pd.DataFrame({
        "id": np.arange(3, dtype=np.int64),
        "when": pd.to_datetime(["2026-01-01", None, "2026-03-01"]),
        "val": [1.5, np.nan, 2.5],
    })
    payload = json.dumps(local_store._json_safe(dp.compute_profile(df)))
    assert isinstance(json.loads(payload), dict)


def test_total_rows_marks_a_sample_with_the_true_count():
    """A live table is profiled from a bounded sample: `total_rows` carries
    the true row count, and the profile says it is sampled."""
    df = pd.DataFrame({"n": range(5), "s": list("abcde")})
    prof = dp.compute_profile(df, total_rows=1000)
    assert prof["rows"] == 1000
    assert prof["sampled"] is True
    assert prof["columns"]["n"]["nunique"] == 5      # stats on the sample


def test_without_total_rows_nothing_changes():
    df = pd.DataFrame({"n": range(5)})
    prof = dp.compute_profile(df)
    assert prof["rows"] == 5
    assert prof["sampled"] is False


# ---------------------------------------------------------------------------
# Narrative session 1 (DIAGNOSTICS_SPEC / plan C3, prompt item 7): the
# "stock, not a flow" warning — two numeric columns equal in more than 90% of
# the rows. Names and an integer percent only; never a value.
# ---------------------------------------------------------------------------

def _stock_flow_frame(n_equal: int, n: int = 100) -> pd.DataFrame:
    base = np.arange(1, n + 1, dtype=np.int64) * 3
    other = base.copy()
    other[n_equal:] = other[n_equal:] + 1000          # the rows that differ
    return pd.DataFrame({"GRANTED_LOAN_COUNT": base, "LOAN_COUNT": other,
                         "label": [f"l{i}" for i in range(n)]})


def test_stock_not_flow_warning():
    prof = dp.compute_profile(_stock_flow_frame(94))
    expected = "GRANTED_LOAN_COUNT equals LOAN_COUNT in 94% of rows: likely a stock, not a flow"
    assert expected in prof["warnings"]
    equal_warnings = [w for w in prof["warnings"] if "equals" in w]
    assert equal_warnings == [expected]
    # names + the integer percent only: no cell value of either column leaks
    blob = json.dumps(equal_warnings)
    for v in (3, 300, 1300):
        assert f" {v} " not in blob and f"= {v}" not in blob


def test_stock_not_flow_requires_more_than_90_percent():
    assert not any("equals" in w for w in dp.compute_profile(_stock_flow_frame(90))["warnings"])
    assert any("equals" in w for w in dp.compute_profile(_stock_flow_frame(91))["warnings"])


def test_stock_not_flow_skips_constant_and_non_numeric_columns():
    df = pd.DataFrame({"a": [7] * 20, "b": [7] * 20,                 # constant pair: skipped
                       "s": [str(i) for i in range(20)],
                       "t": [str(i) for i in range(20)]})            # text pair: never compared
    assert not any("equals" in w for w in dp.compute_profile(df)["warnings"])


def test_profile_without_equal_columns_byte_identical():
    """A frame with no near-equal numeric pair yields the SAME profile as
    before the pair scan existed (the expectation below was captured from the
    pre-change `compute_profile`; `computed_at` is the only volatile key)."""
    df = pd.DataFrame({"a": [1, 2, 3, 4], "b": [10, 20, 30, 40],
                       "name": ["x", "y", "z", "w"]})
    prof = local_store._json_safe(dp.compute_profile(df))
    assert isinstance(prof.pop("computed_at"), str)
    expected = json.loads(
        '{"columns": {"a": {"all_unique": true, "constant": false, "dtype": "int64", '
        '"max": 4, "min": 1, "null_count": 0, "null_pct": 0.0, "nunique": 4, '
        '"top_values": [["1", 1], ["2", 1], ["3", 1], ["4", 1]]}, '
        '"b": {"all_unique": true, "constant": false, "dtype": "int64", "max": 40, '
        '"min": 10, "null_count": 0, "null_pct": 0.0, "nunique": 4, '
        '"top_values": [["10", 1], ["20", 1], ["30", 1], ["40", 1]]}, '
        '"name": {"all_unique": true, "constant": false, "dtype": "object", '
        '"null_count": 0, "null_pct": 0.0, "nunique": 4, '
        '"top_values": [["x", 1], ["y", 1], ["z", 1], ["w", 1]]}}, '
        '"duplicate_row_count": 0, '
        '"grain": {"columns": ["a"], "kind": "single", "text": "one row per (a)"}, '
        '"profile_version": 1, "rows": 4, "sampled": false, "warnings": []}')
    assert json.dumps(prof, sort_keys=True) == json.dumps(expected, sort_keys=True)


def test_stock_not_flow_one_warning_per_equality_group():
    # Review finding: k equal columns gave k(k-1)/2 warnings; one per group.
    base = np.arange(1, 101, dtype=np.int64) % 17
    df = pd.DataFrame({"A": base, "B": base.copy(), "C": base.copy(),
                       "D": (base * 7 + 3) % 11})
    eq = [w for w in dp.compute_profile(df)["warnings"] if "equals" in w]
    assert eq == ["A equals B in 100% of rows: likely a stock, not a flow"]


def test_stock_not_flow_skips_identical_unique_id_columns():
    # Review finding: two row-number / id columns (every value unique, equal
    # in every row) are not a stock-vs-flow pair.
    n = 50
    df = pd.DataFrame({"ROW_NO": np.arange(1, n + 1), "ID": np.arange(1, n + 1),
                       "amount": np.arange(n) % 7})
    assert not any("equals" in w for w in dp.compute_profile(df)["warnings"])
