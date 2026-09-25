"""Client-side auth: email+password landing + the profile + sidebar endpoints.

Password model (all local — the brain never sees a password):
  - Sign-in is INVITATION-ONLY: accounts come to exist through an admin
    invite (`POST /api/admin/users/invite`), a share (password-less
    placeholder) or Microsoft SSO. `/auth/login` never creates one unless
    `ALLOW_SELF_REGISTRATION` is on (the hosted demo only): then an address
    with NO user folder gets the typed password + the welcome mail.
  - Every sign-in failure — unknown address, account without a password,
    wrong password — answers 401 with ONE neutral line and the Reset action,
    after exactly one PBKDF2 verification (a dummy hash where there is none),
    so neither the page nor its timing says which case it was.
  - Reset: `POST /auth/reset_password` answers every well-formed address the
    same (200, neutral line); a background thread checks the account, mints
    a single-use 30-minute link token (only its sha256 is stored) and asks
    the brain to mail the link (`/v1/send_password_reset_email`). The link
    opens `GET /auth/reset/{token}` (set-new-password form) and
    `POST /auth/reset/{token}` sets the password and redirects to
    `/?reset=done` — no automatic sign-in.
  - A temp password stored by the PREVIOUS release still signs in and
    forces a password change (accounts mid-reset at upgrade time).
  - Attempts are limited per address and per peer (`auth_limiter`); a
    refused attempt answers 429 + Retry-After without evaluating anything.
    The configured local admin account is spaced but never locked, so an
    anonymous caller cannot lock the operator out.
  - One password rule everywhere a password is SET (`password_rule_error`,
    `PASSWORD_MIN_LENGTH`); sign-in never checks it, so an existing shorter
    password works until its next change.
  - A password change or a used reset link ends every OTHER session of the
    account: sign-in stamps the account's `session_generation` into the
    session as `gen`, app.py's SessionGenerationGate empties a session whose
    `gen` no longer matches, and the session that made a change re-stamps
    itself.
  - An account that signs in with Microsoft and has no local password
    (`AuthStore.is_sso_only`) cannot obtain one here, neither through a
    reset link nor through the profile change (a local password would
    bypass the identity provider's MFA).
  - "Remember me" → persistent session cookie via the
    RememberMeSessionMiddleware in app.py; unchecked → browser-session
    cookie. Either kind ends REMEMBER_ME_MAX_DAYS after sign-in (`iat`,
    stamped at every sign-in) however often it is renewed: an absolute
    lifetime, not a sliding one.

Every value that reaches a log line passes `exec_transport.log_safe_text` (or
is a validated address / constant); a reset token or link is never logged.

The dashboard.html template (copied verbatim from B2C) expects a profile JSON
with `email`, `username`, `subscription_plan` keys. To keep that page working
without re-skinning it, we return `username = email` and `subscription_plan =
"Enterprise"` (constants). Profile updates only persist the email.
"""
from __future__ import annotations

import re
import secrets
import sys
import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path as _P

import auth_limiter
import password_utils
from exec_transport import log_safe_text
from local_store import AuthStore, reset_record_live
from logger_utils import log_with_sid
from settings import settings
import brain_client

router = APIRouter(tags=["client-auth"])

_TEMPLATES = Jinja2Templates(directory=str(_P(__file__).resolve().parent.parent / "templates"))

# Conventional address characters only, at most 254 characters. Anchored
# with \Z (a `$` would also accept a trailing newline); callers use
# `.fullmatch`. It gates sign-in, reset, the admin invite and every share
# route's recipients, so markup, quotes, slashes and whitespace can never
# become an identity (a user folder, a sidebar owner label).
_EMAIL_RE = re.compile(
    r"^(?=.{1,254}\Z)[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\Z")

# Exactly what secrets.token_urlsafe(32) produces.
_RESET_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")

_FIXED_PLAN = "Enterprise"

SIGNIN_FAILED_TEXT = ("Sign-in failed. Check your email and password, or use "
                      "“Reset password” if you have not set one yet.")
RESET_SENT_TEXT = ("If an account exists for this address, a reset link has been "
                   "sent. It expires in 30 minutes.")
RESET_INVALID_TEXT = "This reset link is invalid or has expired. Request a new one."
TOO_MANY_TEXT = "Too many attempts. Please try again later."
SSO_NO_LOCAL_PASSWORD_TEXT = "This account signs in with Microsoft and has no local password."

