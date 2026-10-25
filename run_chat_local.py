"""Client-side chat orchestrator.

This is the enterprise split of the B2C `agent.run_chat`. The logic of the
flow is identical to the existing app (per the prompt: "Do not change the
logic of this flow — only split it across the client and brain containers."):

  1. Client builds schema_text from local dfs.
  2. Client → POST /v1/plan → Brain returns generated code.
  3. Client runs the code in its analysis sandbox — safe_execute /
     render_plot_safe dispatch it to the separate pdc-executor container
     (executor_client); the web process runs no generated code. A live
     table's SELECT, when the plan wrote one, runs in the web process
     before that (_ensure_live).
  4. On error: Client → POST /v1/retry with error text → Brain returns
     corrected code → Client retries execution (up to 3 attempts, matching
     the B2C retry policy).
  5. Client → POST /v1/describe to get the natural-language intro,
     OR POST /v1/summarize when the result is a scalar or a flat dict of
     scalars (no chart/table) — or, when the brain wrote the answer as a
     TEMPLATE (`narrative`, `captions`), neither: the client fills the
     template locally from the VALUES the code set (DIAGNOSTICS_SPEC A5).

Raw data rows stay on the client. The brain sees: the question, the schema
text (column names, dtypes, descriptions, and the values of text columns
with at most 20 distinct values — none for a SCHEMA_VALUE_DENY_COLUMNS
column), the dataset profile, generated code, the (scrubbed) execution error
text and validated FINDINGS (bools, input column names, fixed codes). The
summarize call carries NO preview any more (`preview=None`, decision F1);
`_safe_preview` now only feeds the local `value_display` line and the
implicit `result` slot. Rendered texts (`narrative.render`) are display only
and never reach a brain call or `content` (R0).
"""
from __future__ import annotations

import hashlib
import re
import time as _time
from dataclasses import dataclass, field
from typing import Any, Optional

import pandas as pd

import brain_client
import executor_client
import html_sanitize
import narrative
import result_backstop
from code_exec import safe_execute
from plot_utils import render_plot_safe, _is_noninteractive_standard_chart
from exec_transport import log_safe_text
from logger_utils import log_with_sid
from schema_builder import schema_text as build_schema_text
from schema_builder import _detect_language as _detect_answer_language
from settings import brain_capabilities, settings


# Caps for capturing a pandas Styler's rendered HTML. A Styler renders the
# WHOLE frame (not just the 50-row preview), so guard against pathologically
# large styled payloads bloating the SSE/persisted history — fall back to the
# plain {columns,rows} table when exceeded. Generic, dataset-agnostic limits.
_STYLED_MAX_ROWS = 200
_STYLED_MAX_COLS = 40
_STYLED_MAX_CHARS = 500_000

# What the user is told when the analysis sandbox itself failed. Never the
# "try rephrasing" sentence: that blames the question for an outage.
_INFRA_BUSY_TEXT = ("The analysis service is busy right now. "
                    "Please try again in a moment.")
_INFRA_DOWN_TEXT = ("The analysis service is not available right now. "
                    "Please try again in a moment or contact your "
                    "administrator.")


def _infrastructure_answer(sid, error_text) -> Optional[str]:
    """The sentence for an infrastructure failure, or None to keep retrying.

    The retry loops below were written when generated code ran in this
    process, where every `error` string was the code's fault. The same channel
    now also carries the sandbox hop's own conditions, and asking the brain to
    REWRITE the code three times spends three brain calls and three
    round-trips on something no code change can fix — ending in "try
    rephrasing" for an outage. `executor_client.is_infrastructure_error` owns
    which failures those are (a timeout, an out-of-memory run and an oversized
    result still retry: cheaper code is a real fix).
    """
    if not executor_client.is_infrastructure_error(error_text):
        return None
    log_with_sid(sid, "error",
                 f"EXEC_INFRA_ERROR {log_safe_text(str(error_text), 200)}")
    if str(error_text).startswith("ExecutorBusy"):
        return _INFRA_BUSY_TEXT
    return _INFRA_DOWN_TEXT


# ---------------------------------------------------------------------------
# Live database tables: the pre-fetch that runs in THIS process right before
# the sandbox. A live table's frame is an empty typed placeholder until the
# code about to run references it; then the planner's own SELECT (or, without
# one / behind an admin row filter, the connector's capped default read) is
# executed by db_connector and the result takes the placeholder's key. The
# sandbox only ever sees a frame (Article XIV: SQL never runs there).
# ---------------------------------------------------------------------------
_LIVE_ROLE_DENIED_TEXT = ("You no longer have access to {table}; ask your "
                          "administrator.")
_LIVE_FAILED_PREFIX = "Live query for table '{table}' failed: {sentence}"
# The same, when the pre-fetch failed before any table was known.
_LIVE_FAILED_NO_TABLE = "Live query failed: {sentence}"
# The answer of a turn whose attempts all ended on the live fetch: the table
# and the value-free class sentence (never a driver's text), instead of the
# generic line that blames the question.
_LIVE_EXHAUSTED_TEXT = ("I couldn't run this analysis: the live query for "
                        "'{table}' failed ({sentence})")
# The refusal a retry's `sql_error` carries when the planner sent a SELECT for
# a table the administrator filtered: the SELECT was ignored (the filtered
# default read ran instead), so it is reported as not run.
_FILTERED_SQL_MESSAGE = ("The table is filtered by the administrator; "
                         "no SELECT is accepted for it.")


def _new_live_state(plan_out: dict | None = None) -> dict:
    """The per-turn live state shared by every executor call and retry:
    `sql_map` (df key -> the planner's SELECT), `fetched` (keys whose frame
    is in place), `sql_used` (what actually ran: the SQL text of a SELECT
    that reached the database — also when the database failed it — or None
    for a default read, a SELECT ignored on a filtered table and a SELECT
    the guard refused), `live_rows`, `truncated`, `failed_sql` (key -> the
    SELECT that failed, so it is never re-run or replaced by a default read
    until the planner sends a new one), `sql_error` (the value-free shape of
    the LAST failure), `ignored_sql` (key -> the guard-shaped refusal of a
    planner SELECT ignored on a filtered table), `failed_key` /
    `failed_text`, `role_denied`."""
    state = {"sql_map": {}, "fetched": set(), "sql_used": {}, "live_rows": {},
             "truncated": set(), "failed_sql": {}, "failed_sql_error": {},
             "failed_sql_text": {}, "sql_error": None, "ignored_sql": {},
             "failed_key": None, "failed_text": None, "role_denied": None}
    _merge_retry_sql(state, plan_out)
    return state


def _merge_retry_sql(state: dict, response: dict | None) -> None:
    """Take a plan / retry response's `sql` map into the state: a NEW SELECT
    for a key drops it from `fetched` (re-fetched before the next executor
    call) and from `failed_sql`. Non-string entries are ignored."""
    new = (response or {}).get("sql") if isinstance(response, dict) else None
    if not isinstance(new, dict):
        return
    for key, sql in new.items():
        if isinstance(key, str) and isinstance(sql, str) and sql.strip():
            state["sql_map"][key] = sql
            state["fetched"].discard(key)
            state["failed_sql"].pop(key, None)


def _retry_sql(state: dict) -> dict | None:
    """The retry payload's `sql`: the SELECTs that RAN this turn — the text
    for a SELECT that reached the database (also when it failed there, so
    the planner can fix it), None for a default read, an ignored SELECT
    (filtered table) or a guard refusal; keys never fetched are absent.
    None when nothing was fetched."""
    return dict(state.get("sql_used") or {}) or None


def _retry_sql_error(state: dict) -> dict | None:
    """The retry payload's `sql_error`: the last fetch failure, else the
    refusal of the most recent SELECT ignored on a filtered table, else None
    (a Python error on a default read is not a query error).

    The refusal is CONSUMED by the retry that reports it: a later retry
    caused by a Python error must not tell the planner to fix SQL again. A
    planner that resends the ignored SELECT records it anew in
    `_ensure_live`, so it rides exactly the next retry."""
    if state.get("sql_error"):
        return state["sql_error"]
    ignored = state.get("ignored_sql") or {}
    if ignored:
        key = list(ignored)[-1]
        return ignored.pop(key)
    return None


def _live_specs(schema_docs, dfs) -> dict:
    """{df key: {dialect, row_cap, filtered, db_ref}} for the chat's LIVE
    tables that are loaded (present in `dfs`)."""
    out = {}
    for key, doc in (schema_docs or {}).items():
        if not (isinstance(doc, dict) and doc.get("source") == "database"
                and doc.get("live")):
            continue
        if dfs is not None and key not in dfs:
            continue
        out[key] = {"dialect": doc.get("dialect") or "",
                    "row_cap": doc.get("row_cap"),
                    "filtered": bool(doc.get("filtered")),
                    "db_ref": doc.get("db_ref") or {}}
    return out


def _live_tables_for_brain(specs: dict) -> list | None:
    """The `live_tables` rows of the plan / retry payload, None when the
    chat has no live table (the payload then carries no such key)."""
    rows = [{"name": key, "dialect": spec["dialect"], "row_cap": spec["row_cap"],
             "filtered": spec["filtered"]} for key, spec in specs.items()]
    return rows or None


def _live_failure_out(state: dict) -> dict:
    """The executor-shaped error dict for an attempt whose live fetch failed
    (the sandbox is NOT run): `error` is the value-free sentence the retry
    prompt gets; `live_failed` tells the loops it was not an execution
    error (no infrastructure verdict); `live_role_denied` ends the turn."""
    if state.get("role_denied"):
        return {"error": _LIVE_ROLE_DENIED_TEXT.format(table=state["role_denied"]),
                "result": None, "preview": None, "image_base64": None,
                "live_failed": True, "live_role_denied": True}
    key = state.get("failed_key")
    sentence = state.get("failed_text")
    error = (_LIVE_FAILED_PREFIX.format(table=key, sentence=sentence) if key
             else _LIVE_FAILED_NO_TABLE.format(sentence=sentence))
    return {"error": error,
            "result": None, "preview": None, "image_base64": None,
            "live_failed": True}


def _live_exhausted_text(state: dict) -> str | None:
    """`_LIVE_EXHAUSTED_TEXT` for the turn's last live-fetch failure, or
    None when the state names no failed table (the caller keeps its own
    text)."""
    key, sentence = state.get("failed_key"), state.get("failed_text")
    if not (key and sentence):
        return None
    return _LIVE_EXHAUSTED_TEXT.format(table=key, sentence=sentence)


def _live_role_denied_result(state: dict, code, usage) -> dict:
    return {"text": _LIVE_ROLE_DENIED_TEXT.format(table=state.get("role_denied")),
            "image_base64": None, "table": None, "code": code, "usage": usage}


def _allowed_table_pairs(row: dict, cfg: dict) -> set:
    """The (schema, table) pairs the guard accepts for one registration: the
    registered schema, the unqualified name and the connection's database
    name (MySQL / ClickHouse registrations carry no schema and the planner
    may qualify with the database) — all lowercased."""
    table = str(row.get("table_name") or "").lower()
    pairs = {("", table), (str(row.get("schema") or "").lower(), table)}
    database = cfg.get("database")
    if isinstance(database, str) and database.strip():
        pairs.add((database.strip().lower(), table))
    return pairs


_DFS_GENERIC_RE = re.compile(r"\bdfs\b")
# The executor binds `df` to the FIRST frame of the dict it receives
# (`code_exec._execute_in_process`); `\bdf\b` matches neither `dfs` nor `df2`.
_DF_ALIAS_RE = re.compile(r"\bdf\b")


def _referenced_live_keys(code: str, live_keys, all_keys, *,
                          first_key=None) -> tuple[list, list]:
    """(named, generic) live keys the code is about to use.

    NAMED: the key's name appears anywhere in the text as a plain quoted
    substring (`'key'` or `"key"`) — `dfs['key']`, `dfs.get("key")` and
    `name = "key"; dfs[name]` all count. GENERIC: after every named-key form
    (`dfs['k']`, `dfs["k"]`, `dfs.get('k')`, `dfs.get("k")`) and every
    quoted df key of the chat is removed, the text still uses `dfs`
    (iteration, `.values()` / `.items()` / `.keys()`, `in dfs`, a variable
    index) — then every live key not named is generic, because an empty
    placeholder must never reach the executor silently. Code that names only
    other keys and uses `dfs` in no generic form references nothing. This
    only decides WHICH keys are fetched; the fetch method (the planner's
    SELECT when one exists, else the default read) is the same for both.

    THE `df` ALIAS: `first_key` is the key of the first frame of the dict
    the executor will receive (explicit — None disables the rule). When it
    is a live key not already named and the code uses `df` as a word, it is
    NAMED too: the executor binds `df` to that frame, so the placeholder
    must not be what the code computes on. A `df` in a comment over-fetches
    (the cost is a fetch, never a miss)."""
    text = code or ""
    named = [k for k in live_keys if f"'{k}'" in text or f'"{k}"' in text]
    if (first_key is not None and first_key in live_keys
            and first_key not in named and _DF_ALIAS_RE.search(text)):
        named.append(first_key)
    stripped = text
    for key in sorted(set(all_keys or ()) | set(live_keys), key=len, reverse=True):
        for quoted in (f"'{key}'", f'"{key}"'):
            stripped = stripped.replace(f"dfs[{quoted}]", "")
            stripped = stripped.replace(f"dfs.get({quoted})", "")
            stripped = stripped.replace(quoted, "")
    if not _DFS_GENERIC_RE.search(stripped):
        return named, []
    return named, [k for k in live_keys if k not in named]


