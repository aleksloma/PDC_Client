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

import ipaddress
import json
import re
import secrets
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.datastructures import MutableHeaders

from settings import settings
from logger_utils import log_with_sid
from exec_transport import log_safe_text
from local_store import SESSION_GEN_REMOVED, AuthStore
import sso_store
import gcs_upload

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
from routes.charts import router as charts_router


_HERE = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HERE / "templates"))

_STARTED_AT = datetime.now(timezone.utc)

# The clock the session layer reads (the absolute session lifetime and the
# sign-in time routes/auth.py stamps). A module attribute so the tests can
# replace it.
_session_clock = time.time


def _session_now() -> int:
    """`_session_clock()` as whole seconds; the wall clock if it fails."""
    try:
        return int(_session_clock())
    except Exception as e:
        log_with_sid("session", "warning",
                     f"SESSION_CLOCK_FAILED {log_safe_text(type(e).__name__, 80)}")
        return int(time.time())


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


def _refuse_on_weak_secret_key() -> None:
    """Refuse to start without a real session-signing key.

    SECRET_KEY signs the session cookie, and the session's `sid` names a
    folder under DATA_ROOT: whoever knows the key can sign any session. An
    empty value, the historical placeholder or anything shorter than
    `SECRET_KEY_MIN_CHARS` is refused before the app serves a request. The
    value itself is never logged.
    """
    import settings as settings_module
    raw = str(getattr(settings, "SECRET_KEY", "") or "")
    if (not raw.strip()
            or raw == settings_module.SECRET_KEY_PLACEHOLDER
            or len(raw) < settings_module.SECRET_KEY_MIN_CHARS):
        log_with_sid("startup", "error",
                     "SECRET_KEY_UNSET refusing to start: set SECRET_KEY to a random "
                     "value of at least 32 characters "
                     "(python -c \"import secrets; print(secrets.token_hex(32))\")")
        raise SystemExit(1)


