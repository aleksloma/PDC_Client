"""A session ends a fixed time after sign-in, however often it is renewed.

The contract pinned here:

* `settings.REMEMBER_ME_MAX_DAYS` (int, the `_int_env` idiom): default 30,
  at least 1, garbage falls back to 30. `app._REMEMBER_ME_MAX_AGE` equals
  `REMEMBER_ME_MAX_DAYS * 86400` at import and still feeds the session
  middleware's `max_age` (the literal `https_only=settings.SESSION_HTTPS_ONLY`
  stays in app.py).
* `app._session_clock` (a module attribute, default `time.time`) is the
  clock the session layer reads; the tests replace it. Every sign-in stamps
  `session["iat"] = int(clock())` -- a fresh sign-in restarts the lifetime --
  and sign-out pops it.
* The session middleware (`RememberMeSessionMiddleware`, whose cap is its
  own `max_age`) computes `elapsed = now - iat`: at `elapsed >= cap` the
  session is refused (emptied: APIs answer 401) and the clearing cookie is
  sent; before that a remembered session's cookie is re-issued with
  `Max-Age = cap - elapsed` -- the REMAINING lifetime, so the browser's
  expiry stays sign-in + cap after any number of renewals. A browser-session
  cookie (no "remember") carries no Max-Age but is refused at the cap too.
* A cookie issued before `iat` existed takes its signer timestamp as `iat`
  (written into the session) and is refused once that time + cap has passed.

About the two clocks: the itsdangerous signer checks its OWN age against
REAL `time.time()` (signature older than `max_age` -> rejected before any of
this runs). The fake clock here therefore always starts at (or after) real
time and only moves forward, so a freshly re-signed cookie is never "older"
than `max_age` by real time; every lifetime decision under test comes from
the fake clock. Offsets are read back from the cookie's own `iat` so the
assertions are exact integers.

Real app, TestClient without the context manager (no lifespan), https
base_url so the cookie comes back under either SESSION_HTTPS_ONLY value.
Offline: DATA_ROOT is tmp_path, brain calls stubbed.
"""
import json
import re
import time
from base64 import b64decode, b64encode
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from itsdangerous import TimestampSigner
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings
from tests.conftest import JSON_HEADERS, csrf_form

USER = "life.user@corp.example"
USER_PW = "Life-user-passw0rd"
LEGACY = "life.legacy@corp.example"
LEGACY_PW = "Life-legacy-passw0rd"
DAY = 86400
CAP = app_mod._REMEMBER_ME_MAX_AGE      # the real app's cap (30 days by default)

_ROOT = Path(__file__).resolve().parent.parent
_MAX_AGE_RE = re.compile(r"max-age=(\d+)", re.I)


class FakeClock:
    def __init__(self, start):
        self.now = int(start)

    def __call__(self):
        return self.now


@pytest.fixture
def clock(monkeypatch):
    # raising=False: before the seam exists the tests still run and fail on
    # the behaviour they pin, not on the patch.
    c = FakeClock(time.time())
    monkeypatch.setattr(app_mod, "_session_clock", c, raising=False)
    return c


@pytest.fixture
def world(tmp_path, monkeypatch, clock):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    if "ALLOW_SELF_REGISTRATION" in type(settings).model_fields:
        monkeypatch.setattr(settings, "ALLOW_SELF_REGISTRATION", False)
    local_store._DATAFRAME_CACHE.invalidate()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    store = local_store.AuthStore()
    store.ensure_user(USER)
    store.set_password(USER, USER_PW)
    yield {"tmp": tmp_path, "clock": clock}
    local_store._DATAFRAME_CACHE.invalidate()


def _client():
    return TestClient(app_mod.app, base_url="https://testserver",
                      raise_server_exceptions=False)


