"""Nightly database-snapshot refresh scheduler + shared refresh execution.

- `refresh_one_table` is the ONE refresh implementation: the admin "Refresh
  now" button and the nightly run both call it (same as `run_item_refresh` is
  shared between chat and dashboards).
- The scheduler thread is started/stopped from app.py's LIFESPAN, never at
  import — an import-time thread would leak into every pytest session (the
  local_store sweeper lesson). Tests call `run_all_due()` / `compute_next_run`
  directly.
- A refresh failure leaves the previous snapshot AND the previous
  refreshed_at in place — chats keep serving the last good data (Article IV).
- On schema drift (columns added/removed in the source), every chat meta that
  references the table is re-synced with the same carry-over rules as
  `_resync_meta_after_add` (shared `local_store.merge_schema_entry`).
- Credentials are decrypted into function-locals at the moment of use and
  never logged (Article VII).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from settings import settings
from logger_utils import log_with_sid

import local_store
import db_sources
import db_connector

_STOP = threading.Event()
_THREAD: Optional[threading.Thread] = None
_STATE_LOCK = threading.Lock()
_STATE = {"last_run_date": None, "next_run_at": None}

_LOCK_NAME = ".db_refresh.lock"


def compute_next_run(now: datetime, hhmm: str) -> datetime:
    """Pure: next occurrence of HH:MM (container-local naive time) at/after
    `now`."""
    try:
        h, m = hhmm.split(":")
        h, m = int(h), int(m)
    except Exception:
        h, m = 0, 0
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


# ---------------------------------------------------------------------------
# One-table refresh (shared by nightly run + admin Refresh-now)
# ---------------------------------------------------------------------------

def refresh_one_table(table_id: str, *, actor: str = "scheduler") -> dict:
    """Re-snapshot one registered table and re-sync drifted chat metas.
    Never raises. Returns {ok, rows?, bytes?, refreshed_at?, drift?, error?}."""
    store = db_sources.DataSourceStore()
    row = store.get_table(table_id)
    if row is None:
        return {"ok": False, "error": "Unknown table."}
    conn = store.get_connection(row.get("connection_id"), with_secret=True)
    if conn is None:
        store.mark_refreshed(table_id, error="Connection no longer exists.")
        return {"ok": False, "error": "Connection no longer exists."}
    password = db_sources.decrypt_password(conn.get("password_enc"))
    if password is None and conn.get("password_enc"):
        store.mark_refreshed(table_id, error="Stored credential cannot be read (encryption key missing or rotated).")
        return {"ok": False,
                "error": "Stored credential cannot be read — re-enter the connection password."}

    label = f"{row.get('schema') or ''}.{row.get('table_name') or ''}".strip(".")
    old_cols = [c.get("name") for c in (row.get("columns") or []) if c.get("name")]
    dest = local_store.db_snapshot_path(table_id)
    res = db_connector.snapshot_table(
        conn, password or "",
        schema=row.get("schema") or None,
        table=row.get("table_name"),
        where=row.get("where_filter") or None,
        row_cap=row.get("row_cap") or None,
        dtype_plan=row.get("dtype_plan") or None,
        dest=dest, sid=f"refresh:{actor}")
    if not res.get("ok"):
        store.mark_refreshed(table_id, error=res.get("error") or "snapshot failed")
        return {"ok": False, "error": res.get("error") or "snapshot failed"}

    fresh_cols = res.get("columns") or []
    added = [c for c in fresh_cols if c not in old_cols]
    removed = [c for c in old_cols if c not in fresh_cols]

    # Column list for the registry: keep admin-confirmed descriptions and
    # indexed flags for surviving columns; append new ones bare.
    old_by_name = {c.get("name"): c for c in (row.get("columns") or []) if c.get("name")}
    new_columns = []
    for name in fresh_cols:
        prev = old_by_name.get(name)
        if prev:
            new_columns.append(dict(prev))
        else:
            new_columns.append({"name": name, "dtype": "", "description": "",
                                "indexed": False})
    # Technical descriptions + dataset profile from the fresh snapshot (one
    # parquet read, pandas stats, no LLM). Best-effort; tech descs flow into
    # chat metas via the resync below, the profile is a sidecar next to the
    # snapshot and reaches the brain lazily at plan time.
    tech, prof = _stats_from_snapshot(dest)
    for col in new_columns:
        td = tech.get(str(col.get("name")))
        if td:
            col["technical_description"] = td
    store.mark_refreshed(table_id, rows=res.get("rows"),
                         snapshot_bytes=res.get("bytes"),
                         columns=new_columns,
                         dtype_plan=res.get("dtype_plan"))
    if prof is not None:
        try:
            st = dest.stat()
            local_store.write_profile(
                local_store.db_profile_path(table_id), prof,
                {"size": st.st_size, "mtime_ns": st.st_mtime_ns})
        except Exception as e:
            log_with_sid("db_refresh", "warning",
                         f"DB_PROFILE_WRITE_FAILED table={table_id}: {e}")

    drift = {"added": added, "removed": removed}
    if added or removed:
        log_with_sid("db_refresh", "warning",
                     f"DB_SCHEMA_DRIFT table={label} added={added} removed={removed}")
        resync_chats_for_table(table_id)
    else:
        # No structural drift — still refresh the per-chat refreshed_at cache.
        _touch_chat_refreshed_at(table_id)

    db_sources.audit(actor, "table.refresh", target=table_id,
                     detail={"table": label, "rows": res.get("rows"),
                             "drift": drift, "ok": True})
    return {"ok": True, "rows": res.get("rows"), "bytes": res.get("bytes"),
            "refreshed_at": (store.get_table(table_id) or {}).get("refreshed_at"),
            "drift": drift}


def _stats_from_snapshot(dest: Path) -> tuple[dict, Optional[dict]]:
    """({column: technical_description}, dataset profile | None) from ONE read
    of the snapshot parquet — the SAME pandas generators uploaded files use.
    Each half individually best-effort (Article IV)."""
    tech: dict = {}
    prof = None
    try:
        import pandas as pd
        from dataset_profile import _generate_technical_description, compute_profile
        df = pd.read_parquet(dest)
        total = len(df)
        try:
            tech = {str(c): _generate_technical_description(df[c], total)
                    for c in df.columns}
        except Exception as e:
            log_with_sid("db_refresh", "warning", f"DB_TECH_DESC_FAILED: {e}")
        try:
            prof = compute_profile(df)
        except Exception as e:
            log_with_sid("db_refresh", "warning", f"DB_PROFILE_COMPUTE_FAILED: {e}")
    except Exception as e:
        log_with_sid("db_refresh", "warning", f"DB_TECH_DESC_FAILED: {e}")
    return tech, prof


def _iter_chats_with_table(table_id: str):
    """Yield (chat_id, meta, entry_index) for every chat meta referencing the
    table. Walks chatdata/*/meta.json; unreadable metas are skipped."""
    chatdata = Path(settings.DATA_ROOT) / "chatdata"
    if not chatdata.is_dir():
        return
    for chat_dir in sorted(chatdata.iterdir()):
        meta_path = chat_dir / "meta.json"
        if not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for i, entry in enumerate(meta.get("files") or []):
            if (isinstance(entry, dict) and entry.get("source") == "database"
                    and (entry.get("db") or {}).get("table_id") == table_id):
                yield chat_dir.name, meta, i
                break


def _registry_meta_entry(row: dict, df_key: str, old_db_block: dict) -> dict:
    """Fresh chat-meta entry for a table from its registry row (keeps the
    chat-local df key and the frozen auto_included/relations context)."""
    fields = {}
    for col in row.get("columns") or []:
        name = col.get("name")
        if not name:
            continue
        fields[str(name)] = {
            "description": col.get("description") or "",
            "technical_description": col.get("technical_description") or "",
            "values": None,
            "indexed": bool(col.get("indexed")),
        }
    db_block = dict(old_db_block or {})
    db_block.update({
        "row_count": row.get("row_count"),
        "refreshed_at": row.get("refreshed_at"),
    })
    return {"file_name": df_key,
            "file_description": row.get("description") or "",
            "source": "database",
            "db": db_block,
            "schema": {"file_name": df_key, "fields": fields}}


def resync_chats_for_table(table_id: str) -> int:
    """Schema-drift resync: rebuild the table's entry in every chat meta with
    the `_resync_meta_after_add` carry-over rules (vanished columns deleted,
    user-edited descriptions/values kept, new columns appended). One failing
    chat never aborts the rest. Returns the number of chats updated."""
    row = db_sources.DataSourceStore().get_table(table_id)
    if row is None:
        return 0
    updated = 0
    for chat_id, meta, idx in _iter_chats_with_table(table_id):
        try:
            # Read-modify-write under the store lock so this background writer
            # can't interleave with a request-path meta write (RLock — nests
            # with write_meta's own acquisition).
            with local_store._LOCK:
                store_c = local_store.ChatDataStore(chat_id)
                meta = store_c.read_meta()  # re-read inside the lock
                entries = meta.get("files") or []
                idx = next((i for i, e in enumerate(entries)
                            if isinstance(e, dict) and e.get("source") == "database"
                            and (e.get("db") or {}).get("table_id") == table_id), None)
                if idx is None:
                    continue
                old_entry = entries[idx]
                fresh = _registry_meta_entry(row, old_entry.get("file_name"),
                                             old_entry.get("db") or {})
                meta["files"][idx] = local_store.merge_schema_entry(old_entry, fresh)
                store_c.write_meta(meta)
            updated += 1
        except Exception as e:
            log_with_sid("db_refresh", "error",
                         f"DB_RESYNC_FAILED chat_id={chat_id} table={table_id}: {e}")
    if updated:
        log_with_sid("db_refresh", "info",
                     f"DB_RESYNC_DONE table={table_id} chats={updated}")
    return updated


def _touch_chat_refreshed_at(table_id: str) -> None:
    """Refresh the display cache (db.refreshed_at / row_count) on every chat
    meta after a drift-free refresh. Best-effort."""
    row = db_sources.DataSourceStore().get_table(table_id)
    if row is None:
        return
    for chat_id, _meta, _idx in _iter_chats_with_table(table_id):
        try:
            with local_store._LOCK:  # same RMW discipline as the drift resync
                store_c = local_store.ChatDataStore(chat_id)
                meta = store_c.read_meta()
                entries = meta.get("files") or []
                idx = next((i for i, e in enumerate(entries)
                            if isinstance(e, dict) and e.get("source") == "database"
                            and (e.get("db") or {}).get("table_id") == table_id), None)
                if idx is None:
                    continue
                db_block = entries[idx].get("db") or {}
                db_block["refreshed_at"] = row.get("refreshed_at")
                db_block["row_count"] = row.get("row_count")
                meta["files"][idx]["db"] = db_block
                store_c.write_meta(meta)
        except Exception as e:
            log_with_sid("db_refresh", "warning",
                         f"DB_TOUCH_REFRESHED_AT_FAILED chat_id={chat_id}: {e}")


# ---------------------------------------------------------------------------
# Run-all + scheduler loop
# ---------------------------------------------------------------------------

def _lock_path() -> Path:
    return Path(settings.DATA_ROOT) / _LOCK_NAME


def _acquire_run_lock() -> bool:
    """O_CREAT|O_EXCL lock file so a manual refresh-all can't collide with the
    nightly run (and a future --workers N can't double-run). A stale lock
    (crashed run) is reclaimed after DB_REFRESH_LOCK_STALE_S."""
    p = _lock_path()
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"pid": os.getpid(), "started_at": time.time()}))
        return True
    except FileExistsError:
        try:
            age = time.time() - p.stat().st_mtime
            if age > int(settings.DB_REFRESH_LOCK_STALE_S):
                log_with_sid("db_refresh", "warning", f"DB_REFRESH_LOCK_STALE age_s={int(age)}")
                p.unlink()
                return _acquire_run_lock()
        except Exception:
            pass
        log_with_sid("db_refresh", "info", "DB_REFRESH_LOCK_BUSY")
        return False
    except Exception as e:
        log_with_sid("db_refresh", "warning", f"DB_REFRESH_LOCK_FAILED: {e}")
        return True  # never let the lock mechanism block refreshes entirely


def _release_run_lock() -> None:
    try:
        _lock_path().unlink()
    except Exception:
        pass


def run_all_due(*, reason: str = "schedule") -> dict:
    """Refresh every registered table SEQUENTIALLY (parallel snapshots would
    spike container RAM and hammer the customer DB at midnight). Each table
    individually try/except'd; _STOP checked between tables. The 'run once'
    body — no loop, no sleep — so tests drive it directly."""
    store = db_sources.DataSourceStore()
    if not db_sources.encryption_ready() and store.list_connections():
        log_with_sid("db_refresh", "warning", "DB_REFRESH_SKIPPED_NO_KEY")
        return {"ok": False, "error": "encryption key missing", "results": []}
    if not _acquire_run_lock():
        return {"ok": False, "error": "A refresh is already running.", "results": []}
    t0 = time.monotonic()
    results = []
    try:
        tables = store.list_tables()
        log_with_sid("db_refresh", "info",
                     f"DB_REFRESH_RUN_START reason={reason} tables={len(tables)}")
        for t in tables:
            if _STOP.is_set():
                break
            tid = t.get("id")
            try:
                res = refresh_one_table(tid, actor=f"scheduler:{reason}")
            except Exception as e:  # refresh_one_table never raises, but belt+braces
                res = {"ok": False, "error": str(e)[:300]}
            label = f"{t.get('schema') or ''}.{t.get('table_name') or ''}".strip(".")
            if res.get("ok"):
                log_with_sid("db_refresh", "info",
                             f"DB_REFRESH_TABLE_OK table={label} rows={res.get('rows')}")
            else:
                log_with_sid("db_refresh", "error",
                             f"DB_REFRESH_TABLE_FAILED table={label} err={res.get('error')}")
            results.append({"table_id": tid, "ok": bool(res.get("ok")),
                            "rows": res.get("rows"), "error": res.get("error")})
    finally:
        _release_run_lock()
    ok_n = sum(1 for r in results if r["ok"])
    summary = {"reason": reason, "ok_count": ok_n,
               "failed_count": len(results) - ok_n,
               "elapsed_s": round(time.monotonic() - t0, 1)}
    log_with_sid("db_refresh", "info",
                 f"DB_REFRESH_RUN_DONE ok={ok_n} failed={len(results) - ok_n} "
                 f"elapsed_s={summary['elapsed_s']}")
    store.record_run({**summary, "results": results})
    db_sources.audit("scheduler", "refresh.scheduled_run", detail=summary)
    return {"ok": True, "results": results, **summary}


def next_run_at() -> Optional[str]:
    with _STATE_LOCK:
        return _STATE.get("next_run_at")


def _loop() -> None:
    log_with_sid("db_refresh", "info", "DB_SCHEDULER_STARTED")
    while not _STOP.is_set():
        try:
            stg = db_sources.DataSourceStore().get_refresh_settings()
            now = datetime.now()
            nxt = compute_next_run(now, stg.get("refresh_time") or "00:00")
            with _STATE_LOCK:
                _STATE["next_run_at"] = nxt.isoformat()
            remaining = (nxt - now).total_seconds()
            # ≤60s slices: DST/NTP shifts cost at most one slice; settings
            # changes take effect without a restart.
            if _STOP.wait(timeout=min(60.0, max(1.0, remaining))):
                break
            now = datetime.now()
            today = now.date().isoformat()
            with _STATE_LOCK:
                already_ran = _STATE.get("last_run_date") == today
            if (not already_ran and stg.get("refresh_enabled")
                    and now >= nxt.replace(tzinfo=None)):
                with _STATE_LOCK:
                    _STATE["last_run_date"] = today  # backward clock jump can't double-run
                run_all_due(reason="schedule")
        except Exception as e:
            log_with_sid("db_refresh", "error", f"DB_SCHEDULER_LOOP_ERROR: {e}")
            if _STOP.wait(timeout=60.0):
                break
    log_with_sid("db_refresh", "info", "DB_SCHEDULER_STOPPED")


def start() -> None:
    global _THREAD
    if _THREAD is not None and _THREAD.is_alive():
        return
    _STOP.clear()
    _THREAD = threading.Thread(target=_loop, daemon=True, name="db_refresh_scheduler")
    _THREAD.start()


def stop(timeout: float = 5.0) -> None:
    global _THREAD
    _STOP.set()
    t = _THREAD
    if t is not None and t.is_alive():
        t.join(timeout=timeout)
    _THREAD = None
