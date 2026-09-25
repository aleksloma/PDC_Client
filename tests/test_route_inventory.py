"""The committed route inventory, and the forced-password-change gate over it.

`EXPECTED_ROUTES` is the sorted list of every `(method, path)` the app
serves (HEAD excluded; the `/static` mount and the framework's own docs
routes excluded). Adding, removing or renaming a route fails
`test_the_route_inventory_is_committed` until this list is updated in the
same change — the point is that the HTTP surface never changes silently,
and every new route is looked at against the gate below.

The gate (`app.PasswordChangeGate`): a session that must change its
password gets `403 {"code": "PASSWORD_CHANGE_REQUIRED"}` from EVERY route
outside the open set (`_PASSWORD_CHANGE_OPEN_PATHS` /
`_PASSWORD_CHANGE_OPEN_PREFIXES`), and never from a route inside it. The open
set itself is pinned, so widening it is a reviewed test change. The gate
matches the path the APPLICATION sees: behind a proxy that mounts the app
under a `root_path`, the prefix is stripped before matching.

Offline, real app: DATA_ROOT is tmp_path, the brain calls a sign-in /
reset could make are stubbed, the TestClient is built WITHOUT the context
manager, https base_url because the session cookie is Secure-flagged.
"""
import asyncio
import re

import pytest
from starlette.routing import Mount
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings

EXPECTED_ROUTES = [
    ('GET', '/'),
    ('GET', '/admin/data_sources'),
    ('GET', '/api/admin/audit'),
    ('GET', '/api/admin/connections'),
    ('GET', '/api/admin/connections/{cid}/schemas'),
    ('GET', '/api/admin/connections/{cid}/tables'),
    ('GET', '/api/admin/dialects'),
    ('GET', '/api/admin/my_roles'),
    ('GET', '/api/admin/refresh_settings'),
    ('GET', '/api/admin/relations/recommendations'),
    ('GET', '/api/admin/roles'),
    ('GET', '/api/admin/sso'),
    ('GET', '/api/admin/tables'),
    ('GET', '/api/admin/users'),
    ('GET', '/api/chat/{chat_id}/auto_analysis/download'),
    ('GET', '/api/chat/{chat_id}/auto_analysis/status'),
    ('GET', '/api/chat/{chat_id}/conversation/{conv_id}/history'),
    ('GET', '/api/chat/{chat_id}/conversation/{conv_id}/status'),
    ('GET', '/api/chat/{chat_id}/file_fingerprints'),
    ('GET', '/api/chat/{chat_id}/full_table/{key}'),
    ('GET', '/api/chat/{chat_id}/history'),
    ('GET', '/api/chat/{chat_id}/publish-status'),
    ('GET', '/api/chat/{chat_id}/schema'),
    ('GET', '/api/chat/{chat_id}/share'),
    ('GET', '/api/chat/{chat_id}/suggested-questions'),
    ('GET', '/api/chat/{chat_id}/welcome'),
    ('GET', '/api/dashboards'),
    ('GET', '/api/dashboards/{dash_id}'),
    ('GET', '/api/db_tables'),
    ('GET', '/auth/active_chats'),
    ('GET', '/auth/change_password'),
    ('GET', '/auth/conversations'),
    ('GET', '/auth/me'),
    ('GET', '/auth/microsoft'),
    ('GET', '/auth/microsoft/callback'),
    ('GET', '/auth/profile'),
    ('GET', '/auth/reset/{token}'),
    ('GET', '/auth/subscription'),
    ('GET', '/c/{conv_id}'),
    ('GET', '/charts/{token}'),
    ('GET', '/dashboards/{dash_id}'),
    ('GET', '/health'),
    ('GET', '/lab'),
    ('GET', '/paddle/config'),
    ('GET', '/power/data_sources'),
    ('GET', '/schema'),
    ('GET', '/schema_common_fields'),
    ('GET', '/schema_details'),
    ('GET', '/version'),
    ('POST', '/add_data_to_chat'),
    ('POST', '/api/admin/connections'),
    ('POST', '/api/admin/connections/test'),
    ('POST', '/api/admin/connections/{cid}'),
    ('POST', '/api/admin/connections/{cid}/delete'),
    ('POST', '/api/admin/connections/{cid}/refresh'),
    ('POST', '/api/admin/refresh_settings'),
    ('POST', '/api/admin/relations/accept'),
    ('POST', '/api/admin/relations/analyze_sql'),
    ('POST', '/api/admin/relations/delete'),
    ('POST', '/api/admin/relations/dismiss'),
    ('POST', '/api/admin/relations/graph'),
    ('POST', '/api/admin/relations/recommendations/accept'),
    ('POST', '/api/admin/relations/recommendations/classify'),
    ('POST', '/api/admin/relations/recommendations/status'),
    ('POST', '/api/admin/relations/scan'),
    ('POST', '/api/admin/relations/wizard_suggest'),
    ('POST', '/api/admin/roles'),
    ('POST', '/api/admin/roles/{rid}'),
    ('POST', '/api/admin/roles/{rid}/delete'),
    ('POST', '/api/admin/schedule_preview'),
    ('POST', '/api/admin/sso/disable'),
    ('POST', '/api/admin/sso/enable'),
    ('POST', '/api/admin/sso/save'),
    ('POST', '/api/admin/sso/test'),
    ('POST', '/api/admin/tables'),
    ('POST', '/api/admin/tables/draft_descriptions'),
    ('POST', '/api/admin/tables/introspect'),
    ('POST', '/api/admin/tables/{tid}'),
    ('POST', '/api/admin/tables/{tid}/delete'),
    ('POST', '/api/admin/tables/{tid}/dismiss_drift'),
    ('POST', '/api/admin/tables/{tid}/refresh'),
    ('POST', '/api/admin/tables/{tid}/schedule'),
    ('POST', '/api/admin/users/invite'),
    ('POST', '/api/admin/users/set_permission'),
    ('POST', '/api/admin/users/set_role'),
    ('POST', '/api/charts'),
    ('POST', '/api/chat/{chat_id}/auto_analysis/start'),
    ('POST', '/api/chat/{chat_id}/chat/stream'),
    ('POST', '/api/chat/{chat_id}/conversation/{conv_id}/download_pptx'),
    ('POST', '/api/chat/{chat_id}/conversation/{conv_id}/download_report'),
    ('POST', '/api/chat/{chat_id}/conversation/{conv_id}/stop'),
    ('POST', '/api/chat/{chat_id}/deactivate'),
    ('POST', '/api/chat/{chat_id}/download_excel/{key}'),
    ('POST', '/api/chat/{chat_id}/edit-regenerate'),
    ('POST', '/api/chat/{chat_id}/export_excel'),
    ('POST', '/api/chat/{chat_id}/export_plotly_png'),
    ('POST', '/api/chat/{chat_id}/generate_filename'),
    ('POST', '/api/chat/{chat_id}/probe_columns'),
    ('POST', '/api/chat/{chat_id}/publish'),
    ('POST', '/api/chat/{chat_id}/refresh_item'),
    ('POST', '/api/chat/{chat_id}/schema'),
    ('POST', '/api/chat/{chat_id}/share'),
    ('POST', '/api/chat/{chat_id}/unpublish'),
    ('POST', '/api/dashboards'),
    ('POST', '/api/dashboards/{dash_id}/delete'),
    ('POST', '/api/dashboards/{dash_id}/layout'),
    ('POST', '/api/dashboards/{dash_id}/rename'),
    ('POST', '/api/dashboards/{dash_id}/share'),
    ('POST', '/api/dashboards/{dash_id}/tiles'),
    ('POST', '/api/dashboards/{dash_id}/tiles/{tile_id}/refresh'),
    ('POST', '/api/dashboards/{dash_id}/tiles/{tile_id}/remove'),
    ('POST', '/api/dashboards/{dash_id}/tiles/{tile_id}/update'),
    ('POST', '/api/paddle/subscription/cancel'),
    ('POST', '/api/paddle/subscription/preview'),
    ('POST', '/api/paddle/subscription/reactivate'),
    ('POST', '/api/paddle/subscription/update'),
    ('POST', '/api/paddle/subscription/update-payment'),
    ('POST', '/auth/active_chats/pin'),
    ('POST', '/auth/active_chats/rename'),
    ('POST', '/auth/change_password'),
    ('POST', '/auth/conversations/delete'),
    ('POST', '/auth/conversations/rename'),
    ('POST', '/auth/conversations/{conv_id}/publish'),
    ('POST', '/auth/conversations/{conv_id}/share'),
    ('POST', '/auth/conversations/{conv_id}/unpublish'),
    ('POST', '/auth/login'),
    ('POST', '/auth/logout'),
    ('POST', '/auth/password'),
    ('POST', '/auth/profile/update'),
    ('POST', '/auth/reset/{token}'),
    ('POST', '/auth/reset_password'),
    ('POST', '/auth/subscription'),
    ('POST', '/generate_chatdata'),
    ('POST', '/new_session'),
    ('POST', '/schema'),
    ('POST', '/schema_autofill_full'),
    ('POST', '/schema_common_fields'),
    ('POST', '/session/db_tables'),
    ('POST', '/upload'),
    ('POST', '/upload/finalize'),
    ('POST', '/upload/init'),
    ('POST', '/upload_from_url'),
]

