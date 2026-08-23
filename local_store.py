"""Client-side local storage.

Same shape and lifecycle as the B2C `storage.py`:
  - `UserStore(sid)` — temporary per-session workspace under DATA_ROOT/sessions/<sid>.
    Files dropped here by `/upload` live here until `/generate_chatdata` clones
    them into a permanent `ChatDataStore`.
  - `ChatDataStore(chat_id)` — permanent per-chat store under
    DATA_ROOT/chatdata/<chat_id>. Holds the canonical file copies, meta, and
    per-conversation JSONL.
  - `AuthStore` — email-keyed user profile + active_chats/conversations index.

Raw data files NEVER leave this server. Per AI_CONSTITUTION Article V, history
is JSONL with atomic appends.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import secrets
import shutil
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from settings import settings
from logger_utils import log_with_sid
from excel_table_detector import load_excel_sheets, _EXTRACTED_TEXT_ABOVE_TABLE  # noqa: F401 — re-exported


_LOCK = threading.RLock()


def _data_root() -> Path:
    root = Path(settings.DATA_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_email(email: str) -> str:
    return email.strip().lower().replace("/", "_").replace("\\", "_")


def _json_safe(value):
    """Recursively make a value JSON-compliant before persistence (verbatim
    port of the B2C `storage._json_safe`): Timestamp/datetime → ISO strings,
    NaT/NaN/Inf → None, numpy scalars → native, DataFrame/Series/ndarray →
    lists, tuple dict-keys → strings, and a final str() catch-all so an
    unexpected object (e.g. a pandas Styler) degrades to text instead of
    killing the write. Every history json.dumps MUST go through this."""
    import datetime as _dt
    try:
        import numpy as _np
    except Exception:
        _np = None

    if value is pd.NaT:
        return None
    if isinstance(value, pd.Timestamp):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if _np is not None and isinstance(value, _np.datetime64):
        try:
            if _np.isnat(value):
                return None
            return pd.Timestamp(value).isoformat()
        except Exception:
            return str(value)
    if isinstance(value, (_dt.datetime, _dt.date)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    if _np is not None and isinstance(value, _np.integer):
        return int(value)
    if _np is not None and isinstance(value, _np.floating):
        if _np.isnan(value) or _np.isinf(value):
            return None
        return float(value)
    if _np is not None and isinstance(value, _np.bool_):
        return bool(value)
    if isinstance(value, pd.DataFrame):
        try:
            return [_json_safe(row) for row in value.where(pd.notnull(value), None).to_dict(orient="records")]
        except Exception:
            return [_json_safe(row) for row in value.to_dict(orient="records")]
    if isinstance(value, pd.Series):
        return [_json_safe(v) for v in value.tolist()]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, dict):
        safe_dict = {}
        for k, v in value.items():
            if isinstance(k, tuple):
                safe_key = "_".join(map(str, k))
            else:
                safe_key = str(k) if not isinstance(k, (str, int, float, bool, type(None))) else k
            safe_dict[safe_key] = _json_safe(v)
        return safe_dict
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if _np is not None and isinstance(value, _np.ndarray):
        return [_json_safe(v) for v in value.tolist()]
    if isinstance(value, pd.Index):
        return [_json_safe(v) for v in value.tolist()]
    if hasattr(pd, "Categorical") and isinstance(value, pd.Categorical):
        return [_json_safe(v) for v in value.tolist()]
    # Fallback: stringify anything unrecognized (Styler, Interval, ...)
    if not isinstance(value, (str, int, float, bool, type(None))):
        try:
            return str(value)
        except Exception:
            return None
    return value


# pandas C-engine ParserError text reads "Expected N fields in line 7, saw 8" —
# the only place a bad line NUMBER is available (the tolerant python-engine
# callable receives the split fields, not a line number).
_BAD_LINE_RE = re.compile(r"line (\d+)")


def _read_csv_tolerant(path: Path, sep: str = ",") -> tuple[Optional[pd.DataFrame], Optional[dict]]:
    """Strict pd.read_csv first; on failure retry with the python engine
    skipping (and COUNTING) malformed rows. Returns:
      (df, None)                                    — clean strict parse
      (df, {"skipped_rows": n, "first_bad_line": m}) — tolerant parse with skips
      (None, {"error": reason})                     — even tolerant parse failed
    A ragged CSV (unquoted comma inside a value) must never load as an empty
    result with no signal — that produced the silent-upload-failure bug (QA 2.1).
    """
    try:
        return pd.read_csv(path, sep=sep), None
    except Exception as strict_err:
        m = _BAD_LINE_RE.search(str(strict_err))
        first_bad = int(m.group(1)) if m else None
        try:
            bad_rows: list = []
            df = pd.read_csv(path, sep=sep, engine="python",
                             on_bad_lines=lambda fields: bad_rows.append(fields) and None)
            log_with_sid(path.name, "warning",
                         f"FILE_LOAD_TOLERANT {path}: skipped={len(bad_rows)} "
                         f"first_bad_line={first_bad}")
            return df, {"skipped_rows": len(bad_rows), "first_bad_line": first_bad}
        except Exception as e:
            log_with_sid(path.name, "warning", f"FILE_LOAD_ERROR {path}: {e}")
            return None, {"error": f"{type(strict_err).__name__}: {strict_err}"}


def _load_one_file(path: Path, report: Optional[list] = None) -> dict[str, pd.DataFrame]:
    """Load one tabular file. Returns {key: df}. Excel sheets yield
    "<filename>::<sheet>" keys (single-sheet excels keep the bare filename).

    Excel files run through the validated 6-stage detection pipeline
    (`excel_table_detector.load_excel_sheets`) — same algorithm the global B2C
    app uses, so multi-row headers, ListObjects, label-vs-measure splits,
    multilingual total-row drops, and text-above-the-table extraction all work.

    `report` (optional list) collects per-file parse outcomes for the upload
    flow: {"file", "status": "warning"|"error", ...}. Callers that don't pass
    it (chat-time loads) behave exactly as before.
    """
    out: dict[str, pd.DataFrame] = {}

    def _report(row: dict) -> None:
        if report is not None:
            report.append(row)

    try:
        ext = path.suffix.lower()
        if ext in (".xls", ".xlsx", ".xlsm"):
            out.update(load_excel_sheets(path, path.name))
        elif ext in (".csv", ".tsv"):
            df, warn = _read_csv_tolerant(path, sep="\t" if ext == ".tsv" else ",")
            if df is None:
                _report({"file": path.name, "status": "error",
                         "message": "Could not parse this file."})
            elif warn is not None:
                if len(df) == 0:
                    # every data row was malformed — a header-only frame is
                    # useless, surface it as a hard failure
                    _report({"file": path.name, "status": "error",
                             "message": "No valid data rows could be parsed."})
                else:
                    out[path.name] = df
                    n = warn.get("skipped_rows")
                    _report({"file": path.name, "status": "warning",
                             "skipped_rows": n,
                             "first_bad_line": warn.get("first_bad_line"),
                             "message": f"{n} malformed row(s) skipped"})
            else:
                out[path.name] = df
        elif ext == ".json":
            out[path.name] = pd.read_json(path)
        elif ext == ".parquet":
            out[path.name] = pd.read_parquet(path)
    except Exception as e:
        log_with_sid(path.name, "warning", f"FILE_LOAD_ERROR {path}: {e}")
        _report({"file": path.name, "status": "error",
                 "message": "Could not parse this file."})
    return out


# ---------------------------------------------------------------------------
# Parquet cache for parsed tables
# ---------------------------------------------------------------------------
# Re-running the full detection pipeline (openpyxl + 6-stage table detection)
# on every question is the dominant per-question cost. The POST-detection
# dataframes are cached as parquet under `files_dir/.parquet_cache/` (a
# dot-named entry, so the `load_dataframes` iteration skips it) next to a
# per-source-file manifest keyed on the source's size + mtime_ns — Add Data
# overwrites file bytes in place, so the stat change invalidates the cache
# with no migration step. Self-healing per Article IV: any read/write failure
# logs and falls back to the existing pipeline; a broken cache can never
# break loading. Article V: lives on local disk under DATA_ROOT only.
_PARQUET_CACHE_DIRNAME = ".parquet_cache"
_PARQUET_CACHEABLE_EXTS = (".xls", ".xlsx", ".xlsm", ".csv", ".tsv", ".json")
# Bump when the PARSE pipeline changes what a cached dataframe looks like
# (column naming, detection semantics). A manifest with a different/absent
# version is a MISS, so caches written by an older parser self-heal by
# re-parsing instead of serving stale shapes forever.
# v2: column names str()-normalized + duplicate names deduped ("name.1").
# v3: tolerant CSV parse (malformed rows skipped instead of the whole file
#     failing) — previously-unparseable CSVs now yield dataframes, so older
#     caches must re-parse (QA 2.1).
_PARQUET_CACHE_PARSER_VERSION = 3


def _parquet_cache_safe_name(key: str) -> str:
    """Filesystem-safe, collision-free name for a dataframe key (which may be
    "file.xlsx::Sheet 1"): readable sanitized prefix + hash of the raw key."""
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", key)[:80]
    return f"{stem}.{digest}"


def _parquet_cache_paths(path: Path) -> tuple[Path, Path]:
    cache_dir = path.parent / _PARQUET_CACHE_DIRNAME
    manifest = cache_dir / (_parquet_cache_safe_name(path.name) + ".manifest.json")
    return cache_dir, manifest


def _parquet_cache_read(path: Path, src_size: int, src_mtime_ns: int) -> Optional[dict[str, pd.DataFrame]]:
    """Fast path: return the cached dataframes when the manifest matches the
    source file's current size + mtime_ns. None → caller runs the pipeline."""
    try:
        cache_dir, manifest_path = _parquet_cache_paths(path)
        if not manifest_path.exists():
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("src_size") != src_size or manifest.get("src_mtime_ns") != src_mtime_ns:
            return None
        if manifest.get("parser_version") != _PARQUET_CACHE_PARSER_VERSION:
            return None  # written by an older parser — re-parse and rewrite
        out: dict[str, pd.DataFrame] = {}
        for entry in manifest.get("entries", []):
            key = entry.get("key")
            pq_name, pkl_name = entry.get("parquet"), entry.get("pickle")
            if not key or not (pq_name or pkl_name):
                return None
            if pq_name:
                out[key] = pd.read_parquet(cache_dir / pq_name)
            else:
                out[key] = pd.read_pickle(cache_dir / pkl_name)
        if not out:
            return None
        log_with_sid(path.name, "info", f"PARQUET_CACHE_HIT {path.name} dfs={len(out)}")
        return out
    except Exception as e:
        log_with_sid(path.name, "warning", f"PARQUET_CACHE_READ_FAILED {path}: {e}")
        return None