def _ensure_live(sid, dfs: dict, schema_docs, code: str, state: dict,
                 user_email, *, check_role: bool = True) -> bool:
    """Fetch every LIVE table `code` references that is not in place yet.
    IDEMPOTENT: called immediately before EVERY executor call, so a key
    first referenced by a retry's code is fetched too. True when the code
    may run; False when a fetch failed (`state["sql_error"]`,
    `failed_key`, `failed_text` describe it) or the requester's role does
    not cover a referenced table (`state["role_denied"]`).

    What counts as referenced is `_referenced_live_keys` (a key NAMED in
    the code in either quote style, or every unfetched live key when the
    code walks `dfs` GENERICALLY). That rule decides WHETHER a key is
    fetched, never how: every referenced key takes the planner's SELECT
    when `sql_map` carries one for it, else the default read.

    Per key: the role gate (`roles_store.allowed_table_ids_for`, connectors
    exempt; the stream already dropped uncovered keys — this is defence in
    depth against a planner-invented key or a revocation mid-turn; the
    refresh paths, whose route gate already ran, pass `check_role=False`),
    then the connection decrypted into a local, then EITHER the planner's
    SELECT through `run_live_select` bound to exactly this table
    (`allowed_tables`) OR — no SELECT, or an admin row filter on the
    registration — `default_live_fetch`. A SELECT that failed is never
    re-run and never replaced by a default read until the planner sends a
    new one. Logs carry the key, counts and the error CLASS — never the SQL
    text, never a driver message."""
    current_key = None
    try:
        # A failure below names only the table THIS call was working on, so a
        # crash before any table is known never reports an earlier one.
        state["failed_key"] = None
        specs = _live_specs(schema_docs, dfs)
        if not specs:
            return True
        named, generic = _referenced_live_keys(code, list(specs), list(dfs or {}),
                                               first_key=next(iter(dfs or {}), None))
        referenced = [k for k in named + generic if k not in state["fetched"]]
        if not referenced:
            return True
        state["sql_error"] = None
        state["failed_key"] = None
        state["failed_text"] = None
        from db_sources import DataSourceStore, decrypt_password
        import db_connector
        import roles_store
        store = DataSourceStore()
        rows = {}
        for key in referenced:
            tid = (specs[key].get("db_ref") or {}).get("table_id")
            rows[key] = store.get_table(tid) if tid else None
        try:
            allowed = roles_store.allowed_table_ids_for(user_email)                 if (check_role and user_email) else set()
        except Exception as e:                               # noqa: BLE001
            log_with_sid(sid, "warning",
                         f"LIVE_ROLE_PROBE_FAILED error={log_safe_text(type(e).__name__, 80)}")
            allowed = set()
        for key in (referenced if check_role else []):
            row = rows.get(key)
            if row is not None and row.get("is_connector"):
                continue
            tid = (row or {}).get("id") or (specs[key].get("db_ref") or {}).get("table_id")
            if tid not in allowed:
                state["role_denied"] = key
                log_with_sid(sid, "warning",
                             f"LIVE_ROLE_DENIED table={log_safe_text(str(key), 120)}")
                return False
        for key in referenced:
            current_key = key
            spec = specs[key]
            row = rows.get(key)
            sql = state["sql_map"].get(key)
            if spec["filtered"] and sql:
                log_with_sid(sid, "info",
                             f"LIVE_SQL_IGNORED_FILTERED table={log_safe_text(str(key), 120)}")
                state["ignored_sql"][key] = brain_client.live_sql_error(
                    key, spec.get("dialect") or "", "guard", guard=True,
                    message=_FILTERED_SQL_MESSAGE)
                sql = None
            if spec["filtered"] and key in state["live_rows"]:
                # A filtered table is only ever read by the default read, and
                # this turn already holds that frame (a resent SELECT dropped
                # the key from `fetched`): the same read again would return
                # the same rows, so nothing is fetched twice.
                state["fetched"].add(key)
                continue
            if sql and state["failed_sql"].get(key) == sql:
                # The same SELECT failed on an earlier attempt of this turn
                # and the planner sent no new one: a failed attempt, never a
                # default read that would silently change the answer.
                state["sql_error"] = state["failed_sql_error"].get(key)
                state["failed_key"] = key
                state["failed_text"] = state["failed_sql_text"].get(key)
                return False
            cid = (row or {}).get("connection_id") or \
                (spec.get("db_ref") or {}).get("connection_id")
            conn = store.get_connection(cid, with_secret=True) if cid else None
            if row is None or conn is None:
                res = {"ok": False, "error": db_connector.error_class_text("other"),
                       "error_class": "other"}
            else:
                password = decrypt_password(conn.get("password_enc")) or ""
                cap = spec.get("row_cap")
                if not (isinstance(cap, int) and not isinstance(cap, bool) and cap > 0):
                    cap = int(settings.LIVE_RESULT_ROW_CAP)
                if sql:
                    res = db_connector.run_live_select(
                        conn, password, sql, dialect=spec.get("dialect") or None,
                        row_cap=cap, allowed_tables=_allowed_table_pairs(row, conn),
                        sid=sid)
                else:
                    res = db_connector.default_live_fetch(conn, password, row,
                                                          cap=cap, sid=sid)
                password = None
            # What RAN: the text only for a SELECT that reached the database
            # (success or a database failure); a guard refusal never did.
            state["sql_used"][key] = None if res.get("guard") else (sql or None)
            if res.get("ok") and res.get("df") is not None:
                df = res["df"]
                try:
                    df.attrs["pdc_live"] = True
                except Exception as e:                       # noqa: BLE001
                    log_with_sid(sid, "info",
                                 f"LIVE_ATTRS_SKIPPED table={log_safe_text(str(key), 120)} "
                                 f"error={log_safe_text(type(e).__name__, 80)}")
                dfs[key] = df
                state["fetched"].add(key)
                state["live_rows"][key] = int(res.get("rows") or len(df))
                if res.get("truncated"):
                    state["truncated"].add(key)
                else:
                    state["truncated"].discard(key)
                log_with_sid(sid, "info",
                             f"LIVE_PREFETCH table={log_safe_text(str(key), 120)} "
                             f"rows={int(res.get('rows') or len(df))} "
                             f"truncated={bool(res.get('truncated'))} "
                             f"elapsed_ms={int(res.get('elapsed_ms') or 0)}")
                continue
            cls = res.get("error_class") or "other"
            guard = bool(res.get("guard"))
            sentence = str(res.get("error") or db_connector.error_class_text(cls))
            state["sql_error"] = brain_client.live_sql_error(
                key, spec.get("dialect") or "", cls, guard=guard,
                message=sentence if guard else None)
            state["failed_key"] = key
            state["failed_text"] = sentence
            if sql:
                state["failed_sql"][key] = sql
                state["failed_sql_error"][key] = state["sql_error"]
                state["failed_sql_text"][key] = sentence
            log_with_sid(sid, "warning",
                         f"LIVE_PREFETCH_FAILED table={log_safe_text(str(key), 120)} "
                         f"class={log_safe_text(str(cls), 40)}")
            return False
        return True
    except Exception as e:                                   # noqa: BLE001
        # Raised about registry rows and frames, so the text could quote a
        # label: the exception TYPE and a fixed reason only.
        key = current_key
        log_with_sid(sid, "error",
                     f"LIVE_PREFETCH_CRASHED table={log_safe_text(str(key or '-'), 120)} "
                     f"error={log_safe_text(type(e).__name__, 80)} "
                     f"reason=prefetch_helper_failed")
        state["sql_error"] = None
        # None when no table was reached yet: the failure texts are then
        # key-less (`_live_failure_out`) or the caller's generic one.
        state["failed_key"] = key or None
        state["failed_text"] = "The database could not run the query."
        return False


def _finish_live_result(out: dict, state: dict, question: str, sid) -> dict:
    """Attach the live fields (`sql`, `live_truncated`, `live_rows`) to a
    turn's result dict and append the localized truncation note to its
    text when a capped fetch fed the answer.

    A narrative answer keeps its brain-safe `text` (marker + template, or a
    caption template) untouched: the note goes on the RENDERED text the user
    reads — `narrative_rendered`, else `caption_rendered` (A5.1)."""
    try:
        out = dict(out or {})
        out["sql"] = dict(state.get("sql_used") or {})
        out["live_truncated"] = bool(state.get("truncated"))
        out["live_rows"] = dict(state.get("live_rows") or {})
        if state.get("truncated"):
            for key in ("narrative_rendered", "caption_rendered"):
                if out.get(key):
                    out[key] = _append_live_notes(out[key], state, question)
                    break
            else:
                if out.get("text") and not out.get("caption_template"):
                    out["text"] = _append_live_notes(out["text"], state, question)
        return out
    except Exception as e:                                   # noqa: BLE001
        log_with_sid(sid, "warning",
                     f"LIVE_RESULT_FIELDS_FAILED error={log_safe_text(type(e).__name__, 80)}")
        return out


def _append_live_notes(text: str, state: dict, question: str) -> str:
    lang = _detect_answer_language(question)
    notes = [result_backstop.live_truncated_sentence(
                 lang, key, state["live_rows"].get(key))
             for key in sorted(state.get("truncated") or ())]
    note = " ".join(n for n in notes if n)
    if not note:
        return text
    return f"{text}\n\n{note}" if text else note


def _styler_to_html(result_obj) -> Optional[str]:
    """Render a pandas Styler to self-contained HTML (gradient + .format kept).

    The model may return ``RESULT = df.style.background_gradient(...).format(...)``
    to convey conditional formatting. ``Styler.to_html`` embeds the gradient as
    uuid-scoped ``<style>`` and applies the model's ``.format()`` so numbers are
    already rounded/grouped. Returns the HTML string, or None for a non-Styler
    result / oversized table / any failure — callers then fall back to the plain
    table. The pages insert this markup into the page itself, so it goes
    through ``html_sanitize.clean_styled_html`` (allowlisted table markup, and
    only ``#T_``-scoped ``<style>`` rules with allowlisted properties); markup
    that does not survive cleaning also answers None.
    """
    try:
        # A Styler exposes the underlying frame as ``.data`` plus ``.to_html``;
        # a bare DataFrame has no ``.data`` attribute, so this picks out Stylers.
        if not (hasattr(result_obj, "data")
                and isinstance(getattr(result_obj, "data", None), pd.DataFrame)
                and hasattr(result_obj, "to_html")):
            return None
        df = result_obj.data
        if df.shape[0] > _STYLED_MAX_ROWS or df.shape[1] > _STYLED_MAX_COLS:
            return None
        html = result_obj.to_html()
        if not isinstance(html, str) or not html.strip() or len(html) > _STYLED_MAX_CHARS:
            return None
        return html_sanitize.clean_styled_html(html)
    except Exception:
        return None


def _normalize_df_for_table(df: pd.DataFrame) -> pd.DataFrame:
    """Make a DataFrame serialize losslessly into the {columns, rows} JSON the
    frontend renders (QA 2.2).

    Two defects this fixes for pivot-shaped frames:
      - the row index (the pivot's grouping key, e.g. city names) was dropped
        by to_dict(orient="records") — reset a MEANINGFUL index into visible
        columns (MultiIndex, any named level, or a non-integer index such as
        Datetime/object). A plain RangeIndex or an unnamed integer index (a
        filtered frame) passes through untouched;
      - MultiIndex/tuple column labels became JSON arrays in `columns` but
        "_"-joined strings as row-dict keys (local_store._json_safe), so the
        frontend's by-name lookup missed every cell — flatten tuple labels to
        strings here so both halves agree.
    Never raises: any failure logs and returns the frame unchanged (Art. IV).
    """
    try:
        out = df
        idx = out.index
        meaningful = (isinstance(idx, pd.MultiIndex)
                      or any(n is not None for n in (idx.names or []))
                      or not (isinstance(idx, pd.RangeIndex)
                              or pd.api.types.is_integer_dtype(idx)))
        if meaningful:
            out = out.reset_index()
        if isinstance(out.columns, pd.MultiIndex) or any(isinstance(c, tuple) for c in out.columns):
            if out is df:
                out = out.copy(deep=False)
            out.columns = [
                " / ".join(str(p) for p in c if str(p).strip()) if isinstance(c, tuple)
                else str(c)
                for c in out.columns]
        # uniqueness after flattening (pandas "name.1" convention)
        if len(set(map(str, out.columns))) != len(out.columns):
            if out is df:
                out = out.copy(deep=False)
            seen: dict = {}
            cols = []
            for c in map(str, out.columns):
                n = seen.get(c, 0)
                seen[c] = n + 1
                cols.append(c if n == 0 else f"{c}.{n}")
            out.columns = cols
        return out
    except Exception as e:
        # pandas quotes the LABEL it choked on and does NOT `repr` it: a result
        # frame whose index name equals a column name makes `reset_index()`
        # raise "cannot insert <name>, already exists", and that name — like
        # every label here — is the executed code's choice and survives the
        # parquet round trip. So this `{e}` is an untrusted string wearing a
        # library's error text, and the durable log is newline-delimited.
        log_with_sid("table", "warning",
                     f"TABLE_NORMALIZE_FAILED: {log_safe_text(str(e), 200)}")
        return df


def _build_table_from_result(result_obj) -> Optional[dict]:
    """Tiny port of the B2C `_build_table_from_result` for DataFrame / Series.

    Returns {columns, rows, total_rows[, styled_html]} or None. When the result
    is a pandas Styler, also carries `styled_html` so the frontend can render
    the conditional formatting (B2C parity) while keeping the raw rows/columns
    for "Show full table" and "Download Excel".
    """
    if result_obj is None:
        return None
    try:
        if hasattr(result_obj, "data") and isinstance(result_obj.data, pd.DataFrame):
            df = result_obj.data
        elif isinstance(result_obj, pd.DataFrame):
            df = result_obj
        elif isinstance(result_obj, pd.Series):
            df = result_obj.reset_index()
        else:
            return None
        df = _normalize_df_for_table(df)
        rows = df.head(50).to_dict(orient="records")
        table = {
            "columns": list(df.columns),
            "rows": rows,
            "total_rows": int(len(df)),
        }
        styled = _styler_to_html(result_obj)
        if styled:
            table["styled_html"] = styled
        return table
    except Exception:
        return None


def _build_tables_from_result(result_obj) -> Optional[list]:
    """Handle a DICT of DataFrames/Series/Stylers as the executed RESULT
    (e.g. RESULT = {'Sales first 10': df1, 'Stock first 10': df2} — a shape
    models produce for "show each table" questions even though the planner
    prompt asks for a single DataFrame; before this, such answers rendered as
    prose garbage because no table event was built).

    Maps each DataFrame-like value through `_build_table_from_result` and
    carries the dict key as the table's `title`. Non-DF values are skipped.
    Returns a non-empty list of {title, columns, rows, total_rows
    [, styled_html]} dicts, or None."""
    if not isinstance(result_obj, dict) or not result_obj:
        return None
    tables: list = []
    for key, val in result_obj.items():
        try:
            t = _build_table_from_result(val)
            if t and int(t.get("total_rows", 0)) > 0 and (t.get("rows") or []):
                t["title"] = str(key)
                tables.append(t)
        except Exception as e:
            # `key` is a RESULT-dict key the executed code chose, and
            # `log_with_sid` interpolates the sid and every context value RAW
            # — an untrusted string must never be used as an identifier field
            # unescaped, or it forges a record from the sid position just as
            # readily as from the message. Both halves are escaped and capped.
            log_with_sid(log_safe_text(str(key), 120), "warning",
                         f"MULTI_TABLE_BUILD_FAILED: {log_safe_text(str(e), 200)}")
    return tables or None


def _tables_from_python_result(result_obj, table):
    """Shared post-processing for the PYTHON branches: when the single-table
    builder found nothing and the result is a dict of DF-likes, build the
    tables list; a single-entry dict unwraps back into the plain single-table
    path (no UI change needed for that common shape). Returns (table, tables)."""
    tables = None
    if table is None:
        tables = _build_tables_from_result(result_obj)
        if tables and len(tables) == 1:
            table = dict(tables[0])
            table.pop("title", None)
            tables = None
    return table, tables


def _other_registered_tables(schema_docs, dfs, user_email) -> list:
    """Registry tables NOT loaded in this chat, as metadata rows for the
    schema text's OTHER REGISTERED TABLES section (QA report §4 / MISSING_DATA).

    Lets the planner NAME the table that would answer a question instead of
    fabricating a mapping from domain context. Gated on the chat actually
    using database tables (pure file-upload chats get no section — their
    schema text stays byte-identical); role-filtered so a user is never shown
    a table their role cannot add (fail-closed like the picker). Metadata
    only — display names, column NAMES, declared relations — never values
    (Article II). Never raises: any failure returns [] (Article IV)."""
    try:
        docs = schema_docs or {}
        if not any(isinstance(v, dict) and v.get("source") == "database"
                   for v in docs.values()):
            return []
        from db_sources import DataSourceStore, table_mode
        from roles_store import allowed_table_ids_for
        rows = DataSourceStore().list_tables(include_connector=False)
        if not rows:
            return []
        allowed = allowed_table_ids_for(user_email) if user_email else set()
        loaded_names = set(dfs or {}) | set(docs)
        id_to_name = {t.get("id"): (t.get("display_name") or "") for t in rows}
        out = []
        for t in rows:
            name = t.get("display_name") or ""
            if not name or name in loaded_names:
                continue
            if t.get("id") not in allowed:
                continue
            relations = []
            for rel in (t.get("relations") or []):
                if not isinstance(rel, dict):
                    continue
                parent_name = id_to_name.get(rel.get("related_table_id"))
                if not parent_name:
                    # legacy relation shape stores a display NAME, not an id
                    legacy = rel.get("related_table")
                    if isinstance(legacy, str) and legacy in loaded_names:
                        parent_name = legacy
                if parent_name and parent_name in loaded_names:
                    relations.append({
                        "related_display_name": parent_name,
                        "join_keys": [[str(p[0]), str(p[1])]
                                      for p in (rel.get("join_keys") or [])
                                      if isinstance(p, (list, tuple)) and len(p) == 2],
                    })
            row = {
                "id": t.get("id"),
                "display_name": name,
                "updated_at": t.get("updated_at"),
                "columns": [str(c.get("name"))
                            for c in (t.get("columns") or []) if isinstance(c, dict)],
                "relations": relations,
            }
            if table_mode(t) == "live":
                row["live"] = True
            out.append(row)
        out.sort(key=lambda r: r["display_name"])
        return out[:20]
    except Exception as e:
        # The failure is raised ABOUT registry metadata (table display names,
        # introspected column names), so the message can quote one.
        log_with_sid(str(user_email or "?"), "warning",
                     f"OTHER_TABLES_FAILED: {log_safe_text(str(e), 200)}")
        return []


def _safe_preview(preview) -> Any:
    """The hard data-boundary guard (Article II): only scalars — or dicts whose
    VALUES are all plain scalars/None — may be forwarded to the brain. NEVER
    send DataFrame rows.

    The value check must be a scalar allow-list, not a list/dict deny-list: a
    dict of DataFrames/Stylers/Timestamps (e.g. RESULT = {"sales": df1.head(10),
    "stock": df2.head(10)}) previously slipped through and crashed the brain
    call's JSON encoding mid-turn ("Object of type Styler is not JSON
    serializable"). numpy scalars are coerced to native first so a
    {"mean": np.float64(...)} summary still passes."""
    if preview is None:
        return None
    if isinstance(preview, (str, int, float, bool)):
        return preview
    if isinstance(preview, dict):
        safe: dict = {}
        for k, v in preview.items():
            if not isinstance(k, (str, int, float, bool)):
                return None
            item = getattr(v, "item", None)
            if callable(item) and not isinstance(v, (str, int, float, bool)):
                try:
                    v = v.item()
                except Exception:
                    return None
            if v is not None and not isinstance(v, (str, int, float, bool)):
                return None
            safe[k] = v
        return safe
    return None


