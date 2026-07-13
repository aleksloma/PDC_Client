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


def _load_one_file(path: Path) -> dict[str, pd.DataFrame]:
    """Load one tabular file. Returns {key: df}. Excel sheets yield
    "<filename>::<sheet>" keys (single-sheet excels keep the bare filename).

    Excel files run through the validated 6-stage detection pipeline
    (`excel_table_detector.load_excel_sheets`) — same algorithm the global B2C
    app uses, so multi-row headers, ListObjects, label-vs-measure splits,
    multilingual total-row drops, and text-above-the-table extraction all work.
    """
    out: dict[str, pd.DataFrame] = {}
    try:
        ext = path.suffix.lower()
        if ext in (".xls", ".xlsx", ".xlsm"):
            out.update(load_excel_sheets(path, path.name))
        elif ext == ".csv":
            out[path.name] = pd.read_csv(path)
        elif ext == ".tsv":
            out[path.name] = pd.read_csv(path, sep="\t")
        elif ext == ".json":
            out[path.name] = pd.read_json(path)
        elif ext == ".parquet":
            out[path.name] = pd.read_parquet(path)
    except Exception as e:
        log_with_sid(path.name, "warning", f"FILE_LOAD_ERROR {path}: {e}")
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
        manifest = {"src_size": src_size, "src_mtime_ns": src_mtime_ns, "entries": entries}
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


def _load_one_file_cached(path: Path) -> dict[str, pd.DataFrame]:
    """Parquet-cache wrapper around `_load_one_file`. The source's size +
    mtime_ns are captured BEFORE parsing, so a file overwritten mid-parse
    stamps a stale manifest that self-heals on the next load. `.parquet`
    sources are read directly — caching them would be a redundant copy."""
    if path.suffix.lower() not in _PARQUET_CACHEABLE_EXTS:
        return _load_one_file(path)
    try:
        st = path.stat()
        src_size, src_mtime_ns = st.st_size, st.st_mtime_ns
    except Exception as e:
        log_with_sid(path.name, "warning", f"PARQUET_CACHE_STAT_FAILED {path}: {e}")
        return _load_one_file(path)
    cached = _parquet_cache_read(path, src_size, src_mtime_ns)
    if cached is not None:
        return cached
    out = _load_one_file(path)
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
    """Approximate in-memory footprint of a dfs dict. None on failure → the
    entry is simply not cached (safe fallback, Article IV)."""
    try:
        return int(sum(int(df.memory_usage(deep=True).sum()) for df in dfs.values()))
    except Exception as e:
        log_with_sid("df_cache", "warning", f"DF_CACHE_SIZE_FAILED: {e}")
        return None


def _load_dataframes_cached(files_dir: Path, owner: str) -> dict[str, pd.DataFrame]:
    """Shared load path for UserStore/ChatDataStore: signature-validated
    in-memory cache in front of the per-file parquet disk cache. Returns a
    shallow copy on hit so callers mutating the dict can't poison the cache
    (in-place DataFrame mutation by executed code remains possible — same
    exposure as the B2C app)."""
    key = str(files_dir)
    sig = _files_signature(files_dir)
    if sig is not None:
        try:
            entry = _DATAFRAME_CACHE.get(key)
            if entry is not None and entry.get("sig") == sig:
                log_with_sid(owner, "info", f"DF_MEMORY_CACHE_HIT dfs={len(entry['dfs'])}")
                return dict(entry["dfs"])
        except Exception as e:
            log_with_sid(owner, "warning", f"DF_MEMORY_CACHE_GET_FAILED: {e}")
    dfs: dict[str, pd.DataFrame] = {}
    for fp in sorted(files_dir.iterdir()):
        if fp.is_file() and not fp.name.startswith("."):
            dfs.update(_load_one_file_cached(fp))
    if sig is not None and dfs:
        try:
            size = _dfs_size_bytes(dfs)
            if size is not None:
                cached = _DATAFRAME_CACHE.set(key, {"sig": sig, "dfs": dict(dfs)},
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

    def set_password(self, email: str, password: str) -> None:
        """Set the user's own password. Clears any outstanding temp password."""
        from password_utils import generate_password_hash
        with _LOCK:
            auth = self.get_auth(email)
            auth["password_hash"] = generate_password_hash(password)
            auth.pop("temp_password_hash", None)
            auth["must_change_password"] = False
            self._write_auth(email, auth)
        log_with_sid(email, "info", "USER_PASSWORD_SET")

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
        # Newest first (descending created_at). Rows missing created_at sort to
        # the bottom (treated as oldest); file order breaks ties. Stable so the
        # rest of the app sees a single consistent ordering.
        out.sort(key=lambda r: (r.get("created_at") or "", r.get("_seq", 0)),
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
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_dataframes(self) -> dict[str, pd.DataFrame]:
        return _load_dataframes_cached(self.files_dir, self.sid)


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
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

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

    def load_dataframes(self) -> dict[str, pd.DataFrame]:
        return _load_dataframes_cached(self.files_dir, self.chat_id)

    def schema_docs(self) -> dict:
        """Build {filename: {file_description, fields: {col: {...}}}} for the brain."""
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
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
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
            fh.write(json.dumps(message, ensure_ascii=False) + "\n")

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
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
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
