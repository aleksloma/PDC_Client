"""CSRF: state-changing requests must be JSON, and the HTML forms carry a
session-bound token.

`app.JsonContentTypeGate` answers 415 to any POST/PUT/PATCH/DELETE that does
not declare `application/json` — a type a cross-site page cannot send without
a CORS preflight — except the four HTML forms (form-encoded) and the two
multipart upload routes. The forms (sign-in, reset request, reset link,
forced change) refuse a POST without the token their page embeds, BEFORE the
attempt limiter counts anything.
"""
import re

import pytest
from fastapi.testclient import TestClient

import app as app_mod
import auth_limiter
import local_store
from settings import settings
from tests.conftest import JSON_HEADERS, csrf_form, session_csrf

EMAIL = "user@acme.com"
PASSWORD = "correct-horse-9"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    store = local_store.AuthStore()
    store.create_account(EMAIL)
    store.set_password(EMAIL, PASSWORD)
    auth_limiter.reset()
    return TestClient(app_mod.app, base_url="https://testserver")


def _sign_in(client):
    r = client.post("/auth/login", data=csrf_form(client, {"email": EMAIL, "password": PASSWORD}),
                    follow_redirects=False)
    assert r.status_code == 302, r.text[:300]


# ---------------------------------------------------------------- the media-type gate
@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded",
                                   "multipart/form-data; boundary=x"])
def test_a_non_json_post_to_the_dashboards_api_is_415(client, ctype):
    _sign_in(client)
    r = client.post("/api/dashboards", content=b'{"name": "x"}',
                    headers={"Content-Type": ctype})
    assert r.status_code == 415, (ctype, r.status_code, r.text[:200])
    assert r.json() == {"error": "Unsupported Media Type"}
    listed = client.get("/api/dashboards").json()
    rows = listed.get("dashboards", listed) if isinstance(listed, dict) else listed
    assert not rows, "a dashboard was created by a non-JSON request"


def test_a_json_post_still_works(client):
    _sign_in(client)
    r = client.post("/api/dashboards", json={"name": "Mine"})
    assert r.status_code in (200, 201), r.text[:200]


def test_a_bodyless_post_without_a_content_type_is_415(client):
    _sign_in(client)
    assert client.post("/new_session").status_code == 415
    assert client.post("/new_session", headers=JSON_HEADERS).status_code == 200


@pytest.mark.parametrize("method", ["put", "patch", "delete"])
def test_every_state_changing_method_is_gated(client, method):
    r = getattr(client, method)("/api/dashboards/x", headers={"Content-Type": "text/plain"})
    assert r.status_code == 415


def test_reads_are_not_gated(client):
    assert client.get("/api/dashboards").status_code in (200, 401)


def test_multipart_is_accepted_only_on_the_upload_routes(client):
    _sign_in(client)
    ok = client.post("/upload", files={"files": ("a.csv", b"a,b\n1,2\n", "text/csv")})
    assert ok.status_code == 200, ok.text[:200]
    refused = client.post("/api/dashboards", files={"f": ("a.csv", b"x", "text/csv")})
    assert refused.status_code == 415


def test_the_gate_sits_outside_the_session_and_inside_the_guard():
    names = [m.cls.__name__ for m in app_mod.app.user_middleware]
    assert names[0] == "BackendNetworkGuard"
    assert names.index("JsonContentTypeGate") < names.index("RememberMeSessionMiddleware")


# ---------------------------------------------------------------- the form tokens
def test_the_landing_embeds_the_session_token(client):
    page = client.get("/?local=1")
    token = session_csrf(client)
    assert token and f'name="csrf" value="{token}"' in page.text


def test_a_sign_in_without_the_token_is_refused_and_not_counted(client, monkeypatch):
    counted = []
    monkeypatch.setattr(auth_limiter, "begin",
                        lambda *a, **k: counted.append(a) or auth_limiter.Verdict(True, 0))
    client.get("/?local=1")
    r = client.post("/auth/login", data={"email": EMAIL, "password": PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 403
    assert "The form has expired" in r.text
    assert counted == [], "a token failure reached the attempt limiter"
    wrong = client.post("/auth/login",
                        data={"email": EMAIL, "password": PASSWORD, "csrf": "x" * 43},
                        follow_redirects=False)
    assert wrong.status_code == 403


def test_a_sign_in_with_the_token_works_and_rotates_it(client):
    client.get("/?local=1")
    before = session_csrf(client)
    _sign_in(client)
    after = session_csrf(client)
    assert after and after != before


def test_the_reset_request_form_requires_the_token(client):
    client.get("/?local=1")
    r = client.post("/auth/reset_password", data={"email": EMAIL})
    assert r.status_code == 403
    ok = client.post("/auth/reset_password", data=csrf_form(client, {"email": EMAIL}))
    assert ok.status_code == 200


def test_the_reset_link_form_requires_the_token(client):
    token = local_store.AuthStore().create_reset_token(EMAIL)
    page = client.get(f"/auth/reset/{token}")
    assert page.status_code == 200 and 'name="csrf"' in page.text
    bad = client.post(f"/auth/reset/{token}",
                      data={"new_password": "brand-new-pw-1", "confirm_password": "brand-new-pw-1"},
                      follow_redirects=False)
    assert bad.status_code == 403
    good = client.post(f"/auth/reset/{token}",
                       data=csrf_form(client, {"new_password": "brand-new-pw-1",
                                               "confirm_password": "brand-new-pw-1"}),
                       follow_redirects=False)
    assert good.status_code == 302, good.text[:200]


def test_the_forced_change_form_requires_the_token(client):
    local_store.AuthStore().set_password(EMAIL, PASSWORD, force_change=True)
    _sign_in(client)
    page = client.get("/auth/change_password")
    assert 'name="csrf"' in page.text
    bad = client.post("/auth/change_password",
                      data={"new_password": "brand-new-pw-2", "confirm_password": "brand-new-pw-2",
                            "csrf": "wrong" * 9}, follow_redirects=False)
    assert bad.status_code == 403
    good = client.post("/auth/change_password",
                       data=csrf_form(client, {"new_password": "brand-new-pw-2",
                                               "confirm_password": "brand-new-pw-2"}),
                       follow_redirects=False)
    assert good.status_code == 302, good.text[:200]


def test_an_anonymous_token_session_passes_the_session_gates(client):
    client.get("/?local=1")
    assert session_csrf(client)
    r = client.get("/lab", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] in ("/", "https://testserver/")


def test_the_templates_carry_the_hidden_field():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "templates"
    for name in ("auth_landing.html", "change_password.html", "reset_password.html"):
        text = (root / name).read_text(encoding="utf-8")
        assert re.search(r'<input type="hidden" name="csrf" value="\{\{ csrf \}\}">', text), name


def test_every_bodyless_frontend_post_declares_json():
    """The gate refuses a state-changing request without application/json, so
    every browser call must declare it (body-less ones included)."""
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "static"
    for name in ("dashboard.js", "dashboard_view.js", "admin_data_sources.js"):
        text = (root / name).read_text(encoding="utf-8")
        assert "{ method: 'POST' }" not in text, name
    # The admin api() helper's default header is REPLACED when a caller passes
    # its own `headers`: every such object must still declare JSON.
    admin = (root / "admin_data_sources.js").read_text(encoding="utf-8")
    for m in re.finditer(r"headers:\s*\{([^}]*)\}", admin):
        assert "application/json" in m.group(1), m.group(0)