def _parquet_cache_write(path: Path, dfs: dict[str, pd.DataFrame],
                         src_size: int, src_mtime_ns: int) -> None:
    """(Re)build this source file's cache. Writes are atomic (temp file +
    os.replace) so concurrent rebuilds can't corrupt each other — the rebuild
    is idempotent. Each dataframe is round-trip-verified. A dataframe parquet
    can't reproduce byte-identically (e.g. a mixed numeric/string object
    column) falls back to a PICKLE entry — pickle preserves the exact dtypes,
    so cached loads stay identical to the first load. Only when the pickle
    round-trip fails too is the dataframe logged and skipped, and then NO
    manifest is written for the source file (a partial manifest would
    silently drop the uncacheable dataframe on read)."""
    if not dfs:
        return
    try:
        cache_dir, manifest_path = _parquet_cache_paths(path)
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp_suffix = f".tmp-{os.getpid()}-{threading.get_ident()}"
        entries: list[dict] = []
        for key, df in dfs.items():
            fname = _parquet_cache_safe_name(key) + ".parquet"
            tmp = cache_dir / (fname + tmp_suffix)
            try:
                df.to_parquet(tmp, engine="pyarrow")
                back = pd.read_parquet(tmp)
                if list(back.columns) != list(df.columns) or not back.equals(df):
                    raise ValueError("parquet round-trip altered the dataframe")
                os.replace(tmp, cache_dir / fname)
                entries.append({"key": key, "parquet": fname})
                continue
            except Exception as e_pq:
                try:
                    tmp.unlink(missing_ok=True)
                except Exception:
                    pass
                pkl_name = _parquet_cache_safe_name(key) + ".pkl"
                pkl_tmp = cache_dir / (pkl_name + tmp_suffix)
                try:
                    df.to_pickle(pkl_tmp)
                    back = pd.read_pickle(pkl_tmp)
                    if list(back.columns) != list(df.columns) or not back.equals(df):
                        raise ValueError("pickle round-trip altered the dataframe")
                    os.replace(pkl_tmp, cache_dir / pkl_name)
                    entries.append({"key": key, "pickle": pkl_name})
                    log_with_sid(path.name, "info",
                                 f"PARQUET_CACHE_PICKLE_FALLBACK file={path.name} key={key}: {e_pq}")
                except Exception as e_pkl:
                    log_with_sid(path.name, "warning",
                                 f"PARQUET_CACHE_SKIP_DF file={path.name} key={key}: "
                                 f"parquet: {e_pq}; pickle: {e_pkl}")
                    try:
                        pkl_tmp.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return
        # Old manifest's cache files (parquet or pickle) that the new one no
        # longer references (e.g. renamed sheets, or a df that switched
        # formats) are removed after the swap. Non-fatal.
        stale: list[str] = []
        try:
            if manifest_path.exists():
                old = json.loads(manifest_path.read_text(encoding="utf-8"))
                new_names = {e.get("parquet") or e.get("pickle") for e in entries}
                stale = [n for e in old.get("entries", [])
                         for n in (e.get("parquet"), e.get("pickle"))
                         if n and n not in new_names]
        except Exception:
            stale = []
        manifest = {"src_size": src_size, "src_mtime_ns": src_mtime_ns,
                    "parser_version": _PARQUET_CACHE_PARSER_VERSION, "entries": entries}
        m_tmp = cache_dir / (manifest_path.name + tmp_suffix)
        m_tmp.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        os.replace(m_tmp, manifest_path)
        for fname in stale:
            try:
                (cache_dir / fname).unlink(missing_ok=True)
            except Exception:
                pass
        log_with_sid(path.name, "info", f"PARQUET_CACHE_REBUILT {path.name} dfs={len(entries)}")
    except Exception as e:
        log_with_sid(path.name, "warning", f"PARQUET_CACHE_WRITE_FAILED {path}: {e}")


def _load_one_file_cached(path: Path, report: Optional[list] = None) -> dict[str, pd.DataFrame]:
    """Parquet-cache wrapper around `_load_one_file`. The source's size +
    mtime_ns are captured BEFORE parsing, so a file overwritten mid-parse
    stamps a stale manifest that self-heals on the next load. `.parquet`
    sources are read directly — caching them would be a redundant copy.

    `report` is forwarded to `_load_one_file`; a cache HIT contributes no
    report rows (meaningful only right after a write, when the stat change
    guarantees a miss and the parse genuinely runs)."""
    if path.suffix.lower() not in _PARQUET_CACHEABLE_EXTS:
        return _load_one_file(path, report)
    try:
        st = path.stat()
        src_size, src_mtime_ns = st.st_size, st.st_mtime_ns
    except Exception as e:
        log_with_sid(path.name, "warning", f"PARQUET_CACHE_STAT_FAILED {path}: {e}")
        return _load_one_file(path, report)
    cached = _parquet_cache_read(path, src_size, src_mtime_ns)
    if cached is not None:
        return cached
    out = _load_one_file(path, report)
    if out:
        _parquet_cache_write(path, out, src_size, src_mtime_ns)
    return out


# ---------------------------------------------------------------------------
# In-memory DataFrame cache
# ---------------------------------------------------------------------------
# Same idea as the B2C storage.py `_DATAFRAME_CACHE`: questions in an active
# chat get the already-parsed dataframes from RAM instead of re-reading the
# disk cache on every request. Client-side memory-safety upgrades over the
# B2C version:
#   - every hit is validated against the source files' (name, mtime_ns, size)
#     signature, so a changed file can never serve stale data (no separate
#     invalidation hooks needed — Add Data overwrite, reset, clone all change
#     the signature or the key);
#   - the cache is bounded by an approximate BYTE budget
#     (settings.DF_CACHE_MAX_MB), not only an entry count; a dataset larger
#     than half the budget is never cached (it just reloads from the parquet
#     disk cache), so one huge upload can never pin the container's RAM;
#   - strict TTL with an active sweep on every get/set plus a background
#     sweeper thread, so an idle container releases the memory instead of
#     holding it until the next request.
# The app runs a single uvicorn worker, so one module-level cache is shared
# by all requests. Self-healing per Article IV: any cache failure logs and
# falls back to the uncached load path.

