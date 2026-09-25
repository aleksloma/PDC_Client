"""routes/charts.py — chart documents served from their own route.

A chart document (plotly HTML: a full document with inline scripts) is not
written into its frame by the page. The page registers it here and loads the
URL it gets back:

* `POST /api/charts` JSON `{html}` — signed-in only; `html` is a non-empty
  string of at most `MAX_DOC_CHARS` characters. The document goes into a
  bounded in-memory store and the answer is `{"url": "/charts/<token>"}`.
* `GET /charts/{token}` — the registered document, when the token is valid,
  younger than `TOKEN_MAX_AGE_S` and issued to the signed-in user, and the
  store still holds the entry; `404` otherwise, whatever the reason.

The token is signed with `SECRET_KEY` (itsdangerous, salt "chart-frame") and
names the store id only (the URL ends up in browser history and access logs);
the entry holds the registering user's email, which the GET compares with the
session. Any chart can be served
this way — a streamed chart before its history row exists, or a refreshed
chart that is never persisted — because the lookup is by store id, not by a
stored record.

The served document carries ONE policy of its own: `sandbox allow-scripts`
(an opaque origin, whatever the frame says), the document's inline scripts
run, and the only external fetch possible is a script from this server
(e.g. the offline Plotly bundle); `'unsafe-eval'` is granted to this
document only (plotly's WebGL traces need it), never to an application
page. There is no nonce and the registered HTML is served byte-for-byte.
The page policy middleware in `app.py` leaves a response that already
carries a policy alone.

The store lives in this process's memory. That is correct because the web
server runs ONE worker (`--workers 1` in the Dockerfile); a second worker
would not see the first one's entries. Entries expire after `ENTRY_TTL_S`;
sizes are counted in UTF-8 bytes: each user holds at most `USER_MAX_BYTES`
and the store at most `STORE_MAX_TOTAL_BYTES`. An insert evicts, in this
order, expired entries, then the SAME user's oldest while that user is over
the share, then the globally oldest while the store is over its total —
never the new entry — so one user's registrations cannot push out another
user's live chart while the store has room for both shares. A restart
empties it; the page simply registers the chart again on its next render.

All bounds are module attributes read at CALL time. Nothing here logs the
HTML or the token.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from itsdangerous import URLSafeTimedSerializer

from exec_transport import log_safe_text
from logger_utils import log_with_sid
from settings import settings

router = APIRouter()

TOKEN_MAX_AGE_S = 1800
ENTRY_TTL_S = 1800
MAX_DOC_CHARS = 5_000_000
STORE_MAX_TOTAL_BYTES = 200_000_000
USER_MAX_BYTES = 40_000_000

_SALT = "chart-frame"

_LOCK = threading.Lock()
# id -> (email, html, created_monotonic, utf8_bytes), oldest first.
_STORE: "OrderedDict[str, tuple]" = OrderedDict()
_STORE_BYTES = 0
_USER_BYTES: dict = {}


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.SECRET_KEY, salt=_SALT)


def _norm_email(value) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _not_found() -> Response:
    return Response("Not found", status_code=404, media_type="text/plain")


def _drop(entry_id: str) -> None:
    """Remove one entry; caller holds `_LOCK`."""
    global _STORE_BYTES
    entry = _STORE.pop(entry_id, None)
    if entry is not None:
        _STORE_BYTES -= entry[3]
        left = _USER_BYTES.get(entry[0], 0) - entry[3]
        if left > 0:
            _USER_BYTES[entry[0]] = left
        else:
            _USER_BYTES.pop(entry[0], None)


def _store_put(email: str, html: str) -> str:
    """Store a document and return its id. Evicts expired entries, then the
    same user's oldest while the user is over `USER_MAX_BYTES`, then the
    globally oldest while the store is over `STORE_MAX_TOTAL_BYTES`; the new
    entry itself is never evicted."""
    global _STORE_BYTES
    entry_id = secrets.token_hex(16)
    size = len(html.encode("utf-8"))
    now = time.monotonic()
    with _LOCK:
        ttl = ENTRY_TTL_S
        for old_id in [k for k, v in _STORE.items() if now - v[2] > ttl]:
            _drop(old_id)
        _STORE[entry_id] = (email, html, now, size)
        _STORE_BYTES += size
        _USER_BYTES[email] = _USER_BYTES.get(email, 0) + size
        share = USER_MAX_BYTES
        if _USER_BYTES[email] > share:
            for old_id in [k for k, v in _STORE.items() if v[0] == email and k != entry_id]:
                if _USER_BYTES.get(email, 0) <= share:
                    break
                _drop(old_id)
        cap = STORE_MAX_TOTAL_BYTES
        while _STORE_BYTES > cap and len(_STORE) > 1:
            oldest = next(iter(_STORE))
            if oldest == entry_id:
                break
            _drop(oldest)
    return entry_id


def _store_get(entry_id: str):
    """`(email, html)` of a live entry, else None (an expired one is removed)."""
    now = time.monotonic()
    with _LOCK:
        entry = _STORE.get(entry_id)
        if entry is None:
            return None
        if now - entry[2] > ENTRY_TTL_S:
            _drop(entry_id)
            return None
        return entry[0], entry[1]


# No nonce: a nonce on every script tag would let the document load script
# from any host. Inside this sandboxed document every inline script is meant
# to run, so inline is allowed and external scripts are limited to this server.
_POLICY = ("sandbox allow-scripts; default-src 'none'; "
           "script-src 'self' 'unsafe-eval' 'unsafe-inline'; "
           "style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'")


@router.post("/api/charts")
async def register_chart(request: Request):
    email = _norm_email(request.session.get("email"))
    if not email:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        body = await request.json()
    except Exception as e:
        log_with_sid("charts", "info",
                     f"CHART_REGISTER_REFUSED reason=bad_json "
                     f"error={log_safe_text(type(e).__name__, 80)}")
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
    html = body.get("html") if isinstance(body, dict) else None
    if not isinstance(html, str) or not html:
        return JSONResponse({"error": "html must be a non-empty string"}, status_code=400)
    limit = MAX_DOC_CHARS
    if len(html) > limit:
        log_with_sid("charts", "info",
                     f"CHART_REGISTER_REFUSED reason=too_large chars={len(html)} "
                     f"limit={int(limit)}")
        return JSONResponse({"error": "Chart document is too large"}, status_code=400)
    try:
        entry_id = _store_put(email, html)
        token = _serializer().dumps({"i": entry_id})
    except Exception as e:
        log_with_sid("charts", "error",
                     f"CHART_REGISTER_FAILED error={log_safe_text(type(e).__name__, 80)}")
        return JSONResponse({"error": "Could not register the chart"}, status_code=500)
    return JSONResponse({"url": "/charts/" + token})


@router.get("/charts/{token}")
async def serve_chart(token: str, request: Request):
    email = _norm_email(request.session.get("email"))
    if not email:
        return _not_found()
    try:
        data = _serializer().loads(token, max_age=TOKEN_MAX_AGE_S)
    except Exception as e:
        log_with_sid("charts", "info",
                     f"CHART_TOKEN_REJECTED error={log_safe_text(type(e).__name__, 80)}")
        return _not_found()
    if not isinstance(data, dict):
        return _not_found()
    entry_id = data.get("i")
    if not isinstance(entry_id, str):
        return _not_found()
    entry = _store_get(entry_id)
    if entry is None:
        return _not_found()
    if _norm_email(entry[0]) != email:
        log_with_sid("charts", "info", "CHART_TOKEN_REJECTED reason=owner_mismatch")
        return _not_found()
    return HTMLResponse(entry[1], headers={
        "Content-Security-Policy": _POLICY,
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
    })
