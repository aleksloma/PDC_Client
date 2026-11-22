"""C5 §2–§3 (F11) — static files: one version per build, long caching for
versioned URLs, gzip for /static only.

  §2 The page routes put `?v=<value>` on every local static URL. The value is
     `settings.BUILD_COMMIT` when set, else ONE per-process value (the process
     start time) — the same on every request, never the per-request clock.
     A versioned static request (`/static/...?v=<that value>`) is answered
     with `Cache-Control: public, max-age=31536000, immutable`; an
     unversioned static request carries no such header (today's behaviour).
     The chart route, the SSE stream and the API responses are untouched.
  §3 Compression for `/static` only (text assets above a minimum size): a
     large JS with `Accept-Encoding: gzip` comes back gzip-encoded; the chat
     stream, `/charts/{token}`, JSON API responses and the HTML pages never
     are. The security middlewares stay outermost: a compressed static
     response is still `nosniff` (exactly once) and carries no page policy.

Seams / readings:
  * BUILD_COMMIT is read at REQUEST time from the `settings` instance (as
    `_build_stamp` and `/version` read it), so `monkeypatch.setattr(settings,
    "BUILD_COMMIT", ...)` changes the rendered `?v=`;
  * the unstamped value is not pinned to a format — only that it is one
    value, the same across requests more than a second apart (the old
    `int(time.time())` would differ), and that the cache header honours it;
  * httpx decodes gzip transparently; `content-encoding` stays visible on the
    response and the decoded body is compared with the file on disk.

Offline, real app: DATA_ROOT is tmp_path, the TestClient is built WITHOUT the
context manager (the lifespan starts the scheduler thread), https base_url
(the session cookie is Secure-flagged), sessions from the real login route.
"""
import pathlib
import re
import time

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings
from tests.conftest import JSON_HEADERS, csrf_form

ROOT = pathlib.Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

USER = "static-user@x.com"
ADMIN = "static-admin@x.com"
PW = "static-pw-123456"
CHAT_ID = "chat_static_00001"
CONV_ID = "cv_00aa11bb22cc33dd"

IMMUTABLE = "public, max-age=31536000, immutable"
LARGE_JS = "dashboard.js"            # ~270 KB
LARGE_CSS = "main.css"               # ~54 KB
BINARY = "logo.png"

