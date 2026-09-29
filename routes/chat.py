"""Client-side chat endpoints — SSE stream + sidebar helpers + B2C-only stubs.

The chat-stream endpoint matches the B2C `/api/chat/{chat_id}/chat/stream`
shape that `dashboard.js` consumes:
  - emits `data: {...}\\n\\n` JSON events
  - `{progress: true, message: "..."}` for status text in the loading bubble
  - `{partial: true, answer, image_base64, chart_n, chart_total, conv_id}`
    for per-chart partial results (not used here — enterprise build emits
    a single final event), kept in the contract for parity
  - `{done: true, answer, image_base64, table, conv_id, tokens}` final event
  - `{error: "..."}` on failure (kill-switch surfaces here)

LLM work is done in a worker thread; results come back through an
`asyncio.Queue` (same pattern as the B2C `chat_stream_api`).
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import secrets
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

import html_sanitize
import local_store
import run_chat_local
import brain_client
from brain_client import TenantRevokedError, BrainError, BrainTimeoutError
from exec_transport import log_safe_text
from logger_utils import log_with_sid
from schema_builder import _detect_language as _detect_lang_for_title


# Token-keyed in-memory cache of full result tables (the chat stream returns
# only a preview when `full_table_key` is set; the frontend fetches the full
# table via /api/chat/{chat_id}/full_table/{key}). Bounded LRU so very long
# sessions don't blow memory.
_FULL_TABLE_CACHE: dict[str, dict] = {}
_FULL_TABLE_ORDER: list[str] = []
_FULL_TABLE_MAX = 256

def _cache_full_table(table: dict) -> str:
    key = secrets.token_hex(8)
    _FULL_TABLE_CACHE[key] = table
    _FULL_TABLE_ORDER.append(key)
    while len(_FULL_TABLE_ORDER) > _FULL_TABLE_MAX:
        old = _FULL_TABLE_ORDER.pop(0)
        _FULL_TABLE_CACHE.pop(old, None)
    return key


# Chart references for the PNG export. The export renders chart markup with
# kaleido/Chromium inside the web container, so it never takes markup from the
# request: only a chart THIS SERVER produced. Every chart the server sends to
# the browser (stream partials, the single-shot answer, edit-regenerate, a
# refresh) is registered here under a random `chart_ref`, bound to the user and
# the chat; after a reload the browser names the stored history row instead
# (`_stored_chart_html`). In memory, per process (correct under --workers 1),
# bounded by age, count and total size. A registration failure only leaves
# that one chart without a reference (its Download button is disabled).
_CHART_REFS: "OrderedDict[str, tuple]" = OrderedDict()
_CHART_REF_LOCK = threading.Lock()
_CHART_REF_TTL_S = 1800
_CHART_REF_MAX = 256
_CHART_REF_MAX_BYTES = 200_000_000
_CHART_REF_TOTAL = {"bytes": 0}
_CHART_REF_RE = re.compile(r"[0-9a-f]{32}")
# One warning per process when a browser still posts chart markup.
_EXPORT_LEGACY_LOGGED = {"logged": False}


def _chart_ref_for(email, chat_id, html) -> str | None:
    """Register a server-produced Plotly chart; its reference, or None."""
    try:
        from routes.report import _is_plotly_html
        if not isinstance(html, str) or not _is_plotly_html(html):
            return None
        ref = secrets.token_hex(16)
        size = len(html)
        now = time.monotonic()
        with _CHART_REF_LOCK:
            _CHART_REFS[ref] = (str(email or "").strip().lower(), str(chat_id), html, now, size)
            _CHART_REF_TOTAL["bytes"] += size
            while _CHART_REFS:
                oldest_ref, oldest = next(iter(_CHART_REFS.items()))
                if (oldest_ref != ref
                        and (now - oldest[3] > _CHART_REF_TTL_S
                             or len(_CHART_REFS) > _CHART_REF_MAX
                             or _CHART_REF_TOTAL["bytes"] > _CHART_REF_MAX_BYTES)):
                    _CHART_REFS.popitem(last=False)
                    _CHART_REF_TOTAL["bytes"] -= oldest[4]
                    continue
                break
        return ref
    except Exception as e:
        log_with_sid(log_safe_text(str(email or "chat"), 254), "warning",
                     f"CHART_REF_FAILED error={log_safe_text(type(e).__name__, 80)}")
        return None


def _chart_by_ref(email, chat_id, ref) -> str | None:
    """The chart registered under `ref` for this user and chat, or None."""
    if not isinstance(ref, str) or not _CHART_REF_RE.fullmatch(ref):
        return None
    with _CHART_REF_LOCK:
        entry = _CHART_REFS.get(ref)
    if not entry:
        return None
    owner, ref_chat, html, created, _size = entry
    if (owner != str(email or "").strip().lower() or ref_chat != str(chat_id)
            or time.monotonic() - created > _CHART_REF_TTL_S):
        return None
    return html


def _with_chart_refs(out: dict, email, chat_id) -> dict:
    """A copy of a response dict whose charts carry a `chart_ref`: the
    top-level `image_base64`, and every entry of an `images` list (copied —
    those dicts may be the persisted history row's)."""
    if not isinstance(out, dict):
        return out
    out = dict(out)
    if out.get("image_base64"):
        out["chart_ref"] = _chart_ref_for(email, chat_id, out.get("image_base64"))
    imgs = out.get("images")
    if isinstance(imgs, list):
        stamped = []
        for img in imgs:
            if isinstance(img, dict) and img.get("image_base64"):
                img = dict(img)
                img["chart_ref"] = _chart_ref_for(email, chat_id, img.get("image_base64"))
            stamped.append(img)
        out["images"] = stamped
    return out


def _stored_chart_html(chat_id: str, conv_id, ai_index, image_index) -> str | None:
    """The Plotly chart stored in a conversation row, or None. `ai_index` is
    the row's position in the history the conversation route serves (that
    route never drops or reorders rows); `image_index` picks one chart of a
    multi-chart row (0 for a single-chart row)."""
    try:
        if not local_store.valid_conv_id(conv_id):
            return None
        if (isinstance(ai_index, bool) or not isinstance(ai_index, int)
                or isinstance(image_index, bool) or not isinstance(image_index, int)):
            return None
        rows = local_store.ChatDataStore(chat_id).get_history(conv_id)
        if not (0 <= ai_index < len(rows)):
            return None
        row = rows[ai_index]
        if not isinstance(row, dict) or row.get("role") != "ai":
            return None
        imgs = row.get("images")
        if isinstance(imgs, list) and imgs:
            if not (0 <= image_index < len(imgs)) or not isinstance(imgs[image_index], dict):
                return None
            html = imgs[image_index].get("image_base64")
        elif image_index == 0:
            html = row.get("image_base64")
        else:
            return None
        from routes.report import _is_plotly_html
        return html if isinstance(html, str) and _is_plotly_html(html) else None
    except Exception as e:
        log_with_sid(chat_id, "warning",
                     f"STORED_CHART_LOOKUP_FAILED error={log_safe_text(type(e).__name__, 80)}")
        return None


# Durable full-table persistence (port of the B2C `download_full_excel`
# mechanism). A tabular RESULT is written to disk as
# `chatdata/{chat_id}/conversations/full/{key}.json` = {columns, rows, code,
# total_rows}, so "Download Excel" / "Show full table" survive a container
# restart AND can re-execute the stored code to return the COMPLETE (uncapped)
# result — not the 50-row preview. The in-memory LRU is kept as a fast path and
# as the fallback for chart-data ("Show data") records, which have no
# re-executable DataFrame code. Article V: local disk under DATA_ROOT only.
_FULL_KEY_RE = re.compile(r"[0-9a-fA-F]{16}")


def _persist_full_table(store, table: dict, code: str | None,
                        result_key: str | None = None,
                        sql: dict | None = None,
                        first_key: str | None = None) -> str | None:
    """Persist {columns, rows, code, total_rows} to disk and mirror it into the
    in-memory LRU. Returns the durable key, or None on failure (caller then
    leaves `full_table_key` unset and the frontend exports the preview rows).

    `result_key`: set for one table of a multi-table answer — names the dict
    entry of the re-executed RESULT this table comes from (see
    `_reexecute_full_df`).

    `sql`: the turn's live-table map (df key -> SELECT or None); the subset
    for the keys `code` references is stored as `sql`, so a re-execution
    of this record (a dashboard tile, Download Excel) re-runs the same
    query.

    `first_key`: the key of the turn's FIRST frame (what the executor binds
    `df` to), so a `df`-only answer on a live-first chat keeps its SELECT;
    None disables the alias rule (the sql map has no frame order)."""
    try:
        key = secrets.token_hex(8)  # 16 hex chars — matches _FULL_KEY_RE
        record = _json_safe({
            "columns": table.get("columns", []),
            "rows": table.get("rows", []),
            "total_rows": table.get("total_rows"),
        })
        if code:
            record["code"] = code
        if result_key is not None:
            record["result_key"] = result_key
        if code and isinstance(sql, dict) and sql:
            # The same referencing rule as the pre-fetch (named in either
            # quote style, or every key on a generic `dfs` walk), so a
            # `dfs.get("k")` answer keeps its SELECT on the durable record.
            named, generic = run_chat_local._referenced_live_keys(
                code, list(sql), list(sql), first_key=first_key)
            refs = set(named) | set(generic)
            subset = {k: v for k, v in sql.items() if k in refs}
            if subset:
                record["sql"] = _json_safe(subset)
        full_dir = store.conversations_dir / "full"
        full_dir.mkdir(parents=True, exist_ok=True)
        (full_dir / f"{key}.json").write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8")
        # Fast-path / fallback copy in the bounded LRU.
        _FULL_TABLE_CACHE[key] = record
        _FULL_TABLE_ORDER.append(key)
        while len(_FULL_TABLE_ORDER) > _FULL_TABLE_MAX:
            old = _FULL_TABLE_ORDER.pop(0)
            _FULL_TABLE_CACHE.pop(old, None)
        return key
    except Exception as e:
        # Raised ABOUT the result table being written (its labels, its cells),
        # so the message can quote one of them; the durable log is
        # newline-delimited and must stay one record per event.
        log_with_sid(getattr(store, "chat_id", ""), "warning",
                     f"PERSIST_FULL_TABLE_FAILED: {log_safe_text(str(e), 200)}")
        return None


def _load_full_table_record(store, key: str) -> dict | None:
    """Resolve a full-table / chart-data record by key: durable disk first
    (survives restart, may carry re-executable `code`), then the in-memory LRU
    (chart-data "Show data", no code). Returns None when the key is malformed or
    unknown. The regex guard also blocks path traversal (Article VII)."""
    if not (key and _FULL_KEY_RE.fullmatch(key)):
        return None
    try:
        p = store.conversations_dir / "full" / f"{key}.json"
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        # Same class: the decoder's own message can quote the stored content.
        log_with_sid(getattr(store, "chat_id", ""), "warning",
                     f"FULL_TABLE_READ_FAILED: {log_safe_text(str(e), 200)}")
    return _FULL_TABLE_CACHE.get(key)


async def _reexecute_full_df(chat_id: str, code: str | None, result_key: str | None = None,
                             *, drop_df_keys=None):
    """Re-run stored code locally to produce the COMPLETE (uncapped) DataFrame.
    Returns a DataFrame or None (caller falls back to the stored preview rows).

    `result_key`: for multi-table answers (RESULT was a dict of DataFrames) the
    durable record stores which dict entry this table came from — pick it out
    of the re-executed dict before the normal normalization.

    `drop_df_keys`: df keys removed from the exec namespace AFTER load (role
    gate defense in depth — the dashboard tile-refresh path passes the denied
    set; the full_table/Download-Excel callers pass nothing: their live fetch
    is gated by the routes (`_live_reexec_block`, before this call) and
    snapshot re-execution stays ungated — viewing existing data is never
    blocked retroactively). Filtered
    post-load on purpose — _load_dataframes_cached's cache key is per-chat,
    not per-user, so the cache must always hold the full set.

    The returned frame carries `attrs["pdc_sql_used"]` (the live SELECTs
    that ran: df key -> SQL text, None for a default read; {} when none)
    and `attrs["pdc_first_key"]` (the first frame's key), always set here —
    a caller persisting a fresh full-table record passes them on as `sql=` /
    `first_key=` (the dashboard multi-table tile). Carried on the frame so
    the signature every caller uses stays as it is.

    Article II: execution is local — nothing here touches the brain.
    Article V: reads local DataFrames only. Article IV: never raises."""
    if not code:
        return None
    try:
        import pandas as pd
        from code_exec import safe_execute
        store = local_store.ChatDataStore(chat_id)
        loop = asyncio.get_running_loop()
        dfs = await loop.run_in_executor(
            _EXEC, lambda: store.load_dataframes(include_live=True))
        if drop_df_keys:
            dfs = {k: v for k, v in dfs.items() if k not in drop_df_keys}
        if not dfs:
            return None
        # Live tables: re-run the SELECT the answer was computed with (the
        # role gate — when the caller has one — ran before this call).
        first_key = next(iter(dfs), None)
        live_err, sql_used = await loop.run_in_executor(
            _EXEC, lambda: _prefetch_stored_live(store, code, dfs, chat_id))
        if live_err is not None:
            log_with_sid(chat_id, "warning",
                         f"DOWNLOAD_REEXEC_LIVE_REFUSED code={log_safe_text(str(live_err.get('code') or 'FETCH_FAILED'), 40)}")
            return None
        exec_out = await loop.run_in_executor(
            _EXEC, lambda: safe_execute(code, dfs, sid=chat_id))
        if not isinstance(exec_out, dict) or exec_out.get("error"):
            return None
        obj = exec_out.get("result")
        if isinstance(obj, dict) and result_key is not None:
            obj = obj.get(result_key)
        # _normalize_df_for_table: same pivot normalization as the chat-time
        # serializer (index surfaced, MultiIndex columns flattened) so the
        # full-table view / Excel download match what the chat rendered (QA 2.2)
        from run_chat_local import _normalize_df_for_table
        if isinstance(obj, pd.DataFrame):
            out = _normalize_df_for_table(obj)
        elif isinstance(obj, pd.Series):
            out = _normalize_df_for_table(obj.reset_index())
        elif hasattr(obj, "data") and isinstance(obj.data, pd.DataFrame):
            out = _normalize_df_for_table(obj.data)
        else:
            return None
        # Overwritten unconditionally: the frame came back from the sandbox,
        # and only this process decides what ran.
        out.attrs["pdc_sql_used"] = dict(sql_used or {})
        out.attrs["pdc_first_key"] = first_key
        return out
    except Exception as e:
        # The re-executed RESULT is normalized here (`_normalize_df_for_table`),
        # and pandas quotes the label it choked on WITHOUT `repr` — a label the
        # sandbox chose. Escape before it reaches the log line.
        log_with_sid(chat_id, "warning",
                     f"DOWNLOAD_REEXEC_FAILED: {log_safe_text(str(e), 200)}")
        return None


def _auto_analysis_busy_response(store, email: str, chat_id: str):
    """409 when Auto Analytics is running for THIS chat, else None (QA 2.3).

    While the job runs, its 4 workers occupy the dispatch gate to the analysis
    sandbox (`executor_client`, one job at a time by default) and the shared
    brain connection pool, so an interactive question waits the queue and then
    fails with a misleading "temporarily unavailable" error. Telling the user
    honestly and immediately — BEFORE any history append — is the fix.
    HONEST LIMIT: the gate is process-wide while this guard is per-chat, so a
    question in a DIFFERENT chat still queues behind the job — answered late
    rather than wrongly, since the queue wait is outside the job's own budget.
    Fail-open: a corrupt meta must never block chat (Article IV)."""
    try:
        import auto_analytics as aa
        if aa.get_auto_analysis_state(store.read_meta()).get("status") == "processing":
            log_with_sid(email, "info", f"CHAT_BLOCKED_AUTO_ANALYSIS chat={chat_id}")
            return JSONResponse({
                "error": ("Auto Analytics is currently running for this chat. "
                          "Your question can start as soon as it finishes — "
                          "please ask again then."),
                "busy": "auto_analysis",
            }, status_code=409)
    except Exception as e:
        log_with_sid(email, "warning",
                     f"AUTO_BUSY_CHECK_FAILED chat={chat_id}: "
                     f"{log_safe_text(str(e), 200)}")
    return None


# In-progress generation registry. A conv_id present here has a worker thread
# generating right now. Used ONLY by the lightweight status endpoint so a page
# reloaded/reopened mid-generation can show the working indicator and block new
# questions until the AI turn is persisted. It NEVER affects persistence — the
# worker remains the sole, exactly-once persister.
_INPROGRESS_CONVS: set[str] = set()
_INPROGRESS_LOCK = threading.Lock()


def _mark_generating(conv_id: str) -> None:
    try:
        with _INPROGRESS_LOCK:
            _INPROGRESS_CONVS.add(conv_id)
    except Exception:
        pass


def _unmark_generating(conv_id: str) -> None:
    try:
        with _INPROGRESS_LOCK:
            _INPROGRESS_CONVS.discard(conv_id)
    except Exception:
        pass


def _is_generating(conv_id: str) -> bool:
    try:
        with _INPROGRESS_LOCK:
            return conv_id in _INPROGRESS_CONVS
    except Exception:
        return False


# Cooperative-cancel registry (STOP button). A conv_id here has had a stop
# requested; the worker checks it between charts and halts at the next chart
# boundary (the in-flight chart still finishes — cancellation is cooperative,
# not instant). Shares the in-progress lock. Never affects exactly-once
# persistence — the worker still persists whatever charts it produced.
_CANCEL_CONVS: set[str] = set()


def _request_cancel(conv_id: str) -> None:
    try:
        with _INPROGRESS_LOCK:
            _CANCEL_CONVS.add(conv_id)
    except Exception:
        pass


def _is_cancelled(conv_id: str) -> bool:
    try:
        with _INPROGRESS_LOCK:
            return conv_id in _CANCEL_CONVS
    except Exception:
        return False


def _clear_cancel(conv_id: str) -> None:
    try:
        with _INPROGRESS_LOCK:
            _CANCEL_CONVS.discard(conv_id)
    except Exception:
        pass


router = APIRouter(prefix="/api/chat", tags=["client-chat"])

# Worker pool for the SSE per-request thread (Article VI)
_EXEC = ThreadPoolExecutor(max_workers=4, thread_name_prefix="client_chat")

import atexit
atexit.register(lambda: _EXEC.shutdown(wait=False, cancel_futures=True))


# One implementation for SSE payloads AND persistence — the full pandas/numpy
# normalizer (Timestamp→ISO, NaT→None, catch-all str()) lives in local_store
# next to the history writers that depend on it.
_json_safe = local_store._json_safe


# Max serialized size of a chart's source data we will PERSIST into history so
# the "Show data" button survives reload. Oversize or missing data is omitted
# (the button just won't appear for that chart) — we never bloat the on-disk
# conversation or risk a serialization failure.
_PERSIST_CHART_DATA_MAX_CHARS = 200_000


def _persistable_chart_data(cd):
    """Return JSON-safe chart_data if it is usable and within the size cap,
    else None. Mirrors the live-cache guard (a dict with `rows` or `tables`)."""
    try:
        if not (isinstance(cd, dict) and (cd.get("rows") or cd.get("tables"))):
            return None
        safe = _json_safe(cd)
        if len(json.dumps(safe, ensure_ascii=False)) > _PERSIST_CHART_DATA_MAX_CHARS:
            return None
        return safe
    except Exception:
        return None


def _build_chart_entry(c: dict) -> dict:
    """One persisted chart entry for a multi-chart turn: image + answer plus its
    OWN code and (size-capped) chart_data, so reload restores Show code / Show
    data per chart (PART B)."""
    entry = {"image_base64": c.get("image_base64"), "answer": c.get("answer", "")}
    if c.get("code"):
        entry["code"] = c["code"]
    cd = _persistable_chart_data(c.get("chart_data"))
    if cd is not None:
        entry["chart_data"] = cd
    return entry


def _attach_live_fields(rec: dict, src) -> dict:
    """Copy the live-table fields (`sql`, `live_truncated`, `live_rows`) a
    result dict / done event carries onto a history record — only the keys
    that are present, so a turn without a live table keeps today's shape."""
    if not isinstance(src, dict):
        return rec
    if isinstance(src.get("sql"), dict):
        rec["sql"] = dict(src["sql"])
    if "live_truncated" in src:
        rec["live_truncated"] = bool(src.get("live_truncated"))
    if isinstance(src.get("live_rows"), dict):
        rec["live_rows"] = dict(src["live_rows"])
    return rec


def _build_ai_history_record(err_msg, single, combined_answer, combined_codes,
                             usage, charts, full_table_key=None,
                             full_table_keys=None, combined_tables=None,
                             live_fields=None):
    """Build the AI-turn history record persisted EXACTLY ONCE by the worker.

    Returns None when there is nothing to persist (never writes an empty turn).
    Enriches with PER-CHART code + chart_data (PART B). Backward-compatible
    shape: 0 imgs → image_base64=None; 1 img → image_base64 + record-level
    code/chart_data; 2+ imgs → images=[{image_base64, answer, code?, chart_data?}].
    `full_table_key` (single-shot tabular result) is persisted so a reloaded
    conversation can re-execute for the FULL Download Excel / Show full table.
    Live tables: the single result's / the done event's (`live_fields`) `sql`,
    `live_truncated` and `live_rows` are persisted so the refresh paths can
    re-run the same query.
    """
    if err_msg:
        return {"role": "ai", "content": err_msg, "ts": time.time()}
    if single is not None:
        rec = {
            "role": "ai",
            "content": single.get("text", ""),
            "image_base64": single.get("image_base64"),
            "table": single.get("table"),
            "code": single.get("code"),
            "usage": single.get("usage"),
            "ts": time.time(),
        }
        _attach_live_fields(rec, single)
        if full_table_key:
            rec["full_table_key"] = full_table_key
        # Multi-table answer: persist the tables array (mirrors the multi-chart
        # `images` array) + per-table durable keys aligned by index.
        tables = single.get("tables")
        if tables:
            rec["tables"] = tables
            if full_table_keys:
                rec["full_table_keys"] = full_table_keys
        cd = _persistable_chart_data(single.get("chart_data"))
        if cd is not None:
            rec["chart_data"] = cd
        return rec
    if combined_answer is not None:
        imgs = [c for c in charts if c.get("image_base64")]
        rec = {
            "role": "ai",
            "content": combined_answer,
            "image_base64": None,
            "table": None,
            "code": "\n\n###NEXT_PLOT###\n\n".join(combined_codes or []),
            "usage": usage or {},
            "ts": time.time(),
        }
        if len(imgs) >= 2:
            rec["images"] = [_build_chart_entry(c) for c in imgs]
        elif len(imgs) == 1:
            c = imgs[0]
            rec["image_base64"] = c.get("image_base64")
            if c.get("code"):
                rec["code"] = c["code"]
            cd = _persistable_chart_data(c.get("chart_data"))
            if cd is not None:
                rec["chart_data"] = cd
        # Mixed dashboard answer: the KPI/table blocks' tables ride alongside
        # the charts (per-table durable keys aligned by index).
        if combined_tables:
            rec["tables"] = combined_tables
            if full_table_keys:
                rec["full_table_keys"] = full_table_keys
        _attach_live_fields(rec, live_fields)
        return rec
    return None


def _build_stopped_record(combined_answer, combined_codes, usage, charts,
                          live_fields=None):
    """Persist a STOPPED AI turn (user clicked Stop) — never the planner
    NO_CODE fallback or a late single-shot result.

    ≥1 chart produced → the partial charts (same per-chart code/chart_data
    shape as a normal multi-chart turn) with a stopped marker appended to the
    content, plus a `stopped: True` flag. 0 charts → a short "Response stopped
    by user." turn. Returns a record dict (never None — a stop always yields a
    turn so reload shows it consistently). Reuses _build_chart_entry /
    _persistable_chart_data so per-chart Show data/code survive reload.
    """
    note = "Response stopped by user."
    imgs = [c for c in charts if c.get("image_base64")]
    if imgs:
        answers = [c.get("answer", "") for c in imgs if c.get("answer")]
        base = "\n\n".join(a for a in answers if a)
        content = (base + "\n\n⏹ " + note).strip() if base else ("⏹ " + note)
        rec = {
            "role": "ai",
            "content": content,
            "image_base64": None,
            "table": None,
            "code": "\n\n###NEXT_PLOT###\n\n".join(combined_codes or []),
            "usage": usage or {},
            "stopped": True,
            "ts": time.time(),
        }
        if len(imgs) >= 2:
            rec["images"] = [_build_chart_entry(c) for c in imgs]
        elif len(imgs) == 1:
            c = imgs[0]
            rec["image_base64"] = c.get("image_base64")
            if c.get("code"):
                rec["code"] = c["code"]
            cd = _persistable_chart_data(c.get("chart_data"))
            if cd is not None:
                rec["chart_data"] = cd
        _attach_live_fields(rec, live_fields)
        return rec
    return {
        "role": "ai",
        "content": note,
        "stopped": True,
        "ts": time.time(),
    }


# Chat ids are generated as `c_` + 16 hex; anything outside this charset is
# not a chat id, whatever directory happens to exist under chatdata/.
_CHAT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _require_chat(request: Request, chat_id: str):
    email = request.session.get("email")
    if not email:
        return None, JSONResponse({"error": "Not authenticated"}, status_code=401)
    if not (isinstance(chat_id, str) and _CHAT_ID_RE.fullmatch(chat_id)):
        return None, JSONResponse({"error": "Chat not found"}, status_code=404)
    if not local_store.chat_exists(chat_id):
        return None, JSONResponse({"error": "Chat not found"}, status_code=404)
    owner = local_store.get_chat_meta_owner(chat_id)
    if owner == email:
        return email, None
    # Allow shared recipients to access the chat (chat-level + conversation-level
    # sharing both populate meta.json["sharing"]["shared_with"]).
    try:
        store = local_store.ChatDataStore(chat_id)
        sharing = (store.read_meta().get("sharing") or {}).get("shared_with") or []
        if email in [s.lower() for s in sharing]:
            return email, None
    except Exception:
        pass
    return None, JSONResponse({"error": "Access denied"}, status_code=403)


def _access_denied() -> JSONResponse:
    return JSONResponse({"error": "Access denied"}, status_code=403)


def _is_chat_owner(chat_id: str, email: str) -> bool:
    """True only when `email` is the chat's recorded owner. A share recipient
    passes `_require_chat` but is not the owner. Fails closed."""
    try:
        return bool(email) and local_store.get_chat_meta_owner(chat_id) == email
    except Exception as e:
        log_with_sid(chat_id, "warning",
                     f"CHAT_OWNER_CHECK_FAILED {log_safe_text(type(e).__name__, 80)}")
        return False


def _require_chat_owner(request: Request, chat_id: str):
    """`_require_chat`, then owner-only: a share recipient gets 403. Used by
    the mutations that change what the owner's chat IS (descriptions, files,
    who it is shared with, the Auto Analytics deck)."""
    email, err = _require_chat(request, chat_id)
    if err:
        return None, err
    if not _is_chat_owner(chat_id, email):
        return None, _access_denied()
    return email, None


def _conv_in_index(email: str, chat_id: str, conv_id: str) -> bool:
    """True when `conv_id` is a conversation of `chat_id` recorded in the
    caller's OWN conversation index (their own conversations in a shared chat
    and the snapshot copies shared with them). Fails closed."""
    if not conv_id:
        return False
    try:
        return any(row.get("conv_id") == conv_id and row.get("chat_id") == chat_id
                   for row in local_store.AuthStore().list_conversations(email))
    except Exception as e:
        log_with_sid(chat_id, "warning",
                     f"CONV_INDEX_CHECK_FAILED {log_safe_text(type(e).__name__, 80)}")
        return False


def _may_use_conversation(email: str, chat_id: str, conv_id: str) -> bool:
    """The owner may act on any conversation of their chat; anyone else only
    on conversations in their own index."""
    return _is_chat_owner(chat_id, email) or _conv_in_index(email, chat_id, conv_id)


def _conv_of_chat(chat_id: str, conv_id) -> bool:
    """The conversation file exists under THIS chat. Stop and status are keyed
    by conversation id alone, so the chat in the path must be related to it."""
    try:
        if not local_store.valid_conv_id(conv_id):
            return False
        return (local_store._data_root() / "chatdata" / chat_id / "conversations"
                / f"{conv_id}.jsonl").is_file()
    except Exception as e:
        log_with_sid(chat_id, "warning",
                     f"CONV_LOOKUP_FAILED {log_safe_text(type(e).__name__, 80)}")
        return False


# ---------------------------------------------------------------------------
# Stored-code binding for the re-run routes
# ---------------------------------------------------------------------------
# `refresh_item` and the dashboard pin execute / store code the browser posts.
# They accept only code the chat already holds: an AI history row's `code`
# (whole, or one `###NEXT_PLOT###` segment of a joined multi-chart row), the
# code of the turn being generated right now, or the `code` of a durable
# full-table record. Comparison is after CRLF->LF and `strip()` — the browser
# trims with JS `trim()`, which agrees with `str.strip()` except on U+FEFF.
_NEXT_PLOT_MARKER = "###NEXT_PLOT###"

# In-flight codes: {chat_id: {normalized code: count}}. The generating worker
# registers each code BEFORE the event carrying it is queued to the browser and
# removes it after the turn is persisted, so a chart streamed on a `partial`
# event (or shown right after Stop) is refreshable and pinnable before its
# history row exists. A count, not a set: two turns in one chat may carry the
# same code, and one finishing must not unregister the other's.
_INFLIGHT_CODES: dict[str, dict[str, int]] = {}
# The live-table SELECTs beside the codes: {chat_id: {normalized code: sql
# map}}, registered with the code and removed with it, so a refresh of a
# chart streamed mid-turn finds the query before its history row exists.
_INFLIGHT_SQL: dict[str, dict[str, dict]] = {}
_INFLIGHT_LOCK = threading.Lock()


def _normalize_code(code) -> str:
    if not isinstance(code, str):
        return ""
    return code.replace("\r\n", "\n").strip()


def _code_segments(code) -> list[str]:
    """The normalized code, and each non-empty `###NEXT_PLOT###` segment."""
    whole = _normalize_code(code)
    if not whole:
        return []
    out = [whole]
    if _NEXT_PLOT_MARKER in whole:
        out.extend(s for s in (_normalize_code(p) for p in whole.split(_NEXT_PLOT_MARKER))
                   if s)
    return out


def _inflight_add(chat_id: str, code, sql=None) -> None:
    try:
        replaced = False
        with _INFLIGHT_LOCK:
            bucket = _INFLIGHT_CODES.setdefault(chat_id, {})
            for seg in _code_segments(code):
                bucket[seg] = bucket.get(seg, 0) + 1
                if isinstance(sql, dict) and sql:
                    sqls = _INFLIGHT_SQL.setdefault(chat_id, {})
                    if seg in sqls and sqls[seg] != sql:
                        replaced = True      # two turns on one segment
                    sqls[seg] = dict(sql)
        if replaced:
            log_with_sid(chat_id, "info", "INFLIGHT_SQL_REPLACED")
    except Exception as e:
        log_with_sid(chat_id, "warning",
                     f"INFLIGHT_ADD_FAILED {log_safe_text(type(e).__name__, 80)}")


def _inflight_discard(chat_id: str, codes) -> None:
    try:
        with _INFLIGHT_LOCK:
            bucket = _INFLIGHT_CODES.get(chat_id)
            if not bucket:
                return
            for code in codes or []:
                for seg in _code_segments(code):
                    left = bucket.get(seg, 0) - 1
                    if left > 0:
                        bucket[seg] = left
                    else:
                        bucket.pop(seg, None)
                        (_INFLIGHT_SQL.get(chat_id) or {}).pop(seg, None)
            if not bucket:
                _INFLIGHT_CODES.pop(chat_id, None)
                _INFLIGHT_SQL.pop(chat_id, None)
    except Exception as e:
        log_with_sid(chat_id, "warning",
                     f"INFLIGHT_DISCARD_FAILED {log_safe_text(type(e).__name__, 80)}")


def _inflight_has(chat_id: str, normalized: str) -> bool:
    with _INFLIGHT_LOCK:
        return normalized in (_INFLIGHT_CODES.get(chat_id) or {})


def _inflight_sql(chat_id: str, normalized: str):
    with _INFLIGHT_LOCK:
        found = (_INFLIGHT_SQL.get(chat_id) or {}).get(normalized)
        return dict(found) if isinstance(found, dict) else None


def stored_sql_for_code(chat_id: str, code) -> dict | None:
    """The live-table SELECT map (`sql`: df key -> SELECT text, or None for
    a default read) the chat holds for `code`, or None when the code is not
    stored with one. Same lookup order as `code_is_stored`: the in-flight
    turn, then the AI history rows whose code (whole or per segment) holds
    the code AND carry `sql`, then the durable full-table records. Blocking
    file I/O — call it off the event loop. Never raises."""
    try:
        wanted = _normalize_code(code)
        if not wanted:
            return None
        found = _inflight_sql(chat_id, wanted)
        if found is not None:
            return found
        conv_dir = local_store._data_root() / "chatdata" / chat_id / "conversations"
        if conv_dir.is_dir():
            for path in conv_dir.glob("*.jsonl"):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except Exception as e:
                    log_with_sid(chat_id, "warning",
                                 f"SQL_LOOKUP_READ_FAILED "
                                 f"{log_safe_text(type(e).__name__, 80)}")
                    continue
                for line in lines:
                    if '"code"' not in line or '"sql"' not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(row, dict) and row.get("role") == "ai" \
                            and isinstance(row.get("sql"), dict) \
                            and wanted in _code_segments(row.get("code")):
                        return dict(row["sql"])
        full_dir = conv_dir / "full"
        if full_dir.is_dir():
            for path in full_dir.glob("*.json"):
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(rec, dict) and isinstance(rec.get("sql"), dict) \
                        and wanted in _code_segments(rec.get("code")):
                    return dict(rec["sql"])
        return None
    except Exception as e:
        log_with_sid(chat_id, "error",
                     f"SQL_LOOKUP_FAILED {log_safe_text(type(e).__name__, 80)}")
        return None


def code_is_stored(chat_id: str, code) -> bool:
    """True when `code` is code this chat already holds (see the block
    comment above). Order: the in-flight turn (in memory, O(1)), then
    history rows, then the durable full-table records (a new one is written on every table refresh
    and nothing prunes them, so they are read only on a miss). Blocking file
    I/O — call it off the event loop. Never raises; an unexpected failure
    answers False, i.e. the code is not run."""
    try:
        wanted = _normalize_code(code)
        if not wanted:
            return False
        if _inflight_has(chat_id, wanted):
            return True
        conv_dir = local_store._data_root() / "chatdata" / chat_id / "conversations"
        if conv_dir.is_dir():
            for path in conv_dir.glob("*.jsonl"):
                try:
                    lines = path.read_text(encoding="utf-8").splitlines()
                except Exception as e:
                    log_with_sid(chat_id, "warning",
                                 f"CODE_LOOKUP_READ_FAILED "
                                 f"{log_safe_text(type(e).__name__, 80)}")
                    continue
                unreadable = 0
                for line in lines:
                    if '"code"' not in line:
                        continue             # human/welcome rows carry no code
                    try:
                        row = json.loads(line)
                    except Exception:
                        unreadable += 1      # logged once per file below
                        continue
                    if isinstance(row, dict) and row.get("role") == "ai" \
                            and wanted in _code_segments(row.get("code")):
                        return True
                if unreadable:
                    log_with_sid(chat_id, "warning",
                                 f"CODE_LOOKUP_ROWS_SKIPPED count={int(unreadable)}")
        full_dir = conv_dir / "full"
        if full_dir.is_dir():
            for path in full_dir.glob("*.json"):
                try:
                    rec = json.loads(path.read_text(encoding="utf-8"))
                except Exception as e:
                    log_with_sid(chat_id, "warning",
                                 f"CODE_LOOKUP_RECORD_FAILED "
                                 f"{log_safe_text(type(e).__name__, 80)}")
                    continue
                if isinstance(rec, dict) and wanted in _code_segments(rec.get("code")):
                    return True
        return False
    except Exception as e:
        log_with_sid(chat_id, "error",
                     f"CODE_LOOKUP_FAILED {log_safe_text(type(e).__name__, 80)}")
        return False


async def code_is_stored_async(chat_id: str, code) -> bool:
    """`code_is_stored` on the worker pool, like the frame load."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_EXEC, code_is_stored, chat_id, code)


def stored_chart_for_code(chat_id: str, code) -> str | None:
    """The Plotly chart a stored AI row of this chat rendered from `code`, or
    None. A multi-chart row matches per chart (its own `code`, else the
    matching segment of the legacy joined code). Used by the dashboard tile
    export for a snapshot the browser posted at pin time: the PNG renderer
    only ever sees chart markup the server itself produced. Blocking file I/O;
    never raises."""
    try:
        from routes.report import _is_plotly_html
        wanted = _normalize_code(code)
        if not wanted:
            return None
        conv_dir = local_store._data_root() / "chatdata" / chat_id / "conversations"
        if not conv_dir.is_dir():
            return None
        for path in sorted(conv_dir.glob("*.jsonl")):
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except Exception as e:
                log_with_sid(chat_id, "warning",
                             f"CHART_LOOKUP_READ_FAILED {log_safe_text(type(e).__name__, 80)}")
                continue
            for line in lines:
                if '"image_base64"' not in line or '"code"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict) or row.get("role") != "ai":
                    continue
                imgs = row.get("images")
                if isinstance(imgs, list) and imgs:
                    legacy = [s for s in str(row.get("code") or "").split("###NEXT_PLOT###")]
                    for idx, img in enumerate(imgs):
                        if not isinstance(img, dict):
                            continue
                        own = img.get("code") or (legacy[idx] if idx < len(legacy) else None)
                        html = img.get("image_base64")
                        if (_normalize_code(own) == wanted and isinstance(html, str)
                                and _is_plotly_html(html)):
                            return html
                else:
                    html = row.get("image_base64")
                    if (wanted in _code_segments(row.get("code")) and isinstance(html, str)
                            and _is_plotly_html(html)):
                        return html
        return None
    except Exception as e:
        log_with_sid(chat_id, "error",
                     f"CHART_LOOKUP_FAILED {log_safe_text(type(e).__name__, 80)}")
        return None


def _code_not_stored(status_code: int) -> JSONResponse:
    return JSONResponse({"error": "This item's code is not part of the chat's history.",
                         "code": "CODE_NOT_STORED"}, status_code=status_code)


def _event_sql(event):
    """The live-table SELECT map an event carries (a partial's / the done
    event's `sql`, a single answer's `result.sql`), or None."""
    if not isinstance(event, dict):
        return None
    src = (event.get("result") or {}) if event.get("single_response") else event
    sql = src.get("sql") if isinstance(src, dict) else None
    return dict(sql) if isinstance(sql, dict) and sql else None


def _drop_uncovered_live_keys(email: str, store, dfs: dict, schema_docs: dict,
                              sid: str) -> list:
    """Role gate BEFORE the planner: the chat's non-connector LIVE keys whose
    table the requester's role does not cover are removed from `dfs` and
    `schema_docs` in place (logged `LIVE_ROLE_DROPPED`), so the planner is
    never shown a table this user may not query and no brain call is spent
    on it. A failure inside the gate drops every live key (fail closed);
    `run_chat_local._ensure_live` checks again before any fetch. Returns the
    dropped tables' display names (the caller ends the turn with the denial
    sentence when nothing is left to plan on)."""
    live_keys = [k for k, d in (schema_docs or {}).items()
                 if isinstance(d, dict) and d.get("live") and k in (dfs or {})]
    if not live_keys:
        return []
    entries = {}
    try:
        import roles_store
        entries = {e.get("file_name"): e
                   for e in local_store.db_entries_from_meta(store.read_meta())}
        allowed = roles_store.allowed_table_ids_for(email)
        drop = []
        for key in live_keys:
            db = (entries.get(key) or {}).get("db") or {}
            if db.get("is_connector"):
                continue
            if db.get("table_id") not in allowed:
                drop.append(key)
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"LIVE_ROLE_DROP_FAILED error={log_safe_text(type(e).__name__, 80)}",
                     chat_id=log_safe_text(str(store.chat_id), 80))
        drop = list(live_keys)
    names = []
    for key in drop:
        dfs.pop(key, None)
        schema_docs.pop(key, None)
        db = (entries.get(key) or {}).get("db") or {}
        names.append(str(db.get("display_name") or key))
        log_with_sid(sid, "info",
                     f"LIVE_ROLE_DROPPED table={log_safe_text(str(key), 120)}",
                     chat_id=log_safe_text(str(store.chat_id), 80))
    return names


def _live_denied_text(dropped: list, chat_id: str, sid: str):
    """The denial sentence for a turn whose EVERY frame was a live table the
    requester's role does not cover: the turn ends with it (persisted as the
    AI row like any answer) and the planner is never called. None when
    something is left to plan on."""
    if not dropped:
        return None
    text = run_chat_local._LIVE_ROLE_DENIED_TEXT.format(table=", ".join(dropped))
    log_with_sid(sid, "warning",
                 f"LIVE_ROLE_DENIED table={log_safe_text(', '.join(dropped), 200)}",
                 chat_id=log_safe_text(str(chat_id), 80))
    return text


def _denied_events(text: str):
    """The one-event generator standing in for `run_chat_multi_plot` on a
    denied turn: a single answer carrying the sentence, so the stream, the
    persistence and edit-regenerate handle it exactly like any answer."""
    yield {"single_response": True,
           "result": {"text": text, "image_base64": None, "table": None,
                      "code": None, "usage": {}}}


def _prefetch_stored_live(store, code: str, dfs: dict, sid: str):
    """Re-run the live-table SELECTs a stored answer was computed with, for
    the refresh paths (per-item refresh, full-table re-execution, dashboard
    tiles). Blocking — call it off the event loop.

    Returns (error, sql_used): `error` is None when every referenced live
    key is in place, else the `{ok: False, ...}` payload the caller answers
    with — `code: "LIVE_NO_QUERY"` when a referenced live key has no stored
    query (the table went live after the answer; a capped default read
    would silently change what the answer computes), or the value-free
    class sentence when the fetch failed. A key whose stored value is None
    was answered from the default read and gets it again. A table back in
    snapshot mode is not live any more: the loader served its parquet and
    the stored SQL is ignored. The caller's role gate ran before this
    (`check_role=False`)."""
    try:
        schema_docs = store.schema_docs()
        specs = run_chat_local._live_specs(schema_docs, dfs)
        if not specs:
            return None, {}
        named, generic = run_chat_local._referenced_live_keys(
            code, list(specs), list(dfs or {}),
            first_key=next(iter(dfs or {}), None))
        referenced = list(dict.fromkeys(named + generic))
        if not referenced:
            return None, {}
        sql_map = stored_sql_for_code(store.chat_id, code) or {}
        missing = [k for k in referenced if k not in sql_map]
        if missing:
            return _live_no_query(store, missing[0]), {}
        state = run_chat_local._new_live_state()
        state["sql_map"] = {k: v for k, v in sql_map.items()
                            if isinstance(v, str) and v.strip()}
        ok = run_chat_local._ensure_live(sid, dfs, schema_docs, code, state,
                                         None, check_role=False)
        if ok:
            return None, dict(state.get("sql_used") or {})
        return {"ok": False,
                "error": state.get("failed_text")
                or "The database could not run the query."}, {}
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"REFRESH_LIVE_PREFETCH_FAILED "
                     f"{log_safe_text(type(e).__name__, 80)}",
                     chat_id=log_safe_text(str(store.chat_id), 80))
        return {"ok": False, "error": "Refresh failed."}, {}


def _live_no_query(store, key: str) -> dict:
    name = key
    try:
        for e in local_store.db_entries_from_meta(store.read_meta()):
            if e.get("file_name") == key:
                name = (e.get("db") or {}).get("display_name") or key
                break
    except Exception as e:
        log_with_sid(log_safe_text(str(store.chat_id), 80), "info",
                     f"LIVE_NO_QUERY_NAME_LOOKUP_FAILED "
                     f"{log_safe_text(type(e).__name__, 80)}")
    return {"ok": False, "code": "LIVE_NO_QUERY",
            "error": f"{name} is now a live table; ask the question again to "
                     f"re-run it."}


def _event_codes(event) -> list:
    """The codes a generator event shows the browser: a streamed chart's
    `code`, a multi-chart done's `combined_codes`, a single answer's code."""
    if not isinstance(event, dict):
        return []
    if event.get("single_response"):
        codes = [(event.get("result") or {}).get("code")]
    elif event.get("partial"):
        codes = [event.get("code")]
    elif event.get("done"):
        codes = list(event.get("combined_codes") or [])
    else:
        codes = []
    return [c for c in codes if isinstance(c, str) and c.strip()]


# ---------------------------------------------------------------------------
# Welcome + schema + history
# ---------------------------------------------------------------------------
@router.get("/{chat_id}/welcome")
async def welcome(request: Request, chat_id: str):
    """Same response shape as the B2C `/api/chat/{chat_id}/welcome`:
    {message, language, suggested_questions} — `dashboard.js` reads
    `welcomeData.message` and `welcomeData.suggested_questions`.
    """
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    msg = meta.get("welcome_message") or store.get_welcome() or ""
    questions = meta.get("suggested_questions") or store.get_suggested_questions() or []
    # Detect language from file descriptions (same logic as global welcome handler)
    from schema_builder import _detect_language
    descs = [f.get("file_description", "") for f in meta.get("files", []) if f.get("file_description")]
    lang = _detect_language(" ".join(descs)) if descs else "en"
    return {
        "message": msg,
        "language": lang,
        "suggested_questions": questions,
    }


def _empty_dataset_response(store, chat_id: str) -> JSONResponse:
    """400 for a chat whose dataframes are empty. When the chat's registered
    database tables are what vanished, the message NAMES them and says why —
    the bare "Chat dataset is empty." left the user with no next step. Genuinely
    empty chats keep that original wording."""
    text, missing = local_store.empty_dataset_message(store.read_meta())
    body = {"error": text}
    if missing:
        body["code"] = "DB_TABLES_MISSING"
        body["missing_tables"] = missing
        # The display name is registry metadata, not a constant — escape and
        # cap each one; `reason` is this module's own fixed vocabulary.
        log_with_sid(chat_id, "warning",
                     "CHAT_DB_TABLES_MISSING " + ", ".join(
                         f"{log_safe_text(str(m['display_name']), 120)}({m['reason']})"
                         for m in missing))
    return JSONResponse(body, status_code=400)


@router.get("/{chat_id}/schema")
async def get_schema(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    # "Data as of" for chats that use registered database tables. Additive:
    # file-only chats get db_tables=[] and data_as_of=None (UI renders
    # nothing). refreshed_at prefers the REGISTRY (authoritative) over the
    # chat meta's display cache; a table gone from the registry or whose
    # snapshot file vanished reports missing=true (load_dataframes skips that
    # key, and the frontend key-freeze already disables items referencing it).
    db_tables = []
    data_as_of = None
    db_entries = local_store.db_entries_from_meta(meta)
    if db_entries:
        try:
            from db_sources import DataSourceStore
            registry = {t.get("id"): t for t in DataSourceStore().list_tables()}
        except Exception:
            registry = {}
        # Role gate (additive `allowed` flag so the UI can grey refresh
        # buttons proactively). Helper failure → None → every row reports
        # allowed=true (the server-side refresh gate is the enforcement).
        allowed_ids = None
        try:
            import roles_store
            allowed_ids = roles_store.allowed_table_ids_for(email)
        except Exception as e:
            log_with_sid(email, "warning",
                         f"SCHEMA_ROLE_PROBE_FAILED: {log_safe_text(str(e), 200)}",
                         chat_id=chat_id)
        # ONE classifier for "this table can no longer be loaded" (also used by
        # the empty-dataset message, which needs the REASON as well).
        try:
            missing_by_id = {m["table_id"]: m for m in local_store.missing_db_tables(meta)}
        except Exception as e:
            log_with_sid(email, "warning",
                         f"DB_TABLE_MISSING_PROBE_FAILED: {log_safe_text(str(e), 200)}",
                         chat_id=chat_id)
            missing_by_id = {}
        from db_sources import table_mode
        for entry in db_entries:
            db = entry.get("db") or {}
            tid = db.get("table_id")
            reg = registry.get(tid)
            live = reg is not None and table_mode(reg) == "live"
            # A live table is queried at question time: it has no "as of".
            refreshed = None if live else \
                ((reg or {}).get("refreshed_at") or db.get("refreshed_at"))
            missing = tid in missing_by_id
            db_tables.append({
                "df_key": entry.get("file_name"),
                "table_id": tid,
                "display_name": db.get("display_name") or entry.get("file_name"),
                "is_connector": bool(db.get("is_connector")),
                "auto_included": bool(db.get("auto_included")),
                "row_count": (reg or {}).get("row_count") or db.get("row_count"),
                "refreshed_at": refreshed,
                "missing": missing,
                "live": live,
                "allowed": bool(db.get("is_connector")) or allowed_ids is None
                           or tid in allowed_ids,
            })
        stamps = [t["refreshed_at"] for t in db_tables
                  if t.get("refreshed_at") and not t.get("live")]
        data_as_of = min(stamps) if stamps else None  # oldest data in the chat
    return {
        "chat_id": chat_id,
        "files": meta.get("files", []),
        "common_fields": meta.get("common_fields", []),
        "db_tables": db_tables,
        "data_as_of": data_as_of,
        # The page hides the owner-only actions (descriptions, Add Data,
        # Auto Analytics) for a share recipient instead of letting them 403.
        "is_owner": _is_chat_owner(chat_id, email),
    }


@router.get("/{chat_id}/file_fingerprints")
async def file_fingerprints(request: Request, chat_id: str):
    """Identity of this chat's SOURCE files for the Add Data name-collision
    dialog: {files: {source_filename: {size_bytes, sha256}}}. The browser
    compares sizes first and hashes the selected File locally only on a size
    tie — everything stays client↔client, nothing reaches the brain
    (Article II). Hashing is disk work → executor (never on the event loop).
    Article IV: any failure degrades to an empty/partial map — the frontend
    then falls back to name-only collision handling."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    import hashlib

    store = local_store.ChatDataStore(chat_id)

    def _compute() -> dict:
        out: dict = {}
        for fp in sorted(store.files_dir.iterdir()):
            if not (fp.is_file() and not fp.name.startswith(".")):
                continue
            # The name is always reported — a hash/stat failure degrades to a
            # name-only entry (nulls), which the frontend treats as "contents
            # unknown → show the dialog", never as "no collision".
            entry: dict = {"size_bytes": None, "sha256": None}
            try:
                entry["size_bytes"] = fp.stat().st_size
                h = hashlib.sha256()
                with fp.open("rb") as fh:
                    for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                        h.update(chunk)
                entry["sha256"] = h.hexdigest()
            except Exception as e:
                # `fp.name` is read back from the files directory, so its
                # newline-freedom rests on another module's sanitizer rather
                # than on anything visible here; the OSError quotes it too.
                log_with_sid(chat_id, "warning",
                             f"FINGERPRINT_FAILED {log_safe_text(fp.name, 200)}: "
                             f"{log_safe_text(str(e), 200)}")
            out[fp.name] = entry
        return out

    try:
        loop = asyncio.get_running_loop()
        files = await loop.run_in_executor(_EXEC, _compute)
    except Exception as e:
        log_with_sid(chat_id, "error",
                     f"FINGERPRINTS_ERROR: {log_safe_text(str(e), 200)}")
        files = {}
    return {"files": files}


