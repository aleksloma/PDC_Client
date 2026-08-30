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
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.datastructures import MutableHeaders

from settings import settings
from logger_utils import log_with_sid
from local_store import AuthStore
import sso_store

from routes.auth import router as auth_router
from routes.upload import router as upload_router
from routes.chat import router as chat_router
from routes.report import router as report_router
from routes.schema import router as schema_router
from routes.dashboards import router as dashboards_router
from routes.admin_data import router as admin_data_router
from routes.admin_users import router as admin_users_router
from routes.sso import router as sso_router
from routes.sso import admin_router as sso_admin_router


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

_STARTED_AT = datetime.now(timezone.utc)


def _build_stamp() -> str:
    """One line identifying the RUNNING build, for the admin sidebar.

    Deliberately not the `?v=` static cache-buster: that is the page-render
    clock, recomputed per request, and reading it as a build marker has twice
    sent verification down the wrong path. An unstamped image (no build args)
    honestly reports when this process started instead of faking an identity.
    """
    if settings.BUILD_COMMIT:
        return f"build {settings.BUILD_COMMIT}" + (
            f" · {settings.BUILD_TIME}" if settings.BUILD_TIME else "")
    return f"started {_STARTED_AT:%Y-%m-%d %H:%M} UTC"


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
    # Build marker — lets an operator confirm from the logs WHICH image is
    # running (a stale image is the classic "my fix isn't live" cause). Fed by
    # the Docker build args; also served by GET /version.
    log_with_sid("startup", "info", "CLIENT_BUILD",
                 commit=settings.BUILD_COMMIT or "dev",
                 build_time=settings.BUILD_TIME or "unstamped",
                 started_at=_STARTED_AT.isoformat(timespec="seconds"))
    # Fixed local admin bootstrap (idempotent; never overwrites an existing
    # password) + the nightly database-snapshot refresh scheduler. Both are
    # lifespan-scoped on purpose: an import-time thread would leak into every
    # pytest session (the local_store sweeper lesson).
    AuthStore().ensure_local_admin()
    import roles_store
    roles_store.RolesStore().migrate_manage_grants()   # 19f doc v1 -> v2
    roles_store.RolesStore().ensure_base_role()
    roles_store.RolesStore().remove_poweruser_role()   # 19e legacy cleanup
    import db_scheduler
    db_scheduler.start()
    yield
    db_scheduler.stop()
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
app.include_router(admin_data_router)
app.include_router(admin_users_router)
app.include_router(sso_router)
app.include_router(sso_admin_router)


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Auth landing — email + password (+ remember me), plus the Microsoft
    SSO button / auto-redirect when the ladmin has enabled Entra ID SSO.
    ?local=1 always shows the password form (the recovery escape hatch for
    ladmin and for a broken SSO config)."""
    if request.session.get("email"):
        if request.session.get("must_change_password"):
            return RedirectResponse(url="/auth/change_password", status_code=302)
        return RedirectResponse(url="/lab", status_code=302)
    try:
        sso_enabled = sso_store.is_enabled()
        sso_auto = sso_store.auto_redirect()
    except Exception as e:
        log_with_sid("sso", "warning", f"SSO_LANDING_CHECK_FAILED: {e}")
        sso_enabled = sso_auto = False
    if sso_enabled and sso_auto and request.query_params.get("local") != "1":
        return RedirectResponse(url="/auth/microsoft", status_code=302)
    return templates.TemplateResponse(
        "auth_landing.html",
        {"request": request, "error": None, "password_error": None,
         "info": None, "email": "", "sso_enabled": sso_enabled},
    )


_AVATAR_COLORS = ["#0d9488", "#2563eb", "#7c3aed", "#db2777", "#ea580c", "#059669", "#4f46e5", "#0891b2"]


def _is_power_user(email: str | None) -> bool:
    """Whether the user's per-account PERMISSION is "power" (19e —
    roles_store.is_power_user delegates to AuthStore) — gates the profile
    dropdown's "DB config" item and /power/data_sources. Fail-closed on any
    error (Article IV)."""
    if not email:
        return False
    try:
        import roles_store
        return roles_store.is_power_user(email)
    except Exception as e:
        log_with_sid(email, "warning", f"POWER_FLAG_FAILED: {e}")
        return False


def _is_admin_user(email: str | None) -> bool:
    """Whether the user is a PROMOTED admin (permission "admin", NOT the
    bootstrap ladmin account) — gates the /lab dropdown's "DB config" item
    pointing at the full /admin/data_sources page (19g). Deliberately named
    is_admin_user, never is_admin: that template key feeds the B2C Publish
    menu (400 by design on-prem). Fail-closed on any error (Article IV)."""
    if not email:
        return False
    try:
        store = AuthStore()
        return store.is_admin(email) and not store.is_bootstrap_admin(email)
    except Exception as e:
        log_with_sid(email, "warning", f"ADMIN_FLAG_FAILED: {e}")
        return False


def _profile_context(email: str | None) -> dict:
    """Build the profile context the dashboard partial expects.

    Email-only: `raw_username` and `display_name` are both the email; plan
    is always "Enterprise"; avatar is derived from the email hash.
    """
    if not email:
        return {"logged_in": False, "display_name": "", "raw_username": "",
                "subscription_plan": "Enterprise",
                "avatar_color": _AVATAR_COLORS[0], "initials": "",
                "is_power_user": False, "is_admin_user": False}
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
        "is_power_user": _is_power_user(email),
        "is_admin_user": _is_admin_user(email),
    }


@app.get("/lab", response_class=HTMLResponse)
async def lab(request: Request):
    """The /lab page — same dashboard the B2C app uses (file copied verbatim)."""
    email = request.session.get("email")
    if not email:
        return RedirectResponse(url="/", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse(url="/auth/change_password", status_code=302)
    if AuthStore().is_bootstrap_admin(email):
        # Only the BOOTSTRAP ladmin account is config-only: no chats, no /lab
        # (mirror of the non-admin guard on /admin/data_sources). A PROMOTED
        # admin (19g) renders /lab like any user.
        return RedirectResponse(url="/admin/data_sources", status_code=302)
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
            # is_admin stays False for EVERYONE — promoted admins included —
            # it feeds the B2C Publish menu (400 by design on-prem). The
            # admin-capability flag is is_admin_user in _profile_context.
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


@app.get("/admin/data_sources", response_class=HTMLResponse)
async def admin_data_sources_page(request: Request):
    """The ladmin "Data sources" page (connections + registered tables +
    refresh schedule). Same guard philosophy as every page route — redirects,
    never an error page: no session → /, forced change → change_password,
    non-admin → /lab."""
    email = request.session.get("email")
    if not email:
        return RedirectResponse(url="/", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse(url="/auth/change_password", status_code=302)
    if not AuthStore().is_admin(email):
        log_with_sid(email, "warning", "ADMIN_PAGE_DENIED")
        return RedirectResponse(url="/lab", status_code=302)
    log_with_sid(email, "info", "OPEN_ADMIN_DATA_SOURCES")
    return templates.TemplateResponse(
        "admin_data_sources.html",
        {"request": request, "ts": int(time.time()), "username": email,
         "build_stamp": _build_stamp(), "manager_mode": "admin",
         # 19g: a PROMOTED admin is a full chat user — the sidebar footer
         # renders "← Back to chat" for them; the bootstrap account has no
         # chat, so its page stays link-free.
         "back_to_chat": not AuthStore().is_bootstrap_admin(email),
         "subscription_plan": "Enterprise", **_profile_context(email)},
    )


@app.get("/power/data_sources", response_class=HTMLResponse)
async def power_data_sources_page(request: Request):
    """The POWER-USER variant of the Data-sources page: the same template in
    "power" mode (Connections read-only; Tables/Relations/per-table schedule
    only; no Users/Roles/Audit/global schedule). Same redirect-never-error
    guard style: no session → /, forced change → change_password, ladmin →
    its own page, non-power users → /lab."""
    email = request.session.get("email")
    if not email:
        return RedirectResponse(url="/", status_code=302)
    if request.session.get("must_change_password"):
        return RedirectResponse(url="/auth/change_password", status_code=302)
    if AuthStore().is_admin(email):
        return RedirectResponse(url="/admin/data_sources", status_code=302)
    if not _is_power_user(email):
        log_with_sid(email, "warning", "POWER_PAGE_DENIED")
        return RedirectResponse(url="/lab", status_code=302)
    log_with_sid(email, "info", "OPEN_POWER_DATA_SOURCES")
    summary, read_beyond = _power_scope_summary(email)
    return templates.TemplateResponse(
        "admin_data_sources.html",
        {"request": request, "ts": int(time.time()), "username": email,
         "build_stamp": _build_stamp(), "manager_mode": "power",
         "manage_scope_summary": summary, "read_beyond_manage": read_beyond,
         "subscription_plan": "Enterprise", **_profile_context(email)},
    )


def _power_scope_summary(email: str) -> tuple[str, bool]:
    """(summary sentence, read-beyond-manage flag) for the /power header —
    computed from manage_grants (19f) + connection names. Fail-soft: any
    error yields an empty summary rather than a broken page (Article IV)."""
    try:
        import roles_store
        from db_sources import DataSourceStore
        store = DataSourceStore()
        scope = roles_store.management_scope_for(email) or []
        names = {c.get("id"): c.get("name") or "?"
                 for c in store.list_connections()}
        parts = []
        for g in scope:
            if g.get("connection_id") not in names:
                continue          # grant on a deleted connection — nothing to say
            label = names[g["connection_id"]]
            parts.append(f"{label} — all schemas" if g.get("schema") is None
                         else f"{label} / {g['schema']}")
        summary = ("You can manage: " + ", ".join(parts) if parts else
                   "You have no manage grants yet — ask your administrator.")
        read_beyond = False
        tables = {t.get("id"): t for t in store.list_tables()}
        for tid in roles_store.allowed_table_ids_for(email):
            t = tables.get(tid)
            if t and not roles_store.scope_covers(
                    scope, t.get("connection_id"), t.get("schema")):
                read_beyond = True
                break
        return summary, read_beyond
    except Exception as e:
        log_with_sid(email, "warning", f"POWER_SCOPE_SUMMARY_FAILED: {e}")
        return "", False


@app.get("/health")
async def health():
    from brain_client import health as brain_health
    return {
        "status": "ok",
        "service": "client",
        "brain_reachable": brain_health() if settings.BRAIN_TENANT_TOKEN else None,
        "tenant_token_configured": bool(settings.BRAIN_TENANT_TOKEN),
        "build_commit": settings.BUILD_COMMIT or None,
    }


@app.get("/version")
async def version():
    """Which build is running — checkable without shell access (the point:
    the static `?v=` parameter is a per-request clock, not a build marker).
    Unauthenticated like /health, and carries no secrets."""
    return {
        "commit": settings.BUILD_COMMIT or None,
        "build_time": settings.BUILD_TIME or None,
        "started_at": _STARTED_AT.isoformat(timespec="seconds"),
    }