def _refuse_on_missing_executor_cidr() -> None:
    """Refuse to start when the sandbox is configured but its subnet is not.

    `BackendNetworkGuard` refuses requests arriving from the sandbox's subnet,
    and it can only do that when it knows the subnet. With EXECUTOR_URL set and
    EXECUTOR_NETWORK_CIDR empty or malformed the guard would stand down
    silently, so the app does not start at all. The value is never logged.
    """
    if not str(getattr(settings, "EXECUTOR_URL", "") or "").strip():
        return
    raw = str(getattr(settings, "EXECUTOR_NETWORK_CIDR", "") or "").strip()
    network = None
    if raw:
        try:
            network = ipaddress.ip_network(raw, strict=False)
        except Exception as e:
            log_with_sid("startup", "error",
                         f"EXECUTOR_CIDR_INVALID {log_safe_text(type(e).__name__, 80)}")
            network = None
    if network is None:
        log_with_sid("startup", "error",
                     "EXECUTOR_CIDR_UNSET refusing to start: EXECUTOR_URL is set but "
                     "EXECUTOR_NETWORK_CIDR is empty or not a valid network (compose "
                     "feeds it from PDC_BACKEND_SUBNET; set EXECUTOR_URL empty only "
                     "when no analysis sandbox runs)")
        raise SystemExit(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Configuration checks come FIRST: a misconfigured install must not serve a
    # single request (the executor's secret self-check is the same pattern).
    _refuse_on_weak_secret_key()
    _refuse_on_missing_executor_cidr()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    # Configuration comes from the environment, which this code did not
    # write: escaped like any other outside text.
    log_with_sid("startup", "info", "CLIENT_STARTED",
                 data_root=log_safe_text(str(settings.DATA_ROOT), 200),
                 brain_url=log_safe_text(str(settings.BRAIN_URL), 200),
                 token_set=bool(settings.BRAIN_TENANT_TOKEN))
    # Build marker — lets an operator confirm from the logs WHICH image is
    # running (a stale image is the classic "my fix isn't live" cause). Fed by
    # the Docker build args; also served by GET /version.
    log_with_sid("startup", "info", "CLIENT_BUILD",
                 commit=log_safe_text(str(settings.BUILD_COMMIT or "dev"), 80),
                 build_time=log_safe_text(str(settings.BUILD_TIME or "unstamped"), 80),
                 started_at=log_safe_text(_STARTED_AT.isoformat(timespec="seconds"), 40))
    # Fixed local admin bootstrap (idempotent; never overwrites an existing
    # password) + the nightly database-snapshot refresh scheduler. Both are
    # lifespan-scoped on purpose: an import-time thread would leak into every
    # pytest session (the local_store sweeper lesson).
    AuthStore().ensure_local_admin()
    # Reset links: index every outstanding token once, so a probe for an
    # unknown link is answered from memory instead of a scan of every
    # account. It never raises; wrapped anyway so the boot cannot fail on it.
    try:
        AuthStore().load_reset_token_index()
    except Exception as e:
        log_with_sid("startup", "error",
                     f"RESET_TOKEN_INDEX_STARTUP_FAILED {log_safe_text(type(e).__name__, 80)}")
    if settings.CSP_REPORT_ONLY:
        log_with_sid("startup", "warning",
                     "CSP_REPORT_ONLY_ENABLED the page policy is reported, not enforced "
                     "(diagnostic setting; unset CSP_REPORT_ONLY to enforce)")
    if not (settings.PUBLIC_BASE_URL or "").strip().lower().startswith(("http://", "https://")):
        log_with_sid("startup", "error",
                     "PUBLIC_BASE_URL_UNSET password-reset and invitation mails are "
                     "disabled until PUBLIC_BASE_URL is set to the http(s) address "
                     "users type to reach this app")
    if settings.ALLOW_SELF_REGISTRATION:
        log_with_sid("startup", "warning",
                     "SELF_REGISTRATION_ENABLED any unknown address that signs in "
                     "becomes an account (hosted-demo setting; unset "
                     "ALLOW_SELF_REGISTRATION on a customer install)")
    # Offline plotly.js: materialize the pip package's bundle into static/vendor/
    # so chart iframes never need cdn.plot.ly (air-gapped LANs). Idempotent —
    # no-ops in the Docker image where the build already baked it.
    import plot_utils
    plot_utils.ensure_plotly_js_asset()
    import roles_store
    roles_store.RolesStore().migrate_manage_grants()   # 19f doc v1 -> v2
    roles_store.RolesStore().ensure_base_role()
    roles_store.RolesStore().remove_poweruser_role()   # 19e legacy cleanup
    # Generated Python runs in the analysis-sandbox container: prepare the
    # shared job directory, greet the service and sweep the job directories a
    # crashed worker left behind. Wrapped because a boot must not fail on the
    # sandbox being slow to come up — the dispatcher reports it per call.
    try:
        import executor_client
        executor_client.startup()
    except Exception as e:
        log_with_sid("startup", "error",
                     f"EXECUTOR_STARTUP_FAILED {log_safe_text(type(e).__name__, 80)}: "
                     f"{log_safe_text(str(e), 200)}")
    import db_scheduler
    db_scheduler.start()
    yield
    db_scheduler.stop()
    log_with_sid("shutdown", "info", "CLIENT_STOPPED")


class RememberMeSessionMiddleware(SessionMiddleware):
    """SessionMiddleware with an ABSOLUTE session lifetime.

    `max_age` (REMEMBER_ME_MAX_DAYS in days) is the cap, counted from the
    sign-in time `iat` that routes/auth.py stamps at every sign-in, however
    often the cookie is renewed. A cookie issued before `iat` existed takes
    its signer timestamp (its last renewal) as `iat`, written into the
    session. At `now - iat >= max_age` the session is emptied and the
    clearing cookie is sent. Sessions carrying `remember: True` (the
    "Remember me" checkbox) get a persistent cookie whose `Max-Age` is the
    REMAINING lifetime, so the browser's expiry stays sign-in + cap; all
    other sessions get a browser-session cookie (no Max-Age), refused at the
    same cap. The signer's own `max_age` check (real time since the last
    renewal) still runs first as the outer bound.
    """

    def _enforce_lifetime(self, scope, signed_at) -> None:
        """Stamp a missing `iat` from the signer timestamp; empty a session
        whose lifetime is over. Never raises (a failure leaves the session
        as it is — it is still a validly signed one)."""
        try:
            session = scope.get("session")
            if not session or not self.max_age:
                return
            iat = session.get("iat")
            if not isinstance(iat, int) or isinstance(iat, bool):
                iat = int(signed_at.timestamp())
                session["iat"] = iat
            if _session_now() - iat >= self.max_age:
                scope["session"] = {}
        except Exception as e:
            log_with_sid("session", "warning",
                         f"SESSION_LIFETIME_CHECK_FAILED {log_safe_text(type(e).__name__, 80)}")

    def _remaining_max_age(self, session) -> int:
        """Seconds left of the session's lifetime (the whole cap when it
        carries no usable `iat`)."""
        iat = session.get("iat")
        if not isinstance(iat, int) or isinstance(iat, bool):
            return int(self.max_age)
        elapsed = max(0, _session_now() - iat)
        return max(0, int(self.max_age) - elapsed)

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
                data, signed_at = self.signer.unsign(data, max_age=self.max_age,
                                                     return_timestamp=True)
                scope["session"] = _json.loads(b64decode(data))
                initial_session_was_empty = False
                self._enforce_lifetime(scope, signed_at)
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
                    # Persistent Max-Age only when the user asked to be
                    # remembered, and then only what is LEFT of the lifetime.
                    persist = bool(scope["session"].get("remember"))
                    max_age = ""
                    if persist and self.max_age:
                        max_age = f"Max-Age={self._remaining_max_age(scope['session'])}; "
                    header_value = "{session_cookie}={data}; path={path}; {max_age}{security_flags}".format(
                        session_cookie=self.session_cookie,
                        data=data.decode("utf-8"),
                        path=self.path,
                        max_age=max_age,
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


# Source addresses the backend-network guard has already refused, and CIDR
# values it could not parse — one log line each per process, so a loop inside
# the sandbox cannot flood the log.
_BACKEND_REFUSED: set = set()
_BACKEND_BAD_CIDRS: set = set()
# How much of a refused request's path the log line repeats.
_REFUSED_PATH_MAX_CHARS = 120


def _refused_path(path) -> str:
    """A request path safe to put on ONE log line. Never raises.

    `repr` of a truncated value: the ASGI server percent-decodes the path, so
    without it a request for `/x%0a…` writes whatever line it likes into the
    durable log, which is a file operators grep and trust.
    """
    try:
        return repr(str(path or "")[:_REFUSED_PATH_MAX_CHARS])
    except Exception:
        return "''"


class BackendNetworkGuard:
    """Refuse every request whose socket peer sits in the sandbox's network.

    WHY this exists: the analysis sandbox shares an internal Docker network
    with this service so the dispatcher can reach it — and a Docker network is
    BIDIRECTIONAL. Nothing in the topology stops generated Python inside the
    sandbox from opening `http://pdc-client:8000/auth/login` or the password
    reset flow, neither of which needs a session. "One direction only" cannot
    be expressed in compose, so the app refuses the range itself.

    The peer address is the one thing code inside the sandbox cannot choose:
    it is the socket's, not a header's. There is no allowlisted path — the
    sandbox never needs to call this app at all, and the container's own
    healthcheck runs over loopback.

    HONEST LIMITATION: a TRUSTED reverse proxy can rewrite the peer. uvicorn's
    `ProxyHeadersMiddleware` sets `scope["client"]` from `X-Forwarded-For` for
    every peer listed in `FORWARDED_ALLOW_IPS`, and it wraps OUTSIDE this app —
    so with `FORWARDED_ALLOW_IPS=*` the sandbox can present any address it
    likes. Never set the wildcard, and never a range that contains the backend
    subnet.
    """

    def __init__(self, app) -> None:
        self.app = app

    @staticmethod
    def _network():
        """The configured range, read at REQUEST time.

        A malformed value disables the guard and logs once (Article IV): one
        typo in one env var must never turn every request into a 500, and a
        boot must never fail on it.
        """
        raw = str(getattr(settings, "EXECUTOR_NETWORK_CIDR", "") or "").strip()
        if not raw:
            return None
        try:
            return ipaddress.ip_network(raw, strict=False)
        except Exception as e:
            if raw not in _BACKEND_BAD_CIDRS:
                _BACKEND_BAD_CIDRS.add(raw)
                log_with_sid("startup", "error",
                             f"BACKEND_CIDR_INVALID {log_safe_text(type(e).__name__, 80)}: "
                             f"{log_safe_text(str(e), 200)}")
            return None

    @staticmethod
    def _inside(host, network) -> bool:
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            # `None`, a hostname, or Starlette's TestClient default peer
            # ("testclient") — none of them is an address in the range.
            return False
        # An IPv6 listener reports an IPv4 peer as `::ffff:192.168.255.242`,
        # and that object is NOT `in` an IPv4 network — the guard would stand
        # down for the whole range. Inert while the server binds IPv4 only, so
        # mapping it here is what keeps one `--host ::` from silently turning
        # the guard off.
        addr = getattr(addr, "ipv4_mapped", None) or addr
        return addr in network

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        network = self._network()
        client = scope.get("client") or None
        host = client[0] if client else None
        if network is None or host is None or not self._inside(host, network):
            await self.app(scope, receive, send)
            return
        if host not in _BACKEND_REFUSED:
            _BACKEND_REFUSED.add(host)
            # The path is logged through `repr` and truncated: the server
            # percent-DECODES it, so a request for `/x%0a…` would otherwise
            # write a forged line into the durable log — and the caller this
            # guard exists for is the one the design assumes hostile. `repr`
            # escapes CR/LF and every other control character. Both values
            # still pass the shared helper, which is the identity for them;
            # its cap is repr's worst case (`􏿿` = 10 characters per
            # character, plus the quotes), so it never cuts the rendering.
            log_with_sid("security", "warning",
                         f"BACKEND_REQUEST_REFUSED client={log_safe_text(str(host), 64)} "
                         f"path={log_safe_text(_refused_path(scope.get('path')), _REFUSED_PATH_MAX_CHARS * 10 + 2)}")
        if scope["type"] == "websocket":
            # A websocket scope cannot be answered with an HTTP response
            # message; the refusal is a close instead. (This app serves no
            # websocket route today — the branch exists so the guard cannot
            # become the thing that raises.)
            await send({"type": "websocket.close", "code": 1008})
            return
        # Raw ASGI messages rather than a Response class: the body must stay
        # exactly this, naming neither the path, the range, nor the caller.
        await send({
            "type": "http.response.start",
            "status": 403,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "http.response.body", "body": b'{"error": "forbidden"}'})


# Paths a session that must change its password may still reach: the page
# routes (each redirects to the change form itself), static files, the two
# unauthenticated probes, and the sign-in / sign-out / reset / change / SSO
# flows. `/auth/me` answers "who is signed in" and carries nothing else.
_PASSWORD_CHANGE_OPEN_PATHS = frozenset({
    "/", "/lab", "/admin/data_sources", "/power/data_sources",
    "/health", "/version",
    "/auth/login", "/auth/logout", "/auth/change_password",
    "/auth/reset_password", "/auth/me",
})
# "/auth/reset/": a mailed reset link must work for a user who signed in
# with a temp password on the same browser.
_PASSWORD_CHANGE_OPEN_PREFIXES = ("/static/", "/c/", "/dashboards/",
                                  "/auth/microsoft", "/auth/reset/")


def _routed_path(scope) -> str:
    """The path the ROUTER matches: `scope["path"]` with the ASGI
    `root_path` removed when the server mounts the app under a prefix (a
    proxy's `--root-path`). Without a root_path it is `scope["path"]`."""
    path = str(scope.get("path") or "")
    root = str(scope.get("root_path") or "").rstrip("/")
    if root and (path == root or path.startswith(root + "/")):
        path = path[len(root):] or "/"
    return path


def _open_during_password_change(path) -> bool:
    path = str(path or "")
    return (path in _PASSWORD_CHANGE_OPEN_PATHS
            or path.startswith(_PASSWORD_CHANGE_OPEN_PREFIXES))


class PasswordChangeGate:
    """Refuse the API to a session that must change its password first.

    Signing in with a temporary password (a reset, or the administrator's
    bootstrap password) marks the session `must_change_password`. The page
    routes redirect such a session to the change form, but the JSON APIs used
    to ignore the flag, so the forced change could be skipped by calling them
    directly. Every path outside `_open_during_password_change` answers
    `403 {"error": "Password change required", "code":
    "PASSWORD_CHANGE_REQUIRED"}` until the change is made.

    Pure ASGI and registered INSIDE the session middleware, which is what
    fills `scope["session"]` before this layer reads it.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        session = scope.get("session") or {}
        if (not (session.get("email") and session.get("must_change_password"))
                or _open_during_password_change(_routed_path(scope))):
            await self.app(scope, receive, send)
            return
        response = JSONResponse({"error": "Password change required",
                                 "code": "PASSWORD_CHANGE_REQUIRED"}, status_code=403)
        await response(scope, receive, send)


class SessionGenerationGate:
    """End a session whose account changed its password since it signed in.

    Sign-in stamps the account's `session_generation` into the session as
    `gen`; a password change or a used reset link writes a new one (the
    session that made a change re-stamps itself). For every session carrying
    an `email`, a `gen` (absent = "") that differs from the account's value
    EMPTIES the session in place: the request continues as anonymous (pages
    redirect to `/`, APIs answer 401) and the session middleware sends the
    clearing cookie. A cookie without `gen` on an account that has no
    generation matches, so an upgrade signs nobody out. An account that no
    longer exists (it reads `SESSION_GEN_REMOVED`) ends the session whatever
    `gen` it carries — a live account never reads that value. The account's
    value is cached in-process (local_store), never read per request.

    Pure ASGI, registered INSIDE the session middleware (which fills
    `scope["session"]`) and outside the password-change gate. A failure is
    logged and lets the request through (the cookie is still a signed one).
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            try:
                session = scope.get("session")
                email = session.get("email") if session else None
                current = AuthStore().session_generation(email) if email else None
                if email and (current == SESSION_GEN_REMOVED
                              or session.get("gen", "") != current):
                    session.clear()
                    log_with_sid("session", "info",
                                 f"SESSION_ENDED_BY_PASSWORD_CHANGE "
                                 f"email={log_safe_text(str(email), 254)}")
            except Exception as e:
                log_with_sid("session", "error",
                             f"SESSION_GENERATION_CHECK_FAILED {log_safe_text(type(e).__name__, 80)}")
        await self.app(scope, receive, send)


# The enterprise page policy. `{nonce}` is the per-response nonce every
# inline <script> of the templates carries; nothing else inline may run.
# Styles stay 'unsafe-inline' (the pages and the styled tables use inline
# style attributes); frames are same-origin documents only (the chart route).
_CSP_BASE = {
    "default-src": ["'self'"],
    "script-src": ["'self'", "'nonce-{nonce}'"],
    "style-src": ["'self'", "'unsafe-inline'"],
    "img-src": ["'self'", "data:", "blob:"],
    "font-src": ["'self'", "data:"],
    "connect-src": ["'self'"],
    "frame-src": ["'self'"],
    "frame-ancestors": ["'self'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'"],
    "object-src": ["'none'"],
}
# Hosted-demo widenings, each behind the setting that loads the thing.
_CSP_THIRD_PARTY = {
    "script-src": ["https://www.googletagmanager.com", "https://cdn.paddle.com"],
    "connect-src": ["https://*.google-analytics.com", "https://*.analytics.google.com",
                    "https://www.googletagmanager.com", "https://*.paddle.com"],
    "frame-src": ["https://*.paddle.com"],
    "img-src": ["https://*.google-analytics.com", "https://www.googletagmanager.com"],
}
_CSP_DIRECT_UPLOAD = {"connect-src": ["https://storage.googleapis.com"]}
_CSP_FAILED_LOGGED = False


def _render_policy(directives: dict, nonce: str) -> str:
    return "; ".join(
        " ".join([name] + [token.replace("{nonce}", nonce) for token in tokens])
        for name, tokens in directives.items())


def _build_csp(nonce: str) -> str:
    """The policy for one response, built from the settings at REQUEST time
    (the suite and an operator's restart both change them). Never raises: a
    failure logs once and answers the enterprise default — never no policy."""
    global _CSP_FAILED_LOGGED
    try:
        directives = {name: list(tokens) for name, tokens in _CSP_BASE.items()}
        widenings = []
        if settings.ENABLE_THIRD_PARTY_SCRIPTS:
            widenings.append(_CSP_THIRD_PARTY)
        if gcs_upload.enabled():
            widenings.append(_CSP_DIRECT_UPLOAD)
        for extra in widenings:
            for name, tokens in extra.items():
                directives[name].extend(t for t in tokens if t not in directives[name])
        return _render_policy(directives, nonce)
    except Exception as e:
        if not _CSP_FAILED_LOGGED:
            _CSP_FAILED_LOGGED = True
            log_with_sid("security", "error",
                         f"CSP_BUILD_FAILED {log_safe_text(type(e).__name__, 80)}")
        return _render_policy(_CSP_BASE, nonce)


class ContentSecurityPolicy:
    """Nonce-based Content-Security-Policy on every HTML response.

    Per http request a fresh nonce goes into `scope["state"]["csp_nonce"]`
    (read by handlers and templates as `request.state.csp_nonce`); when the
    response starts with a `text/html` content type the policy header is
    added (`Content-Security-Policy`, or `...-Report-Only` while the
    `CSP_REPORT_ONLY` diagnostic is on). JSON, event streams, static files
    and redirects pass untouched, and so does an HTML response that already
    carries a `Content-Security-Policy` of its own (the chart route,
    `routes/charts.py`): nothing is added to it, report-only included.

    Pure ASGI, registered INSIDE the backend-network guard (a refusal there
    never reaches this layer) and outside the session middleware.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        nonce = secrets.token_urlsafe(16)
        state = scope.setdefault("state", {})
        state["csp_nonce"] = nonce

        async def send_with_policy(message) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                content_type = (headers.get("content-type") or "").lower()
                # A response that carries its own policy (the chart route)
                # keeps it: no page policy, no report-only variant on top.
                has_own = "content-security-policy" in headers
                if content_type.startswith("text/html") and not has_own:
                    name = ("Content-Security-Policy-Report-Only"
                            if settings.CSP_REPORT_ONLY else "Content-Security-Policy")
                    headers.append(name, _build_csp(nonce))
            await send(message)

        await self.app(scope, receive, send_with_policy)


class UploadTooLarge(HTTPException):
    """Raised from UploadByteCap's counting receive. An HTTPException subclass
    because FastAPI re-raises those unchanged out of body parsing (anything
    else becomes a generic 400); its own handler answers the same body as the
    Content-Length refusal."""

    def __init__(self, max_bytes: int):
        super().__init__(status_code=413, detail="Upload too large")
        self.max_bytes = int(max_bytes)


async def upload_too_large_handler(request: Request, exc: UploadTooLarge):
    return JSONResponse({"error": "Upload too large", "max_bytes": exc.max_bytes},
                        status_code=413, headers={"connection": "close"})


# The multipart routes whose body UploadByteCap bounds.
_UPLOAD_CAP_PATH_RE = re.compile(r"^/(?:upload|api/chat/[^/]+/probe_columns)$")


class UploadByteCap:
    """Pure ASGI: bounds the request body of the multipart upload routes at
    `settings.MAX_UPLOAD_BYTES` (read per request).

    A declared `Content-Length` above the cap is answered 413 before the app
    (or the multipart parser) runs; uvicorn never delivers more bytes than a
    declared length. A body without one (chunked) is counted as it is read,
    and crossing the cap raises a 413 `HTTPException`, which FastAPI passes
    through its body parsing unchanged.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http" or scope.get("method") != "POST"
                or not _UPLOAD_CAP_PATH_RE.match(_routed_path(scope))):
            await self.app(scope, receive, send)
            return
        try:
            cap = int(settings.MAX_UPLOAD_BYTES)
        except Exception as e:
            log_with_sid("upload", "error",
                         f"UPLOAD_CAP_SETTING_INVALID {log_safe_text(type(e).__name__, 80)}")
            cap = 100 * 1024 * 1024
        declared = None
        for name, value in scope.get("headers") or []:
            if name == b"content-length":
                try:
                    declared = int(value.decode("latin-1").strip())
                except ValueError:
                    declared = None
        if declared is not None and declared > cap:
            log_with_sid("upload", "warning",
                         f"UPLOAD_TOO_LARGE declared_bytes={int(declared)} cap={int(cap)}")
            body = json.dumps({"error": "Upload too large", "max_bytes": cap}).encode()
            await send({"type": "http.response.start", "status": 413,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str(len(body)).encode()),
                                    (b"connection", b"close")]})
            await send({"type": "http.response.body", "body": body})
            return
        seen = {"bytes": 0}

        async def counted_receive():
            message = await receive()
            if message.get("type") == "http.request":
                seen["bytes"] += len(message.get("body") or b"")
                if seen["bytes"] > cap:
                    log_with_sid("upload", "warning",
                                 f"UPLOAD_TOO_LARGE streamed_bytes={int(seen['bytes'])} cap={int(cap)}")
                    raise UploadTooLarge(cap)
            return message

        await self.app(scope, counted_receive, send)


