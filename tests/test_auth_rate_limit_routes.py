"""Sign-in / reset attempt limiting on the real routes (Task 9, R-08).

The routes call `auth_limiter.begin(kind, email, ip)` BEFORE they evaluate
anything and `auth_limiter.success(...)` on a successful sign-in; a refused
attempt answers 429 with a `Retry-After` header and the landing page saying
"Too many attempts. Please try again later." -- no credential evaluation (no
PBKDF2), no mail. The peer is `request.client.host` (`"testclient"` for every
TestClient here).

What counts (plan D9-14): `/auth/login` every attempt (retracted on
success); `/auth/reset_password` every request, in its own `reset:` bucket;
`GET/POST /auth/reset/{token}` only an invalid, expired or used token (per
IP); the admin invite route is not limited.

`auth_limiter.clock` is replaced by a fake clock, so nothing here waits; the
thresholds are the defaults unless a test lowers the IP threshold to keep the
number of PBKDF2 evaluations small (the default of 20 is pinned in
tests/test_auth_limiter.py).

Offline: DATA_ROOT is tmp_path, the brain calls are stubbed, the background
mail hand-off runs inline. Router-only apps.
"""
import importlib

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
from settings import settings

USER = "limited.user@corp.example"
USER_PW = "Limited-passw0rd"
OTHER = "other.user@corp.example"
OTHER_PW = "Other-passw0rd"
TOO_MANY = "Too many attempts. Please try again later."


class FakeClock:
    def __init__(self, start: float = 2_000_000.0):
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _limiter():
    try:
        return importlib.import_module("auth_limiter")
    except ImportError:
        return None


def _need(mod):
    if mod is None:
        pytest.fail("auth_limiter module missing")
    return mod


def _set(monkeypatch, name, value):
    if name not in type(settings).model_fields:
        pytest.fail(f"settings.{name} missing")
    monkeypatch.setattr(settings, name, value)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    for name, value in (("AUTH_FAIL_THRESHOLD", 5), ("AUTH_FAIL_THRESHOLD_IP", 20),
                        ("AUTH_FAIL_WINDOW_S", 900), ("AUTH_LOCKOUT_S", 900),
                        ("PUBLIC_BASE_URL", "https://pdc.corp.example"),
                        ("ALLOW_SELF_REGISTRATION", False)):
        if name in type(settings).model_fields:
            monkeypatch.setattr(settings, name, value)
    clock = FakeClock()
    lim = _limiter()
    if lim is not None:
        monkeypatch.setattr(lim, "clock", clock)
        lim.reset()

    import password_utils
    import routes.auth as auth_mod
    sent = []
    monkeypatch.setattr(brain_client, "send_password_reset_email",
                        lambda email, reset_url, **kw: sent.append((email, reset_url)))
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    monkeypatch.setattr(auth_mod, "_run_in_background",
                        lambda fn, *args: fn(*args), raising=False)

    real_check = password_utils.check_password_hash
    verifications = []

    def counting(*args, **kwargs):
        verifications.append(1)
        return real_check(*args, **kwargs)

    monkeypatch.setattr(password_utils, "check_password_hash", counting)
    for mod in (auth_mod, local_store):
        if hasattr(mod, "check_password_hash"):
            monkeypatch.setattr(mod, "check_password_hash", counting)

    store = local_store.AuthStore()
    for email, pw in ((USER, USER_PW), (OTHER, OTHER_PW)):
        store.ensure_user(email)
        store.set_password(email, pw)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)
    yield {"clock": clock, "lim": lim, "sent": sent, "verifications": verifications,
           "client": lambda: TestClient(app, raise_server_exceptions=False),
           "app": app, "auth_mod": auth_mod}
    if lim is not None:
        lim.reset()


def _login(world, email, password):
    return world["client"]().post("/auth/login", data={"email": email, "password": password},
                                  follow_redirects=False)