class LRUTTLCache:
    """Thread-safe LRU cache with TTL expiration and an approximate byte budget."""

    def __init__(self, max_size: int = 8, ttl_seconds: float = 300,
                 max_bytes: Optional[int] = None):
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._max_bytes = max_bytes
        # {key: {"value": ..., "size": int, "timestamp": float, "access_time": float}}
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, key: str):
        """Return the value if present and not expired; sweeps ALL expired
        entries first so idle data is freed on any cache traffic."""
        with self._lock:
            self._evict_expired_locked(_time.time())
            entry = self._cache.get(key)
            if entry is None:
                return None
            entry["access_time"] = _time.time()
            return entry["value"]

    def set(self, key: str, value, size_bytes: int = 0) -> bool:
        """Insert value. Returns False (not cached) when the entry alone would
        occupy more than half the byte budget. Evicts expired entries, then
        LRU entries until both the entry count and byte budget fit."""
        with self._lock:
            now = _time.time()
            self._evict_expired_locked(now)
            if self._max_bytes is not None and size_bytes > self._max_bytes // 2:
                self._cache.pop(key, None)
                return False
            self._cache.pop(key, None)
            while self._cache and (
                len(self._cache) >= self._max_size
                or (self._max_bytes is not None
                    and self._total_bytes_locked() + size_bytes > self._max_bytes)
            ):
                self._evict_lru_locked()
            self._cache[key] = {"value": value, "size": int(size_bytes),
                                "timestamp": now, "access_time": now}
            return True

    def sweep(self):
        """Drop every TTL-expired entry (called by the background sweeper)."""
        with self._lock:
            self._evict_expired_locked(_time.time())

    def invalidate(self, key: str = None):
        with self._lock:
            if key is None:
                self._cache.clear()
            else:
                self._cache.pop(key, None)

    def _total_bytes_locked(self) -> int:
        return sum(e["size"] for e in self._cache.values())

    def _evict_expired_locked(self, now: float):
        expired = [k for k, v in self._cache.items()
                   if now - v["timestamp"] > self._ttl_seconds]
        for k in expired:
            del self._cache[k]

    def _evict_lru_locked(self):
        if not self._cache:
            return
        lru_key = min(self._cache.keys(), key=lambda k: self._cache[k]["access_time"])
        del self._cache[lru_key]

    def __len__(self):
        with self._lock:
            return len(self._cache)


_DF_CACHE_TTL_SECONDS = 300
_DF_CACHE_SWEEP_SECONDS = 60
_DATAFRAME_CACHE = LRUTTLCache(
    max_size=8,
    ttl_seconds=_DF_CACHE_TTL_SECONDS,
    max_bytes=max(1, int(settings.DF_CACHE_MAX_MB)) * 1024 * 1024,
)


def _df_cache_sweeper():
    while True:
        _time.sleep(_DF_CACHE_SWEEP_SECONDS)
        try:
            _DATAFRAME_CACHE.sweep()
        except Exception as e:
            log_with_sid("df_cache", "warning", f"DF_CACHE_SWEEP_FAILED: {e}")


threading.Thread(target=_df_cache_sweeper, daemon=True, name="df_cache_sweeper").start()


def _files_signature(files_dir: Path) -> Optional[tuple]:
    """Sorted (name, mtime_ns, size) per source file — the freshness proof for
    a memory-cache hit. None on any failure → caller treats it as a miss and
    never caches."""
    try:
        sig = []
        for fp in sorted(files_dir.iterdir()):
            if fp.is_file() and not fp.name.startswith("."):
                st = fp.stat()
                sig.append((fp.name, st.st_mtime_ns, st.st_size))
        return tuple(sig)
    except Exception as e:
        log_with_sid(str(files_dir), "warning", f"DF_CACHE_SIGNATURE_FAILED: {e}")
        return None


def _dfs_size_bytes(dfs: dict[str, pd.DataFrame]) -> Optional[int]:
    """Approximate in-memory footprint of a dfs dict. Large frames are
    estimated from a head sample — memory_usage(deep=True) walks every Python
    object in object-dtype columns, which would add real latency to the cache
    fill on multi-million-cell frames just to feed an approximate budget.
    None on failure → the entry is simply not cached (safe fallback, Art. IV)."""
    try:
        total = 0
        for df in dfs.values():
            n = len(df)
            if n > 100_000:
                sample = df.head(10_000)
                total += int(sample.memory_usage(deep=True).sum()) * n // max(1, len(sample))
            else:
                total += int(df.memory_usage(deep=True).sum())
        return int(total)
    except Exception as e:
        log_with_sid("df_cache", "warning", f"DF_CACHE_SIZE_FAILED: {e}")
        return None


# ---------------------------------------------------------------------------
# Database-table snapshots (admin-registered "Data sources")
# ---------------------------------------------------------------------------
# A chat that uses registered database tables carries meta entries with
# source == "database". The dataframe bytes live in ONE central snapshot per
# table at DATA_ROOT/db_snapshots/{table_id}.parquet (written atomically by
# db_connector.snapshot_table; refreshed nightly by db_scheduler). Chat meta
# references only the table_id — never a path — so a volume remount or a
# refresh can never break a chat. The df key visible to users and generated
# code is the DISPLAY NAME (entry["file_name"]), same indirection the
# "file.xlsx::Sheet" keys already use.
_DB_SNAPSHOT_DIRNAME = "db_snapshots"
_DB_TABLE_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")


def db_snapshot_path(table_id: str) -> Path:
    """Canonical snapshot location for a registered table (id regex-guarded —
    the filename derives from it, so this is path-traversal safety)."""
    if not (isinstance(table_id, str) and _DB_TABLE_ID_RE.match(table_id)):
        raise ValueError("invalid table id")
    d = _data_root() / _DB_SNAPSHOT_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{table_id}.parquet"


def db_entries_from_meta(meta) -> list[dict]:
    """The meta entries backed by a database snapshot. Absent `source` ⇒ file
    (every pre-feature meta.json keeps loading unchanged)."""
    if not isinstance(meta, dict):
        return []
    return [e for e in (meta.get("files") or [])
            if isinstance(e, dict) and e.get("source") == "database"]


def _db_signature(db_entries) -> tuple:
    """(df_key, table_id, mtime_ns, size) per snapshot — the DB half of the
    memory-cache freshness proof. A missing snapshot contributes (None, None)
    so its appearance/disappearance also flips the signature."""
    sig = []
    for entry in sorted(db_entries or [], key=lambda e: e.get("file_name") or ""):
        key = entry.get("file_name")
        tid = (entry.get("db") or {}).get("table_id")
        try:
            st = db_snapshot_path(tid).stat()
            sig.append((key, tid, st.st_mtime_ns, st.st_size))
        except Exception:
            sig.append((key, tid, None, None))
    return tuple(sig)


def _load_db_snapshots(db_entries: list, owner: str) -> dict[str, pd.DataFrame]:
    """Read each entry's central snapshot parquet, keyed by display name.
    A missing/unreadable snapshot is SKIPPED (logged) — the chat still answers
    on its remaining frames (Article IV); the frontend's key-freeze machinery
    disables items that referenced the vanished key.

    String columns are served as plain object dtype — the registry's
    dtype_plan 'category' entries are deliberately NOT applied here: pandas
    (< 3.0) groupby on categoricals defaults to observed=False, so generated
    code grouping by a dimension column would emit the full cartesian product
    of ALL categories (every city × every product, mostly NaN) and plotly
    would put every category on the axis."""
    out: dict[str, pd.DataFrame] = {}
    for entry in db_entries or []:
        key = entry.get("file_name")
        tid = (entry.get("db") or {}).get("table_id")
        if not key or not tid:
            continue
        try:
            path = db_snapshot_path(tid)
        except ValueError:
            log_with_sid(owner, "warning", f"DB_SNAPSHOT_BAD_ID key={key}")
            continue
        if not path.exists():
            log_with_sid(owner, "warning", f"DB_SNAPSHOT_MISSING table={tid} key={key}")
            continue
        try:
            out[key] = pd.read_parquet(path)
        except Exception as e:
            log_with_sid(owner, "warning", f"DB_SNAPSHOT_LOAD_FAILED table={tid}: {e}")
    return out


def unique_df_key(existing_keys, display_name: str) -> str:
    """Collision-free df key for a DB table's display name against the keys
    already present in a meta (uploaded files or other tables). Resolved at
    META-WRITE time, never at load time — file_name IS the df key."""
    base = (display_name or "table").strip() or "table"
    existing = set(existing_keys or ())
    if base not in existing:
        return base
    n = 2
    while f"{base} ({n})" in existing:
        n += 1
    return f"{base} ({n})"


def merge_schema_entry(old_entry: dict, fresh_entry: dict) -> dict:
    """Carry user-editable content from an old meta entry onto a fresh one:
    non-empty old file_description wins; per column present in BOTH, the old
    description + values map win; everything else (new columns, dtypes,
    technical_description) comes from the fresh entry. Shared by the Add Data
    overwrite resync, the DB drift resync, and the Add Data DB re-merge —
    one source of truth for 'edits must survive'."""
    import copy as _copy

    fresh = _copy.deepcopy(fresh_entry)
    key = fresh.get("file_name") or old_entry.get("file_name")
    old_fd = (old_entry.get("file_description") or "").strip()
    if old_fd:
        fresh["file_description"] = old_fd
    old_schema = old_entry.get("schema") if isinstance(old_entry.get("schema"), dict) else {}
    old_fields = (old_schema.get("fields") or {}) if isinstance(old_schema.get("fields"), dict) else {}
    if not isinstance(fresh.get("schema"), dict):
        fresh["schema"] = {"file_name": key, "fields": {}}
    new_fields = fresh["schema"].setdefault("fields", {})
    for col, old_f in old_fields.items():
        if col not in new_fields or not isinstance(old_f, dict):
            continue
        if not isinstance(new_fields[col], dict):
            new_fields[col] = {}
        old_desc = old_f.get("description")
        if isinstance(old_desc, str) and old_desc.strip():
            new_fields[col]["description"] = old_desc
        old_vals = old_f.get("values")
        if isinstance(old_vals, dict) and old_vals:
            new_fields[col]["values"] = old_vals
    return fresh


