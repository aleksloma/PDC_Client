"""Client-side auth: email-only landing + the profile + sidebar endpoints.

Per the prompt: "before reaching the lab page, show an authorization/landing
page that asks ONLY for the user's email. ... No password, nothing else on
that page."

The dashboard.html template (copied verbatim from B2C) expects a profile JSON
with `email`, `username`, `subscription_plan` keys. To keep that page working
without re-skinning it, we return `username = email` and `subscription_plan =
"Enterprise"` (constants). Profile updates only persist the email; password
endpoints are no-ops.
"""
from __future__ import annotations

import re
import secrets

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path as _P

from local_store import AuthStore
from logger_utils import log_with_sid
import brain_client

router = APIRouter(tags=["client-auth"])

_TEMPLATES = Jinja2Templates(directory=str(_P(__file__).resolve().parent.parent / "templates"))

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_FIXED_PLAN = "Enterprise"


def _public_profile(email: str) -> dict:
    """Shape the profile in the way dashboard.js expects."""
    return {
        "username": email,
        "email": email,
        "full_name": "",
        "subscription_plan": _FIXED_PLAN,
    }


# --- Auth landing + login ----------------------------------------------------

@router.post("/auth/login")
async def login(request: Request):
    """Email-only login (form-encoded). Creates the local profile on first sight
    and starts a session."""
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    if not _EMAIL_RE.match(email):
        return _TEMPLATES.TemplateResponse(
            "auth_landing.html",
            {"request": request, "error": "Please enter a valid email"},
            status_code=400,
        )
    AuthStore().ensure_user(email)
    request.session["email"] = email
    # Issue a per-session SID for the temp UserStore (the upload flow keys off it)
    if not request.session.get("sid"):
        request.session["sid"] = "s_" + secrets.token_hex(8)
    log_with_sid(email, "info", "USER_LOGIN", sid=request.session.get("sid"))
    # Fire-and-forget activity ping to brain (centralized per-tenant activity log)
    try:
        brain_client.post_activity("login", email)
    except Exception:
        pass
    return RedirectResponse(url="/lab", status_code=302)


@router.post("/auth/logout")
async def logout(request: Request):
    email = request.session.get("email")
    request.session.pop("email", None)
    request.session.pop("sid", None)
    if email:
        log_with_sid(email, "info", "USER_LOGOUT")
    return RedirectResponse(url="/", status_code=302)


@router.get("/auth/me")
async def me(request: Request):
    email = request.session.get("email")
    if not email:
        return JSONResponse({"authenticated": False}, status_code=401)
    return {"authenticated": True, "email": email}


# --- Profile (email-only) ----------------------------------------------------

@router.get("/auth/profile")
async def get_profile(request: Request):
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return _public_profile(email)


@router.post("/auth/profile/update")
async def update_profile(request: Request):
    """Profile updates other than the email itself are silently ignored.
    The email IS the identity in the enterprise build."""
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    new_email = (body.get("email") or "").strip().lower()
    if new_email and new_email != email:
        # Enterprise build does not support changing email mid-session — would
        # require re-logging in. Surface a friendly no-op rather than failing.
        log_with_sid(email, "info", "PROFILE_UPDATE_IGNORED_EMAIL_CHANGE", attempt=new_email)
    AuthStore().update_profile(email)
    return _public_profile(email)


@router.post("/auth/password")
async def password_noop(request: Request):
    """No-op. Enterprise build has no password to change."""
    if not request.session.get("email"):
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return {"ok": True, "message": "Password management is disabled in the enterprise build."}


@router.get("/auth/subscription")
async def subscription_const(request: Request):
    return {"plan": _FIXED_PLAN, "subscription_plan": _FIXED_PLAN}


# --- Sidebar listings + renaming --------------------------------------------

@router.get("/auth/active_chats")
async def active_chats(request: Request):
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    rows = AuthStore().list_active_chats(email)
    # Chat records persist the display name under `title`, but the dashboard JS
    # reads `chat.name` (falling back to "Untitled"). Expose `name` as an alias
    # so the LLM-generated chat name actually renders. (PROBLEM 3 — the title was
    # always generated + persisted; only this field name mismatched.)
    for r in rows:
        if isinstance(r, dict) and "name" not in r:
            r["name"] = r.get("title", "")
    return {"active_chats": rows}


@router.get("/auth/conversations")
async def conversations(request: Request):
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    return {"conversations": AuthStore().list_conversations(email)}


