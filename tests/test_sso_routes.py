"""Microsoft SSO routes: 404 while disabled, the OIDC start/callback flow
(faked authlib client — no network), the admin /api/admin/sso* surface
(guards, masking, validation, the mocked Microsoft connection test incl.
POLICY_BLOCKED, the enable gate), and the landing-page integration
(button render, auto-redirect + ?local=1 escape hatch on the real app)."""
import json

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_sources
import local_store
import sso_store
from settings import settings

ADMIN = "ladmin"
USER = "user@x.com"

TENANT = "11111111-2222-3333-4444-555555555555"
TOKEN_ENDPOINT = f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/token"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY_OLD", "")
    local_store.AuthStore().ensure_user(ADMIN)
    local_store.AuthStore().set_role(ADMIN, "admin")
    local_store.AuthStore().ensure_user(USER)

    import routes.sso as sso_mod
    activity = []
    monkeypatch.setattr(sso_mod.brain_client, "post_activity",
                        lambda ev, em, *a, **k: activity.append((ev, em)))

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(sso_mod.router)
    app.include_router(sso_mod.admin_router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session.pop("must_change_password", None)
        return {"ok": True}

    @app.get("/_whoami")
    async def _whoami(request: Request):
        return {"email": request.session.get("email"),
                "remember": request.session.get("remember"),
                "sid": request.session.get("sid")}

    @app.get("/_landing")
    async def _landing_probe(request: Request):
        from routes.auth import _landing
        return _landing(request)

    tc = TestClient(app)
    tc._activity = activity
    return tc


def _save(client=None, **over):
    body = {"tenant_id": TENANT, "client_id": "app-client-id",
            "client_secret": "s3cret-sso", "public_base_url": "",
            "auto_redirect": False}
    body.update(over)
    sso_store.save(body, ADMIN)


def _enable():
    sso_store.record_test_ok(ADMIN)
    sso_store.set_enabled(True, ADMIN)


class _FakeOAuthClient:
    def __init__(self, token=None, error=None):
        self.token = token
        self.error = error
        self.redirect_uris = []

    async def authorize_redirect(self, request, redirect_uri):
        self.redirect_uris.append(redirect_uri)
        return RedirectResponse(url="https://login.microsoftonline.com/x/authorize")

    async def authorize_access_token(self, request):
        if self.error is not None:
            raise self.error
        return self.token


def _install_fake(monkeypatch, fake):
    import routes.sso as sso_mod
    monkeypatch.setattr(sso_mod, "_oauth_client", lambda: fake)
    return fake


# ── Public routes: 404 while disabled ─────────────────────────────────────

def test_public_routes_404_when_unconfigured_and_disabled(client):
    for path in ("/auth/microsoft", "/auth/microsoft/callback"):
        assert client.get(path, follow_redirects=False).status_code == 404
    _save()                                    # saved but never enabled
    for path in ("/auth/microsoft", "/auth/microsoft/callback"):
        assert client.get(path, follow_redirects=False).status_code == 404
    _enable()
    sso_store.set_enabled(False, ADMIN)        # disabled again
    for path in ("/auth/microsoft", "/auth/microsoft/callback"):
        assert client.get(path, follow_redirects=False).status_code == 404


# ── OIDC start + callback (faked authlib) ─────────────────────────────────

def test_start_redirects_with_computed_redirect_uri(client, monkeypatch):
    _save()
    _enable()
    fake = _install_fake(monkeypatch, _FakeOAuthClient())
    r = client.get("/auth/microsoft", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert fake.redirect_uris == ["http://testserver/auth/microsoft/callback"]

    _save(public_base_url="https://pdc.bank.local")
    _enable()
    client.get("/auth/microsoft", follow_redirects=False)
    assert fake.redirect_uris[-1] == "https://pdc.bank.local/auth/microsoft/callback"


def test_callback_success(client, monkeypatch):
    _save()
    _enable()
    _install_fake(monkeypatch, _FakeOAuthClient(
        token={"userinfo": {"preferred_username": "User@X.com"}}))
    r = client.get("/auth/microsoft/callback", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/lab"
    auth = local_store.AuthStore().get_auth("user@x.com")
    assert auth["sso_provider"] == "microsoft"
    assert auth["sso_last_login"]
    assert "password_hash" not in auth
    who = client.get("/_whoami").json()
    assert who["email"] == "user@x.com"
    assert who["sid"]
    # Browser-session cookie on purpose: remember must NOT be set.
    assert who["remember"] is None
    assert client._activity == [("login", "user@x.com")]
    # Auto-provisioned profile exists.
    assert local_store.AuthStore().get_profile("user@x.com")


def test_callback_email_claim_fallback(client, monkeypatch):
    _save()
    _enable()
    _install_fake(monkeypatch, _FakeOAuthClient(
        token={"userinfo": {"email": "Fallback@X.com"}}))
    r = client.get("/auth/microsoft/callback", follow_redirects=False)
    assert r.status_code == 302
    assert client.get("/_whoami").json()["email"] == "fallback@x.com"


def test_callback_refuses_bootstrap_admin_identity(client, monkeypatch):
    """Even when LOCAL_ADMIN_USERNAME is configured as a real Entra email,
    SSO must never open the bootstrap-admin session (password only)."""
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "admin@x.com")
    _save()
    _enable()
    _install_fake(monkeypatch, _FakeOAuthClient(
        token={"userinfo": {"preferred_username": "Admin@X.com"}}))
    r = client.get("/auth/microsoft/callback", follow_redirects=False)
    assert r.status_code == 403
    assert "Microsoft sign-in failed" in r.text
    assert client.get("/_whoami").json()["email"] is None


def test_no_module_scope_secret_after_flow(client, monkeypatch):
    """Article VII rule 8: after a full start+callback, nothing importable at
    module scope in routes.sso may hold the client secret — only the PUBLIC
    metadata cache exists."""
    import routes.sso as sso_mod
    _save()
    _enable()
    # Real _oauth_client (not the fake) so the module-scope state is the
    # production shape; no network happens because we never run the flow.
    c = sso_mod._oauth_client()
    assert c is not None
    dumped = repr(vars(sso_mod).get("_METADATA_CACHE"))
    assert "s3cret-sso" not in dumped
    assert not any(k for k in vars(sso_mod)
                   if "OAUTH_CACHE" in k)   # the old secret-holding cache


def test_callback_missing_email_claim(client, monkeypatch):
    _save()
    _enable()
    _install_fake(monkeypatch, _FakeOAuthClient(token={"userinfo": {"sub": "abc"}}))
    r = client.get("/auth/microsoft/callback", follow_redirects=False)
    assert r.status_code == 400
    assert "Microsoft sign-in failed" in r.text
    assert client.get("/_whoami").json()["email"] is None


def test_callback_oauth_error(client, monkeypatch):
    from authlib.integrations.starlette_client import OAuthError
    _save()
    _enable()
    _install_fake(monkeypatch, _FakeOAuthClient(
        error=OAuthError(error="invalid_grant", description="AADSTS70008 expired")))
    r = client.get("/auth/microsoft/callback", follow_redirects=False)
    assert r.status_code == 401
    assert "Microsoft sign-in failed" in r.text
    assert "access_token" not in r.text
    assert client.get("/_whoami").json()["email"] is None


# ── Admin API: guards + masking + validation ──────────────────────────────

def test_admin_routes_guard_parity(client):
    import routes.sso as sso_mod
    paths = [(r.path, sorted(r.methods - {"HEAD", "OPTIONS"})[0])
             for r in sso_mod.admin_router.routes]
    assert len(paths) == 5
    for path, method in paths:
        assert client.request(method, path).status_code == 401, path
    client.post(f"/_login/{USER}")
    for path, method in paths:
        assert client.request(method, path).status_code == 403, path
    denied = [r for r in db_sources.read_audit_tail(100)
              if r["action"] == "admin.denied"]
    assert len(denied) == len(paths)


def test_admin_get_masked_and_redirect_uri(client):
    _save()
    client.post(f"/_login/{ADMIN}")
    data = client.get("/api/admin/sso").json()
    assert data["client_secret_set"] is True
    assert data["client_secret_masked"] == "••••••••"
    assert "s3cret-sso" not in json.dumps(data)
    assert "client_secret_enc" not in data
    assert "last_test_hash" not in data
    assert data["redirect_uri"] == "http://testserver/auth/microsoft/callback"
    assert data["test_current"] is False
    assert data["encryption_ready"] is True


def test_save_validation(client):
    client.post(f"/_login/{ADMIN}")
    ok_body = {"tenant_id": TENANT, "client_id": "cid",
               "client_secret": "distinctive-secret-value"}
    # Bad tenant.
    r = client.post("/api/admin/sso/save",
                    json={**ok_body, "tenant_id": "not valid!"})
    assert r.status_code == 400 and "Tenant ID" in r.json()["error"]
    # Missing client id.
    r = client.post("/api/admin/sso/save", json={**ok_body, "client_id": ""})
    assert r.status_code == 400
    # Secret required on first save.
    r = client.post("/api/admin/sso/save", json={**ok_body, "client_secret": ""})
    assert r.status_code == 400 and "secret" in r.json()["error"].lower()
    # Bad public base URL.
    r = client.post("/api/admin/sso/save",
                    json={**ok_body, "public_base_url": "pdc.bank.local"})
    assert r.status_code == 400
    # GUID tenant accepted; empty secret now keeps the stored one.
    assert client.post("/api/admin/sso/save", json=ok_body).status_code == 200
    r = client.post("/api/admin/sso/save", json={**ok_body, "client_secret": ""})
    assert r.status_code == 200
    # Domain tenant accepted.
    r = client.post("/api/admin/sso/save",
                    json={**ok_body, "tenant_id": "contoso.onmicrosoft.com",
                          "client_secret": ""})
    assert r.status_code == 200
    # Audit rows exist and never carry the secret.
    rows = [r for r in db_sources.read_audit_tail(100) if r["action"] == "sso.save"]
    dumped = json.dumps(rows)
    assert rows and rows[0]["detail"]["secret_changed"] in (True, False)
    assert "distinctive-secret-value" not in dumped
    assert "client_secret" not in dumped


# ── Connection test (mocked Microsoft) ────────────────────────────────────

def _mock_ms(monkeypatch, token_status, token_json):
    import routes.sso as sso_mod

    def handler(request: httpx.Request) -> httpx.Response:
        if ".well-known" in str(request.url):
            return httpx.Response(200, json={"token_endpoint": TOKEN_ENDPOINT})
        return httpx.Response(token_status, json=token_json)

    monkeypatch.setattr(sso_mod, "_http_client", lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=10.0))


def test_test_connection_success(client, monkeypatch):
    _save()
    client.post(f"/_login/{ADMIN}")
    _mock_ms(monkeypatch, 200, {"access_token": "tok"})
    r = client.post("/api/admin/sso/test")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert sso_store.test_is_current() is True
    rows = [x for x in db_sources.read_audit_tail(100) if x["action"] == "sso.test"]
    assert rows and rows[0]["ok"] is True
    assert "s3cret-sso" not in json.dumps(rows)
    assert "tok" not in json.dumps(rows)


def test_test_connection_bad_secret(client, monkeypatch):
    _save()
    client.post(f"/_login/{ADMIN}")
    _mock_ms(monkeypatch, 401, {"error": "invalid_client",
                                "error_description":
                                    "AADSTS7000215: Invalid client secret provided."})
    r = client.post("/api/admin/sso/test")
    body = r.json()
    assert body["ok"] is False and "AADSTS7000215" in body["message"]
    assert "code" not in body
    assert sso_store.test_is_current() is False


def test_test_connection_policy_blocked_records_ok(client, monkeypatch):
    _save()
    client.post(f"/_login/{ADMIN}")
    _mock_ms(monkeypatch, 400, {"error": "invalid_grant",
                                "error_description":
                                    "AADSTS65001: The user or administrator has "
                                    "not consented to use the application."})
    r = client.post("/api/admin/sso/test")
    body = r.json()
    assert body["ok"] is False and body["code"] == "POLICY_BLOCKED"
    assert "policy blocked" in body["message"]
    # The one policy-shaped outcome still arms the enable gate…
    assert sso_store.test_is_current() is True
    # …and audits ok=true with the code.
    row = [x for x in db_sources.read_audit_tail(100) if x["action"] == "sso.test"][0]
    assert row["ok"] is True and row["detail"]["code"] == "POLICY_BLOCKED"


def test_test_connection_app_not_found_is_plain_failure(client, monkeypatch):
    _save()
    client.post(f"/_login/{ADMIN}")
    _mock_ms(monkeypatch, 400, {"error": "unauthorized_client",
                                "error_description":
                                    "AADSTS700016: Application not found in the "
                                    "directory."})
    body = client.post("/api/admin/sso/test").json()
    assert body["ok"] is False and "code" not in body
    assert sso_store.test_is_current() is False


# ── Enable gate ───────────────────────────────────────────────────────────

def test_enable_refused_without_current_test(client, monkeypatch):
    client.post(f"/_login/{ADMIN}")
    # Nothing saved → 400.
    assert client.post("/api/admin/sso/enable").status_code == 400
    _save()
    r = client.post("/api/admin/sso/enable")
    assert r.status_code == 409 and r.json()["code"] == "TEST_REQUIRED"
    assert sso_store.is_enabled() is False
    # Passing test → enable works.
    _mock_ms(monkeypatch, 200, {"access_token": "tok"})
    assert client.post("/api/admin/sso/test").json()["ok"] is True
    r = client.post("/api/admin/sso/enable")
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert sso_store.is_enabled() is True
    # Rotating the secret invalidates the test → enable refused again after
    # a disable.
    client.post("/api/admin/sso/save",
                json={"tenant_id": TENANT, "client_id": "app-client-id",
                      "client_secret": "rotated"})
    assert client.post("/api/admin/sso/disable").json()["enabled"] is False
    assert client.post("/api/admin/sso/enable").status_code == 409
    assert sso_store.is_enabled() is False


# ── Landing page integration ──────────────────────────────────────────────

def test_landing_button_only_when_enabled(client):
    r = client.get("/_landing")
    assert "Sign in with Microsoft" not in r.text
    _save()
    _enable()
    r = client.get("/_landing")
    assert "Sign in with Microsoft" in r.text
    assert 'href="/auth/microsoft"' in r.text


@pytest.fixture
def real_app_client(tmp_path, monkeypatch):
    """The real app.py app (auto-redirect lives in its "/" route). The
    TestClient context manager is deliberately NOT entered — the lifespan
    would start the db_scheduler thread (see tests/test_version_endpoint)."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    import brain_client
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    import app as app_mod
    return TestClient(app_mod.app)


def test_auto_redirect_and_local_escape(real_app_client):
    tc = real_app_client
    # No config → the plain form.
    assert tc.get("/", follow_redirects=False).status_code == 200
    _save(auto_redirect=True)
    _enable()
    r = tc.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/auth/microsoft"
    # The escape hatch always shows the form.
    r = tc.get("/?local=1", follow_redirects=False)
    assert r.status_code == 200 and "Sign in with Microsoft" in r.text
    # An authenticated session never bounces to Microsoft.
    local_store.AuthStore().ensure_user(USER)
    local_store.AuthStore().set_password(USER, "pw12345")
    r = tc.post("/auth/login", data={"email": USER, "password": "pw12345"},
                follow_redirects=False)
    assert r.status_code == 302
    r = tc.get("/", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/lab"
