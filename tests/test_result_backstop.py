"""Deterministic result backstop (Prompt 14 Part B) — pure unit, no app imports.

The detector decides whether the user is about to be shown a result that says
nothing (every bar the same height, every series overlapping, every table row
carrying the same measure) — and therefore needs the mandatory explanation.
False POSITIVES put a confusing caveat on a good answer, false NEGATIVES lose
the explanation entirely, so both directions are pinned here.

Runs under pytest: `python -m pytest tests/test_result_backstop.py`.
"""
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import result_backstop
from result_backstop import (DATA_NOTE_MARKER, fallback_sentence, inspect_chart,
                             inspect_outputs, inspect_table, strip_marker,
                             values_are_constant)


def _chart(rows, columns=None):
    return {"columns": columns or list(rows[0].keys()), "rows": rows,
            "total_rows": len(rows)}


# --- chart: constant metric -------------------------------------------------

def test_constant_single_series_fires():
    cd = _chart([{"city": "Tbilisi", "Quantity": 1},
                 {"city": "Batumi", "Quantity": 1},
                 {"city": "Kutaisi", "Quantity": 1}])
    det = inspect_chart(cd)
    assert det["kind"] == "constant_metric"
    assert "Quantity" in det["facts"][0] and "= 1" in det["facts"][0]


def test_varying_series_does_not_fire():
    cd = _chart([{"city": "Tbilisi", "Quantity": 5},
                 {"city": "Batumi", "Quantity": 3}])
    assert inspect_chart(cd) is None


def test_float_jitter_counts_as_constant():
    # Relative-spread test (same tolerance as the degenerate-chart skip): a
    # float-noise "constant" must still be caught, else the skip and the
    # backstop disagree and the explanation is lost.
    cd = _chart([{"c": "a", "v": 1.0}, {"c": "b", "v": 1.0 + 1e-12}])
    assert inspect_chart(cd)["kind"] == "constant_metric"


def test_single_point_chart_does_not_fire():
    assert inspect_chart(_chart([{"c": "a", "v": 7}])) is None


def test_categorical_only_chart_does_not_fire():
    # No numeric series at all -> nothing to call constant.
    assert inspect_chart(_chart([{"c": "a", "d": "x"}, {"c": "b", "d": "x"}])) is None


def test_subplot_tables_all_constant_fires():
    cd = {"tables": [_chart([{"c": "a", "v": 2}, {"c": "b", "v": 2}]),
                     _chart([{"c": "a", "v": 9}, {"c": "b", "v": 9}])]}
    assert inspect_chart(cd)["kind"] == "constant_metric"


def test_subplot_tables_one_varying_does_not_fire():
    cd = {"tables": [_chart([{"c": "a", "v": 2}, {"c": "b", "v": 2}]),
                     _chart([{"c": "a", "v": 1}, {"c": "b", "v": 8}])]}
    assert inspect_chart(cd) is None


# --- chart: identical multi-series ------------------------------------------

def test_identical_multi_series_fires():
    rows = []
    for series in ("Apple", "Banana", "Cherry"):
        for city, val in (("Tbilisi", 3), ("Batumi", 5)):
            rows.append({"Series": series, "city": city, "Quantity": val})
    det = inspect_chart(_chart(rows))
    assert det["kind"] == "identical_series"
    assert "3 series" in det["facts"][0]


def test_differing_multi_series_does_not_fire():
    rows = [{"Series": "A", "city": "Tbilisi", "v": 3},
            {"Series": "A", "city": "Batumi", "v": 5},
            {"Series": "B", "city": "Tbilisi", "v": 4},
            {"Series": "B", "city": "Batumi", "v": 5}]
    assert inspect_chart(_chart(rows)) is None


def test_series_equals_category_quirk_does_not_fire():
    # A chart colored by its own x dimension gets one row per series, so the
    # series' label sets are disjoint — comparing them would be meaningless.
    rows = [{"Series": "Tbilisi", "city": "Tbilisi", "v": 4},
            {"Series": "Batumi", "city": "Batumi", "v": 9}]
    assert inspect_chart(_chart(rows)) is None


def test_matrix_chart_with_identical_columns_fires():
    """A heatmap is ONE trace with two label dimensions (no `Series` column) —
    the verified production case: every product carries the same number within
    each city, so the matrix says nothing about products."""
    rows = []
    for city, val in (("Tbilisi", 1), ("Kutaisi", 2), ("Batumi", 3)):
        for product in ("Milk", "Cheese", "Butter"):
            rows.append({"product_name": product, "city_name": city, "value": val})
    det = inspect_chart(_chart(rows))
    assert det["kind"] == "identical_series"
    assert "product_name" in det["facts"][0]


def test_matrix_chart_with_real_variation_does_not_fire():
    rows = [{"product": "Milk", "city": "Tbilisi", "value": 4},
            {"product": "Cheese", "city": "Tbilisi", "value": 9},
            {"product": "Milk", "city": "Batumi", "value": 2},
            {"product": "Cheese", "city": "Batumi", "value": 7}]
    assert inspect_chart(_chart(rows)) is None


def test_matrix_needs_two_label_dimensions_and_a_grid():
    # one label column only -> not a matrix, and a single key per series is not
    # a comparison worth making
    rows = [{"city": "Tbilisi", "v": 3}, {"city": "Batumi", "v": 4}]
    assert inspect_chart(_chart(rows)) is None
    rows = [{"a": "x", "b": "p", "v": 1}, {"a": "y", "b": "q", "v": 1}]
    # every value equal -> caught by the constant check, not the series check
    assert inspect_chart(_chart(rows))["kind"] == "constant_metric"


