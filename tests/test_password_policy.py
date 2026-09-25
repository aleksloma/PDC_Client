"""The password policy: one minimum length, one message, every place a
password is set.

The contract pinned here:

* `settings.PASSWORD_MIN_LENGTH` (int, the `_int_env` idiom): default 8; a
  value below 4 is raised to 4 (no install can go below the old minimum) and
  an unparsable one falls back to 8.
* `routes.auth.password_rule_error(pw) -> str | None` returns exactly
  "Password must be at least N characters" (N read from the setting at CALL
  time) for a too-short password, else None.
* The rule is enforced with that message wherever a password is SET:
  - `POST /auth/reset/{token}` -> 400, the reset form re-rendered with the
    message, the link NOT consumed;
  - `POST /auth/change_password` (forced change) -> 400, the change form
    with the message, nothing written;
  - `POST /auth/password` (profile change) -> 400 `{"error": message}`; the
    route keeps stripping surrounding whitespace before it measures;
  - the open self-registration branch of `POST /auth/login` (hosted demo
    only) -> 400, the landing page with the message, no account written.
* Sign-in never checks the rule: an account whose existing password is
  shorter keeps signing in until its next change.
* The admin panel's own password modal carries no client-side copy of the
  old 4-character rule (the server's message is the rule).

Offline: DATA_ROOT is tmp_path, every brain call is stubbed, the reset mail
hand-off runs inline. Router-only app (the tests/test_auth_reset_flow.py
idiom).
"""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
from settings import settings

USER = "policy.user@corp.example"
USER_PW = "Policy-passw0rd"
FORCED = "policy.forced@corp.example"
FORCED_PW = "Forced-temp-pw1"
SHORT_OLD = "old.short@corp.example"
NEWCOMER = "newcomer@corp.example"

SEVEN = "Abcde-1"          # 7 characters
EIGHT = "Abcdef-1"         # 8 characters

_ROOT = Path(__file__).resolve().parent.parent


def _msg(n: int) -> str:
    return f"Password must be at least {n} characters"


def _set(monkeypatch, name, value):
    if name not in type(settings).model_fields:
        pytest.fail(f"settings.{name} missing")
    monkeypatch.setattr(settings, name, value)


def _read_auth(tmp, email) -> dict:
    p = tmp / "users" / email / "auth.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    for name, value in (("PUBLIC_BASE_URL", "https://pdc.corp.example"),
                        ("ALLOW_SELF_REGISTRATION", False),
                        ("PASSWORD_MIN_LENGTH", 8)):
        if name in type(settings).model_fields:
            monkeypatch.setattr(settings, name, value)
    index = getattr(local_store, "_RESET_TOKEN_INDEX", None)
    if isinstance(index, dict):
        index.clear()

    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "send_password_reset_email",
                        lambda email, reset_url, **kw: {"ok": True})
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    monkeypatch.setattr(auth_mod, "_run_in_background",
                        lambda fn, *args: fn(*args), raising=False)

    store = local_store.AuthStore()
    store.ensure_user(USER)
    store.set_password(USER, USER_PW)
    store.ensure_user(FORCED)
    store.set_password(FORCED, FORCED_PW, force_change=True)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)

    def client():
        return TestClient(app, raise_server_exceptions=False)

    return {"tmp": tmp_path, "client": client, "auth_mod": auth_mod}


def _login(tc, email, password):
    return tc.post("/auth/login", data={"email": email, "password": password},
                   follow_redirects=False)


def _rule():
    import routes.auth as auth_mod
    fn = getattr(auth_mod, "password_rule_error", None)
    if fn is None:
        pytest.fail("routes.auth.password_rule_error missing")
    return fn


# ===========================================================================
# the setting
# ===========================================================================
def test_the_default_minimum_is_eight(monkeypatch):
    from settings import Settings
    monkeypatch.delenv("PASSWORD_MIN_LENGTH", raising=False)
    value = getattr(Settings(), "PASSWORD_MIN_LENGTH", None)   # bound: the Settings repr carries SECRET_KEY
    assert value == 8, value


