"""Every `TemplateResponse` site renders through the REAL app.

Starlette 1.0 removed the `TemplateResponse(name, context)` call form; the
only one left is `TemplateResponse(request, name, context)`. The wrong form
is a TypeError AT REQUEST TIME — the module imports fine, so a structural
grep (tests/test_dependency_pins.py) and a render test are complementary.
Four of the nine sites had no test that ever rendered them:

  * `GET /`                          app.py           auth_landing.html
  * `GET /dashboards/{dash_id}`      app.py           dashboard_view.html
  * `GET /auth/change_password`      routes/auth.py   change_password.html
  * `POST /auth/change_password`     routes/auth.py   change_password.html
    (the error re-render — mismatched passwords → 400, same template)

Each case here must come back as a non-empty `text/html` body carrying a
marker copied from the template, with the expected status. The session
state each page needs (none / an owned dashboard / `must_change_password`)
is produced through the real login route, not by poking the session.

Offline: DATA_ROOT is tmp_path, brain calls on the login path are stubbed,
no SSO config exists under DATA_ROOT (so `/` renders the password form
instead of redirecting to Microsoft). The TestClient is built WITHOUT the
context manager (the lifespan would start the db_scheduler thread — see
tests/test_version_endpoint.py). https base_url because the session cookie
is Secure-flagged by default. Every asserted value is bound to a local
first so a failure prints it.
"""
import pytest
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings

EMAIL = "render-user@x.com"
PASSWORD = "render-pw-123"
TEMP_EMAIL = "render-temp@x.com"
TEMP_PASSWORD = "temp-pw-123"

# Stable strings copied from the templates.
LANDING_MARKER = "PowerDataChat — Sign in"
DASHBOARD_MARKER = 'id="dashGrid"'
CHANGE_PASSWORD_MARKER = "Set a new password"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    local_store._DATAFRAME_CACHE.invalidate()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    auth = local_store.AuthStore()
    auth.ensure_user(EMAIL)
    auth.set_password(EMAIL, PASSWORD)
    # A forced-change account: the login route lands it on
    # /auth/change_password with `must_change_password` in the session.
    auth.ensure_user(TEMP_EMAIL)
    auth.set_password(TEMP_EMAIL, TEMP_PASSWORD, force_change=True)
    tc = TestClient(app_mod.app, base_url="https://testserver")
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _login(client, email, password):
    r = client.post("/auth/login", data={"email": email, "password": password},
                    follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    return r


def _assert_html(r, expected_status, marker):
    status = r.status_code
    content_type = r.headers.get("content-type", "")
    text = r.text
    assert status == expected_status, (status, text[:300])
    assert "text/html" in content_type, (content_type, text[:300])
    assert len(text) > 0, "empty HTML body"
    assert marker in text, (marker, text[:300])


def test_landing_page_renders_html(client):
    """`GET /` without a session renders auth_landing.html (200)."""
    r = client.get("/", follow_redirects=False)
    _assert_html(r, 200, LANDING_MARKER)
    # The password form itself is in the body (not an SSO redirect page).
    text = r.text
    assert 'name="password"' in text, text[:300]


def test_dashboard_page_renders_html_for_an_owned_dashboard(client):
    """`GET /dashboards/{dash_id}` for a dashboard the session user OWNS
    renders dashboard_view.html (200) with the id injected for the page JS."""
    _login(client, EMAIL, PASSWORD)
    doc = local_store.DashboardStore().create_dashboard(EMAIL, "Render check")
    dash_id = doc["dash_id"]
    assert local_store.DashboardStore.valid_id(dash_id), doc
    r = client.get(f"/dashboards/{dash_id}", follow_redirects=False)
    _assert_html(r, 200, DASHBOARD_MARKER)
    text = r.text
    assert f'window.DASH_ID = "{dash_id}"' in text, text[:300]


def test_change_password_page_renders_html_for_a_forced_change_session(client):
    """`GET /auth/change_password` with `email` AND `must_change_password`
    in the session renders change_password.html (200). Without the flag the
    route redirects, so the forced-change login is what gets us here."""
    login = _login(client, TEMP_EMAIL, TEMP_PASSWORD)
    location = login.headers.get("location")
    assert location == "/auth/change_password", location
    r = client.get("/auth/change_password", follow_redirects=False)
    _assert_html(r, 200, CHANGE_PASSWORD_MARKER)
    text = r.text
    assert TEMP_EMAIL in text, text[:300]


@pytest.mark.parametrize(
    "form,expected_error",
    [
        ({"new_password": "abcd-1234", "confirm_password": "abcd-9999"}, "Passwords do not match"),
        ({"new_password": "ab", "confirm_password": "ab"}, "Password must be at least 4 characters"),
    ],
    ids=["mismatch", "too_short"],
)
def test_change_password_error_rerenders_html_with_400(client, form, expected_error):
    """`POST /auth/change_password` with a rejected pair re-renders the same
    template with the error text and status 400 — the second TemplateResponse
    site in routes/auth.py (the `_page` closure with `status_code=`)."""
    _login(client, TEMP_EMAIL, TEMP_PASSWORD)
    r = client.post("/auth/change_password", data=form, follow_redirects=False)
    _assert_html(r, 400, CHANGE_PASSWORD_MARKER)
    text = r.text
    assert expected_error in text, text[:300]
    # The rejected attempt did not clear the forced-change state: the page
    # is still served, not redirected away.
    again = client.get("/auth/change_password", follow_redirects=False)
    again_status = again.status_code
    assert again_status == 200, (again_status, again.text[:300])