# The paths a session that must change its password may still reach, exactly
# as documented: the page routes (each redirects to the change form itself),
# the two probes, sign-in / sign-out / reset / change, "who am I", static
# files, the deep links and the SSO flow.
EXPECTED_OPEN_PATHS = frozenset({
    "/", "/lab", "/admin/data_sources", "/power/data_sources",
    "/health", "/version",
    "/auth/login", "/auth/logout", "/auth/change_password",
    "/auth/reset_password", "/auth/me",
})
# "/auth/reset/" (Task 9, D9-8): the emailed reset link must work for a user
# who signed in with a temp password on the same browser. The walk below
# requests it as /auth/reset/x, an invalid link, which answers 404 -- not the
# gate's 403, which is exactly what an open prefix means.
EXPECTED_OPEN_PREFIXES = ("/static/", "/c/", "/dashboards/", "/auth/microsoft",
                          "/auth/reset/")

GATE_BODY = {"error": "Password change required", "code": "PASSWORD_CHANGE_REQUIRED"}
FLAGGED = "inventory-flagged@x.com"
FLAGGED_PW = "Temp-passw0rd!"


def _docs_paths() -> set:
    app = app_mod.app
    paths = {app.docs_url, app.redoc_url, app.openapi_url}
    if app.docs_url:
        paths.add(app.docs_url + "/oauth2-redirect")
    return {p for p in paths if p}


def _walk(routes, prefix=""):
    """(method, path) for every endpoint, descending into included routers
    (both the flattened and the lazily-included router shapes)."""
    for route in routes:
        if isinstance(route, Mount):
            continue                      # /static
        original = getattr(route, "original_router", None)
        if original is not None:
            ctx = getattr(route, "include_context", None)
            yield from _walk(original.routes, prefix + (getattr(ctx, "prefix", "") or ""))
            continue
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or not methods:
            continue
        full = prefix + path
        if full in _docs_paths():
            continue
        for method in methods:
            if method != "HEAD":
                yield (method, full)


def _live_routes() -> list:
    return sorted(set(_walk(app_mod.app.routes)))


def _is_open(path: str) -> bool:
    return path in EXPECTED_OPEN_PATHS or path.startswith(EXPECTED_OPEN_PREFIXES)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


# ---------------------------------------------------------------------------
# (i) the inventory
# ---------------------------------------------------------------------------
def test_the_walker_sees_routers_and_skips_the_static_mount():
    rows = _live_routes()
    assert ("GET", "/health") in rows
    assert ("POST", "/api/chat/{chat_id}/chat/stream") in rows, "router routes missing"
    assert not any(p.startswith("/static") for _, p in rows), rows
    assert not any(m == "HEAD" for m, _ in rows)
    assert not (_docs_paths() & {p for _, p in rows})