def _assert_limited(r, retry_after=None):
    assert r.status_code == 429, (r.status_code, r.headers.get("location"))
    assert TOO_MANY in r.text
    header = r.headers.get("retry-after")
    assert header is not None and header.isdigit(), r.headers
    if retry_after is not None:
        assert int(header) == retry_after, header
    return int(header)


def _five_wrong(world, email=USER):
    for i in range(5):
        r = _login(world, email, f"wrong-{i}")
        assert r.status_code == 401, (i + 1, r.status_code)


# ===========================================================================
# /auth/login
# ===========================================================================
def test_five_wrong_passwords_are_answered_then_the_sixth_must_wait(world):
    _need(world["lim"])
    _five_wrong(world)
    assert len(world["verifications"]) == 5
    _assert_limited(_login(world, USER, "wrong-6"), retry_after=1)
    assert len(world["verifications"]) == 5, "a refused attempt must not run PBKDF2"


def test_advancing_the_clock_admits_the_next_attempt(world):
    _need(world["lim"])
    _five_wrong(world)
    _assert_limited(_login(world, USER, "wrong-6"), retry_after=1)
    world["clock"].advance(1)
    assert _login(world, USER, "wrong-6").status_code == 401


def test_the_schedule_then_the_lockout_refuses_the_correct_password(world):
    _need(world["lim"])
    _five_wrong(world)
    for gap in (1, 2, 4, 8):
        _assert_limited(_login(world, USER, "early"), retry_after=gap)
        world["clock"].advance(gap)
        assert _login(world, USER, f"wrong-after-{gap}").status_code == 401, gap
    world["clock"].advance(16)
    tc = world["client"]()
    r = tc.post("/auth/login", data={"email": USER, "password": USER_PW},
                follow_redirects=False)
    wait = _assert_limited(r)
    assert 800 <= wait <= 900, wait
    assert tc.get("/auth/me").status_code == 401, "no session during the lockout"


def test_the_lockout_ends(world):
    _need(world["lim"])
    _five_wrong(world)
    for gap in (1, 2, 4, 8):
        world["clock"].advance(gap)
        assert _login(world, USER, f"wrong-after-{gap}").status_code == 401
    world["clock"].advance(1)
    wait = _assert_limited(_login(world, USER, USER_PW))
    world["clock"].advance(wait + 1)
    r = _login(world, USER, USER_PW)
    assert r.status_code == 302, r.status_code


def test_a_successful_sign_in_clears_the_address(world):
    _need(world["lim"])
    for i in range(4):
        assert _login(world, USER, f"wrong-{i}").status_code == 401
    assert _login(world, USER, USER_PW).status_code == 302
    _five_wrong(world)                         # a fresh five after the success


def test_another_address_from_the_same_peer_is_still_served(world):
    _need(world["lim"])
    _five_wrong(world)
    _assert_limited(_login(world, USER, "wrong-6"))
    assert _login(world, OTHER, OTHER_PW).status_code == 302


def test_after_the_ip_threshold_a_fresh_address_is_spaced_too(world, monkeypatch):
    _need(world["lim"])
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    for i in range(3):
        assert _login(world, f"spray{i}@corp.example", "x-pw-123").status_code == 401
    _assert_limited(_login(world, OTHER, OTHER_PW), retry_after=1)
    world["clock"].advance(1)
    assert _login(world, OTHER, OTHER_PW).status_code == 302