def _load_dataframes_cached(files_dir: Path, owner: str,
                            db_entries: Optional[list] = None) -> dict[str, pd.DataFrame]:
    """Shared load path for UserStore/ChatDataStore: signature-validated
    in-memory cache in front of the per-file parquet disk cache.

    `db_entries` are the meta entries with source == "database" — their
    central snapshot parquets (DATA_ROOT/db_snapshots/) are merged into the
    result keyed by DISPLAY NAME, and their (mtime_ns, size) join the cache
    signature so a re-snapshot invalidates the memory cache exactly like an
    overwritten upload. Empty/None → behavior is byte-identical to before.

    ISOLATION INVARIANT: the cache never shares DataFrame objects with any
    caller. Executed LLM code mutates dataframes in place (dropna(inplace=
    True), column assignment, ...), and before this cache existed every
    request got fresh objects from disk — so the cache stores its own copies
    at set() and hands out copies at get(). A df.copy() is a memcpy-fast
    operation, orders of magnitude cheaper than the disk reload it saves."""
    key = str(files_dir)
    files_sig = _files_signature(files_dir)
    sig = None if files_sig is None else (files_sig, _db_signature(db_entries))
    if sig is not None:
        try:
            entry = _DATAFRAME_CACHE.get(key)
            if entry is not None and entry.get("sig") == sig:
                log_with_sid(owner, "info", f"DF_MEMORY_CACHE_HIT dfs={len(entry['dfs'])}")
                return {k: df.copy() for k, df in entry["dfs"].items()}
        except Exception as e:
            log_with_sid(owner, "warning", f"DF_MEMORY_CACHE_GET_FAILED: {e}")
    dfs: dict[str, pd.DataFrame] = {}
    for fp in sorted(files_dir.iterdir()):
        if fp.is_file() and not fp.name.startswith("."):
            dfs.update(_load_one_file_cached(fp))
    if db_entries:
        dfs.update(_load_db_snapshots(db_entries, owner))
    if sig is not None and dfs:
        try:
            size = _dfs_size_bytes(dfs)
            if size is not None:
                cached = _DATAFRAME_CACHE.set(
                    key,
                    {"sig": sig, "dfs": {k: df.copy() for k, df in dfs.items()}},
                    size_bytes=size)
                if not cached:
                    log_with_sid(owner, "info",
                                 f"DF_MEMORY_CACHE_TOO_LARGE size_mb={size // (1024 * 1024)}")
        except Exception as e:
            log_with_sid(owner, "warning", f"DF_MEMORY_CACHE_SET_FAILED: {e}")
    return dfs


