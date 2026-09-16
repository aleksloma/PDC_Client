"""T-cookie-real-app — the Secure flag on the REAL `app` object.

The existing coverage (tests/test_upload_filename_sanitize.py) builds its own
FastAPI with `RememberMeSessionMiddleware(https_only=...)` and separately greps
app.py for `https_only=settings.SESSION_HTTPS_ONLY`. Neither proves that the
app a customer actually runs emits `Secure` — the wiring is evaluated at
import time, so only the live app can answer that.

Both Set-Cookie branches of `RememberMeSessionMiddleware.__call__` are
covered: the SET branch (login, non-empty session) and the CLEAR branch
(logout, session emptied -> `expires=Thu, 01 Jan 1970`). A clear-cookie sent
WITHOUT Secure over a Secure-only session is how a stale cookie survives.

The TestClient is built WITHOUT the context manager on purpose: entering it
runs the lifespan, which starts the db_scheduler thread (see
tests/test_version_endpoint.py). Redirects are not followed — /lab is not
under test. DATA_ROOT is tmp_path, and every brain call the login path makes
is stubbed, so this stays fully offline.
"""
import pytest
from starlette.testclient import TestClient

import app as app_mod
import local_store
from settings import settings

EMAIL = "cookie-user@x.com"
PASSWORD = "S3cure-passw0rd!"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    local_store._DATAFRAME_CACHE.invalidate()
    import routes.auth as auth_mod
    monkeypatch.setattr(auth_mod.brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod.brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    # https base_url: a Secure cookie is only stored/resent by the client over
    # https, and the logout branch needs the cookie to come back.
    tc = TestClient(app_mod.app, base_url="https://testserver")
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _login(client):
    return client.post("/auth/login", data={"email": EMAIL, "password": PASSWORD},
                       follow_redirects=False)


# app.py builds the session middleware at IMPORT time from
# settings.SESSION_HTTPS_ONLY, so these tests can only observe whatever this
# environment configured — monkeypatching the setting afterwards changes
# nothing. A box whose .env sets SESSION_HTTPS_ONLY=false (the documented
# plain-HTTP escape hatch) therefore SKIPS rather than fails: that the DEFAULT
# is true is asserted without the env in
# tests/test_upload_filename_sanitize.py, which builds a fresh Settings().
# Bound to a local first: the Settings repr carries SECRET_KEY.
_HTTPS_ONLY = getattr(settings, "SESSION_HTTPS_ONLY", None)
pytestmark = pytest.mark.skipif(
    _HTTPS_ONLY is not True,
    reason="SESSION_HTTPS_ONLY is off in this environment; the real app's "
           "middleware was already built without Secure",
)


def test_real_app_login_sets_a_secure_session_cookie(client):
    r = _login(client)
    assert r.status_code == 302, r.text
    assert r.headers["location"] == "/lab"
    raw = r.headers["set-cookie"]
    flags = raw.lower()
    assert "secure" in flags, raw
    assert "httponly" in flags, raw
    assert "samesite=lax" in flags, raw
    assert client.cookies.get("session"), "the session cookie was not stored"


def test_real_app_logout_clear_cookie_is_also_secure(client):
    assert _login(client).status_code == 302
    r = client.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 302
    raw = r.headers["set-cookie"]
    flags = raw.lower()
    assert "expires=thu, 01 jan 1970" in flags, raw      # the CLEAR branch
    assert "session=null" in flags, raw
    assert "secure" in flags, raw
    assert "httponly" in flags, raw
