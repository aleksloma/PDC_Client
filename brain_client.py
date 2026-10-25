"""HTTP client that the on-prem container uses to call the brain.

Transport: HTTPS + per-tenant bearer token in the Authorization header
(per the prompt: "The client container calls the brain over HTTPS. ...
Authentication is a per-tenant bearer token sent in the Authorization header
(Authorization: Bearer <tenant_token>).").

A revoked tenant gets HTTP 403 — the kill-switch. We surface that as a
`TenantRevokedError` so the chat handlers can show a clean message instead
of a stack trace.
"""
from __future__ import annotations

import atexit
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import httpx

from settings import settings, value_denied
from logger_utils import log_with_sid
from exec_transport import log_safe_text


class BrainError(RuntimeError):
    pass


class TenantRevokedError(BrainError):
    """Raised when the brain responds with 401/403 — token revoked/suspended."""


class BrainTimeoutError(BrainError):
    """Raised when a call exceeds its timeout, so callers can name the stalled
    dependency instead of showing a generic "cannot reach brain"."""


_CLIENT: Optional[httpx.Client] = None
_CLIENT_LOCK = threading.Lock()


def _get_client() -> httpx.Client:
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = httpx.Client(
                    base_url=settings.BRAIN_URL.rstrip("/"),
                    timeout=settings.BRAIN_REQUEST_TIMEOUT,
                    # 20, not 10: an Auto Analytics job keeps 4 workers'
                    # plan/retry/describe calls in flight; with interactive
                    # chat on top, a 10-connection pool produced PoolTimeouts
                    # that surfaced as "service unavailable" (QA 2.3).
                    limits=httpx.Limits(max_connections=20),
                )
    return _CLIENT


def close_client():
    global _CLIENT
    with _CLIENT_LOCK:
        if _CLIENT is not None:
            try:
                _CLIENT.close()
            except Exception:
                pass
            finally:
                _CLIENT = None


import atexit
atexit.register(close_client)


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.BRAIN_TENANT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }


# On a failed brain call we ALWAYS surface the HTTP status + body snippet so it
# is never swallowed (this is independent of CLIENT_LLM_DEBUG).
_ERROR_BODY_SNIPPET_CHARS = 2000
# The mail relays: their error body is never logged (only the status).
_MAIL_PATHS = frozenset({"/v1/send_password_reset_email", "/v1/send_welcome_email"})


def _trunc(value: Any, limit: int) -> str:
    """Stringify and truncate a value for debug logging."""
    try:
        s = value if isinstance(value, str) else str(value)
    except Exception:
        return "<unprintable>"
    return s if len(s) <= limit else s[:limit] + f"…(+{len(s) - limit} chars)"


def _debug_log_request(path: str, payload: dict[str, Any], sid: str) -> None:
    """Log the outgoing brain request (CLIENT_LLM_DEBUG only). Metadata only —
    these payload fields carry no raw row values by design (Art. II)."""
    n = settings.CLIENT_LLM_DEBUG_MAX_CHARS
    log_with_sid(
        sid, "info", "BRAIN_REQUEST",
        endpoint=path,
        question=_trunc(payload.get("question"), n) if payload.get("question") is not None else None,
        df_names=_trunc(payload.get("df_names"), n) if payload.get("df_names") is not None else None,
        schema_text=_trunc(payload.get("schema_text"), n) if payload.get("schema_text") is not None else None,
        history_n=len(payload.get("history_rows") or []) if payload.get("history_rows") is not None else None,
        # A count only — chat titles never reach a log line.
        existing_names=len(payload.get("existing_names") or []) if payload.get("existing_names") is not None else None,
        error_msg=_trunc(payload.get("error_msg"), n) if payload.get("error_msg") is not None else None,
        failed_code=_trunc(payload.get("failed_code"), n) if payload.get("failed_code") is not None else None,
    )


def _debug_log_response(path: str, data: dict, sid: str) -> None:
    """Log the parsed brain response (CLIENT_LLM_DEBUG only)."""
    n = settings.CLIENT_LLM_DEBUG_MAX_CHARS
    usage = data.get("usage") or {}
    log_with_sid(
        sid, "info", "BRAIN_RESPONSE",
        endpoint=path,
        kind=data.get("kind"),
        code=_trunc(data.get("code"), n) if data.get("code") is not None else None,
        text=_trunc(data.get("text"), n) if data.get("text") is not None else None,
        finish_reason=usage.get("finish_reason") if isinstance(usage, dict) else None,
        usage=_trunc(usage, n) if usage else None,
    )