# ---------------------------------------------------------------------------
# AuthStore — user profile (email-only)
# ---------------------------------------------------------------------------
class AuthStore:
    """Email-only profile storage. The enterprise build has no password,
    no subscription plan upgrades, etc. — just the email as identifier."""

    def ensure_user(self, email: str) -> dict:
        email = _safe_email(email)
        udir = _data_root() / "users" / email
        udir.mkdir(parents=True, exist_ok=True)
        profile_path = udir / "profile.json"
        if profile_path.exists():
            try:
                return json.loads(profile_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        profile = {"email": email, "created_at": _now()}
        profile_path.write_text(json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8")
        log_with_sid(email, "info", "USER_PROFILE_CREATED")
        return profile

    # --- Password auth (hash-only storage under users/{email}/auth.json) ----
    #
    # Two hash slots:
    #   password_hash       — the user's own password.
    #   temp_password_hash  — a reset-issued temporary password (must_change).
    # A login with the temp password forces a password change; a login with
    # the primary password invalidates any outstanding temp password (so a
    # third party requesting resets can never lock the real user out).
    # Legacy users from the email-only build simply have no auth.json — their
    # next login is treated like a first visit (entered password gets set).

    def _auth_path(self, email: str) -> Path:
        return _data_root() / "users" / _safe_email(email) / "auth.json"

    def user_exists(self, email: str) -> bool:
        """True when this email has a local profile (any prior login)."""
        udir = _data_root() / "users" / _safe_email(email)
        return (udir / "profile.json").exists() or (udir / "auth.json").exists()

    def get_auth(self, email: str) -> dict:
        p = self._auth_path(email)
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log_with_sid(email, "error", f"AUTH_READ_FAILED: {e}")
            return {}

    def _write_auth(self, email: str, auth: dict) -> None:
        p = self._auth_path(email)
        p.parent.mkdir(parents=True, exist_ok=True)
        auth["updated_at"] = _now()
        p.write_text(json.dumps(auth, indent=2, ensure_ascii=False), encoding="utf-8")

    def has_password(self, email: str) -> bool:
        return bool(self.get_auth(email).get("password_hash"))

    def set_password(self, email: str, password: str, *, force_change: bool = False) -> None:
        """Set the user's own password. Clears any outstanding temp password.
        force_change=True (used only by the ladmin bootstrap) keeps the
        must_change_password flag ON so the first login forces a change."""
        from password_utils import generate_password_hash
        with _LOCK:
            auth = self.get_auth(email)
            auth["password_hash"] = generate_password_hash(password)
            auth.pop("temp_password_hash", None)
            auth["must_change_password"] = bool(force_change)
            self._write_auth(email, auth)
        log_with_sid(email, "info", "USER_PASSWORD_SET")

    # --- Roles (stored on profile.json — the identity record; auth.json is
    # credentials-only and is rewritten wholesale on password churn) ---------
    #
    # Default "user"; the fixed local admin is "admin". The field lives on the
    # local user record so roles can later be assigned from the brain side
    # without a storage migration. Legacy profiles without the key read as
    # "user" (stored-shape backward compatibility).

    def get_role(self, email: str) -> str:
        return (self.get_profile(email) or {}).get("role") or "user"

    def set_role(self, email: str, role: str) -> None:
        email = _safe_email(email)
        with _LOCK:
            prof = self.get_profile(email) or {"email": email, "created_at": _now()}
            if prof.get("role") == role:
                return
            prof["role"] = role
            p = _data_root() / "users" / email / "profile.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(prof, indent=2, ensure_ascii=False), encoding="utf-8")
        log_with_sid(email, "info", f"USER_ROLE_SET role={role}")

    def is_admin(self, email: str) -> bool:
        return self.get_role(email) == "admin"

    # --- Data role (roles_store.py role id; gates DB-table access) ----------
    #
    # Same storage decision as `role` above: profile.json, not auth.json.
    # Missing/dangling ids resolve to the built-in Base role dynamically
    # (roles_store.role_for_email) — deleting a role needs no profile rewrites
    # and an old-shape profile keeps loading (stored-shape backward compat).

    def get_data_role(self, email: str) -> str:
        return (self.get_profile(email) or {}).get("data_role") or "base"

    def set_data_role(self, email: str, role_id: str) -> None:
        email = _safe_email(email)
        with _LOCK:
            prof = self.get_profile(email) or {"email": email, "created_at": _now()}
            if prof.get("data_role") == role_id:
                return
            prof["data_role"] = role_id
            p = _data_root() / "users" / email / "profile.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(p, prof)
        log_with_sid(email, "info", f"USER_DATA_ROLE_SET role_id={role_id}")

    def touch_last_login(self, email: str) -> None:
        """Stamp last_login_at on the profile. Best-effort — a failed stamp
        must never break a login (Article IV)."""
        try:
            email = _safe_email(email)
            with _LOCK:
                prof = self.get_profile(email) or {"email": email, "created_at": _now()}
                prof["last_login_at"] = _now()
                p = _data_root() / "users" / email / "profile.json"
                p.parent.mkdir(parents=True, exist_ok=True)
                _write_json_atomic(p, prof)
        except Exception as e:
            log_with_sid(email, "warning", f"LAST_LOGIN_STAMP_FAILED: {e}")

    def list_users(self) -> list:
        """Every non-admin user with a readable profile.json, sorted by email.
        Skips the config-only local admin (and any role=admin profile) — the
        admin Users window manages regular users only. Unreadable profiles are
        skipped, never fatal (Article IV)."""
        users_dir = _data_root() / "users"
        if not users_dir.exists():
            return []
        ladmin = (settings.LOCAL_ADMIN_USERNAME or "").strip().lower()
        rows = []
        for udir in users_dir.iterdir():
            try:
                if not udir.is_dir() or udir.name == ladmin:
                    continue
                p = udir / "profile.json"
                if not p.exists():
                    continue
                prof = json.loads(p.read_text(encoding="utf-8"))
                if not isinstance(prof, dict) or prof.get("role") == "admin":
                    continue
                email = prof.get("email") or udir.name
                rows.append({
                    "email": email,
                    "data_role": prof.get("data_role") or "base",
                    "created_at": prof.get("created_at"),
                    "last_login_at": prof.get("last_login_at"),
                })
            except Exception as e:
                log_with_sid("admin", "warning", f"LIST_USERS_SKIP dir={udir.name}: {e}")
                continue
        rows.sort(key=lambda r: r["email"])
        return rows

    def ensure_local_admin(self) -> None:
        """Idempotent boot-time bootstrap of the fixed local admin account
        (settings.LOCAL_ADMIN_USERNAME, default "ladmin"). Only a HASH is ever
        stored; the bootstrap password forces a change on first login. An
        EXISTING password hash is never overwritten — a LOCAL_ADMIN_PASSWORD
        left in the env must not re-assert itself (that would be a permanent
        backdoor). Recovery: delete users/ladmin/auth.json and restart."""
        try:
            uname = (settings.LOCAL_ADMIN_USERNAME or "").strip().lower()
            if not uname:
                log_with_sid("startup", "info", "LADMIN_DISABLED")
                return
            self.ensure_user(uname)
            self.set_role(uname, "admin")
            auth = self.get_auth(uname)
            if auth.get("password_hash") or auth.get("temp_password_hash"):
                log_with_sid("startup", "info", "LADMIN_PRESENT")
                return
            boot_pw = settings.LOCAL_ADMIN_PASSWORD or ""
            if not boot_pw:
                log_with_sid("startup", "warning", "LADMIN_BOOTSTRAP_SKIPPED_NO_PASSWORD")
                return
            self.set_password(uname, boot_pw, force_change=True)
            log_with_sid("startup", "info", "LADMIN_BOOTSTRAP_CREATED")
        except Exception as e:
            log_with_sid("startup", "error", f"LADMIN_BOOTSTRAP_FAILED: {e}")

    def set_temp_password(self, email: str, temp_password: str) -> None:
        """Store a reset-issued temp password hash + must_change flag. The
        user's own password (if any) stays valid until the temp one is used."""
        from password_utils import generate_password_hash
        with _LOCK:
            auth = self.get_auth(email)
            auth["temp_password_hash"] = generate_password_hash(temp_password)
            auth["must_change_password"] = True
            self._write_auth(email, auth)
        log_with_sid(email, "info", "USER_TEMP_PASSWORD_SET")

    def clear_temp_password(self, email: str) -> None:
        with _LOCK:
            auth = self.get_auth(email)
            if auth.pop("temp_password_hash", None) is not None or auth.get("must_change_password"):
                auth["must_change_password"] = False
                self._write_auth(email, auth)

    def verify_password(self, email: str, password: str) -> Optional[str]:
        """Check a login attempt. Returns:
          "ok"   — matched the user's own password (any temp is invalidated),
          "temp" — matched an outstanding temporary password (force change),
          None   — no match.
        """
        from password_utils import check_password_hash
        auth = self.get_auth(email)
        if auth.get("password_hash") and check_password_hash(auth["password_hash"], password):
            if auth.get("temp_password_hash"):
                self.clear_temp_password(email)
            return "ok"
        if auth.get("temp_password_hash") and check_password_hash(auth["temp_password_hash"], password):
            return "temp"
        return None

    def get_profile(self, email: str) -> Optional[dict]:
        email = _safe_email(email)
        p = _data_root() / "users" / email / "profile.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    def update_profile(self, email: str, *, new_email: str = None) -> dict:
        """Only the email is editable. Anything else is ignored."""
        with _LOCK:
            email = _safe_email(email)
            prof = self.get_profile(email) or {"created_at": _now()}
            if new_email and _safe_email(new_email) != email:
                # Cannot change identity — the email IS the identity.
                # Silently ignore to avoid breaking the page.
                pass
            prof["email"] = email
            (_data_root() / "users" / email / "profile.json").write_text(
                json.dumps(prof, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            return prof

    def list_active_chats(self, email: str) -> list[dict]:
        email = _safe_email(email)
        p = _data_root() / "users" / email / "active_chats.jsonl"
        if not p.exists():
            return []
        out = []
        for idx, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
            except Exception:
                continue
            row.setdefault("_seq", idx)   # stable file-order tiebreak
            out.append(row)
        # Pinned chats first, then newest first (descending created_at). Rows
        # missing created_at sort to the bottom (treated as oldest); file order
        # breaks ties. Rows without a `pinned` key (every pre-feature row)
        # sort exactly as before. Stable so the rest of the app sees a single
        # consistent ordering.
        out.sort(key=lambda r: (1 if r.get("pinned") else 0,
                                r.get("created_at") or "", r.get("_seq", 0)),
                 reverse=True)
        for r in out:
            r.pop("_seq", None)
        return out

    def list_conversations(self, email: str) -> list[dict]:
        email = _safe_email(email)
        p = _data_root() / "users" / email / "conversations.jsonl"
        if not p.exists():
            return []
        out = []
        for idx, line in enumerate(p.read_text(encoding="utf-8").splitlines()):
            try:
                row = json.loads(line)
            except Exception:
                continue
            row.setdefault("_seq", idx)
            out.append(row)
        # Same newest-first ordering as active chats (descending by updated_at,
        # then created_at; file order breaks ties). Missing timestamps sort last.
        out.sort(key=lambda r: (r.get("updated_at") or r.get("created_at") or "",
                                r.get("_seq", 0)),
                 reverse=True)
        for r in out:
            r.pop("_seq", None)
        return out

    def get_active_chat_names(self, email: str) -> set[str]:
        return {row.get("title", "") for row in self.list_active_chats(email) if row.get("title")}

    def record_active_chat(self, email: str, chat_id: str, title: str, files: list[str]) -> None:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "active_chats.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "chat_id": chat_id, "title": title, "files": files,
                    "created_at": _now(),
                }, ensure_ascii=False) + "\n")

    def record_conversation(self, email: str, chat_id: str, conv_id: str, title: str,
                             *, shared_by: str = "") -> None:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "conversations.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "conv_id": conv_id, "chat_id": chat_id, "title": title,
                "created_at": _now(),
            }
            if shared_by:
                row["shared_by"] = shared_by
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    def record_shared_chat(self, email: str, chat_id: str, title: str,
                            files: list[str], shared_by: str) -> None:
        """Record a chat in the recipient's active_chats so they can open it."""
        with _LOCK:
            email = _safe_email(email)
            existing_ids = {row.get("chat_id") for row in self.list_active_chats(email)}
            if chat_id in existing_ids:
                return
            p = _data_root() / "users" / email / "active_chats.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "chat_id": chat_id, "title": title, "files": files,
                    "created_at": _now(),
                    "shared_by": shared_by,
                }, ensure_ascii=False) + "\n")

    def rename_active_chat(self, email: str, chat_id: str, new_title: str) -> bool:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "active_chats.jsonl"
            if not p.exists():
                return False
            out_lines = []
            found = False
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("chat_id") == chat_id:
                        rec["title"] = new_title
                        found = True
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            return found

    def set_chat_pinned(self, email: str, chat_id: str, pinned: bool) -> bool:
        """Pin/unpin one of the user's active chats (QA 3.2). Additive flag on
        the jsonl row — absent means unpinned, so old-shape rows and readers
        are unaffected. Same rewrite idiom as rename_active_chat."""
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "active_chats.jsonl"
            if not p.exists():
                return False
            out_lines = []
            found = False
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("chat_id") == chat_id:
                        if pinned:
                            rec["pinned"] = True
                        else:
                            rec.pop("pinned", None)
                        found = True
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            return found

    def update_active_chat_files(self, email: str, chat_id: str, files: list[str]) -> bool:
        """Refresh the `files` list on a chat's active_chats record (used after
        Add Data so the sidebar subtitle reflects the current file set)."""
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "active_chats.jsonl"
            if not p.exists():
                return False
            out_lines = []
            found = False
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("chat_id") == chat_id:
                        rec["files"] = files
                        found = True
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            return found

    def rename_conversation(self, email: str, conv_id: str, new_title: str) -> bool:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "conversations.jsonl"
            if not p.exists():
                return False
            out_lines = []
            found = False
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("conv_id") == conv_id:
                        rec["title"] = new_title
                        found = True
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            return found

    def delete_conversation(self, email: str, conv_id: str) -> bool:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "conversations.jsonl"
            if not p.exists():
                return False
            out_lines = []
            removed = False
            chat_id = None
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("conv_id") == conv_id:
                        removed = True
                        chat_id = rec.get("chat_id")
                        continue
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            # Remove the actual JSONL too
            if chat_id:
                conv_path = _data_root() / "chatdata" / chat_id / "conversations" / f"{conv_id}.jsonl"
                try:
                    if conv_path.exists():
                        conv_path.unlink()
                except Exception:
                    pass
            return removed

    def deactivate_chat(self, email: str, chat_id: str) -> bool:
        with _LOCK:
            email = _safe_email(email)
            p = _data_root() / "users" / email / "active_chats.jsonl"
            if not p.exists():
                return False
            out_lines = []
            removed = False
            for line in p.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("chat_id") == chat_id:
                        removed = True
                        continue
                    out_lines.append(json.dumps(rec, ensure_ascii=False))
                except Exception:
                    out_lines.append(line)
            p.write_text("\n".join(out_lines) + ("\n" if out_lines else ""), encoding="utf-8")
            return removed


