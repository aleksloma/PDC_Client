"""A password reset or change ends every OTHER session of the account.

The contract pinned here:

* `auth.json` carries `session_generation` (a string). Both password writers
  replace it: `AuthStore.set_password(...)` RETURNS the new value and
  `AuthStore.consume_reset_token(...)` writes a new one too. Since Task 14b
  (D14b-1) every NEW account gets one at creation (`AuthStore.create_account`,
  behind `ensure_user` / `ensure_invited_user`).
  `AuthStore().session_generation(email) -> str` answers the stored value,
  "" for an EXISTING account that has none (a profile-only account or a
  key-less auth.json written before generations existed — the upgrade rule).
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
    """Updated for Task 14 (D14-5): an address with NEITHER profile.json nor
    auth.json (never seen, or removed) answers the fixed non-hex sentinel
    `local_store.SESSION_GEN_REMOVED`, so a `gen: ""` session of a removed
    account cannot survive. An account that EXISTS without a generation
    (profile only) still answers "" — the upgrade back-compat this test
    was written for."""
    sentinel = getattr(local_store, "SESSION_GEN_REMOVED", None)
    assert isinstance(sentinel, str) and sentinel, "local_store.SESSION_GEN_REMOVED missing"
    assert _generation("never.seen@corp.example") == sentinel
    # Task 14b: the profile-only account is written BY HAND — that is the
    # upgrade shape this pin is about; `ensure_user` now creates an auth.json
    # with a generation, so it can no longer produce it. Intent unchanged.
    d = world["tmp"] / "users" / "profile.only@corp.example"
    d.mkdir(parents=True)
    (d / "profile.json").write_text(json.dumps({"email": "profile.only@corp.example"}),
                                    encoding="utf-8")
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


def test_the_gate_ends_a_session_whose_account_has_no_record(world):
    # A signed-in address with neither profile nor auth record is a removed
    # account: its session must not survive, whatever generation it carries.
    reached, sent, _ = _run_gate({"email": "a@b.c"})
    assert reached == [{}], reached
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


# ===========================================================================
# an unreadable auth record
# ===========================================================================
BROKEN = "gen.broken@corp.example"


def _write_unreadable_auth(tmp, email=BROKEN):
    d = tmp / "users" / email
    d.mkdir(parents=True, exist_ok=True)
    (d / "profile.json").write_text(json.dumps({"email": email}), encoding="utf-8")
    (d / "auth.json").write_text('{"session_generation": "abc', encoding="utf-8")


def _capture_store_log(monkeypatch):
    lines = []

    def rec(sid, level, message, **ctx):
        lines.append(" ".join([str(sid), str(level), str(message)]
                              + [f"{k}={v}" for k, v in ctx.items()]))

    monkeypatch.setattr(local_store, "log_with_sid", rec)
    return lines


def test_an_unreadable_record_answers_empty_and_is_logged_once(world, monkeypatch):
    _write_unreadable_auth(world["tmp"])
    lines = _capture_store_log(monkeypatch)
    answers = [_generation(BROKEN) for _ in range(3)]
    assert answers == ["", "", ""], answers
    hits = [ln for ln in lines if "SESSION_GENERATION_READ_FAILED" in ln]
    assert len(hits) == 1, ("not latched per path", lines)
    assert "auth.json" in hits[0], ("the line does not name the record", hits[0])
    assert '"session_generation"' not in hits[0], "the record's content reached the log"


def test_the_gate_passes_an_empty_generation_on_an_unreadable_record(world):
    _write_unreadable_auth(world["tmp"])
    reached, sent, _ = _run_gate({"email": BROKEN, "gen": "", "sid": "s_1"})
    assert reached == [{"email": BROKEN, "gen": "", "sid": "s_1"}], reached
    assert sent and sent[0].get("status") == 200, sent


def test_the_gate_empties_a_real_generation_on_an_unreadable_record(world):
    _write_unreadable_auth(world["tmp"])
    session = {"email": BROKEN, "gen": "0123456789abcdef", "sid": "s_1"}
    reached, sent, _ = _run_gate(session)
    assert reached == [{}], reached
    assert session == {}, session
    assert sent and sent[0].get("status") == 200, sent


# ===========================================================================
# the cache key
# ===========================================================================
def test_a_cached_lookup_touches_no_filesystem_path(world, monkeypatch):
    """After the first lookup the per-request check is a dictionary read:
    no `_data_root()` (which mkdirs) on a hit."""
    first = _generation(USER)
    assert first
    calls = []
    real = local_store._data_root

    def counting():
        calls.append(1)
        return real()

    monkeypatch.setattr(local_store, "_data_root", counting)
    assert _generation(USER) == first
    assert _generation(USER) == first
    assert calls == [], f"_data_root() called {len(calls)} time(s) on a cache hit"


def test_another_data_root_does_not_see_the_first_roots_value(world, monkeypatch, tmp_path_factory):
    first = _generation(USER)
    assert first
    other = tmp_path_factory.mktemp("other_root")
    monkeypatch.setattr(settings, "DATA_ROOT", str(other))
    # Task 14 (D14-5): the account does not exist under this root, so it
    # answers the removed-account sentinel — any value but `first` proves the
    # cache did not cross roots.
    assert _generation(USER) != first, "a cached value crossed DATA_ROOTs"
    other2 = tmp_path_factory.mktemp("other_root2")
    d2 = other2 / "users" / USER
    d2.mkdir(parents=True)
    (d2 / "auth.json").write_text(json.dumps({"session_generation": "0011223344556677"}),
                                  encoding="utf-8")
    monkeypatch.setattr(settings, "DATA_ROOT", str(other2))
    assert _generation(USER) == "0011223344556677"
    monkeypatch.setattr(settings, "DATA_ROOT", str(world["tmp"]))
    assert _generation(USER) == first


def test_set_password_refreshes_the_cached_value_after_a_hit(world):
    before = _generation(USER)
    assert _generation(USER) == before                    # a cache hit
    after = local_store.AuthStore().set_password(USER, NEW_PW)
    assert _generation(USER) == after != before


# ===========================================================================
# the cache is per process
# ===========================================================================
def test_an_out_of_process_change_is_seen_only_after_a_restart(world):
    """The generation cache lives in the web process. A change written to
    auth.json by another process (an operator editing the file, a script in
    the container) is NOT seen by a running web container: an open session
    keeps working until the web container is restarted, which empties the
    cache (modelled here by clearing it)."""
    a = _signed_in()
    assert a.get("/auth/profile").status_code == 200        # value now cached
    p = world["tmp"] / "users" / USER / "auth.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["session_generation"] = "feedfacefeedface"
    p.write_text(json.dumps(rec), encoding="utf-8")
    still = a.get("/auth/profile")
    assert still.status_code == 200, (still.status_code, still.text[:200])
    local_store._SESSION_GEN_CACHE.clear()                  # a restart
    ended = a.get("/auth/profile")
    assert ended.status_code == 401, (ended.status_code, ended.text[:200])