def run_chat(
    sid: str,
    dfs: dict[str, pd.DataFrame],
    schema_docs: dict,
    question: str,
    history_rows: list,
    user_email: str,
    common_fields: list | None = None,
    dataset_profile: dict | None = None,
) -> dict:
    """Run one chat turn end-to-end. Returns:
        {
          "text": str,
          "image_base64": str | None,
          "table": dict | None,
          "code": str | None,
          "usage": dict,
          "sql": dict,            # live tables: df key -> SELECT run, or None
          "live_truncated": bool,
          "live_rows": dict,
        }
    """
    state = _new_live_state()
    out = _run_chat_body(sid=sid, dfs=dfs, schema_docs=schema_docs,
                         question=question, history_rows=history_rows,
                         user_email=user_email, common_fields=common_fields,
                         dataset_profile=dataset_profile, state=state)
    return _finish_live_result(out, state, question, sid)


def _run_chat_body(*, sid, dfs, schema_docs, question, history_rows, user_email,
                   common_fields, dataset_profile, state) -> dict:
    df_names = list(dfs.keys())
    df_columns = {name: list(df.columns) for name, df in dfs.items()}
    schema_str = build_schema_text(
        schema_docs or {}, dfs, common_fields,
        other_tables=_other_registered_tables(schema_docs, dfs, user_email))
    live_tables = _live_tables_for_brain(_live_specs(schema_docs, dfs))

    def _exec(code_, kind_, **kw):
        # The live pre-fetch runs before EVERY executor call (idempotent).
        if not _ensure_live(sid, dfs, schema_docs, code_, state, user_email):
            return _live_failure_out(state)
        fn = render_plot_safe if kind_ == "PLOT_CODE" else safe_execute
        return fn(code_, dfs, sid, **kw)

    # 1) Plan
    plan_out = brain_client.plan(
        sid=sid,
        question=question,
        schema_text=schema_str,
        df_names=df_names,
        history_rows=history_rows,
        common_fields=common_fields,
        user_email=user_email,
        dataset_profile=dataset_profile,
        live_tables=live_tables,
    )
    _merge_retry_sql(state, plan_out)
    kind = plan_out.get("kind")
    code = plan_out.get("code") or ""
    usage = plan_out.get("usage") or {}
    context_decision = plan_out.get("context_decision") or {}
    # Both fields are lifted VERBATIM out of the plan response and neither is
    # validated before this line: `kind` is only compared against a known set
    # afterwards, and `model_used` is free text. Escaped and capped like every
    # other field whose origin is not local.
    log_with_sid(sid, "info",
                 f"PLAN kind={log_safe_text(str(kind), 40)} "
                 f"model={log_safe_text(str(plan_out.get('model_used')), 80)}")

    # Clarification / text answer — return as text
    if kind in ("CLARIFICATION", "ANSWER", "MISSING_DATA"):
        return {"text": code, "image_base64": None, "table": None, "code": None, "usage": usage}

    # Greeting — call brain for natural reply
    if kind == "NO_CODE":
        if context_decision.get("is_greeting"):
            g = brain_client.greeting(sid, question, df_names, user_email=user_email)
            return {"text": g.get("text", ""), "image_base64": None, "table": None, "code": None, "usage": _sum_usage(usage, g.get("usage", {}))}
        # Else: try to summarize from scalar preview (we have no result yet, so just return generic)
        return {"text": "I couldn't generate analysis code for this question. Try rephrasing.", "image_base64": None, "table": None, "code": None, "usage": usage}

    # 2) Execute locally with retry (up to 3 attempts matching B2C policy)
    exec_out = _exec(code, kind)

    retry_count = 0
    max_retries = 3  # up to 3 retry attempts; escalate to pro/search from the 2nd
    while exec_out.get("error") and retry_count < max_retries:
        if exec_out.get("live_role_denied"):
            return _live_role_denied_result(state, code, usage)
        error_msg = exec_out.get("error", "Unknown")
        # The sandbox itself failed: no rewrite of this code can help, so
        # answer once instead of spending three brain calls on an outage.
        # (A failed live fetch is a query error, never an outage verdict.)
        infra_text = None if exec_out.get("live_failed") else \
            _infrastructure_answer(sid, error_msg)
        if infra_text:
            return {"text": infra_text, "image_base64": None, "table": None,
                    "code": code, "usage": usage}
        log_with_sid(sid, "warning",
                     f"EXEC_ERROR attempt {retry_count+1}: {log_safe_text(error_msg, 200)}")
        use_pro = retry_count >= 1
        use_search = retry_count >= 1
        retry_out = brain_client.retry(
            sid=sid, question=question, schema_text=schema_str,
            df_names=df_names, df_columns=df_columns,
            history_rows=history_rows,
            error_msg=error_msg, failed_code=code,
            use_pro=use_pro, use_search=use_search,
            user_email=user_email,
            dataset_profile=dataset_profile,
            sql=_retry_sql(state), sql_error=_retry_sql_error(state),
            live_tables=live_tables,
        )
        _merge_retry_sql(state, retry_out)
        new_code = retry_out.get("code") or ""
        new_kind = retry_out.get("kind")
        usage = _sum_usage(usage, retry_out.get("usage") or {})
        retry_count += 1
        # A retry that returns prose (NO_CODE/CLARIFICATION/ANSWER/MISSING_DATA)
        # or no code is a FAILED attempt, not a reason to abort: keep retrying
        # with escalation. Prose text must never reach an executor.
        if not new_code or new_kind in ("NO_CODE", "CLARIFICATION", "ANSWER", "MISSING_DATA"):
            continue
        code = new_code
        kind = new_kind
        exec_out = _exec(code, kind)

    if exec_out.get("live_role_denied"):
        return _live_role_denied_result(state, code, usage)

    # One-figure-per-chart: if the chart still combines multiple axes after the
    # retries (used by Auto Analytics, which renders one finding at a time),
    # split it and keep the FIRST single-figure chart — so a deck slide never
    # shows two plots in one image and is never silently dropped.
    if exec_out.get("multi_axes"):
        split_out = _exec(code, "PLOT_CODE", split_multi_axes=True)
        sub = [c for c in (split_out.get("multi_charts") or []) if c.get("image")]
        if sub:
            d = _describe_with_backstop(sid, question, code, user_email,
                                        chart_data=sub[0].get("chart_data"),
                                        dataset_profile=dataset_profile)
            log_with_sid(sid, "info", f"MULTI_AXES_SPLIT_SINGLE kept=1 of {len(sub)}")
            return {
                "text": d.get("text", ""),
                "image_base64": sub[0].get("image"),
                "table": None, "code": code,
                "chart_data": sub[0].get("chart_data"),
                "usage": _sum_usage(usage, d.get("usage") or {}),
            }

    if exec_out.get("error"):
        log_with_sid(sid, "error", f"EXEC_FINAL_ERROR after {retry_count+1} attempts")
        live_text = _live_exhausted_text(state) if exec_out.get("live_failed") else None
        return {
            "text": live_text or "I couldn't run this analysis with your current data. Try rephrasing or simplifying the request.",
            "image_base64": None, "table": None, "code": code, "usage": usage,
        }

    # Interactivity enforcement: a STANDARD chart that rendered as static
    # matplotlib/seaborn gets ONE Plotly-rewrite retry. Genuine matplotlib
    # exceptions (plot_tree/venn/wordcloud/...) are left untouched.
    if (kind == "PLOT_CODE" and not exec_out.get("is_plotly")
            and not exec_out.get("multi_charts")
            and _is_noninteractive_standard_chart(code)):
        log_with_sid(sid, "info", "NONINTERACTIVE_REGEN_SINGLE")
        retry_out = brain_client.retry(
            sid=sid, question=question, schema_text=schema_str,
            df_names=df_names, df_columns=df_columns, history_rows=history_rows,
            error_msg=_PLOTLY_REGEN_INSTRUCTION, failed_code=code,
            use_pro=False, use_search=False, user_email=user_email,
            dataset_profile=dataset_profile,
            sql=_retry_sql(state), sql_error=None, live_tables=live_tables,
        )
        _merge_retry_sql(state, retry_out)
        usage = _sum_usage(usage, retry_out.get("usage") or {})
        new_code = retry_out.get("code") or ""
        if new_code and retry_out.get("kind") == "PLOT_CODE":
            re_out = _exec(new_code, "PLOT_CODE")
            if not re_out.get("error") and not re_out.get("multi_axes"):
                code, exec_out = new_code, re_out

    # 3) Collect outputs
    if kind == "PLOT_CODE":
        if exec_out.get("is_plotly"):
            img_b64 = exec_out.get("plotly_html")
        else:
            img_b64 = exec_out.get("image")
        # Describe from code (brain — no data leaves)
        d = _describe_with_backstop(sid, question, code, user_email,
                                    chart_data=exec_out.get("chart_data"),
                                    dataset_profile=dataset_profile)
        return {
            "text": d.get("text", ""),
            "image_base64": img_b64,
            "table": None,
            "code": code,
            "chart_data": exec_out.get("chart_data"),
            "usage": _sum_usage(usage, d.get("usage") or {}),
        }

    # PYTHON branch
    result_obj = exec_out.get("result")
    table = _build_table_from_result(result_obj)
    if table and (int(table.get("total_rows", 0)) <= 0 or not (table.get("rows") or [])):
        table = None
    table, tables = _tables_from_python_result(result_obj, table)
    img_b64 = exec_out.get("image_base64")

    if img_b64:
        d = _describe_with_backstop(sid, question, code, user_email,
                                    result_obj=result_obj, dataset_profile=dataset_profile)
        return {"text": d.get("text", ""), "image_base64": img_b64, "table": table, "tables": tables, "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}

    if table or tables:
        d = _describe_with_backstop(sid, question, code, user_email,
                                    result_obj=result_obj, dataset_profile=dataset_profile)
        return {"text": d.get("text", ""), "image_base64": None, "table": table, "tables": tables, "code": code, "usage": _sum_usage(usage, d.get("usage") or {})}

    # Scalar result — the summarizer gets the question and the history only:
    # the preview is never sent (DIAGNOSTICS_SPEC A6, decision F1). This text
    # feeds /v1/report (Auto Analytics), so no local value line is added.
    s = brain_client.summarize(
        sid=sid, question=question, schema_text=schema_str,
        history_rows=history_rows, preview=None,
        context_decision=context_decision,
        user_email=user_email,
    )
    return {"text": s.get("text", ""), "image_base64": None, "table": None, "code": code, "usage": _sum_usage(usage, s.get("usage") or {})}


def _sum_usage(a: dict, b: dict) -> dict:
    keys = {"input_tokens", "output_tokens", "total_tokens", "reasoning_tokens"}
    out = {}
    for k in keys:
        out[k] = int((a.get(k) or 0) + (b.get(k) or 0))
    return out


# ── Deterministic result backstop (Prompt 14 Part B) ─────────────────────────
# CRITICAL RULE 7 asks the MODEL to detect a constant metric / identical series
# and explain it. Verified live: it complies only sometimes. So every describe
# call goes through this wrapper — the client inspects the executed result
# itself (result_backstop, pure pandas), sends the findings to /v1/describe as a
# mandatory caveat, and if the returned description does not carry the marker
# the client prepends its OWN localized sentence. The note is never lost.
_DF_KEY_RE = re.compile(r"dfs\[['\"]([^'\"]+)['\"]\]")


def _profile_caveat_facts(code: str, dataset_profile: dict | None) -> tuple[list, bool]:
    """Grain / catalog facts for the tables the executed CODE actually used.
    Returns (grain_lines, catalog_flag). Never raises."""
    grain: list = []
    catalog = False
    try:
        if not dataset_profile:
            return grain, catalog
        used = set(_DF_KEY_RE.findall(code or ""))
        for key, prof in dataset_profile.items():
            if used and key not in used:
                continue
            if not isinstance(prof, dict):
                continue
            g = prof.get("grain") or {}
            if isinstance(g, dict) and g.get("text"):
                grain.append(f"{key}: {g['text']}")
            for w in (prof.get("warnings") or []):
                if "catalog/link table" in str(w):
                    catalog = True
                    if str(w) not in grain:
                        grain.append(f"{key}: {w}")
    except Exception:                                        # noqa: BLE001
        return grain, catalog
    return grain[:4], catalog


def _describe_with_backstop(sid: str, question: str, code: str,
                            user_email: str | None = None, *,
                            chart_data=None, chart_data_list=None,
                            result_obj=None,
                            dataset_profile: dict | None = None,
                            findings: dict | None = None) -> dict:
    """brain_client.describe + the deterministic flat-result backstop.

    No detection and no FINDINGS -> a plain describe call with unchanged
    behavior (errors propagate exactly as before). FINDINGS only -> the
    describe carries `data_caveat={"findings": ...}` (B7.4). Detection -> the
    facts ride along as `data_caveat` (plus `findings` when there are any),
    and the answer is guaranteed to carry the explanation.

    `findings` must already be VALIDATED (`narrative.validate_findings`):
    they cross the data boundary."""
    det = result_backstop.inspect_outputs(
        chart_data=chart_data, chart_data_list=chart_data_list,
        result_obj=result_obj, sid=sid)
    finds = dict(findings) if isinstance(findings, dict) and findings else None
    if not det:
        if finds:
            return brain_client.describe(sid=sid, question=question, code=code,
                                         user_email=user_email,
                                         data_caveat={"findings": finds})
        return brain_client.describe(sid=sid, question=question, code=code,
                                     user_email=user_email)

    grain, catalog = _profile_caveat_facts(code, dataset_profile)
    caveat = {"kind": det["kind"], "facts": det.get("facts") or [],
              "grain": grain, "catalog": catalog}
    if finds:
        caveat["findings"] = finds
    log_with_sid(sid, "info",
                 f"RESULT_BACKSTOP_FIRED kind={det['kind']} "
                 f"facts={len(caveat['facts'])} catalog={catalog}")
    try:
        d = brain_client.describe(sid=sid, question=question, code=code,
                                  user_email=user_email, data_caveat=caveat)
        reason = "missing_marker"
    except Exception as e:                                   # noqa: BLE001
        # The caveat's facts embed COLUMN NAMES taken from the executed
        # result, so a failure raised while compacting them for transport can
        # quote one — same class as any other label on a log line.
        log_with_sid(sid, "warning",
                     f"RESULT_BACKSTOP_DESCRIBE_FAILED: {log_safe_text(str(e), 200)}")
        d = {"text": "", "usage": {}}
        reason = "describe_failed"

    text = (d.get("text") or "").strip()
    if result_backstop.DATA_NOTE_MARKER in text:
        log_with_sid(sid, "info", "RESULT_BACKSTOP_MARKER_OK")
        return {"text": result_backstop.strip_marker(text), "usage": d.get("usage") or {}}

    # Hard fallback: the model dropped the note (or describe failed) — the
    # client states it itself, FIRST, so the user can never see a flat result
    # without the explanation.
    lang = _detect_answer_language(question)
    sentence = result_backstop.fallback_sentence(
        lang, catalog=catalog, matrix=bool(det.get("matrix")),
        flat=det["kind"] != "matrix_readability")
    log_with_sid(sid, "info", f"RESULT_BACKSTOP_NOTE_PREPENDED reason={reason}")
    combined = f"{sentence}\n\n{text}".strip() if text else sentence
    return {"text": combined, "usage": d.get("usage") or {}}


# ── Narrative templates, VALUES and FINDINGS (DIAGNOSTICS_SPEC A5.1) ─────────
# The brain writes the answer as a TEMPLATE next to the code (`narrative`,
# `captions{"n"}`); the client fills it here from the VALUES the executed code
# set, so no result value ever reaches the brain (Article II). A rendered text
# is DISPLAY text only (R0): brain-bound text is built from templates
# (`narrative.history_content`) and describe/summarize replies.

def _backstop_prefix(lang: str, det: dict | None, catalog: bool) -> str:
    """The value-free fallback sentence + blank line that a CAPTIONED visual
    carries when the result backstop fired for it (A5.1) — the caption
    replaces the describe call, so the client states the note itself, with no
    brain call. "" when nothing fired. Never raises."""
    if not det:
        return ""
    try:
        sentence = result_backstop.fallback_sentence(
            lang, catalog=bool(catalog), matrix=bool(det.get("matrix")),
            flat=det.get("kind") != "matrix_readability")
        return f"{sentence}\n\n" if sentence else ""
    except Exception as e:                                   # noqa: BLE001
        log_with_sid("narrative", "warning",
                     f"CAPTION_BACKSTOP_FAILED error={log_safe_text(type(e).__name__, 80)}")
        return ""


def _visual_backstop_prefix(sid, question, code, *, chart_data=None,
                            chart_data_list=None, result_obj=None,
                            dataset_profile=None) -> str:
    """`_backstop_prefix` for one executed visual: the same inspection
    `_describe_with_backstop` runs, in the question's language."""
    try:
        det = result_backstop.inspect_outputs(
            chart_data=chart_data, chart_data_list=chart_data_list,
            result_obj=result_obj, sid=sid)
        if not det:
            return ""
        _grain, catalog = _profile_caveat_facts(code, dataset_profile)
        log_with_sid(sid, "info",
                     f"RESULT_BACKSTOP_CAPTION_NOTE kind={det['kind']} catalog={catalog}")
        return _backstop_prefix(_detect_answer_language(question), det, catalog)
    except Exception as e:                                   # noqa: BLE001
        log_with_sid(sid, "warning",
                     f"CAPTION_BACKSTOP_FAILED error={log_safe_text(type(e).__name__, 80)}")
        return ""


def _positive_count(value) -> int:
    """A sandbox-reported drop count as a non-negative int (0 otherwise)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


def _slot_counts(template: str, values: dict) -> tuple[int, int]:
    """(distinct slot names, how many of them have no VALUE) — for the
    NARRATIVE_USED log line only. (0, 0) for a template that does not parse."""
    try:
        names: set = set()

        def walk(nodes):
            for node in nodes:
                if node[0] == "slot":
                    names.add(node[1])
                elif node[0] == "section":
                    walk(node[3])

        walk(narrative.parse(template))
        return len(names), len([n for n in names if n not in (values or {})])
    except Exception:                                        # noqa: BLE001
        return 0, 0


# A pdc_diag `prefix=` argument in a block's code: the prefix it PASSED (the
# only prefixes a section name may resolve through, A2.2).
_PASSED_PREFIX_ARG_RE = re.compile(r"\bprefix\s*=\s*(['\"])([a-z][a-z0-9]{0,9}_)\1")


# The narrative-only retry's own time-box (A5.1): a slow retry is cut here and
# the answer takes the no-narrative path (describe / summarize + value line),
# instead of waiting the client-wide BRAIN_REQUEST_TIMEOUT.
NARRATIVE_RETRY_TIMEOUT_S = 45.0
# The live status line shown while the narrative-only retry runs (A5.4).
_NARRATIVE_RETRY_PROGRESS = {"progress": True, "message_key": "lab.narrative_retry",
                             "message": "Writing the summary…"}


def _emit_narrative_progress(sid, on_progress) -> None:
    """Hand the page the narrative-retry progress event through `on_progress`
    (None -> nothing). A raising callback is logged (type only) and ignored:
    a status line never breaks the answer."""
    if on_progress is None:
        return
    try:
        on_progress(dict(_NARRATIVE_RETRY_PROGRESS))
    except Exception as e:                                   # noqa: BLE001
        log_with_sid(sid, "warning",
                     f"NARRATIVE_PROGRESS_FAILED error={log_safe_text(type(e).__name__, 80)}")


def _log_narrative_retry_timeout(sid, started: float) -> None:
    """`NARRATIVE_RETRY_TIMEOUT elapsed_ms= timeout_s=` — the retry hit
    NARRATIVE_RETRY_TIMEOUT_S; no narrative, today's no-narrative path."""
    log_with_sid(sid, "warning",
                 f"NARRATIVE_RETRY_TIMEOUT elapsed_ms={int((_time.monotonic() - started) * 1000)} "
                 f"timeout_s={int(NARRATIVE_RETRY_TIMEOUT_S)}")


def _log_narrative_retry_done(sid, kind, started: float, reply) -> None:
    """`NARRATIVE_RETRY_DONE kind= elapsed_ms= narrative=` — whether the
    reply carries a non-blank narrative, never its text."""
    has = isinstance(reply, dict) and isinstance(reply.get("narrative"), str) \
        and bool(reply.get("narrative").strip())
    log_with_sid(sid, "info",
                 f"NARRATIVE_RETRY_DONE kind={log_safe_text(kind, 20)} "
                 f"elapsed_ms={int((_time.monotonic() - started) * 1000)} "
                 f"narrative={bool(has)}")


def _passed_prefixes(code) -> frozenset:
    """The section-name prefixes `code` passes as `prefix="<p>_"`."""
    if not isinstance(code, str):
        return frozenset()
    return frozenset(m.group(2) for m in _PASSED_PREFIX_ARG_RE.finditer(code))


@dataclass
class _NarrState:
    """One phase's narrative state (A5.1): the received templates, the
    merged VALUES, the per-block validated FINDINGS and the counters the
    NARRATIVE_USED line reports. Every method logs and degrades instead of
    raising (Article IV) — a narrative problem never breaks an answer."""
    phase: int
    template: Optional[str]
    captions: dict
    input_columns: frozenset
    sid: str
    values: dict = field(default_factory=dict)
    findings: dict = field(default_factory=dict)
    ok_blocks: int = 0
    block_findings: dict = field(default_factory=dict)
    captions_used: set = field(default_factory=set)
    # captions the quality gate emptied (every sentence held "n/a"): their
    # visuals go to describe, and a later visual of the block does too
    captions_dropped: set = field(default_factory=set)
    described: int = 0
    display_answers: list = field(default_factory=list)
    rendered: Optional[str] = None
    # the answer language code ("en" | "ka" | "ru" | "uz"): the locale of
    # every rendered number and the language of the FINDING sentences
    lang: str = "en"
    # the section-name prefixes each absorbed block's code PASSED to a
    # pdc_diag helper (`prefix="ovl_"`), by block index: a caption resolves
    # its own block's, the narrative the union of the phase's (A2.2)
    block_prefixes: dict = field(default_factory=dict)
    # the (slot name, cause) of the last first-sentence-invalid template
    # (`narrative.first_sentence_na_slot`), None otherwise: the narrative-only
    # retry's `error_msg` names it (`narrative.invalid_error_msg`)
    invalid_slot: Optional[tuple] = None
    # the local headline of a DROPPED narrative (`NARRATIVE_INVALID_TWICE`,
    # `narrative.headline_sentence`); display text only (R0)
    headline: Optional[str] = None

    @classmethod
    def from_reply(cls, reply, input_columns, sid, phase: int = 1,
                   lang: str = "en") -> "_NarrState":
        reply = reply if isinstance(reply, dict) else {}
        return cls(
            phase=phase,
            template=narrative.clean_template(reply.get("narrative"),
                                              max_len=narrative.TEMPLATE_MAX,
                                              sid=sid, what="narrative"),
            captions=narrative.clean_captions(reply.get("captions"), sid=sid),
            input_columns=frozenset(input_columns or ()),
            sid=sid,
            lang=lang if isinstance(lang, str) and lang else "en")

    @property
    def locale(self) -> Optional[str]:
        return narrative.locale_for(self.lang)

    @property
    def prefixes(self) -> frozenset:
        """The phase's passed prefixes: the union over every absorbed block."""
        out: set = set()
        for found in self.block_prefixes.values():
            out |= found
        return frozenset(out)

    def take_retry_reply(self, reply, *, block_index: int, single: bool) -> None:
        """A1.5: a retry reply's `caption:1` replaces caption `block_index`
        (absent -> kept); its `narrative` replaces the plan's only in the
        single flow (the multi flow ignores it)."""
        try:
            if not isinstance(reply, dict):
                return
            if single:
                t = narrative.clean_template(reply.get("narrative"),
                                             max_len=narrative.TEMPLATE_MAX,
                                             sid=self.sid, what="narrative")
                if t:
                    self.template = t
            caps = narrative.clean_captions(reply.get("captions"), sid=self.sid)
            if 1 in caps:
                self.captions[block_index] = caps[1]
        except Exception as e:                               # noqa: BLE001
            log_with_sid(log_safe_text(self.sid, 200), "warning",
                         f"NARRATIVE_RETRY_REPLY_FAILED error={log_safe_text(type(e).__name__, 80)}")

    def absorb(self, exec_out, code, block_index: int) -> dict:
        """After a SUCCESSFUL block: merge its VALUES (later wins), validate
        its FINDINGS against its own code, count it. Returns the block's own
        VALUES (what its caption renders with)."""
        try:
            out = exec_out if isinstance(exec_out, dict) else {}
            raw = out.get("values")
            block_values = dict(raw) if isinstance(raw, dict) else {}
            narrative.merge_values(self.values, block_values, sid=self.sid)
            values_dropped = _positive_count(out.get("values_dropped"))
            findings_dropped = _positive_count(out.get("findings_dropped"))
            if values_dropped:
                log_with_sid(log_safe_text(self.sid, 200), "info", f"VALUES_DROPPED n={int(values_dropped)}")
            if findings_dropped:
                log_with_sid(log_safe_text(self.sid, 200), "info", f"FINDINGS_DROPPED n={int(findings_dropped)}")
            validated = narrative.validate_findings(out.get("findings"), code or "",
                                                    self.input_columns, self.sid)
            passed = _passed_prefixes(code)
            self.block_prefixes[block_index] = (
                self.block_prefixes.get(block_index, frozenset()) | passed)
            if block_index in self.block_findings:
                # the children of a multi-axes regen share their block's index
                narrative.merge_findings(self.block_findings[block_index], validated,
                                         sid=self.sid)
            else:
                self.block_findings[block_index] = validated
            self.ok_blocks += 1
            return block_values
        except Exception as e:                               # noqa: BLE001
            log_with_sid(log_safe_text(self.sid, 200), "warning",
                         f"NARRATIVE_ABSORB_FAILED error={log_safe_text(type(e).__name__, 80)}")
            return {}

    def block_findings_for(self, block_index: int) -> dict:
        found = self.block_findings.get(block_index)
        return dict(found) if isinstance(found, dict) else {}

    def caption(self, block_index: int, block_values: dict):
        """(template, rendered) of caption `block_index`, rendered with THAT
        block's VALUES and FINDINGS only; None when there is none, it was
        already used (the first visual of the block takes it), or the quality
        gate dropped every sentence of it (the visual then goes to describe)."""
        try:
            if (block_index in self.captions_used or block_index in self.captions_dropped
                    or block_index not in self.captions):
                return None
            self.captions_used.add(block_index)
            template = self.captions[block_index]
            rendered = narrative.render(template, block_values or {},
                                        self.block_findings_for(block_index),
                                        sid=self.sid,
                                        max_len=narrative.CAPTION_RENDERED_MAX,
                                        locale=self.locale,
                                        prefixes=self.block_prefixes.get(block_index))
            rendered = narrative.drop_na_sentences(rendered, sid=self.sid, what="caption")
            if not rendered.strip():
                self.captions_used.discard(block_index)
                self.captions_dropped.add(block_index)
                log_with_sid(log_safe_text(self.sid, 200), "info",
                             f"CAPTION_SKIPPED block={int(block_index)} "
                             "reason=all_sentences_dropped")
                return None
            return template, rendered
        except Exception as e:                               # noqa: BLE001
            log_with_sid(log_safe_text(self.sid, 200), "warning",
                         f"CAPTION_RENDER_FAILED error={log_safe_text(type(e).__name__, 80)}")
            return None

    def _checked_render(self, template: str) -> tuple:
        """`(invalid_kind, rendered, sections)` of `template`: a parse error
        (`NARRATIVE_PARSE_FAILED kind=`) or a FIRST sentence holding "n/a"
        before the quality gate (`NARRATIVE_INVALID reason=first_sentence`)
        makes it invalid — `(kind, None, None)`; else `(None, text, sections)`.
        A first-sentence verdict caused by a slot (missing key, None or
        non-finite value) stores `(name, cause)` on `self.invalid_slot` and
        names it in the log line; any other "n/a" leaves it None."""
        self.invalid_slot = None
        kind = narrative.template_error_kind(template)
        if kind is not None:
            log_with_sid(log_safe_text(self.sid, 200), "warning",
                         f"NARRATIVE_PARSE_FAILED kind={log_safe_text(kind, 20)}")
            return kind, None, None
        rendered, sections = narrative.render_with_sections(
            template, self.values, self.findings, sid=self.sid, locale=self.locale,
            prefixes=self.prefixes)
        if narrative.first_sentence_has_na(rendered):
            slot = narrative.first_sentence_na_slot(
                template, self.values, self.findings, locale=self.locale,
                prefixes=self.prefixes, sid=self.sid)
            self.invalid_slot = slot
            if slot is not None:
                log_with_sid(log_safe_text(self.sid, 200), "warning",
                             "NARRATIVE_INVALID reason=first_sentence "
                             f"slot={log_safe_text(slot[0], 40)} "
                             f"cause={log_safe_text(slot[1], 10)}")
            else:
                log_with_sid(log_safe_text(self.sid, 200), "warning",
                             "NARRATIVE_INVALID reason=first_sentence")
            return "first_sentence", None, None
        return None, rendered, sections

    def _retry_template(self, kind: str, retry_narrative) -> Optional[str]:
        """The narrative of ONE narrative-only retry reply (`clean_template`),
        or None — no callback, a raise (logged, type only), no reply or no
        narrative. Only the narrative is read: the reply's code and captions
        are ignored and nothing re-runs."""
        if retry_narrative is None:
            return None
        try:
            reply = retry_narrative(kind)
        except Exception as e:                               # noqa: BLE001
            log_with_sid(log_safe_text(self.sid, 200), "warning",
                         f"NARRATIVE_RETRY_FAILED error={log_safe_text(type(e).__name__, 80)}")
            return None
        if not isinstance(reply, dict):
            return None
        return narrative.clean_template(reply.get("narrative"),
                                        max_len=narrative.TEMPLATE_MAX,
                                        sid=self.sid, what="narrative")

    def finish(self, retry_narrative=None) -> Optional[str]:
        """Merge the blocks' FINDINGS in block_index order (B7.2) and render
        the narrative — only when a template exists and at least one block
        succeeded. Returns the rendered text, or None (narrative not used).

        An INVALID template (a parse error, or a first sentence holding
        "n/a") gets ONE `retry_narrative(kind)` call; a valid narrative in
        its reply replaces `self.template`, anything else logs
        `NARRATIVE_INVALID_TWICE` and the narrative is not used. A valid
        template passes the quality gate, then gains the FINDING sentences
        of every finding no stated section covers (display text, R0).

        On both `NARRATIVE_INVALID_TWICE` returns (the retry gave no
        narrative, raised or timed out, or its template is invalid too)
        `self.headline` gets the local headline sentence of the main figure
        (`narrative.headline_sentence`, None when no VALUES key qualifies)."""
        self.headline = None
        try:
            self.findings = {}
            for n in sorted(self.block_findings):
                narrative.merge_findings(self.findings, self.block_findings[n], sid=self.sid)
            if not self.template:
                return None
            if self.ok_blocks < 1:
                log_with_sid(log_safe_text(self.sid, 200), "info", "NARRATIVE_SKIPPED reason=no_successful_block")
                return None
            kind, rendered, sections = self._checked_render(self.template)
            if kind is not None:
                new_template = self._retry_template(kind, retry_narrative)
                if new_template is None:
                    # no callback, a raise, no reply or a reply without a narrative
                    self.rendered = None
                    log_with_sid(log_safe_text(self.sid, 200), "warning",
                                 "NARRATIVE_INVALID_TWICE reason=no_narrative")
                    self.headline = narrative.headline_sentence(self.values, self.lang,
                                                                sid=self.sid)
                    return None
                kind, rendered, sections = self._checked_render(new_template)
                if kind is not None:
                    self.rendered = None
                    log_with_sid(log_safe_text(self.sid, 200), "warning",
                                 "NARRATIVE_INVALID_TWICE reason=invalid "
                                 f"kind={log_safe_text(kind, 20)}")
                    self.headline = narrative.headline_sentence(self.values, self.lang,
                                                                sid=self.sid)
                    return None
                self.template = new_template
            # Quality gate: a sentence holding "n/a" (a missing slot or a None
            # value) is dropped. The first sentence is protected above, so an
            # empty result is a guard only.
            rendered = narrative.drop_na_sentences(rendered, sid=self.sid, what="narrative")
            if not rendered.strip():
                self.rendered = None
                log_with_sid(log_safe_text(self.sid, 200), "info",
                             "NARRATIVE_SKIPPED reason=all_sentences_dropped")
                return None
            covered = narrative.covered_codes(rendered, sections)
            rendered = narrative.add_finding_sentences(rendered, self.findings, self.values,
                                                       covered, self.lang, sid=self.sid)
            self.rendered = rendered
            slots, missing = _slot_counts(self.template, self.values)
            log_with_sid(log_safe_text(self.sid, 200), "info",
                         f"NARRATIVE_USED phase={int(self.phase)} slots={int(slots)} "
                         f"missing={int(missing)} captions={len(self.captions_used)} "
                         f"described={int(self.described)}")
            return self.rendered
        except Exception as e:                               # noqa: BLE001
            log_with_sid(log_safe_text(self.sid, 200), "warning",
                         f"NARRATIVE_FINISH_FAILED error={log_safe_text(type(e).__name__, 80)}")
            self.rendered = None
            return None

    def phase_view(self, rendered: Optional[str] = None) -> Optional[dict]:
        """The `narrative_view.phase1` dict of the done event (`rendered`
        overrides the stored text, e.g. with the live notes appended). When
        the narrative was not used but validated FINDINGS exist, only
        `{"findings": ...}` — the record keeps them (A5.5) while its content
        stays today's text; None when there is neither."""
        if self.rendered is None or not self.template:
            return {"findings": dict(self.findings)} if self.findings else None
        return {"narrative_template": self.template,
                "narrative_rendered": rendered if rendered is not None else self.rendered,
                "values": dict(self.values), "findings": dict(self.findings)}


# ---------------------------------------------------------------------------
# Multi-chart streaming
# ---------------------------------------------------------------------------
_MAX_MULTI_PLOTS = 6
# Max BLOCKS considered before dedup (bounds render cost). The final EMITTED
# chart count is capped at _MAX_MULTI_PLOTS *after* dedup so the kept charts are
# the distinct ones — a candidate pool a bit larger than the emit cap gives
# dedup room to drop near-duplicates without starving the response.
_MAX_MULTI_PLOT_CANDIDATES = 12
# How many times a single block that renders >1 axes is sent back to the brain
# asking for SEPARATE ###NEXT_PLOT### blocks before falling back to image-split.
_MAX_MULTI_AXES_RETRY = 1
# How many times a STANDARD chart that rendered as static matplotlib/seaborn is
# sent back to the brain asking for an interactive Plotly rewrite (the model can
# regress to seaborn on retry/stochastically — this guard makes interactivity
# reliable rather than dependent on model compliance).
_MAX_PLOTLY_REGEN = 1
# One redraw for a number-grid matrix that should have been a grouped bar
# (verified live: px.imshow with text_auto over city x product row counts).
# Same shape as the interactivity regen below — the brain returns NEW CODE, so
# the stored code and the rendered chart stay in sync and per-message Refresh /
# dashboard tiles reproduce what the user saw. A server-built figure would not.
_MAX_MATRIX_REGEN = 1
_MATRIX_CODE_RE = re.compile(r"imshow|Heatmap|density_heatmap", re.IGNORECASE)
# A user who ASKED for a heatmap/matrix gets one — the planner rules allow it
# explicitly, and silently redrawing a requested chart is worse than a grid.
# Stems only — Georgian and Russian inflect ("тепловую карту", "სითბურ რუკას").
_MATRIX_ASKED_RE = re.compile(
    r"heat\s?map|matrix|სითბურ|თბურ|რუკ|матриц|теплов|теплокарт",
    re.IGNORECASE)
_MATRIX_REGEN_INSTRUCTION = (
    "UnreadableMatrixError: this chart is a number-grid matrix (heatmap) of "
    "COUNTS whose smaller dimension has 6 or fewer categories. Redraw it as a "
    "GROUPED BAR — px.bar(agg, x=<the larger dimension>, y=<the value>, "
    "color=<the smaller dimension>, barmode='group', "
    "color_discrete_sequence=px.colors.qualitative.Set2, template='plotly_white') "
    "— keeping the same aggregation, title and labels. Do NOT use px.imshow, "
    "go.Heatmap or px.density_heatmap. A matrix is only allowed here if you "
    "encode BINARY exists/absent availability with no numeric cell values."
)

_PLOTLY_REGEN_INSTRUCTION = (
    "NonInteractiveChartError: this is a STANDARD chart but it was rendered with "
    "matplotlib/seaborn. Rewrite it using INTERACTIVE Plotly — plotly.express (px) "
    "or go.Figure assigned to a variable named fig — with category NAMES on the axis "
    "(never set_xticks(range(n))+set_xticklabels). Do NOT use matplotlib or seaborn. "
    "Keep the same data, grouping, title and axis labels."
)


# ── Near-duplicate chart dedup (chat multi-plot) ──────────────────────────
# Deterministic safety net mirroring the deck-side `_aa_dedupe_instructions`
# (brain/routes/llm.py): drop a chart whose ANALYSIS is near-identical to one
# already produced in THIS multi-chart response. The deck path compares the
# natural-language instructions; here the client only sees CODE, so we compare
# an analysis SIGNATURE built from the code: the set of PLOTTED source columns
# (core px/go axis/measure kwargs + groupby dimension keys) plus the chart KIND.
# Renamed/aggregated outputs (named-agg + .rename) and concatenated labels are
# resolved back to their SOURCE columns; orientation, dataframe filenames, and a
# purely cosmetic categorical color are ignored — so two re-worded versions of
# the SAME measure by the SAME dimension (even flipped, renamed, or tinted by an
# extra category) collapse, while genuinely different charts (different metric,
# dimension, chart type, or a numeric color carrying a 2nd measure) stay.
# Threshold 0.8 matches the deck path and was calibrated on real persisted decks
# (158 within-response pairs): every true duplicate scored >= 0.8 and was dropped
# (incl. the sell-through, avg-sales-progress-rename, and oldest-models-tinted-by-
# brand cases at 1.0); distinct pairs — e.g. "Closing Balance" vs "Age in
# Inventory" by the same products (0.50), inventory-cost-AND-stock-quantity vs
# inventory-cost-only (0.75), and a bar vs a scatter on the same columns — kept.
import re as _re_sig

_DEDUP_JACCARD = 0.8
_SIG_FILE_RE = _re_sig.compile(r"\.(xlsx|xls|csv|json|parquet|tsv)$", _re_sig.IGNORECASE)
_SIG_AGGS = "sum|mean|median|count|nunique|min|max|std|var|size|first|last"
# CORE encodings define WHAT is measured against WHAT — they always count toward
# the analysis identity. Orientation is irrelevant because the signature is an
# unordered SET (a vertical and a horizontal bar of the same columns coincide).
_SIG_CORE_KW = ("x", "y", "values", "z", "dimensions", "path", "theta", "r",
                "parents", "names")
# DECORATIVE encodings (a secondary grouping/coloring). A `color=`/`line_group=`
# column only counts when it is a REAL analysis variable: an aggregation key, a
# renamed/derived measure, or a numeric color scale (color_continuous_*). A plain
# categorical color (e.g. coloring a top-N bar by brand) is cosmetic and ignored,
# so "same measure by same dimension, one tinted by an extra category" collapses.
_SIG_DECOR_KW = ("color", "line_group")
_SIG_CHART_KINDS = (
    "px.bar", "px.line", "px.scatter", "px.pie", "px.histogram", "px.box",
    "px.violin", "px.area", "px.treemap", "px.sunburst", "px.imshow",
    "px.density_heatmap", "px.density_contour", "px.funnel", "px.ecdf",
    "px.line_polar", "px.strip", "go.bar", "go.scatter", "go.pie",
    "go.histogram", "go.box", "go.violin", "go.heatmap", "go.scatterpolar",
    "go.waterfall", "go.funnel", "go.indicator",
)


def _sig_aliases(code: str) -> tuple:
    """Resolve renamed/derived columns back to their SOURCE columns. Returns
    (named, concat):
      - named  : full-substitution aliases — a renamed/aggregated output stands
        in for its source measure/dimension. Catches named-agg outputs
        `Agg=('source_col', 'mean')` AND `.rename(columns={'old':'new'})`.
      - concat : ordered components of a concatenated label
        `df['label'] = df['col_a'] + ' (' + df['col_b'] + ')'`. Whether the
        extra components count depends on context (see _chart_signature).
    """
    named: dict = {}
    concat: dict = {}
    for new, src in _re_sig.findall(
            r"(\w+)\s*=\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"](?:" + _SIG_AGGS + r")['\"]\s*\)", code):
        named.setdefault(new.strip().lower(), []).append(src.strip().lower())
    for body in _re_sig.findall(r"rename\(\s*columns\s*=\s*\{([^}]*)\}", code):
        for old, new in _re_sig.findall(r"['\"]([^'\"]+)['\"]\s*:\s*['\"]([^'\"]+)['\"]", body):
            named.setdefault(new.strip().lower(), []).append(old.strip().lower())
    for new, expr in _re_sig.findall(r"\[\s*['\"]([^'\"]+)['\"]\s*\]\s*=\s*([^\n]+)", code):
        srcs = _re_sig.findall(r"\[\s*['\"]([^'\"]+)['\"]\s*\]", expr)
        if len(srcs) >= 2 and "+" in expr:
            concat.setdefault(new.strip().lower(), []).extend(s.strip().lower() for s in srcs)
    return named, concat


def _chart_signature(code: str) -> set:
    """Analysis signature of a single plot block: the set of PLOTTED source
    columns (resolved through renames/aggregations/concat-labels) + groupby
    dimension keys + chart kind. NOT the title or styling. Two charts of the same
    measure by the same dimension yield near-identical signatures regardless of
    bar orientation, column renaming, or a cosmetic extra coloring; charts on a
    different metric, dimension, or chart type do not. Returns an empty set when
    no plotted columns can be identified (such a chart is never a duplicate)."""
    c = code or ""
    named, concat = _sig_aliases(c)
    has_numeric_color = bool(_re_sig.search(r"color_continuous_(scale|midpoint)", c))

    # groupby keys identify the real aggregation dimensions; resolve them first so
    # color / concat components can be classified against them below.
    gb_keys: set = set()
    for gb in _re_sig.findall(r"groupby\(\s*(\[[^\]]*\]|['\"][^'\"]+['\"])", c):
        for col in _re_sig.findall(r"['\"]([^'\"]+)['\"]", gb):
            gb_keys.add((col or "").strip().lower())

    def resolve(col: str) -> list:
        col = (col or "").strip().lower()
        if col in named:
            out: list = []
            for s in named[col]:
                out.extend(resolve(s) if s != col else [s])
            return out
        if col in concat:
            comps = concat[col]
            # a real multi-dimension label keeps every component that is an
            # aggregation key; a per-row label (no component is a groupby key)
            # is identified by its primary (first) component, the rest decoration.
            keep = [s for s in comps if s in gb_keys] or comps[:1]
            out = []
            for s in keep:
                out.extend(resolve(s) if s != col else [s])
            return out
        return [col]

    cols: set = set()

    def add(raw: str) -> None:
        for r in resolve(raw):
            if r and not _SIG_FILE_RE.search(r):
                cols.add(r)

    for k in gb_keys:
        add(k)

    # CORE encodings always count (scalar and list forms)
    for kw in _SIG_CORE_KW:
        for m in _re_sig.findall(rf"\b{kw}\s*=\s*['\"]([^'\"]+)['\"]", c):
            add(m)
        for lst in _re_sig.findall(rf"\b{kw}\s*=\s*\[([^\]]*)\]", c):
            for col in _re_sig.findall(r"['\"]([^'\"]+)['\"]", lst):
                add(col)
    # DECORATIVE encodings count only when the column is a real analysis variable
    for kw in _SIG_DECOR_KW:
        for m in _re_sig.findall(rf"\b{kw}\s*=\s*['\"]([^'\"]+)['\"]", c):
            for r in resolve(m):
                if not r or _SIG_FILE_RE.search(r):
                    continue
                if r in gb_keys or r in named or has_numeric_color:
                    cols.add(r)

    if not cols:
        # Nothing plotted could be identified — don't risk a false dedup.
        return set()

    toks = {("c", col) for col in cols}
    cl = c.lower()
    for k in _SIG_CHART_KINDS:
        if k in cl:
            toks.add(("k", k))
    return toks


def _signature_is_duplicate(sig: set, produced_sigs: list,
                            threshold: float = _DEDUP_JACCARD) -> bool:
    """True when `sig` is near-identical (Jaccard overlap >= threshold) to any
    signature already produced. Empty/unknown signatures are never treated as
    duplicates (keep them — better an extra chart than a dropped insight)."""
    if not sig:
        return False
    for prev in produced_sigs:
        if not prev:
            continue
        union = sig | prev
        if not union:
            continue
        if len(sig & prev) / len(union) >= threshold:
            return True
    return False


# ── Degenerate (zero-variance) chart detection (chat multi-plot) ──────────
# A chart whose plotted numeric measure is effectively constant (every bar the
# same height, a flat line, equal pie slices) conveys no insight. Detection is
# purely STATISTICAL and generic — it reads the rendered chart's numeric series
# from chart_data and compares their spread to their magnitude via a RELATIVE
# tolerance, so it works for any dataset/column/units with no hardcoded names or
# absolute thresholds. Charts with any real variation, purely categorical charts
# (no numeric series), single-point charts, and unavailable data are all kept.
_DEGENERATE_REL_TOL = 1e-6


def _series_is_flat(nums: list) -> bool:
    """True when a numeric series of >=2 points is effectively constant: its
    relative spread (range / magnitude) is within tolerance. All-equal (incl.
    all-zero) -> True; any meaningful variation -> False."""
    if len(nums) < 2:
        return False
    lo, hi = min(nums), max(nums)
    spread = hi - lo
    scale = max(abs(hi), abs(lo))
    rel = (spread / scale) if scale > 0 else spread
    return rel <= _DEGENERATE_REL_TOL


def _chart_is_degenerate(chart_data) -> bool:
    """True only when the chart has >=1 numeric plotted series AND every numeric
    series is effectively constant (see _series_is_flat) — i.e. the whole chart
    is flat and uninformative. Conservative by design: if any numeric series
    varies, or there is no numeric series (categorical/identifier chart), or the
    data is unavailable/odd-shaped, returns False (keep the chart). Never raises
    (Article IV)."""
    try:
        if not isinstance(chart_data, dict):
            return False
        tables = chart_data.get("tables")
        if isinstance(tables, list) and tables:
            row_sets = [t.get("rows") or [] for t in tables]
        else:
            row_sets = [chart_data.get("rows") or []]
        saw_numeric = False
        for rows in row_sets:
            if not rows:
                continue
            for col in list(rows[0].keys()):
                vals = [r.get(col) for r in rows]
                non_null = [v for v in vals if v is not None]
                nums = [v for v in non_null
                        if isinstance(v, (int, float)) and not isinstance(v, bool)]
                # treat a column as a numeric series only when (nearly) all its
                # non-null values are numbers and there are >=2 of them
                if len(nums) < 2 or len(nums) < len(non_null):
                    continue
                saw_numeric = True
                if not _series_is_flat([float(v) for v in nums]):
                    return False  # a real-varying series -> chart is informative
        return saw_numeric
    except Exception:
        return False


# ── Data-level near-duplicate dedup (chat multi-plot, SECOND net) ─────────
# `_chart_signature` reads the generated CODE and so cannot resolve columns
# renamed positionally (`df.columns = [...]`) instead of via `.rename(...)`.
# This net compares the chart's actual RENDERED values (the same chart_data we
# already extract), keyed on the VALUES not the column header text — so two
# charts with identical numbers under different labels (e.g. a renamed measure)
# collapse, while a different dimension, a different set of values, or a real
# extra series (dual-axis 2nd measure) stays distinct. Conservative: it acts
# ONLY on charts with a clear categorical-dimension structure and never drops
# when the data is missing/ambiguous (caller falls back to the code signature).
_DATA_DEDUP_CONTAIN = 0.9   # share of the SMALLER chart's tokens that must match
_DATA_SIG_MIN_KEYS = 2      # need >=2 distinct dimension labels to dedup safely
_DATA_SIZE_RATIO = 3.0      # only dedup comparably-sized charts (larger/smaller);
                            # a tiny rollup contained in a much larger breakdown
                            # is NOT treated as a duplicate (conservative)


def _round_sig(v, sig: int = 4):
    """Round to `sig` significant figures so tiny recompute/float differences
    don't defeat value matching. None for non-finite/non-numeric."""
    try:
        v = float(v)
    except Exception:
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    if v == 0:
        return 0.0
    import math
    try:
        d = sig - 1 - int(math.floor(math.log10(abs(v))))
        return round(v, d)
    except Exception:
        return None


def _chart_data_signature(chart_data):
    """Value-keyed signature of a chart's RENDERED data for the data-level dedup.
    Maps each dimension-label key (the row's non-numeric cell values) to the SET
    of rounded numeric values plotted for it, merged across subplots/tables — so a
    real dual-axis 2nd series makes a key's value-set differ from a single-series
    chart, keeping them distinct. Column header TEXT is ignored entirely. Returns
    a frozenset of (key, frozenset(values)) tokens, or None when the data is
    missing or lacks a categorical-dimension structure (>= _DATA_SIG_MIN_KEYS
    keys) — in which case the caller relies on the code signature only. Never
    raises (Article IV)."""
    try:
        if not isinstance(chart_data, dict):
            return None
        tables = chart_data.get("tables")
        if isinstance(tables, list) and tables:
            row_sets = [t.get("rows") or [] for t in tables]
        else:
            row_sets = [chart_data.get("rows") or []]
        rowmap: dict = {}
        for rows in row_sets:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                labels = []
                vals = set()
                for v in row.values():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        rv = _round_sig(v)
                        if rv is not None:
                            vals.add(rv)
                    elif v is not None:
                        s = str(v).strip().lower()
                        if s:
                            labels.append(s)
                if not labels and not vals:
                    continue
                # de-duplicate identical label strings within a row: a chart that
                # colors by its own dimension makes _plotly_traces_to_table add a
                # "Series" column equal to the category, which would otherwise
                # carry the same label twice and never match the un-colored twin.
                rowmap.setdefault(tuple(sorted(set(labels))), set()).update(vals)
        keyed = {k: v for k, v in rowmap.items() if v}
        if len(keyed) < _DATA_SIG_MIN_KEYS:
            return None
        return frozenset((k, frozenset(v)) for k, v in keyed.items())
    except Exception:
        return None


def _data_signature_is_duplicate(sig, produced: list,
                                 contain: float = _DATA_DEDUP_CONTAIN,
                                 max_ratio: float = _DATA_SIZE_RATIO) -> bool:
    """True when `sig` shares >= `contain` of the SMALLER chart's tokens with an
    already-produced data signature AND the two charts are comparable in size
    (larger/smaller token count <= max_ratio). Containment (not Jaccard) catches a
    Top-10 view contained in a Top-15 view of the same data; the size-ratio bound
    keeps a small rollup that is merely a fragment of a much larger breakdown
    distinct. Requires >= 2 shared tokens so a single coincidental value never
    triggers a drop. Empty/None sigs never match (keep the chart)."""
    if not sig:
        return False
    for prev in produced:
        if not prev:
            continue
        inter = len(sig & prev)
        if inter < 2:
            continue
        smaller = min(len(sig), len(prev))
        larger = max(len(sig), len(prev))
        if smaller and larger / smaller <= max_ratio and inter / smaller >= contain:
            return True
    return False


def _has_hidden_color_measure(code: str) -> bool:
    """True when the chart encodes a 2nd numeric MEASURE via a continuous color
    scale on a column that is NOT already on an axis (x/y/values). chart_data
    captures only x/y, so such a measure is invisible to the data signature —
    exclude these charts from the data-level dedup so a genuinely richer
    dual-measure chart is never collapsed onto a single-measure one. (A
    continuous color on the SAME column already plotted is just decorative and
    does NOT trigger this.) Structural/generic; never raises (Article IV)."""
    try:
        c = code or ""
        if not _re_sig.search(r"color_continuous_(scale|midpoint)", c):
            return False
        color_cols = _re_sig.findall(r"\bcolor\s*=\s*['\"]([^'\"]+)['\"]", c)
        if not color_cols:
            return False
        axis_cols = set()
        for kw in ("x", "y", "values"):
            axis_cols.update(m.strip().lower()
                             for m in _re_sig.findall(rf"\b{kw}\s*=\s*['\"]([^'\"]+)['\"]", c))
        return any(cc.strip().lower() not in axis_cols for cc in color_cols)
    except Exception:
        return False


def _split_code_on_delimiter(code: str) -> list[str]:
    """Split regenerated code on ###NEXT_PLOT### into standalone blocks (strip
    code fences if present). Returns [] when there is no delimiter."""
    if not code or "###NEXT_PLOT###" not in code:
        return []
    import re as _re
    out: list[str] = []
    for seg in code.split("###NEXT_PLOT###"):
        seg = seg.strip()
        m = _re.search(r"```(?:plot_code|python)?\s*([\s\S]*?)```", seg, _re.IGNORECASE)
        if m:
            seg = m.group(1).strip()
        if seg:
            out.append(seg)
    return out


def _extract_multi_plot_blocks(raw_text: str) -> list[str]:
    """Verbatim port of global agent._extract_multi_plot_blocks. Splits the
    planner output by ###NEXT_PLOT### and pulls one plot_code/python block
    out of each segment. Returns [] if no delimiter is present (single-plot)."""
    if not raw_text or "###NEXT_PLOT###" not in raw_text:
        return []
    import re as _re
    blocks: list[str] = []
    for seg in raw_text.split("###NEXT_PLOT###"):
        seg = seg.strip()
        if not seg:
            continue
        m = _re.search(r"```plot_code\s*([\s\S]*?)```", seg, _re.IGNORECASE)
        if not m:
            m = _re.search(r"```(?:python)?\s*([\s\S]*?)```", seg, _re.IGNORECASE)
        if m:
            code = m.group(1).strip()
            if code:
                blocks.append(code)
    return blocks


_CHART_MARKERS = ("plt.", "px.", "go.", "sns.", "plotly", "matplotlib", "seaborn",
                  "fig =", "fig=", ".figure(")


def _looks_like_table_block(code: str) -> bool:
    """A ###NEXT_PLOT### segment that assigns RESULT and never touches a
    charting API is a TABLE block (the planner's dashboard/KPI pattern mixes a
    RESULT block with plot blocks). Previously such blocks were fed to the
    chart renderer, always failed, and the table silently vanished from the
    answer."""
    if "RESULT" not in code:
        return False
    return not any(m in code for m in _CHART_MARKERS)


_MAX_MIXED_TABLE_BLOCKS = 4


def run_chat_multi_plot(
    sid: str,
    dfs: dict[str, pd.DataFrame],
    schema_docs: dict,
    question: str,
    history_rows: list,
    user_email: str,
    common_fields: list | None = None,
    dataset_profile: dict | None = None,
    on_progress=None,
):
    """Generator port of global `agent.run_chat_multi_plot`. Yields:
      - {"partial": True,  "answer", "image_base64", "chart_n", "chart_total", "usage", "code"}
      - {"partial": False, "done": True, "combined_answer", "combined_codes", "total_usage"
         [, "tables", "table_codes", "table_result_keys"]}
    For non-multi-plot responses, yields a single {"single_response": True, "result": {...}} item.

    Narrative templates (DIAGNOSTICS_SPEC A5.4): every chart partial also
    carries `caption_template` (or None), `phase: 1` and `block_index`, and
    its `answer` is the rendered caption or the describe text; when the plan's
    narrative is used, a `{"narrative": True, "phase": 1, "text", "template",
    "values", "findings"}` event follows the last block; the done event adds
    `display_answer`, `narrative_view` and, with tables, `table_captions` /
    `table_caption_templates` / `table_phases`. `combined_answer` stays
    brain-safe (templates and describe texts only, R0).

    When the narrative is dropped (`NARRATIVE_INVALID_TWICE`) and VALUES hold
    a main figure, a `{"narrative": True, "phase": 1, "text": <headline>}`
    event (no template) precedes the done event, `display_answer` starts with
    the headline and `narrative_view["headline_display"]` carries it —
    display text only; `combined_answer` never does (R0).

    `on_progress` (optional, called from this generator's thread): receives
    the narrative-only retry's `{"progress": True, "message_key":
    "lab.narrative_retry", "message"}` right before that retry, in the multi
    and the single flow. A raising callback is logged and ignored.
    """
    df_names = list(dfs.keys())
    df_columns = {name: list(df.columns) for name, df in dfs.items()}
    # The FINDINGS validator's column set (A5.1): the registered/uploaded
    # column names, taken BEFORE the plan and the live pre-fetch.
    input_columns = frozenset(str(c) for cols in df_columns.values() for c in cols)
    schema_str = build_schema_text(
        schema_docs or {}, dfs, common_fields,
        other_tables=_other_registered_tables(schema_docs, dfs, user_email))

    live_tables = _live_tables_for_brain(_live_specs(schema_docs, dfs))
    # Read ONCE per turn and passed explicitly to the plan and to every retry
    # made from this turn (brain_client never reads the setting itself).
    caps = brain_capabilities()

    # 1) Plan once — reuse the raw_text for single-vs-multi detection
    plan_out = brain_client.plan(
        sid=sid, question=question, schema_text=schema_str,
        df_names=df_names, history_rows=history_rows,
        common_fields=common_fields, user_email=user_email,
        dataset_profile=dataset_profile, live_tables=live_tables,
        capabilities=caps,
    )
    raw_text = plan_out.get("raw_text") or ""
    plan_usage = plan_out.get("usage") or {}
    # The live state is shared across the whole worklist: a table is fetched
    # once per turn and every block sees the same frame.
    state = _new_live_state(plan_out)

    def _render(code_, **kw):
        if not _ensure_live(sid, dfs, schema_docs, code_, state, user_email):
            return _live_failure_out(state)
        return render_plot_safe(code_, dfs, sid, **kw)

    def _execute(code_):
        if not _ensure_live(sid, dfs, schema_docs, code_, state, user_email):
            return _live_failure_out(state)
        return safe_execute(code_, dfs, sid)

    def _live_fields():
        return {"sql": dict(state["sql_used"]),
                "live_truncated": bool(state["truncated"]),
                "live_rows": dict(state["live_rows"])}

    plot_blocks = _extract_multi_plot_blocks(raw_text)

    if not plot_blocks:
        kind = plan_out.get("kind")
        code = plan_out.get("code") or ""
        context_decision = plan_out.get("context_decision") or {}

        # A single PLOT_CODE block is routed through the per-chart pipeline below
        # too, so the one-figure-per-chart guard applies (a lone block that
        # renders >1 axes is regenerated / split into separate charts). Non-plot
        # responses (tables, scalars, greetings, clarifications) keep the
        # single_response shape.
        if kind == "PLOT_CODE" and code.strip():
            plot_blocks = [code]
        else:
            result = _run_single_from_plan(
                sid=sid, dfs=dfs, schema_docs=schema_docs, schema_str=schema_str,
                df_columns=df_columns, df_names=df_names,
                question=question, history_rows=history_rows, user_email=user_email,
                kind=kind, code=code, usage=plan_usage,
                context_decision=context_decision,
                dataset_profile=dataset_profile,
                state=state, live_tables=live_tables,
                plan_narrative=plan_out.get("narrative"),
                plan_captions=plan_out.get("captions"),
                capabilities=caps, input_columns=input_columns,
                on_progress=on_progress,
            )
            yield {"single_response": True, "result": result}
            return

    # The phase's narrative state: the plan's templates, the merged VALUES and
    # the per-block FINDINGS (A5.1).
    narr = _NarrState.from_reply(
        plan_out, input_columns, sid,
        lang=narrative.answer_lang_code(plan_out.get("context_decision"), question))

    # Block numbers are the planner's own positions (1-based), assigned BEFORE
    # the table blocks are pulled out — `caption:n` names the n-th fence of the
    # reply, table or chart (2.2).
    numbered = list(enumerate(plot_blocks, start=1))

    # Mixed dashboard answers (KPI table + charts): pull TABLE blocks out of
    # the worklist — they execute via safe_execute and land on the done event
    # as `tables` instead of dying in the chart renderer (which silently
    # dropped the table part of every mixed answer before).
    table_blocks = [(n, b) for n, b in numbered if _looks_like_table_block(b)]
    plot_blocks = numbered
    if table_blocks:
        plot_blocks = [(n, b) for n, b in numbered if not _looks_like_table_block(b)]
        table_blocks = table_blocks[:_MAX_MIXED_TABLE_BLOCKS]
        log_with_sid(sid, "info",
                     f"MIXED_TABLE_BLOCKS extracted={len(table_blocks)} charts={len(plot_blocks)}")

    # Cap CANDIDATE blocks to bound render cost; the final EMITTED count is
    # capped at _MAX_MULTI_PLOTS *after* dedup (see the produced>=cap breaks
    # below), so the charts kept are the distinct ones rather than the first N.
    if len(plot_blocks) > _MAX_MULTI_PLOT_CANDIDATES:
        log_with_sid(sid, "warning",
                     f"MULTI_PLOT_CANDIDATES_CAPPED from={len(plot_blocks)} to={_MAX_MULTI_PLOT_CANDIDATES}")
        plot_blocks = plot_blocks[:_MAX_MULTI_PLOT_CANDIDATES]

    total_usage = dict(plan_usage)
    # Brain-safe texts (caption TEMPLATES, describe texts, fixed sentences) —
    # what builds `combined_answer`, i.e. the stored `content` (R0) ...
    combined_answers: list[str] = []
    # ... and the matching texts the user reads (rendered captions instead).
    display_answers = narr.display_answers
    combined_codes: list[str] = []
    # Analysis signatures of charts already EMITTED this response — used to drop
    # near-duplicate charts (same measure by the same dimension) the planner or a
    # regen path may produce. Mirrors the deck-side dedup (llm.py).
    produced_sigs: list = []
    # Value-keyed signatures of EMITTED charts' rendered data — the second dedup
    # net that catches duplicates the code parser can't resolve (positional
    # column renames). See _chart_data_signature.
    produced_data_sigs: list = []

    def _visual_text(n, block_values, code_, **inspect_kw):
        """The text of one visual of block `n`: its rendered caption (with
        the backstop note, no brain call) when caption n exists, else the
        per-visual describe with that block's validated FINDINGS. Returns
        (brain_safe_text, display_text, caption_template_or_None, usage)."""
        cap = narr.caption(n, block_values)
        if cap is not None:
            template, rendered = cap
            prefix = _visual_backstop_prefix(sid, question, code_,
                                             dataset_profile=dataset_profile,
                                             **inspect_kw)
            return prefix + template, prefix + rendered, prefix + template, {}
        d = _describe_with_backstop(sid, question, code_, user_email,
                                    dataset_profile=dataset_profile,
                                    findings=narr.block_findings_for(n),
                                    **inspect_kw)
        narr.described += 1
        text_ = d.get("text") or ""
        return text_, text_, None, d.get("usage") or {}

    # Worklist so a single block can EXPAND into several (when a multi-axes block
    # is regenerated as separate ###NEXT_PLOT### blocks). Each item:
    # (code, n_multi_axes_retry, n_plotly_retry, n_matrix_retry, block_index);
    # the children of a regenerated block keep its block_index.
    from collections import deque
    work = deque((blk, 0, 0, 0, n) for n, blk in plot_blocks)
    produced = 0
    # The sentence recorded once the analysis sandbox itself proved to be down
    # — it stops the chart worklist below AND the table blocks after it, which
    # dispatch through the same hop and would each wait the same queue for the
    # same answer.
    service_down_text: Optional[str] = None
    # The live-failure answer of the LAST block that failed, None when that
    # failure was not a live fetch — used only when nothing was produced.
    last_live_failure: Optional[str] = None
    log_with_sid(sid, "info", f"MULTI_PLOT_START blocks={len(work)}")

    while work:
        # Emit cap AFTER dedup: stop once _MAX_MULTI_PLOTS distinct charts have
        # been produced (dedup already ran on the earlier ones, so the kept set
        # is the distinct one). Prefer fewer, genuinely-distinct insights.
        if produced >= _MAX_MULTI_PLOTS:
            log_with_sid(sid, "info", f"MULTI_PLOT_EMIT_CAP reached={produced}")
            break
        code, ma_retry, np_retry, mx_retry, block_n = work.popleft()
        try:
            ch = hashlib.sha256(code.encode("utf-8", errors="ignore")).hexdigest()[:settings.CODE_HASH_LEN]
            log_with_sid(sid, "info", f"MULTI_PLOT_EXEC produced={produced} remaining={len(work)} code_hash={ch}")
        except Exception:
            pass

        plot_out = _render(code)

        # Standard execution-error retry (skip when it's the multi-axes signal).
        retry_count = 0
        infra_text = None
        while plot_out.get("error") and not plot_out.get("multi_axes") and retry_count < 3:
            error_msg = plot_out.get("error", "Unknown")
            if plot_out.get("live_role_denied"):
                # Ends the turn like an outage does: the sentence is the
                # answer and no further block is attempted.
                infra_text = error_msg
                break
            infra_text = None if plot_out.get("live_failed") else \
                _infrastructure_answer(sid, error_msg)
            if infra_text:
                break
            log_with_sid(sid, "warning",
                         f"MULTI_PLOT_ERROR attempt={retry_count+1}: {log_safe_text(error_msg, 200)}")
            use_pro = retry_count >= 1
            use_search = retry_count >= 1
            retry_out = brain_client.retry(
                sid=sid, question=question, schema_text=schema_str,
                df_names=df_names, df_columns=df_columns, history_rows=history_rows,
                error_msg=error_msg, failed_code=code,
                use_pro=use_pro, use_search=use_search, user_email=user_email,
                dataset_profile=dataset_profile,
                sql=_retry_sql(state), sql_error=_retry_sql_error(state),
                live_tables=live_tables, capabilities=caps,
                timeout=None,                 # the client-wide BRAIN_REQUEST_TIMEOUT
            )
            _merge_retry_sql(state, retry_out)
            new_code = retry_out.get("code") or ""
            new_kind = retry_out.get("kind")
            total_usage = _sum_usage(total_usage, retry_out.get("usage") or {})
            retry_count += 1
            # A retry returning prose (NO_CODE) or no code is a FAILED attempt,
            # not a reason to abort the block: keep retrying with escalation.
            if not new_code:
                continue
            if new_kind in ("PLOT_CODE", "PYTHON"):
                # A1.5: the reply's caption:1 replaces caption n (its
                # narrative is ignored in the multi flow).
                narr.take_retry_reply(retry_out, block_index=block_n, single=False)
            if new_kind == "PLOT_CODE":
                code = new_code
                plot_out = _render(code)
                continue
            if new_kind == "PYTHON":
                # The brain rewrote it as analysis code. Execute it; accept the
                # chart ONLY if it produced an image, else treat as a failed
                # attempt and keep retrying.
                py_out = _execute(new_code)
                if py_out.get("image_base64"):
                    code = new_code
                    plot_out = py_out
                    break
                if py_out.get("live_role_denied"):
                    infra_text = py_out.get("error")
                    break
                # This branch dispatches too, so the sandbox can fail here as
                # well — without the check it would burn the remaining
                # round-trips after the render already failed for that reason.
                infra_text = None if py_out.get("live_failed") else \
                    _infrastructure_answer(sid, py_out.get("error"))
                if infra_text:
                    break
                continue
            # Any other kind (CLARIFICATION, ANSWER, MISSING_DATA, …) → failed
            # attempt; keep retrying.
            continue

        # The sandbox itself failed: STOP the whole worklist. The remaining
        # blocks would each wait the same dispatch queue for the same answer,
        # so the user would wait one timeout per block for one outage. The
        # sentence rides `combined_answers`, which is what builds
        # `combined_answer` on the done event — so it appears exactly once and
        # the zero-charts fallback ("Something went wrong…") never fires.
        if infra_text:
            combined_answers.append(infra_text)
            display_answers.append(infra_text)
            service_down_text = infra_text
            break

        # ── One-figure-per-chart enforcement ──
        if plot_out.get("multi_axes"):
            # Preferred: ask the brain to regenerate as SEPARATE single-figure blocks.
            if ma_retry < _MAX_MULTI_AXES_RETRY:
                log_with_sid(sid, "info", f"MULTI_AXES_REGEN attempt={ma_retry+1}")
                retry_out = brain_client.retry(
                    sid=sid, question=question, schema_text=schema_str,
                    df_names=df_names, df_columns=df_columns, history_rows=history_rows,
                    error_msg=plot_out.get("error", ""), failed_code=code,
                    use_pro=True, use_search=False, user_email=user_email,
                    dataset_profile=dataset_profile,
                    sql=_retry_sql(state), sql_error=None,
                    live_tables=live_tables, capabilities=caps,
                )
                _merge_retry_sql(state, retry_out)
                total_usage = _sum_usage(total_usage, retry_out.get("usage") or {})
                new_code = retry_out.get("code") or ""
                if new_code:
                    # A1.5: caption:1 -> the first child (block n); the other
                    # children have no caption (the first visual takes it).
                    narr.take_retry_reply(retry_out, block_index=block_n, single=False)
                    blocks = _split_code_on_delimiter(new_code) or [new_code]
                    for b in blocks:
                        work.append((b, ma_retry + 1, np_retry, mx_retry, block_n))
                    continue
            # Fallback: split the rendered figure into one image (+ data) per axis.
            split_out = _render(code, split_multi_axes=True)
            sub = [c for c in (split_out.get("multi_charts") or []) if c.get("image")]
            if sub:
                # The block executed successfully: its VALUES count even if
                # the group is dropped as a duplicate below (A3.2).
                block_values = narr.absorb(split_out, code, block_n)
                # Dedup: a split group is ONE analysis (all sub-charts share this
                # block's code) — drop the whole group if near-identical to an
                # already-emitted chart. The sub-charts within it are distinct
                # axes and are NOT deduped against each other.
                sig = _chart_signature(code)
                if _signature_is_duplicate(sig, produced_sigs):
                    log_with_sid(sid, "info", "MULTI_PLOT_DEDUP_SKIP split")
                    continue
                produced_sigs.append(sig)
                base_safe, base_shown, base_tmpl, d_usage = _visual_text(
                    block_n, block_values, code,
                    chart_data_list=[c.get("chart_data") for c in sub])
                total_usage = _sum_usage(total_usage, d_usage)
                log_with_sid(sid, "info", f"MULTI_AXES_SPLIT_EMIT charts={len(sub)}")
                for i, cobj in enumerate(sub):
                    produced += 1
                    # caption n (or the describe text) on sub-chart 0, the
                    # others keep their own title (A5.1)
                    title = cobj.get("title") or ""
                    txt = base_shown if i == 0 else title
                    combined_answers.append(base_safe if i == 0 else title)
                    display_answers.append(txt)
                    if i == 0:
                        combined_codes.append(code)
                    sub_data_sig = (None if _has_hidden_color_measure(code)
                                    else _chart_data_signature(cobj.get("chart_data")))
                    if sub_data_sig is not None:
                        produced_data_sigs.append(sub_data_sig)
                    yield {
                        "partial": True, "answer": txt, "image_base64": cobj.get("image"),
                        "chart_n": produced, "chart_total": produced + len(work),
                        "usage": dict(total_usage), "code": code,
                        "chart_data": cobj.get("chart_data"),
                        "sql": dict(state["sql_used"]),
                        "caption_template": base_tmpl if i == 0 else None,
                        "phase": 1, "block_index": block_n,
                    }
                    # A split block can expand into several axes — respect the
                    # emit cap within the group too.
                    if produced >= _MAX_MULTI_PLOTS:
                        break
            continue

        if plot_out.get("error"):
            # Never absorbed: a block that failed every retry contributes no
            # VALUES, so its slots render "n/a" (A3.2).
            log_with_sid(sid, "warning", "MULTI_PLOT_SKIP after retries")
            last_live_failure = (_live_exhausted_text(state)
                                 if plot_out.get("live_failed") else None)
            continue

        # ── Interactivity enforcement (standard charts must be Plotly) ──
        # The render succeeded but came back static matplotlib/seaborn for a
        # STANDARD chart — ask the brain once for an interactive Plotly rewrite.
        if (np_retry < _MAX_PLOTLY_REGEN and not plot_out.get("is_plotly")
                and not plot_out.get("multi_charts")
                and _is_noninteractive_standard_chart(code)):
            log_with_sid(sid, "info", f"NONINTERACTIVE_REGEN attempt={np_retry+1}")
            retry_out = brain_client.retry(
                sid=sid, question=question, schema_text=schema_str,
                df_names=df_names, df_columns=df_columns, history_rows=history_rows,
                error_msg=_PLOTLY_REGEN_INSTRUCTION, failed_code=code,
                use_pro=False, use_search=False, user_email=user_email,
                dataset_profile=dataset_profile,
                sql=_retry_sql(state), sql_error=None, live_tables=live_tables,
                capabilities=caps,
            )
            _merge_retry_sql(state, retry_out)
            total_usage = _sum_usage(total_usage, retry_out.get("usage") or {})
            new_code = retry_out.get("code") or ""
            if new_code and retry_out.get("kind") == "PLOT_CODE":
                narr.take_retry_reply(retry_out, block_index=block_n, single=False)
                # Reprocess this item next (preserve order); np_retry+1 caps it.
                work.appendleft((new_code, ma_retry, np_retry + 1, mx_retry, block_n))
                continue

        # ── Readable-encoding enforcement (count matrix -> grouped bar) ──
        # A number grid whose smaller dimension has <=6 categories is a grouped
        # bar waiting to happen (the planner's own default). Ask the brain ONCE
        # for the redraw — the answer comes back as CODE, so the stored code and
        # the chart stay in sync (per-message Refresh and dashboard tiles
        # re-execute that code). Both gates must agree before spending the call:
        # the rendered data must have the matrix shape AND the code must
        # actually draw one. If the redraw fails, the chart is kept and the
        # deterministic caveat names the alternative instead.
        if (mx_retry < _MAX_MATRIX_REGEN and _MATRIX_CODE_RE.search(code or "")
                and not _MATRIX_ASKED_RE.search(question or "")
                and result_backstop.matrix_readability(plot_out.get("chart_data"))):
            log_with_sid(sid, "info", f"MATRIX_REGEN attempt={mx_retry+1}")
            retry_out = brain_client.retry(
                sid=sid, question=question, schema_text=schema_str,
                df_names=df_names, df_columns=df_columns, history_rows=history_rows,
                error_msg=_MATRIX_REGEN_INSTRUCTION, failed_code=code,
                use_pro=False, use_search=False, user_email=user_email,
                dataset_profile=dataset_profile,
                sql=_retry_sql(state), sql_error=None, live_tables=live_tables,
                capabilities=caps,
            )
            _merge_retry_sql(state, retry_out)
            total_usage = _sum_usage(total_usage, retry_out.get("usage") or {})
            new_code = retry_out.get("code") or ""
            if new_code and retry_out.get("kind") == "PLOT_CODE":
                narr.take_retry_reply(retry_out, block_index=block_n, single=False)
                work.appendleft((new_code, ma_retry, np_retry, mx_retry + 1, block_n))
                continue
            log_with_sid(sid, "info", "MATRIX_REGEN_KEPT_ORIGINAL")

        # The block executed successfully and is final: its VALUES merge now,
        # BEFORE the degenerate / dedup skips below, so a dropped chart still
        # contributes them (A3.2).
        block_values = narr.absorb(plot_out, code, block_n)

        # ── Degenerate (zero-variance) chart skip ──
        # Drop a chart whose every numeric series is effectively constant (no
        # insight). Purely statistical (see _chart_is_degenerate). Safety: never
        # skip when it would leave the response with zero charts — only skip if at
        # least one chart was already produced OR another block remains to try; the
        # sole/last candidate is always kept. (run_chat single-chart path is
        # untouched — this guard lives only in the multi-plot loop.)
        if ((produced >= 1 or len(work) >= 1)
                and _chart_is_degenerate(plot_out.get("chart_data"))):
            log_with_sid(sid, "info", "DEGENERATE_CHART_SKIP")
            continue

        # ── Near-duplicate dedup ──
        # Skip a chart whose analysis is near-identical to one already emitted in
        # THIS response (planner re-wording or a regen re-introducing the same
        # measure×dimension). Applies to initial AND regen-added blocks (every
        # block reaches this point before emission).
        sig = _chart_signature(code)
        if _signature_is_duplicate(sig, produced_sigs):
            log_with_sid(sid, "info", "MULTI_PLOT_DEDUP_SKIP")
            continue
        # ── Data-level dedup (second net) ──
        # Catches duplicates the code parser misses (e.g. positional
        # `df.columns=[...]` renames) by comparing the actual rendered values.
        # Only fires when chart_data yields a usable value-keyed signature; never
        # drops on missing/ambiguous data (falls back to the code signature).
        data_sig = (None if _has_hidden_color_measure(code)
                    else _chart_data_signature(plot_out.get("chart_data")))
        if data_sig is not None and _data_signature_is_duplicate(data_sig, produced_data_sigs):
            log_with_sid(sid, "info", "MULTI_PLOT_DEDUP_SKIP_DATA")
            continue
        produced_sigs.append(sig)
        if data_sig is not None:
            produced_data_sigs.append(data_sig)

        # Determine the renderable chart from either executor's output shape
        # (render_plot_safe → image/plotly_html; safe_execute → image_base64).
        if plot_out.get("image_base64"):
            img_b64 = plot_out.get("image_base64")
        elif plot_out.get("is_plotly"):
            img_b64 = plot_out.get("plotly_html")
        else:
            img_b64 = plot_out.get("image")
        # Caption n (rendered locally) or describe from code (brain — no data
        # leaves)
        safe_text, text, caption_tmpl, d_usage = _visual_text(
            block_n, block_values, code, chart_data=plot_out.get("chart_data"))
        total_usage = _sum_usage(total_usage, d_usage)

        produced += 1
        combined_answers.append(safe_text)
        display_answers.append(text)
        combined_codes.append(code)
        log_with_sid(sid, "info", f"PLOT_RENDERED chart={produced} plotly={plot_out.get('is_plotly')}")

        yield {
            "partial": True, "answer": text, "image_base64": img_b64,
            "chart_n": produced, "chart_total": produced + len(work),
            "usage": dict(total_usage), "code": code,
            "chart_data": plot_out.get("chart_data"),
            "sql": dict(state["sql_used"]),
            "caption_template": caption_tmpl, "phase": 1, "block_index": block_n,
        }

    # Execute the extracted TABLE blocks (mixed dashboard answers). Each block
    # yields one table (or several, when its RESULT was a dict of DataFrames);
    # they ride on the done event with their block code + dict entry so the
    # per-table Download Excel / Show full table re-execute correctly.
    combined_tables: list = []
    table_codes: list = []
    table_result_keys: list = []
    # Aligned with `combined_tables`: the rendered caption (None without one,
    # "" on the 2nd+ table of a captioned dict result), its template, its phase.
    table_captions: list = []
    table_caption_templates: list = []
    table_phases: list = []
    for block_n, tb in table_blocks if table_blocks else []:
        # The chart loop already established that the sandbox is down, and
        # these blocks dispatch through the SAME hop: each would wait the full
        # dispatch queue (ten minutes by default) to be told the same thing,
        # which is precisely the per-block wait the chart loop's `break`
        # exists to avoid. Stopping here loses nothing — the tables emitted
        # before the outage are already on `combined_tables` and still ride
        # the done event, and the sentence is already in `combined_answers`.
        if service_down_text:
            log_with_sid(sid, "warning", "MIXED_TABLE_BLOCKS_SKIPPED service_down")
            break
        try:
            exec_out = _execute(tb)
            if exec_out.get("error"):
                if exec_out.get("live_role_denied"):
                    infra_text = exec_out.get("error")
                elif exec_out.get("live_failed"):
                    infra_text = None
                else:
                    infra_text = _infrastructure_answer(sid, exec_out.get("error"))
                if infra_text:
                    # Same reasoning, for an outage that starts HERE (a run
                    # whose chart blocks all succeeded, or one that had none
                    # left after the extraction above).
                    service_down_text = infra_text
                    if infra_text not in combined_answers:
                        combined_answers.append(infra_text)
                        display_answers.append(infra_text)
                    log_with_sid(sid, "warning", "MIXED_TABLE_BLOCKS_SKIPPED service_down")
                    break
                log_with_sid(sid, "warning",
                             f"MIXED_TABLE_BLOCK_ERROR: "
                             f"{log_safe_text(str(exec_out['error']), 200)}")
                last_live_failure = (_live_exhausted_text(state)
                                     if exec_out.get("live_failed") else None)
                continue
            # Executed successfully: VALUES merge even when no table comes
            # out of it (A3.2).
            block_values = narr.absorb(exec_out, tb, block_n)
            result_obj = exec_out.get("result")
            t = _build_table_from_result(result_obj)
            if t and (int(t.get("total_rows", 0)) <= 0 or not (t.get("rows") or [])):
                t = None
            ts = None
            if t is None:
                ts = _build_tables_from_result(result_obj)
            emitted = ([t] if t else []) + (ts or [])
            if not emitted:
                continue
            safe_text, text, caption_tmpl, d_usage = _visual_text(
                block_n, block_values, tb, result_obj=result_obj)
            total_usage = _sum_usage(total_usage, d_usage)
            if caption_tmpl is None and text:
                # the per-block describe text, as today
                combined_answers.append(safe_text)
                display_answers.append(text)
            for i, entry in enumerate(emitted):
                combined_tables.append(entry)
                table_codes.append(tb)
                table_result_keys.append(entry.get("title") if ts else None)
                if caption_tmpl is not None:
                    # caption n on the first table of the block, "" on the others
                    table_captions.append(text if i == 0 else "")
                    table_caption_templates.append(caption_tmpl if i == 0 else "")
                else:
                    table_captions.append(None)
                    table_caption_templates.append(None)
                table_phases.append(1)
            if caption_tmpl is not None:
                combined_answers.append(safe_text)
                display_answers.append(text)
            log_with_sid(sid, "info", f"MIXED_TABLE_EMIT tables={len(emitted)}")
        except Exception as e:
            # Raised while building the table from the executed RESULT, so the
            # message can carry a label or dict key the sandbox chose.
            log_with_sid(sid, "warning",
                         f"MIXED_TABLE_BLOCK_FAILED: {log_safe_text(str(e), 200)}")

    def _retry_narrative(kind: str):
        """ONE narrative-only retry (A5.1): the exec-error retry's keywords,
        `error_msg = narrative.invalid_error_msg(kind, narr.invalid_slot)`,
        the emitted codes as `failed_code`, time-boxed to
        NARRATIVE_RETRY_TIMEOUT_S and announced through `on_progress` right
        before. Only the reply's narrative is used — its code never runs, so
        the SQL map is not merged. A raise or a timeout -> no reply."""
        nonlocal total_usage
        _emit_narrative_progress(sid, on_progress)
        started = _time.monotonic()
        try:
            emitted = list(dict.fromkeys(list(combined_codes) + list(table_codes)))
            retry_out = brain_client.retry(
                sid=sid, question=question, schema_text=schema_str,
                df_names=df_names, df_columns=df_columns, history_rows=history_rows,
                error_msg=narrative.invalid_error_msg(kind, narr.invalid_slot),
                failed_code="\n###NEXT_PLOT###\n".join(emitted),
                use_pro=True, use_search=False, user_email=user_email,
                dataset_profile=dataset_profile,
                sql=_retry_sql(state), sql_error=None,
                live_tables=live_tables, capabilities=caps,
                timeout=NARRATIVE_RETRY_TIMEOUT_S,
            )
            if isinstance(retry_out, dict):
                total_usage = _sum_usage(total_usage, retry_out.get("usage") or {})
            _log_narrative_retry_done(sid, kind, started, retry_out)
            return retry_out
        except brain_client.BrainTimeoutError:
            _log_narrative_retry_timeout(sid, started)
            return None
        except Exception as e:                               # noqa: BLE001
            log_with_sid(sid, "warning",
                         f"NARRATIVE_RETRY_FAILED error={log_safe_text(type(e).__name__, 80)}")
            return None

    # The phase's narrative, rendered once every block has run (A5.1).
    rendered = narr.finish(retry_narrative=_retry_narrative)
    narrative_used = rendered is not None

    # If every block failed all its retries we rendered ZERO charts — surface a
    # clear failure rather than the misleading bare "Analysis complete." (a run
    # that still emitted tables is not a failure).
    if combined_answers:
        combined_text = "\n\n".join([a for a in combined_answers if a])
        display_text = "\n\n".join([a for a in display_answers if a])
    elif combined_tables:
        combined_text = display_text = "Analysis complete."
    else:
        combined_text = display_text = (
            last_live_failure
            or "Something went wrong with this analysis. Please try again.")

    if narrative_used:
        # Brain-safe: the FINDINGS marker + the TEMPLATE, never a value (R0);
        # the live notes go on the rendered text only.
        combined_text = narrative.history_content(narr.template, narr.findings)
        display_text = rendered
        if state["truncated"] and display_text:
            display_text = _append_live_notes(display_text, state, question)
        yield {"narrative": True, "phase": 1, "text": display_text,
               "template": narr.template, "values": dict(narr.values),
               "findings": dict(narr.findings)}
    elif state["truncated"]:
        if display_text:
            display_text = _append_live_notes(display_text, state, question)
        # Today's behaviour for an answer built from describe texts only; an
        # answer carrying caption templates keeps its brain-safe text clean.
        if combined_text and not narr.captions_used:
            combined_text = _append_live_notes(combined_text, state, question)

    # A dropped narrative (NARRATIVE_INVALID_TWICE) with a main figure in
    # VALUES: the local headline fills slot 1 above the charts and leads the
    # display text. Display only (R0) — `combined_text` never carries it.
    headline = narr.headline if not narrative_used else None
    if headline:
        yield {"narrative": True, "phase": 1, "text": headline}
        display_text = headline + "\n\n" + display_text if display_text else headline

    log_with_sid(sid, "info", f"MULTI_PLOT_DONE rendered={produced}")
    done_event = {
        "partial": False, "done": True,
        "combined_answer": combined_text,
        "display_answer": display_text,
        "combined_codes": combined_codes,
        "total_usage": total_usage,
        "narrative_view": {
            "phase1": narr.phase_view(display_text if narrative_used else None),
            "phase2": None, "phase2_status": None,
        },
        **_live_fields(),
    }
    if headline:
        done_event["narrative_view"]["headline_display"] = headline
    if combined_tables:
        done_event["tables"] = combined_tables
        done_event["table_codes"] = table_codes
        done_event["table_result_keys"] = table_result_keys
        done_event["table_captions"] = table_captions
        done_event["table_caption_templates"] = table_caption_templates
        done_event["table_phases"] = table_phases
    yield done_event


def _run_single_from_plan(*, sid, dfs, schema_docs, schema_str, df_columns, df_names,
                           question, history_rows, user_email,
                           kind, code, usage, context_decision,
                           dataset_profile: dict | None = None,
                           state: dict | None = None,
                           live_tables: list | None = None,
                           plan_narrative=None, plan_captions=None,
                           capabilities: list | None = None,
                           input_columns: frozenset | None = None,
                           on_progress=None) -> dict:
    """Execute one already-planned response (kind/code from a prior /v1/plan)
    locally, reusing the same retry + describe/summarize flow as run_chat.

    `dataset_profile` feeds both the retry planner and the result backstop. It
    was referenced by the retry call below without being a parameter (Prompt 13
    oversight) — a failing first execution raised NameError instead of retrying.

    `state` is the turn's live state (the caller's, built from the plan
    response's `sql`); `live_tables` the payload rows for the retry calls.
    The result carries `sql`, `live_truncated` and `live_rows`.

    `plan_narrative` / `plan_captions` are the plan's templates, `capabilities`
    the turn's list (sent on every retry), `input_columns` the FINDINGS
    validator's column set. The result then carries the A5.3 keys
    (`narrative_template`, `narrative_rendered`, `values`, `findings`,
    `caption_template`, `caption_rendered`, `caption_described`,
    `value_display`, and `headline_display` for a dropped narrative without
    a value line) — each only when present; `text` stays brain-safe.
    `on_progress` gets the narrative-only retry's progress event.
    """
    if state is None:
        state = _new_live_state()
    out = _run_single_body(sid=sid, dfs=dfs, schema_docs=schema_docs,
                           schema_str=schema_str, df_columns=df_columns,
                           df_names=df_names, question=question,
                           history_rows=history_rows, user_email=user_email,
                           kind=kind, code=code, usage=usage,
                           context_decision=context_decision,
                           dataset_profile=dataset_profile, state=state,
                           live_tables=live_tables,
                           plan_narrative=plan_narrative, plan_captions=plan_captions,
                           capabilities=capabilities,
                           input_columns=input_columns,
                           on_progress=on_progress)
    return _finish_live_result(out, state, question, sid)


def _run_single_body(*, sid, dfs, schema_docs, schema_str, df_columns, df_names,
                     question, history_rows, user_email,
                     kind, code, usage, context_decision,
                     dataset_profile, state, live_tables,
                     plan_narrative=None, plan_captions=None,
                     capabilities=None, input_columns=None,
                     on_progress=None) -> dict:
    def _exec(code_, kind_):
        if not _ensure_live(sid, dfs, schema_docs, code_, state, user_email):
            return _live_failure_out(state)
        fn = render_plot_safe if kind_ == "PLOT_CODE" else safe_execute
        return fn(code_, dfs, sid)

    if kind in ("CLARIFICATION", "ANSWER", "MISSING_DATA"):
        return {"text": code, "image_base64": None, "table": None, "code": None, "usage": usage}

    if kind == "NO_CODE":
        if context_decision.get("is_greeting"):
            g = brain_client.greeting(sid, question, df_names, user_email=user_email)
            return {"text": g.get("text", ""), "image_base64": None, "table": None,
                    "code": None, "usage": _sum_usage(usage, g.get("usage", {}))}
        return {"text": "I couldn't generate analysis code for this question. Try rephrasing.",
                "image_base64": None, "table": None, "code": None, "usage": usage}

    narr = _NarrState.from_reply({"narrative": plan_narrative, "captions": plan_captions},
                                 input_columns, sid,
                                 lang=narrative.answer_lang_code(context_decision, question))
    # The usage of the narrative-only retry (`_retry_narrative`), added to
    # every answer `_compose` builds.
    narr_usage: dict = {}

    def _retry_narrative(kind: str):
        """ONE narrative-only retry (A5.1): the exec-error retry's keywords,
        `error_msg = narrative.invalid_error_msg(kind, narr.invalid_slot)`,
        the executed `code` as `failed_code`, time-boxed to
        NARRATIVE_RETRY_TIMEOUT_S and announced through `on_progress` right
        before. Only the reply's narrative is used — its code never runs, so
        the SQL map is not merged. A raise or a timeout -> no reply."""
        _emit_narrative_progress(sid, on_progress)
        started = _time.monotonic()
        try:
            retry_out = brain_client.retry(
                sid=sid, question=question, schema_text=schema_str,
                df_names=df_names, df_columns=df_columns,
                history_rows=history_rows,
                error_msg=narrative.invalid_error_msg(kind, narr.invalid_slot),
                failed_code=code,
                use_pro=True, use_search=False,
                user_email=user_email,
                dataset_profile=dataset_profile,
                sql=_retry_sql(state), sql_error=None,
                live_tables=live_tables, capabilities=capabilities,
                timeout=NARRATIVE_RETRY_TIMEOUT_S,
            )
            if isinstance(retry_out, dict):
                narr_usage.update(_sum_usage(narr_usage, retry_out.get("usage") or {}))
            _log_narrative_retry_done(sid, kind, started, retry_out)
            return retry_out
        except brain_client.BrainTimeoutError:
            _log_narrative_retry_timeout(sid, started)
            return None
        except Exception as e:                               # noqa: BLE001
            log_with_sid(sid, "warning",
                         f"NARRATIVE_RETRY_FAILED error={log_safe_text(type(e).__name__, 80)}")
            return None

    exec_out = _exec(code, kind)

    retry_count = 0
    max_retries = 3  # up to 3 retry attempts; escalate to pro/search from the 2nd
    while exec_out.get("error") and retry_count < max_retries:
        if exec_out.get("live_role_denied"):
            return _live_role_denied_result(state, code, usage)
        error_msg = exec_out.get("error", "Unknown")
        # The sandbox itself failed: no rewrite of this code can help, so
        # answer once instead of spending three brain calls on an outage.
        # (A failed live fetch is a query error, never an outage verdict.)
        infra_text = None if exec_out.get("live_failed") else \
            _infrastructure_answer(sid, error_msg)
        if infra_text:
            return {"text": infra_text, "image_base64": None, "table": None,
                    "code": code, "usage": usage}
        log_with_sid(sid, "warning",
                     f"EXEC_ERROR attempt {retry_count+1}: {log_safe_text(error_msg, 200)}")
        use_pro = retry_count >= 1
        use_search = retry_count >= 1
        retry_out = brain_client.retry(
            sid=sid, question=question, schema_text=schema_str,
            df_names=df_names, df_columns=df_columns,
            history_rows=history_rows,
            error_msg=error_msg, failed_code=code,
            use_pro=use_pro, use_search=use_search,
            user_email=user_email,
            dataset_profile=dataset_profile,
            sql=_retry_sql(state), sql_error=_retry_sql_error(state),
            live_tables=live_tables, capabilities=capabilities,
            timeout=None,                     # the client-wide BRAIN_REQUEST_TIMEOUT
        )
        _merge_retry_sql(state, retry_out)
        new_code = retry_out.get("code") or ""
        new_kind = retry_out.get("kind")
        usage = _sum_usage(usage, retry_out.get("usage") or {})
        retry_count += 1
        # A retry that returns prose (NO_CODE/CLARIFICATION/ANSWER/MISSING_DATA)
        # or no code is a FAILED attempt, not a reason to abort: keep retrying
        # with escalation. Prose text must never reach an executor.
        if not new_code or new_kind in ("NO_CODE", "CLARIFICATION", "ANSWER", "MISSING_DATA"):
            continue
        # A1.5 (single flow): a narrative in the reply REPLACES the plan's,
        # its caption:1 replaces caption 1; absent -> kept.
        narr.take_retry_reply(retry_out, block_index=1, single=True)
        code = new_code
        kind = new_kind
        exec_out = _exec(code, kind)

    if exec_out.get("live_role_denied"):
        narr.finish()
        return _live_role_denied_result(state, code, usage)
    if exec_out.get("error"):
        # Never absorbed; the narrative is not used (no successful block).
        narr.finish()
        live_text = _live_exhausted_text(state) if exec_out.get("live_failed") else None
        return {
            "text": live_text or "I couldn't run this analysis with your current data. Try rephrasing.",
            "image_base64": None, "table": None, "code": code, "usage": usage,
        }

    block_values = narr.absorb(exec_out, code, 1)
    # The implicit `result` slot (A3.2): a scalar RESULT fills it unless the
    # code set its own.
    safe_p = _safe_preview(exec_out.get("preview")) if kind != "PLOT_CODE" else None
    if isinstance(safe_p, (str, int, float, bool)):
        narr.values.setdefault("result", safe_p)
        block_values.setdefault("result", safe_p)

    def _visual(**inspect_kw) -> tuple[dict, dict]:
        """Caption 1 (rendered locally, with the backstop note, no brain call)
        or the describe text with the validated FINDINGS. Returns (fields,
        usage) where fields holds `caption_template` / `caption_rendered`, or
        `described`."""
        cap = narr.caption(1, block_values)
        if cap is not None:
            template, rendered_cap = cap
            prefix = _visual_backstop_prefix(sid, question, code,
                                             dataset_profile=dataset_profile,
                                             **inspect_kw)
            return ({"caption_template": prefix + template,
                     "caption_rendered": prefix + rendered_cap}, {})
        d = _describe_with_backstop(sid, question, code, user_email,
                                    dataset_profile=dataset_profile,
                                    findings=narr.block_findings_for(1),
                                    **inspect_kw)
        narr.described += 1
        return {"described": d.get("text", "")}, d.get("usage") or {}

    def _compose(visual: dict | None) -> dict:
        """The A5.3 keys and the brain-safe `text`, from the visual's fields
        and the rendered narrative."""
        rendered = narr.finish(retry_narrative=_retry_narrative)
        fields: dict = {}
        visual = visual or {}
        if rendered is not None:
            fields["narrative_template"] = narr.template
            fields["narrative_rendered"] = rendered
            fields["text"] = narrative.history_content(narr.template, narr.findings)
            if "described" in visual:
                fields["caption_described"] = visual["described"]
        elif "caption_template" in visual:
            fields["text"] = visual["caption_template"]        # never the rendered one (R0)
        elif "described" in visual:
            fields["text"] = visual["described"]
        if "caption_template" in visual:
            fields["caption_template"] = visual["caption_template"]
            fields["caption_rendered"] = visual["caption_rendered"]
        if rendered is not None or "caption_template" in visual:
            fields["values"] = dict(narr.values)
            fields["findings"] = dict(narr.findings)
        elif narr.findings:
            # A describe-only turn keeps its validated FINDINGS on the record
            # (A5.5); `text` stays the describe text (no marker).
            fields["findings"] = dict(narr.findings)
        if rendered is None and narr.headline:
            # The dropped narrative's local headline (display only, R0). The
            # scalar / flat-dict path discards these fields (no `text`) and
            # shows the figure through its `value_display` line instead.
            fields["headline_display"] = narr.headline
        return fields

    if kind == "PLOT_CODE":
        img_b64 = exec_out.get("plotly_html") if exec_out.get("is_plotly") else exec_out.get("image")
        visual, d_usage = _visual(chart_data=exec_out.get("chart_data"))
        fields = _compose(visual)
        return {"image_base64": img_b64, "table": None,
                "code": code, "chart_data": exec_out.get("chart_data"),
                "usage": _sum_usage(_sum_usage(usage, d_usage), narr_usage), **fields}

    # PYTHON
    result_obj = exec_out.get("result")
    table = _build_table_from_result(result_obj)
    if table and (int(table.get("total_rows", 0)) <= 0 or not (table.get("rows") or [])):
        table = None
    table, tables = _tables_from_python_result(result_obj, table)
    img_b64 = exec_out.get("image_base64")

    if img_b64:
        visual, d_usage = _visual(result_obj=result_obj)
        fields = _compose(visual)
        return {"image_base64": img_b64, "table": table,
                "tables": tables, "code": code,
                "usage": _sum_usage(_sum_usage(usage, d_usage), narr_usage), **fields}
    if table or tables:
        visual, d_usage = _visual(result_obj=result_obj)
        fields = _compose(visual)
        return {"image_base64": None, "table": table,
                "tables": tables, "code": code,
                "usage": _sum_usage(_sum_usage(usage, d_usage), narr_usage), **fields}

    # A scalar / flat-dict result. With a narrative, the answer IS the
    # rendered narrative: no summarize call. Without one, summarize gets no
    # preview (A6, decision F1) and the value is shown through the local
    # `value_display` line (display only, R0).
    if narr.template:
        fields = _compose(None)
        if "text" in fields:
            return {"image_base64": None, "table": None, "code": code,
                    "usage": _sum_usage(usage, narr_usage), **fields}
    s = brain_client.summarize(
        sid=sid, question=question, schema_text=schema_str,
        history_rows=history_rows, preview=None,
        context_decision=context_decision, user_email=user_email,
    )
    out = {"text": s.get("text", ""), "image_base64": None, "table": None,
           "code": code,
           "usage": _sum_usage(_sum_usage(usage, s.get("usage") or {}), narr_usage)}
    shown = narrative.value_display(_safe_preview(exec_out.get("preview")),
                                    narrative.answer_lang_code(context_decision, question))
    if shown:
        out["value_display"] = shown
    elif narr.headline:
        # no value line to show the figure: the dropped narrative's headline
        out["headline_display"] = narr.headline
    if not narr.template:
        narr.finish()                  # merges the block's FINDINGS (no template here)
    # with a template `_compose` above already ran `finish` (the narrative was
    # dropped whole by the quality gate) and merged the FINDINGS
    if narr.findings:
        out["findings"] = dict(narr.findings)
    return out
