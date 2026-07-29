"""Database-snapshot dataset integration: display-name df keys, central
snapshot loading, memory-cache invalidation on re-snapshot, graceful missing-
snapshot degradation, and zero regression for file-only chats."""
import json
import os
import time
from pathlib import Path

import pandas as pd
import pytest

import local_store


TID = "aa11bb22cc33dd44"
TID2 = "bb22cc33dd44ee55"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    yield
    local_store._DATAFRAME_CACHE.invalidate()


def _write_snapshot(tid, df):
    p = local_store.db_snapshot_path(tid)
    df.to_parquet(p, engine="pyarrow")
    return p


def _db_entry(key, tid, **db_extra):
    return {"file_name": key, "file_description": "reg desc",
            "source": "database",
            "db": {"table_id": tid, "connection_id": "cc11cc11cc11cc11",
                   "schema": "shop", "table_name": "T", "display_name": key,
                   "is_connector": False, "auto_included": False,
                   "refreshed_at": "2026-07-27T00:00:00+00:00", **db_extra},
            "schema": {"file_name": key,
                       "fields": {"a": {"description": "col a", "values": None}}}}


def _chat_with(entries):
    store = local_store.ChatDataStore("c_dbtest")
    meta = store.read_meta()
    meta["files"] = entries
    store.write_meta(meta)
    return store


def test_db_key_is_display_name():
    _write_snapshot(TID, pd.DataFrame({"a": [1, 2, 3]}))
    store = _chat_with([_db_entry("clients information", TID)])
    dfs = store.load_dataframes()
    assert list(dfs) == ["clients information"]
    assert len(dfs["clients information"]) == 3


def test_mixed_files_and_db_tables():
    _write_snapshot(TID, pd.DataFrame({"a": [1]}))
    store = _chat_with([_db_entry("clients information", TID)])
    (store.files_dir / "data.csv").write_text("x,y\n1,2\n", encoding="utf-8")
    meta = store.read_meta()
    meta["files"].append({"file_name": "data.csv", "schema": {}})
    store.write_meta(meta)
    dfs = store.load_dataframes()
    assert set(dfs) == {"clients information", "data.csv"}


def test_resnapshot_invalidates_memory_cache():
    p = _write_snapshot(TID, pd.DataFrame({"a": [1]}))
    store = _chat_with([_db_entry("t", TID)])
    first = store.load_dataframes()
    assert first["t"]["a"].tolist() == [1]
    # Warm hit.
    again = store.load_dataframes()
    assert again["t"]["a"].tolist() == [1]
    # Re-snapshot (atomic replace in production; rewrite + mtime bump here).
    pd.DataFrame({"a": [9, 9]}).to_parquet(p, engine="pyarrow")
    ns = time.time_ns()
    os.utime(p, ns=(ns, ns))
    refreshed = store.load_dataframes()
    assert refreshed["t"]["a"].tolist() == [9, 9]


def test_file_only_chat_signature_and_counts_unchanged(monkeypatch):
    """No DB entries → composite signature collapses exactly like before, and
    a _files_signature failure still disables caching."""
    store = _chat_with([])
    (store.files_dir / "data.csv").write_text("a\n1\n", encoding="utf-8")
    calls = {"n": 0}
    real = local_store._load_one_file_cached

    def _counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(local_store, "_load_one_file_cached", _counting)
    store.load_dataframes()
    assert calls["n"] == 1
    store.load_dataframes()
    assert calls["n"] == 1  # memory hit — no second pipeline call

    local_store._DATAFRAME_CACHE.invalidate()
    monkeypatch.setattr(local_store, "_files_signature", lambda d: None)
    store.load_dataframes()
    store.load_dataframes()
    assert calls["n"] == 3  # never cached when the signature fails


def test_missing_snapshot_skips_key_and_keeps_others():
    _write_snapshot(TID, pd.DataFrame({"a": [1]}))
    store = _chat_with([_db_entry("ok table", TID), _db_entry("gone table", TID2)])
    dfs = store.load_dataframes()
    assert set(dfs) == {"ok table"}  # missing snapshot skipped, no exception