def test_the_lockout_log_line_carries_no_address(world, monkeypatch):
    lim = _need(world["lim"])
    lines = []

    def rec(sid, level, message, **ctx):
        lines.append((str(sid), str(level), str(message), repr(ctx)))

    import logger_utils
    monkeypatch.setattr(world["auth_mod"], "log_with_sid", rec)
    monkeypatch.setattr(lim, "log_with_sid", rec, raising=False)
    monkeypatch.setattr(logger_utils, "log_with_sid", rec)
    _five_wrong(world)
    for gap in (1, 2, 4, 8):
        world["clock"].advance(gap)
        _login(world, USER, "still-wrong")
    for _ in range(3):
        world["clock"].advance(1)
        _assert_limited(_login(world, USER, USER_PW))
    lockouts = [ln for ln in lines if "AUTH_LOCKOUT" in ln[2]]
    assert len(lockouts) == 1, lockouts
    sid, _level, message, ctx = lockouts[0]
    record = " ".join((sid, message, ctx)).lower()
    assert USER not in record, lockouts[0]
    assert "limited.user" not in record, lockouts[0]
    assert "kind=login" in message, message
    assert "h=" in message, message


# ===========================================================================
# /auth/reset_password
# ===========================================================================
def _reset(world, email):
    return world["client"]().post("/auth/reset_password", data={"email": email},
                                  follow_redirects=False)


@pytest.mark.parametrize("email", [USER, "nobody@corp.example"], ids=["known", "unknown"])
def test_every_reset_request_counts_known_or_unknown(world, email):
    _need(world["lim"])
    for i in range(5):
        assert _reset(world, email).status_code == 200, i + 1
    _assert_limited(_reset(world, email), retry_after=1)


def test_a_refused_reset_sends_no_mail(world):
    _need(world["lim"])
    for _ in range(5):
        _reset(world, USER)
    before = len(world["sent"])
    _assert_limited(_reset(world, USER))
    assert len(world["sent"]) == before


def test_reset_requests_do_not_count_against_sign_in(world):
    _need(world["lim"])
    for _ in range(5):
        assert _reset(world, USER).status_code == 200
    _assert_limited(_reset(world, USER))
    assert _login(world, USER, USER_PW).status_code == 302


# ===========================================================================
# /auth/reset/{token}
# ===========================================================================
def test_a_bad_token_counts_per_ip_and_a_valid_get_does_not(world, monkeypatch):
    _need(world["lim"])
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    token = local_store.AuthStore().create_reset_token(USER)
    assert token
    tc = world["client"]()
    for i in range(6):
        assert tc.get(f"/auth/reset/{token}").status_code == 200, i + 1
    for i in range(3):
        assert tc.get(f"/auth/reset/{'B' * 43}").status_code == 404, i + 1
    _assert_limited(tc.get(f"/auth/reset/{'C' * 43}"), retry_after=1)


def test_a_bad_token_post_counts_too(world, monkeypatch):
    _need(world["lim"])
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    tc = world["client"]()
    form = {"new_password": "Guess-pw-1", "confirm_password": "Guess-pw-1"}
    for i in range(3):
        r = tc.post(f"/auth/reset/{'D' * 43}", data=form, follow_redirects=False)
        assert r.status_code == 404, i + 1
    _assert_limited(tc.post(f"/auth/reset/{'E' * 43}", data=form, follow_redirects=False))


def test_a_password_rule_failure_on_a_valid_link_is_not_a_failure(world, monkeypatch):
    _need(world["lim"])
    _set(monkeypatch, "AUTH_FAIL_THRESHOLD_IP", 3)
    token = local_store.AuthStore().create_reset_token(USER)
    tc = world["client"]()
    for i in range(6):
        r = tc.post(f"/auth/reset/{token}",
                    data={"new_password": "abcd-1", "confirm_password": "abcd-2"},
                    follow_redirects=False)
        assert r.status_code == 400, (i + 1, r.status_code)


# ===========================================================================
# the admin invite route is not limited
# ===========================================================================
def test_the_invite_route_is_not_limited(world, monkeypatch):
    _need(world["lim"])
    from cryptography.fernet import Fernet
    import routes.admin_users as users_mod
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    local_store.AuthStore().ensure_user("ladmin")
    local_store.AuthStore().set_role("ladmin", "admin")
    app = world["app"]
    app.include_router(users_mod.router)

    @app.post("/_login/{email}")
    async def _login_route(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app, raise_server_exceptions=False)
    tc.post("/_login/ladmin")
    codes = [tc.post("/api/admin/users/invite", json={"email": f"invitee{i}@corp.example"}).status_code
             for i in range(25)]
    assert codes == [200] * 25, codes