def _probe_structure(data: bytes, filename: str) -> dict | None:
    """Fast, best-effort header-level structure of a tabular file WITHOUT the
    full detection pipeline: {sheet_name_or_'': [column strings]}.

    - .xlsx/.xlsm — openpyxl read_only: VISIBLE sheets, first non-empty row
      within the first 50 rows per sheet as approximate columns.
    - .csv/.tsv — first non-empty row within the first 50 lines (exact header).
    Returns None for unsupported formats or any parse failure — callers must
    then skip the comparison (Article IV: never raise)."""
    import io as _io
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    try:
        if ext in ("xlsx", "xlsm"):
            from openpyxl import load_workbook
            from excel_table_detector import inspect_workbook_if_zip
            # A decompression bomb is refused before openpyxl opens it (the
            # raise lands in the except below: no comparison, never a crash).
            inspect_workbook_if_zip(_io.BytesIO(data), filename)
            wb = load_workbook(_io.BytesIO(data), read_only=True, data_only=True)
            out: dict = {}
            try:
                for ws in wb.worksheets:
                    if getattr(ws, "sheet_state", "visible") != "visible":
                        continue
                    header: list = []
                    for row in ws.iter_rows(min_row=1, max_row=50, values_only=True):
                        cells = ["" if v is None else str(v).strip() for v in row]
                        while cells and not cells[-1]:
                            cells.pop()
                        if any(cells):
                            header = cells
                            break
                    out[ws.title] = header
            finally:
                wb.close()
            return out
        if ext in ("csv", "tsv"):
            import csv as _csv
            text = data.decode("utf-8-sig", errors="replace")
            reader = _csv.reader(_io.StringIO(text),
                                 delimiter="\t" if ext == "tsv" else ",")
            for i, row in enumerate(reader):
                if i >= 50:
                    break
                cells = [str(v).strip() for v in row]
                while cells and not cells[-1]:
                    cells.pop()
                if any(cells):
                    return {"": cells}
            return {"": []}
        return None
    except Exception:
        return None