_DUMMY_HASH = None
_DUMMY_LOCK = threading.Lock()


def _local_admin_username() -> str:
    return (settings.LOCAL_ADMIN_USERNAME or "").strip().lower()


def _valid_login_id(value: str) -> bool:
    """A real email, or the fixed local-admin username (the ONLY non-email
    identity; every other id still requires a valid email)."""
    return bool(_EMAIL_RE.fullmatch(value)) or bool(value and value == _local_admin_username())


def password_rule_error(password: str):
    """The one password rule, applied wherever a password is SET: the
    message when `password` is shorter than PASSWORD_MIN_LENGTH (read at call
    time), else None. Sign-in never applies it."""
    minimum = int(settings.PASSWORD_MIN_LENGTH)
    if len(password or "") < minimum:
        return f"Password must be at least {minimum} characters"
    return None


def _session_now() -> int:
    """The session layer's clock (app._session_clock, which the tests
    replace), read without importing app (app imports this module). Falls
    back to the wall clock when app is not loaded (router-only apps)."""
    app_mod = sys.modules.get("app")
    clock = getattr(app_mod, "_session_clock", None)
    if not callable(clock):
        clock = time.time
    try:
        return int(clock())
    except Exception as e:
        log_with_sid("auth", "warning",
                     f"SESSION_CLOCK_FAILED {log_safe_text(type(e).__name__, 80)}")
        return int(time.time())


def _peer(request: Request) -> str:
    return request.client.host if request.client and request.client.host else "unknown"


def _run_in_background(fn, *args) -> None:
    """Run `fn(*args)` on a daemon thread (the reset mail must not make the
    response time depend on whether the account exists). Never raises."""
    try:
        threading.Thread(target=fn, args=args, daemon=True, name="auth_bg").start()
    except Exception as e:
        log_with_sid("auth", "error",
                     f"AUTH_BACKGROUND_START_FAILED {log_safe_text(type(e).__name__, 80)}")


def _dummy_hash() -> str:
    """A hash of a random secret nobody knows, generated on first use."""
    global _DUMMY_HASH
    with _DUMMY_LOCK:
        if _DUMMY_HASH is None:
            _DUMMY_HASH = password_utils.generate_password_hash(secrets.token_urlsafe(24))
        return _DUMMY_HASH


def _verify_nothing(password: str) -> None:
    """Spend the same PBKDF2 verification a wrong password costs, for a
    sign-in that has no hash to check (unknown / password-less address)."""
    try:
        password_utils.check_password_hash(_dummy_hash(), password)
    except Exception as e:
        log_with_sid("auth", "warning",
                     f"AUTH_DUMMY_VERIFY_FAILED {log_safe_text(type(e).__name__, 80)}")


def _public_base() -> str:
    """The base of a mailed reset link: PUBLIC_BASE_URL when it is an http(s)
    URL (trailing slash stripped), else "". There is deliberately NO fallback
    to the request's own address: that comes from the caller's Host header,
    so an anonymous reset request could make a victim's genuine mail carry a
    live token to a host of the caller's choosing (D9-26). Empty means no
    link is minted or mailed at all."""
    base = (settings.PUBLIC_BASE_URL or "").strip()
    if base.lower().startswith(("http://", "https://")):
        return base.rstrip("/")
    return ""


def reset_link(base: str, token: str) -> str:
    return base + "/auth/reset/" + token


def _public_profile(email: str) -> dict:
    """Shape the profile in the way dashboard.js expects.

    NOTE: the admin flags are `is_local_admin` (ANY admin permission —
    bootstrap and promoted alike; no JS consumer, kept for shape stability)
    and `is_admin_user` (19g: PROMOTED admin — permission "admin" but not
    the bootstrap account), deliberately NEVER `is_admin` — dashboard.js
    feeds `profile.is_admin` into the B2C Publish context-menu items, whose
    routes return 400 by design on-prem."""
    is_power = False
    try:
        import roles_store
        is_power = roles_store.is_power_user(email)   # permission == "power" (19e)
    except Exception as e:
        log_with_sid(email, "warning", f"PROFILE_POWER_FLAG_FAILED: {log_safe_text(str(e), 200)}")
    is_admin_user = False
    try:
        store = AuthStore()
        is_admin_user = store.is_admin(email) and not store.is_bootstrap_admin(email)
    except Exception as e:
        log_with_sid(email, "warning", f"PROFILE_ADMIN_FLAG_FAILED: {log_safe_text(str(e), 200)}")
    return {
        "username": email,
        "email": email,
        "full_name": "",
        "subscription_plan": _FIXED_PLAN,
        "is_local_admin": AuthStore().is_admin(email),
        "is_power_user": is_power,
        "is_admin_user": is_admin_user,
    }


