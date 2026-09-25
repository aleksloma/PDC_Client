"""Every HTML page carries a nonce-based Content-Security-Policy.

A pure-ASGI middleware in `app.py` issues a fresh nonce per request
(`request.state.csp_nonce`, which the templates put on every inline
`<script>` and expose as `window.__CSP_NONCE__`) and adds the header to every
response whose content type is `text/html`. JSON, event streams, static
files, redirects and the backend-network refusal carry no header.

Enterprise policy (default settings):

    default-src 'self'; script-src 'self' 'nonce-N'; style-src 'self' 'unsafe-inline';
    img-src 'self' data: blob:; font-src 'self' data:; connect-src 'self';
    frame-src 'self' blob:; frame-ancestors 'self'; base-uri 'self';
    form-action 'self'; object-src 'none'

Widenings are setting-driven: `ENABLE_THIRD_PARTY_SCRIPTS` adds the Google
Analytics and Paddle origins, a configured `GCS_UPLOAD_BUCKET` adds
`https://storage.googleapis.com` to `connect-src`. `CSP_REPORT_ONLY` (a
diagnostic, default off) sends the same policy under
`Content-Security-Policy-Report-Only` instead.

The network guard stays the OUTERMOST layer: a refusal from the sandbox's
range is answered before this middleware runs, and carries no header.

Offline, real app: DATA_ROOT is tmp_path, the login path's brain calls are
stubbed, the TestClient is built WITHOUT the context manager (the lifespan
starts the scheduler thread), https base_url because the session cookie is
Secure-flagged. Sessions come from the real login route.
"""
import re

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings

CSP = "content-security-policy"
CSP_RO = "content-security-policy-report-only"

USER = "csp-user@x.com"
ADMIN = "csp-admin@x.com"
TEMP = "csp-temp@x.com"
PW = "csp-pw-123456"
CHAT_ID = "chat_csp_000001"
CONV_ID = "cv_0011223344556677"

INSIDE = ("192.168.255.242", 51000)
CIDR = "192.168.255.240/28"