# The four HTML forms (their POSTs are form-encoded and carry a session-bound
# CSRF token, checked by routes/auth.py) and the two multipart upload routes.
_FORM_POST_PATHS = frozenset({"/auth/login", "/auth/reset_password", "/auth/change_password"})
_FORM_POST_PREFIX = "/auth/reset/"
_STATE_CHANGING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MEDIA_REFUSED: set = set()


class JsonContentTypeGate:
    """Pure ASGI CSRF control for everything that is not an HTML form.

    A cross-site page can send a POST with a "simple" content type
    (text/plain, form-encoded, multipart) without a CORS preflight, and the
    browser attaches the cookie wherever SameSite allows it. So every
    state-changing request must declare `application/json` — a type a
    cross-site page cannot send without a preflight this app never answers —
    except the four HTML forms (form-encoded, token-checked in routes/auth.py)
    and the two multipart upload routes. Anything else: 415, logged once per
    path.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") not in _STATE_CHANGING:
            await self.app(scope, receive, send)
            return
        path = _routed_path(scope)
        ctype = ""
        for name, value in scope.get("headers") or []:
            if name == b"content-type":
                ctype = value.decode("latin-1").split(";", 1)[0].strip().lower()
        if path in _FORM_POST_PATHS or path.startswith(_FORM_POST_PREFIX):
            allowed = ctype in ("application/x-www-form-urlencoded", "multipart/form-data")
        elif _UPLOAD_CAP_PATH_RE.match(path):
            allowed = ctype == "multipart/form-data"
        else:
            allowed = ctype == "application/json"
        if allowed:
            await self.app(scope, receive, send)
            return
        if path not in _MEDIA_REFUSED and len(_MEDIA_REFUSED) < 1000:
            _MEDIA_REFUSED.add(path)
            log_with_sid("csrf", "warning",
                         f"CSRF_MEDIA_TYPE_REFUSED path={log_safe_text(path, 200)} "
                         f"content_type={log_safe_text(ctype or '-', 80)}")
        body = json.dumps({"error": "Unsupported Media Type"}).encode()
        await send({"type": "http.response.start", "status": 415,
                    "headers": [(b"content-type", b"application/json"),
                                (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})


# The absolute session lifetime (seconds): RememberMeSessionMiddleware's cap.
_REMEMBER_ME_MAX_AGE = settings.REMEMBER_ME_MAX_DAYS * 86400

app = FastAPI(title="PowerDataChat Client (enterprise)", version="1.0", lifespan=lifespan)
app.add_exception_handler(UploadTooLarge, upload_too_large_handler)
# Registered FIRST, so it sits INSIDE the session middleware below and sees
# the unsigned session.
app.add_middleware(PasswordChangeGate)
# Between the session middleware and the password gate: a session ended by
# a password change is emptied before anything else reads it.
app.add_middleware(SessionGenerationGate)
app.add_middleware(RememberMeSessionMiddleware, secret_key=settings.SECRET_KEY,
                   same_site="lax", max_age=_REMEMBER_ME_MAX_AGE,
                   https_only=settings.SESSION_HTTPS_ONLY)
# Outside the session (needs none): an oversized upload is refused before a
# session is unsigned or the multipart body is parsed.
app.add_middleware(UploadByteCap)
# Outside the session too: a state-changing request of the wrong media type
# never reaches a session or a route.
app.add_middleware(JsonContentTypeGate)
# Inside the guard, outside the session: adds the page policy to every HTML
# response (and so also covers the password gate and the session layer).
app.add_middleware(ContentSecurityPolicy)
# LAST registered = OUTERMOST layer (Starlette inserts each at index 0 and
# builds the stack from the front), which is what the guard needs: a request
# from the sandbox's range must be refused before a session is even unsigned.
app.add_middleware(BackendNetworkGuard)

# Static assets (copied byte-for-byte from the B2C app)
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

app.include_router(auth_router)
app.include_router(upload_router)
app.include_router(schema_router)
app.include_router(chat_router)
app.include_router(report_router)
app.include_router(dashboards_router)
app.include_router(charts_router)
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
        log_with_sid("sso", "warning",
                     f"SSO_LANDING_CHECK_FAILED: {log_safe_text(str(e), 200)}")
        sso_enabled = sso_auto = False
    if sso_enabled and sso_auto and request.query_params.get("local") != "1":
        return RedirectResponse(url="/auth/microsoft", status_code=302)
    info = ("Your password has been updated. Sign in with it."
            if request.query_params.get("reset") == "done" else None)
    from routes.auth import csrf_token
    return templates.TemplateResponse(
        request,
        "auth_landing.html",
        {"request": request, "error": None, "password_error": None,
         "info": info, "email": "", "sso_enabled": sso_enabled,
         "self_registration": bool(settings.ALLOW_SELF_REGISTRATION),
         "csrf": csrf_token(request)},
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
        log_with_sid(email, "warning", f"POWER_FLAG_FAILED: {log_safe_text(str(e), 200)}")
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
        log_with_sid(email, "warning", f"ADMIN_FLAG_FAILED: {log_safe_text(str(e), 200)}")
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
        request,
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
            # Direct-to-GCS large-file branch in dashboard.js runs ONLY when
            # GCS_UPLOAD_BUCKET is set (Cloud Run demo); false on every
            # customer install => multipart /upload for all sizes.
            "direct_upload_enabled": gcs_upload.enabled(),
            # Google Analytics + the Paddle widget. False on every customer
            # install => the page loads nothing from a third-party origin.
            "third_party_scripts": settings.ENABLE_THIRD_PARTY_SCRIPTS,
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
        log_with_sid(email, "error",
                     f"DEEPLINK_LOOKUP_FAILED: {log_safe_text(str(e), 200)}",
                     conv_id=log_safe_text(str(conv_id), 80))
        return RedirectResponse(url="/lab", status_code=302)
    if not chat_id:
        # `conv_id` is a path segment: escaped on every line.
        log_with_sid(email, "info", "DEEPLINK_CONV_NOT_FOUND",
                     conv_id=log_safe_text(str(conv_id), 80))
        return RedirectResponse(url="/lab", status_code=302)
    log_with_sid(email, "info", "OPEN_CONV_DEEPLINK",
                 conv_id=log_safe_text(str(conv_id), 80), chat_id=chat_id)
    ts = int(time.time())
    prof = _profile_context(email)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "ts": ts,
            "default_days": settings.CHAT_ACTIVE_DEFAULT_DAYS,
            "max_days": settings.CHAT_ACTIVE_MAX_DAYS,
            "is_admin": False,
            # Direct-to-GCS large-file branch in dashboard.js runs ONLY when
            # GCS_UPLOAD_BUCKET is set (Cloud Run demo); false on every
            # customer install => multipart /upload for all sizes.
            "direct_upload_enabled": gcs_upload.enabled(),
            # Google Analytics + the Paddle widget. False on every customer
            # install => the page loads nothing from a third-party origin.
            "third_party_scripts": settings.ENABLE_THIRD_PARTY_SCRIPTS,
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
        log_with_sid(email, "error",
                     f"DASH_PAGE_LOOKUP_FAILED: {log_safe_text(str(e), 200)}",
                     dash_id=log_safe_text(str(dash_id), 80))
        return RedirectResponse(url="/lab", status_code=302)
    if doc is None:
        # `dash_id` is a path segment: escaped on every line.
        log_with_sid(email, "info", "DASH_PAGE_NOT_FOUND",
                     dash_id=log_safe_text(str(dash_id), 80))
        return RedirectResponse(url="/lab", status_code=302)
    log_with_sid(email, "info", "OPEN_DASHBOARD_UI",
                 dash_id=log_safe_text(str(dash_id), 80))
    ts = int(time.time())
    prof = _profile_context(email)
    return templates.TemplateResponse(
        request,
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
        request,
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
        request,
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
        log_with_sid(email, "warning",
                     f"POWER_SCOPE_SUMMARY_FAILED: {log_safe_text(str(e), 200)}")
        return "", False


@app.get("/health")
async def health():
    """Still 200 when the analysis sandbox is down — that is the point.

    `executor_reachable` is the CACHED verdict of the last conversation with
    the sandbox (startup handshake, every dispatch, a short background
    refresh), never a live call: an unresolvable sandbox name costs ~8 s in
    `getaddrinfo` before any HTTP timeout applies, this handler is async while
    the clients are sync, and the container's healthcheck allows 5 s — so a
    probe here would mark the web container unhealthy exactly when an operator
    needs the page to say which half is down. `None` means nobody has checked
    yet, which is not the same answer as "down".
    """
    import executor_client
    from brain_client import health as brain_health

    executor_ok, executor_checked_at = executor_client.reachable()
    return {
        "status": "ok",
        "service": "client",
        "brain_reachable": brain_health() if settings.BRAIN_TENANT_TOKEN else None,
        "tenant_token_configured": bool(settings.BRAIN_TENANT_TOKEN),
        "build_commit": settings.BUILD_COMMIT or None,
        "executor_reachable": executor_ok,
        "executor_checked_at": executor_checked_at or None,
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