@pytest.mark.parametrize("raw,expected", [
    ("12", 12), ("8", 8), ("4", 4), ("3", 4), ("0", 4), ("-5", 4),
    ("abc", 8), ("", 8), ("1e6", 8),
], ids=["12", "8", "4", "3-floors", "0-floors", "negative-floors",
        "garbage", "empty", "scientific"])
def test_the_minimum_floors_at_four_and_survives_garbage(monkeypatch, raw, expected):
    """No install can go below the old 4-character minimum, and a typo in
    the environment never crashes Settings()."""
    from settings import Settings
    monkeypatch.setenv("PASSWORD_MIN_LENGTH", raw)
    value = getattr(Settings(), "PASSWORD_MIN_LENGTH", None)   # bound: the Settings repr carries SECRET_KEY
    assert value == expected, value


# ===========================================================================
# the helper
# ===========================================================================
def test_the_helper_rejects_seven_and_accepts_eight(world):
    rule = _rule()
    assert rule(SEVEN) == _msg(8)
    assert rule("") == _msg(8)
    assert rule(EIGHT) is None
    assert rule("x" * 200) is None


def test_the_helper_reads_the_setting_at_call_time(world, monkeypatch):
    rule = _rule()
    _set(monkeypatch, "PASSWORD_MIN_LENGTH", 10)
    assert rule("A" * 9) == _msg(10)
    assert rule("A" * 10) is None
    _set(monkeypatch, "PASSWORD_MIN_LENGTH", 4)
    assert rule("abc") == _msg(4)
    assert rule("abcd") is None


# ===========================================================================
# the reset-link form
# ===========================================================================
def test_the_reset_form_rejects_seven_characters_and_consumes_nothing(world):
    token = local_store.AuthStore().create_reset_token(USER)
    assert token
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}",
                data={"new_password": SEVEN, "confirm_password": SEVEN},
                follow_redirects=False)
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert _msg(8) in r.text, r.text[:600]
    assert "Set a new password" in r.text, "not the reset form"
    assert _read_auth(world["tmp"], USER).get("reset_used") is False
    assert tc.get(f"/auth/reset/{token}").status_code == 200, "the link was consumed"
    assert local_store.AuthStore().verify_password(USER, SEVEN) is None
    assert local_store.AuthStore().verify_password(USER, USER_PW) == "ok"


def test_the_reset_form_accepts_eight_characters(world):
    token = local_store.AuthStore().create_reset_token(USER)
    r = world["client"]().post(f"/auth/reset/{token}",
                               data={"new_password": EIGHT, "confirm_password": EIGHT},
                               follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert local_store.AuthStore().verify_password(USER, EIGHT) == "ok"


def test_the_reset_form_follows_the_setting(world, monkeypatch):
    _set(monkeypatch, "PASSWORD_MIN_LENGTH", 10)
    token = local_store.AuthStore().create_reset_token(USER)
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}",
                data={"new_password": "Abcdefgh-", "confirm_password": "Abcdefgh-"},
                follow_redirects=False)
    assert r.status_code == 400, r.status_code
    assert _msg(10) in r.text, r.text[:600]


# ===========================================================================
# the forced-change form
# ===========================================================================
def _forced_session(world):
    tc = world["client"]()
    r = _login(tc, FORCED, FORCED_PW)
    assert r.status_code == 302 and r.headers["location"] == "/auth/change_password", \
        (r.status_code, r.headers.get("location"))
    return tc


def test_the_forced_change_rejects_seven_characters(world):
    tc = _forced_session(world)
    before = _read_auth(world["tmp"], FORCED).get("password_hash")
    r = tc.post("/auth/change_password",
                data={"new_password": SEVEN, "confirm_password": SEVEN},
                follow_redirects=False)
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert _msg(8) in r.text, r.text[:600]
    assert _read_auth(world["tmp"], FORCED).get("password_hash") == before
    assert _read_auth(world["tmp"], FORCED).get("must_change_password") is True


