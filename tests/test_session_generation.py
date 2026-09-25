"""A password reset or change ends every OTHER session of the account.

The contract pinned here:

* `auth.json` carries `session_generation` (a string). Both password writers
  replace it: `AuthStore.set_password(...)` RETURNS the new value and
  `AuthStore.consume_reset_token(...)` writes a new one too.
  `AuthStore().session_generation(email) -> str` answers the stored value,
  "" when the account has none (or has no auth.json at all).
* Every sign-in stores the account's current value in the session as `gen`;
  sign-out pops `gen` (and `iat`).
* `app.SessionGenerationGate` (pure ASGI, registered INSIDE the session
  middleware) compares `session.get("gen", "")` with the account's value for
  every session carrying an `email`. A mismatch EMPTIES the session: the
  request continues as anonymous -- pages redirect to `/`, APIs answer 401 --
  and the response carries the clearing cookie (`session=null`, expires
  1970). The line `SESSION_ENDED_BY_PASSWORD_CHANGE` is logged.
* The session that MADE the change (`/auth/password`, the forced
  `/auth/change_password`) is re-stamped and stays signed in; a reset link
  signs nobody in, so after one is used every session of the account ends.
* A cookie issued before this behaviour existed (no `gen`) on an account
  that has no stored generation keeps working: an upgrade signs nobody out.
* The backend-network guard stays the outermost middleware.

Real app (`app.app`), TestClient built WITHOUT the context manager (no
lifespan, no scheduler thread), https base_url so the session cookie comes
back whether or not this environment sets SESSION_HTTPS_ONLY. Offline:
DATA_ROOT is tmp_path, every brain call is stubbed.
"""
import asyncio
import json
from base64 import b64decode, b64encode

import pytest
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings

USER = "gen.user@corp.example"
USER_PW = "Gen-user-passw0rd"
FORCED = "gen.forced@corp.example"
FORCED_PW = "Gen-forced-temp1"
LEGACY = "gen.legacy@corp.example"
LEGACY_PW = "Gen-legacy-passw0rd"
NEW_PW = "Brand-new-passw0rd"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    for name, value in (("ALLOW_SELF_REGISTRATION", False), ("PASSWORD_MIN_LENGTH", 8)):
        if name in type(settings).model_fields:
            monkeypatch.setattr(settings, name, value)
    local_store._DATAFRAME_CACHE.invalidate()
    index = getattr(local_store, "_RESET_TOKEN_INDEX", None)
    if isinstance(index, dict):
        index.clear()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_password_reset_email",
                        lambda email, reset_url, **kw: {"ok": True})
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    monkeypatch.setattr(auth_mod, "_run_in_background",
                        lambda fn, *args: fn(*args), raising=False)
    store = local_store.AuthStore()
    store.ensure_user(USER)
    store.set_password(USER, USER_PW)
    store.ensure_user(FORCED)
    store.set_password(FORCED, FORCED_PW, force_change=True)
    yield {"tmp": tmp_path, "auth_mod": auth_mod}
    local_store._DATAFRAME_CACHE.invalidate()


def _client():
    return TestClient(app_mod.app, base_url="https://testserver",
                      raise_server_exceptions=False)


