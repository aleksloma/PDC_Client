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

from settings import settings
from logger_utils import log_with_sid

from routes.auth import router as auth_router
from routes.upload import router as upload_router
from routes.chat import router as chat_router
from routes.report import router as report_router
from routes.schema import router as schema_router


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


app = FastAPI(title="PowerDataChat Client (enterprise)", version="1.0", lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=settings.SECRET_KEY, same_site="lax")

# Static assets (copied byte-for-byte from the B2C app)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

app.include_router(auth_router)
app.include_router(upload_router)
app.include_router(schema_router)
app.include_router(chat_router)
app.include_router(report_router)


@app.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    """Auth landing — email-only. The ONLY frontend difference vs the B2C app."""
    if request.session.get("email"):
        return RedirectResponse(url="/lab", status_code=302)
    return templates.TemplateResponse("auth_landing.html", {"request": request, "error": None})


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


@app.get("/health")
async def health():
    from brain_client import health as brain_health
    return {
        "status": "ok",
        "service": "client",
        "brain_reachable": brain_health() if settings.BRAIN_TENANT_TOKEN else None,
        "tenant_token_configured": bool(settings.BRAIN_TENANT_TOKEN),
    }