def test_the_forced_change_accepts_eight_characters(world):
    tc = _forced_session(world)
    r = tc.post("/auth/change_password",
                data={"new_password": EIGHT, "confirm_password": EIGHT},
                follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert local_store.AuthStore().verify_password(FORCED, EIGHT) == "ok"


# ===========================================================================
# the profile change (JSON)
# ===========================================================================
def _signed_in(world):
    tc = world["client"]()
    assert _login(tc, USER, USER_PW).status_code == 302
    return tc


def test_the_profile_change_rejects_seven_characters(world):
    tc = _signed_in(world)
    r = tc.post("/auth/password", json={"current_password": USER_PW, "new_password": SEVEN})
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert r.json() == {"error": _msg(8)}, r.json()
    assert local_store.AuthStore().verify_password(USER, USER_PW) == "ok"


def test_the_profile_change_measures_after_stripping(world):
    """The route keeps its `.strip()`: seven characters padded with spaces
    are still seven characters."""
    tc = _signed_in(world)
    r = tc.post("/auth/password",
                json={"current_password": USER_PW, "new_password": "  " + SEVEN + "  "})
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert r.json() == {"error": _msg(8)}, r.json()


def test_the_profile_change_accepts_eight_characters(world):
    tc = _signed_in(world)
    r = tc.post("/auth/password", json={"current_password": USER_PW, "new_password": EIGHT})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert local_store.AuthStore().verify_password(USER, EIGHT) == "ok"


def test_the_profile_change_follows_the_setting(world, monkeypatch):
    _set(monkeypatch, "PASSWORD_MIN_LENGTH", 10)
    tc = _signed_in(world)
    r = tc.post("/auth/password",
                json={"current_password": USER_PW, "new_password": "A" * 9})
    assert r.status_code == 400, r.status_code
    assert r.json() == {"error": _msg(10)}, r.json()
    ok = tc.post("/auth/password",
                 json={"current_password": USER_PW, "new_password": "A" * 10})
    assert ok.status_code == 200, (ok.status_code, ok.text[:300])


# ===========================================================================
# the open self-registration branch (hosted demo only)
# ===========================================================================
def test_self_registration_rejects_seven_characters_and_writes_nothing(world, monkeypatch):
    _set(monkeypatch, "ALLOW_SELF_REGISTRATION", True)
    tc = world["client"]()
    r = _login(tc, NEWCOMER, SEVEN)
    assert r.status_code == 400, (r.status_code, r.headers.get("location"))
    assert _msg(8) in r.text, r.text[:600]
    assert not (world["tmp"] / "users" / NEWCOMER / "auth.json").exists()
    assert not local_store.AuthStore().user_exists(NEWCOMER)
    assert tc.get("/auth/me").status_code == 401, "a refused registration signed in"


def test_self_registration_accepts_eight_characters(world, monkeypatch):
    _set(monkeypatch, "ALLOW_SELF_REGISTRATION", True)
    r = _login(world["client"](), NEWCOMER, EIGHT)
    assert r.status_code == 302 and r.headers["location"] == "/lab", \
        (r.status_code, r.headers.get("location"))
    assert local_store.AuthStore().verify_password(NEWCOMER, EIGHT) == "ok"


# ===========================================================================
# sign-in never checks the rule
# ===========================================================================
def test_an_existing_short_password_still_signs_in(world):
    store = local_store.AuthStore()
    store.ensure_user(SHORT_OLD)
    store.set_password(SHORT_OLD, "abcd")
    r = _login(world["client"](), SHORT_OLD, "abcd")
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == "/lab"


def test_an_existing_short_password_still_signs_in_under_a_higher_setting(world, monkeypatch):
    _set(monkeypatch, "PASSWORD_MIN_LENGTH", 12)
    store = local_store.AuthStore()
    store.ensure_user(SHORT_OLD)
    store.set_password(SHORT_OLD, "abcdefgh")
    r = _login(world["client"](), SHORT_OLD, "abcdefgh")
    assert r.status_code == 302 and r.headers["location"] == "/lab"


# ===========================================================================
# the admin panel carries no copy of the old rule
# ===========================================================================
def test_the_admin_panel_has_no_client_side_length_rule():
    src = (_ROOT / "static" / "admin_data_sources.js").read_text(encoding="utf-8")
    assert "length < 4" not in src, "the admin modal still pre-checks 4 characters"
    assert "at least 4" not in src, "the admin modal still carries the old message"