@router.post("/auth/active_chats/rename")
async def rename_chat(request: Request):
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    chat_id = body.get("chat_id")
    new_title = (body.get("title") or "").strip()
    if not chat_id or not new_title:
        return JSONResponse({"error": "Missing chat_id or title"}, status_code=400)
    ok = AuthStore().rename_active_chat(email, chat_id, new_title)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/auth/conversations/rename")
async def rename_conv(request: Request):
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    conv_id = body.get("conv_id")
    new_title = (body.get("title") or "").strip()
    if not conv_id or not new_title:
        return JSONResponse({"error": "Missing conv_id or title"}, status_code=400)
    ok = AuthStore().rename_conversation(email, conv_id, new_title)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)


@router.post("/auth/conversations/{conv_id}/share")
async def share_conversation(request: Request, conv_id: str):
    """Conversation-level share — snapshot-clone variant (matches global).

    For each recipient:
      1. Snapshot the conversation history into a fresh `conv_id` under the
         owner's `ChatDataStore` via `copy_conv_to_new` (so subsequent edits
         on either side stay independent).
      2. Add the recipient to the chat's `meta["sharing"]["shared_with"]` so
         they can access the chat.
      3. Record the new conv_id in the recipient's `conversations.jsonl`
         (title prefixed with "(Shared)"), and add the chat to their
         `active_chats.jsonl`.
      4. Ask the brain to SMTP-relay an invitation email via this tenant's
         SMTP config (best-effort).

    Body: {emails: ["a@x.com", ...], message?: "..."}.
    Returns: {ok, shared_with, snapshot_conv_ids, email_sent, smtp_configured}.
    """
    import local_store as _ls
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}

    raw_emails = body.get("emails") or body.get("allowed_emails") or []
    if isinstance(raw_emails, str):
        raw_emails = [e.strip() for e in raw_emails.replace(",", "\n").splitlines() if e.strip()]
    recipients: list[str] = []
    for e in raw_emails:
        e = (e or "").strip().lower()
        if _EMAIL_RE.match(e) and e != email:
            recipients.append(e)
    if not recipients:
        return JSONResponse({"error": "Provide at least one valid recipient email."}, status_code=400)

    message_text = (body.get("message") or body.get("comment") or "").strip()

    # Find the conv → chat
    convs = AuthStore().list_conversations(email)
    conv = next((c for c in convs if c.get("conv_id") == conv_id), None)
    if not conv:
        return JSONResponse({"error": "Conversation not found"}, status_code=404)
    chat_id = conv.get("chat_id")
    if not chat_id or not _ls.chat_exists(chat_id):
        return JSONResponse({"error": "Invalid conversation"}, status_code=400)

    store = _ls.ChatDataStore(chat_id)
    meta = store.read_meta()
    chat_title = meta.get("title") or "Chat"
    files = [f.get("file_name") for f in meta.get("files", []) if f.get("file_name")]
    conv_title = (conv.get("title") or "").strip() or f"Shared by {email}"

    # Add recipients to chat's sharing list (so /_require_chat lets them in)
    added = store.add_share_recipients(recipients)

    snapshot_conv_ids: dict[str, str] = {}
    for rec in recipients:
        new_conv_id = store.copy_conv_to_new(conv_id)
        snapshot_conv_ids[rec] = new_conv_id
        recipient_title = f"(Shared) {conv_title}" if not conv_title.startswith("(Shared)") else conv_title
        AuthStore().record_conversation(rec, chat_id, new_conv_id, recipient_title, shared_by=email)
        AuthStore().record_shared_chat(rec, chat_id, chat_title, files, shared_by=email)

    smtp_result = {"smtp_configured": False, "sent": [], "failed": []}
    if recipients:
        try:
            smtp_result = brain_client.send_share_email(
                to=recipients,
                subject=f"{email} shared a conversation with you",
                sender_email=email, chat_title=chat_title, message=message_text,
            ) or smtp_result
        except Exception as e:
            log_with_sid(email, "warning", f"CONV_SHARE_EMAIL_ERROR: {e}")

    log_with_sid(email, "info",
                 f"CONV_SHARED chat_id={chat_id} conv_id={conv_id} to={','.join(recipients)}")
    return {
        "ok": True,
        "shared_with": recipients,
        "added": added,
        "snapshot_conv_ids": snapshot_conv_ids,
        "email_sent": bool(smtp_result.get("sent")),
        "smtp_configured": bool(smtp_result.get("smtp_configured")),
        "failed": smtp_result.get("failed") or [],
    }


@router.post("/auth/conversations/delete")
async def delete_conv(request: Request):
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    conv_id = body.get("conv_id")
    if not conv_id:
        return JSONResponse({"error": "Missing conv_id"}, status_code=400)
    ok = AuthStore().delete_conversation(email, conv_id)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 404)
