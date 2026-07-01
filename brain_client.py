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

import threading
import time
from typing import Any, Optional

import httpx

from settings import settings
from logger_utils import log_with_sid


class BrainError(RuntimeError):
    pass


class TenantRevokedError(BrainError):
    """Raised when the brain responds with 401/403 — token revoked/suspended."""


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


def _post(path: str, payload: dict[str, Any], sid: str) -> dict:
    """POST to the brain and return the parsed JSON body."""
    if not settings.BRAIN_TENANT_TOKEN:
        raise BrainError("BRAIN_TENANT_TOKEN is not configured")
    client = _get_client()
    t0 = time.monotonic()
    try:
        resp = client.post(path, json=payload, headers=_headers())
    except httpx.RequestError as e:
        log_with_sid(sid, "error", f"BRAIN_NETWORK_ERROR {path}: {e}")
        raise BrainError(f"Cannot reach brain: {e}") from e

    elapsed = time.monotonic() - t0
    if resp.status_code in (401, 403):
        log_with_sid(sid, "error", f"BRAIN_AUTH {resp.status_code} {path}: {resp.text[:200]}")
        raise TenantRevokedError(f"Tenant {resp.status_code}: {resp.text[:200]}")
    if resp.status_code >= 500:
        log_with_sid(sid, "error", f"BRAIN_5XX {resp.status_code} {path}: {resp.text[:200]}")
        raise BrainError(f"Brain error {resp.status_code}")
    if resp.status_code >= 400:
        log_with_sid(sid, "warning", f"BRAIN_4XX {resp.status_code} {path}: {resp.text[:200]}")
        raise BrainError(f"Brain rejected request {resp.status_code}: {resp.text[:200]}")

    log_with_sid(sid, "info", f"BRAIN_OK {path}", elapsed_s=f"{elapsed:.2f}")
    try:
        return resp.json()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Wrappers matching the brain's /v1/* endpoints.
# Input shapes are the same as the brain's documented shapes.
# ---------------------------------------------------------------------------
def plan(sid: str, question: str, schema_text: str, df_names: list[str],
         history_rows: list, common_fields: list | None = None,
         user_email: str | None = None) -> dict:
    return _post("/v1/plan", {
        "sid": sid,
        "question": question,
        "schema_text": schema_text,
        "df_names": df_names,
        "history_rows": history_rows,
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
        "history_rows": history_rows,
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
        "history_rows": history_rows,
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
    }, sid)


def health() -> bool:
    try:
        client = _get_client()
        r = client.get("/health", timeout=5.0)
        return r.status_code == 200
    except Exception:
        return False


def post_activity(event: str, user_email: str, metadata: dict | None = None) -> dict:
    """Centralized activity log. Brain stores in tenants/{id}/activity.jsonl."""
    try:
        return _post("/v1/activity", {
            "event": event,
            "user_email": user_email,
            "metadata": metadata or {},
        }, sid=f"activity:{event}")
    except Exception as e:
        # Activity logging must never break the user flow
        log_with_sid(user_email or "—", "warning", f"ACTIVITY_POST_FAILED event={event}: {e}")
        return {"ok": False}


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