# ===========================================================================
# the configured local admin account is spaced but never locked
#
# An anonymous caller must not be able to lock the operator out: sign-in
# attempts for LOCAL_ADMIN_USERNAME keep the 1, 2, 4, 8 s address spacing
# (and the per-peer spacing), but where another address would be locked for
# AUTH_LOCKOUT_S the admin account only waits another 8 s.
# ===========================================================================
LADMIN = "ladmin"
LADMIN_PW = "Ladmin-passw0rd"


def _bootstrap_ladmin():
    store = local_store.AuthStore()
    store.ensure_user(LADMIN)
    store.set_role(LADMIN, "admin")
    store.set_password(LADMIN, LADMIN_PW)


def _hammer(world, email, attempts, client=None):
    """`attempts` wrong passwords, each admitted after waiting whatever the
    429 asked for. Returns every Retry-After seen."""
    waits = []
    evaluated = 0
    for i in range(attempts * 3):
        if evaluated == attempts:
            break
        tc = client or world["client"]()
        r = tc.post("/auth/login", data={"email": email, "password": f"wrong-{i}"},
                    follow_redirects=False)
        if r.status_code == 429:
            wait = _assert_limited(r)
            waits.append(wait)
            if wait > 8:
                return waits, evaluated
            world["clock"].advance(wait)
            continue
        assert r.status_code == 401, (i, r.status_code)
        evaluated += 1
    return waits, evaluated


def test_the_local_admin_is_spaced_but_never_locked(world, monkeypatch):
    lim = _need(world["lim"])
    _bootstrap_ladmin()
    lines = []

    def rec(sid, level, message, **ctx):
        lines.append(f"{sid} {level} {message} {ctx}")

    monkeypatch.setattr(lim, "log_with_sid", rec, raising=False)
    waits, evaluated = _hammer(world, LADMIN, 14)
    assert waits, "the admin account was never spaced"
    assert max(waits) <= 8, waits
    assert evaluated == 14, (evaluated, waits)
    assert not [ln for ln in lines if "AUTH_LOCKOUT" in ln], lines
    # After waiting, the right password signs in.
    early = world["client"]().post("/auth/login",
                                   data={"email": LADMIN, "password": LADMIN_PW},
                                   follow_redirects=False)
    if early.status_code == 429:
        wait = _assert_limited(early)
        assert wait <= 8, wait
        world["clock"].advance(wait)
    r = world["client"]().post("/auth/login", data={"email": LADMIN, "password": LADMIN_PW},
                               follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.headers.get("retry-after"))
    assert r.headers["location"] == "/admin/data_sources"


def test_a_normal_address_from_another_peer_still_locks(world):
    _need(world["lim"])
    _bootstrap_ladmin()
    _hammer(world, LADMIN, 12)
    other_peer = TestClient(world["app"], raise_server_exceptions=False,
                            client=("10.20.30.40", 50000))
    _five_wrong_from(other_peer, USER)
    for gap in (1, 2, 4, 8):
        world["clock"].advance(gap)
        r = other_peer.post("/auth/login", data={"email": USER, "password": f"w{gap}"},
                            follow_redirects=False)
        assert r.status_code == 401, (gap, r.status_code)
    world["clock"].advance(16)
    r = other_peer.post("/auth/login", data={"email": USER, "password": USER_PW},
                        follow_redirects=False)
    wait = _assert_limited(r)
    assert wait > 8, ("a normal address must still lock", wait)


def _five_wrong_from(tc, email):
    for i in range(5):
        r = tc.post("/auth/login", data={"email": email, "password": f"wrong-{i}"},
                    follow_redirects=False)
        assert r.status_code == 401, (i + 1, r.status_code)