def _post(path: str, payload: dict[str, Any], sid: str,
          timeout: float | None = None) -> dict:
    """POST to the brain and return the parsed JSON body.

    `timeout` overrides the client-wide BRAIN_REQUEST_TIMEOUT for calls made
    behind an interactive click, where waiting minutes is worse than failing.
    """
    if not settings.BRAIN_TENANT_TOKEN:
        raise BrainError("BRAIN_TENANT_TOKEN is not configured")
    client = _get_client()
    if settings.CLIENT_LLM_DEBUG:
        _debug_log_request(path, payload, sid)
    t0 = time.monotonic()
    try:
        resp = client.post(
            path, json=payload, headers=_headers(),
            timeout=httpx.USE_CLIENT_DEFAULT if timeout is None else timeout)
    # Before RequestError — TimeoutException is a subclass of it.
    except httpx.TimeoutException as e:
        log_with_sid(sid, "error", f"BRAIN_TIMEOUT {path}: {type(e).__name__}")
        raise BrainTimeoutError(f"Brain timed out: {type(e).__name__}") from e
    except httpx.RequestError as e:
        # Transport error — never swallowed.
        log_with_sid(sid, "error", f"BRAIN_NETWORK_ERROR {path}: {e}")
        raise BrainError(f"Cannot reach brain: {e}") from e

    elapsed = time.monotonic() - t0
    if resp.status_code >= 400:
        # The body is the brain's text, escaped before it reaches the
        # newline-delimited log; a mail call's body may echo the address or
        # the link, so for those only the status is logged.
        if path in _MAIL_PATHS:
            detail = ""
        else:
            detail = ": " + log_safe_text(resp.text or "", _ERROR_BODY_SNIPPET_CHARS)
    if resp.status_code in (401, 403):
        log_with_sid(sid, "error", f"BRAIN_AUTH {resp.status_code} {path}{detail}")
        raise TenantRevokedError(f"Tenant {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 500:
        log_with_sid(sid, "error", f"BRAIN_5XX {resp.status_code} {path}{detail}")
        raise BrainError(f"Brain error {resp.status_code}")
    if resp.status_code >= 400:
        log_with_sid(sid, "warning", f"BRAIN_4XX {resp.status_code} {path}{detail}")
        raise BrainError(f"Brain rejected request {resp.status_code}: {resp.text[:200]}")

    log_with_sid(sid, "info", f"BRAIN_OK {path}", elapsed_s=f"{elapsed:.2f}")
    try:
        data = resp.json()
    except Exception:
        return {}
    if settings.CLIENT_LLM_DEBUG:
        _debug_log_response(path, data if isinstance(data, dict) else {}, sid)
    return data


# ---------------------------------------------------------------------------
# Wrappers matching the brain's /v1/* endpoints.
# Input shapes are the same as the brain's documented shapes.
# ---------------------------------------------------------------------------
def _sanitize_history_rows(history_rows: list | None) -> list[dict]:
    """Strip conversation-history records down to role/content(+code) — the
    only fields the brain reads. Persisted records also carry image_base64,
    chart_data, table rows, usage, full_table_key, ts — raw data values that
    must never cross the boundary (Article II). Rows lacking role or content
    are skipped; order is preserved."""
    out: list[dict] = []
    for row in history_rows or []:
        try:
            if not isinstance(row, dict):
                continue
            role = row.get("role")
            content = row.get("content")
            if not role or content is None:
                continue
            slim = {"role": str(role), "content": str(content)}
            code = row.get("code")
            if code:
                slim["code"] = str(code)
            out.append(slim)
        except Exception as e:
            log_with_sid("history-sanitize", "warning", f"HISTORY_ROW_SKIPPED: {e}")
            continue
    return out


_PROFILE_TRANSPORT_MAX_CHARS = 150_000


_PREVIEW_STR_MAX = 500
_PREVIEW_DICT_MAX_KEYS = 20
_TITLE_ANSWER_MAX = 300
_TRUNCATED_MARK = "…[truncated]"


def _compact_preview_for_transport(preview):
    """The boundary guard for `preview` on /v1/summarize.

    `run_chat_local._safe_preview` already drops everything but scalars and a
    flat dict of scalars; this caps what is left: a string at 500 characters,
    a dict at its first 20 keys (the rest counted in `_truncated_keys`), each
    string value at 500 characters, each marked when cut. Never raises."""
    try:
        if isinstance(preview, str):
            if len(preview) <= _PREVIEW_STR_MAX:
                return preview
            return preview[:_PREVIEW_STR_MAX] + _TRUNCATED_MARK
        if isinstance(preview, dict):
            items = list(preview.items())
            out = {}
            for key, value in items[:_PREVIEW_DICT_MAX_KEYS]:
                if isinstance(key, str) and len(key) > _PREVIEW_STR_MAX:
                    key = key[:_PREVIEW_STR_MAX] + _TRUNCATED_MARK
                if isinstance(value, str) and len(value) > _PREVIEW_STR_MAX:
                    value = value[:_PREVIEW_STR_MAX] + _TRUNCATED_MARK
                out[key] = value
            if len(items) > _PREVIEW_DICT_MAX_KEYS:
                out["_truncated_keys"] = len(items) - _PREVIEW_DICT_MAX_KEYS
            return out
        return preview
    except Exception as e:
        log_with_sid("preview-transport", "warning",
                     f"PREVIEW_COMPACT_FAILED {log_safe_text(type(e).__name__, 80)}")
        return None


_PROFILE_VALUE_FREE_KEYS = frozenset({"dtype", "nunique", "null_count", "null_pct",
                                      "constant", "all_unique"})
_CONSTANT_WARNING_RE = re.compile(r"^(?P<col>.*?) is constant: every value = .*$", re.DOTALL)


def _safe_profile_warning(text: str) -> str:
    """A stored profile warning with the constant value removed. B7.6
    (decision F1): EVERY constant-column warning crosses in the value-free
    form `<col> is constant`, not only a deny-listed column's; the stored
    sidecar keeps the full wording (`_CONSTANT_WARNING_RE` parses it)."""
    m = _CONSTANT_WARNING_RE.match(text)
    if m:
        return f"{m.group('col')} is constant"
    return text


# B7.6: keys that carry a cell value of the column; never on the transport.
_PROFILE_VALUE_KEYS = ("min", "max", "top_values")


def _compact_profiles_for_transport(profiles: dict | None) -> dict | None:
    """Boundary guard for the dataset_profile payload field (Article II class:
    aggregate metadata only — counts, rates, flags; never a cell value).
    Strips the storage-only "src" stamp, drops `min` / `max` / `top_values`
    from EVERY column and rewrites every constant-column warning to
    `<col> is constant` (B7.6, decision F1 — the stored sidecar keeps them),
    enforces the caps, and degrades deterministically when oversized: the
    per-column block is dropped entirely (table-level + grain + warnings
    stay). The input is never mutated."""
    if not profiles:
        return None
    try:
        out: dict = {}
        for key, prof in profiles.items():
            if not isinstance(prof, dict):
                continue
            p = dict(prof)
            p.pop("src", None)
            if isinstance(p.get("warnings"), list):
                p["warnings"] = [_safe_profile_warning(str(w)[:200]) for w in p["warnings"][:6]]
            cols = p.get("columns")
            if isinstance(cols, dict):
                slim_cols = {}
                for cname, cprof in cols.items():
                    if not isinstance(cprof, dict):
                        continue
                    c = dict(cprof)
                    if value_denied(cname):
                        # SCHEMA_VALUE_DENY_COLUMNS: counts and flags only.
                        c = {k: v for k, v in c.items() if k in _PROFILE_VALUE_FREE_KEYS}
                    for value_key in _PROFILE_VALUE_KEYS:
                        c.pop(value_key, None)
                    slim_cols[str(cname)] = c
                p["columns"] = slim_cols
            out[str(key)] = p
        if len(json.dumps(out, ensure_ascii=False, default=str)) > _PROFILE_TRANSPORT_MAX_CHARS:
            for p in out.values():
                p.pop("columns", None)
        return out or None
    except Exception as e:
        log_with_sid("profile-transport", "warning",
                     f"PROFILE_COMPACT_FAILED {log_safe_text(type(e).__name__, 80)}")
        return None


_CAVEAT_KINDS = ("constant_metric", "identical_series", "constant_table",
                 # Prompt 15: not a flat result — a readable-encoding finding
                 # (a count matrix that should have been a grouped bar).
                 "matrix_readability")


# B7.6 belt and braces: a fact ending in a constant-value hint "(= …)".
_VALUE_HINT_RE = re.compile(r"\(\s*=\s*[^)]*\)\s*$")

# FINDINGS on the wire (B7.1 shape rules; the semantic check is
# narrative.validate_findings): a lowercase key, a bool or a short string
# that cannot break the history marker grammar.
_FINDING_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_FINDING_STR_BAD_RE = re.compile("[\x00-\x1f\x7f;\\]\u2028\u2029]")
_FINDINGS_MAX_KEYS = 24
_FINDING_STR_MAX = 64


def _compact_findings_for_transport(findings) -> dict | None:
    """Shape guard for FINDINGS sent on /v1/plan and inside `data_caveat`:
    key `^[a-z][a-z0-9_]{0,39}$`, value a bool or a 1–64 char str without C0
    controls, ";" or "]". At most 24 keys survive, chosen with the B7.2 merge
    priority (`needs_phase2`, then `pdc_diag` registry keys, then the rest,
    each in the given order). Anything else is dropped; nothing left (or not
    a dict) -> None. Never raises."""
    if not isinstance(findings, dict) or not findings:
        return None
    try:
        shaped: dict = {}
        for key, value in findings.items():
            if not isinstance(key, str) or not _FINDING_KEY_RE.match(key):
                continue
            if isinstance(value, bool):
                shaped[key] = value
            elif (isinstance(value, str) and 1 <= len(value) <= _FINDING_STR_MAX
                  and not _FINDING_STR_BAD_RE.search(value)):
                shaped[key] = value
        if len(shaped) <= _FINDINGS_MAX_KEYS:
            return shaped or None
        try:
            # Lazy: the registry lives in the sandbox leaf module; a failed
            # import only loses the priority, never the guard.
            from pdc_diag import PDC_DIAG_FINDING_KEYS as registry
        except Exception:
            registry = frozenset()
        order = ([k for k in shaped if k == "needs_phase2"]
                 + [k for k in shaped if k != "needs_phase2" and k in registry]
                 + [k for k in shaped if k != "needs_phase2" and k not in registry])
        keep = set(order[:_FINDINGS_MAX_KEYS])
        return {k: v for k, v in shaped.items() if k in keep}
    except Exception as e:
        log_with_sid("findings-transport", "warning",
                     f"FINDINGS_COMPACT_FAILED {log_safe_text(type(e).__name__, 80)}")
        return None


def _compact_caveat_for_transport(caveat: dict | None) -> dict | None:
    """Boundary guard for the describe `data_caveat` field. Same Article II
    class as the profiles: aggregate findings and column NAMES — never rows,
    never a cell value (B7.6: a fact ending in a "(= …)" value hint is
    dropped). `kind`, `facts`, `grain` and `catalog` are copied only for a
    known kind; an unknown or absent kind drops only those keys, never
    `findings` (B7.4), which are re-validated for shape. Returns the caveat
    when it holds a kind with facts or grain, or findings; otherwise None
    (describe runs unchanged)."""
    if not caveat or not isinstance(caveat, dict):
        return None
    try:
        out: dict = {}
        kind = caveat.get("kind")
        if kind in _CAVEAT_KINDS:
            body: dict = {}
            for key in ("facts", "grain"):
                items = caveat.get(key)
                if isinstance(items, list):
                    vals = [str(x)[:200] for x in items[:6] if str(x).strip()]
                    if key == "facts":
                        vals = [v for v in vals if not _VALUE_HINT_RE.search(v)]
                    if vals:
                        body[key] = vals
            if body:
                out["kind"] = kind
                out.update(body)
                if caveat.get("catalog"):
                    out["catalog"] = True
        findings = _compact_findings_for_transport(caveat.get("findings"))
        if findings:
            out["findings"] = findings
        return out or None
    except Exception as e:
        log_with_sid("caveat-transport", "warning",
                     f"CAVEAT_COMPACT_FAILED {log_safe_text(type(e).__name__, 80)}")
        return None


# B7.7: a quoted literal in an exception text. The opening quote must not
# follow a letter or digit (so the apostrophe of "can't" never opens one) and
# the closing single quote must not precede one (so "O'Brien" stays one
# literal); a literal never spans lines.
# A Python string prefix (b'…', u'…', rb'…' — a repr of bytes, say) right
# before a quote: dropped first, or the letter would hide the literal from the
# opening-quote rule above and its content would leak.
_STRING_PREFIX_RE = re.compile(r"(?<![A-Za-z0-9_])[bBuUrRfF]{1,2}(?=['\"])")
_SINGLE_QUOTED_RE = re.compile(r"(?<![A-Za-z0-9])'([^\n]*?)'(?![A-Za-z0-9])")
_DOUBLE_QUOTED_RE = re.compile(r'(?<![A-Za-z0-9])"([^"\n]*)"')
_SCRUBBED_LITERAL = "'<value>'"
# A string literal of the FAILED CODE (single or double quoted, one line,
# backslash escapes skipped) — its content was written by the brain.
_CODE_STRING_RE = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"")