def test_the_route_inventory_is_committed():
    live = _live_routes()
    added = sorted(set(live) - set(EXPECTED_ROUTES))
    removed = sorted(set(EXPECTED_ROUTES) - set(live))
    assert (added, removed) == ([], []), (
        f"routes added: {added}; routes removed: {removed} - update EXPECTED_ROUTES "
        f"and check each new route against the password-change gate")
    assert EXPECTED_ROUTES == sorted(EXPECTED_ROUTES), "keep EXPECTED_ROUTES sorted"
    assert len(EXPECTED_ROUTES) == len(set(EXPECTED_ROUTES)), "duplicate rows"


# ---------------------------------------------------------------------------
# (ii) the gate over every route
# ---------------------------------------------------------------------------
def test_the_open_set_is_pinned():
    assert app_mod._PASSWORD_CHANGE_OPEN_PATHS == EXPECTED_OPEN_PATHS
    assert tuple(app_mod._PASSWORD_CHANGE_OPEN_PREFIXES) == EXPECTED_OPEN_PREFIXES


@pytest.fixture
def flagged(tmp_path, monkeypatch):
    """The real app, and a way to (re)open a session whose account must
    change its password (stored hash + must_change_password)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    local_store._DATAFRAME_CACHE.invalidate()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    monkeypatch.setattr(auth_mod.brain_client, "send_password_reset_email",
                        lambda email, reset_url, **kw: None)
    auth = local_store.AuthStore()
    auth.ensure_user(FLAGGED)
    auth.set_password(FLAGGED, FLAGGED_PW, force_change=True)
    tc = TestClient(app_mod.app, base_url="https://testserver")

    def login():
        tc.cookies.clear()
        r = tc.post("/auth/login", data={"email": FLAGGED, "password": FLAGGED_PW},
                    follow_redirects=False)
        assert r.status_code == 302, r.text[:300]
        assert r.headers["location"] == "/auth/change_password"

    login()
    yield {"client": tc, "login": login}
    local_store._DATAFRAME_CACHE.invalidate()


def _is_gate_refusal(r) -> bool:
    if r.status_code != 403:
        return False
    try:
        return r.json() == GATE_BODY
    except Exception:
        return False


def test_every_route_outside_the_open_set_is_refused_and_no_open_route_is(flagged):
    tc = flagged["client"]
    wrong = []
    for method, path in _live_routes():
        url = _concrete(path)
        kwargs = {"json": {}} if method in ("POST", "PUT", "PATCH", "DELETE") else {}
        r = tc.request(method, url, follow_redirects=False, **kwargs)
        refused = _is_gate_refusal(r)
        if _is_open(url):
            if refused:
                wrong.append(("open path refused", method, path))
            if method != "GET":
                flagged["login"]()        # sign-out / sign-in may have replaced it
        elif not refused:
            wrong.append(("not refused", method, path, r.status_code, r.text[:80]))
    assert wrong == [], wrong


# ---------------------------------------------------------------------------
# (iii) root_path
# ---------------------------------------------------------------------------
def _run_gate(path: str, root_path: str) -> tuple[list, list]:
    reached, sent = [], []

    async def inner(scope, receive, send):
        reached.append(scope["path"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "https", "path": path, "raw_path": path.encode(),
        "root_path": root_path, "query_string": b"", "headers": [],
        "client": ("127.0.0.1", 50000), "server": ("testserver", 443),
        "session": {"email": "a@b.c", "must_change_password": True},
    }
    asyncio.run(app_mod.PasswordChangeGate(inner)(scope, receive, send))
    return reached, sent


def _status(sent) -> int:
    return next(m["status"] for m in sent if m["type"] == "http.response.start")


def test_the_gate_self_test_without_a_root_path():
    reached, sent = _run_gate("/auth/change_password", "")
    assert reached and _status(sent) == 200
    reached, sent = _run_gate("/api/dashboards", "")
    assert not reached and _status(sent) == 403


def test_an_open_path_under_a_root_path_passes_through():
    reached, sent = _run_gate("/proxy/auth/change_password", "/proxy")
    assert reached, "the change form must stay reachable behind a root_path"
    assert _status(sent) == 200


def test_a_closed_path_under_a_root_path_is_still_refused():
    reached, sent = _run_gate("/proxy/api/dashboards", "/proxy")
    assert not reached
    assert _status(sent) == 403
