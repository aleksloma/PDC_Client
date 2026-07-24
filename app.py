"""Client (on-prem) FastAPI app.

Frontend:
  /                   → email-only auth landing (or /lab if already signed in)
  /lab                → the existing /lab dashboard (copied byte-compatible)
Backend:
  /auth/*             → email-only profile + session management
  /new_session        → reset per-session temp area
  /upload             → file upload (raw data stays here)
  /schema_autofill_full → optional brain-driven description fill-in
  /generate_chatdata  → promote temp upload into a permanent chat
  /api/chat/*         → chat endpoints (SSE stream + sidebar + reports)
"""
from __future__ import annotations

import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.datastructures import MutableHeaders

from settings import settings
from logger_utils import log_with_sid

from routes.auth import router as auth_router
from routes.upload import router as upload_router
from routes.chat import router as chat_router
from routes.report import router as report_router
from routes.schema import router as schema_router
from routes.dashboards import router as dashboards_router


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    log_with_sid("startup", "info", "CLIENT_STARTED",
                 data_root=settings.DATA_ROOT,
                 brain_url=settings.BRAIN_URL,
                 token_set=bool(settings.BRAIN_TENANT_TOKEN))
    # Build marker — lets an operator confirm from the logs that the running
    # image actually carries the clone renderer (a stale image is the classic
    # "decks still look un-branded" cause: the new code never got deployed).
    log_with_sid("startup", "info", "CLIENT_BUILD", marker="pptx-clone-iter6")
    yield
    log_with_sid("shutdown", "info", "CLIENT_STOPPED")