def _code_terms(code) -> tuple:
    """(the code text with its string literals blanked out, the contents of
    its string literals) — what the brain itself wrote into `failed_code`.
    ("", ()) for anything that is not a non-empty str. Never raises."""
    try:
        if not isinstance(code, str) or not code:
            return "", ()
        lits = []

        def _blank(m):
            lits.append(m.group(1) if m.group(1) is not None else m.group(2))
            return " "

        bare = _CODE_STRING_RE.sub(_blank, code)
        return bare, tuple(lits)
    except Exception:
        return "", ()


def _written_in_code(literal: str, code: str, code_strings: tuple) -> bool:
    """True when `literal` was written by the brain in the failed code: as a
    whole identifier TOKEN of the code outside its strings (dotted names too:
    `No module named 'a.b'`), or as one WHOLE string literal of the code
    (exact equality — a part of a code string is never kept, or `'North'`
    would be confirmed by code that only says `'North East'`). Such a literal
    adds no information the brain does not already hold. Never raises."""
    try:
        if not literal:
            return False
        if code and all(part.isidentifier() for part in literal.split(".")):
            if re.search(r"(?<!\w)" + re.escape(literal) + r"(?!\w)", code):
                return True
        return literal in code_strings
    except Exception:
        return False


def _scrub_error_msg(text, keep, code=None) -> str:
    """The retry `error_msg` with every quoted literal replaced by '<value>'
    unless its content is a name in `keep` (the input column names and the df
    names, so `KeyError: 'Department'` stays useful for self-correction) or
    was written by the brain itself in `code` (the failed code: a whole
    identifier token — `NameError: name 'df'`, a misspelt variable, a module
    or attribute name — or EXACTLY one whole string literal of it, e.g. the
    `'North'` of `df.loc['North']`, but not of `df.loc['North East']`). A
    literal the code does not contain that way — a cell or category value —
    is still scrubbed.
    Exception texts quote the value they choked on — a cell value of the
    customer's data (Article II, B7.7). Single-quoted literals are scrubbed
    first; a double-quoted literal that held one (pandas' `KeyError: "['x']
    not in index"`) keeps its already-scrubbed content, any other is replaced
    whole. A literal is replaced whatever its length. Never raises: None reads
    as "", any other non-str as its type name, an internal failure answers
    the exception type only."""
    try:
        if text is None:
            return ""
        if not isinstance(text, str):
            return type(text).__name__
        names = frozenset(str(k) for k in (keep or ()))
        code_text, code_strings = _code_terms(code)

        def _kept(lit: str) -> bool:
            return lit in names or _written_in_code(lit, code_text, code_strings)

        def _single(m):
            return m.group(0) if _kept(m.group(1)) else _SCRUBBED_LITERAL

        def _double(m):
            inner = m.group(1)
            if _kept(inner):
                return m.group(0)
            if _SINGLE_QUOTED_RE.search(inner):
                return m.group(0)        # its single-quoted literals are already scrubbed
            return _SCRUBBED_LITERAL

        out = _STRING_PREFIX_RE.sub("", text)
        out = _SINGLE_QUOTED_RE.sub(_single, out)
        return _DOUBLE_QUOTED_RE.sub(_double, out)
    except Exception as e:
        return type(e).__name__