V_RE = re.compile(r"""/static/[^"'?\s>]+\?v=([^"'&\s>]+)""")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    auth = local_store.AuthStore()
    for email in (USER, ADMIN):
        auth.ensure_user(email)
        auth.set_password(email, PW)
    auth.set_role(ADMIN, "admin")
    store = local_store.ChatDataStore(CHAT_ID)
    pd.DataFrame({"a": [1, 2]}).to_csv(store.files_dir / "d.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = USER
    meta["files"] = [{"file_name": "d.csv", "file_description": "d",
                      "schema": {"fields": {"a": {"description": "a"}}}}]
    store.write_meta(meta)
    auth.record_conversation(USER, CHAT_ID, CONV_ID, "Static check")

    cache: dict = {}

    def client(who: str = "anon") -> TestClient:
        if who in cache:
            return cache[who]
        tc = TestClient(app_mod.app, base_url="https://testserver")
        if who != "anon":
            email = {"user": USER, "admin": ADMIN}[who]
            r = tc.post("/auth/login", data=csrf_form(tc, {"email": email, "password": PW}),
                        follow_redirects=False)
            assert r.status_code == 302, (who, r.status_code, r.text[:300])
        cache[who] = tc
        return tc

    def dashboard_id() -> str:
        if "dash" not in cache:
            r = client("user").post("/api/dashboards", json={"name": "Static"})
            assert r.status_code == 200, r.text[:300]
            cache["dash"] = r.json()["dash_id"]
        return cache["dash"]

    yield {"client": client, "dash": dashboard_id, "store": store}
    local_store._DATAFRAME_CACHE.invalidate()


# (id, who, path-builder) — every page that versions its static URLs.
def _pages():
    return [
        ("lab", "user", lambda e: "/lab"),
        ("conversation", "user", lambda e: f"/c/{CONV_ID}"),
        ("dashboard", "user", lambda e: f"/dashboards/{e['dash']()}"),
        ("admin_data_sources", "admin", lambda e: "/admin/data_sources"),
    ]


PAGE_IDS = [p[0] for p in _pages()]


def _get_page(env, page_id, **kw):
    _, who, path = next(p for p in _pages() if p[0] == page_id)
    r = env["client"](who).get(path(env), follow_redirects=False, **kw)
    assert r.status_code == 200, (page_id, r.status_code, r.text[:300])
    assert r.headers.get("content-type", "").startswith("text/html"), r.headers
    return r


def _versions(html: str) -> set:
    found = set(V_RE.findall(html))
    assert found, "no versioned /static URL on the page"
    return found


def _page_version(env, page_id="lab") -> str:
    vs = _versions(_get_page(env, page_id).text)
    assert len(vs) == 1, f"{page_id}: more than one ?v= value on one page: {vs}"
    return next(iter(vs))


def _gzip(**extra):
    return {"Accept-Encoding": "gzip", **extra}


def _identity():
    return {"Accept-Encoding": "identity"}


def _encoding(r) -> str:
    return (r.headers.get("content-encoding") or "").lower()


def _cc(r) -> str:
    return (r.headers.get("cache-control") or "").lower()


# ===========================================================================
# §2 the ?v= value
# ===========================================================================
@pytest.mark.parametrize("page_id", PAGE_IDS)
def test_a_stamped_build_versions_every_static_url_with_the_commit(env, monkeypatch,
                                                                    page_id):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    assert _versions(_get_page(env, page_id).text) == {"abc1234"}


def test_the_version_follows_the_commit_setting(env, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    assert _page_version(env) == "abc1234"
    monkeypatch.setattr(settings, "BUILD_COMMIT", "def5678")
    assert _page_version(env) == "def5678"


def test_the_stamped_version_matches_the_version_endpoint(env, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    commit = env["client"]("anon").get("/version").json()["commit"]
    assert _page_version(env) == commit


def test_an_unstamped_process_uses_one_value_across_requests(env, monkeypatch):
    """Not the per-request clock: two renders more than a second apart carry
    the same value, and every page of the process shares it."""
    monkeypatch.setattr(settings, "BUILD_COMMIT", "")
    first = _page_version(env, "lab")
    time.sleep(1.2)
    second = _page_version(env, "lab")
    assert first == second, (first, second)
    for page_id in PAGE_IDS:
        assert _versions(_get_page(env, page_id).text) == {first}, page_id


def test_an_unstamped_version_is_not_empty_and_not_the_commit_placeholder(env, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "")
    v = _page_version(env)
    assert v.strip() and v not in {"None", "none", "dev", ""}, v


# ===========================================================================
# §2 cache headers
# ===========================================================================
@pytest.mark.parametrize("name", [LARGE_JS, LARGE_CSS, BINARY, "http.js"])
def test_a_versioned_static_request_is_cached_immutably(env, monkeypatch, name):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    v = _page_version(env)
    r = env["client"]("anon").get(f"/static/{name}?v={v}")
    assert r.status_code == 200, (name, r.status_code)
    assert _cc(r) == IMMUTABLE, (name, r.headers.get("cache-control"))


def test_the_unstamped_version_gets_the_immutable_header_too(env, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "")
    v = _page_version(env)
    r = env["client"]("anon").get(f"/static/{LARGE_JS}?v={v}")
    assert r.status_code == 200
    assert _cc(r) == IMMUTABLE, r.headers.get("cache-control")


@pytest.mark.parametrize("name", [LARGE_JS, LARGE_CSS, BINARY, "http.js"])
def test_an_unversioned_static_request_is_not_cached_immutably(env, monkeypatch, name):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    r = env["client"]("anon").get(f"/static/{name}")
    assert r.status_code == 200, (name, r.status_code)
    assert "immutable" not in _cc(r), r.headers.get("cache-control")
    assert "max-age=31536000" not in _cc(r), r.headers.get("cache-control")


def test_a_versioned_static_body_is_the_file(env, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    r = env["client"]("anon").get(f"/static/{LARGE_JS}?v=abc1234", headers=_identity())
    assert r.status_code == 200
    assert r.content == (STATIC / LARGE_JS).read_bytes()


@pytest.mark.parametrize("page_id", PAGE_IDS)
def test_pages_are_not_cached_immutably(env, monkeypatch, page_id):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    r = _get_page(env, page_id)
    assert "immutable" not in _cc(r), r.headers.get("cache-control")


def test_a_json_api_response_is_not_cached_immutably(env, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    r = env["client"]("user").get("/api/dashboards?v=abc1234")
    assert r.status_code == 200, r.text[:200]
    assert "immutable" not in _cc(r), r.headers.get("cache-control")


# ===========================================================================
# §3 compression — /static only
# ===========================================================================
@pytest.mark.parametrize("name", [LARGE_JS, LARGE_CSS])
def test_a_large_static_text_asset_is_gzip_encoded(env, name):
    r = env["client"]("anon").get(f"/static/{name}", headers=_gzip())
    assert r.status_code == 200
    assert _encoding(r) == "gzip", dict(r.headers)
    assert r.content == (STATIC / name).read_bytes()      # httpx decoded it
    assert "accept-encoding" in (r.headers.get("vary") or "").lower(), (
        "a compressed response a shared cache may hold must vary on Accept-Encoding")


def test_a_versioned_static_asset_is_both_compressed_and_immutable(env, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    r = env["client"]("anon").get(f"/static/{LARGE_JS}?v=abc1234", headers=_gzip())
    assert r.status_code == 200
    assert _encoding(r) == "gzip", dict(r.headers)
    assert _cc(r) == IMMUTABLE, r.headers.get("cache-control")
    assert r.content == (STATIC / LARGE_JS).read_bytes()


def test_a_gzipped_static_response_carries_a_weak_etag(env):
    """The compressed bytes are a different representation of the file: a
    strong ETag shared with the identity response would let a cache serve
    one for the other (N5)."""
    tc = env["client"]("anon")
    gz = tc.get(f"/static/{LARGE_JS}", headers=_gzip())
    plain = tc.get(f"/static/{LARGE_JS}", headers=_identity())
    assert _encoding(gz) == "gzip", dict(gz.headers)
    etag = gz.headers.get("etag") or ""
    assert etag.startswith("W/"), etag
    assert etag != (plain.headers.get("etag") or ""), (etag, plain.headers.get("etag"))


def _plotly_bundle() -> pathlib.Path:
    path = STATIC / "vendor" / "plotly" / "plotly.min.js"
    if not path.is_file():
        import plot_utils
        plot_utils.ensure_plotly_js_asset()
    if not path.is_file():
        pytest.skip("the offline plotly bundle is not materialized here")
    return path


@pytest.mark.parametrize("query", ["", "?v=abc1234"])
def test_the_plotly_bundle_is_never_gzipped(env, monkeypatch, query):
    """The chart frames load the ~4.5 MB bundle; compressing it per request
    is excluded (N6). Other static JS is still compressed."""
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    path = _plotly_bundle()
    tc = env["client"]("anon")
    r = tc.get(f"/static/vendor/plotly/plotly.min.js{query}", headers=_gzip())
    assert r.status_code == 200
    assert _encoding(r) != "gzip", dict(r.headers)
    assert r.content == path.read_bytes()
    assert r.headers.get_list("x-content-type-options") == ["nosniff"]
    other = tc.get(f"/static/{LARGE_JS}{query}", headers=_gzip())
    assert _encoding(other) == "gzip", dict(other.headers)


def test_no_compression_without_accept_encoding(env):
    r = env["client"]("anon").get(f"/static/{LARGE_JS}", headers=_identity())
    assert r.status_code == 200
    assert _encoding(r) in ("", "identity"), dict(r.headers)
    assert r.content == (STATIC / LARGE_JS).read_bytes()


def test_a_binary_image_is_not_gzip_encoded(env):
    r = env["client"]("anon").get(f"/static/{BINARY}", headers=_gzip())
    assert r.status_code == 200
    assert _encoding(r) != "gzip", dict(r.headers)


@pytest.mark.parametrize("page_id", PAGE_IDS)
def test_html_pages_are_not_compressed(env, page_id):
    r = _get_page(env, page_id, headers=_gzip())
    assert len(r.content) > 1000          # large enough that a blanket gzip would bite
    assert _encoding(r) != "gzip", dict(r.headers)


def test_a_json_api_response_is_not_compressed(env):
    store = env["store"]
    conv = store.new_conversation("big")
    store.append_history(conv, {"role": "human", "content": "q"})
    store.append_history(conv, {"role": "ai", "content": "x" * 20000})
    r = env["client"]("user").get(f"/api/chat/{CHAT_ID}/conversation/{conv}/history",
                                  headers=_gzip())
    assert r.status_code == 200, r.text[:200]
    assert r.headers.get("content-type", "").startswith("application/json"), r.headers
    assert len(r.content) > 10000
    assert _encoding(r) != "gzip", dict(r.headers)


def test_the_chat_event_stream_is_not_compressed(env, monkeypatch):
    import routes.chat as chat_mod

    def fake_multi_plot(**kw):
        yield {"single_response": True,
               "result": {"text": "stubbed " + "y" * 20000, "image_base64": None,
                          "table": None, "code": None, "usage": {}}}

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: fake_multi_plot(**kw))
    # The title call this answered first turn starts is stubbed by the
    # conftest autouse fixture `no_brain_title_call`.
    r =env["client"]("user").post(f"/api/chat/{CHAT_ID}/chat/stream",
                                   json={"question": "q"},
                                   headers={**JSON_HEADERS, **_gzip()})
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get("content-type", "").startswith("text/event-stream"), r.headers
    assert "stubbed" in r.text
    assert _encoding(r) != "gzip", dict(r.headers)
    assert "immutable" not in _cc(r), r.headers.get("cache-control")


def test_the_chart_route_is_not_compressed_and_keeps_its_headers(env, monkeypatch):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    tc = env["client"]("user")
    html = "<html><head></head><body>" + ("<div>chart</div>" * 4000) + "</body></html>"
    r = tc.post("/api/charts", json={"html": html})
    assert r.status_code == 200, r.text[:200]
    url = r.json()["url"]
    r = tc.get(url, headers=_gzip())
    assert r.status_code == 200, r.text[:200]
    assert _encoding(r) != "gzip", dict(r.headers)
    assert r.text == html
    assert _cc(r) == "no-store", r.headers.get("cache-control")
    assert "sandbox allow-scripts" in (r.headers.get("content-security-policy") or "")
    assert r.headers.get_list("x-content-type-options") == ["nosniff"]


# ===========================================================================
# §3 security headers on static and compressed responses
# ===========================================================================
@pytest.mark.parametrize("query", ["", "?v=abc1234"])
def test_a_compressed_static_response_is_nosniff_once_and_has_no_page_policy(
        env, monkeypatch, query):
    monkeypatch.setattr(settings, "BUILD_COMMIT", "abc1234")
    r = env["client"]("anon").get(f"/static/{LARGE_JS}{query}", headers=_gzip())
    assert r.status_code == 200
    assert _encoding(r) == "gzip", dict(r.headers)
    assert r.headers.get_list("x-content-type-options") == ["nosniff"]
    assert "content-security-policy" not in r.headers
    assert "content-security-policy-report-only" not in r.headers
    assert r.headers.get_list("strict-transport-security") == [
        "max-age=31536000; includeSubDomains"]


def test_an_uncompressed_page_keeps_its_policy_and_nosniff(env):
    r = _get_page(env, "lab", headers=_gzip())
    assert r.headers.get("content-security-policy"), dict(r.headers)
    assert r.headers.get_list("x-content-type-options") == ["nosniff"]


def test_compression_sits_inside_the_security_middlewares():
    names = [getattr(m.cls, "__name__", str(m.cls)) for m in app_mod.app.user_middleware]
    assert names[0] == "BackendNetworkGuard", names
    assert names.index("SecurityHeaders") == names.index("ContentSecurityPolicy") - 1, names
    outer = max(names.index("SecurityHeaders"), names.index("ContentSecurityPolicy"))
    for i, n in enumerate(names):
        if re.search(r"gzip|compress", n, re.I):
            assert i > outer, (n, names)