THIRD_PARTY_HOSTS = ["googletagmanager", "paddle", "google-analytics"]


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
    for email in (USER, ADMIN):
        auth.ensure_user(email)
        auth.set_password(email, PW)
    auth.set_role(ADMIN, "admin")
    auth.ensure_user(TEMP)
    auth.set_password(TEMP, PW, force_change=True)
    # A chat owned by USER with one conversation in their index (the /c/{conv}
    # deep link resolves through it) and a data file (the stream needs frames).
    store = local_store.ChatDataStore(CHAT_ID)
    pd.DataFrame({"a": [1, 2]}).to_csv(store.files_dir / "d.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = USER
    meta["files"] = [{"file_name": "d.csv", "file_description": "d",
                      "schema": {"fields": {"a": {"description": "a"}}}}]
    store.write_meta(meta)
    auth.record_conversation(USER, CHAT_ID, CONV_ID, "CSP check")

    cache: dict = {}

    def client(who: str = "anon") -> TestClient:
        if who in cache:
            return cache[who]
        tc = TestClient(app_mod.app, base_url="https://testserver")
        if who != "anon":
            email = {"user": USER, "admin": ADMIN, "temp": TEMP}[who]
            r = tc.post("/auth/login", data={"email": email, "password": PW},
                        follow_redirects=False)
            assert r.status_code == 302, (who, r.status_code, r.text[:300])
        cache[who] = tc
        return tc

    def dashboard_id() -> str:
        if "dash" not in cache:
            r = client("user").post("/api/dashboards", json={"name": "CSP"})
            assert r.status_code == 200, r.text[:300]
            cache["dash"] = r.json()["dash_id"]
        return cache["dash"]

    yield {"client": client, "dash": dashboard_id}
    local_store._DATAFRAME_CACHE.invalidate()


# (id, who, path-builder) — every HTML page the app renders.
def _pages():
    return [
        ("sign_in", "anon", lambda e: "/"),
        ("lab", "user", lambda e: "/lab"),
        ("conversation", "user", lambda e: f"/c/{CONV_ID}"),
        ("dashboard", "user", lambda e: f"/dashboards/{e['dash']()}"),
        ("admin_data_sources", "admin", lambda e: "/admin/data_sources"),
        ("change_password", "temp", lambda e: "/auth/change_password"),
    ]


PAGE_IDS = [p[0] for p in _pages()]
NONCE_EXPOSED = {"lab", "conversation", "dashboard"}


def _get_page(env, page_id):
    _, who, path = next(p for p in _pages() if p[0] == page_id)
    r = env["client"](who).get(path(env), follow_redirects=False)
    assert r.status_code == 200, (page_id, r.status_code, r.text[:300])
    assert r.headers.get("content-type", "").startswith("text/html"), r.headers
    return r


def _parse(header: str) -> dict:
    out = {}
    for part in (header or "").split(";"):
        tokens = part.strip().split()
        if tokens:
            out[tokens[0].lower()] = tokens[1:]
    return out


def _policy(r) -> dict:
    header = r.headers.get(CSP)
    assert header, f"no Content-Security-Policy header on {r.request.url}"
    return _parse(header)


def _nonce_of(policy: dict) -> str:
    nonces = [t for t in policy.get("script-src", []) if t.startswith("'nonce-")]
    assert len(nonces) == 1, policy.get("script-src")
    return nonces[0][len("'nonce-"):-1]


INLINE_SCRIPT_RE = re.compile(r"<script\b([^>]*)>", re.I)


def _inline_script_attrs(body: str) -> list[str]:
    return [attrs for attrs in INLINE_SCRIPT_RE.findall(body)
            if not re.search(r"\bsrc\s*=", attrs, re.I)]


# ---------------------------------------------------------------------------
# presence and shape
# ---------------------------------------------------------------------------
def test_the_policy_parser_reads_a_known_header():
    p = _parse("default-src 'self'; script-src 'self' 'nonce-abc'; object-src 'none'")
    assert p == {"default-src": ["'self'"], "script-src": ["'self'", "'nonce-abc'"],
                 "object-src": ["'none'"]}
    assert _nonce_of(p) == "abc"
    assert _inline_script_attrs('<script>x</script><script src="/a.js"></script>'
                                '<script nonce="n">y</script>') == ["", ' nonce="n"']


@pytest.mark.parametrize("page_id", PAGE_IDS)
def test_every_html_page_carries_the_header(env, page_id):
    r = _get_page(env, page_id)
    assert r.headers.get(CSP), (page_id, dict(r.headers))
    assert CSP_RO not in r.headers, page_id


@pytest.mark.parametrize("page_id", PAGE_IDS)
def test_the_enterprise_directives(env, page_id):
    p = _policy(_get_page(env, page_id))
    assert p.get("default-src") == ["'self'"], p
    script = p.get("script-src", [])
    assert "'self'" in script, script
    _nonce_of(p)
    assert "'unsafe-inline'" not in script and "'unsafe-eval'" not in script, script
    assert sorted(p.get("style-src", [])) == sorted(["'self'", "'unsafe-inline'"]), p
    assert {"'self'", "data:", "blob:"} <= set(p.get("img-src", [])), p
    assert sorted(p.get("font-src", [])) == sorted(["'self'", "data:"]), p
    assert p.get("connect-src") == ["'self'"], p
    assert "'self'" in p.get("frame-src", []), p
    assert p.get("frame-ancestors") == ["'self'"], p
    assert p.get("base-uri") == ["'self'"], p
    assert p.get("form-action") == ["'self'"], p
    assert p.get("object-src") == ["'none'"], p


@pytest.mark.parametrize("page_id", ["sign_in"] + sorted(NONCE_EXPOSED))
def test_the_page_policy_never_allows_eval(env, page_id):
    """The eval grant belongs to the chart document route alone
    (`/charts/{token}`, tests/test_chart_frames.py); the pages never carry it."""
    r = _get_page(env, page_id)
    assert "'unsafe-eval'" not in r.headers.get(CSP, ""), r.headers.get(CSP)


@pytest.mark.parametrize("page_id", sorted(NONCE_EXPOSED))
def test_the_page_policy_lets_chart_frames_load_from_the_app(env, page_id):
    """Chart frames load `/charts/{token}` from the application origin."""
    p = _policy(_get_page(env, page_id))
    assert "'self'" in p.get("frame-src", []), p.get("frame-src")


@pytest.mark.parametrize("page_id", PAGE_IDS)
def test_every_inline_script_carries_the_header_nonce(env, page_id):
    r = _get_page(env, page_id)
    nonce = _nonce_of(_policy(r))
    for attrs in _inline_script_attrs(r.text):
        m = re.search(r"""\bnonce\s*=\s*["']([^"']+)["']""", attrs)
        assert m, (page_id, f"<script{attrs}> has no nonce")
        assert m.group(1) == nonce, (page_id, m.group(1), nonce)


@pytest.mark.parametrize("page_id", sorted(NONCE_EXPOSED))
def test_the_page_exposes_the_nonce_to_its_scripts(env, page_id):
    r = _get_page(env, page_id)
    nonce = _nonce_of(_policy(r))
    m = re.search(r"""window\.__CSP_NONCE__\s*=\s*["']([^"']+)["']""", r.text)
    assert m, f"{page_id}: window.__CSP_NONCE__ is not set"
    assert m.group(1) == nonce, (m.group(1), nonce)


def test_each_response_gets_a_fresh_nonce(env):
    first = _nonce_of(_policy(_get_page(env, "lab")))
    second = _nonce_of(_policy(_get_page(env, "lab")))
    assert first != second
    assert len(first) >= 16, first


# ---------------------------------------------------------------------------
# absence
# ---------------------------------------------------------------------------
def _assert_no_policy(r, label):
    assert CSP not in r.headers and CSP_RO not in r.headers, (label, dict(r.headers))


@pytest.mark.parametrize("who,path", [
    ("anon", "/health"), ("anon", "/version"), ("user", "/api/dashboards"),
    ("user", "/auth/me"), ("anon", "/static/i18n.js"),
])
def test_non_html_responses_carry_no_policy(env, who, path):
    r = env["client"](who).get(path, follow_redirects=False)
    assert r.status_code == 200, (path, r.status_code, r.text[:200])
    assert not r.headers.get("content-type", "").startswith("text/html"), r.headers
    _assert_no_policy(r, path)


def test_the_password_change_refusal_carries_no_policy(env):
    r = env["client"]("temp").get("/api/dashboards", follow_redirects=False)
    assert r.status_code == 403, r.text[:200]
    assert r.json().get("code") == "PASSWORD_CHANGE_REQUIRED", r.text[:200]
    _assert_no_policy(r, "gate 403")


def test_a_redirect_carries_no_policy(env):
    r = env["client"]("anon").get("/lab", follow_redirects=False)
    assert r.status_code == 302, r.status_code
    _assert_no_policy(r, "302")


def test_the_chat_event_stream_carries_no_policy(env, monkeypatch):
    import routes.chat as chat_mod

    def fake_multi_plot(**kw):
        yield {"single_response": True,
               "result": {"text": "stubbed", "image_base64": None, "table": None,
                          "code": None, "usage": {}}}

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: fake_multi_plot(**kw))
    r = env["client"]("user").post(f"/api/chat/{CHAT_ID}/chat/stream",
                                   json={"question": "q"})
    assert r.status_code == 200, r.text[:300]
    assert r.headers.get("content-type", "").startswith("text/event-stream"), r.headers
    assert "stubbed" in r.text, r.text[:300]
    _assert_no_policy(r, "sse")


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
def test_report_only_switches_the_header_name(env, monkeypatch):
    assert hasattr(settings, "CSP_REPORT_ONLY"), (
        "settings must define CSP_REPORT_ONLY (bool, default False)")
    monkeypatch.setattr(settings, "CSP_REPORT_ONLY", True, raising=False)
    r = _get_page(env, "lab")
    assert CSP not in r.headers, dict(r.headers)
    policy = _parse(r.headers.get(CSP_RO, ""))
    assert policy.get("default-src") == ["'self'"], r.headers
    nonce = _nonce_of(policy)
    assert f'nonce="{nonce}"' in r.text