# A copy of the canonical `db_connector.ERROR_CLASSES` (kept here so this
# module stays free of the connector import); the two must stay identical.
SQL_ERROR_CLASSES = ("syntax", "unknown_column", "unknown_table", "timeout",
                     "permission", "guard", "other")


def live_sql_error(table: str, dialect: str, error_class: str, *,
                   guard: bool = False, message: str | None = None) -> dict:
    """The `sql_error` shape a failed live-table SELECT hands the retry flow:
    exactly {table, dialect, class, guard, message}. `class` is one of
    `SQL_ERROR_CLASSES` (anything else reads as `other`); `guard` is True
    when this client's SQL gate refused the text and `message` is then the
    gate's own sentence (an identifier at most), otherwise None. The driver's
    message — which can quote literals and cell values — never goes in here
    (Article II)."""
    cls = error_class if error_class in SQL_ERROR_CLASSES else "other"
    return {"table": str(table), "dialect": str(dialect or ""), "class": cls,
            "guard": bool(guard),
            "message": str(message) if (guard and message) else None}


def plan(sid: str, question: str, schema_text: str, df_names: list[str],
         history_rows: list, common_fields: list | None = None,
         user_email: str | None = None,
         dataset_profile: dict | None = None,
         live_tables: list | None = None,
         capabilities: list | None = None,
         findings: dict | None = None) -> dict:
    payload = {
        "sid": sid,
        "question": question,
        "schema_text": schema_text,
        "df_names": df_names,
        "history_rows": _sanitize_history_rows(history_rows),
        "common_fields": common_fields or [],
        "user_email": user_email,
        "dataset_profile": _compact_profiles_for_transport(dataset_profile),
    }
    # Sent ONLY when the chat holds a live table — a chat without one posts
    # exactly the payload it always did.
    if live_tables:
        payload["live_tables"] = live_tables
    # Narrative session 1: both ride only when given, so a call without them
    # (Auto Analytics, the kill switch) posts exactly the old payload. The
    # caller passes `settings.brain_capabilities()`; this module never reads
    # the setting itself.
    if capabilities:
        payload["capabilities"] = list(capabilities)
    compact_findings = _compact_findings_for_transport(findings) if findings else None
    if compact_findings:
        payload["findings"] = compact_findings
    return _post("/v1/plan", payload, sid)


