"""Client-side auth: email+password landing + the profile + sidebar endpoints.

Password model (all local — the brain never sees a password):
  - First-time email (no stored hash — includes every legacy user from the
    email-only build): the entered password BECOMES the password (hash-only
    storage via `AuthStore.set_password`), and the brain Gmail-relays a
    welcome mail (fire-and-forget — login succeeds even if that fails).
  - Wrong password → red "Incorrect password" + a Reset password action.
  - Reset: the client generates a secure temp password, stores its hash with
    `must_change_password`, and asks the brain to email it
    (`/v1/send_password_reset_email`). Logging in with the temp password
    forces a password change before the workspace opens.
  - "Remember me" → persistent session cookie (~30 days) via the
    RememberMeSessionMiddleware in app.py; unchecked → browser-session cookie.

The dashboard.html template (copied verbatim from B2C) expects a profile JSON
with `email`, `username`, `subscription_plan` keys. To keep that page working
without re-skinning it, we return `username = email` and `subscription_plan =
"Enterprise"` (constants). Profile updates only persist the email.
"""
from __future__ import annotations

import re
import secrets
import threading

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


def _local_admin_username() -> str:
    from settings import settings
    return (settings.LOCAL_ADMIN_USERNAME or "").strip().lower()


def _valid_login_id(value: str) -> bool:
    """A real email, or the fixed local-admin username (the ONLY non-email
    identity; every other id still requires a valid email)."""
    return bool(_EMAIL_RE.match(value)) or (value and value == _local_admin_username())


def _public_profile(email: str) -> dict:
    """Shape the profile in the way dashboard.js expects.

    NOTE: the admin flag is `is_local_admin`, deliberately NOT `is_admin` —
    dashboard.js feeds `profile.is_admin` into the B2C Publish context-menu
    items, whose routes return 400 by design on-prem."""
    return {
        "username": email,
        "email": email,
        "full_name": "",
        "subscription_plan": _FIXED_PLAN,
        "is_local_admin": AuthStore().is_admin(email),
    }


def _post_login_target(email: str) -> str:
    """Where a signed-in user lands. The local admin is config-only — it goes
    straight to the Data-sources page, never the /lab chat UI (app.py's /lab
    route mirrors this with a redirect guard)."""
    return "/admin/data_sources" if AuthStore().is_admin(email) else "/lab"


# --- Auth landing + login ----------------------------------------------------

def _landing(request: Request, *, error: str = None, password_error: str = None,
             info: str = None, email: str = "", status_code: int = 200,
             show_reset: bool = False, legacy_reset: bool = False):
    """Render the landing page with optional messages.

    `password_error` renders in red under the password field; `show_reset`
    additionally renders the Reset-password action next to it;
    `legacy_reset` renders the i18n-wrapped "set your password via reset"
    notice for pre-password accounts. `error` is the generic top message.
    """
    return _TEMPLATES.TemplateResponse(
        "auth_landing.html",
        {"request": request, "error": error, "password_error": password_error,
         "info": info, "email": email, "show_reset": show_reset,
         "legacy_reset": legacy_reset},
        status_code=status_code,
    )


def _start_session(request: Request, email: str, *, remember: bool,
                   must_change: bool = False) -> None:
    request.session["email"] = email
    if remember:
        request.session["remember"] = True
    else:
        request.session.pop("remember", None)
    if must_change:
        request.session["must_change_password"] = True
    else:
        request.session.pop("must_change_password", None)
    # Issue a per-session SID for the temp UserStore (the upload flow keys off it)
    if not request.session.get("sid"):
        request.session["sid"] = "s_" + secrets.token_hex(8)
    # Single funnel for BOTH login branches (new-user and returning) — the one
    # place to stamp last_login_at. touch_last_login never raises.
    AuthStore().touch_last_login(email)