def test_report_only_defaults_to_false(monkeypatch):
    from settings import Settings
    monkeypatch.delenv("CSP_REPORT_ONLY", raising=False)
    assert getattr(Settings(), "CSP_REPORT_ONLY", None) is False


def test_third_party_origins_are_absent_by_default(env):
    header = _get_page(env, "lab").headers.get(CSP, "")
    assert header, "no policy header"
    for host in THIRD_PARTY_HOSTS + ["storage.googleapis.com"]:
        assert host not in header, (host, header)


def test_third_party_scripts_widen_exactly_their_directives(env, monkeypatch):
    monkeypatch.setattr(settings, "ENABLE_THIRD_PARTY_SCRIPTS", True, raising=False)
    r = _get_page(env, "lab")
    p = _policy(r)
    assert "https://www.googletagmanager.com" in p.get("script-src", []), p
    assert "https://cdn.paddle.com" in p.get("script-src", []), p
    assert "https://*.paddle.com" in p.get("frame-src", []), p
    assert "https://*.google-analytics.com" in p.get("connect-src", []), p
    assert "'unsafe-inline'" not in p.get("script-src", []), p
    # The vendor blocks' own inline scripts are nonced too.
    nonce = _nonce_of(p)
    for attrs in _inline_script_attrs(r.text):
        assert f'nonce="{nonce}"' in attrs.replace("'", '"'), attrs


def test_gcs_upload_widens_connect_src(env, monkeypatch):
    monkeypatch.setattr(settings, "GCS_UPLOAD_BUCKET", "b", raising=False)
    p = _policy(_get_page(env, "lab"))
    assert "https://storage.googleapis.com" in p.get("connect-src", []), p


def test_gcs_origin_absent_without_a_bucket(env, monkeypatch):
    monkeypatch.setattr(settings, "GCS_UPLOAD_BUCKET", "", raising=False)
    header = _get_page(env, "lab").headers.get(CSP, "")
    assert header and "storage.googleapis.com" not in header, header


# ---------------------------------------------------------------------------
# middleware order
# ---------------------------------------------------------------------------
def _middleware_names() -> list[str]:
    return [getattr(m.cls, "__name__", str(m.cls)) for m in app_mod.app.user_middleware]


def test_the_network_guard_stays_outermost_and_the_policy_layer_is_inside_it():
    names = _middleware_names()
    assert names and names[0] == "BackendNetworkGuard", names
    csp = [i for i, n in enumerate(names)
           if re.search(r"content.?security.?policy|csp", n, re.I)]
    assert csp, f"no Content-Security-Policy middleware registered: {names}"
    assert all(i > 0 for i in csp), names


def test_a_refused_backend_request_is_bare_and_carries_no_policy(env, monkeypatch):
    assert hasattr(settings, "EXECUTOR_NETWORK_CIDR")
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", CIDR, raising=False)
    r = TestClient(app_mod.app, base_url="https://testserver", client=INSIDE).get("/lab")
    assert r.status_code == 403, r.status_code
    assert r.json() == {"error": "forbidden"}, r.text
    _assert_no_policy(r, "guard 403")