def retry(sid: str, question: str, schema_text: str, df_names: list[str],
          df_columns: dict[str, list[str]], history_rows: list,
          error_msg: str, failed_code: str,
          use_pro: bool = False, use_search: bool = False,
          user_email: str | None = None,
          dataset_profile: dict | None = None,
          sql: dict | None = None, sql_error: dict | None = None,
          live_tables: list | None = None,
          capabilities: list | None = None,
          timeout: float | None = None) -> dict:
    # `timeout` overrides the client-wide BRAIN_REQUEST_TIMEOUT (the
    # narrative-only retry's time-box); a transport setting, never a payload
    # field. None -> the client default, as before.
    # B7.7: exception texts quote the value they failed on — scrub every
    # quoted literal that is not an input column or df name.
    keep = {str(n) for n in (df_names or [])}
    for cols in (df_columns or {}).values():
        keep.update(str(c) for c in (cols or []))
    payload = {
        "sid": sid,
        "question": question,
        "schema_text": schema_text,
        "df_names": df_names,
        "df_columns": df_columns,
        "history_rows": _sanitize_history_rows(history_rows),
        "error_msg": _scrub_error_msg(error_msg, frozenset(keep), failed_code),
        "failed_code": failed_code,
        "use_pro": use_pro,
        "use_search": use_search,
        "user_email": user_email,
        "dataset_profile": _compact_profiles_for_transport(dataset_profile),
    }
    # The live-table fields ride only when a live fetch was involved: `sql`
    # is the map of what RAN this turn, per fetched key — the SELECT text
    # when it reached the database (also when the database failed it), null
    # for a default read, a SELECT ignored on a filtered table or a SELECT
    # the guard refused; keys never fetched are absent. `sql_error` is the
    # value-free `live_sql_error` shape of the failure (or of the ignored
    # SELECT's refusal).
    if sql:
        payload["sql"] = sql
    if sql_error:
        payload["sql_error"] = sql_error
    if live_tables:
        payload["live_tables"] = live_tables
    if capabilities:
        payload["capabilities"] = list(capabilities)
    if timeout is not None:
        return _post("/v1/retry", payload, sid, timeout=timeout)
    # no override: the exact pre-time-box call (client-wide default)
    return _post("/v1/retry", payload, sid)