def _send_welcome_email_async(email: str) -> None:
    """Fire-and-forget welcome mail via the brain. Never blocks or fails login."""
    def _fire():
        try:
            brain_client.send_welcome_email(email)
            log_with_sid(email, "info", "WELCOME_EMAIL_REQUESTED")
        except Exception as e:
            log_with_sid(email, "warning", f"WELCOME_EMAIL_FAILED: {e}")
    threading.Thread(target=_fire, daemon=True, name="welcome_email").start()


@router.post("/auth/login")
async def login(request: Request):
    """Email+password login (form-encoded: email, password, remember?).

    A genuinely NEW email (no user folder) gets the entered password set as
    theirs + the welcome mail. A LEGACY account (folder from the email-only
    build, no hash) is refused with a "set your password via reset" notice —
    the reset flow proves mailbox ownership. A stored temp password logs in
    but forces a change before /lab opens.
    """
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    remember = bool(form.get("remember"))
    if not _valid_login_id(email):
        return _landing(request, error="Please enter a valid email",
                        email=email, status_code=400)
    if not password:
        return _landing(request, password_error="Please enter a password",
                        email=email, status_code=400)

    store = AuthStore()
    auth = store.get_auth(email)
    if not auth.get("password_hash") and not auth.get("temp_password_hash"):
        if email == _local_admin_username():
            # Bootstrapped account without a password (LOCAL_ADMIN_PASSWORD
            # unset at boot). The reset flow is refused for ladmin (no
            # mailbox), so point at the server-side fix instead.
            log_with_sid(email, "warning", "LADMIN_LOGIN_NO_BOOTSTRAP")
            return _landing(
                request, email=email, status_code=403,
                password_error=("The administrator account has no password yet. "
                                "Set LOCAL_ADMIN_PASSWORD in the server "
                                "environment and restart the container."))
        if store.user_exists(email):
            # LEGACY account from the email-only build (folder exists, no
            # hash): the typed password must NOT silently become theirs —
            # ownership of the mailbox is proven through the reset flow
            # (temp password emailed via the brain + forced change).
            log_with_sid(email, "info", "USER_LOGIN_LEGACY_RESET_REQUIRED")
            return _landing(
                request, email=email, status_code=403,
                password_error=("This account existed before passwords were "
                                "introduced. Please click “Reset password” "
                                "— we will email you a temporary password to "
                                "sign in and set your own."),
                show_reset=True, legacy_reset=True,
            )
        # Genuinely NEW email (no user folder): the entered password becomes
        # this user's password. An outstanding temp password counts as
        # credentials — it goes through verify_password below so the forced
        # change still triggers.
        store.ensure_user(email)
        store.set_password(email, password)
        _start_session(request, email, remember=remember)
        log_with_sid(email, "info", "USER_LOGIN_FIRST_PASSWORD_SET",
                     sid=request.session.get("sid"))
        _send_welcome_email_async(email)
        try:
            brain_client.post_activity("login", email)
        except Exception:
            pass
        return RedirectResponse(url="/lab", status_code=302)

    verdict = store.verify_password(email, password)
    if verdict is None:
        log_with_sid(email, "warning", "USER_LOGIN_BAD_PASSWORD")
        return _landing(request, password_error="Incorrect password",
                        email=email, status_code=401, show_reset=True)

    must_change = (verdict == "temp") or bool(store.get_auth(email).get("must_change_password"))
    _start_session(request, email, remember=remember, must_change=must_change)
    log_with_sid(email, "info", "USER_LOGIN", sid=request.session.get("sid"),
                 must_change=must_change)
    try:
        brain_client.post_activity("login", email)
    except Exception:
        pass
    target = "/auth/change_password" if must_change else _post_login_target(email)
    return RedirectResponse(url=target, status_code=302)