def _post_login_target(email: str) -> str:
    """Where a signed-in user lands. Only the BOOTSTRAP ladmin account is
    config-only — it goes straight to the Data-sources page, never the /lab
    chat UI (app.py's /lab route mirrors this with a redirect guard). A
    PROMOTED admin (19g) lands on /lab like everyone else."""
    return ("/admin/data_sources"
            if AuthStore().is_bootstrap_admin(email) else "/lab")


# --- Auth landing + login ----------------------------------------------------

def _landing(request: Request, *, error: str = None, password_error: str = None,
             info: str = None, email: str = "", status_code: int = 200,
             show_reset: bool = False, signin_failed: bool = False,
             headers: dict = None):
    """Render the landing page with optional messages.

    `password_error` renders in red under the password field; `signin_failed`
    renders the ONE neutral sign-in failure line there instead (i18n-tagged);
    `show_reset` additionally renders the Reset-password action. `error` is
    the generic top message, `info` the green one.
    """
    # Function-local import ON PURPOSE: routes/sso.py imports this module, so
    # a top-level sso_store import here would invite a future cycle. The flag
    # keeps the Microsoft button visible on error re-renders too.
    sso_enabled = False
    try:
        import sso_store
        sso_enabled = sso_store.is_enabled()
    except Exception as e:
        log_with_sid("sso", "warning", f"SSO_LANDING_CHECK_FAILED: {log_safe_text(str(e), 200)}")
    return _TEMPLATES.TemplateResponse(
        request,
        "auth_landing.html",
        {"request": request, "error": error, "password_error": password_error,
         "info": info, "email": email, "show_reset": show_reset,
         "signin_failed": signin_failed, "sso_enabled": sso_enabled,
         "self_registration": bool(settings.ALLOW_SELF_REGISTRATION)},
        status_code=status_code,
        headers=headers,
    )


def _too_many(request: Request, verdict, *, email: str = ""):
    """The 429 answer to a refused attempt: nothing was evaluated."""
    return _landing(request, error=TOO_MANY_TEXT, email=email, status_code=429,
                    headers={"Retry-After": str(int(verdict.retry_after_s))})


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
    # The account's current generation (a later password change or reset
    # ends this session) and the sign-in time (the absolute lifetime starts
    # again at every sign-in).
    request.session["gen"] = AuthStore().session_generation(email)
    request.session["iat"] = _session_now()
    # Single funnel for every sign-in branch (password, self-registration,
    # SSO) — the one place to stamp last_login_at. touch_last_login never
    # raises.
    AuthStore().touch_last_login(email)


def _send_welcome_email_async(email: str) -> None:
    """Fire-and-forget welcome mail via the brain. Never blocks or fails login."""
    def _fire():
        try:
            brain_client.send_welcome_email(email)
            log_with_sid(email, "info", "WELCOME_EMAIL_REQUESTED")
        except Exception as e:
            log_with_sid(email, "warning", f"WELCOME_EMAIL_FAILED: {log_safe_text(str(e), 200)}")
    threading.Thread(target=_fire, daemon=True, name="welcome_email").start()