def describe(sid: str, question: str, code: str, user_email: str | None = None,
             data_caveat: dict | None = None) -> dict:
    body = {"sid": sid, "question": question, "code": code, "user_email": user_email}
    caveat = _compact_caveat_for_transport(data_caveat)
    if caveat:
        # Only sent when the client's deterministic inspection fired or the
        # turn carries FINDINGS (B7.4) — an absent field leaves the brain's
        # describe prompt byte-identical.
        body["data_caveat"] = caveat
    return _post("/v1/describe", body, sid)


def greeting(sid: str, question: str, df_names: list[str], user_email: str | None = None) -> dict:
    return _post("/v1/greeting", {
        "sid": sid, "question": question, "df_names": df_names, "user_email": user_email,
    }, sid)


def summarize(sid: str, question: str, schema_text: str, history_rows: list,
              preview, context_decision: dict | None = None,
              user_email: str | None = None) -> dict:
    return _post("/v1/summarize", {
        "sid": sid,
        "question": question,
        "schema_text": schema_text,
        "history_rows": _sanitize_history_rows(history_rows),
        "preview": _compact_preview_for_transport(preview),
        "context_decision": context_decision or {},
        "user_email": user_email,
    }, sid)


def report(sid: str, qa_pairs: list, user_email: str | None = None,
           slot_budgets: dict | None = None) -> dict:
    return _post("/v1/report", {
        "sid": sid, "qa_pairs": qa_pairs, "user_email": user_email,
        "slot_budgets": slot_budgets or {},
    }, sid)


