"""Chart documents are served by their own route, under their own policy.

A chart document (plotly HTML: a full document with inline scripts) is not
written into its frame by the page. The page registers it and loads it from
a URL the app serves:

* `POST /api/charts` JSON `{html}` — signed-in only (`401 {"error": ...}`
  otherwise); `html` must be a non-empty string of at most 5_000_000
  characters (`400` otherwise); answers `200 {"url": "/charts/<token>"}`.
* `GET /charts/{token}` — the registered HTML as `text/html` when the token
  is valid, unexpired, and issued to the signed-in user; `404` for a tampered
  token, another user's token, no session, an expired token, or an entry the
  store no longer holds.

The token is signed and short-lived (`routes.charts.TOKEN_MAX_AGE_S`, 1800,
read at call time); the store is in memory, bounded
(`STORE_MAX_TOTAL_BYTES` in UTF-8 bytes, plus a per-user share
`USER_MAX_BYTES` so one user's registrations cannot evict another's live
chart -- Task 8b Part A #4) and time-limited (`ENTRY_TTL_S`), all read at
call time. The token carries the store id only; the entry holds the email.

The served document carries ONE Content-Security-Policy of its own:
`sandbox allow-scripts; default-src 'none'; script-src 'self' 'unsafe-eval'
'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; frame-ancestors
'self'` -- no nonce, no host source -- and the body is the registered HTML
byte-for-byte; plus `X-Content-Type-Options: nosniff`, `Cache-Control:
no-store` and `Referrer-Policy: no-referrer`. The page-policy middleware
leaves that response alone (no second policy, no report-only variant).

Why no nonce: a nonce written into every `<script` tag of a stored document
also blesses `<script src="https://any-host/...">` in it (a nonced script
may load from any URL), so inside the sandboxed, opaque-origin frame the
nonce filtered nothing and only widened where script could come from. The
earlier nonce tests are REPLACED by the stricter checks below, not dropped:
the policy names no nonce and no host, and the document is served unchanged.

The password-change gate covers both routes.

Offline, real app: DATA_ROOT is tmp_path, the login path's brain calls are
stubbed, the TestClient is built WITHOUT the context manager (the lifespan
starts the scheduler thread), https base_url because the session cookie is
Secure-flagged. Sessions come from the real login route.
"""
import importlib
import re
import time

import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings
from tests.conftest import csrf_form

CSP = "content-security-policy"
CSP_RO = "content-security-policy-report-only"

USER = "charts-user@x.com"
OTHER = "charts-other@x.com"
TEMP = "charts-temp@x.com"
PW = "charts-pw-123456"

MAX_HTML_CHARS = 5_000_000
URL_RE = re.compile(r"^/charts/([^/?#\s]+)$")
ALLOWED_SCRIPT_SOURCES = {"'self'", "'unsafe-eval'", "'unsafe-inline'"}

SIMPLE_DOC = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\">"
              "<script src=\"/static/vendor/plotly/plotly.min.js\"></script>"
              "<script type=\"text/javascript\">window.CHART_MARK = 1;</script>"
              "<style>#plotly-chart{height:100%}</style></head>"
              "<body><div id=\"plotly-chart\">chart-mark-42</div>"
              "<script>var x = 2;</script></body></html>")

EXPECTED_DIRECTIVES = {
    "sandbox", "default-src", "script-src", "style-src", "img-src", "frame-ancestors",
}


