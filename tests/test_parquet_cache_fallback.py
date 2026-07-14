"""Parquet-cache pickle fallback tests.

A dataframe whose object column mixes numbers and strings (the customer's
'Dicc %' case) can't round-trip through parquet; it must fall back to a
pickle entry so the disk cache still covers the whole file — and the cached
load must be dtype-identical to the first load. Old all-parquet manifests
(written by the previous release) must keep loading unchanged.
"""
import json

import pandas as pd
import pytest

import local_store
from local_store import (
    _parquet_cache_paths,
    _parquet_cache_read,
    _parquet_cache_write,
)


@pytest.fixture(autouse=True)
def _no_memory_cache(monkeypatch):
    # These tests exercise the DISK cache layer directly.
    local_store._DATAFRAME_CACHE.invalidate()
    yield
    local_store._DATAFRAME_CACHE.invalidate()


def _mixed_df():
    # Miniature of the customer's Sales sheet 'Dicc %' column: floats + a few
    # 'Division by zero' strings in one object column.
    vals = [float(i) for i in range(118)] + ["Division by zero"] * 3
    return pd.DataFrame({"shop": [f"s{i}" for i in range(121)], "disc": vals})


def _src(tmp_path, name="data.xlsx"):
    p = tmp_path / name
    p.write_bytes(b"raw bytes stand-in")
    st = p.stat()
    return p, st.st_size, st.st_mtime_ns


def test_mixed_dtype_df_round_trips_via_pickle_entry(tmp_path):
    src, size, mtime = _src(tmp_path)
    df = _mixed_df()
    _parquet_cache_write(src, {"data.xlsx": df}, size, mtime)

    cache_dir, manifest_path = _parquet_cache_paths(src)
    assert manifest_path.exists()  # the whole-file abort is gone
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest["entries"]
    assert len(entries) == 1 and entries[0].get("pickle")

    out = _parquet_cache_read(src, size, mtime)
    assert out is not None
    # dtype-consistency: cached load identical to the first load, mixed
    # values intact (this is why coercion to str was rejected).
    pd.testing.assert_frame_equal(out["data.xlsx"], df)
    assert out["data.xlsx"]["disc"].dtype == object
    assert "Division by zero" in out["data.xlsx"]["disc"].tolist()


def test_mixed_manifest_parquet_plus_pickle(tmp_path):
    src, size, mtime = _src(tmp_path)
    clean = pd.DataFrame({"a": [1, 2], "b": ["x", "y"]})
    dfs = {"data.xlsx::Clean": clean, "data.xlsx::Mixed": _mixed_df()}
    _parquet_cache_write(src, dfs, size, mtime)

    _, manifest_path = _parquet_cache_paths(src)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    formats = {e["key"]: ("parquet" if e.get("parquet") else "pickle")
               for e in manifest["entries"]}
    assert formats == {"data.xlsx::Clean": "parquet", "data.xlsx::Mixed": "pickle"}

    out = _parquet_cache_read(src, size, mtime)
    pd.testing.assert_frame_equal(out["data.xlsx::Clean"], clean)
    pd.testing.assert_frame_equal(out["data.xlsx::Mixed"], _mixed_df())


def test_old_shape_manifest_triggers_reparse_not_stale_serve(tmp_path):
    # A manifest written by a PREVIOUS release (no parser_version) must be a
    # cache MISS — the old parser could have cached Timestamp/duplicate column
    # names, and serving them forever would keep pre-fix chats broken. The
    # data still loads (upgrade-safe): the miss falls back to the detection
    # pipeline and the cache is rewritten at the current version.
    src, size, mtime = _src(tmp_path)
    df = pd.DataFrame({"a": [1, 2, 3]})
    cache_dir, manifest_path = _parquet_cache_paths(src)
    cache_dir.mkdir(parents=True)
    df.to_parquet(cache_dir / "old_entry.parquet", engine="pyarrow")
    manifest_path.write_text(json.dumps({
        "src_size": size, "src_mtime_ns": mtime,
        "entries": [{"key": "data.xlsx", "parquet": "old_entry.parquet"}],
    }), encoding="utf-8")

    assert _parquet_cache_read(src, size, mtime) is None  # versionless → miss

    # A manifest at the CURRENT parser version loads normally.
    _parquet_cache_write(src, {"data.xlsx": df}, size, mtime)
    out = _parquet_cache_read(src, size, mtime)
    assert out is not None
    pd.testing.assert_frame_equal(out["data.xlsx"], df)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["parser_version"] == local_store._PARQUET_CACHE_PARSER_VERSION


def test_corrupted_pickle_self_heals_to_pipeline(tmp_path, monkeypatch):
    src, size, mtime = _src(tmp_path)
    _parquet_cache_write(src, {"data.xlsx": _mixed_df()}, size, mtime)
    cache_dir, manifest_path = _parquet_cache_paths(src)
    pkl = next(cache_dir.glob("*.pkl"))
    pkl.write_bytes(b"corrupted")
    assert _parquet_cache_read(src, size, mtime) is None  # Article IV self-heal


def test_pickle_failure_keeps_abort_semantics(tmp_path, monkeypatch):
    src, size, mtime = _src(tmp_path)
    monkeypatch.setattr(pd.DataFrame, "to_pickle",
                        lambda self, *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    _parquet_cache_write(src, {"data.xlsx": _mixed_df()}, size, mtime)
    _, manifest_path = _parquet_cache_paths(src)
    assert not manifest_path.exists()  # no partial manifest, ever


def test_clone_carries_parquet_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(local_store.settings, "DATA_ROOT", str(tmp_path))
    us = local_store.UserStore("s_clonecache")
    (us.files_dir / "d.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    us.load_dataframes()  # populates .parquet_cache
    assert (us.files_dir / ".parquet_cache").is_dir()

    cs = local_store.ChatDataStore("c_clonecache")
    cs.clone_from_user_store(us)
    dst = cs.files_dir / ".parquet_cache"
    assert dst.is_dir() and any(dst.glob("*.manifest.json"))
    # And the cloned manifest is VALID for the copied file (mtime preserved).
    copied = cs.files_dir / "d.csv"
    st = copied.stat()
    out = local_store._parquet_cache_read(copied, st.st_size, st.st_mtime_ns)
    assert out is not None and "d.csv" in out