def chat_metadata(sid: str, files_info: list, file_descriptions: dict,
                  context: str, lang_instruction: str,
                  columns_to_human: dict | None = None,
                  user_email: str | None = None,
                  existing_names: list | None = None) -> dict:
    """Calls /v1/chat_metadata. Returns {name, welcome_message, suggested_questions}.

    `existing_names` — the user's own chat titles, newest first (titles
    only, no data values), so the brain can name the new chat differently.
    The key is sent ONLY when given: a call without it posts the same
    payload as before."""
    payload = {
        "sid": sid,
        "files_info": files_info,
        "file_descriptions": file_descriptions,
        "context": context,
        "lang_instruction": lang_instruction,
        "columns_to_human": columns_to_human or {},
        "user_email": user_email,
    }
    if existing_names is not None:
        payload["existing_names"] = existing_names
    return _post("/v1/chat_metadata", payload, sid)


def file_description(sid: str, extracted_text: str, user_email: str | None = None) -> dict:
    """Calls /v1/file_description. Returns {description}."""
    return _post("/v1/file_description", {
        "sid": sid,
        "extracted_text": extracted_text,
        "user_email": user_email,
    }, sid)


def schema_autofill(
    sid: str,
    fname: str,
    cols_to_fill: list,
    unique_hints: dict,
    dtypes: dict | None = None,
    file_desc: str = "",
    notes_text: str = "",
    lang_name: str = "English",
    desc_word_limit: int = 20,
    user_email: str | None = None,
    timeout: float | None = None,
) -> dict:
    """Combined autofill (file + per-column descriptions). One LLM call per file
    on the brain. Verbatim port of global's `_build_combined_autofill_prompt`.

    Returns {file_description: str, columns: {col: desc}}. The client is
    responsible for building `unique_hints` (sampled / truncated values) and
    `cols_to_fill` BEFORE this call so no raw row data crosses the boundary
    beyond what global itself sends for this same prompt.
    """
    return _post("/v1/schema_autofill", {
        "sid": sid,
        "fname": fname,
        "cols_to_fill": cols_to_fill,
        "unique_hints": unique_hints,
        "dtypes": dtypes or {},
        "file_desc": file_desc,
        "notes_text": notes_text,
        "lang_name": lang_name,
        "desc_word_limit": desc_word_limit,
        "user_email": user_email,
    }, sid, timeout=timeout)


def health() -> bool:
    try:
        client = _get_client()
        r = client.get("/health", timeout=5.0)
        return r.status_code == 200
    except Exception:
        return False


# Activity posts run on a single background worker: several call sites sit in
# async handlers (login, chat SSE, report, upload), and a synchronous brain
# call there blocks uvicorn's whole event loop — a slow /v1/activity response
# froze the entire platform for its duration. One worker keeps events ordered;
# telemetry may lag, requests never do.
_ACTIVITY_EXEC = ThreadPoolExecutor(max_workers=1, thread_name_prefix="activity")
atexit.register(lambda: _ACTIVITY_EXEC.shutdown(wait=False, cancel_futures=True))


def _post_activity_sync(event: str, user_email: str, metadata: dict | None) -> None:
    try:
        _post("/v1/activity", {
            "event": event,
            "user_email": user_email,
            "metadata": metadata or {},
        }, sid=f"activity:{event}")
    except Exception as e:
        # Activity logging must never break the user flow
        log_with_sid(user_email or "—", "warning", f"ACTIVITY_POST_FAILED event={event}: {e}")


def post_activity(event: str, user_email: str, metadata: dict | None = None) -> dict:
    """Centralized activity log (fire-and-forget — see _ACTIVITY_EXEC above).
    Brain stores in tenants/{id}/activity.jsonl."""
    try:
        _ACTIVITY_EXEC.submit(_post_activity_sync, event, user_email, metadata)
    except Exception as e:   # interpreter shutdown — nothing to do
        log_with_sid(user_email or "—", "warning", f"ACTIVITY_QUEUE_FAILED event={event}: {e}")
        return {"ok": False}
    return {"ok": True, "queued": True}


