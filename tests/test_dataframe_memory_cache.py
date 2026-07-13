"""In-memory DataFrame cache (`local_store._DATAFRAME_CACHE`) tests.

All offline, DATA_ROOT isolated to tmp_path. The cache must:
  - serve repeat loads from memory (zero pipeline calls),
  - reload when a source file changes (signature validation),
  - never let callers poison it (shallow copy on hit),
  - respect the byte budget (oversized datasets are served but not cached),
  - actually FREE expired entries (TTL sweep), and
  - fall back safely when the signature can't be computed.
"""
import os
import time

import pandas as pd
import pytest

import local_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    yield local_store.ChatDataStore("c_testcache")
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def load_counter(monkeypatch):
    """Count invocations of the per-file disk/pipeline loader."""
    calls = {"n": 0}
    real = local_store._load_one_file_cached

    def _counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(local_store, "_load_one_file_cached", _counting)
    return calls


def _write_csv(store, name="data.csv", rows=("a,b", "1,2", "3,4")):
    fp = store.files_dir / name
    fp.write_text("\n".join(rows), encoding="utf-8")
    return fp


def test_second_load_hits_memory_zero_pipeline_calls(store, load_counter):
    _write_csv(store)
    first = store.load_dataframes()
    assert load_counter["n"] == 1
    second = store.load_dataframes()
    assert load_counter["n"] == 1  # no disk/pipeline work on the hit
    pd.testing.assert_frame_equal(first["data.csv"], second["data.csv"])


def test_changed_file_invalidates_and_reloads(store, load_counter):
    fp = _write_csv(store)
    assert store.load_dataframes()["data.csv"]["a"].tolist() == [1, 3]
    # Overwrite with new content and force a distinct mtime_ns.
    fp.write_text("a,b\n9,9\n", encoding="utf-8")
    st = fp.stat()
    os.utime(fp, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))
    out = store.load_dataframes()
    assert out["data.csv"]["a"].tolist() == [9]
    assert load_counter["n"] == 2  # re-parsed after the signature change


def test_hit_returns_shallow_copy_cache_not_poisoned(store):
    _write_csv(store)
    first = store.load_dataframes()
    del first["data.csv"]
    assert "data.csv" in store.load_dataframes()


def test_userstore_and_chatstore_have_distinct_keys(tmp_path, monkeypatch, load_counter):
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    us = local_store.UserStore("s_cachetest")
    cs = local_store.ChatDataStore("c_cachetest2")
    _write_csv(us)
    _write_csv(cs, rows=("x,y", "5,6"))
    assert "a" in us.load_dataframes()["data.csv"].columns
    assert "x" in cs.load_dataframes()["data.csv"].columns
    assert load_counter["n"] == 2
    us.load_dataframes()
    cs.load_dataframes()
    assert load_counter["n"] == 2  # both hits
    local_store._DATAFRAME_CACHE.invalidate()


def test_signature_failure_loads_but_never_caches(store, load_counter, monkeypatch):
    _write_csv(store)
    monkeypatch.setattr(local_store, "_files_signature", lambda d: None)
    assert "data.csv" in store.load_dataframes()
    assert "data.csv" in store.load_dataframes()
    assert load_counter["n"] == 2  # no caching without a signature
    assert len(local_store._DATAFRAME_CACHE) == 0


def test_oversized_dataset_served_but_not_cached(store, load_counter, monkeypatch):
    _write_csv(store)
    tiny = local_store.LRUTTLCache(max_size=8, ttl_seconds=300, max_bytes=10)
    monkeypatch.setattr(local_store, "_DATAFRAME_CACHE", tiny)
    assert "data.csv" in store.load_dataframes()
    assert len(tiny) == 0  # any real df exceeds half of a 10-byte budget
    assert "data.csv" in store.load_dataframes()
    assert load_counter["n"] == 2  # reloaded from disk, RAM never pinned


def test_ttl_sweep_frees_expired_entries():
    cache = local_store.LRUTTLCache(max_size=8, ttl_seconds=0.01, max_bytes=None)
    cache.set("k", {"sig": (), "dfs": {}}, size_bytes=1)
    assert len(cache) == 1
    time.sleep(0.05)
    cache.sweep()
    assert len(cache) == 0  # memory actually released, not just hidden


def test_byte_budget_evicts_lru():
    cache = local_store.LRUTTLCache(max_size=8, ttl_seconds=300, max_bytes=100)
    cache.set("old", "v1", size_bytes=40)
    time.sleep(0.01)
    cache.set("newer", "v2", size_bytes=40)
    time.sleep(0.01)
    assert cache.get("newer") is not None  # make "old" the LRU entry
    cache.set("third", "v3", size_bytes=40)  # 120 > 100 → evict LRU ("old")
    assert cache.get("old") is None
    assert cache.get("newer") is not None
    assert cache.get("third") is not None


def test_entry_count_bound_evicts_lru():
    cache = local_store.LRUTTLCache(max_size=2, ttl_seconds=300, max_bytes=None)
    cache.set("a", 1)
    time.sleep(0.01)
    cache.set("b", 2)
    time.sleep(0.01)
    cache.set("c", 3)
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