def test_constant_metric_wins_over_identical_series():
    rows = [{"Series": "A", "c": "x", "v": 1}, {"Series": "A", "c": "y", "v": 1},
            {"Series": "B", "c": "x", "v": 1}, {"Series": "B", "c": "y", "v": 1}]
    assert inspect_chart(_chart(rows))["kind"] == "constant_metric"


# --- tables -----------------------------------------------------------------

def test_constant_table_fires_ignoring_label_columns():
    df = pd.DataFrame({"city": ["Tbilisi", "Batumi", "Kutaisi"],
                       "product": ["A", "B", "C"],
                       "Quantity": [1, 1, 1]})
    det = inspect_table(df)
    assert det["kind"] == "constant_table"
    assert "Quantity" in det["facts"][0]


def test_table_with_one_varying_value_column_does_not_fire():
    df = pd.DataFrame({"city": ["a", "b"], "qty": [1, 1], "revenue": [10, 99]})
    assert inspect_table(df) is None


def test_table_without_value_columns_does_not_fire():
    df = pd.DataFrame({"city": ["a", "b"], "product": ["x", "x"]})
    assert inspect_table(df) is None


def test_single_row_table_does_not_fire():
    assert inspect_table(pd.DataFrame({"c": ["a"], "v": [1]})) is None


def test_all_null_value_column_does_not_fire():
    # Unknown is not constant.
    df = pd.DataFrame({"c": ["a", "b"], "v": [None, None]})
    assert inspect_table(df) is None


def test_series_and_styler_results():
    s = pd.Series([2, 2, 2], index=["a", "b", "c"], name="qty")
    assert inspect_table(s)["kind"] == "constant_table"
    styler = pd.DataFrame({"c": ["a", "b"], "v": [4, 4]}).style
    assert inspect_table(styler)["kind"] == "constant_table"


def test_dict_of_tables_all_constant_fires_mixed_does_not():
    flat = pd.DataFrame({"c": ["a", "b"], "v": [1, 1]})
    other = pd.DataFrame({"c": ["a", "b"], "v": [7, 7]})
    varying = pd.DataFrame({"c": ["a", "b"], "v": [1, 9]})
    assert inspect_table({"one": flat, "two": other})["kind"] == "constant_table"
    assert inspect_table({"one": flat, "two": varying}) is None


def test_boolean_and_datetime_columns_are_not_value_columns():
    df = pd.DataFrame({"flag": [True, True],
                       "when": pd.to_datetime(["2024-01-01", "2024-01-01"])})
    assert inspect_table(df) is None


# --- public entry / robustness ----------------------------------------------

def test_inspect_outputs_dispatch_and_none_inputs():
    assert inspect_outputs(sid="t") is None
    assert inspect_outputs(chart_data=None, result_obj=None, sid="t") is None
    cd = _chart([{"c": "a", "v": 1}, {"c": "b", "v": 1}])
    assert inspect_outputs(chart_data=cd, sid="t")["kind"] == "constant_metric"


def test_chart_data_list_fires_only_when_every_member_fires():
    flat = _chart([{"c": "a", "v": 1}, {"c": "b", "v": 1}])
    varying = _chart([{"c": "a", "v": 1}, {"c": "b", "v": 6}])
    assert inspect_outputs(chart_data_list=[flat, flat], sid="t")["kind"] == "constant_metric"
    assert inspect_outputs(chart_data_list=[flat, varying], sid="t") is None


def test_inspection_error_is_logged_and_skipped(monkeypatch):
    calls = []
    monkeypatch.setattr(result_backstop, "log_with_sid",
                        lambda sid, lvl, msg, **kw: calls.append((lvl, msg)))

    class Exploding:
        @property
        def data(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(result_backstop, "inspect_chart",
                        lambda cd: (_ for _ in ()).throw(RuntimeError("boom")))
    assert inspect_outputs(chart_data={"rows": []}, sid="t") is None
    assert calls and calls[0][0] == "warning" and "RESULT_BACKSTOP_SKIP" in calls[0][1]


def test_odd_inputs_never_raise():
    for bad in ("string", 42, [], {"rows": "not-a-list"}, {"tables": "nope"}):
        assert inspect_outputs(chart_data=bad, sid="t") is None
    assert inspect_outputs(result_obj=object(), sid="t") is None


# --- primitives / fallback text ---------------------------------------------

def test_values_are_constant_edges():
    assert values_are_constant([1, 1]) is True
    assert values_are_constant([1]) is False
    assert values_are_constant([]) is False
    assert values_are_constant([1, 2]) is False
    assert values_are_constant(["x", "x"]) is True
    assert values_are_constant([None, 3, None, 3]) is True


def test_fallback_sentences_localized_and_catalog_aware():
    ka = fallback_sentence("ka", catalog=True)
    assert "კატალოგ" in ka
    assert fallback_sentence("ka", catalog=False) != ka
    assert fallback_sentence("en", catalog=True).startswith("Note:")
    assert "Примечание" in fallback_sentence("ru", catalog=False)
    # unknown language degrades to English, never raises
    assert fallback_sentence("de", catalog=False) == fallback_sentence("en", catalog=False)


def test_strip_marker():
    assert strip_marker(f"{DATA_NOTE_MARKER} hello") == "hello"
    assert strip_marker(f"a {DATA_NOTE_MARKER} b {DATA_NOTE_MARKER}") == "a  b"
    assert strip_marker("") == ""
    assert strip_marker(None) == ""