def _sign_in(tc, remember=True, email=USER, password=USER_PW):
    data = {"email": email, "password": password}
    if remember:
        data["remember"] = "on"
    r = tc.post("/auth/login", data=csrf_form(tc, data), follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    return r


def _set_cookie(r) -> str:
    return "; ".join(r.headers.get_list("set-cookie"))


def _max_age(r):
    m = _MAX_AGE_RE.search(_set_cookie(r))
    return int(m.group(1)) if m else None


def _assert_cleared(r):
    raw = _set_cookie(r).lower()
    assert "session=null" in raw, raw
    assert "expires=thu, 01 jan 1970" in raw, raw


def _session_of(tc, secret=None) -> dict:
    raw = tc.cookies.get("session")
    assert raw, "no session cookie"
    data = TimestampSigner(str(secret or settings.SECRET_KEY)).unsign(raw.encode("utf-8"))
    return json.loads(b64decode(data))


def _session_in(r) -> dict:
    """The session a RESPONSE re-issued, read from its Set-Cookie header (a
    hand-set cookie and the server's one may sit in the jar side by side)."""
    m = re.search(r"session=([^;]+)", _set_cookie(r))
    assert m and m.group(1) != "null", ("no session cookie issued", _set_cookie(r))
    data = TimestampSigner(str(settings.SECRET_KEY)).unsign(m.group(1).encode("utf-8"))
    return json.loads(b64decode(data))


def _iat(tc, secret=None) -> int:
    session = _session_of(tc, secret)
    assert isinstance(session.get("iat"), int), ("no iat stamped at sign-in", session)
    return session["iat"]


# ===========================================================================
# the setting and the wiring
# ===========================================================================
def test_the_default_is_thirty_days(monkeypatch):
    from settings import Settings
    monkeypatch.delenv("REMEMBER_ME_MAX_DAYS", raising=False)
    value = getattr(Settings(), "REMEMBER_ME_MAX_DAYS", None)   # bound: the Settings repr carries SECRET_KEY
    assert value == 30, value


@pytest.mark.parametrize("raw,expected", [("7", 7), ("1", 1), ("0", 1), ("-3", 1),
                                          ("abc", 30), ("", 30)])
def test_the_days_floor_at_one_and_survive_garbage(monkeypatch, raw, expected):
    from settings import Settings
    monkeypatch.setenv("REMEMBER_ME_MAX_DAYS", raw)
    value = getattr(Settings(), "REMEMBER_ME_MAX_DAYS", None)   # bound: the Settings repr carries SECRET_KEY
    assert value == expected, value


def test_the_app_cap_follows_the_setting_and_the_clock_seam_exists():
    days = getattr(settings, "REMEMBER_ME_MAX_DAYS", None)
    assert isinstance(days, int), "settings.REMEMBER_ME_MAX_DAYS missing"
    assert app_mod._REMEMBER_ME_MAX_AGE == days * DAY
    assert callable(getattr(app_mod, "_session_clock", None)), "app._session_clock missing"
    assert app_mod._session_clock is time.time


def test_the_session_middleware_wiring_is_unchanged():
    src = (_ROOT / "app.py").read_text(encoding="utf-8")
    assert "https_only=settings.SESSION_HTTPS_ONLY" in src
    assert "max_age=_REMEMBER_ME_MAX_AGE" in src


# ===========================================================================
# the absolute lifetime on the real app
# ===========================================================================
def test_sign_in_stamps_iat_from_the_session_clock(world):
    tc = _client()
    _sign_in(tc)
    assert _iat(tc) == world["clock"].now


def test_renewals_count_down_the_remaining_lifetime(world):
    tc = _client()
    login = _sign_in(tc)
    t0 = _iat(tc)
    assert _max_age(login) == CAP, _set_cookie(login)
    for days in (1, 10, 20):
        world["clock"].now = t0 + days * DAY
        r = tc.get("/auth/profile")
        assert r.status_code == 200, (days, r.status_code, r.text[:200])
        assert _max_age(r) == CAP - days * DAY, (days, _set_cookie(r))
        assert _iat(tc) == t0, "a renewal must not move the sign-in time"


def test_the_session_is_refused_at_the_cap_despite_renewals(world):
    tc = _client()
    _sign_in(tc)
    t0 = _iat(tc)
    for days in (1, 10, 20, 29):
        world["clock"].now = t0 + days * DAY
        assert tc.get("/auth/profile").status_code == 200, days
    world["clock"].now = t0 + CAP
    r = tc.get("/auth/profile")
    assert r.status_code == 401, (r.status_code, r.text[:200])
    _assert_cleared(r)
    assert tc.get("/auth/me").status_code == 401


def test_a_page_redirects_to_the_landing_after_the_cap(world):
    tc = _client()
    _sign_in(tc)
    world["clock"].now = _iat(tc) + CAP + 5
    r = tc.get("/lab", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/", \
        (r.status_code, r.headers.get("location"))
    _assert_cleared(r)


def test_a_fresh_sign_in_restarts_the_lifetime(world):
    tc = _client()
    _sign_in(tc)
    t0 = _iat(tc)
    world["clock"].now = t0 + 29 * DAY
    login = _sign_in(tc)
    t1 = _iat(tc)
    assert t1 == t0 + 29 * DAY, (t0, t1)
    assert _max_age(login) == CAP, _set_cookie(login)
    world["clock"].now = t0 + 31 * DAY
    r = tc.get("/auth/profile")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert _max_age(r) == CAP - 2 * DAY, _set_cookie(r)


def test_a_browser_session_cookie_ends_at_the_cap_too(world):
    tc = _client()
    login = _sign_in(tc, remember=False)
    t0 = _iat(tc)
    assert _max_age(login) is None, _set_cookie(login)
    world["clock"].now = t0 + 10 * DAY
    r = tc.get("/auth/profile")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert _max_age(r) is None, _set_cookie(r)
    world["clock"].now = t0 + CAP
    r = tc.get("/auth/profile")
    assert r.status_code == 401, (r.status_code, r.text[:200])
    _assert_cleared(r)


def test_sign_out_pops_iat(world):
    tc = _client()
    _sign_in(tc)
    assert "iat" in _session_of(tc), "sign-in stamped no iat"
    r = tc.post("/auth/logout", headers=JSON_HEADERS, follow_redirects=False)
    assert r.status_code == 302
    _assert_cleared(r)


# ===========================================================================
# a cookie issued before iat existed
# ===========================================================================
class _PinnedSigner(TimestampSigner):
    def __init__(self, secret, ts):
        super().__init__(secret)
        self._ts = int(ts)

    def get_timestamp(self):
        return self._ts


def _legacy_account(world):
    """An account whose auth.json predates the session generation, so a
    crafted cookie without `gen` is not refused for that reason."""
    from password_utils import generate_password_hash
    d = world["tmp"] / "users" / LEGACY
    d.mkdir(parents=True)
    (d / "profile.json").write_text(json.dumps({"email": LEGACY}), encoding="utf-8")
    (d / "auth.json").write_text(json.dumps({
        "password_hash": generate_password_hash(LEGACY_PW),
        "must_change_password": False}), encoding="utf-8")


def _cookie_without_iat(signed_at: int) -> str:
    session = {"email": LEGACY, "sid": "s_0123456789abcdef", "remember": True}
    data = b64encode(json.dumps(session).encode("utf-8"))
    return _PinnedSigner(str(settings.SECRET_KEY), signed_at).sign(data).decode("utf-8")


def test_a_cookie_without_iat_takes_its_signer_timestamp(world):
    _legacy_account(world)
    signed_at = int(time.time())            # real time: the signer's own check passes
    tc = _client()
    tc.cookies.set("session", _cookie_without_iat(signed_at))
    world["clock"].now = signed_at + DAY
    r = tc.get("/auth/profile")
    assert r.status_code == 200, (r.status_code, r.text[:200])
    assert _session_in(r).get("iat") == signed_at, "the signer timestamp was not written as iat"
    assert _max_age(r) == CAP - DAY, _set_cookie(r)


def test_a_cookie_without_iat_is_refused_after_its_signer_timestamp_plus_the_cap(world):
    _legacy_account(world)
    signed_at = int(time.time())
    tc = _client()
    tc.cookies.set("session", _cookie_without_iat(signed_at))
    world["clock"].now = signed_at + CAP
    r = tc.get("/auth/profile")
    assert r.status_code == 401, (r.status_code, r.text[:200])
    _assert_cleared(r)


# ===========================================================================
# the middleware honours its own max_age as the cap
# ===========================================================================
def test_the_middleware_cap_is_its_max_age(clock):
    """The app builds the middleware from REMEMBER_ME_MAX_DAYS; a middleware
    constructed with another max_age (here two days) enforces THAT cap."""
    secret = "lifetime-test-secret"
    small = FastAPI()
    small.add_middleware(app_mod.RememberMeSessionMiddleware, secret_key=secret,
                         same_site="lax", max_age=2 * DAY, https_only=False)

    @small.post("/in")
    async def sign_in(request: Request):
        request.session["email"] = "small@corp.example"
        request.session["remember"] = True
        request.session["iat"] = int(clock())
        return {"ok": True}

    @small.get("/who")
    async def who(request: Request):
        email = request.session.get("email")
        if not email:
            return JSONResponse({"error": "Not authenticated"}, status_code=401)
        return {"email": email}

    tc = TestClient(small)
    first = tc.post("/in")
    assert _max_age(first) == 2 * DAY, _set_cookie(first)
    t0 = clock.now
    clock.now = t0 + DAY
    r = tc.get("/who")
    assert r.status_code == 200, r.text
    assert _max_age(r) == DAY, _set_cookie(r)
    clock.now = t0 + 2 * DAY
    r = tc.get("/who")
    assert r.status_code == 401, (r.status_code, r.text)
    _assert_cleared(r)
