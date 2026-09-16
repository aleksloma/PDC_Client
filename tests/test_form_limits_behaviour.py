"""Form-parser limits are enforced by the framework BEFORE any route runs.

Starlette 1.x parses urlencoded and multipart bodies with hard caps
(`max_fields=1000`, `max_files=1000`, `max_part_size=1 MiB` — see
`starlette/requests.py::_get_form`) and, when the request belongs to an app,
turns the parser's `MultiPartException` into `HTTPException(400,
detail=<parser message>)`. FastAPI re-raises that HTTPException unchanged
(`fastapi/routing.py`, the `except HTTPException: raise` branch around the
`await request.form()` call), so the caller sees the PARSER's message. A
handler that reads the form itself is entered, but it never gets past that
first `await`; a handler whose form is a declared parameter is never entered
at all.

tests/test_dependency_pins.py pins the starlette VERSION; these tests pin the
BEHAVIOUR that version was chosen for, through the real `app` object:

  * an urlencoded body with 1001 fields to the unauthenticated login form is
    refused with the parser's 400 + "Too many fields";
  * a multipart batch with 1001 file parts to /upload is refused with the
    parser's 400 + "Too many files" — while LOGGED IN, so the route's own
    `MAX_FILES` answer ("You can upload up to N files.") and the 401 branch
    are both provably not what answered;
  * an ordinary 2-field login form still reaches the route (a route-produced
    response, not the parser's 400).

Offline: DATA_ROOT is tmp_path, every brain call on the login path is
stubbed. The TestClient is built WITHOUT the context manager (entering it
runs the lifespan, which starts the db_scheduler thread — see
tests/test_version_endpoint.py). https base_url because the session cookie
is Secure-flagged by default. Every asserted value is bound to a local first
so a failure prints it.
"""
import pytest
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings

EMAIL = "limits-user@x.com"
PASSWORD = "limits-pw-123"

# Starlette's defaults, restated here so a change in the shipped pin that
# moves them is caught by the message assertions below.
STARLETTE_MAX_FIELDS = 1000
STARLETTE_MAX_FILES = 1000


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
    tc = TestClient(app_mod.app, base_url="https://testserver")
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _login(client):
    r = client.post("/auth/login", data={"email": EMAIL, "password": PASSWORD},
                    follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text)
    return r


# ---------------------------------------------------------------------------
# (1) urlencoded: 1001 fields to the unauthenticated login form
# ---------------------------------------------------------------------------
def test_login_form_with_too_many_fields_is_refused_by_the_parser(client):
    """1001 urlencoded fields exceed `max_fields=1000`; the FormParser raises
    and Starlette answers 400 with the parser's own message. The login route
    gets no further than its own `await request.form()` — otherwise it would
    answer with its landing page (a 400 HTML "Please enter a valid email",
    since no `email` field is present)."""
    n_fields = STARLETTE_MAX_FIELDS + 1
    body = "&".join(f"f{i}=x" for i in range(n_fields))
    r = client.post(
        "/auth/login",
        content=body.encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )
    status = r.status_code
    text = r.text
    content_type = r.headers.get("content-type", "")
    assert status == 400, (status, text[:300])
    # The parser's message, delivered as FastAPI's JSON HTTPException body —
    # not the login template (which would be text/html).
    assert "application/json" in content_type, (content_type, text[:300])
    detail = r.json().get("detail")
    assert isinstance(detail, str), r.json()
    assert "Too many fields" in detail, detail
    assert str(STARLETTE_MAX_FIELDS) in detail, detail
    assert "Please enter a valid email" not in text, text[:300]


# ---------------------------------------------------------------------------
# (2) multipart: 1001 file parts to /upload, logged in
# ---------------------------------------------------------------------------
def test_upload_with_too_many_file_parts_is_refused_by_the_parser(client):
    """1001 multipart file parts exceed `max_files=1000`. The refusal must be
    the PARSER's (400 + "Too many files"), not the route's: the route is
    entered with a valid session, so its own answer would have been the
    MAX_FILES JSON ("You can upload up to N files.", `settings.MAX_FILES` is
    far below 1001) — and without a session it would have been 401. Neither
    appears, which proves the body was rejected before the route ran."""
    _login(client)
    n_files = STARLETTE_MAX_FILES + 1
    files = [("files", (f"part{i}.csv", b"a,b\n1,2\n", "text/csv")) for i in range(n_files)]
    r = client.post("/upload", files=files, follow_redirects=False)
    status = r.status_code
    text = r.text
    content_type = r.headers.get("content-type", "")
    assert status == 400, (status, text[:300])
    assert "application/json" in content_type, (content_type, text[:300])
    payload = r.json()
    detail = payload.get("detail")
    assert isinstance(detail, str), payload
    assert "Too many files" in detail, detail
    assert str(STARLETTE_MAX_FILES) in detail, detail
    # The route's own MAX_FILES answer has a different shape and message.
    route_message = f"You can upload up to {settings.MAX_FILES} files."
    assert "error" not in payload, payload
    assert route_message not in text, text[:300]
    assert "Not authenticated" not in text, text[:300]


# ---------------------------------------------------------------------------
# (3) an ordinary login form still reaches the route
# ---------------------------------------------------------------------------
def test_normal_login_form_still_reaches_the_route(client):
    """A 2-field urlencoded form is far below the caps; the login route must
    run and produce ITS response. Wrong password is used so the response is
    unmistakably route-produced (401 + the landing template with the
    "Incorrect password" message) and not the parser's 400."""
    r = client.post("/auth/login", data={"email": EMAIL, "password": "wrong-pw"},
                    follow_redirects=False)
    status = r.status_code
    text = r.text
    content_type = r.headers.get("content-type", "")
    assert status == 401, (status, text[:300])
    assert status != 400, (status, text[:300])
    assert "text/html" in content_type, (content_type, text[:300])
    assert "Incorrect password" in text, text[:300]
    assert "Too many fields" not in text, text[:300]
    # And the correct password goes all the way through the route (302 to /lab).
    ok = _login(client)
    location = ok.headers.get("location")
    assert location == "/lab", (ok.status_code, location)