@router.post("/auth/login")
async def login(request: Request):
    """Email+password login (form-encoded: email, password, remember?).

    Invitation-only: an address without a password (unknown, placeholder,
    legacy) and a wrong password get the same 401 page after one PBKDF2
    verification. `ALLOW_SELF_REGISTRATION` (demo only) lets an address with
    NO user folder set its password here. A stored temp password (previous
    release) signs in but forces a change before /lab opens. Every attempt
    counts against the limiter until it succeeds.
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

    ip = _peer(request)
    # The configured local admin account is spaced but never locked: an
    # anonymous caller must not be able to lock the operator out.
    verdict = auth_limiter.begin("login", email, ip,
                                 lockout=(email != _local_admin_username()))
    if not verdict.allowed:
        return _too_many(request, verdict, email=email)

    def _refuse():
        return _landing(request, email=email, status_code=401,
                        signin_failed=True, show_reset=True)

    store = AuthStore()
    auth = store.get_auth(email)
    if not auth.get("password_hash") and not auth.get("temp_password_hash"):
        if email == _local_admin_username():
            # Bootstrapped account without a password (LOCAL_ADMIN_PASSWORD
            # unset at boot). The reset flow does not serve ladmin (no
            # mailbox), so point at the server-side fix instead.
            log_with_sid(email, "warning", "LADMIN_LOGIN_NO_BOOTSTRAP")
            return _landing(
                request, email=email, status_code=403,
                password_error=("The administrator account has no password yet. "
                                "Set LOCAL_ADMIN_PASSWORD in the server "
                                "environment and restart the container."))
        if not store.user_exists(email) and settings.ALLOW_SELF_REGISTRATION:
            # Demo-only open registration: a genuinely NEW address (no user
            # folder) sets its own password here, under the same rule as
            # every other place a password is set, checked before anything
            # is written.
            rule_error = password_rule_error(password)
            if rule_error:
                return _landing(request, password_error=rule_error,
                                email=email, status_code=400)
            store.ensure_user(email)
            store.set_password(email, password)
            auth_limiter.success("login", email, ip)
            _start_session(request, email, remember=remember)
            log_with_sid(email, "info", "USER_LOGIN_FIRST_PASSWORD_SET",
                         sid=log_safe_text(request.session.get("sid"), 40))
            _send_welcome_email_async(email)
            try:
                brain_client.post_activity("login", email)
            except Exception as e:
                log_with_sid(email, "warning",
                             f"LOGIN_ACTIVITY_FAILED {log_safe_text(type(e).__name__, 80)}")
            return RedirectResponse(url="/lab", status_code=302)
        # Unknown address, share placeholder or legacy account: the typed
        # password must never become theirs, and the page must not say which
        # case it was. The mailbox owner sets a password through the reset
        # link instead.
        _verify_nothing(password)
        if store.user_exists(email):
            log_with_sid(email, "info", "USER_LOGIN_NO_PASSWORD")
        else:
            log_with_sid(email, "info", "USER_LOGIN_UNKNOWN_ADDRESS")
        return _refuse()

    outcome = store.verify_password(email, password)
    if outcome is None:
        log_with_sid(email, "warning", "USER_LOGIN_BAD_PASSWORD")
        return _refuse()

    auth_limiter.success("login", email, ip)
    must_change = (outcome == "temp") or bool(store.get_auth(email).get("must_change_password"))
    _start_session(request, email, remember=remember, must_change=must_change)
    log_with_sid(email, "info", "USER_LOGIN", sid=log_safe_text(request.session.get("sid"), 40),
                 must_change=bool(must_change))
    try:
        brain_client.post_activity("login", email)
    except Exception as e:
        log_with_sid(email, "warning",
                     f"LOGIN_ACTIVITY_FAILED {log_safe_text(type(e).__name__, 80)}")
    target = "/auth/change_password" if must_change else _post_login_target(email)
    return RedirectResponse(url=target, status_code=302)


def _send_reset_link(email: str, base: str) -> None:
    """The account-dependent half of a reset request, off the request path:
    existence check, token mint, mail. Logs only; never raises."""
    try:
        if email == _local_admin_username():
            # The bootstrap account has no mailbox; recovery is server-side
            # (CUSTOMER_INSTALL).
            log_with_sid(email, "warning", "LADMIN_RESET_REFUSED")
            return
        store = AuthStore()
        if not store.user_exists(email):
            log_with_sid(email, "info", "PASSWORD_RESET_UNKNOWN_ACCOUNT")
            return
        if store.is_sso_only(email):
            # Signs in with Microsoft and has no local password: mint
            # nothing. The caller already has the neutral page, which must
            # not reveal the account type.
            log_with_sid(email, "info", "SSO_ACCOUNT_RESET_REFUSED")
            return
        if not base:
            # No trusted address to build a link from (D9-26): mint nothing,
            # mail nothing; the caller already has the neutral page.
            log_with_sid(log_safe_text(email, 254), "error",
                         "PASSWORD_RESET_NO_BASE_URL PUBLIC_BASE_URL is not set, "
                         "so no reset link can be mailed")
            return
        token = store.create_reset_token(email)
        if not token:
            return
        try:
            brain_client.send_password_reset_email(
                email, reset_link(base, token),
                timeout=settings.BRAIN_DRAFT_TIMEOUT)
        except Exception as e:
            # No unusable link stays behind; the user's own password still
            # works, and "Reset password" can be tried again.
            store.clear_reset_token(email)
            log_with_sid(email, "error",
                         f"PASSWORD_RESET_EMAIL_FAILED {log_safe_text(type(e).__name__, 80)}")
            return
        log_with_sid(email, "info", "PASSWORD_RESET_EMAIL_SENT")
    except Exception as e:
        log_with_sid(email, "error",
                     f"PASSWORD_RESET_FAILED {log_safe_text(type(e).__name__, 80)}")


@router.post("/auth/reset_password")
async def reset_password(request: Request):
    """Password reset request (form-encoded: email). Every well-formed
    address takes the same path and gets the same page: count the request,
    hand the address to a background thread, answer 200 with the neutral
    line. Whether a link was minted and mailed never shows."""
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    if not _EMAIL_RE.fullmatch(email):
        return _landing(request, error="Please enter a valid email",
                        email=email, status_code=400)
    verdict = auth_limiter.begin("reset", email, _peer(request))
    if not verdict.allowed:
        return _too_many(request, verdict, email=email)
    _run_in_background(_send_reset_link, email, _public_base())
    return _landing(request, email=email, info=RESET_SENT_TEXT)


# --- Reset link ----------------------------------------------------------------

_RESET_PAGE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def _live_reset_email(token: str):
    """The email a live reset link belongs to, else None. Read only."""
    if not _RESET_TOKEN_RE.fullmatch(token or ""):
        return None
    found = AuthStore().find_reset_token(token)
    if not found:
        return None
    if found[1].get("reset_used") is True:
        log_with_sid(log_safe_text(found[0], 254), "warning", "RESET_TOKEN_REUSED")
        return None
    return found[0] if reset_record_live(found[1]) else None


def _reset_invalid(request: Request):
    return _landing(request, error=RESET_INVALID_TEXT, status_code=404,
                    headers=dict(_RESET_PAGE_HEADERS))


def _reset_page(request: Request, token: str, error: str = None, status_code: int = 200):
    return _TEMPLATES.TemplateResponse(
        request,
        "reset_password.html",
        {"request": request, "token": token, "error": error},
        status_code=status_code,
        headers=dict(_RESET_PAGE_HEADERS),
    )


@router.get("/auth/reset/{token}")
async def reset_link_page(request: Request, token: str):
    """The set-new-password form behind a mailed link. Read only — a mail
    scanner pre-fetching the link changes nothing. Only an invalid, expired
    or used link counts against the limiter."""
    ip = _peer(request)
    verdict = auth_limiter.begin("token", None, ip)
    if not verdict.allowed:
        return _too_many(request, verdict)
    if _live_reset_email(token) is None:
        log_with_sid("auth", "info", "PASSWORD_RESET_LINK_INVALID")
        return _reset_invalid(request)
    auth_limiter.success("token", None, ip)
    return _reset_page(request, token)


@router.post("/auth/reset/{token}")
async def reset_link_submit(request: Request, token: str):
    """Form-encoded {new_password, confirm_password}. Sets the password,
    marks the link used and redirects to the landing page — the user then
    signs in with the new password (no automatic sign-in)."""
    ip = _peer(request)
    verdict = auth_limiter.begin("token", None, ip)
    if not verdict.allowed:
        return _too_many(request, verdict)
    link_email = _live_reset_email(token)
    if link_email is None:
        log_with_sid("auth", "info", "PASSWORD_RESET_LINK_INVALID")
        return _reset_invalid(request)
    # A live link: whatever happens next is not a failed guess.
    auth_limiter.success("token", None, ip)
    if AuthStore().is_sso_only(link_email):
        # A link minted before the account moved to Microsoft sign-in.
        log_with_sid(log_safe_text(link_email, 254), "warning", "SSO_ACCOUNT_RESET_REFUSED")
        return _reset_page(request, token, SSO_NO_LOCAL_PASSWORD_TEXT, 403)
    form = await request.form()
    new_password = form.get("new_password") or ""
    confirm = form.get("confirm_password") or ""
    rule_error = password_rule_error(new_password)
    if rule_error:
        return _reset_page(request, token, rule_error, 400)
    if new_password != confirm:
        return _reset_page(request, token, "Passwords do not match", 400)
    try:
        email = AuthStore().consume_reset_token(token, new_password)
    except Exception as e:
        log_with_sid("auth", "error",
                     f"PASSWORD_RESET_SAVE_FAILED {log_safe_text(type(e).__name__, 80)}")
        return _reset_page(request, token, "Could not save the new password. Please try again.", 500)
    if not email:
        return _reset_invalid(request)
    log_with_sid(log_safe_text(email, 254), "info", "PASSWORD_RESET_COMPLETED")
    return RedirectResponse(url="/?reset=done", status_code=302,
                            headers=dict(_RESET_PAGE_HEADERS))


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
        request,
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
            request,
            "change_password.html",
            {"request": request, "email": email, "error": err},
            status_code=code,
        )

    if AuthStore().is_sso_only(email):
        log_with_sid(log_safe_text(email, 254), "warning", "SSO_ACCOUNT_PASSWORD_REFUSED",
                     forced=True)
        return _page(SSO_NO_LOCAL_PASSWORD_TEXT, 403)
    rule_error = password_rule_error(new_password)
    if rule_error:
        return _page(rule_error)
    if new_password != confirm:
        return _page("Passwords do not match")
    try:
        generation = AuthStore().set_password(email, new_password)
    except Exception as e:
        log_with_sid(email, "error", f"FORCED_PASSWORD_CHANGE_FAILED: {log_safe_text(str(e), 200)}")
        return _page("Could not save the new password. Please try again.", 500)
    # Every OTHER session of the account ends; this one carries on.
    request.session["gen"] = generation
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
    request.session.pop("gen", None)
    request.session.pop("iat", None)
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
        log_with_sid(email, "info", "PROFILE_UPDATE_IGNORED_EMAIL_CHANGE",
                     attempt=log_safe_text(new_email, 254))
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
    store = AuthStore()
    if store.is_sso_only(email):
        # Without this, an account with no password would set one here
        # without any current-password check.
        log_with_sid(log_safe_text(email, 254), "warning", "SSO_ACCOUNT_PASSWORD_REFUSED",
                     forced=False)
        return JSONResponse({"error": SSO_NO_LOCAL_PASSWORD_TEXT, "code": "SSO_ACCOUNT"},
                            status_code=403)
    rule_error = password_rule_error(new_password)
    if rule_error:
        return JSONResponse({"error": rule_error}, status_code=400)

    if store.has_password(email) or store.get_auth(email).get("temp_password_hash"):
        if store.verify_password(email, current) is None:
            log_with_sid(email, "warning", "PASSWORD_CHANGE_BAD_CURRENT")
            return JSONResponse({"error": "Incorrect current password"}, status_code=401)
    try:
        generation = store.set_password(email, new_password)
    except Exception as e:
        log_with_sid(email, "error", f"PASSWORD_CHANGE_FAILED: {log_safe_text(str(e), 200)}")
        return JSONResponse({"error": "Could not save the new password"}, status_code=500)
    # Every OTHER session of the account ends; this one carries on.
    request.session["gen"] = generation
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
    log_with_sid(email, "info", f"CONVERSATION_PUBLISH_DISABLED conv_id={log_safe_text(conv_id, 80)}")
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
        if _EMAIL_RE.fullmatch(e) and e != email:
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
    # Sharing a conversation also grants access to its chat, so only the
    # chat's owner may do it — the same rule as the chat-level share.
    if _ls.get_chat_meta_owner(chat_id) != email:
        return JSONResponse({"error": "Access denied"}, status_code=403)

    store = _ls.ChatDataStore(chat_id)
    meta = store.read_meta()
    chat_title = meta.get("title") or "Chat"
    files = [f.get("file_name") for f in meta.get("files", []) if f.get("file_name")]
    conv_title = (conv.get("title") or "").strip() or f"Shared by {email}"

    # Add recipients to chat's sharing list (so /_require_chat lets them in)
    added = store.add_share_recipients(recipients)
    # An address that has never signed in gets a password-less placeholder,
    # so whoever types it first at the sign-in page cannot claim the share.
    for rec in recipients:
        AuthStore().ensure_invited_user(rec, email)

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
            log_with_sid(email, "warning", f"CONV_SHARE_EMAIL_ERROR: {log_safe_text(str(e), 200)}")

    log_with_sid(email, "info",
                 f"CONV_SHARED chat_id={chat_id} conv_id={log_safe_text(conv_id, 80)} "
                 f"to={log_safe_text(','.join(recipients), 2000)}")
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
