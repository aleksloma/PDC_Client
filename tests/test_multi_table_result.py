"""Multi-table answers: executed code returning a DICT of DataFrames
(RESULT = {'Sales…': df1, 'Stock…': df2}) must render as a `tables` list —
previously it fell through every branch and the user got prose garbage.

Brain calls are stubbed (fake-client pattern); code execution is REAL.
"""
import asyncio
import json

import pandas as pd
import pytest

import local_store
import run_chat_local


@pytest.fixture
def dfs():
    return {
        "sales.csv": pd.DataFrame({"store": ["A", "B", "C"], "amount": [10, 20, 30]}),
        "stock.csv": pd.DataFrame({"sku": ["x", "y"], "qty": [5, 7]}),
    }


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


def _run(monkeypatch, dfs, code, describe_calls=None, summarize_calls=None):
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: {
        "raw_text": "", "kind": "PYTHON", "code": code, "usage": {}})

    def fake_describe(**kw):
        if describe_calls is not None:
            describe_calls.append(kw)
        return {"text": "described", "usage": {}}

    def fake_summarize(**kw):
        if summarize_calls is not None:
            summarize_calls.append(kw)
        # The real path JSON-encodes the payload over HTTP — simulate that so
        # an unserializable preview would fail here like production.
        json.dumps(kw.get("preview"))
        return {"text": "summarized", "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "describe", fake_describe)
    monkeypatch.setattr(run_chat_local.brain_client, "summarize", fake_summarize)
    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="show tables",
        history_rows=[], user_email="alice@acme.com"))
    assert len(events) == 1 and events[0]["single_response"]
    return events[0]["result"]


def test_dict_of_dataframes_builds_tables_list(monkeypatch, dfs):
    describe_calls, summarize_calls = [], []
    code = ("RESULT = {'Sales first rows': dfs['sales.csv'].head(2), "
            "'Stock first rows': dfs['stock.csv'].head(2)}")
    result = _run(monkeypatch, dfs, code, describe_calls, summarize_calls)

    tables = result["tables"]
    assert result["table"] is None
    assert [t["title"] for t in tables] == ["Sales first rows", "Stock first rows"]
    assert tables[0]["columns"] == ["store", "amount"]
    assert tables[0]["total_rows"] == 2 and len(tables[0]["rows"]) == 2
    assert tables[1]["columns"] == ["sku", "qty"]
    assert result["text"] == "described"
    assert len(describe_calls) == 1  # table answers describe from code only
    assert not summarize_calls      # never the garbage summarize path


def test_single_entry_dict_unwraps_to_plain_table(monkeypatch, dfs):
    code = "RESULT = {'Sales': dfs['sales.csv'].head(2)}"
    result = _run(monkeypatch, dfs, code)
    assert result.get("tables") is None
    assert result["table"]["columns"] == ["store", "amount"]
    assert "title" not in result["table"]


def test_dict_of_scalars_keeps_summarize_path(monkeypatch, dfs):
    summarize_calls = []
    code = "RESULT = {'total': 60, 'stores': 3}"
    result = _run(monkeypatch, dfs, code, summarize_calls=summarize_calls)
    assert result["table"] is None and result.get("tables") is None
    assert result["text"] == "summarized"
    assert summarize_calls[0]["preview"] == {"total": 60, "stores": 3}


def test_mixed_dict_takes_only_df_values(monkeypatch, dfs):
    code = ("RESULT = {'Sales': dfs['sales.csv'].head(2), 'note': 'hello', "
            "'Stock': dfs['stock.csv'].head(1)}")
    result = _run(monkeypatch, dfs, code)
    assert [t["title"] for t in result["tables"]] == ["Sales", "Stock"]


def test_history_record_with_tables_round_trips(tmp_path, monkeypatch, dfs):
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    store = local_store.ChatDataStore("c_multitbl")
    conv = store.new_conversation(title="t")
    tables = run_chat_local._build_tables_from_result(
        {"Sales": dfs["sales.csv"], "Stock": dfs["stock.csv"]})
    store.append_history(conv, {"role": "ai", "content": "x", "table": None,
                                "tables": tables, "full_table_keys": ["k1", "k2"],
                                "ts": 1.0})
    rec = store.get_history(conv)[0]
    assert [t["title"] for t in rec["tables"]] == ["Sales", "Stock"]
    assert rec["full_table_keys"] == ["k1", "k2"]
    # Old-shape single-table record (previous release) still loads unchanged.
    store.append_history(conv, {"role": "ai", "content": "old", "ts": 2.0,
                                "table": {"columns": ["a"], "rows": [{"a": 1}],
                                          "total_rows": 1}})
    old = store.get_history(conv)[1]
    assert old["table"]["columns"] == ["a"] and "tables" not in old


def test_reexecute_full_df_result_key_picks_dict_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    import routes.chat as chat_mod
    store = local_store.ChatDataStore("c_rekey")
    (store.files_dir / "m.csv").write_text("a,b\n1,2\n3,4\n5,6\n", encoding="utf-8")
    local_store._DATAFRAME_CACHE.invalidate()
    code = ("RESULT = {'head': dfs['m.csv'].head(1), 'full': dfs['m.csv']}")
    df = asyncio.run(chat_mod._reexecute_full_df("c_rekey", code, "full"))
    assert df is not None and len(df) == 3  # picked the FULL entry
    df_head = asyncio.run(chat_mod._reexecute_full_df("c_rekey", code, "head"))
    assert df_head is not None and len(df_head) == 1
    # No result_key + dict result → old behavior (None → preview fallback).
    assert asyncio.run(chat_mod._reexecute_full_df("c_rekey", code, None)) is None
    local_store._DATAFRAME_CACHE.invalidate()