@router.post("/auth/reset_password")
async def reset_password(request: Request):
    """Password reset (form-encoded: email). Generates a temp password
    locally, stores ONLY its hash (+ must_change flag), and asks the brain
    to email it. The temp password never appears in any log."""
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    if email and email == _local_admin_username():
        # ladmin has no mailbox — an email reset would store a temp hash
        # nobody can ever receive AND lock the admin behind must_change.
        # Server-side recovery: delete users/ladmin/auth.json and restart.
        log_with_sid(email, "warning", "LADMIN_RESET_REFUSED")
        return _landing(
            request, email=email, status_code=403,
            error=("The local administrator password cannot be reset by email. "
                   "Ask whoever installed PowerDataChat to reset it on the server."))
    if not _EMAIL_RE.match(email):
        return _landing(request, error="Please enter a valid email",
                        email=email, status_code=400)

    store = AuthStore()
    if not store.user_exists(email):
        log_with_sid(email, "info", "PASSWORD_RESET_UNKNOWN_ACCOUNT")
        return _landing(request, error="This account does not exist.",
                        email=email, status_code=404)

    temp_password = secrets.token_urlsafe(9)
    store.set_temp_password(email, temp_password)
    try:
        brain_client.send_password_reset_email(email, temp_password)
    except Exception as e:
        # Roll the temp password back so the failed reset leaves no
        # unusable credential behind; the user's own password still works.
        store.clear_temp_password(email)
        log_with_sid(email, "error", f"PASSWORD_RESET_EMAIL_FAILED: {e}")
        return _landing(
            request, email=email, status_code=502,
            error="Could not send the reset email. Please try again later or contact your administrator.",
        )
    log_with_sid(email, "info", "PASSWORD_RESET_EMAIL_SENT")
    return _landing(request, email=email,
                    info="A temporary password has been sent to your email.")


# --- Forced password change (temp-password logins) ----------------------------

@router.get("/auth/change_password")
async def change_password_page(request: Request):
    """The set-a-new-password page a temp-password login lands on. Without a
    session (or without the must_change flag) it just bounces to the start."""
    email = request.session.get("email")
    if not email:
        return RedirectResponse(url="/", status_code=302)
    if not request.session.get("must_change_password"):
        return RedirectResponse(url=_post_login_target(email), status_code=302)
    return _TEMPLATES.TemplateResponse(
        "change_password.html",
        {"request": request, "email": email, "error": None},
    )


@router.post("/auth/change_password")
async def change_password_submit(request: Request):
    """Form-encoded {new_password, confirm_password} for the forced-change
    page. The user already authenticated with the temp password."""
    email = request.session.get("email")
    if not email:
        return RedirectResponse(url="/", status_code=302)
    if not request.session.get("must_change_password"):
        # Only the forced flow may set a password WITHOUT the current one —
        # that user just authenticated with the temp password. Everyone else
        # goes through /auth/password (current password verified).
        return RedirectResponse(url=_post_login_target(email), status_code=302)
    form = await request.form()
    new_password = form.get("new_password") or ""
    confirm = form.get("confirm_password") or ""

    def _page(err: str, code: int = 400):
        return _TEMPLATES.TemplateResponse(
            "change_password.html",
            {"request": request, "email": email, "error": err},
            status_code=code,
        )

    if len(new_password) < 4:
        return _page("Password must be at least 4 characters")
    if new_password != confirm:
        return _page("Passwords do not match")
    try:
        AuthStore().set_password(email, new_password)
    except Exception as e:
        log_with_sid(email, "error", f"FORCED_PASSWORD_CHANGE_FAILED: {e}")
        return _page("Could not save the new password. Please try again.", 500)
    request.session.pop("must_change_password", None)
    log_with_sid(email, "info", "USER_PASSWORD_CHANGED", forced=True)
    return RedirectResponse(url=_post_login_target(email), status_code=302)