def _signed_in(email=USER, password=USER_PW, expect="/lab"):
    tc = _client()
    r = tc.post("/auth/login", data={"email": email, "password": password},
                follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == expect, r.headers["location"]
    assert tc.get("/auth/me").status_code == 200, "the sign-in did not hold"
    return tc


def _set_cookie(r) -> str:
    return "; ".join(r.headers.get_list("set-cookie"))


def _assert_cleared(r):
    raw = _set_cookie(r).lower()
    assert "session=null" in raw, raw
    assert "expires=thu, 01 jan 1970" in raw, raw


def _session_of(tc) -> dict:
    """The session a client's cookie carries, unsigned the way the
    middleware signs it (TimestampSigner over the SECRET_KEY, no salt)."""
    raw = tc.cookies.get("session")
    assert raw, "no session cookie"
    data = TimestampSigner(str(settings.SECRET_KEY)).unsign(raw.encode("utf-8"))
    return json.loads(b64decode(data))


def _generation(email) -> str:
    fn = getattr(local_store.AuthStore, "session_generation", None)
    if fn is None:
        pytest.fail("AuthStore.session_generation missing")
    return local_store.AuthStore().session_generation(email)


def _read_auth(tmp, email) -> dict:
    p = tmp / "users" / email / "auth.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ===========================================================================
# the store
# ===========================================================================
def test_set_password_returns_and_stores_a_new_generation(world):
    store = local_store.AuthStore()
    first = store.set_password(USER, "First-passw0rd")
    second = store.set_password(USER, "Second-passw0rd")
    assert isinstance(first, str) and first, first
    assert isinstance(second, str) and second, second
    assert first != second
    assert _read_auth(world["tmp"], USER).get("session_generation") == second
    assert _generation(USER) == second


def test_consuming_a_reset_link_writes_a_new_generation(world):
    before = _generation(USER)
    token = local_store.AuthStore().create_reset_token(USER)
    assert local_store.AuthStore().consume_reset_token(token, NEW_PW) == USER
    after = _generation(USER)
    assert isinstance(after, str) and after and after != before, (before, after)
    assert _read_auth(world["tmp"], USER).get("session_generation") == after


def test_an_account_without_a_generation_answers_empty(world):
    assert _generation("never.seen@corp.example") == ""
    local_store.AuthStore().ensure_user("profile.only@corp.example")
    assert _generation("profile.only@corp.example") == ""


# ===========================================================================
# the session carries the generation
# ===========================================================================
def test_sign_in_stamps_the_generation_into_the_session(world):
    tc = _signed_in()
    session = _session_of(tc)
    assert session.get("gen") == _generation(USER), session
    assert session.get("gen"), "no generation stamped"


def test_sign_out_pops_the_generation(world):
    tc = _signed_in()
    assert "gen" in _session_of(tc), "sign-in stamped no generation"
    r = tc.post("/auth/logout", follow_redirects=False)
    assert r.status_code == 302
    _assert_cleared(r)


# ===========================================================================
# a change ends the OTHER sessions
# ===========================================================================
def test_a_profile_change_ends_the_other_sessions_and_keeps_the_changer(world):
    a = _signed_in()
    c = _signed_in()
    b = _signed_in()
    assert a.get("/auth/profile").status_code == 200
    r = b.post("/auth/password", json={"current_password": USER_PW, "new_password": NEW_PW})
    assert r.status_code == 200, (r.status_code, r.text[:300])

    page = a.get("/lab", follow_redirects=False)
    assert page.status_code == 302, (page.status_code, page.text[:200])
    assert page.headers["location"] == "/", page.headers["location"]
    _assert_cleared(page)

    api = c.get("/auth/profile")
    assert api.status_code == 401, (api.status_code, api.text[:200])
    _assert_cleared(api)

    assert b.get("/auth/profile").status_code == 200, "the changing session was ended"
    assert _session_of(b).get("gen") == _generation(USER)


def test_a_forced_change_ends_the_other_sessions_and_keeps_the_changer(world):
    a = _signed_in(FORCED, FORCED_PW, expect="/auth/change_password")
    b = _signed_in(FORCED, FORCED_PW, expect="/auth/change_password")
    r = b.post("/auth/change_password",
               data={"new_password": NEW_PW, "confirm_password": NEW_PW},
               follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert b.get("/auth/profile").status_code == 200, "the changing session was ended"
    stale = a.get("/auth/profile")
    assert stale.status_code == 401, (stale.status_code, stale.text[:200])
    _assert_cleared(stale)


def test_a_used_reset_link_ends_every_session(world):
    a = _signed_in()
    b = _signed_in()
    token = local_store.AuthStore().create_reset_token(USER)
    assert token
    r = _client().post(f"/auth/reset/{token}",
                       data={"new_password": NEW_PW, "confirm_password": NEW_PW},
                       follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    for tc in (a, b):
        stale = tc.get("/auth/profile")
        assert stale.status_code == 401, (stale.status_code, stale.text[:200])
        _assert_cleared(stale)
    fresh = _signed_in(USER, NEW_PW)
    assert fresh.get("/auth/profile").status_code == 200


def test_a_direct_store_change_is_seen_at_once(world):
    """The generation cache is refreshed by the writer: a session that was
    just accepted is refused on its next request, never accepted from a
    stale cached value."""
    a = _signed_in()
    assert a.get("/auth/profile").status_code == 200       # the value is now cached
    local_store.AuthStore().set_password(USER, NEW_PW)
    stale = a.get("/auth/profile")
    assert stale.status_code == 401, (stale.status_code, stale.text[:200])


def test_the_ended_session_is_logged(world, monkeypatch):
    lines = []

    def rec(sid, level, message, **ctx):
        lines.append(f"{sid} {level} {message} {ctx}")

    import logger_utils
    monkeypatch.setattr(app_mod, "log_with_sid", rec)
    monkeypatch.setattr(logger_utils, "log_with_sid", rec)
    a = _signed_in()
    local_store.AuthStore().set_password(USER, NEW_PW)
    assert a.get("/auth/profile").status_code == 401
    hits = [ln for ln in lines if "SESSION_ENDED_BY_PASSWORD_CHANGE" in ln]
    assert hits, lines


# ===========================================================================
# the upgrade path
# ===========================================================================
def _legacy_cookie(session: dict) -> str:
    data = b64encode(json.dumps(session).encode("utf-8"))
    return TimestampSigner(str(settings.SECRET_KEY)).sign(data).decode("utf-8")


def test_a_cookie_without_a_generation_keeps_working_on_an_untouched_account(world):
    """An account whose auth.json predates the generation, and a cookie that
    predates it too: nothing to compare, nobody signed out."""
    from password_utils import generate_password_hash
    d = world["tmp"] / "users" / LEGACY
    d.mkdir(parents=True)
    (d / "profile.json").write_text(json.dumps({"email": LEGACY}), encoding="utf-8")
    (d / "auth.json").write_text(json.dumps({
        "password_hash": generate_password_hash(LEGACY_PW),
        "must_change_password": False}), encoding="utf-8")
    assert _generation(LEGACY) == ""
    tc = _client()
    tc.cookies.set("session", _legacy_cookie({"email": LEGACY, "sid": "s_0123456789abcdef"}))
    r = tc.get("/auth/profile")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert r.json().get("email") == LEGACY


# ===========================================================================
# the gate itself
# ===========================================================================
def _run_gate(session: dict):
    reached, sent = [], []

    async def inner(scope, receive, send):
        reached.append(dict(scope.get("session") or {}))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "GET", "scheme": "https", "path": "/auth/profile",
        "raw_path": b"/auth/profile", "root_path": "", "query_string": b"",
        "headers": [], "client": ("127.0.0.1", 50000), "server": ("testserver", 443),
        "session": session,
    }
    gate = getattr(app_mod, "SessionGenerationGate", None)
    if gate is None:
        pytest.fail("app.SessionGenerationGate missing")
    asyncio.run(gate(inner)(scope, receive, send))
    return reached, sent, scope


def test_the_gate_passes_a_session_whose_account_has_no_record(world):
    reached, sent, _ = _run_gate({"email": "a@b.c"})
    assert reached == [{"email": "a@b.c"}], reached
    assert sent and sent[0].get("status") == 200, sent


def test_the_gate_passes_an_anonymous_session(world):
    reached, _, _ = _run_gate({})
    assert reached == [{}], reached


def test_the_gate_passes_a_matching_generation(world):
    gen = _generation(USER)
    assert gen, "set_password stored no generation"
    reached, _, _ = _run_gate({"email": USER, "gen": gen, "sid": "s_1"})
    assert reached == [{"email": USER, "gen": gen, "sid": "s_1"}], reached


def test_the_gate_empties_a_stale_session_in_place(world):
    session = {"email": USER, "gen": "0000000000000000", "sid": "s_1", "remember": True}
    reached, sent, scope = _run_gate(session)
    assert reached == [{}], "the stale session reached the app"
    assert scope["session"] == {}, scope["session"]
    assert session == {}, "the session dict was replaced instead of emptied"
    assert sent and sent[0].get("status") == 200, "the request must continue as anonymous"


def test_the_gate_treats_a_missing_gen_as_empty(world):
    """A cookie without `gen` on an account that HAS a generation is stale."""
    assert _generation(USER)
    reached, _, _ = _run_gate({"email": USER, "sid": "s_1"})
    assert reached == [{}], reached


# ===========================================================================
# the middleware order
# ===========================================================================
def test_the_gate_sits_inside_the_session_layer_and_the_guard_stays_outermost():
    classes = [m.cls for m in app_mod.app.user_middleware]
    assert classes[0] is app_mod.BackendNetworkGuard, classes
    gate = getattr(app_mod, "SessionGenerationGate", None)
    assert gate is not None, "app.SessionGenerationGate missing"
    assert gate in classes, classes
    assert classes.index(gate) > classes.index(app_mod.RememberMeSessionMiddleware), \
        "the gate must run INSIDE the session middleware"