class RememberMeSessionMiddleware(SessionMiddleware):
    """SessionMiddleware with a per-session cookie lifetime.

    Sessions carrying `remember: True` (the "Remember me" checkbox at login)
    get a persistent cookie with `Max-Age` = the configured `max_age`
    (~30 days); all other sessions get a browser-session cookie (no Max-Age),
    which dies when the browser closes. Only the Set-Cookie write differs
    from the stock starlette middleware — reading/unsigning is inherited
    behavior (max_age acts as the outer validity cap for both kinds).
    """

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        import json as _json
        from base64 import b64decode, b64encode
        from itsdangerous.exc import BadSignature
        from starlette.requests import HTTPConnection

        connection = HTTPConnection(scope)
        initial_session_was_empty = True

        if self.session_cookie in connection.cookies:
            data = connection.cookies[self.session_cookie].encode("utf-8")
            try:
                data = self.signer.unsign(data, max_age=self.max_age)
                scope["session"] = _json.loads(b64decode(data))
                initial_session_was_empty = False
            except BadSignature:
                scope["session"] = {}
        else:
            scope["session"] = {}

        async def send_wrapper(message) -> None:
            if message["type"] == "http.response.start":
                if scope["session"]:
                    data = b64encode(_json.dumps(scope["session"]).encode("utf-8"))
                    data = self.signer.sign(data)
                    headers = MutableHeaders(scope=message)
                    # Persistent Max-Age only when the user asked to be remembered.
                    persist = bool(scope["session"].get("remember"))
                    header_value = "{session_cookie}={data}; path={path}; {max_age}{security_flags}".format(
                        session_cookie=self.session_cookie,
                        data=data.decode("utf-8"),
                        path=self.path,
                        max_age=f"Max-Age={self.max_age}; " if (persist and self.max_age) else "",
                        security_flags=self.security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
                elif not initial_session_was_empty:
                    headers = MutableHeaders(scope=message)
                    header_value = "{session_cookie}={data}; path={path}; {expires}{security_flags}".format(
                        session_cookie=self.session_cookie,
                        data="null",
                        path=self.path,
                        expires="expires=Thu, 01 Jan 1970 00:00:00 GMT; ",
                        security_flags=self.security_flags,
                    )
                    headers.append("Set-Cookie", header_value)
            await send(message)

        await self.app(scope, receive, send_wrapper)


_REMEMBER_ME_MAX_AGE = 30 * 24 * 60 * 60   # ~30 days

app = FastAPI(title="PowerDataChat Client (enterprise)", version="1.0", lifespan=lifespan)
app.add_middleware(RememberMeSessionMiddleware, secret_key=settings.SECRET_KEY,
                   same_site="lax", max_age=_REMEMBER_ME_MAX_AGE)

# Static assets (copied byte-for-byte from the B2C app)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

app.include_router(auth_router)
app.include_router(upload_router)
app.include_router(schema_router)
app.include_router(chat_router)
app.include_router(report_router)
app.include_router(dashboards_router)


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Auth landing — email + password (+ remember me)."""
    if request.session.get("email"):
        if request.session.get("must_change_password"):
            return RedirectResponse(url="/auth/change_password", status_code=302)
        return RedirectResponse(url="/lab", status_code=302)
    return templates.TemplateResponse(
        "auth_landing.html",
        {"request": request, "error": None, "password_error": None,
         "info": None, "email": ""},
    )


_AVATAR_COLORS = ["#0d9488", "#2563eb", "#7c3aed", "#db2777", "#ea580c", "#059669", "#4f46e5", "#0891b2"]


def _profile_context(email: str | None) -> dict:
    """Build the profile context the dashboard partial expects.

    Email-only: `raw_username` and `display_name` are both the email; plan
    is always "Enterprise"; avatar is derived from the email hash.
    """
    if not email:
        return {"logged_in": False, "display_name": "", "raw_username": "",
                "subscription_plan": "Enterprise",
                "avatar_color": _AVATAR_COLORS[0], "initials": ""}
    display_name = email
    parts = email.split("@")[0].replace(".", " ").replace("_", " ").split()
    if len(parts) >= 2:
        initials = (parts[0][0] + parts[1][0]).upper()
    elif len(email) >= 2:
        initials = email[:2].upper()
    else:
        initials = email.upper()
    h = 0
    for ch in email:
        h = ((h << 5) - h + ord(ch)) & 0xFFFFFFFF
        if h >= 0x80000000:
            h -= 0x100000000
    avatar_color = _AVATAR_COLORS[abs(h) % len(_AVATAR_COLORS)]
    return {
        "logged_in": True,
        "display_name": display_name,
        "raw_username": email,
        "subscription_plan": "Enterprise",
        "avatar_color": avatar_color,
        "initials": initials,
    }


@app.get("/lab", response_class=HTMLResponse)
async def lab(request: Request):
    """The /lab page — same dashboard the B2C app uses (file copied verbatim)."""
    email = request.session.get("email")
    if not email:
        return RedirectResponse(url="/", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse(url="/auth/change_password", status_code=302)
    log_with_sid(email, "info", "OPEN_LAB_UI")
    ts = int(time.time())
    prof = _profile_context(email)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "ts": ts,
            "default_days": settings.CHAT_ACTIVE_DEFAULT_DAYS,
            "max_days": settings.CHAT_ACTIVE_MAX_DAYS,
            "is_admin": False,
            "username": email,
            "subscription_plan": "Enterprise",
            **prof,
        },
    )


@app.get("/c/{conv_id}", response_class=HTMLResponse)
async def open_conversation_deeplink(request: Request, conv_id: str):
    """Deep-link / hard-refresh target for a single conversation.

    The frontend auto-opens `window.OPEN_CONV_ID`/`OPEN_CHAT_ID` on load, but a
    direct hit to `/c/{conv_id}` previously 404'd because no server route
    existed. This mirrors the `/lab` handler and additionally seeds the open
    conversation. Never 404s: unknown/foreign conv → `/lab`; no session → `/`.
    """
    email = request.session.get("email")
    if not email:
        return RedirectResponse(url="/", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse(url="/auth/change_password", status_code=302)
    # Resolve the conversation's chat_id from this user's own conversations
    # index (local_store records {conv_id, chat_id} per conversation). A conv
    # that isn't in the caller's index (unknown or another user's) → /lab.
    chat_id = None
    try:
        from local_store import AuthStore
        for row in AuthStore().list_conversations(email):
            if row.get("conv_id") == conv_id:
                chat_id = row.get("chat_id")
                break
    except Exception as e:
        log_with_sid(email, "error", f"DEEPLINK_LOOKUP_FAILED: {e}", conv_id=conv_id)
        return RedirectResponse(url="/lab", status_code=302)
    if not chat_id:
        log_with_sid(email, "info", "DEEPLINK_CONV_NOT_FOUND", conv_id=conv_id)
        return RedirectResponse(url="/lab", status_code=302)
    log_with_sid(email, "info", "OPEN_CONV_DEEPLINK", conv_id=conv_id, chat_id=chat_id)
    ts = int(time.time())
    prof = _profile_context(email)
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "ts": ts,
            "default_days": settings.CHAT_ACTIVE_DEFAULT_DAYS,
            "max_days": settings.CHAT_ACTIVE_MAX_DAYS,
            "is_admin": False,
            "username": email,
            "subscription_plan": "Enterprise",
            "open_conv_id": conv_id,
            "open_chat_id": chat_id,
            **prof,
        },
    )


@app.get("/dashboards/{dash_id}", response_class=HTMLResponse)
async def dashboard_view_page(request: Request, dash_id: str):
    """The dashboard page (grid of pinned tiles). Mirrors the /lab handler's
    session guards; unknown/unshared dashboard → /lab (never 404s, same
    philosophy as the /c/{conv_id} deep-link)."""
    email = request.session.get("email")
    if not email:
        return RedirectResponse(url="/", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse(url="/auth/change_password", status_code=302)
    try:
        from local_store import DashboardStore
        doc, is_owner = DashboardStore().resolve_dashboard(email, dash_id)
    except Exception as e:
        log_with_sid(email, "error", f"DASH_PAGE_LOOKUP_FAILED: {e}", dash_id=dash_id)
        return RedirectResponse(url="/lab", status_code=302)
    if doc is None:
        log_with_sid(email, "info", "DASH_PAGE_NOT_FOUND", dash_id=dash_id)
        return RedirectResponse(url="/lab", status_code=302)
    log_with_sid(email, "info", "OPEN_DASHBOARD_UI", dash_id=dash_id)
    ts = int(time.time())
    prof = _profile_context(email)
    return templates.TemplateResponse(
        "dashboard_view.html",
        {
            "request": request,
            "ts": ts,
            "dash_id": dash_id,
            "username": email,
            "subscription_plan": "Enterprise",
            **prof,
        },
    )


@app.get("/health")
async def health():
    from brain_client import health as brain_health
    return {
        "status": "ok",
        "service": "client",
        "brain_reachable": brain_health() if settings.BRAIN_TENANT_TOKEN else None,
        "tenant_token_configured": bool(settings.BRAIN_TENANT_TOKEN),
    }