@router.post("/auth/logout")
async def logout(request: Request):
    email = request.session.get("email")
    request.session.pop("email", None)
    request.session.pop("sid", None)
    request.session.pop("remember", None)
    request.session.pop("must_change_password", None)
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
async def change_password(request: Request):
    """Change password from the lab profile dropdown.
    Body: {current_password, new_password}. Verified server-side."""
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        body = {}
    current = body.get("current_password") or ""
    new_password = (body.get("new_password") or "").strip()
    if len(new_password) < 4:
        return JSONResponse({"error": "Password must be at least 4 characters"}, status_code=400)

    store = AuthStore()
    if store.has_password(email) or store.get_auth(email).get("temp_password_hash"):
        if store.verify_password(email, current) is None:
            log_with_sid(email, "warning", "PASSWORD_CHANGE_BAD_CURRENT")
            return JSONResponse({"error": "Incorrect current password"}, status_code=401)
    try:
        store.set_password(email, new_password)
    except Exception as e:
        log_with_sid(email, "error", f"PASSWORD_CHANGE_FAILED: {e}")
        return JSONResponse({"error": "Could not save the new password"}, status_code=500)
    request.session.pop("must_change_password", None)
    log_with_sid(email, "info", "USER_PASSWORD_CHANGED", forced=False)
    return {"ok": True}


@router.get("/auth/subscription")
async def subscription_const(request: Request):
    return {"plan": _FIXED_PLAN, "subscription_plan": _FIXED_PLAN}


# --- B2C billing / publishing — disabled in enterprise -----------------------
# The dashboard template is a verbatim B2C copy that still calls the Paddle
# billing + publish endpoints. Enterprise has no subscriptions (constant
# "Enterprise" plan) and no public publishing. We return clean responses so the
# UI degrades gracefully and the access log carries no 404s (mirrors the
# chat-level publish stub in routes/chat.py). No internals are exposed (Art. IV).

@router.get("/paddle/config")
async def paddle_config_disabled(request: Request):
    """Billing is disabled in the enterprise build. Return a benign config so the
    dashboard's page-load Paddle bootstrap is a no-op (no `client_token` → no
    `Paddle.Initialize`) and produces no 404 / console error."""
    return {"enabled": False, "client_token": None, "environment": None}


@router.post("/auth/subscription")
async def subscription_change_disabled(request: Request):
    """B2C plan-change POST. Enterprise plan is constant; no changes allowed."""
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    log_with_sid(email, "info", "SUBSCRIPTION_CHANGE_DISABLED")
    return JSONResponse(
        {"ok": False, "error": "Subscription changes are not available in the enterprise build."},
        status_code=400,
    )


@router.post("/api/paddle/subscription/update-payment")
@router.post("/api/paddle/subscription/reactivate")
@router.post("/api/paddle/subscription/preview")
@router.post("/api/paddle/subscription/cancel")
@router.post("/api/paddle/subscription/update")
async def paddle_subscription_disabled(request: Request):
    """All Paddle subscription-management calls — billing is off-prem."""
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    log_with_sid(email, "info", "PADDLE_SUBSCRIPTION_DISABLED")
    return JSONResponse(
        {"ok": False, "error": "Billing is not available in the enterprise build."},
        status_code=400,
    )


@router.post("/auth/conversations/{conv_id}/publish")
@router.post("/auth/conversations/{conv_id}/unpublish")
async def conversation_publish_disabled(request: Request, conv_id: str):
    """Conversation-level public publish — not available on-prem (sharing is
    recipient-list only). Mirrors the chat-level publish stub."""
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    log_with_sid(email, "info", f"CONVERSATION_PUBLISH_DISABLED conv_id={conv_id}")
    return JSONResponse(
        {"error": "Public publish is not available in the on-prem build."},
        status_code=400,
    )


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


@router.post("/auth/active_chats/pin")
async def pin_chat(request: Request):
    """Pin/unpin a chat in the sidebar (QA 3.2). Body: {chat_id, pinned}.
    Per-user (each user's own active_chats.jsonl); pinned chats sort first."""
    email = request.session.get("email")
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    body = await request.json()
    chat_id = body.get("chat_id")
    pinned = bool(body.get("pinned"))
    if not chat_id:
        return JSONResponse({"error": "Missing chat_id"}, status_code=400)
    ok = AuthStore().set_chat_pinned(email, chat_id, pinned)
    return JSONResponse({"ok": ok, "pinned": pinned}, status_code=200 if ok else 404)


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
