"""Wide styled pivot tables must serialize with every cell present (QA 2.2).

A pivot_table with two value columns produces MultiIndex columns; the old
serializer emitted `columns` as JSON ARRAYS while `_json_safe` "_"-joined the
row-dict tuple KEYS — the frontend's by-name lookup missed every cell and the
table rendered EMPTY, with the row index (city names) dropped entirely.

Brain calls are stubbed (fake-client pattern); code execution is REAL.
"""
import json

import pandas as pd
import pytest

import local_store
import run_chat_local
from run_chat_local import _normalize_df_for_table


@pytest.fixture
def dfs():
    return {
        "sales.csv": pd.DataFrame({
            "city": ["Tbilisi", "Batumi"] * 4,
            "month": ["2024-01", "2024-01", "2024-02", "2024-02"] * 2,
            "revenue": [100.0, 80.0, 120.0, 90.0, 110.0, 85.0, 130.0, 95.0],
            "units": [10, 8, 12, 9, 11, 8, 13, 9],
        }),
    }


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


def _run(monkeypatch, dfs, code):
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: {
        "raw_text": "", "kind": "PYTHON", "code": code, "usage": {}})
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **kw: {"text": "described", "usage": {}})

    def fake_summarize(**kw):
        json.dumps(kw.get("preview"))   # simulate the HTTP JSON encoding
        return {"text": "summarized", "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "summarize", fake_summarize)
    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="pivot it",
        history_rows=[], user_email="alice@acme.com"))
    assert len(events) == 1 and events[0]["single_response"]
    return events[0]["result"]


def test_styled_two_value_pivot_serializes_every_cell(monkeypatch, dfs):
    code = ("RESULT = dfs['sales.csv'].pivot_table(index='city', columns='month', "
            "values=['revenue', 'units'], aggfunc='sum').style.background_gradient()")
    result = _run(monkeypatch, dfs, code)
    table = result["table"]
    assert table is not None
    # city (the pivot's row index) is a visible column; all labels are strings
    assert "city" in table["columns"]
    assert all(isinstance(c, str) for c in table["columns"])
    assert len(table["columns"]) == 1 + 4     # city + 2 values x 2 months
    # round-trip through the persistence normalizer (append_history path):
    # every row must carry every column with a real value
    t = json.loads(json.dumps(local_store._json_safe(table)))
    assert {r["city"] for r in t["rows"]} == {"Tbilisi", "Batumi"}
    for row in t["rows"]:
        for col in t["columns"]:
            assert col in row, f"cell missing for {col!r}"
            assert row[col] is not None
    # the Styler still renders (small pivot, under the caps)
    assert "styled_html" in table


def test_plain_table_shape_unchanged(monkeypatch, dfs):
    code = "RESULT = dfs['sales.csv'].head(2)"
    result = _run(monkeypatch, dfs, code)
    table = result["table"]
    # byte-for-byte the pre-change shape: RangeIndex untouched, no index column
    expected = {
        "columns": ["city", "month", "revenue", "units"],
        "rows": [
            {"city": "Tbilisi", "month": "2024-01", "revenue": 100.0, "units": 10},
            {"city": "Batumi", "month": "2024-01", "revenue": 80.0, "units": 8},
        ],
        "total_rows": 2,
    }
    assert json.dumps(local_store._json_safe(table), sort_keys=True) == \
        json.dumps(expected, sort_keys=True)


# --- _normalize_df_for_table unit -------------------------------------------

def test_rangeindex_passthrough_same_object():
    df = pd.DataFrame({"a": [1, 2]})
    assert _normalize_df_for_table(df) is df


def test_filtered_unnamed_int_index_passthrough():
    df = pd.DataFrame({"a": [1, 2, 3, 4]})
    filtered = df[df["a"] > 2]          # non-contiguous Int64 index, unnamed
    out = _normalize_df_for_table(filtered)
    assert list(out.columns) == ["a"]   # no noise "index" column


def test_named_index_reset_to_column():
    df = pd.DataFrame({"v": [1.0, 2.0]}, index=pd.Index(["x", "y"], name="city"))
    out = _normalize_df_for_table(df)
    assert list(out.columns) == ["city", "v"]
    assert list(out["city"]) == ["x", "y"]


def test_multiindex_columns_flattened_with_separator():
    df = pd.DataFrame({"city": ["A", "A"], "m": ["j", "f"], "v": [1, 2], "u": [3, 4]})
    piv = df.pivot_table(index="city", columns="m", values=["v", "u"], aggfunc="sum")
    out = _normalize_df_for_table(piv)
    assert "city" in out.columns
    flat = [c for c in out.columns if c != "city"]
    assert all(isinstance(c, str) and " / " in c for c in flat)
    # values aligned: to_dict keys match columns exactly
    rows = out.to_dict(orient="records")
    assert all(set(r.keys()) == set(out.columns) for r in rows)


def test_duplicate_names_after_flatten_are_deduped():
    df = pd.DataFrame([[1, 2]], columns=["a", "a"])
    out = _normalize_df_for_table(df)
    assert list(out.columns) == ["a", "a.1"]