def _charts():
    try:
        return importlib.import_module("routes.charts")
    except ImportError as e:
        pytest.fail(f"routes/charts.py is missing (POST /api/charts, GET /charts/{{token}}): {e}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    auth = local_store.AuthStore()
    for email in (USER, OTHER):
        auth.ensure_user(email)
        auth.set_password(email, PW)
    auth.ensure_user(TEMP)
    auth.set_password(TEMP, PW, force_change=True)

    cache: dict = {}

    def client(who: str = "anon") -> TestClient:
        if who in cache:
            return cache[who]
        tc = TestClient(app_mod.app, base_url="https://testserver")
        if who != "anon":
            email = {"user": USER, "other": OTHER, "temp": TEMP}[who]
            r = tc.post("/auth/login", data=csrf_form(tc, {"email": email, "password": PW}),
                        follow_redirects=False)
            assert r.status_code == 302, (who, r.status_code, r.text[:300])
        cache[who] = tc
        return tc

    yield {"client": client}
    local_store._DATAFRAME_CACHE.invalidate()


def _register(env, html=SIMPLE_DOC, who="user") -> str:
    r = env["client"](who).post("/api/charts", json={"html": html})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    body = r.json()
    assert set(body) == {"url"}, body
    assert URL_RE.match(body["url"]), body
    return body["url"]


def _parse(header: str) -> dict:
    out = {}
    for part in (header or "").split(";"):
        tokens = part.strip().split()
        if tokens:
            out[tokens[0].lower()] = tokens[1:]
    return out


def _get_ok(env, url, who="user"):
    r = env["client"](who).get(url, follow_redirects=False)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    return r


def _real_plotly_document() -> str:
    import plotly.graph_objects as go

    import plot_utils
    return plot_utils._plotly_to_html(go.Figure(go.Bar(x=["a", "b", "c"], y=[3, 1, 2])))


# ---------------------------------------------------------------------------
# the module
# ---------------------------------------------------------------------------
def _bound(mod, name):
    if not isinstance(getattr(mod, name, None), int):
        pytest.fail(f"routes.charts.{name} missing")
    return getattr(mod, name)


def test_the_module_exposes_its_bounds():
    """The store bounds are counted in UTF-8 BYTES (renamed from the
    character bound, Task 8b Part A #4); the request contract MAX_DOC_CHARS
    is unchanged. A maximal document of 4-byte characters still fits in one
    user's share, and a share fits in the store."""
    mod = _charts()
    assert getattr(mod, "TOKEN_MAX_AGE_S", None) == 1800
    assert getattr(mod, "MAX_DOC_CHARS", None) == MAX_HTML_CHARS
    total = _bound(mod, "STORE_MAX_TOTAL_BYTES")
    share = _bound(mod, "USER_MAX_BYTES")
    assert share >= 4 * MAX_HTML_CHARS, share
    assert total >= share, (total, share)
    assert getattr(mod, "ENTRY_TTL_S", None) == 1800


def test_the_router_is_included_in_the_app():
    # The pinned framework lists an included router as ONE entry of
    # `app.routes`, so walk into it the way the route inventory does.
    from tests.test_route_inventory import _walk
    paths = {path for _method, path in _walk(app_mod.app.routes)}
    assert "/api/charts" in paths, "POST /api/charts is not routed"
    assert "/charts/{token}" in paths, "GET /charts/{token} is not routed"


# ---------------------------------------------------------------------------
# POST /api/charts
# ---------------------------------------------------------------------------
def test_register_without_a_session_is_401(env):
    _charts()
    r = env["client"]("anon").post("/api/charts", json={"html": SIMPLE_DOC})
    assert r.status_code == 401, (r.status_code, r.text[:200])
    assert "error" in r.json(), r.text[:200]


@pytest.mark.parametrize("body", [
    {}, {"html": None}, {"html": 123}, {"html": ["<b>"]}, {"html": {"a": 1}},
    {"html": ""}, {"html": "x" * (MAX_HTML_CHARS + 1)}, [],
], ids=["missing", "null", "int", "list", "dict", "empty", "too-long", "not-an-object"])
def test_register_rejects_a_bad_body(env, body):
    _charts()
    r = env["client"]("user").post("/api/charts", json=body)
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_register_rejects_a_non_json_body(env):
    _charts()
    r = env["client"]("user").post("/api/charts", content=b"not json",
                                   headers={"content-type": "application/json"})
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_register_answers_a_chart_url(env):
    _charts()
    url = _register(env)
    assert url.startswith("/charts/"), url


def test_register_accepts_the_maximum_size(env):
    _charts()
    url = _register(env, html="<p>" + "x" * (MAX_HTML_CHARS - 3))
    assert URL_RE.match(url), url


def test_each_registration_gets_its_own_url(env):
    _charts()
    assert _register(env) != _register(env)


# ---------------------------------------------------------------------------
# GET /charts/{token}: who may read it
# ---------------------------------------------------------------------------
def test_the_owner_reads_the_registered_document(env):
    url = _register(env)
    r = _get_ok(env, url)
    assert r.headers.get("content-type", "").startswith("text/html"), r.headers
    assert "chart-mark-42" in r.text, r.text[:300]


def test_another_users_token_is_404(env):
    url = _register(env, who="user")
    r = env["client"]("other").get(url, follow_redirects=False)
    assert r.status_code == 404, (r.status_code, r.text[:200])
    assert "chart-mark-42" not in r.text


def test_no_session_is_404(env):
    url = _register(env)
    r = env["client"]("anon").get(url, follow_redirects=False)
    assert r.status_code == 404, (r.status_code, r.text[:200])
    assert "chart-mark-42" not in r.text


def _tampered(token: str) -> list:
    mid = len(token) // 2
    flip = "A" if token[mid] != "A" else "B"
    return [token[:mid] + flip + token[mid + 1:], token + "x", token[:-1],
            "abc", "x" * len(token)]


def test_a_tampered_token_is_404(env):
    url = _register(env)
    token = URL_RE.match(url).group(1)
    for bad in _tampered(token):
        r = env["client"]("user").get(f"/charts/{bad}", follow_redirects=False)
        assert r.status_code == 404, (bad, r.status_code, r.text[:200])
        assert "chart-mark-42" not in r.text, bad


def test_an_expired_token_is_404(env, monkeypatch):
    mod = _charts()
    url = _register(env)
    _get_ok(env, url)
    monkeypatch.setattr(mod, "TOKEN_MAX_AGE_S", 0)
    time.sleep(1.2)          # signed timestamps have whole-second resolution
    r = env["client"]("user").get(url, follow_redirects=False)
    assert r.status_code == 404, (r.status_code, r.text[:200])


def test_an_entry_past_its_ttl_is_404(env, monkeypatch):
    mod = _charts()
    monkeypatch.setattr(mod, "ENTRY_TTL_S", 0)
    url = _register(env)
    time.sleep(0.05)
    r = env["client"]("user").get(url, follow_redirects=False)
    assert r.status_code == 404, (r.status_code, r.text[:200])


def test_the_store_evicts_the_oldest_entries_first(env, monkeypatch):
    mod = _charts()
    _bound(mod, "STORE_MAX_TOTAL_BYTES")
    _bound(mod, "USER_MAX_BYTES")
    monkeypatch.setattr(mod, "STORE_MAX_TOTAL_BYTES", 250)
    monkeypatch.setattr(mod, "USER_MAX_BYTES", 10_000)
    docs = [f"<p>doc-{i}</p>" + "x" * (100 - len(f"<p>doc-{i}</p>")) for i in range(3)]
    assert all(len(d) == 100 for d in docs)
    urls = [_register(env, html=d) for d in docs]
    r = env["client"]("user").get(urls[0], follow_redirects=False)
    assert r.status_code == 404, ("the oldest entry must be evicted", r.status_code)
    newest = _get_ok(env, urls[-1])
    assert "doc-2" in newest.text, newest.text[:200]


# ---------------------------------------------------------------------------
# GET /charts/{token}: the response's own headers
# ---------------------------------------------------------------------------
def test_the_document_carries_exactly_one_policy_of_its_own(env):
    r = _get_ok(env, _register(env))
    headers = r.headers.get_list(CSP)
    assert len(headers) == 1, headers
    assert CSP_RO not in r.headers, dict(r.headers)
    p = _parse(headers[0])
    assert set(p) == EXPECTED_DIRECTIVES, p
    assert p["sandbox"] == ["allow-scripts"], p
    assert p["default-src"] == ["'none'"], p
    assert sorted(p["script-src"]) == sorted(["'self'", "'unsafe-eval'", "'unsafe-inline'"]), p
    assert p["style-src"] == ["'unsafe-inline'"], p
    assert p["img-src"] == ["data:"], p
    assert p["frame-ancestors"] == ["'self'"], p


def test_the_document_is_not_sniffed_cached_or_referred(env):
    r = _get_ok(env, _register(env))
    assert r.headers.get("x-content-type-options") == "nosniff", dict(r.headers)
    assert r.headers.get("cache-control") == "no-store", dict(r.headers)
    assert r.headers.get("referrer-policy") == "no-referrer", dict(r.headers)


def test_report_only_mode_does_not_touch_the_chart_policy(env, monkeypatch):
    monkeypatch.setattr(settings, "CSP_REPORT_ONLY", True, raising=False)
    r = _get_ok(env, _register(env))
    assert CSP_RO not in r.headers, dict(r.headers)
    headers = r.headers.get_list(CSP)
    assert len(headers) == 1, headers
    assert _parse(headers[0]).get("sandbox") == ["allow-scripts"], headers


def test_the_script_source_list_names_no_nonce_and_no_host(env):
    # Replaces the former per-response nonce checks: a nonce would let a
    # stored document load script from any host, so its absence is pinned.
    r = _get_ok(env, _register(env))
    header = r.headers.get(CSP, "")
    assert "'nonce-" not in header, header
    tokens = _parse(header).get("script-src", [])
    assert tokens, header
    for token in tokens:
        assert token in ALLOWED_SCRIPT_SOURCES, (token, header)


@pytest.mark.parametrize("doc_id", ["simple", "real-plotly"])
def test_the_body_is_the_registered_document_byte_for_byte(env, doc_id):
    html = SIMPLE_DOC if doc_id == "simple" else _real_plotly_document()
    r = _get_ok(env, _register(env, html=html))
    assert r.content == html.encode("utf-8"), (len(r.content), len(html), r.text[:400])


def test_two_reads_serve_identical_bodies(env):
    url = _register(env)
    first, second = _get_ok(env, url), _get_ok(env, url)
    assert first.content == second.content, (first.text[:300], second.text[:300])
    assert first.text == SIMPLE_DOC, first.text[:400]


# ---------------------------------------------------------------------------
# the password-change gate
# ---------------------------------------------------------------------------
GATE_BODY = {"error": "Password change required", "code": "PASSWORD_CHANGE_REQUIRED"}


def test_the_password_change_gate_covers_registration(env):
    _charts()
    r = env["client"]("temp").post("/api/charts", json={"html": SIMPLE_DOC})
    assert r.status_code == 403, (r.status_code, r.text[:200])
    assert r.json() == GATE_BODY, r.text[:200]


def test_the_password_change_gate_covers_the_document_route(env):
    url = _register(env, who="user")
    r = env["client"]("temp").get(url, follow_redirects=False)
    assert r.status_code == 403, (r.status_code, r.text[:200])
    assert r.json() == GATE_BODY, r.text[:200]


# ---------------------------------------------------------------------------
# Task 8b Part A #4: token without the email, byte totals, per-user share
# ---------------------------------------------------------------------------
def _token_payload(url):
    from itsdangerous import URLSafeTimedSerializer
    token = URL_RE.match(url).group(1)
    return URLSafeTimedSerializer(settings.SECRET_KEY, salt="chart-frame").loads(token)


def _doc(tag: str, size: int) -> str:
    head = f"<p>{tag}</p>"
    return head + "x" * (size - len(head))


def test_the_token_carries_the_store_id_only(env):
    """The chart URL ends up in browser history and access logs; it names the
    store entry, never the registering user's address."""
    url = _register(env)
    payload = _token_payload(url)
    assert isinstance(payload, dict), payload
    assert set(payload) == {"i"}, payload
    assert USER not in url


def test_the_owner_check_still_holds_without_the_email_in_the_token(env):
    """The entry holds the email; another user's session still gets 404."""
    url = _register(env, who="user")
    assert set(_token_payload(url)) == {"i"}
    r = env["client"]("other").get(url, follow_redirects=False)
    assert r.status_code == 404, (r.status_code, r.text[:200])


def test_the_store_total_is_counted_in_utf8_bytes(env, monkeypatch):
    """Three documents of 103 characters but 403 UTF-8 bytes each (100
    astral-plane characters of 4 bytes) exceed a 1000-byte store; counted in
    characters they would not."""
    mod = _charts()
    _bound(mod, "STORE_MAX_TOTAL_BYTES")
    _bound(mod, "USER_MAX_BYTES")
    monkeypatch.setattr(mod, "STORE_MAX_TOTAL_BYTES", 1000)
    monkeypatch.setattr(mod, "USER_MAX_BYTES", 100_000)
    astral = "😀" * 100
    docs = [f"<p{i}" + astral for i in range(3)]
    assert all(len(d) == 103 and len(d.encode("utf-8")) == 403 for d in docs)
    urls = [_register(env, html=d) for d in docs]
    r = env["client"]("user").get(urls[0], follow_redirects=False)
    assert r.status_code == 404, ("the oldest entry must be evicted by bytes",
                                  r.status_code)
    _get_ok(env, urls[1])
    _get_ok(env, urls[2])


def test_another_users_registrations_never_evict_a_live_entry_within_its_share(
        env, monkeypatch):
    """User B registering far more than the store would hold loses B's own
    oldest entries; A's single entry, well within A's share, stays served."""
    mod = _charts()
    _bound(mod, "STORE_MAX_TOTAL_BYTES")
    _bound(mod, "USER_MAX_BYTES")
    monkeypatch.setattr(mod, "STORE_MAX_TOTAL_BYTES", 500)
    monkeypatch.setattr(mod, "USER_MAX_BYTES", 300)
    a_url = _register(env, html=_doc("user-a", 100), who="user")
    b_urls = [_register(env, html=_doc(f"user-b-{i}", 100), who="other")
              for i in range(8)]
    kept = _get_ok(env, a_url, who="user")
    assert "user-a" in kept.text
    # B keeps its newest three (its 300-byte share), its older ones are gone.
    for url in b_urls[:-3]:
        r = env["client"]("other").get(url, follow_redirects=False)
        assert r.status_code == 404, (url, r.status_code)
    for url in b_urls[-3:]:
        _get_ok(env, url, who="other")


def test_a_user_over_their_share_loses_their_own_oldest_first(env, monkeypatch):
    """The per-user share evicts the SAME user's oldest entry, not the
    globally oldest one (which belongs to someone else)."""
    mod = _charts()
    _bound(mod, "STORE_MAX_TOTAL_BYTES")
    _bound(mod, "USER_MAX_BYTES")
    monkeypatch.setattr(mod, "STORE_MAX_TOTAL_BYTES", 10_000)
    monkeypatch.setattr(mod, "USER_MAX_BYTES", 300)
    b_url = _register(env, html=_doc("user-b", 100), who="other")
    a_urls = [_register(env, html=_doc(f"user-a-{i}", 100), who="user")
              for i in range(4)]
    r = env["client"]("user").get(a_urls[0], follow_redirects=False)
    assert r.status_code == 404, ("A's oldest must go first", r.status_code)
    for url in a_urls[1:]:
        _get_ok(env, url, who="user")
    _get_ok(env, b_url, who="other")