def test_snapshot_never_enters_parquet_cache():
    _write_snapshot(TID, pd.DataFrame({"a": [1]}))
    store = _chat_with([_db_entry("t", TID)])
    store.load_dataframes()
    assert not (store.files_dir / ".parquet_cache").exists()
    assert not (local_store.db_snapshot_path(TID).parent / ".parquet_cache").exists()


def test_db_snapshot_strings_stay_object_never_category():
    """Regression (demo 3D-chart wrong-axis bug): snapshot string columns
    served as pandas categoricals made generated groupby code — pandas < 3.0
    defaults to observed=False — emit the full cartesian product of ALL
    categories (every city × every product, mostly NaN), so plotly put every
    category on the chart axes. The loader must serve plain object dtype even
    when the registry's dtype_plan requests 'category'."""
    _write_snapshot(TID, pd.DataFrame({"seg": ["a", "b", "a"],
                                       "qty": [1, 2, 3]}))
    reg = {"connections": [],
           "tables": [{"id": TID, "dtype_plan": {"seg": "category"}}]}
    (Path(local_store.settings.DATA_ROOT) / "data_sources.json").write_text(
        json.dumps(reg), encoding="utf-8")
    store = _chat_with([_db_entry("t", TID)])
    dfs = store.load_dataframes()
    assert str(dfs["t"]["seg"].dtype) != "category"
    # Observed groups only — a categorical would also surface unused categories.
    grouped = dfs["t"].groupby("seg")["qty"].sum()
    assert set(grouped.index) == {"a", "b"}


def test_unique_df_key():
    assert local_store.unique_df_key({"a.csv"}, "clients") == "clients"
    assert local_store.unique_df_key({"clients"}, "clients") == "clients (2)"
    assert local_store.unique_df_key({"clients", "clients (2)"}, "clients") == "clients (3)"
    assert local_store.unique_df_key(set(), "  ") == "table"


def test_schema_docs_db_extras_and_file_shape_unchanged():
    _write_snapshot(TID, pd.DataFrame({"a": [1]}))
    store = _chat_with([
        {"file_name": "data.csv", "file_description": "fd",
         "schema": {"file_name": "data.csv", "fields": {"x": {"description": "d"}}}},
        _db_entry("t", TID, relations=[{"related_table_id": TID2,
                                        "related_df_key": "other",
                                        "join_keys": [["a", "b"]]}]),
    ])
    docs = store.schema_docs()
    # File entry: EXACTLY the two historical keys.
    assert set(docs["data.csv"]) == {"file_description", "fields"}
    # DB entry: the extras schema_builder consumes.
    assert docs["t"]["source"] == "database"
    assert docs["t"]["db_table"] == "shop.T"
    assert docs["t"]["refreshed_at"] == "2026-07-27T00:00:00+00:00"
    assert docs["t"]["relations"][0]["join_keys"] == [["a", "b"]]


def test_clone_carries_db_entries():
    _write_snapshot(TID, pd.DataFrame({"a": [1]}))
    user = local_store.UserStore("s_dbclone")
    meta = user.read_meta()
    meta["files"] = [_db_entry("t", TID)]
    user.write_meta(meta)
    chat = local_store.ChatDataStore("c_dbclone")
    chat.clone_from_user_store(user)
    dfs = chat.load_dataframes()
    assert list(dfs) == ["t"]


def test_merge_schema_entry_carries_user_edits():
    old = {"file_name": "t", "file_description": "user edited",
           "schema": {"file_name": "t", "fields": {
               "keep": {"description": "user desc", "values": {"1": "one"},
                        "technical_description": "old tech"},
               "gone": {"description": "vanished col"}}}}
    fresh = {"file_name": "t", "file_description": "registry desc",
             "schema": {"file_name": "t", "fields": {
                 "keep": {"description": "fresh", "values": None,
                          "technical_description": "new tech"},
                 "new": {"description": "fresh new"}}}}
    merged = local_store.merge_schema_entry(old, fresh)
    f = merged["schema"]["fields"]
    assert merged["file_description"] == "user edited"
    assert f["keep"]["description"] == "user desc"
    assert f["keep"]["values"] == {"1": "one"}
    assert f["keep"]["technical_description"] == "new tech"  # fresh wins
    assert "gone" not in f                                    # vanished col dropped
    assert f["new"]["description"] == "fresh new"