# ---------------------------------------------------------------------------
# UserStore — per-session temp area (mirror of B2C UserStore)
# ---------------------------------------------------------------------------
class UserStore:
    """Per-session temporary workspace.

    SID is the session id used by `dashboard.js` for the upload flow. Files
    live here only between `/upload` and `/generate_chatdata`. After that
    they are cloned into a permanent `ChatDataStore`.
    """

    def __init__(self, sid: str):
        self.sid = sid
        self.root = _data_root() / "sessions" / sid
        self.files_dir = self.root / "files"
        self.meta_path = self.root / "meta.json"
        self._ensure_layout()

    def _ensure_layout(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            self.meta_path.write_text(json.dumps({"files": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    def reset_all(self):
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self._ensure_layout()

    def save_upload(self, filename: str, content: bytes) -> Path:
        out = self.files_dir / filename
        out.write_bytes(content)
        meta = self.read_meta()
        if filename not in [f.get("file_name") for f in meta.get("files", [])]:
            meta.setdefault("files", []).append({"file_name": filename, "schema": {}})
            self.write_meta(meta)
        return out

    def read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {"files": []}

    def write_meta(self, meta: dict):
        # _json_safe: schema fields are keyed by COLUMN NAMES, which can be
        # non-string objects on messy sheets — normalize keys+values or the
        # write raises and the upload 500s (same net as the history writers).
        self.meta_path.write_text(json.dumps(_json_safe(meta), ensure_ascii=False, indent=2), encoding="utf-8")

    def load_dataframes(self, include_db: bool = True) -> dict[str, pd.DataFrame]:
        """include_db=False is the session-stage fast path (upload/autofill/
        schema endpoints must not read multi-million-row snapshots just to
        list keys)."""
        db = db_entries_from_meta(self.read_meta()) if include_db else None
        return _load_dataframes_cached(self.files_dir, self.sid, db)

    def load_dataframes_with_report(self) -> tuple[dict[str, pd.DataFrame], list[dict]]:
        """Upload-flow variant: parse the session's FILES (no DB snapshots)
        and return (dfs, report) where report lists per-file parse warnings /
        errors from `_load_one_file`. Deliberately bypasses the in-memory
        `_DATAFRAME_CACHE` (it cannot carry a report); the parquet disk cache
        still applies, and right after `save_upload` the stat change makes it
        miss, so the parse genuinely runs and the report is complete (QA 2.1)."""
        report: list[dict] = []
        dfs: dict[str, pd.DataFrame] = {}
        try:
            for fp in sorted(self.files_dir.iterdir()):
                if fp.is_file() and not fp.name.startswith("."):
                    dfs.update(_load_one_file_cached(fp, report))
        except Exception as e:
            log_with_sid(self.sid, "warning", f"UPLOAD_LOAD_REPORT_FAILED: {e}")
        return dfs, report


# ---------------------------------------------------------------------------
# ChatDataStore — permanent per-chat workspace
# ---------------------------------------------------------------------------
class ChatDataStore:
    """Per-chat permanent workspace under chatdata/<chat_id>."""

    def __init__(self, chat_id: str):
        self.chat_id = chat_id
        self.root = _data_root() / "chatdata" / chat_id
        self.files_dir = self.root / "files"
        self.meta_path = self.root / "meta.json"
        self.conversations_dir = self.root / "conversations"
        self._ensure_layout()

    def _ensure_layout(self):
        self.root.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.conversations_dir.mkdir(parents=True, exist_ok=True)
        if not self.meta_path.exists():
            self.meta_path.write_text(json.dumps({"files": []}, ensure_ascii=False, indent=2), encoding="utf-8")

    def clone_from_user_store(self, user_store: UserStore) -> None:
        # Database-table entries (source == "database") are meta-only — they
        # reference the central DATA_ROOT/db_snapshots/ parquet by table_id,
        # so the verbatim meta copy below carries them with zero extra work.
        for fp in user_store.files_dir.iterdir():
            if fp.is_file() and not fp.name.startswith("."):
                shutil.copy2(fp, self.files_dir / fp.name)
        # Also carry over the parsed-table disk cache: copy2 preserves
        # mtime_ns, so the manifests (keyed on source size+mtime_ns) stay
        # valid in the new chat store and the first question skips the
        # parse entirely. Best-effort — a failed copy just re-parses.
        try:
            src_cache = user_store.files_dir / _PARQUET_CACHE_DIRNAME
            if src_cache.is_dir():
                shutil.copytree(src_cache, self.files_dir / _PARQUET_CACHE_DIRNAME,
                                dirs_exist_ok=True)
        except Exception as e:
            log_with_sid(self.chat_id, "warning", f"PARQUET_CACHE_CLONE_FAILED: {e}")
        # Copy meta wholesale (already contains schema entries)
        try:
            self.meta_path.write_text(user_store.meta_path.read_text(encoding="utf-8"), encoding="utf-8")
        except Exception:
            pass

    def read_meta(self) -> dict:
        try:
            return json.loads(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return {"files": []}

    def write_meta(self, meta: dict):
        # Same _json_safe net as UserStore.write_meta (non-string schema keys).
        # ATOMIC (tmp + os.replace) under _LOCK: since the nightly DB-snapshot
        # resync (db_scheduler) rewrites chat metas OUTSIDE request context, a
        # plain write_text could leave a truncated meta.json on a crash and
        # race a concurrent request-path write. Chat meta is customer state —
        # it must never be torn (CLAUDE.md data safety).
        with _LOCK:
            _write_json_atomic(self.meta_path, meta)

    def set_welcome(self, msg: str):
        (self.root / "welcome.txt").write_text(msg or "", encoding="utf-8")

    def get_welcome(self) -> str:
        p = self.root / "welcome.txt"
        if not p.exists():
            return ""
        try:
            return p.read_text(encoding="utf-8")
        except Exception:
            return ""

    def set_suggested_questions(self, questions: list[str]):
        (self.root / "suggested_questions.json").write_text(
            json.dumps(questions or [], ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def get_suggested_questions(self) -> list[str]:
        p = self.root / "suggested_questions.json"
        if not p.exists():
            return []
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return []

    def load_dataframes(self, include_db: bool = True) -> dict[str, pd.DataFrame]:
        db = db_entries_from_meta(self.read_meta()) if include_db else None
        return _load_dataframes_cached(self.files_dir, self.chat_id, db)

    def schema_docs(self) -> dict:
        """Build {filename: {file_description, fields: {col: {...}}}} for the brain.

        Entries with source == "database" additionally carry source/db_table/
        refreshed_at/relations so schema_builder can render the DB source line
        and the declared-join-keys block. File entries emit EXACTLY the two
        keys they always did — schema_text stays byte-identical for any chat
        with no database tables."""
        out = {}
        for file_entry in self.read_meta().get("files", []) or []:
            name = file_entry.get("file_name")
            if not name:
                continue
            schema = file_entry.get("schema") or {}
            out[name] = {
                "file_description": file_entry.get("file_description", ""),
                "fields": (schema.get("fields") if isinstance(schema, dict) else {}) or {},
            }
            if file_entry.get("source") == "database":
                db = file_entry.get("db") or {}
                out[name]["source"] = "database"
                out[name]["db_table"] = f"{db.get('schema') or ''}.{db.get('table_name') or ''}".strip(".")
                out[name]["refreshed_at"] = db.get("refreshed_at")
                out[name]["relations"] = db.get("relations") or []
        return out

    def new_conversation(self, title: str = "New conversation") -> str:
        conv_id = "cv_" + secrets.token_hex(8)
        (self.conversations_dir / f"{conv_id}.jsonl").touch()
        return conv_id

    def copy_conv_to_new(self, source_conv_id: str) -> str:
        """Snapshot copy of a conversation into a fresh conv_id, used for
        conversation-level sharing. Verbatim port of global
        `storage.ChatDataStore.copy_conv_to_new`.

        Returns the new conv_id. After this point both conversations evolve
        independently (no sync).
        """
        new_conv_id = self.new_conversation()
        source_history = self.get_history(source_conv_id)
        if source_history:
            new_path = self.conversations_dir / f"{new_conv_id}.jsonl"
            with new_path.open("w", encoding="utf-8") as f:
                for msg in source_history:
                    f.write(json.dumps(_json_safe(msg), ensure_ascii=False) + "\n")
        return new_conv_id

    def add_share_recipients(self, recipients: list[str]) -> list[str]:
        """Append distinct emails to `meta.json["sharing"]["shared_with"]`.
        Returns the new (previously-absent) recipients only."""
        with _LOCK:
            meta = self.read_meta()
            sharing = meta.get("sharing") or {"shared_with": []}
            existing = set(sharing.get("shared_with") or [])
            new = [r for r in recipients if r and r not in existing]
            existing.update(new)
            sharing["shared_with"] = sorted(existing)
            meta["sharing"] = sharing
            self.write_meta(meta)
        return new

    def append_history(self, conv_id: str, message: dict) -> None:
        p = self.conversations_dir / f"{conv_id}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            # _json_safe: result tables carry raw pd.Timestamp cells (and any
            # stray pandas object) — normalize the WHOLE record or the write
            # raises and the AI turn is lost (mirrors B2C append_conv_history).
            fh.write(json.dumps(_json_safe(message), ensure_ascii=False) + "\n")

    def get_history(self, conv_id: str) -> list[dict]:
        p = self.conversations_dir / f"{conv_id}.jsonl"
        if not p.exists():
            return []
        out = []
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    def truncate_conv_history(self, conv_id: str, keep_count: int) -> list[dict]:
        """Rewrite the conv JSONL keeping only the first `keep_count` messages.

        Used by edit-regenerate to drop the last human turn (and everything after
        it) before re-running with the edited question. Verbatim port of global
        `storage.ChatDataStore.truncate_conv_history`.
        """
        history = self.get_history(conv_id)
        if keep_count >= len(history):
            return history
        truncated = history[:keep_count]
        p = self.conversations_dir / f"{conv_id}.jsonl"
        with p.open("w", encoding="utf-8") as fh:
            for msg in truncated:
                fh.write(json.dumps(_json_safe(msg), ensure_ascii=False) + "\n")
        return truncated


def get_chat_meta_owner(chat_id: str) -> Optional[str]:
    """Convenience: read the owner email out of meta.json."""
    p = _data_root() / "chatdata" / chat_id / "meta.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("owner")
    except Exception:
        return None


def chat_exists(chat_id: str) -> bool:
    return (_data_root() / "chatdata" / chat_id / "meta.json").exists()


# ---------------------------------------------------------------------------
# DashboardStore — per-user dashboards (pinned charts/tables + grid layout)
# ---------------------------------------------------------------------------

_DASH_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")

# Default grid cell for a newly pinned tile (12-column grid).
_DASH_DEFAULT_W = 6
_DASH_DEFAULT_H = 5


def _write_json_atomic(path: Path, payload) -> None:
    """Whole-file JSON replace: tmp + os.replace (same pattern as the parquet
    cache manifests) so a crash mid-write can never corrupt a dashboard doc."""
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{threading.get_ident()}")
    tmp.write_text(json.dumps(_json_safe(payload), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class DashboardStore:
    """Per-user dashboards under users/{email}/dashboards/.

    Layout:
      index.json       — light listing rows for the dropdown:
                           own:    {dash_id, name, created_at, last_used_at, tile_count}
                           shared: {dash_id, name, owner, shared_by, created_at, last_used_at}
                         A row with `shared_by` is a POINTER — the doc lives in
                         the owner's folder (single source of truth, live share).
      {dash_id}.json   — full doc incl. tile snapshots (can be MBs):
                           {dash_id, name, owner, version, created_at, updated_at,
                            last_used_at, sharing: {shared_with: [emails]},
                            tiles: [{tile_id, chat_id, kind, title, description,
                                     code, snapshot, chart_data, full_table_key,
                                     frozen, frozen_reason, layout, added_at}]}

    Readers .get() every field with defaults (stored-shape backward compat).
    All mutations run under _LOCK with atomic whole-file replaces.
    """

    @staticmethod
    def valid_id(value) -> bool:
        return bool(isinstance(value, str) and _DASH_ID_RE.match(value))

    def _dir(self, email: str) -> Path:
        d = _data_root() / "users" / _safe_email(email) / "dashboards"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _index_path(self, email: str) -> Path:
        return self._dir(email) / "index.json"

    def _doc_path(self, email: str, dash_id: str) -> Path:
        return self._dir(email) / f"{dash_id}.json"

    # ---- index -------------------------------------------------------------
    def _read_index(self, email: str) -> list[dict]:
        p = self._index_path(email)
        if not p.exists():
            return []
        try:
            rows = json.loads(p.read_text(encoding="utf-8"))
            return rows if isinstance(rows, list) else []
        except Exception as e:
            log_with_sid(email, "error", f"DASH_INDEX_READ_FAILED: {e}")
            return []

    def _write_index(self, email: str, rows: list[dict]) -> None:
        _write_json_atomic(self._index_path(email), rows)

    # ---- docs --------------------------------------------------------------
    def _read_doc(self, owner_email: str, dash_id: str) -> Optional[dict]:
        if not self.valid_id(dash_id):
            return None
        p = self._doc_path(owner_email, dash_id)
        if not p.exists():
            return None
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            return doc if isinstance(doc, dict) else None
        except Exception as e:
            log_with_sid(owner_email, "error", f"DASH_DOC_READ_FAILED dash={dash_id}: {e}")
            return None

    def _write_doc(self, owner_email: str, doc: dict) -> None:
        doc["updated_at"] = _now()
        _write_json_atomic(self._doc_path(owner_email, doc["dash_id"]), doc)

    # ---- public API --------------------------------------------------------
    def list_dashboards(self, email: str) -> list[dict]:
        """Own + shared rows, MRU-sorted (last_used_at, then created_at, desc).
        Shared pointers whose owner doc vanished (owner deleted the dashboard)
        are lazily dropped here."""
        with _LOCK:
            rows = self._read_index(email)
            keep: list[dict] = []
            dropped = False
            for row in rows:
                dash_id = row.get("dash_id")
                if not self.valid_id(dash_id):
                    dropped = True
                    continue
                if row.get("shared_by"):
                    if not self._doc_path(row.get("owner") or "", dash_id).exists():
                        dropped = True
                        continue
                keep.append(row)
            if dropped:
                try:
                    self._write_index(email, keep)
                except Exception as e:
                    log_with_sid(email, "warning", f"DASH_INDEX_PRUNE_FAILED: {e}")
        keep.sort(key=lambda r: (r.get("last_used_at") or r.get("created_at") or ""),
                  reverse=True)
        return keep

    def create_dashboard(self, email: str, name: str) -> dict:
        email = _safe_email(email)
        dash_id = secrets.token_hex(8)
        now = _now()
        doc = {
            "dash_id": dash_id, "name": name, "owner": email, "version": 1,
            "created_at": now, "updated_at": now, "last_used_at": now,
            "sharing": {"shared_with": []}, "tiles": [],
        }
        row = {"dash_id": dash_id, "name": name, "created_at": now,
               "last_used_at": now, "tile_count": 0}
        with _LOCK:
            _write_json_atomic(self._doc_path(email, dash_id), doc)
            rows = self._read_index(email)
            rows.append(row)
            self._write_index(email, rows)
        log_with_sid(email, "info", f"DASHBOARD_CREATED dash={dash_id}")
        return row

    def get_dashboard(self, email: str, dash_id: str) -> Optional[dict]:
        """Own doc only (owner path)."""
        return self._read_doc(email, dash_id)

    def resolve_dashboard(self, email: str, dash_id: str):
        """Resolve a dashboard the caller may access: own doc, else a shared
        pointer whose owner doc still lists the caller. Returns (doc, is_owner)
        or (None, False)."""
        email = _safe_email(email)
        doc = self._read_doc(email, dash_id)
        if doc is not None:
            return doc, True
        for row in self._read_index(email):
            if row.get("dash_id") == dash_id and row.get("shared_by"):
                owner = row.get("owner") or ""
                doc = self._read_doc(owner, dash_id)
                if doc is None:
                    return None, False
                shared = [s.lower() for s in
                          ((doc.get("sharing") or {}).get("shared_with") or [])]
                if email in shared:
                    return doc, False
                return None, False
        return None, False

    def touch_last_used(self, email: str, dash_id: str) -> None:
        """Bump the caller's MRU row (and the doc when the caller owns it).
        For shared pointers this also re-syncs the name from the live doc."""
        email = _safe_email(email)
        now = _now()
        try:
            with _LOCK:
                rows = self._read_index(email)
                changed = False
                for row in rows:
                    if row.get("dash_id") != dash_id:
                        continue
                    row["last_used_at"] = now
                    if row.get("shared_by"):
                        live = self._read_doc(row.get("owner") or "", dash_id)
                        if live and live.get("name"):
                            row["name"] = live["name"]
                    changed = True
                if changed:
                    self._write_index(email, rows)
                doc = self._read_doc(email, dash_id)
                if doc is not None:
                    doc["last_used_at"] = now
                    self._write_doc(email, doc)
        except Exception as e:
            log_with_sid(email, "warning", f"DASH_TOUCH_FAILED dash={dash_id}: {e}")

    def rename_dashboard(self, owner_email: str, dash_id: str, name: str) -> bool:
        owner_email = _safe_email(owner_email)
        with _LOCK:
            doc = self._read_doc(owner_email, dash_id)
            if doc is None:
                return False
            doc["name"] = name
            self._write_doc(owner_email, doc)
            rows = self._read_index(owner_email)
            for row in rows:
                if row.get("dash_id") == dash_id:
                    row["name"] = name
            self._write_index(owner_email, rows)
        log_with_sid(owner_email, "info", f"DASHBOARD_RENAMED dash={dash_id}")
        return True

    def delete_dashboard(self, owner_email: str, dash_id: str) -> bool:
        """Owner delete: remove doc + own index row. Recipients' pointer rows
        go stale and are pruned by their next list_dashboards. Idempotent."""
        owner_email = _safe_email(owner_email)
        if not self.valid_id(dash_id):
            return False
        with _LOCK:
            p = self._doc_path(owner_email, dash_id)
            existed = p.exists()
            try:
                p.unlink(missing_ok=True)
            except Exception as e:
                log_with_sid(owner_email, "error", f"DASH_DELETE_FAILED dash={dash_id}: {e}")
                return False
            rows = [r for r in self._read_index(owner_email) if r.get("dash_id") != dash_id]
            self._write_index(owner_email, rows)
        if existed:
            log_with_sid(owner_email, "info", f"DASHBOARD_DELETED dash={dash_id}")
        return True

    def remove_from_index(self, email: str, dash_id: str) -> bool:
        """Recipient 'delete': drop only the caller's index row (pointer or
        stale row) — the owner's doc is untouched (deactivate_chat semantics)."""
        email = _safe_email(email)
        with _LOCK:
            rows = self._read_index(email)
            keep = [r for r in rows if r.get("dash_id") != dash_id]
            if len(keep) == len(rows):
                return False
            self._write_index(email, keep)
        return True

    # ---- tiles -------------------------------------------------------------
    def _sync_tile_count(self, owner_email: str, doc: dict) -> None:
        rows = self._read_index(owner_email)
        for row in rows:
            if row.get("dash_id") == doc.get("dash_id"):
                row["tile_count"] = len(doc.get("tiles") or [])
        self._write_index(owner_email, rows)

    def add_tile(self, owner_email: str, dash_id: str, tile: dict) -> Optional[dict]:
        """Assign a tile_id, auto-place at the bottom of the layout, append."""
        owner_email = _safe_email(owner_email)
        with _LOCK:
            doc = self._read_doc(owner_email, dash_id)
            if doc is None:
                return None
            tiles = doc.get("tiles") or []
            bottom = 0
            for t in tiles:
                lay = t.get("layout") or {}
                try:
                    bottom = max(bottom, int(lay.get("y", 0)) + int(lay.get("h", _DASH_DEFAULT_H)))
                except Exception:
                    continue
            tile = dict(tile)
            tile["tile_id"] = secrets.token_hex(8)
            tile["added_at"] = _now()
            tile.setdefault("frozen", False)
            tile.setdefault("frozen_reason", None)
            # Kind-aware defaults (QA 3.1): charts half-width, two per row —
            # a new chart pairs with a previous LEFT-half chart on its row;
            # tables/wide content full-width; text blocks a slim full row.
            kind = (tile.get("kind") or "").lower()
            if kind == "chart":
                last = tiles[-1] if tiles else None
                llay = (last or {}).get("layout") or {}
                try:
                    pair = (last is not None and (last.get("kind") or "").lower() == "chart"
                            and int(llay.get("w", 12)) <= _DASH_DEFAULT_W
                            and int(llay.get("x", 0)) == 0)
                except Exception:
                    pair = False
                if pair:
                    tile["layout"] = {"x": _DASH_DEFAULT_W, "y": int(llay.get("y", 0)),
                                      "w": _DASH_DEFAULT_W, "h": _DASH_DEFAULT_H}
                else:
                    tile["layout"] = {"x": 0, "y": bottom,
                                      "w": _DASH_DEFAULT_W, "h": _DASH_DEFAULT_H}
            elif kind == "text":
                tile["layout"] = {"x": 0, "y": bottom, "w": 12, "h": 2}
            else:  # table / anything else → full-width
                tile["layout"] = {"x": 0, "y": bottom, "w": 12, "h": _DASH_DEFAULT_H}
            tiles.append(tile)
            doc["tiles"] = tiles
            self._write_doc(owner_email, doc)
            self._sync_tile_count(owner_email, doc)
        log_with_sid(owner_email, "info",
                     f"DASH_TILE_ADDED dash={dash_id} tile={tile['tile_id']} kind={tile.get('kind')}")
        return tile

    def remove_tile(self, owner_email: str, dash_id: str, tile_id: str) -> bool:
        owner_email = _safe_email(owner_email)
        with _LOCK:
            doc = self._read_doc(owner_email, dash_id)
            if doc is None:
                return False
            tiles = doc.get("tiles") or []
            keep = [t for t in tiles if t.get("tile_id") != tile_id]
            if len(keep) == len(tiles):
                return False
            doc["tiles"] = keep
            self._write_doc(owner_email, doc)
            self._sync_tile_count(owner_email, doc)
        log_with_sid(owner_email, "info", f"DASH_TILE_REMOVED dash={dash_id} tile={tile_id}")
        return True

    def update_layout(self, owner_email: str, dash_id: str, layouts: list[dict]) -> bool:
        """Bulk layout save: [{tile_id, x, y, w, h}]. Unknown tile_ids are
        ignored (stale client); non-int values are clamped/skipped."""
        owner_email = _safe_email(owner_email)
        by_id = {}
        for lay in layouts or []:
            tid = lay.get("tile_id")
            try:
                x = max(0, int(lay.get("x", 0)))
                y = max(0, int(lay.get("y", 0)))
                w = min(12, max(1, int(lay.get("w", _DASH_DEFAULT_W))))
                h = min(50, max(1, int(lay.get("h", _DASH_DEFAULT_H))))
            except Exception:
                continue
            if tid:
                by_id[tid] = {"x": x, "y": y, "w": w, "h": h}
        with _LOCK:
            doc = self._read_doc(owner_email, dash_id)
            if doc is None:
                return False
            for t in doc.get("tiles") or []:
                lay = by_id.get(t.get("tile_id"))
                if lay:
                    t["layout"] = lay
            self._write_doc(owner_email, doc)
        return True

    def update_tile(self, owner_email: str, dash_id: str, tile_id: str, patch: dict) -> bool:
        """Merge a patch into one tile (snapshot swap / frozen flags). The doc
        always lives in the OWNER's folder — recipients refresh through here
        with the owner email resolved from the doc."""
        owner_email = _safe_email(owner_email)
        with _LOCK:
            doc = self._read_doc(owner_email, dash_id)
            if doc is None:
                return False
            for t in doc.get("tiles") or []:
                if t.get("tile_id") == tile_id:
                    t.update(patch)
                    self._write_doc(owner_email, doc)
                    return True
        return False

    # ---- sharing -----------------------------------------------------------
    def add_dashboard_share(self, owner_email: str, dash_id: str,
                            recipients: list[str]) -> list[str]:
        """Append distinct emails to the doc's shared_with (same shape as chat
        meta sharing) + write a pointer row into each NEW recipient's index.
        Returns the previously-absent recipients only."""
        owner_email = _safe_email(owner_email)
        with _LOCK:
            doc = self._read_doc(owner_email, dash_id)
            if doc is None:
                return []
            sharing = doc.get("sharing") or {"shared_with": []}
            existing = set(sharing.get("shared_with") or [])
            new = [r for r in (_safe_email(r) for r in recipients if r)
                   if r and r != owner_email and r not in existing]
            existing.update(new)
            sharing["shared_with"] = sorted(existing)
            doc["sharing"] = sharing
            self._write_doc(owner_email, doc)
            now = _now()
            for rcpt in new:
                try:
                    rows = self._read_index(rcpt)
                    if any(r.get("dash_id") == dash_id for r in rows):
                        continue
                    rows.append({"dash_id": dash_id, "name": doc.get("name") or "",
                                 "owner": owner_email, "shared_by": owner_email,
                                 "created_at": now, "last_used_at": None})
                    self._write_index(rcpt, rows)
                except Exception as e:
                    log_with_sid(owner_email, "error",
                                 f"DASH_SHARE_POINTER_FAILED dash={dash_id} rcpt={rcpt}: {e}")
        if new:
            log_with_sid(owner_email, "info",
                         f"DASHBOARD_SHARED dash={dash_id} added={len(new)}")
        return new
