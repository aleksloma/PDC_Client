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
    yield
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
