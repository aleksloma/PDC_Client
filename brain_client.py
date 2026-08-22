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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

import httpx

from settings import settings
from logger_utils import log_with_sid


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
                    limits=httpx.Limits(max_connections=10),
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
    if resp.status_code in (401, 403):
        log_with_sid(sid, "error", f"BRAIN_AUTH {resp.status_code} {path}: {resp.text[:_ERROR_BODY_SNIPPET_CHARS]}")
        raise TenantRevokedError(f"Tenant {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 500:
        log_with_sid(sid, "error", f"BRAIN_5XX {resp.status_code} {path}: {resp.text[:_ERROR_BODY_SNIPPET_CHARS]}")
        raise BrainError(f"Brain error {resp.status_code}")
    if resp.status_code >= 400:
        log_with_sid(sid, "warning", f"BRAIN_4XX {resp.status_code} {path}: {resp.text[:_ERROR_BODY_SNIPPET_CHARS]}")
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


def plan(sid: str, question: str, schema_text: str, df_names: list[str],
         history_rows: list, common_fields: list | None = None,
         user_email: str | None = None) -> dict:
    return _post("/v1/plan", {
        "sid": sid,
        "question": question,
        "schema_text": schema_text,
        "df_names": df_names,
        "history_rows": _sanitize_history_rows(history_rows),
        "common_fields": common_fields or [],
        "user_email": user_email,
    }, sid)


def retry(sid: str, question: str, schema_text: str, df_names: list[str],
          df_columns: dict[str, list[str]], history_rows: list,
          error_msg: str, failed_code: str,
          use_pro: bool = False, use_search: bool = False,
          user_email: str | None = None) -> dict:
    return _post("/v1/retry", {
        "sid": sid,
        "question": question,
        "schema_text": schema_text,
        "df_names": df_names,
        "df_columns": df_columns,
        "history_rows": _sanitize_history_rows(history_rows),
        "error_msg": error_msg,
        "failed_code": failed_code,
        "use_pro": use_pro,
        "use_search": use_search,
        "user_email": user_email,
    }, sid)


def describe(sid: str, question: str, code: str, user_email: str | None = None) -> dict:
    return _post("/v1/describe", {
        "sid": sid, "question": question, "code": code, "user_email": user_email,
    }, sid)


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
        "preview": preview,
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
                  user_email: str | None = None) -> dict:
    """Calls /v1/chat_metadata. Returns {name, welcome_message, suggested_questions}."""
    return _post("/v1/chat_metadata", {
        "sid": sid,
        "files_info": files_info,
        "file_descriptions": file_descriptions,
        "context": context,
        "lang_instruction": lang_instruction,
        "columns_to_human": columns_to_human or {},
        "user_email": user_email,
    }, sid)


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
        "sid": sid, "question": question, "answer": answer,
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


def send_password_reset_email(email: str, temp_password: str) -> dict:
    """Calls /v1/send_password_reset_email — brain Gmail-relays the
    client-generated temporary password to the user. The temp password is
    never logged on either side; only its hash is stored (client-side)."""
    return _post("/v1/send_password_reset_email", {
        "sid": "reset-email", "email": email, "temp_password": temp_password,
    }, sid="reset-email")


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