def get_app_settings() -> dict:
    """Fetch per-tenant client app settings (MAX_FILES, TITLE_*). Falls back to
    an empty dict on error so caller uses local defaults."""
    if not settings.BRAIN_TENANT_TOKEN:
        return {}
    try:
        client = _get_client()
        r = client.get("/v1/app_settings", headers=_headers(), timeout=5.0)
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception:
        return {}


def get_pptx_template_spec() -> dict:
    """Fetch the per-tenant .pptx style-spec from the brain.

    Returns the parsed body — `{has_template: bool, spec: {...}|null}` — or an
    empty dict on any error (caller falls back to the built-in renderer).
    """
    if not settings.BRAIN_TENANT_TOKEN:
        return {}
    try:
        client = _get_client()
        r = client.get("/v1/pptx_template_spec", headers=_headers(), timeout=10.0)
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception:
        return {}


def get_pptx_layout_plan() -> dict:
    """Fetch the per-tenant version-3 layout plan from the brain.

    Returns `{has_plan: bool, plan: {...}|null}` — or an empty dict on any
    error (caller falls back to the built-in renderer).
    """
    if not settings.BRAIN_TENANT_TOKEN:
        return {}
    try:
        client = _get_client()
        r = client.get("/v1/pptx_layout_plan", headers=_headers(), timeout=10.0)
        if r.status_code != 200:
            return {}
        return r.json() or {}
    except Exception:
        return {}


def download_pptx_template() -> bytes | None:
    """Fetch the tenant's uploaded .pptx template bytes from the brain, or
    None if there is no template (404) or any other error."""
    if not settings.BRAIN_TENANT_TOKEN:
        return None
    try:
        client = _get_client()
        r = client.get("/v1/pptx_template", headers=_headers(), timeout=30.0)
        if r.status_code != 200:
            return None
        return r.content or None
    except Exception:
        return None


def title(sid: str, question: str, answer: str, lang: str = "English",
          user_email: str | None = None) -> dict:
    """Calls /v1/title. Returns {title}."""
    return _post("/v1/title", {
        # The first 300 characters are enough to title a conversation.
        "sid": sid, "question": question, "answer": (answer or "")[:_TITLE_ANSWER_MAX],
        "lang": lang, "user_email": user_email,
    }, sid)


def send_share_email(to: list[str], subject: str, sender_email: str,
                     chat_title: str = "", message: str = "") -> dict:
    """Calls /v1/send_share_email. Brain SMTP-relays the invite using this
    tenant's SMTP config. Returns {ok, sent, failed, smtp_configured}."""
    return _post("/v1/send_share_email", {
        "to": to, "subject": subject, "sender_email": sender_email,
        "chat_title": chat_title, "message": message,
    }, sid=f"share-email:{len(to)}")


def send_welcome_email(email: str) -> dict:
    """Calls /v1/send_welcome_email — brain Gmail-relays the fixed welcome
    mail to a first-time user. Callers treat this as fire-and-forget (a
    failure must never block login); raises BrainError on non-2xx like every
    other wrapper, so wrap in try/except at the call site."""
    return _post("/v1/send_welcome_email", {
        "sid": "welcome-email", "email": email,
    }, sid="welcome-email")


def send_password_reset_email(email: str, reset_url: str,
                              timeout: float | None = None,
                              kind: str = "reset") -> dict:
    """Calls /v1/send_password_reset_email — brain Gmail-relays the reset
    link (valid 30 minutes, single use) to the user. The link carries a
    bearer token: it is never logged on either side, and the client stores
    only its sha256. `timeout` overrides the client-wide default (the admin
    invite waits for the answer; the anonymous reset runs in the background).
    `kind` is always posted and chooses the mail's wording: "reset" (the
    anonymous reset request) or "invite" (the admin invite).
    Raises BrainError on non-2xx like every other wrapper."""
    return _post("/v1/send_password_reset_email", {
        "sid": "reset-email", "email": email, "reset_url": reset_url,
        "kind": kind,
    }, sid="reset-email", timeout=timeout)


def auto_analytics_plan(sid: str, schema_text: str, df_names: list[str],
                        common_fields: list, user_email: str | None = None) -> dict:
    """Auto Analytics planner. Returns {instructions: [...]}.

    NO row data is sent — only the schema_text the planner reads.
    """
    return _post("/v1/auto_analytics_plan", {
        "sid": sid, "schema_text": schema_text,
        "df_names": df_names, "common_fields": common_fields,
        "user_email": user_email,
    }, sid)