@router.post("/{chat_id}/probe_columns")
async def probe_columns(request: Request, chat_id: str,
                        file: UploadFile = File(...)):
    """Header-level structure comparison for the Add Data collision dialog:
    the uploaded file vs the chat's EXISTING same-named file, WITHOUT running
    the detection pipeline (capped at the first 50 rows per sheet — fast).
    Client-container only; nothing reaches the brain (Article II). Cell values
    are returned to the browser but never logged (filename + match only).
    Any failure → {ok: false} — the dialog keeps its generic warning."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    try:
        # Sanitized, never just `.name`: Path("..").name is "..", which made
        # `files_dir / fname` point at the parent directory.
        fname = local_store.sanitize_upload_filename(file.filename or "")
        if not fname:
            return {"ok": False}
        store = local_store.ChatDataStore(chat_id)
        existing_path = store.files_dir / fname
        if not existing_path.exists():
            return {"ok": False}
        data = await file.read()
        loop = asyncio.get_running_loop()
        uploaded = await loop.run_in_executor(_EXEC, _probe_structure, data, fname)
        existing_bytes = await loop.run_in_executor(_EXEC, existing_path.read_bytes)
        existing = await loop.run_in_executor(_EXEC, _probe_structure, existing_bytes, fname)
        if uploaded is None or existing is None:
            return {"ok": False}
        match = uploaded == existing
        log_with_sid(chat_id, "info", f"PROBE_COLUMNS file={fname} match={match}")
        return {"ok": True, "match": match, "uploaded": uploaded, "existing": existing}
    except Exception as e:
        # openpyxl/csv raise ABOUT the file's contents and quote the cell they
        # choked on, so this message is a customer value wearing a library's
        # error text — the route's contract is that values are never logged.
        log_with_sid(chat_id, "warning",
                     f"PROBE_COLUMNS_FAILED: {log_safe_text(str(e), 200)}")
        return {"ok": False}


@router.post("/{chat_id}/schema")
async def save_schema(request: Request, chat_id: str):
    email, err = _require_chat_owner(request, chat_id)
    if err:
        return err
    body = await request.json()
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    # Body shape from dashboard.js: {files: [{file_name, fields:{...}, file_description}]}
    posted = {f.get("file_name"): f for f in (body.get("files") or []) if f.get("file_name")}
    for entry in meta.get("files", []):
        name = entry.get("file_name")
        if name in posted:
            p = posted[name]
            if "fields" in p:
                entry.setdefault("schema", {})["fields"] = p["fields"]
            if "file_description" in p:
                entry["file_description"] = p["file_description"]
    store.write_meta(meta)
    return {"ok": True}


@router.get("/{chat_id}/conversation/{conv_id}/history")
async def history(request: Request, chat_id: str, conv_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    if not _may_use_conversation(email, chat_id, conv_id):
        return _access_denied()
    store = local_store.ChatDataStore(chat_id)
    return {"history": _clean_history_rows(store.get_history(conv_id))}


def _clean_history_rows(rows):
    """History rows as served: each row's `table` / `tables` styled markup
    passes `html_sanitize` (rows written before the sanitiser existed carry
    it raw). Shallow copies — the stored conversation file is never
    rewritten by a read."""
    if not isinstance(rows, list):
        return rows
    out = []
    for row in rows:
        if isinstance(row, dict) and ("table" in row or "tables" in row):
            row = dict(row)
            if "table" in row:
                row["table"] = html_sanitize.clean_table(row["table"])
            if "tables" in row:
                row["tables"] = html_sanitize.clean_tables(row["tables"])
        out.append(row)
    return out


@router.get("/{chat_id}/conversation/{conv_id}/status")
async def conversation_status(request: Request, chat_id: str, conv_id: str):
    """Is a generation worker currently running for this conversation?

    Registry lookup only (no I/O). Lets a page reloaded/reopened mid-generation
    show the working indicator and block new questions until the worker has
    persisted the AI turn. Mirrors the Auto Analytics status-poll pattern.
    Open to every reader of the chat, for conversations of THIS chat only.
    """
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    if not _conv_of_chat(chat_id, conv_id):
        return _access_denied()
    return {"generating": _is_generating(conv_id)}


@router.post("/{chat_id}/conversation/{conv_id}/stop")
async def conversation_stop(request: Request, chat_id: str, conv_id: str):
    """Request cancellation of an in-progress generation for this conversation.

    Cooperative: the worker halts at the NEXT chart boundary (the in-flight
    chart still finishes), persists the partial AI turn (the charts produced so
    far, same shape as a normal turn), and emits a normal `done`. Idempotent —
    setting the flag when nothing is running is a harmless no-op.
    The conversation must belong to THIS chat, and a non-owner may stop only
    a conversation in their own index.
    """
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    if not (_conv_of_chat(chat_id, conv_id)
            and _may_use_conversation(email, chat_id, conv_id)):
        return _access_denied()
    _request_cancel(conv_id)
    # `conv_id` is request-controlled at every one of these log sites (a path
    # segment here, a JSON body field in the stream) and is NOT validated
    # before the line is written — `valid_conv_id` guards the STORE, which
    # returns empty rather than refusing the request. Every context value is
    # rendered raw, so escape it; the escape is the identity for a real id.
    log_with_sid("stop", "info", "CHAT_STOP_REQUESTED", user=email,
                 chat_id=chat_id, conv_id=log_safe_text(str(conv_id), 80))
    return {"ok": True, "stopping": True}


@router.get("/{chat_id}/history")
async def chat_history(request: Request, chat_id: str):
    """Legacy single-conversation history — return the most recent conv.
    The newest conversation may be anyone's, so a non-owner gets none."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    if not _is_chat_owner(chat_id, email):
        return {"history": []}
    store = local_store.ChatDataStore(chat_id)
    # Pick the newest conversation file
    try:
        convs = sorted(store.conversations_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not convs:
            return {"history": []}
        conv_id = convs[0].stem
        return {"history": _clean_history_rows(store.get_history(conv_id)),
                "conv_id": conv_id}
    except Exception:
        return {"history": []}


@router.post("/{chat_id}/deactivate")
async def deactivate(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    local_store.AuthStore().deactivate_chat(email, chat_id)
    return {"ok": True}


# ---------------------------------------------------------------------------
# SSE chat stream  — the working chat path
# ---------------------------------------------------------------------------
@router.post("/{chat_id}/chat/stream")
async def chat_stream(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    body = await request.json()
    question = (body.get("question") or "").strip()
    conv_id = body.get("conv_id")
    if not question:
        return JSONResponse({"error": "Question cannot be empty."}, status_code=400)
    # A supplied conversation is read into the prompt and appended to: a
    # non-owner may continue only a conversation in their own index. Checked
    # before any history is read or written.
    if conv_id and not _may_use_conversation(email, chat_id, conv_id):
        return _access_denied()

    store = local_store.ChatDataStore(chat_id)
    busy = _auto_analysis_busy_response(store, email, chat_id)
    if busy is not None:
        return busy
    # Off the event loop — parsing the source files blocks every other
    # request otherwise (same pattern as _reexecute_full_df / refresh_item).
    loop = asyncio.get_running_loop()
    dfs = await loop.run_in_executor(
        _EXEC, lambda: store.load_dataframes(include_live=True))
    if not dfs:
        return _empty_dataset_response(store, chat_id)
    schema_docs = await loop.run_in_executor(_EXEC, store.schema_docs)
    sid = secrets.token_hex(8)
    # Live tables the requester's role does not cover leave before the
    # planner sees them; a chat left with NO frame by that drop ends with the
    # denial sentence instead of a brain call on an empty schema.
    dropped = await loop.run_in_executor(
        _EXEC, _drop_uncovered_live_keys, email, store, dfs, schema_docs, sid)
    denied_text = _live_denied_text(dropped, chat_id, sid) if not dfs else None
    # Dataset profiles (computed facts for the planner). Backfilled from the
    # stored frames when missing/stale; a failure never blocks the chat.
    try:
        dataset_profiles = await loop.run_in_executor(
            _EXEC, local_store.ensure_chat_profiles, store, dfs)
    except Exception:
        dataset_profiles = {}

    if not conv_id:
        conv_id = store.new_conversation(title=question[:80])
        local_store.AuthStore().record_conversation(email, chat_id, conv_id, title=question[:80])
        # Seed the conversation with the welcome message (matches B2C)
        welcome = store.get_welcome()
        if welcome:
            store.append_history(conv_id, {"role": "ai", "content": welcome, "ts": time.time()})

    history_rows = store.get_history(conv_id)
    store.append_history(conv_id, {"role": "human", "content": question, "ts": time.time()})

    # `q` is the user's own free text and `log_with_sid` renders every context
    # value RAW, so a pasted newline forges a whole record with one request.
    log_with_sid(sid, "info", "CHAT_STREAM_REQ", user=email, chat_id=chat_id,
                 conv_id=log_safe_text(str(conv_id), 80),
                 q=log_safe_text(question, 120))

    async def _sse_generator():
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _worker():
            # PART A: accumulate the AI turn and persist it EXACTLY ONCE here, in
            # the worker thread, so a browser refresh/close (which cancels the SSE
            # consumer below) can never discard the generated response. The
            # consumer performs live UX only — it does NO persistence. The worker
            # runs to completion regardless of the connection.
            w_charts: list[dict] = []      # per-chart {image_base64, answer, code, chart_data}
            w_combined_answer = None        # set by the multi-plot done event
            w_combined_codes: list = []
            w_usage: dict = {}
            w_single = None                 # single-shot result dict
            w_full_table_key = None         # durable key for a tabular result
            w_full_table_keys = None        # per-table keys for a multi-table result
            w_combined_tables = None        # tables of a mixed charts+tables answer
            w_live: dict = {}               # sql / live_truncated / live_rows of the turn
            err_msg = None
            cancelled = False               # STOP requested mid-generation
            w_inflight: list = []           # codes registered for refresh/pin
            # Clear any stale cancel flag from a prior attempt, then mark this
            # conv in-progress for the duration of the worker so a page reloaded
            # mid-generation sees generating=true. Both flags are cleared in
            # `finally` AFTER persistence — once unmarked, the AI turn is readable.
            _clear_cancel(conv_id)
            _mark_generating(conv_id)
            try:
                # Use the multi-plot generator: it yields per-chart partials when
                # the planner emitted ###NEXT_PLOT### blocks, and a single
                # {single_response, result} for normal one-shot answers.
                gen = _denied_events(denied_text) if denied_text else \
                    run_chat_local.run_chat_multi_plot(
                        sid=sid, dfs=dfs, schema_docs=schema_docs,
                        question=question, history_rows=history_rows,
                        user_email=email, dataset_profile=dataset_profiles,
                    )
                for event in gen:
                    # Durable full-table persistence for a single-shot tabular
                    # result: write {columns, rows, code, total_rows} to disk and
                    # stash the key ON the shared event dict BEFORE the consumer
                    # sees it — so the live download and the persisted history
                    # record share ONE durable key that re-executes for the FULL
                    # result (and survives a container restart / reload).
                    if isinstance(event, dict) and event.get("single_response"):
                        _res = event.get("result") or {}
                        _tbl = _res.get("table")
                        if isinstance(_tbl, dict) and _tbl.get("rows"):
                            _k = _persist_full_table(store, _tbl, _res.get("code"),
                                                     sql=_res.get("sql"),
                                                     first_key=next(iter(dfs), None))
                            if _k:
                                event["full_table_key"] = _k
                                w_full_table_key = _k
                        # Multi-table answer (RESULT was a dict of DataFrames):
                        # one durable key per table, each tagged with the dict
                        # entry (`result_key`) it re-executes to.
                        _tbls = _res.get("tables")
                        if isinstance(_tbls, list) and _tbls:
                            _keys = []
                            for _t in _tbls:
                                _k = _persist_full_table(
                                    store, _t, _res.get("code"),
                                    result_key=_t.get("title"),
                                    sql=_res.get("sql"),
                                    first_key=next(iter(dfs), None))
                                _keys.append(_k)
                            event["full_table_keys"] = _keys
                            w_full_table_keys = _keys
                    # Mixed dashboard done event (charts + KPI/table blocks):
                    # persist each table with its OWN block code + dict entry.
                    if (isinstance(event, dict) and event.get("done")
                            and not event.get("single_response") and event.get("tables")):
                        _tbls = event.get("tables") or []
                        _codes = event.get("table_codes") or []
                        _rkeys = event.get("table_result_keys") or []
                        _keys = []
                        for _i, _t in enumerate(_tbls):
                            _keys.append(_persist_full_table(
                                store, _t,
                                _codes[_i] if _i < len(_codes) else None,
                                result_key=_rkeys[_i] if _i < len(_rkeys) else None,
                                sql=event.get("sql"),
                                first_key=next(iter(dfs), None)))
                        event["full_table_keys"] = _keys
                    # Register the codes this event shows BEFORE the browser can
                    # see it: its refresh / pin buttons work while the turn is
                    # still being generated. Removed after persistence below.
                    _event_sql_map = _event_sql(event)
                    for _c in _event_codes(event):
                        _inflight_add(chat_id, _c, sql=_event_sql_map)
                        w_inflight.append(_c)
                    # The turn's live fields ride every event (a partial
                    # carries `sql`); the last seen values are persisted.
                    if isinstance(event, dict) and not event.get("single_response"):
                        for _k in ("sql", "live_truncated", "live_rows"):
                            if _k in event:
                                w_live[_k] = event[_k]
                    # Stream live first (UX identical to before), then accumulate.
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", event))
                    try:
                        if isinstance(event, dict):
                            if event.get("partial"):
                                w_usage = event.get("usage") or w_usage
                                if event.get("image_base64"):
                                    w_charts.append({
                                        "image_base64": event.get("image_base64"),
                                        "answer": event.get("answer", ""),
                                        "code": event.get("code"),
                                        "chart_data": event.get("chart_data"),
                                    })
                            elif event.get("done") and not event.get("single_response"):
                                w_combined_answer = event.get("combined_answer", "")
                                w_combined_codes = event.get("combined_codes") or []
                                w_usage = event.get("total_usage") or {}
                                if event.get("tables"):
                                    w_combined_tables = event.get("tables")
                                    w_full_table_keys = event.get("full_table_keys") or w_full_table_keys
                            elif event.get("single_response"):
                                w_single = event.get("result") or {}
                    except Exception:
                        pass
                    # Cooperative STOP: halt at this chart boundary (the chart just
                    # streamed is kept). No further charts are pulled from the gen.
                    if _is_cancelled(conv_id):
                        cancelled = True
                        log_with_sid(sid, "info", "CHAT_STOP_AT_BOUNDARY",
                                     charts=len(w_charts))
                        break
                # If STOP broke us out before the generator's own done event,
                # synthesize the combined result from the charts produced so far
                # (so persistence + the client's done handler behave normally) and
                # emit a done so the live stream finalizes and shows the partial.
                if cancelled and w_single is None and w_combined_answer is None:
                    if w_charts:
                        answers = [c.get("answer", "") for c in w_charts if c.get("answer")]
                        w_combined_answer = "\n\n".join(answers) if answers else "Analysis stopped."
                        w_combined_codes = [c.get("code") for c in w_charts if c.get("code")]
                    loop.call_soon_threadsafe(queue.put_nowait, ("event", {
                        "partial": False, "done": True,
                        "combined_answer": w_combined_answer or "",
                        "combined_codes": w_combined_codes,
                        "total_usage": w_usage,
                    }))
            except TenantRevokedError:
                err_msg = "Service unavailable. Please contact your administrator."
                loop.call_soon_threadsafe(queue.put_nowait, ("error", err_msg))
            except BrainTimeoutError:
                # BrainTimeoutError subclasses BrainError — caught FIRST. A
                # timeout under load (e.g. Auto Analytics occupying the shared
                # connection pool) is contention, not an outage; the old
                # "temporarily unavailable" wording was misleading (QA 2.3).
                err_msg = "The analysis service is busy right now. Please try again in a moment."
                loop.call_soon_threadsafe(queue.put_nowait, ("error", err_msg))
            except BrainError:
                err_msg = "Analysis service is temporarily unavailable."
                loop.call_soon_threadsafe(queue.put_nowait, ("error", err_msg))
            except Exception as e:
                # `e` comes out of the analysis pipeline: pandas raising about
                # a customer frame quotes its labels, and the sandbox's own
                # error text arrives here too.
                log_with_sid(sid, "error",
                             f"CHAT_THREAD_ERROR: {log_safe_text(type(e).__name__, 80)}: "
                             f"{log_safe_text(str(e), 200)}")
                err_msg = "Something went wrong while processing your request."
                loop.call_soon_threadsafe(queue.put_nowait, ("error", err_msg))

            # ── Exactly-once AI-turn persistence (Articles IV & V) ──
            try:
                if cancelled:
                    # User clicked Stop. Persist a STOPPED turn — the partial
                    # charts (if any) or a short "stopped by user" note — never
                    # the planner NO_CODE error or a late single-shot result.
                    record = _build_stopped_record(
                        w_combined_answer, w_combined_codes, w_usage, w_charts,
                        live_fields=w_live)
                else:
                    record = _build_ai_history_record(
                        err_msg, w_single, w_combined_answer, w_combined_codes,
                        w_usage, w_charts, full_table_key=w_full_table_key,
                        full_table_keys=w_full_table_keys,
                        combined_tables=w_combined_tables,
                        live_fields=w_live)
                if record is not None:
                    store.append_history(conv_id, record)
                    if not err_msg and not cancelled:
                        answer_for_title = (
                            (w_single or {}).get("text", "") if w_single is not None
                            else (w_combined_answer or ""))
                        try:
                            human_count = sum(
                                1 for m in store.get_history(conv_id)
                                if m.get("role") == "human")
                            if human_count == 2:
                                _start_title_generation(
                                    email, chat_id, conv_id, question, answer_for_title)
                        except Exception:
                            pass
            except Exception as e:
                # Raised while persisting the answer record — its tables and
                # their labels are what the message can quote.
                log_with_sid(sid, "error",
                             f"CHAT_PERSIST_ERROR: {log_safe_text(type(e).__name__, 80)}: "
                             f"{log_safe_text(str(e), 200)}")
            finally:
                # Unmark only after persistence above has completed, so a status
                # poll that now sees generating=false will find the AI turn saved.
                # Clear the cancel flag too (paired with the start-of-worker clear).
                # The in-flight codes go with it: they are in the history now.
                _inflight_discard(chat_id, w_inflight)
                _unmark_generating(conv_id)
                _clear_cancel(conv_id)
                loop.call_soon_threadsafe(queue.put_nowait, None)

        threading.Thread(target=_worker, daemon=True).start()

        # First event — empty progress + conv_id so the frontend can capture the conv_id
        yield f'data: {json.dumps({"progress": True, "message": "Working...", "conv_id": conv_id})}\n\n'

        # The consumer streams events to the browser and caches live chart_data;
        # it persists NOTHING (the worker owns persistence — exactly-once).
        while True:
            event = await queue.get()
            if event is None:
                break
            tag, payload = event
            if tag == "error":
                err_event = {"error": payload, "conv_id": conv_id, "done": True}
                yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n"
                continue
            if tag == "event":
                ev = payload
                # ── Multi-plot: per-chart partial event (live UX only) ──
                if ev.get("partial"):
                    img = ev.get("image_base64")
                    chart_answer = ev.get("answer", "")
                    # Cache the chart's source data for the client-side
                    # "Show data" button — reuses the full_table cache + endpoint.
                    # Client-only display; nothing to brain. `chart_data` is a
                    # single {rows} table or a multi-subplot {tables:[...]}.
                    chart_data_key = None
                    cd = ev.get("chart_data")
                    if isinstance(cd, dict) and (cd.get("rows") or cd.get("tables")):
                        chart_data_key = _cache_full_table(cd)
                    partial_payload = {
                        "partial": True,
                        "answer": chart_answer,
                        "image_base64": img,
                        "chart_n": ev.get("chart_n"),
                        "chart_total": ev.get("chart_total"),
                        "conv_id": conv_id,
                        "code": ev.get("code"),
                        "chart_data_key": chart_data_key,
                        "tokens": ev.get("usage") or {},
                        # The PNG export's reference to THIS chart (the
                        # export never takes chart markup from the browser).
                        "chart_ref": _chart_ref_for(email, chat_id, img),
                    }
                    yield f"data: {json.dumps(_json_safe(partial_payload), ensure_ascii=False)}\n\n"
                    if img:
                        try:
                            brain_client.post_activity("plot_generated", email,
                                                       {"chat_id": chat_id, "conv_id": conv_id,
                                                        "chart_n": ev.get("chart_n")})
                        except Exception:
                            pass
                    continue

                # ── Multi-plot: final done event (live UX only) ──
                if ev.get("done") and not ev.get("single_response"):
                    answer = ev.get("combined_answer", "")
                    codes = ev.get("combined_codes") or []
                    usage = ev.get("total_usage") or {}
                    out = {
                        "done": True, "partial": False,
                        "conv_id": conv_id,
                        "answer": answer,
                        "image_base64": None, "table": None,
                        # Mixed dashboard answers carry the KPI/table blocks'
                        # results; the worker already stamped per-table keys.
                        "tables": ev.get("tables"),
                        "full_table_keys": ev.get("full_table_keys"),
                        "code": "\n\n###NEXT_PLOT###\n\n".join(codes),
                        "tokens": usage,
                    }
                    yield f"data: {json.dumps(_json_safe(out), ensure_ascii=False)}\n\n"
                    continue

                # ── Single-shot path: {single_response: True, result: ...} ─
                result = ev.get("result") or {}
                if result.get("image_base64"):
                    try:
                        brain_client.post_activity("plot_generated", email,
                                                   {"chat_id": chat_id, "conv_id": conv_id})
                    except Exception:
                        pass
                # The worker already persisted the tabular result to disk and
                # stashed its durable key on the event (re-executes for the FULL
                # result + survives restart). Use it; do not re-cache the preview.
                full_table_key = ev.get("full_table_key")
                # Chart source data for the "Show data" button — reuses the
                # full_table cache + endpoint. Client-only display. Single
                # {rows} table or multi-subplot {tables:[...]}.
                chart_data_key = None
                cd = result.get("chart_data")
                if isinstance(cd, dict) and (cd.get("rows") or cd.get("tables")):
                    chart_data_key = _cache_full_table(cd)
                out = {
                    "done": True,
                    "partial": False,
                    "conv_id": conv_id,
                    "answer": result.get("text", ""),
                    "image_base64": result.get("image_base64"),
                    "table": result.get("table"),
                    "tables": result.get("tables"),
                    "full_table_key": full_table_key,
                    "full_table_keys": ev.get("full_table_keys"),
                    "chart_data_key": chart_data_key,
                    "code": result.get("code"),
                    "tokens": result.get("usage") or {},
                    "chart_ref": _chart_ref_for(email, chat_id, result.get("image_base64")),
                }
                yield f"data: {json.dumps(_json_safe(out), ensure_ascii=False)}\n\n"

    return StreamingResponse(_sse_generator(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })


# ---------------------------------------------------------------------------
# Edit-regenerate — verbatim port of global backend/routes/chat.py
# `edit_regenerate_api`, adapted for the brain/client split (LLM via brain,
# raw data + execution on client). dashboard.js triggers this from the
# pencil-edit affordance on a past user message.
# ---------------------------------------------------------------------------
@router.post("/{chat_id}/edit-regenerate")
async def edit_regenerate(request: Request, chat_id: str):
    """Edit the last user message and regenerate the AI response.

    Body: {edited_question: str, conv_id: str}
    Returns: {answer, image_base64?, table?, full_table_key?, conv_id, tokens}
    (or, for multi-chart responses, {answer, images: [...], conv_id, tokens}).

    Mirrors global's truncate-then-rerun behavior:
      1. Truncate the conv history to drop the last human turn (and everything
         after it).
      2. Append the edited question as the new human turn.
      3. Run `run_chat_multi_plot` against the local dfs (matches the normal
         chat-stream path), persisting the AI turn in the same JSONL shape.
    """
    email, err = _require_chat(request, chat_id)
    if err:
        return err

    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    edited_question = (body.get("edited_question") or "").strip()
    conv_id = (body.get("conv_id") or "").strip()
    if not edited_question:
        return JSONResponse({"error": "Question cannot be empty."}, status_code=400)
    if not conv_id:
        return JSONResponse({"error": "Conversation ID is required."}, status_code=400)
    # Truncates the conversation: a non-owner may edit only one in their own
    # index. Checked before anything is read or written.
    if not _may_use_conversation(email, chat_id, conv_id):
        return _access_denied()

    store = local_store.ChatDataStore(chat_id)
    if not store.root.exists():
        return JSONResponse({"error": "Chat not found."}, status_code=404)
    busy = _auto_analysis_busy_response(store, email, chat_id)
    if busy is not None:
        return busy

    history = store.get_history(conv_id)
    if len(history) < 2:
        return JSONResponse(
            {"error": "Not enough messages to edit. Need at least one exchange."},
            status_code=400,
        )

    # Find the last human turn → keep everything *before* it.
    last_human_idx = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].get("role") == "human":
            last_human_idx = i
            break
    if last_human_idx is None:
        return JSONResponse({"error": "No user message found to edit."}, status_code=400)

    store.truncate_conv_history(conv_id, last_human_idx)
    log_with_sid(chat_id, "info",
                 f"EDIT_REGENERATE user={email} "
                 f"conv_id={log_safe_text(str(conv_id), 80)} "
                 f"truncated_to={last_human_idx}")

    try:
        loop = asyncio.get_running_loop()
        dfs = await loop.run_in_executor(
            _EXEC, lambda: store.load_dataframes(include_live=True))
        if not dfs:
            return _empty_dataset_response(store, chat_id)
        schema_docs = await loop.run_in_executor(_EXEC, store.schema_docs)
        sid = secrets.token_hex(8)
        dropped = await loop.run_in_executor(
            _EXEC, _drop_uncovered_live_keys, email, store, dfs, schema_docs, sid)
        denied_text = _live_denied_text(dropped, chat_id, sid) if not dfs else None
        try:
            dataset_profiles = await loop.run_in_executor(
                _EXEC, local_store.ensure_chat_profiles, store, dfs)
        except Exception:
            dataset_profiles = {}

        history_rows = store.get_history(conv_id)
        # Append the edited human turn after truncation, before running.
        store.append_history(conv_id, {
            "role": "human", "content": edited_question, "ts": time.time(),
        })

        def _run_blocking():
            if denied_text:
                return list(_denied_events(denied_text))
            return list(run_chat_local.run_chat_multi_plot(
                sid=sid, dfs=dfs, schema_docs=schema_docs,
                question=edited_question, history_rows=history_rows,
                user_email=email, dataset_profile=dataset_profiles,
            ))

        events = await loop.run_in_executor(_EXEC, _run_blocking)
    except TenantRevokedError:
        msg = "Service unavailable. Please contact your administrator."
        store.append_history(conv_id, {"role": "ai", "content": msg, "ts": time.time()})
        return JSONResponse({"error": msg, "conv_id": conv_id}, status_code=503)
    except BrainTimeoutError as e:
        # subclass of BrainError — caught first; see the chat_stream worker (QA 2.3)
        msg = "The analysis service is busy right now. Please try again in a moment."
        # No field reaches a log as free text, and the escaping obligation is
        # on the writer. The brain is our own service but not the author of
        # everything it returns — it relays model output — so its error text
        # is escaped like any other string whose origin is not local.
        log_with_sid(chat_id, "warning",
                     f"EDIT_REGENERATE_BRAIN_TIMEOUT: {log_safe_text(str(e), 200)}")
        store.append_history(conv_id, {"role": "ai", "content": msg, "ts": time.time()})
        return JSONResponse({"error": msg, "conv_id": conv_id}, status_code=503)
    except BrainError as e:
        msg = "Analysis service is temporarily unavailable."
        log_with_sid(chat_id, "warning",
                     f"EDIT_REGENERATE_BRAIN_ERROR: {log_safe_text(str(e), 200)}")
        store.append_history(conv_id, {"role": "ai", "content": msg, "ts": time.time()})
        return JSONResponse({"error": msg, "conv_id": conv_id}, status_code=503)
    except Exception as e:
        # Same pipeline as CHAT_THREAD_ERROR above, same untrusted text.
        log_with_sid(chat_id, "error",
                     f"EDIT_REGENERATE_ERROR: {log_safe_text(type(e).__name__, 80)}: "
                     f"{log_safe_text(str(e), 200)}")
        return JSONResponse(
            {"error": "Something went wrong while processing your request."},
            status_code=500,
        )

    # Accumulate multi-plot images (same shape used in the normal SSE path so
    # persistence + frontend rendering match exactly).
    all_images: list[str] = []
    all_answers: list[str] = []
    single_result: dict | None = None
    combined_codes: list[str] = []
    final_usage: dict = {}

    # The answer is persisted inside this loop; its codes count as stored
    # until then (same registry the streaming worker uses).
    inflight = [c for ev in events for c in _event_codes(ev)]
    for ev in events:
        for c in _event_codes(ev):
            _inflight_add(chat_id, c, sql=_event_sql(ev))
    try:
        for ev in events:
            if ev.get("partial"):
                img = ev.get("image_base64")
                if img:
                    all_images.append(img)
                    all_answers.append(ev.get("answer", ""))
                continue
            if ev.get("done") and not ev.get("single_response"):
                combined_codes = ev.get("combined_codes") or []
                final_usage = ev.get("total_usage") or {}
                combined_answer = ev.get("combined_answer", "")
                history_obj: dict = {
                    "role": "ai", "content": combined_answer,
                    "image_base64": None, "table": None,
                    "code": "\n\n###NEXT_PLOT###\n\n".join(combined_codes),
                    "usage": final_usage, "ts": time.time(),
                }
                if len(all_images) >= 2:
                    history_obj["images"] = [
                        {"image_base64": img, "answer": ans}
                        for img, ans in zip(all_images, all_answers) if img
                    ]
                elif len(all_images) == 1:
                    history_obj["image_base64"] = all_images[0]
                _attach_live_fields(history_obj, ev)
                # Mixed dashboard answer: persist the KPI/table blocks' tables with
                # per-table durable keys (same as chat_stream's worker).
                tbls = ev.get("tables") or []
                if tbls:
                    codes_per = ev.get("table_codes") or []
                    rkeys = ev.get("table_result_keys") or []
                    keys = [
                        _persist_full_table(store, t,
                                            codes_per[i] if i < len(codes_per) else None,
                                            result_key=rkeys[i] if i < len(rkeys) else None,
                                            sql=ev.get("sql"),
                                            first_key=next(iter(dfs), None))
                        for i, t in enumerate(tbls)
                    ]
                    history_obj["tables"] = tbls
                    history_obj["full_table_keys"] = keys
                store.append_history(conv_id, history_obj)
                out: dict = {
                    "ok": True, "done": True, "conv_id": conv_id,
                    "answer": combined_answer,
                    "image_base64": None, "table": None,
                    "code": "\n\n###NEXT_PLOT###\n\n".join(combined_codes),
                    "tokens": final_usage,
                }
                if "images" in history_obj:
                    out["images"] = history_obj["images"]
                elif history_obj.get("image_base64"):
                    out["image_base64"] = history_obj["image_base64"]
                if tbls:
                    out["tables"] = tbls
                    out["full_table_keys"] = history_obj.get("full_table_keys")
                return JSONResponse(_json_safe(_with_chart_refs(out, email, chat_id)))

            # Single-shot path
            if ev.get("single_response"):
                single_result = ev.get("result") or {}
                # Persist the tabular result durably (with code) BEFORE the history
                # record, so the key can be embedded → a reloaded conversation
                # re-executes for the FULL Download Excel / Show full table.
                full_table_key = None
                tbl = single_result.get("table")
                if isinstance(tbl, dict) and tbl.get("rows"):
                    full_table_key = _persist_full_table(
                        store, tbl, single_result.get("code"),
                        sql=single_result.get("sql"),
                        first_key=next(iter(dfs), None))
                # Multi-table answer: one durable key per table (see chat_stream).
                full_table_keys = None
                tbls = single_result.get("tables")
                if isinstance(tbls, list) and tbls:
                    full_table_keys = [
                        _persist_full_table(store, t, single_result.get("code"),
                                            result_key=t.get("title"),
                                            sql=single_result.get("sql"),
                                            first_key=next(iter(dfs), None))
                        for t in tbls
                    ]
                ai_record = {
                    "role": "ai",
                    "content": single_result.get("text", ""),
                    "image_base64": single_result.get("image_base64"),
                    "table": single_result.get("table"),
                    "code": single_result.get("code"),
                    "usage": single_result.get("usage"),
                    "ts": time.time(),
                }
                _attach_live_fields(ai_record, single_result)
                if full_table_key:
                    ai_record["full_table_key"] = full_table_key
                if tbls:
                    ai_record["tables"] = tbls
                    if full_table_keys:
                        ai_record["full_table_keys"] = full_table_keys
                store.append_history(conv_id, ai_record)
                chart_data_key = None
                cd = single_result.get("chart_data")
                if isinstance(cd, dict) and (cd.get("rows") or cd.get("tables")):
                    chart_data_key = _cache_full_table(cd)
                out = {
                    "ok": True, "done": True, "conv_id": conv_id,
                    "answer": single_result.get("text", ""),
                    "image_base64": single_result.get("image_base64"),
                    "table": single_result.get("table"),
                    "tables": single_result.get("tables"),
                    "full_table_key": full_table_key,
                    "full_table_keys": full_table_keys,
                    "chart_data_key": chart_data_key,
                    "code": single_result.get("code"),
                    "tokens": single_result.get("usage") or {},
                }
                return JSONResponse(_json_safe(_with_chart_refs(out, email, chat_id)))
    finally:
        _inflight_discard(chat_id, inflight)

    # Should not reach here, but degrade safely.
    return JSONResponse(
        {"error": "No response was produced.", "conv_id": conv_id},
        status_code=500,
    )


# ---------------------------------------------------------------------------
# Filename generator (used by the report download button)
# ---------------------------------------------------------------------------
@router.post("/{chat_id}/generate_filename")
async def generate_filename(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    title = (body.get("title") or "analysis_report").strip()
    import re as _re
    safe = _re.sub(r"[^a-zA-Z0-9_-]", "_", title)[:40] or "analysis_report"
    return {"filename": safe}


# ---------------------------------------------------------------------------
# Title generation — background, fired after 2nd human message
# ---------------------------------------------------------------------------
def _start_title_generation(email: str, chat_id: str, conv_id: str,
                             question: str, answer: str) -> None:
    """Spin a daemon thread to call brain /v1/title and rename the conversation."""
    def _worker():
        try:
            lang_code = _detect_lang_for_title((question + " " + answer)[:300])
            lang_name = {"ka": "Georgian", "ru": "Russian"}.get(lang_code, "English")
            rsp = brain_client.title(
                sid=f"title:{conv_id}", question=question, answer=answer,
                lang=lang_name, user_email=email,
            )
            new_title = (rsp.get("title") or "").strip()
            if new_title:
                local_store.AuthStore().rename_conversation(email, conv_id, new_title)
                # `title` is `!r` (repr escapes CR/LF); conv_id is not.
                log_with_sid(email, "info",
                             f"CONV_TITLE_UPDATED "
                             f"conv_id={log_safe_text(str(conv_id), 80)} "
                             f"title={new_title!r}")
        except Exception as e:
            # The guarded block carries the question, the answer and the
            # returned title, so this message can quote any of the three on
            # top of the brain's own error text.
            log_with_sid(email, "warning",
                         f"TITLE_GEN_FAILED: {log_safe_text(str(e), 200)}")
    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# Chat sharing — implemented (brain SMTP relay sends invites)
# ---------------------------------------------------------------------------
@router.get("/{chat_id}/share")
async def share_get(request: Request, chat_id: str):
    """Return the current chat-level sharing record."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    sharing = meta.get("sharing") or {}
    return {
        "shared_with": sharing.get("shared_with") or [],
        "owner": email,
    }


@router.post("/{chat_id}/share")
async def share_post(request: Request, chat_id: str):
    """Body: {emails: ["a@x.com", ...], message?: "..."}.

    Adds the recipients to this chat's sharing list and asks the brain to
    SMTP-relay an invite email to each. The brain's SMTP relay uses this
    tenant's smtp_* config (set in the per-tenant admin page).
    Owner-only: a recipient cannot hand the chat on.
    """
    email, err = _require_chat_owner(request, chat_id)
    if err:
        return err
    body = await request.json()
    raw_emails = body.get("emails") or []
    if isinstance(raw_emails, str):
        raw_emails = [e.strip() for e in raw_emails.replace(",", "\n").splitlines() if e.strip()]
    recipients = []
    from routes.auth import _EMAIL_RE   # the one address pattern
    for e in raw_emails:
        e = (e or "").strip().lower()
        if _EMAIL_RE.fullmatch(e) and e != email:
            recipients.append(e)
    if not recipients:
        return JSONResponse({"error": "Provide at least one valid recipient email."}, status_code=400)
    message_text = (body.get("message") or "").strip()

    store = local_store.ChatDataStore(chat_id)
    meta = store.read_meta()
    sharing = meta.get("sharing") or {"shared_with": []}
    existing = set(sharing.get("shared_with") or [])
    new_recipients = [r for r in recipients if r not in existing]
    existing.update(new_recipients)
    sharing["shared_with"] = sorted(existing)
    meta["sharing"] = sharing
    store.write_meta(meta)
    # An address that has never signed in gets a password-less placeholder,
    # so whoever types it first at the sign-in page cannot claim the share.
    auth = local_store.AuthStore()
    for rec in recipients:
        auth.ensure_invited_user(rec, email)
    # List the chat in each recipient's sidebar, as the conversation-level
    # share does (the store skips a chat the recipient already lists).
    sidebar_title = meta.get("title") or "Chat"
    sidebar_files = [f.get("file_name") for f in meta.get("files", []) if f.get("file_name")]
    for rec in recipients:
        try:
            auth.record_shared_chat(rec, chat_id, sidebar_title, sidebar_files,
                                    shared_by=email)
        except Exception as e:
            log_with_sid(email, "warning",
                         f"SHARE_SIDEBAR_RECORD_FAILED "
                         f"error={log_safe_text(type(e).__name__, 80)}",
                         chat_id=chat_id, recipient=log_safe_text(rec, 120))

    chat_title = meta.get("title", "")
    smtp_result = {"smtp_configured": False, "sent": [], "failed": []}
    if new_recipients:
        try:
            smtp_result = brain_client.send_share_email(
                to=new_recipients, subject=f"{email} shared an analysis with you",
                sender_email=email, chat_title=chat_title, message=message_text,
            ) or smtp_result
        except (TenantRevokedError, BrainError) as e:
            log_with_sid(email, "warning",
                         f"SHARE_EMAIL_BRAIN_ERROR: {log_safe_text(str(e), 200)}")
        except Exception as e:
            # The relayed payload holds the chat title and the sharer's own
            # comment, so a failure building it quotes user text.
            log_with_sid(email, "warning",
                         f"SHARE_EMAIL_ERROR: {log_safe_text(str(e), 200)}")

    return {
        "ok": True,
        "shared_with": sharing["shared_with"],
        "added": new_recipients,
        "email_sent": bool(smtp_result.get("sent")),
        "smtp_configured": bool(smtp_result.get("smtp_configured")),
        "failed": smtp_result.get("failed") or [],
    }


# ---------------------------------------------------------------------------
# B2C-only features — disabled in enterprise (clean errors so the UI handles gracefully)
# ---------------------------------------------------------------------------
def _disabled(message: str, code: int = 400):
    return JSONResponse({"error": message}, status_code=code)


@router.post("/{chat_id}/publish")
@router.post("/{chat_id}/unpublish")
async def publish_disabled(request: Request, chat_id: str):
    return _disabled("Public publish is not available in the on-prem build.")


@router.get("/{chat_id}/publish-status")
async def publish_status_disabled(request: Request, chat_id: str):
    return {"public": False}


@router.post("/{chat_id}/auto_analysis/start")
async def auto_analysis_start(request: Request, chat_id: str):
    """Kick off the auto-analysis background job. Returns immediately; poll
    `/auto_analysis/status` for state and finally fetch the PPTX from
    `/auto_analysis/download` when status is `done`. Owner-only: the job
    writes its deck and state into the chat."""
    email, err = _require_chat_owner(request, chat_id)
    if err:
        return err
    import auto_analytics as aa
    store = local_store.ChatDataStore(chat_id)
    state = aa.get_auto_analysis_state(store.read_meta())
    if state.get("status") == "processing":
        return {"ok": True, "status": "processing", "message": "Already running."}
    from datetime import datetime, timezone as _tz
    aa._update_state(
        store, status="processing",
        started_at=datetime.now(_tz.utc).isoformat(),
        finished_at=None, error=None, progress="Starting…", pptx_path=None,
    )
    aa.start_job(chat_id, email)
    log_with_sid(email, "info", f"AUTO_ANALYTICS_START chat={chat_id}")
    return {"ok": True, "status": "processing"}


@router.get("/{chat_id}/auto_analysis/status")
async def auto_analysis_status(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    import auto_analytics as aa
    store = local_store.ChatDataStore(chat_id)
    return aa.get_auto_analysis_state(store.read_meta())


@router.get("/{chat_id}/auto_analysis/download")
async def auto_analysis_download(request: Request, chat_id: str):
    """Stream the auto-analysis PPTX. 404 until the job is `done`."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    import auto_analytics as aa
    store = local_store.ChatDataStore(chat_id)
    state = aa.get_auto_analysis_state(store.read_meta())
    if state.get("status") != "done":
        return JSONResponse(
            {"error": "Auto-analysis is not ready", "status": state.get("status")},
            status_code=404,
        )
    pptx_path = store.root / aa.AUTO_ANALYSIS_PPTX_NAME
    if not pptx_path.exists():
        return JSONResponse({"error": "Deck file is missing"}, status_code=404)
    from fastapi.responses import FileResponse
    return FileResponse(
        str(pptx_path),
        media_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        filename=f"auto_analysis_{chat_id[:8]}.pptx",
    )


@router.get("/{chat_id}/suggested-questions")
async def suggested_questions_stub(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    return {"questions": store.get_suggested_questions()}


@router.get("/{chat_id}/full_table/{key}")
async def full_table_get(request: Request, chat_id: str, key: str):
    """Return the FULL row set for this key. The chat stream sets
    `full_table_key` on responses that contain a tabular result; a chart's or
    a dashboard tile's "Show data" fetches the full rows via this endpoint
    (the /lab table block shows a row-count cue, not a full-table action).

    Re-executes the stored code locally to return the complete (uncapped)
    result, falling back to the stored preview rows if re-execution yields
    nothing (e.g. chart-data records, which carry no re-executable code)."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    store = local_store.ChatDataStore(chat_id)
    rec = _load_full_table_record(store, key)
    if not rec:
        return JSONResponse({"error": "Full table not found or expired."}, status_code=404)
    denied = await _live_reexec_denial(email, chat_id, rec.get("code"),
                                       "FULL_TABLE_ROLE_DENIED")
    if denied is not None:
        return denied
    df = await _reexecute_full_df(chat_id, rec.get("code"), rec.get("result_key"))
    if df is not None and not df.empty:
        return _json_safe({
            "columns": list(df.columns),
            "rows": df.to_dict(orient="records"),
            "total_rows": int(len(df)),
        })
    return _json_safe(rec)


# --- Per-chart / per-table refresh -------------------------------------------
# Re-runs ONE item's stored code against the chat's CURRENT dataframes (after
# an Add Data update) and returns the re-rendered chart/table. Purely local
# code re-execution (Article II — nothing touches the brain), same execution
# path _reexecute_full_df uses. Body: {code, kind: "chart"|"table"}.
# Auth/permission/validation failures use HTTP status codes; EXECUTION failures
# return 200 {ok: false, error} so the frontend keeps the previous render and
# shows a small non-blocking note (Article IV: logged, safe fallback).

_DF_KEY_RE = re.compile(r"dfs\[\s*['\"]([^'\"]+)['\"]\s*\]")


class _GateFailed(tuple):
    """The `(frozenset(), [])` answer `_role_refresh_block` gives when it
    crashed. Unpacks exactly like the ordinary answer; the refresh paths
    (`_refresh_role_gate`) and `_live_reexec_block` recognise it and refuse
    on a chat holding a live table. `error_type` is the caught exception's
    type name."""
    error_type = ""


def _gate_failed(error_type: str) -> "_GateFailed":
    out = _GateFailed((frozenset(), []))
    out.error_type = error_type
    return out


def _role_refresh_block(email: str, chat_id: str, code: str):
    """Role gate for the refresh paths (chat refresh_item + dashboard tile
    refresh, both branches). Returns (denied_df_keys, blocked_display_names):

      denied_df_keys — frozenset of the chat's df keys whose backing DB table
        the REQUESTER's role does not cover (non-connector entries only —
        connectors are exempt by design). Passed as drop_df_keys so denied
        frames never enter the exec namespace even when unreferenced.
      blocked_display_names — the denied tables this item's code actually
        references — non-empty means the refresh must be refused; empty
        means the item only touches allowed tables and proceeds (per-table
        semantics). Two rules, added together, over EVERY denied key,
        snapshot and live alike: the `dfs['…']` key regex (the one
        dashboard.js freezes on) and the pre-fetch's own rule
        (`run_chat_local._referenced_live_keys`: a quoted key anywhere, a
        generic `dfs` walk, the `df` alias of the first frame) — so a
        `dfs.get(...)`, `df` or loop-over-`dfs` refresh of a denied table is
        refused instead of computing on whatever frame is left. The
        `denied_df_keys` drop is defence in depth only.

    Genuine denials fail CLOSED by construction (.get() defaults resolve to
    Base → empty grant set; an unreadable registry reads as the default,
    empty one, so every entry counts as denied). Blocking — it loads the chat's frames
    whenever a table is denied; call it off the event loop. A failure of
    that load or of the referencing rule — the denial already known — is
    logged ROLE_GATE_REFERENCE_FAILED and REFUSES naming every denied table.
    An earlier unexpected crash (the meta or the roles read) is logged
    ROLE_GATE_FAILED (the exception TYPE only) and answered
    with the `_GateFailed` marker; the callers decide
    (`_refresh_role_gate`, `_live_reexec_block`)."""
    try:
        meta = local_store.ChatDataStore(chat_id).read_meta()
        entries = [e for e in local_store.db_entries_from_meta(meta)
                   if not (e.get("db") or {}).get("is_connector")]
        if not entries:
            return frozenset(), []
        import roles_store
        allowed = roles_store.allowed_table_ids_for(email)
        denied = {e["file_name"]: ((e.get("db") or {}).get("display_name") or e["file_name"])
                  for e in entries
                  if e.get("file_name")
                  and (e.get("db") or {}).get("table_id") not in allowed}
        if not denied:
            return frozenset(), []
        try:
            keys = {k for k in _DF_KEY_RE.findall(code or "") if k in denied}
            dfs = local_store.ChatDataStore(chat_id).load_dataframes(include_live=True) or {}
            all_keys = list(dict.fromkeys(
                list(dfs) + [e["file_name"] for e in local_store.db_entries_from_meta(meta)
                             if e.get("file_name")]))
            # `_referenced_live_keys` never checks liveness: its `live_keys`
            # argument is only the candidate set, here every denied key.
            named, generic = run_chat_local._referenced_live_keys(
                code or "", list(denied), all_keys,
                first_key=next(iter(dfs), None))
            keys |= {k for k in set(named) | set(generic) if k in denied}
        except Exception as e:
            # The denial is known, only what the code references is not:
            # refuse naming every denied table (fail closed). The type only.
            log_with_sid(log_safe_text(str(email), 254), "warning",
                         f"ROLE_GATE_REFERENCE_FAILED "
                         f"error={log_safe_text(type(e).__name__, 80)}",
                         chat_id=log_safe_text(str(chat_id), 80))
            return frozenset(denied), sorted(set(denied.values()))
        return frozenset(denied), sorted(denied[k] for k in keys)
    except Exception as e:
        # The type only: the gate reads chat meta, the roles and the table
        # registry, so a message could quote table names and df keys.
        log_with_sid(email, "warning",
                     f"ROLE_GATE_FAILED error={log_safe_text(type(e).__name__, 80)} "
                     f"reason=role_gate_helper_failed",
                     chat_id=log_safe_text(str(chat_id), 80))
        return _gate_failed(type(e).__name__)


def _chat_holds_live_entry(chat_id: str) -> bool:
    """True when the chat holds a non-connector database entry whose table
    is registered LIVE (a connector never feeds a refresh's own gate).
    Blocking. Reads the registry directly; any failure answers True — the
    caller then refuses, because whether a stored SELECT could run is
    unknown."""
    try:
        meta = local_store.ChatDataStore(chat_id).read_meta()
        tids = {(e.get("db") or {}).get("table_id")
                for e in local_store.db_entries_from_meta(meta)
                if not (e.get("db") or {}).get("is_connector")}
        tids.discard(None)
        if not tids:
            return False
        import db_sources
        return any(t.get("id") in tids and db_sources.table_mode(t) == "live"
                   for t in db_sources.DataSourceStore().list_tables())
    except Exception as e:                                   # noqa: BLE001
        log_with_sid(log_safe_text(str(chat_id), 80), "warning",
                     f"CHAT_LIVE_PROBE_FAILED error={log_safe_text(type(e).__name__, 80)} "
                     f"reason=live_probe_failed")
        return True


async def _refresh_role_gate(email: str, chat_id: str, code: str):
    """The role gate of the two refresh paths (`refresh_item`, the
    dashboard tile). Returns (drop_df_keys, blocked_display_names, refused).

    `_role_refresh_block` runs off the event loop (it may load the chat's
    frames). A gate crash — its `_GateFailed` marker, or the gate raising —
    REFUSES when the chat holds a live table or that cannot be told
    (`refused`, no table named): a fail-open there would let the stored
    SELECT run for a requester whose role was never checked. A chat without
    a live table keeps the fail-open answer (nothing denied), so a gate bug
    never freezes refreshes of data the user can already see."""
    loop = asyncio.get_running_loop()
    try:
        gate = await loop.run_in_executor(_EXEC, _role_refresh_block,
                                          email, chat_id, code)
    except Exception as e:                                   # noqa: BLE001
        log_with_sid(email, "warning",
                     f"ROLE_GATE_FAILED error={log_safe_text(type(e).__name__, 80)} "
                     f"reason=role_gate_raised",
                     chat_id=log_safe_text(str(chat_id), 80))
        gate = _gate_failed(type(e).__name__)
    if isinstance(gate, _GateFailed):
        try:
            live = await loop.run_in_executor(_EXEC, _chat_holds_live_entry, chat_id)
        except Exception as e:                               # noqa: BLE001
            log_with_sid(email, "warning",
                         f"CHAT_LIVE_PROBE_FAILED error={log_safe_text(type(e).__name__, 80)} "
                         f"reason=live_probe_raised",
                         chat_id=log_safe_text(str(chat_id), 80))
            live = True
        if live:
            log_with_sid(email, "warning", "REFRESH_ROLE_GATE_REFUSED",
                         chat_id=log_safe_text(str(chat_id), 80))
            return frozenset(), [], True
        return frozenset(), [], False
    drop, blocked = gate
    return frozenset(drop or ()), list(blocked or []), False


_ROLE_DENIED_REFRESH_TEXT = ("Your role does not include this table's data — "
                             "refresh is unavailable.")


def _live_reexec_block(email: str, chat_id: str, code: str | None):
    """Role gate for the two routes that re-run a stored answer for its
    FULL result ("Show full table", "Download Excel"). Blocking — call it
    off the event loop. Returns (denied, blocked_display_names).

    The same gate `refresh_item` uses, by table (`_role_refresh_block`),
    narrowed to the LIVE fetch: the item is refused only when its code
    references — by the pre-fetch's own rule (`_referenced_live_keys`: a
    quoted key, a generic `dfs` walk, the `df` alias of the first frame) —
    a live key the requester's role does not cover, because re-running it
    would query the customer database on their behalf. Snapshot
    re-execution is unchanged (viewing existing data is never blocked
    retroactively). FAILS CLOSED: an exception raised here — by the gate
    call, the loader, the schema read or the referencing rule — and a gate
    answer marked `_GateFailed` (the gate swallowed its own crash) are a
    denial naming no table (`LIVE_REEXEC_GATE_FAILED`, the exception type
    only). Neither applies where no live fetch can happen: a record without
    code (nothing is re-run) and a chat holding no live table are served
    before the gate is consulted."""
    try:
        if not (code or "").strip():
            return False, []
        if not _chat_holds_live_entry(chat_id):
            return False, []
        gate = _role_refresh_block(email, chat_id, code)
        if isinstance(gate, _GateFailed):
            log_with_sid(email, "warning",
                         f"LIVE_REEXEC_GATE_FAILED "
                         f"error={log_safe_text(str(gate.error_type or '?'), 80)}",
                         chat_id=log_safe_text(str(chat_id), 80))
            return True, []
        drop, _ = gate
        if not drop:
            return False, []
        store = local_store.ChatDataStore(chat_id)
        dfs = store.load_dataframes(include_live=True)
        schema_docs = store.schema_docs()
        specs = run_chat_local._live_specs(schema_docs, dfs)
        if not specs:
            return False, []
        named, generic = run_chat_local._referenced_live_keys(
            code or "", list(specs), list(dfs or {}),
            first_key=next(iter(dfs or {}), None))
        keys = [k for k in dict.fromkeys(named + generic) if k in drop]
        if not keys:
            return False, []
        names = {}
        for e in local_store.db_entries_from_meta(store.read_meta()):
            if e.get("file_name"):
                names[e["file_name"]] = ((e.get("db") or {}).get("display_name")
                                         or e["file_name"])
        return True, sorted(str(names.get(k) or k) for k in keys)
    except Exception as e:                                   # noqa: BLE001
        log_with_sid(email, "warning",
                     f"LIVE_REEXEC_GATE_FAILED "
                     f"error={log_safe_text(type(e).__name__, 80)}",
                     chat_id=log_safe_text(str(chat_id), 80))
        return True, []


async def _live_reexec_denial(email: str, chat_id: str, code: str | None,
                              event: str):
    """The 403 a full-table / Excel request gets when `_live_reexec_block`
    denies it (the refresh path's `ROLE_DENIED` body — 403 because the Excel
    route's success answer is a byte stream), else None. Decided BEFORE
    `_reexecute_full_df`, so a denied call issues no database query."""
    try:
        loop = asyncio.get_running_loop()
        denied, blocked = await loop.run_in_executor(
            _EXEC, _live_reexec_block, email, chat_id, code)
    except Exception as e:                                   # noqa: BLE001
        log_with_sid(email, "warning",
                     f"LIVE_REEXEC_GATE_FAILED "
                     f"error={log_safe_text(type(e).__name__, 80)}",
                     chat_id=log_safe_text(str(chat_id), 80))
        denied, blocked = True, []
    if not denied:
        return None
    log_with_sid(email, "info",
                 f"{log_safe_text(event, 40)} tables={log_safe_text(', '.join(blocked), 400)}",
                 chat_id=log_safe_text(str(chat_id), 80))
    return JSONResponse({"ok": False, "code": "ROLE_DENIED",
                         "blocked_tables": blocked,
                         "error": _ROLE_DENIED_REFRESH_TEXT}, status_code=403)


async def run_item_refresh(chat_id: str, code: str, kind: str, sid: str,
                           *, drop_df_keys=None) -> dict:
    """Re-run ONE item's stored code against the chat's current dataframes and
    return the re-rendered item. Shared by `refresh_item` (per-message refresh
    in /lab) and the dashboard tile refresh (routes/dashboards.py). Purely
    local (Article II). Returns `{ok: True, ...}` payloads identical to the
    historical refresh_item contract, or `{ok: False, error}` on any execution
    failure (Article IV: logged, safe fallback — never raises).

    `drop_df_keys` — see _reexecute_full_df: role-denied df keys filtered out
    AFTER the (per-chat, user-agnostic) cached load."""
    store = local_store.ChatDataStore(chat_id)
    loop = asyncio.get_running_loop()
    try:
        dfs = await loop.run_in_executor(
            _EXEC, lambda: store.load_dataframes(include_live=True))
        if drop_df_keys:
            dfs = {k: v for k, v in dfs.items() if k not in drop_df_keys}
        if not dfs:
            # Names the de-registered / snapshot-less tables when that is the
            # reason (drop_df_keys is a role denial, not a missing table).
            text, missing = local_store.empty_dataset_message(store.read_meta())
            out = {"ok": False, "error": text}
            if missing:
                out["code"] = "DB_TABLES_MISSING"
                out["missing_tables"] = missing
            return out
        # Live tables: the stored SELECT is re-run first (the caller's role
        # gate already dropped the denied keys above); no stored query for a
        # live key, or a failing one, answers the {ok: False} contract.
        live_err, sql_used = await loop.run_in_executor(
            _EXEC, lambda: _prefetch_stored_live(store, code, dfs, sid))
        if live_err is not None:
            log_with_sid(sid, "info",
                         f"REFRESH_ITEM_LIVE_REFUSED "
                         f"code={log_safe_text(str(live_err.get('code') or 'FETCH_FAILED'), 40)}",
                         chat_id=chat_id)
            return live_err

        if kind == "table":
            from code_exec import safe_execute
            from run_chat_local import _build_table_from_result
            exec_out = await loop.run_in_executor(
                _EXEC, lambda: safe_execute(code, dfs, sid=sid))
            if not isinstance(exec_out, dict) or exec_out.get("error"):
                emsg = (exec_out or {}).get("error", "unknown") if isinstance(exec_out, dict) else "unknown"
                log_with_sid(sid, "warning",
                             f"REFRESH_ITEM_TABLE_EXEC_ERROR: {log_safe_text(str(emsg), 200)}",
                             chat_id=chat_id)
                return {"ok": False, "error": "Could not re-run this table with the current data."}
            table = _build_table_from_result(exec_out.get("result"))
            if not table or not (table.get("rows") or []):
                return {"ok": False, "error": "Re-execution did not produce a table."}
            payload = {"ok": True, "kind": "table", "table": table}
            full_table_key = _persist_full_table(store, table, code,
                                                 sql=sql_used or None,
                                                 first_key=next(iter(dfs), None))
            if full_table_key:
                payload["full_table_key"] = full_table_key
            log_with_sid(sid, "info", "REFRESH_ITEM_OK", kind="table",
                         chat_id=chat_id, rows=table.get("total_rows"))
            return _json_safe(payload)

        # kind == "chart" (default)
        from plot_utils import render_plot_safe
        plot_out = await loop.run_in_executor(
            _EXEC, lambda: render_plot_safe(code, dfs, sid))
        if (not isinstance(plot_out, dict) or plot_out.get("error")
                or plot_out.get("multi_axes")):
            emsg = (plot_out or {}).get("error", "unknown") if isinstance(plot_out, dict) else "unknown"
            log_with_sid(sid, "warning",
                         f"REFRESH_ITEM_CHART_EXEC_ERROR: {log_safe_text(str(emsg), 200)}",
                         chat_id=chat_id)
            return {"ok": False, "error": "Could not re-run this chart with the current data."}
        img = plot_out.get("plotly_html") if plot_out.get("is_plotly") else plot_out.get("image")
        if not img:
            return {"ok": False, "error": "Re-execution did not produce a chart."}
        payload = {"ok": True, "kind": "chart", "image_base64": img,
                   "is_plotly": bool(plot_out.get("is_plotly"))}
        # Fresh chart source data so a subsequent "Show data" shows the
        # refreshed values (same cache + endpoint the live stream uses).
        cd = plot_out.get("chart_data")
        if isinstance(cd, dict) and (cd.get("rows") or cd.get("tables")):
            payload["chart_data_key"] = _cache_full_table(cd)
        log_with_sid(sid, "info", "REFRESH_ITEM_OK", kind="chart",
                     chat_id=chat_id, plotly=bool(plot_out.get("is_plotly")))
        return _json_safe(payload)
    except Exception as e:
        # Re-execution + table normalization run under this guard, so the
        # message can carry a label the sandbox chose.
        log_with_sid(sid, "error",
                     f"REFRESH_ITEM_ERROR: {log_safe_text(type(e).__name__, 80)}: "
                     f"{log_safe_text(str(e), 200)}",
                     chat_id=chat_id)
        return {"ok": False, "error": "Refresh failed."}


@router.post("/{chat_id}/refresh_item")
async def refresh_item(request: Request, chat_id: str):
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    code = ((body or {}).get("code") or "").strip()
    kind = ((body or {}).get("kind") or "chart").strip().lower()
    if not code:
        return JSONResponse({"error": "No code to re-run for this item."}, status_code=400)
    if "###NEXT_PLOT###" in code:
        # Legacy joined multi-chart record — not a single executable block.
        return JSONResponse({"error": "This item cannot be refreshed."}, status_code=400)
    # Only code the chat already holds is re-run — decided before the role
    # gate, so unstored code never reaches it.
    if not await code_is_stored_async(chat_id, code):
        log_with_sid(email, "warning", "REFRESH_CODE_NOT_STORED", chat_id=chat_id)
        return _code_not_stored(403)
    drop, blocked, refused = await _refresh_role_gate(email, chat_id, code)
    if refused:
        # The gate failed on a chat holding a live table: refused, no table
        # named (the gate could not tell which).
        return {"ok": False, "code": "ROLE_DENIED", "blocked_tables": [],
                "error": _ROLE_DENIED_REFRESH_TEXT}
    if blocked:
        # Role denial follows the EXECUTION-failure contract (200 {ok:false})
        # — the frontend keeps the previous render and fail-freezes the button.
        log_with_sid(email, "info", "REFRESH_ROLE_DENIED",
                     chat_id=chat_id, tables=blocked)
        return {"ok": False, "code": "ROLE_DENIED", "blocked_tables": blocked,
                "error": _ROLE_DENIED_REFRESH_TEXT}
    result = await run_item_refresh(chat_id, code, kind, secrets.token_hex(8),
                                    drop_df_keys=drop)
    # A refreshed chart is not persisted; its PNG export needs a reference to
    # the chart the server just produced.
    if isinstance(result, dict) and result.get("ok") and result.get("image_base64"):
        ref = _chart_ref_for(email, chat_id, result.get("image_base64"))
        if ref:
            result = dict(result)
            result["chart_ref"] = ref
    return result


# --- Downloads (chart PNG + table Excel) -------------------------------------
# The B2C dashboard calls these three routes; they were never ported, so every
# "Download" button 404'd. Rendering + Excel build happen locally (Article II:
# raw values stay on the client — nothing here touches the brain).

def _safe_filename(name: str, default: str = "download") -> str:
    """Strip anything that could break a Content-Disposition header or path."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip()).strip("._")
    return cleaned[:120] or default


def _table_to_xlsx_bytes(columns: list, rows: list) -> bytes:
    """Build a .xlsx from the cached/posted table shape ({columns, rows})."""
    import pandas as pd

    cols = list(columns or [])
    data = rows or []
    df = pd.DataFrame(data, columns=cols) if cols else pd.DataFrame(data)
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Data")
    return buf.getvalue()


def _df_to_xlsx_bytes(df) -> bytes:
    """Build a .xlsx from a full (uncapped) DataFrame — every row."""
    import pandas as pd

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Data")
    return buf.getvalue()


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# What an operator can act on, per Excel-build failure type. The table itself
# is never described.
_XLSX_FAILURE_REASONS = {
    "IllegalCharacterError": "a cell holds a character xlsx cannot store",
}


def _xlsx_failure_reason(exc: BaseException) -> str:
    """A VALUE-FREE description of an Excel-build failure, for the log.

    The exception's own text is deliberately DROPPED here rather than escaped:
    openpyxl's `IllegalCharacterError` quotes the offending CELL VALUE in its
    message, and `/export_excel` takes its rows straight from the request
    body, so logging that text would let any authenticated caller write a
    chosen string — newline included — into the operator's log, and the
    download path would log a value the sandbox produced. No log line in this
    client carries a row value. An operator needs to know that an export
    failed on an illegal character, not which character in whose row; the
    exception TYPE plus this sentence says that much. Never raises
    (Article IV): an unknown type falls back to the generic sentence."""
    try:
        return _XLSX_FAILURE_REASONS.get(type(exc).__name__,
                                         "the table could not be written")
    except Exception:
        return "the table could not be written"


@router.post("/{chat_id}/export_plotly_png")
async def export_plotly_png(request: Request, chat_id: str):
    """Render one of THIS chat's charts to a high-res PNG via kaleido.

    The renderer is a Chromium inside the web container, so the chart markup
    never comes from the request: the body names a chart the server produced.
    Body: {chart_ref, filename?} for a chart the server just sent (a live turn
    or a refresh), or {conv_id, ai_index, image_index?, filename?} for a chart
    stored in the conversation. Any `html` field is ignored."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if "html" in body and not _EXPORT_LEGACY_LOGGED["logged"]:
        _EXPORT_LEGACY_LOGGED["logged"] = True
        log_with_sid(email, "warning", "EXPORT_PNG_LEGACY_BODY html field ignored",
                     chat_id=chat_id)
    filename = _safe_filename(body.get("filename") or "chart", "chart")
    ref = body.get("chart_ref")
    conv_id = body.get("conv_id")
    if ref:
        html = _chart_by_ref(email, chat_id, ref)
    elif conv_id is not None and "ai_index" in body:
        if not _may_use_conversation(email, chat_id, conv_id):
            return _access_denied()
        image_index = body.get("image_index", 0)
        html = await asyncio.get_running_loop().run_in_executor(
            _EXEC, lambda: _stored_chart_html(chat_id, conv_id, body.get("ai_index"), image_index))
    else:
        return JSONResponse({"error": "A chart reference is required."}, status_code=400)
    if not html:
        return JSONResponse(
            {"error": "Chart not found. Reload the conversation and try again."},
            status_code=404)
    try:
        from routes.report import _plotly_html_to_png
        png = await asyncio.get_running_loop().run_in_executor(
            _EXEC, _plotly_html_to_png, html, email)
    except Exception as e:
        # The type only: a plotly/kaleido message can quote the chart's data.
        log_with_sid(email, "error",
                     f"EXPORT_PLOTLY_PNG_FAILED: {log_safe_text(type(e).__name__, 80)}",
                     chat_id=chat_id)
        png = None
    if not png:
        return JSONResponse({"error": "Could not render chart image."}, status_code=502)
    return Response(
        content=png,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}.png"'},
    )


@router.post("/{chat_id}/download_excel/{key}")
async def download_excel(request: Request, chat_id: str, key: str):
    """Build a .xlsx with the FULL (uncapped) result for `key`. Body: {filename?}.

    Re-executes the stored code against the chat's local DataFrames to produce
    the complete result (no 50-row cap), then writes every row. Falls back to
    the stored preview rows only if re-execution errors or yields nothing (e.g.
    chart-data "Show data" records, which have no re-executable code).
    Article II: execution is local; Article IV: guarded, safe fallback."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    filename = _safe_filename((body or {}).get("filename") or "table", "table")
    store = local_store.ChatDataStore(chat_id)
    rec = _load_full_table_record(store, key)
    if not rec:
        return JSONResponse({"error": "Table not found or expired."}, status_code=404)
    denied = await _live_reexec_denial(email, chat_id, rec.get("code"),
                                       "DOWNLOAD_EXCEL_ROLE_DENIED")
    if denied is not None:
        return denied
    try:
        df = await _reexecute_full_df(chat_id, rec.get("code"), rec.get("result_key"))
        if df is not None and not df.empty:
            xlsx = _df_to_xlsx_bytes(df)
        else:
            xlsx = _table_to_xlsx_bytes(rec.get("columns"), rec.get("rows"))
    except Exception as e:
        # Type + a value-free reason only — see `_xlsx_failure_reason`. The
        # type name is escaped and capped for the same reason every other
        # untrusted field on a log line is.
        log_with_sid(email, "error",
                     f"DOWNLOAD_EXCEL_FAILED type={log_safe_text(type(e).__name__, 80)} "
                     f"reason={_xlsx_failure_reason(e)}", chat_id=chat_id)
        return JSONResponse({"error": "Could not build Excel file."}, status_code=502)
    return Response(
        content=xlsx,
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'},
    )


@router.post("/{chat_id}/export_excel")
async def export_excel(request: Request, chat_id: str):
    """Build a .xlsx from the preview table posted verbatim by the frontend.
    Body: {columns, rows, filename?}."""
    email, err = _require_chat(request, chat_id)
    if err:
        return err
    try:
        body = await request.json()
    except Exception:
        body = {}
    filename = _safe_filename((body or {}).get("filename") or "table", "table")
    columns = (body or {}).get("columns") or []
    rows = (body or {}).get("rows") or []
    if not columns and not rows:
        return JSONResponse({"error": "No table data provided."}, status_code=400)
    try:
        xlsx = _table_to_xlsx_bytes(columns, rows)
    except Exception as e:
        # `rows` came straight off the request body, so the exception text is
        # a caller-chosen value — dropped, never escaped-and-logged.
        log_with_sid(email, "error",
                     f"EXPORT_EXCEL_FAILED type={log_safe_text(type(e).__name__, 80)} "
                     f"reason={_xlsx_failure_reason(e)}", chat_id=chat_id)
        return JSONResponse({"error": "Could not build Excel file."}, status_code=502)
    return Response(
        content=xlsx,
        media_type=_XLSX_MIME,
        headers={"Content-Disposition": f'attachment; filename="{filename}.xlsx"'},
    )
