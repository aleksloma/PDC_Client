"""Refresh scheduler: pure next-run computation, run_all_due isolation +
lock, drift resync into chat metas, refresh failure keeping the last good
snapshot, and no import-time threads (the local_store sweeper lesson)."""
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from cryptography.fernet import Fernet

import db_scheduler
import db_sources
import local_store
from settings import settings


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()
    # stop() leaves the module _STOP flag set by design; a test that called it
    # must not make a later run_all_due() exit before its first table.
    db_scheduler._STOP.clear()
    # A refresh that finds an ADDED column drafts its description through the
    # brain. Offline by default: the draft fails fast (the refresh carries on
    # with an empty description); tests that exercise the draft override it.
    import brain_client

    def _offline_autofill(**kw):
        raise brain_client.BrainError("offline test suite")
    monkeypatch.setattr(brain_client, "schema_autofill", _offline_autofill)
    yield
    db_scheduler._STOP.clear()
    local_store._DATAFRAME_CACHE.invalidate()


def _sqlite_setup(tmp_path, ddl_rows=3):
    """A registered sqlite table backed by a real file DB, driven through the
    SAME registry + connector path production uses."""
    from sqlalchemy import create_engine, text
    db = tmp_path / "src.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (a INTEGER, b TEXT)"))
        for i in range(ddl_rows):
            conn.execute(text(f"INSERT INTO t VALUES ({i}, 'x{i}')"))
    eng.dispose()
    store = db_sources.DataSourceStore()
    c = store.create_connection(
        {"name": "s", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor="ladmin")
    t = store.upsert_table({
        "connection_id": c["id"], "schema": "", "table_name": "t",
        "display_name": "test table", "description": "d",
        "columns": [{"name": "a", "dtype": "INTEGER", "description": "col a"},
                    {"name": "b", "dtype": "TEXT", "description": "col b"}],
    }, actor="ladmin")
    return db, store, t["id"]


# ---------------------------------------------------------------------------
# next-run computation (pure — full matrix in tests/test_schedule_utils.py)
# ---------------------------------------------------------------------------

def test_daily_next_fire_before_after_and_exact():
    import schedule_utils
    now = datetime(2026, 7, 28, 10, 30)

    def daily(t):
        return schedule_utils.validate_schedule({"mode": "daily", "time": t,
                                                 "enabled": True})
    assert schedule_utils.next_fire(daily("11:00"), now) == datetime(2026, 7, 28, 11, 0)
    assert schedule_utils.next_fire(daily("09:00"), now) == datetime(2026, 7, 29, 9, 0)
    # Exactly at the boundary → tomorrow (fire strictly after `after`).
    assert schedule_utils.next_fire(daily("00:00"), datetime(2026, 7, 28, 0, 0)) == \
        datetime(2026, 7, 29, 0, 0)
    # Garbage settings fall back to daily midnight via the migration seam.
    g = schedule_utils.schedule_from_settings({"refresh_time": "bogus",
                                               "refresh_enabled": True})
    assert schedule_utils.next_fire(g, now) == datetime(2026, 7, 29, 0, 0)


# ---------------------------------------------------------------------------
# refresh_one_table
# ---------------------------------------------------------------------------

def test_refresh_one_table_snapshots_and_marks(tmp_path):
    _, store, tid = _sqlite_setup(tmp_path)
    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is True and res["rows"] == 3
    assert local_store.db_snapshot_path(tid).exists()
    row = store.get_table(tid)
    assert row["refreshed_at"] and row["row_count"] == 3
    # Technical descriptions computed from the snapshot.
    assert any(c.get("technical_description") for c in row["columns"])


def test_refresh_writes_profile_sidecar(tmp_path):
    _, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    ppath = local_store.db_profile_path(tid)
    assert ppath.is_file()
    prof = json.loads(ppath.read_text(encoding="utf-8"))
    assert prof["rows"] == 3
    st = local_store.db_snapshot_path(tid).stat()
    assert prof["src"] == {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def test_refresh_failure_keeps_previous_profile(tmp_path):
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    ppath = local_store.db_profile_path(tid)
    before = ppath.read_text(encoding="utf-8")
    db.unlink()
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is False
    assert ppath.read_text(encoding="utf-8") == before   # previous profile kept


# ---------------------------------------------------------------------------
# Smart refresh (Part C): fingerprint skip / reload / force / fallback
# ---------------------------------------------------------------------------

def test_smart_refresh_skips_unchanged_then_reloads_on_change(tmp_path):
    from sqlalchemy import create_engine, text
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test", force=False)["ok"] is True
    snap = local_store.db_snapshot_path(tid)
    mtime = snap.stat().st_mtime_ns
    refreshed = store.get_table(tid)["refreshed_at"]
    checked = store.get_table(tid)["last_checked_at"]

    res = db_scheduler.refresh_one_table(tid, actor="test", force=False)
    assert res["ok"] is True and res["skipped"] is True
    row = store.get_table(tid)
    assert snap.stat().st_mtime_ns == mtime            # snapshot untouched
    assert row["refreshed_at"] == refreshed            # not a refresh
    assert row["last_checked_at"] >= checked           # but checked moved

    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO t VALUES (99, 'new')"))
    eng.dispose()
    res2 = db_scheduler.refresh_one_table(tid, actor="test", force=False)
    assert res2["ok"] is True and res2.get("skipped") is False
    assert res2["rows"] == 4
    assert snap.stat().st_mtime_ns > mtime


def test_schema_only_change_reloads_and_records_drift(tmp_path):
    from sqlalchemy import create_engine, text
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test", force=False)["ok"] is True
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE t ADD COLUMN c INTEGER"))  # no row change
    eng.dispose()
    res = db_scheduler.refresh_one_table(tid, actor="test", force=False)
    assert res["ok"] is True and res.get("skipped") is False      # schema in hash
    assert res["drift"]["added"] == ["c"]
    drift = store.get_table(tid)["last_drift"]
    assert drift["added"] == ["c"] and drift["dismissed"] is False


def test_force_resnapshot_despite_matching_fingerprint(tmp_path):
    _, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test", force=False)["ok"] is True
    snap = local_store.db_snapshot_path(tid)
    mtime = snap.stat().st_mtime_ns
    res = db_scheduler.refresh_one_table(tid, actor="test")   # default force=True
    assert res["ok"] is True and res.get("skipped") is False
    assert snap.stat().st_mtime_ns > mtime


def test_fingerprint_failure_falls_through_to_full_snapshot(tmp_path, monkeypatch):
    import db_connector
    _, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test", force=False)["ok"] is True
    monkeypatch.setattr(db_connector, "fingerprint_table",
                        lambda *a, **k: {"ok": False, "error": "boom"})
    snap = local_store.db_snapshot_path(tid)
    mtime = snap.stat().st_mtime_ns
    res = db_scheduler.refresh_one_table(tid, actor="test", force=False)
    assert res["ok"] is True and res.get("skipped") is False   # never blocks
    assert snap.stat().st_mtime_ns > mtime
    # stale hash cleared so a later run can never false-skip
    assert store.get_table(tid)["last_fingerprint"] is None


def test_refresh_multichunk_all_null_leading_column(tmp_path, monkeypatch):
    """END-TO-END for the production snapshot bug: the scheduler passes the
    fingerprint's introspected type_info into snapshot_table, so a column
    that is all-NULL through chunk 1 still lands as int64 — pre-fix pandas
    inferred it null/object and a later chunk's real values killed the
    write with 'Table schema does not match schema used to create file'."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from sqlalchemy import create_engine, text
    monkeypatch.setattr(settings, "DB_SNAPSHOT_CHUNK_ROWS", 2)
    db = tmp_path / "src.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (code INTEGER, b TEXT)"))
        for i in range(6):
            code = "NULL" if i < 4 else str(100 + i)
            conn.execute(text(f"INSERT INTO t VALUES ({code}, 'x{i}')"))
    eng.dispose()
    store = db_sources.DataSourceStore()
    c = store.create_connection(
        {"name": "s", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor="ladmin")
    t = store.upsert_table({
        "connection_id": c["id"], "schema": "", "table_name": "t",
        "display_name": "nulls", "description": "d",
        "columns": [{"name": "code", "dtype": "INTEGER", "description": ""},
                    {"name": "b", "dtype": "TEXT", "description": ""}],
    }, actor="ladmin")
    res = db_scheduler.refresh_one_table(t["id"], actor="test")
    assert res["ok"] is True and res["rows"] == 6
    schema = pq.read_schema(local_store.db_snapshot_path(t["id"]))
    assert schema.field("code").type == pa.int64()
    assert schema.field("b").type == pa.string()


def test_failed_cast_prunes_plan_and_next_refresh_self_heals(tmp_path, monkeypatch):
    """A stored downcast a chunk outgrows fails THAT refresh loudly (previous
    snapshot kept), but the pruned plan is persisted on the failure path —
    the next run succeeds at the canonical width instead of failing nightly
    forever."""
    from sqlalchemy import create_engine, text
    monkeypatch.setattr(settings, "DB_SNAPSHOT_CHUNK_ROWS", 2)
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    snap = local_store.db_snapshot_path(tid)
    mtime = snap.stat().st_mtime_ns
    # A legacy value-derived plan pinned int8; new data outgrows it in a
    # later chunk.
    doc = store.get_table(tid)
    doc["dtype_plan"] = {"a": "int8"}
    store.upsert_table(doc, actor="ladmin")
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO t VALUES (300, 'big')"))
    eng.dispose()
    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is False
    assert "'a'" in (res["error"] or "")
    assert snap.stat().st_mtime_ns == mtime            # previous snapshot kept
    assert "a" not in (store.get_table(tid).get("dtype_plan") or {})
    res2 = db_scheduler.refresh_one_table(tid, actor="test")
    assert res2["ok"] is True and res2["rows"] == 4    # self-healed


def test_dtype_change_detected_and_meta_resynced(tmp_path):
    from sqlalchemy import create_engine, text
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test", force=False)["ok"] is True
    # chat meta referencing the table (the resync target)
    chat = local_store.ChatDataStore("c_dtype")
    chat.write_meta({"files": [{
        "file_name": "test table", "source": "database",
        "db": {"table_id": tid},
        "schema": {"file_name": "test table", "fields": {}}}]})
    # sqlite can't ALTER COLUMN TYPE → drop + re-add same name, different type
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE t DROP COLUMN b"))
        conn.execute(text("ALTER TABLE t ADD COLUMN b REAL"))
    eng.dispose()
    res = db_scheduler.refresh_one_table(tid, actor="test", force=False)
    assert res["ok"] is True
    assert res["drift"]["retyped"] == [{"col": "b", "from": "TEXT", "to": "REAL"}]
    row = store.get_table(tid)
    bcol = next(c for c in row["columns"] if c["name"] == "b")
    assert bcol["dtype"] == "REAL"                      # registry dtype refreshed
    assert bcol["description"] == "col b"               # admin description kept
    assert row["last_drift"]["retyped"][0]["col"] == "b"
    meta = chat.read_meta()
    fields = meta["files"][0]["schema"]["fields"]
    # Resync delivered the FRESH snapshot stats (the re-added column is all
    # NULL → "0/3 filled" proves it's the new column, not the old TEXT one).
    assert "0/3 filled" in fields["b"]["technical_description"]


def test_run_all_due_table_filter_and_skipped_count(tmp_path):
    _, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test", force=False)["ok"] is True
    out = db_scheduler.run_all_due(reason="test", table_ids=[tid])
    assert len(out["results"]) == 1
    assert out["results"][0]["skipped"] is True
    assert out["skipped_count"] == 1
    out2 = db_scheduler.run_all_due(reason="test", table_ids=[])
    assert out2["results"] == []


def test_compose_fingerprint_stable_and_sensitive():
    fp = {"columns": [{"name": "a", "dtype": "INTEGER"}],
          "agg": {"count": 3, "sums": {"a": "6"}, "avgs": {"a": "2.0"},
                  "maxes": {}}}
    h1 = db_scheduler.compose_fingerprint(fp)
    h2 = db_scheduler.compose_fingerprint(json.loads(json.dumps(fp)))
    assert h1 == h2 and h1.startswith("fp1:") and len(h1) == 4 + 64
    changed = json.loads(json.dumps(fp))
    changed["agg"]["count"] = 4
    assert db_scheduler.compose_fingerprint(changed) != h1
    retyped = json.loads(json.dumps(fp))
    retyped["columns"][0]["dtype"] = "REAL"
    assert db_scheduler.compose_fingerprint(retyped) != h1


def test_refresh_failure_keeps_previous_snapshot_and_timestamp(tmp_path):
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    good = store.get_table(tid)["refreshed_at"]
    snap = local_store.db_snapshot_path(tid)
    before = snap.stat().st_mtime_ns
    db.unlink()  # source DB gone → refresh fails
    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is False
    assert snap.exists() and snap.stat().st_mtime_ns == before
    after = store.get_table(tid)
    assert after["refreshed_at"] == good           # last good timestamp kept
    assert after["last_refresh_error"]


def test_drift_resync_updates_chat_meta(tmp_path):
    from sqlalchemy import create_engine, text
    db, store, tid = _sqlite_setup(tmp_path)
    db_scheduler.refresh_one_table(tid, actor="test")

    # A chat using the table, with a user-edited column description.
    chat = local_store.ChatDataStore("c_drift")
    meta = chat.read_meta()
    meta["files"] = [{
        "file_name": "test table", "file_description": "user file desc",
        "source": "database",
        "db": {"table_id": tid, "display_name": "test table",
               "auto_included": False, "relations": []},
        "schema": {"file_name": "test table",
                   "fields": {"a": {"description": "USER EDIT", "values": None},
                              "b": {"description": "col b", "values": None}}},
    }]
    chat.write_meta(meta)

    # Source drifts: column b removed, c added.
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE t DROP COLUMN b"))
        conn.execute(text("ALTER TABLE t ADD COLUMN c INTEGER"))
    eng.dispose()

    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is True
    assert res["drift"]["added"] == ["c"] and res["drift"]["removed"] == ["b"]

    fields = local_store.ChatDataStore("c_drift").read_meta()["files"][0]["schema"]["fields"]
    assert "b" not in fields                       # vanished column deleted
    assert "c" in fields                           # new column appended
    assert fields["a"]["description"] == "USER EDIT"  # user edit survives
    # file_description: the user's edit wins over the registry description.
    assert local_store.ChatDataStore("c_drift").read_meta()["files"][0][
        "file_description"] == "user file desc"


def test_driftfree_refresh_touches_chat_refreshed_at(tmp_path):
    db, store, tid = _sqlite_setup(tmp_path)
    chat = local_store.ChatDataStore("c_touch")
    meta = chat.read_meta()
    meta["files"] = [{"file_name": "test table", "source": "database",
                      "db": {"table_id": tid, "refreshed_at": None},
                      "schema": {"file_name": "test table", "fields": {}}}]
    chat.write_meta(meta)
    db_scheduler.refresh_one_table(tid, actor="test")
    got = local_store.ChatDataStore("c_touch").read_meta()["files"][0]["db"]["refreshed_at"]
    assert got == store.get_table(tid)["refreshed_at"]


# ---------------------------------------------------------------------------
# run_all_due + lock + thread hygiene
# ---------------------------------------------------------------------------

def test_run_all_due_isolates_per_table_failure(tmp_path, monkeypatch):
    _, store, tid = _sqlite_setup(tmp_path)
    # Second registered table with no reachable source → fails.
    c2 = store.create_connection(
        {"name": "bad", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{tmp_path}/missing/x.db"},
        "pw", actor="ladmin")
    store.upsert_table({"connection_id": c2["id"], "schema": "",
                        "table_name": "nope", "display_name": "nope",
                        "columns": []}, actor="ladmin")
    out = db_scheduler.run_all_due(reason="test")
    assert out["ok"] is True
    oks = [r["ok"] for r in out["results"]]
    assert oks.count(True) == 1 and oks.count(False) == 1
    assert store.get_refresh_settings()["last_run_at"]


def test_run_all_due_skips_without_encryption_key(tmp_path, monkeypatch):
    _sqlite_setup(tmp_path)
    monkeypatch.setattr(db_sources.settings, "CLIENT_ENCRYPTION_KEY", "")
    out = db_scheduler.run_all_due(reason="test")
    assert out["ok"] is False and "encryption" in out["error"]


def test_lock_prevents_concurrent_runs(tmp_path):
    _sqlite_setup(tmp_path)
    assert db_scheduler._acquire_run_lock() is True
    out = db_scheduler.run_all_due(reason="test")
    assert out["ok"] is False and "already running" in out["error"]
    db_scheduler._release_run_lock()
    assert db_scheduler.run_all_due(reason="test")["ok"] is True


def test_stale_lock_is_reclaimed(tmp_path, monkeypatch):
    _sqlite_setup(tmp_path)
    assert db_scheduler._acquire_run_lock() is True
    monkeypatch.setattr(settings, "DB_REFRESH_LOCK_STALE_S", 0)
    import time
    time.sleep(0.01)
    assert db_scheduler.run_all_due(reason="test")["ok"] is True


def test_start_stop_thread_joins():
    import threading
    before = threading.active_count()
    db_scheduler.start()
    assert threading.active_count() == before + 1
    db_scheduler.stop(timeout=5.0)
    assert threading.active_count() == before


def test_import_starts_no_thread():
    """The local_store sweeper anti-pattern regression: importing the
    scheduler module in a fresh interpreter must spawn nothing."""
    code = ("import threading, db_scheduler; "
            "names=[t.name for t in threading.enumerate()]; "
            "assert 'db_refresh_scheduler' not in names, names; print('clean')")
    out = subprocess.run([sys.executable, "-c", code],
                         capture_output=True, text=True,
                         cwd=str(Path(__file__).resolve().parent.parent))
    assert out.returncode == 0, out.stderr
    assert "clean" in out.stdout


# ---------------------------------------------------------------------------
# The snapshot column list — a failed fingerprint must never fall back to a
# name-less SELECT *, whose keys come from the DRIVER's casing (UPPERCASE on
# Oracle) and would read as "every column added AND removed".
# ---------------------------------------------------------------------------

def test_snapshot_names_columns_from_the_fingerprint(tmp_path, monkeypatch):
    _, store, tid = _sqlite_setup(tmp_path)
    seen = {}
    real = db_scheduler.db_connector.snapshot_table

    def spy(*a, **kw):
        seen["columns"] = kw.get("columns")
        return real(*a, **kw)

    monkeypatch.setattr(db_scheduler.db_connector, "snapshot_table", spy)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    assert seen["columns"] == ["a", "b"]


def test_failed_fingerprint_falls_back_to_a_fresh_introspect(tmp_path, monkeypatch):
    """The realistic failure: the fingerprint's aggregate SELECT dies (exotic
    type, statement timeout) while the Inspector is perfectly healthy."""
    _, store, tid = _sqlite_setup(tmp_path)
    monkeypatch.setattr(db_scheduler.db_connector, "fingerprint_table",
                        lambda *a, **kw: {"ok": False, "error": "boom"})
    seen = {}
    real = db_scheduler.db_connector.snapshot_table

    def spy(*a, **kw):
        seen["columns"] = kw.get("columns")
        return real(*a, **kw)

    monkeypatch.setattr(db_scheduler.db_connector, "snapshot_table", spy)
    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is True and res["rows"] == 3
    assert seen["columns"] == ["a", "b"]
    # Names line up with the registry, so nothing reads as drift.
    assert res["drift"] == {"added": [], "removed": [], "retyped": []}


def test_both_probes_failing_aborts_and_keeps_the_last_snapshot(tmp_path, monkeypatch):
    """Inspector unreachable → fail fast down the existing failed-refresh path
    rather than snapshot with unnamed columns."""
    _, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    dest = local_store.db_snapshot_path(tid)
    before_mtime = dest.stat().st_mtime_ns
    before_at = store.get_table(tid)["refreshed_at"]

    monkeypatch.setattr(db_scheduler.db_connector, "fingerprint_table",
                        lambda *a, **kw: {"ok": False, "error": "boom"})
    monkeypatch.setattr(db_scheduler.db_connector, "introspect",
                        lambda *a, **kw: {"ok": False, "error": "boom"})

    def never(*a, **kw):                       # pragma: no cover - must not run
        raise AssertionError("snapshot must not run without a column list")

    monkeypatch.setattr(db_scheduler.db_connector, "snapshot_table", never)
    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is False and res["error"]
    row = store.get_table(tid)
    assert dest.stat().st_mtime_ns == before_mtime      # last good data kept
    assert row["refreshed_at"] == before_at
    assert [c["name"] for c in row["columns"]] == ["a", "b"]


# ---------------------------------------------------------------------------
# Live tables are never snapshotted by a refresh
# ---------------------------------------------------------------------------

def _no_engine_spy(monkeypatch):
    import db_connector
    calls = []

    def no_engine(*a, **kw):
        calls.append(1)
        raise RuntimeError("no connection may be opened for a live table")
    monkeypatch.setattr(db_connector, "get_engine", no_engine)
    return calls


def test_refresh_one_table_skips_a_live_table(tmp_path, monkeypatch, caplog):
    import logging
    _, store, tid = _sqlite_setup(tmp_path)
    assert store.set_table_mode(tid, "live", actor="ladmin", reason="manual")
    calls = _no_engine_spy(monkeypatch)
    with caplog.at_level(logging.INFO):
        res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res.get("ok") is True
    assert res.get("skipped") is True
    assert res.get("reason") == "live"
    assert calls == []
    assert not local_store.db_snapshot_path(tid).exists()
    assert not local_store.db_profile_path(tid).exists()
    row = store.get_table(tid)
    assert not row.get("refreshed_at")
    assert row["mode"] == "live"
    assert any("LIVE_SKIP" in r.getMessage() and tid in r.getMessage()
               for r in caplog.records)


def test_refresh_one_table_skips_a_live_table_without_force(tmp_path, monkeypatch):
    _, store, tid = _sqlite_setup(tmp_path)
    assert store.set_table_mode(tid, "live", actor="ladmin", reason="manual")
    calls = _no_engine_spy(monkeypatch)
    res = db_scheduler.refresh_one_table(tid, actor="test", force=False)
    assert res.get("skipped") is True and res.get("reason") == "live"
    assert calls == []


def test_run_all_due_leaves_live_tables_out(tmp_path, caplog):
    import logging
    from sqlalchemy import create_engine, text
    db, store, snap_tid = _sqlite_setup(tmp_path)
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE u (a INTEGER, b TEXT)"))
        conn.execute(text("INSERT INTO u VALUES (1, 'y')"))
    eng.dispose()
    cid = store.get_table(snap_tid)["connection_id"]
    live_tid = store.upsert_table({
        "connection_id": cid, "schema": "", "table_name": "u",
        "display_name": "live table", "description": "d", "mode": "live",
        "columns": [{"name": "a", "dtype": "INTEGER", "description": "col a"},
                    {"name": "b", "dtype": "TEXT", "description": "col b"}],
    }, actor="ladmin")["id"]
    with caplog.at_level(logging.INFO):
        out = db_scheduler.run_all_due(reason="test")
    ids = [r["table_id"] for r in out["results"]]
    assert live_tid not in ids
    assert snap_tid in ids
    snap_res = next(r for r in out["results"] if r["table_id"] == snap_tid)
    assert snap_res["ok"] is True
    assert local_store.db_snapshot_path(snap_tid).exists()
    assert not local_store.db_snapshot_path(live_tid).exists()
    assert not store.get_table(live_tid).get("refreshed_at")
    assert any("LIVE_SKIP" in r.getMessage() and live_tid in r.getMessage()
               for r in caplog.records)


# ---------------------------------------------------------------------------
# Added columns get an AI-drafted description (the ONE draft mechanism);
# surviving columns and the table description are never touched, and a
# draft failure never fails the refresh.
# ---------------------------------------------------------------------------

def _add_column_c(db):
    from sqlalchemy import create_engine, text
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("ALTER TABLE t ADD COLUMN c INTEGER"))
    eng.dispose()


def _fake_drafter(monkeypatch, result=None, raises=None):
    import routes.admin_data as admin_mod
    calls = []

    def fake(cfg, password, schema, table, email, intro=None,
             existing_descriptions=None):
        calls.append({"schema": schema, "table": table, "email": email,
                      "intro": intro,
                      "existing_descriptions": existing_descriptions})
        if raises is not None:
            raise raises
        return result
    monkeypatch.setattr(admin_mod, "_draft_table_descriptions", fake)
    return calls


_DRAFT_OK = {"ok": True, "confirmed": False,
             "draft": {"table_description": "NEW TABLE DESC",
                       "columns": {"a": "DRAFT A", "b": "DRAFT B",
                                   "c": "DRAFT C"}}}


def test_added_column_gets_the_drafted_description(tmp_path, monkeypatch):
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    chat = local_store.ChatDataStore("c_draft")
    meta = chat.read_meta()
    meta["files"] = [{
        "file_name": "test table", "source": "database",
        "db": {"table_id": tid, "display_name": "test table",
               "auto_included": False, "relations": []},
        "schema": {"file_name": "test table",
                   "fields": {"a": {"description": "col a", "values": None},
                              "b": {"description": "col b", "values": None}}}}]
    chat.write_meta(meta)
    _add_column_c(db)
    calls = _fake_drafter(monkeypatch, result=_DRAFT_OK)

    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is True
    # The returned drift keeps its three keys.
    assert res["drift"] == {"added": ["c"], "removed": [], "retyped": []}
    row = store.get_table(tid)
    by_name = {c["name"]: c for c in row["columns"]}
    assert by_name["c"]["description"] == "DRAFT C"
    assert by_name["a"]["description"] == "col a"      # surviving: untouched
    assert by_name["b"]["description"] == "col b"
    assert row["description"] == "d"                    # table desc untouched
    assert row["last_drift"]["added"] == ["c"]
    assert row["last_drift"]["drafted"] == ["c"]
    # Draft only the added columns: every surviving column arrives
    # pre-described, so the prompt's cols_to_fill can only be ["c"].
    assert len(calls) == 1
    assert calls[0]["intro"] is None
    assert calls[0]["email"] == "test"
    assert calls[0]["table"] == "t"
    assert calls[0]["existing_descriptions"] == {"a": "col a", "b": "col b"}
    # The resync carries the drafted text into the referencing chat.
    fields = local_store.ChatDataStore("c_draft").read_meta()["files"][0][
        "schema"]["fields"]
    assert fields["c"]["description"] == "DRAFT C"
    assert fields["a"]["description"] == "col a"


def test_the_real_drafter_asks_only_for_the_added_column(tmp_path, monkeypatch):
    """End to end through routes.admin_data._draft_table_descriptions against
    the sqlite source: the brain call's cols_to_fill is exactly the added
    column, and its unique_hints carry no real value of it."""
    import routes.admin_data as admin_mod
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    _add_column_c(db)
    seen = {}

    def fake_autofill(**kw):
        seen.update(kw)
        return {"file_description": "ignored", "columns": {"c": "DRAFT C"}}
    monkeypatch.setattr(admin_mod.brain_client, "schema_autofill", fake_autofill)
    monkeypatch.setattr(settings, "BRAIN_DRAFT_TIMEOUT", 7.0)

    res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is True
    assert seen["cols_to_fill"] == ["c"]
    assert seen["timeout"] == 7.0
    # The added column is all NULL here: one value-free profile, no values.
    hint_c = seen["unique_hints"]["c"]
    assert len(hint_c) == 1 and hint_c[0].startswith("[profile: ")
    assert "nulls=100.0%" in hint_c[0]
    by_name = {c["name"]: c for c in store.get_table(tid)["columns"]}
    assert by_name["c"]["description"] == "DRAFT C"
    assert by_name["a"]["description"] == "col a"


def test_scheduled_run_drafts_as_the_registrant(tmp_path, monkeypatch):
    db, store, tid = _sqlite_setup(tmp_path)
    doc = store.get_table(tid)
    doc["registered_by"] = "owner@x.com"
    store.upsert_table(doc, actor="ladmin")
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    _add_column_c(db)
    calls = _fake_drafter(monkeypatch, result=_DRAFT_OK)
    assert db_scheduler.refresh_one_table(tid, actor="scheduler:test")["ok"] is True
    assert calls[0]["email"] == "owner@x.com"


def test_scheduled_run_without_registrant_drafts_as_the_admin(tmp_path, monkeypatch):
    """An admin-registered table carries no registered_by: the draft goes out
    as the local admin, never as "scheduler:<reason>"."""
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    _add_column_c(db)
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    calls = _fake_drafter(monkeypatch, result=_DRAFT_OK)
    assert db_scheduler.refresh_one_table(tid, actor="scheduler:nightly")["ok"] is True
    assert calls[0]["email"] == "ladmin"


@pytest.mark.parametrize("mode", ["not_ok", "raises"])
def test_draft_failure_never_fails_the_refresh(tmp_path, monkeypatch, caplog, mode):
    import logging
    db, store, tid = _sqlite_setup(tmp_path)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    _add_column_c(db)
    if mode == "not_ok":
        _fake_drafter(monkeypatch, result={"ok": False, "error": "boom"})
    else:
        _fake_drafter(monkeypatch, raises=RuntimeError("brain down"))
    with caplog.at_level(logging.WARNING):
        res = db_scheduler.refresh_one_table(tid, actor="test")
    assert res["ok"] is True and res["drift"]["added"] == ["c"]
    row = store.get_table(tid)
    by_name = {c["name"]: c for c in row["columns"]}
    assert by_name["c"]["description"] == ""
    assert by_name["a"]["description"] == "col a"
    assert row["last_drift"]["drafted"] == []
    assert local_store.db_snapshot_path(tid).exists()
    assert any("DB_REFRESH_DRAFT_FAILED" in r.getMessage() and tid in r.getMessage()
               for r in caplog.records)


def test_no_added_column_means_no_draft_call(tmp_path, monkeypatch):
    _, store, tid = _sqlite_setup(tmp_path)
    calls = _fake_drafter(monkeypatch, result=_DRAFT_OK)
    assert db_scheduler.refresh_one_table(tid, actor="test")["ok"] is True
    assert calls == []
    by_name = {c["name"]: c for c in store.get_table(tid)["columns"]}
    assert by_name["a"]["description"] == "col a"
