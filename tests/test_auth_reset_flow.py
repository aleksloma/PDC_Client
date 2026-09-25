"""Tokenized password reset and invitation-only sign-in (Task 9, R-05).

The contract pinned here (plan D9-1 .. D9-9, D9-15):

* `POST /auth/reset_password` (form `email`): a non-email id -> 400 "Please
  enter a valid email"; EVERY regex-valid address -- unknown, known,
  placeholder, legacy, an email-shaped bootstrap admin -- gets the SAME
  request path and the SAME page: 200, "If an account exists for this
  address, a reset link has been sent." The existence check, the mint and
  the mail run in a background hand-off `routes.auth._run_in_background(fn,
  *args)`, which these tests replace with an inline call.
* `AuthStore.create_reset_token(email) -> str | None` (None and nothing
  written for an address with neither profile nor auth record) writes
  `reset_token_hash` (sha256 hex of a fresh `secrets.token_urlsafe(32)`, i.e.
  43 url-safe characters), `reset_expires_at` (epoch seconds, now + 1800) and
  `reset_used: false` into auth.json and returns the raw token; a module-level
  dict `local_store._RESET_TOKEN_INDEX` (hash -> email) is a cache only, the
  lookup falls back to scanning `users/*/auth.json` (the restart case).
  `find_reset_token(token) -> (email, record) | None`,
  `consume_reset_token(token, new_password) -> email | None` (validate + set +
  mark used in ONE locked step), `clear_reset_token(email)`. `set_password`
  on every path drops the reset keys. `_write_auth` writes atomically.
* `brain_client.send_password_reset_email(email, reset_url)` posts
  `{sid, email, reset_url}` -- no temp password. `reset_url` =
  `settings.PUBLIC_BASE_URL.rstrip('/') + '/auth/reset/' + token` when that
  setting starts with http:// or https://; otherwise NO link is minted or
  mailed (D9-26 -- the request's own base URL comes from its Host header),
  the page stays the neutral one and `PASSWORD_RESET_NO_BASE_URL` is logged.
* `GET /auth/reset/{token}`: shape gate `^[A-Za-z0-9_-]{43}$`, lookup,
  expiry, used -- read only. Invalid -> 404 with "This reset link is invalid
  or has expired."; valid -> 200 form (`Cache-Control: no-store`,
  `Referrer-Policy: no-referrer`). `POST /auth/reset/{token}`: same checks,
  the unchanged password rules (a rule failure re-renders, consumes nothing),
  then 302 to `/?reset=done`; the landing then shows "Your password has been
  updated." No automatic sign-in.
* Sign-in is invitation-only: an unknown address creates nothing and gets the
  same 401 page as a wrong password; every failure runs exactly ONE password
  verification (a dummy hash where there is none).
* `_EMAIL_RE` = conventional characters, at most 254 characters; the share
  routes use it too.

Offline: DATA_ROOT is tmp_path, every brain call is stubbed, the background
hand-off is made synchronous. Router-only app (the tests/test_auth_routes.py
idiom) unless the landing page or the real middleware stack matters.
"""
import hashlib
import json
import re
import threading
import time

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
from settings import settings

KNOWN = "known.user@corp.example"
KNOWN_PW = "Known-passw0rd"
PLACEHOLDER = "invited.user@corp.example"
LEGACY = "legacy.user@corp.example"
UNKNOWN = "nobody.here@corp.example"
OWNER = "owner@corp.example"
BASE = "https://pdc.corp.example"

NEUTRAL_RESET = "If an account exists for this address, a reset link has been sent."
INVALID_LINK = "This reset link is invalid or has expired."
RESET_DONE = "Your password has been updated."
NEUTRAL_FAILURE = ("Sign-in failed. Check your email and password, or use "
                   "“Reset password” if you have not set one yet.")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")

_NONCE_RE = re.compile(r"""nonce(?:-[A-Za-z0-9_\-]+|\s*=\s*["'][^"']*["'])"""
                       r"""|__CSP_NONCE__\s*=\s*["'][^"']*["']""")


def _normalise(text: str, email: str) -> str:
    return _NONCE_RE.sub("nonce", text.replace(email, "EMAIL"))


def _set(monkeypatch, name, value):
    if name not in type(settings).model_fields:
        pytest.fail(f"settings.{name} missing")
    monkeypatch.setattr(settings, name, value)


def _store_api(name):
    fn = getattr(local_store.AuthStore, name, None)
    if fn is None:
        pytest.fail(f"AuthStore.{name} missing")
    return getattr(local_store.AuthStore(), name)


def _auth_path(tmp, email):
    return tmp / "users" / email / "auth.json"


def _read_auth(tmp, email) -> dict:
    p = _auth_path(tmp, email)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    if "PUBLIC_BASE_URL" in type(settings).model_fields:
        monkeypatch.setattr(settings, "PUBLIC_BASE_URL", BASE + "/")
    if "ALLOW_SELF_REGISTRATION" in type(settings).model_fields:
        monkeypatch.setattr(settings, "ALLOW_SELF_REGISTRATION", False)
    index = getattr(local_store, "_RESET_TOKEN_INDEX", None)
    if isinstance(index, dict):
        index.clear()

    import routes.auth as auth_mod
    sent = []
    sent_kwargs = []
    fail = {"on": False}

    def fake_reset_mail(email, reset_url, **kwargs):
        if fail["on"]:
            raise brain_client.BrainError("brain answered 400")
        sent.append((email, reset_url))
        sent_kwargs.append(dict(kwargs))
        return {"ok": True}

    monkeypatch.setattr(brain_client, "send_password_reset_email", fake_reset_mail)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    monkeypatch.setattr(auth_mod, "_run_in_background",
                        lambda fn, *args: fn(*args), raising=False)

    store = local_store.AuthStore()
    store.ensure_user(KNOWN)
    store.set_password(KNOWN, KNOWN_PW)
    store.ensure_user(OWNER)
    store.set_password(OWNER, "owner-pw-1")
    store.ensure_invited_user(PLACEHOLDER, OWNER)
    legacy_dir = tmp_path / "users" / LEGACY
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "profile.json").write_text(
        json.dumps({"email": LEGACY, "created_at": "2025-01-01T00:00:00Z"}),
        encoding="utf-8")

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)

    def client():
        # A server-side exception is an answer to assert on, not a crash of
        # the test (today an address with `<` cannot become a folder on some
        # filesystems).
        return TestClient(app, raise_server_exceptions=False)

    return {"tmp": tmp_path, "sent": sent, "sent_kwargs": sent_kwargs, "fail": fail,
            "client": client, "app": app, "auth_mod": auth_mod}


def _request_reset(world, email):
    return world["client"]().post("/auth/reset_password", data={"email": email},
                                  follow_redirects=False)


def _token_from_mail(world, email):
    mails = [url for to, url in world["sent"] if to == email]
    assert mails, f"no reset mail for {email}"
    url = mails[-1]
    assert isinstance(url, str) and "/auth/reset/" in url, "the mail carries no reset link"
    return url.rsplit("/", 1)[1]


def _mint(email):
    token = _store_api("create_reset_token")(email)
    assert token, f"no token minted for {email}"
    return token


def _login(tc, email, password):
    return tc.post("/auth/login", data={"email": email, "password": password},
                   follow_redirects=False)


# ===========================================================================
# settings and the brain call
# ===========================================================================
def test_settings_defaults(monkeypatch):
    from settings import Settings
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    monkeypatch.delenv("ALLOW_SELF_REGISTRATION", raising=False)
    fresh = Settings()
    values = {"PUBLIC_BASE_URL": getattr(fresh, "PUBLIC_BASE_URL", None),
              "ALLOW_SELF_REGISTRATION": getattr(fresh, "ALLOW_SELF_REGISTRATION", None)}
    assert values == {"PUBLIC_BASE_URL": "", "ALLOW_SELF_REGISTRATION": False}, values


@pytest.mark.parametrize("raw,expected", [("true", True), ("1", True), ("yes", True),
                                          ("false", False), ("", False), ("no", False)])
def test_allow_self_registration_parses_like_the_other_flags(monkeypatch, raw, expected):
    from settings import Settings
    monkeypatch.setenv("ALLOW_SELF_REGISTRATION", raw)
    value = getattr(Settings(), "ALLOW_SELF_REGISTRATION", None)
    assert value is expected


class _FakeResp:
    status_code = 200
    text = "{}"

    def json(self):
        return {"ok": True}


def test_the_reset_mail_carries_a_link_and_never_a_password(monkeypatch):
    """Article II / R-05: the brain receives the address and the link only."""
    posted = []

    class _FakeClient:
        def post(self, path, json=None, headers=None, timeout=None):
            posted.append((path, json))
            return _FakeResp()

    monkeypatch.setattr(brain_client, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(brain_client.settings, "BRAIN_TENANT_TOKEN", "tkn", raising=False)
    url = BASE + "/auth/reset/" + "A" * 43
    brain_client.send_password_reset_email(KNOWN, url)
    assert len(posted) == 1, posted
    path, payload = posted[0]
    assert path == "/v1/send_password_reset_email"
    assert set(payload) == {"sid", "email", "reset_url", "kind"}, payload
    assert payload["email"] == KNOWN and payload["reset_url"] == url
    assert payload["kind"] == "reset", payload


def test_the_reset_mail_names_its_kind(monkeypatch):
    """`kind` is always posted: "reset" by default, "invite" when asked, so
    the brain words an invitation as one."""
    posted = []

    class _FakeClient:
        def post(self, path, json=None, headers=None, timeout=None):
            posted.append(json)
            return _FakeResp()

    monkeypatch.setattr(brain_client, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(brain_client.settings, "BRAIN_TENANT_TOKEN", "tkn", raising=False)
    url = BASE + "/auth/reset/" + "A" * 43
    brain_client.send_password_reset_email(KNOWN, url, kind="invite")
    brain_client.send_password_reset_email(KNOWN, url, timeout=5, kind="reset")
    assert [p.get("kind") for p in posted] == ["invite", "reset"], posted
    for payload in posted:
        assert set(payload) == {"sid", "email", "reset_url", "kind"}, payload


def test_the_brain_client_no_longer_mentions_a_temp_password():
    from pathlib import Path
    src = (Path(brain_client.__file__)).read_text(encoding="utf-8")
    assert "temp_password" not in src


# ===========================================================================
# POST /auth/reset_password: one answer for every address
# ===========================================================================
def test_unknown_and_known_addresses_get_the_same_answer(world):
    known = _request_reset(world, KNOWN)
    unknown = _request_reset(world, UNKNOWN)
    assert (known.status_code, unknown.status_code) == (200, 200)
    assert NEUTRAL_RESET in known.text
    assert _normalise(known.text, KNOWN) == _normalise(unknown.text, UNKNOWN)
    assert "does not exist" not in unknown.text


def test_an_unknown_address_leaves_nothing_behind(world):
    r = _request_reset(world, UNKNOWN)
    assert r.status_code == 200
    assert not (world["tmp"] / "users" / UNKNOWN).exists()
    assert [to for to, _ in world["sent"]] == []


def test_the_anonymous_reset_mail_is_of_kind_reset(world):
    assert _request_reset(world, KNOWN).status_code == 200
    assert world["sent_kwargs"], "no reset mail"
    assert world["sent_kwargs"][-1].get("kind", "reset") == "reset", world["sent_kwargs"]


def test_a_known_address_gets_a_token_record_and_a_link(world):
    before = time.time()
    r = _request_reset(world, KNOWN)
    assert r.status_code == 200
    token = _token_from_mail(world, KNOWN)
    assert TOKEN_RE.match(token), token
    url = [u for to, u in world["sent"] if to == KNOWN][-1]
    assert url == f"{BASE}/auth/reset/{token}", "the link is not PUBLIC_BASE_URL + token"
    rec = _read_auth(world["tmp"], KNOWN)
    assert rec.get("reset_token_hash") == _sha(token), rec
    assert rec.get("reset_used") is False, rec
    expires = rec.get("reset_expires_at")
    assert isinstance(expires, (int, float)), rec
    assert before + 1800 - 5 <= expires <= time.time() + 1800 + 5, (before, expires)
    assert token not in json.dumps(rec), "the raw token must never be stored"
    # The user's own password still works until the link is used.
    assert local_store.AuthStore().verify_password(KNOWN, KNOWN_PW) == "ok"
    assert not rec.get("temp_password_hash")


@pytest.mark.parametrize("email", [PLACEHOLDER, LEGACY], ids=["placeholder", "legacy"])
def test_a_password_less_account_gets_a_token_too(world, email):
    r = _request_reset(world, email)
    assert r.status_code == 200 and NEUTRAL_RESET in r.text
    token = _token_from_mail(world, email)
    assert _read_auth(world["tmp"], email).get("reset_token_hash") == _sha(token)


def test_an_email_shaped_bootstrap_admin_gets_the_same_page_and_nothing(world, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "admin@corp.example")
    local_store.AuthStore().ensure_user("admin@corp.example")
    local_store.AuthStore().set_password("admin@corp.example", "admin-pw-1")
    ladmin = _request_reset(world, "admin@corp.example")
    other = _request_reset(world, KNOWN)
    assert ladmin.status_code == 200
    assert _normalise(ladmin.text, "admin@corp.example") == _normalise(other.text, KNOWN)
    rec = _read_auth(world["tmp"], "admin@corp.example")
    assert not rec.get("reset_token_hash") and not rec.get("temp_password_hash"), rec
    assert "admin@corp.example" not in [to for to, _ in world["sent"]]


def test_a_non_email_id_is_still_a_400(world):
    r = _request_reset(world, "not-an-email")
    assert r.status_code == 400
    assert "Please enter a valid email" in r.text


def _record_logs(world, monkeypatch):
    lines = []

    def rec(sid, level, message, **ctx):
        lines.append(f"{sid} {level} {message} {ctx}")

    monkeypatch.setattr(world["auth_mod"], "log_with_sid", rec)
    return lines


def test_without_a_public_base_url_nothing_is_minted_or_mailed(world, monkeypatch):
    """D9-26: no request-address fallback. Unset => no token, no mail, an
    error line, and the caller sees the same page as for an unknown address."""
    _set(monkeypatch, "PUBLIC_BASE_URL", "")
    lines = _record_logs(world, monkeypatch)
    known = _request_reset(world, KNOWN)
    unknown = _request_reset(world, UNKNOWN)
    assert (known.status_code, unknown.status_code) == (200, 200)
    assert NEUTRAL_RESET in known.text
    assert _normalise(known.text, KNOWN) == _normalise(unknown.text, UNKNOWN)
    assert not _read_auth(world["tmp"], KNOWN).get("reset_token_hash")
    assert world["sent"] == [], world["sent"]
    hits = [ln for ln in lines if "PASSWORD_RESET_NO_BASE_URL" in ln]
    assert hits and all(" error " in ln for ln in hits), lines


def test_a_forged_host_header_never_reaches_the_link(world, monkeypatch):
    _set(monkeypatch, "PUBLIC_BASE_URL", "https://pdc.corp.example")
    r = world["client"]().post("/auth/reset_password", data={"email": KNOWN},
                               headers={"Host": "attacker.example"},
                               follow_redirects=False)
    assert r.status_code == 200
    url = [u for to, u in world["sent"] if to == KNOWN][-1]
    assert url.startswith("https://pdc.corp.example/auth/reset/"), url
    assert "attacker" not in url, url


@pytest.mark.parametrize("bad", ["javascript:alert(1)", "pdc.corp.example", "ftp://x.example"])
def test_a_public_base_url_without_an_http_scheme_behaves_as_unset(world, monkeypatch, bad):
    _set(monkeypatch, "PUBLIC_BASE_URL", bad)
    lines = _record_logs(world, monkeypatch)
    assert _request_reset(world, KNOWN).status_code == 200
    assert world["sent"] == [], world["sent"]
    assert not _read_auth(world["tmp"], KNOWN).get("reset_token_hash")
    assert any("PASSWORD_RESET_NO_BASE_URL" in ln for ln in lines), lines


def test_a_failed_send_clears_the_token_and_still_answers_neutrally(world):
    world["fail"]["on"] = True
    r = _request_reset(world, KNOWN)
    assert r.status_code == 200 and NEUTRAL_RESET in r.text
    rec = _read_auth(world["tmp"], KNOWN)
    assert not rec.get("reset_token_hash"), rec
    assert local_store.AuthStore().verify_password(KNOWN, KNOWN_PW) == "ok"


def test_a_second_request_replaces_the_first_token(world):
    _request_reset(world, KNOWN)
    first = _token_from_mail(world, KNOWN)
    _request_reset(world, KNOWN)
    second = _token_from_mail(world, KNOWN)
    assert first != second
    tc = world["client"]()
    assert tc.get(f"/auth/reset/{first}").status_code == 404
    assert tc.get(f"/auth/reset/{second}").status_code == 200


def test_no_log_line_carries_the_token(world, monkeypatch):
    lines = []

    def rec(sid, level, message, **ctx):
        lines.append(f"{sid} {level} {message} {ctx}")

    monkeypatch.setattr(world["auth_mod"], "log_with_sid", rec)
    monkeypatch.setattr(local_store, "log_with_sid", rec)
    _request_reset(world, KNOWN)
    token = _token_from_mail(world, KNOWN)
    tc = world["client"]()
    tc.get(f"/auth/reset/{token}")
    tc.post(f"/auth/reset/{token}", data={"new_password": "N3w-pass", "confirm_password": "N3w-pass"},
            follow_redirects=False)
    tc.get(f"/auth/reset/{token}")
    assert lines, "nothing was logged at all"
    leaked = [ln for ln in lines if token in ln]
    assert leaked == [], leaked


# ===========================================================================
# GET / POST /auth/reset/{token}
# ===========================================================================
def test_the_happy_path(world):
    _request_reset(world, KNOWN)
    token = _token_from_mail(world, KNOWN)
    tc = world["client"]()
    page = tc.get(f"/auth/reset/{token}")
    assert page.status_code == 200, page.text[:300]
    assert page.headers.get("content-type", "").startswith("text/html")
    assert "no-store" in page.headers.get("cache-control", "").lower(), page.headers
    assert page.headers.get("referrer-policy", "").lower() == "no-referrer", page.headers
    assert f'action="/auth/reset/{token}"' in page.text
    r = tc.post(f"/auth/reset/{token}",
                data={"new_password": "Brand-new-pw1", "confirm_password": "Brand-new-pw1"},
                follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == "/?reset=done"
    assert tc.get("/auth/me").status_code == 401, "a reset must not sign the user in"
    store = local_store.AuthStore()
    assert store.verify_password(KNOWN, "Brand-new-pw1") == "ok"
    assert store.verify_password(KNOWN, KNOWN_PW) is None
    login = _login(world["client"](), KNOWN, "Brand-new-pw1")
    assert login.status_code == 302 and login.headers["location"] == "/lab"
    assert not store.get_auth(KNOWN).get("must_change_password")


def test_a_placeholder_signs_in_after_using_its_link(world):
    _request_reset(world, PLACEHOLDER)
    token = _token_from_mail(world, PLACEHOLDER)
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}",
                data={"new_password": "Invited-pw1", "confirm_password": "Invited-pw1"},
                follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/?reset=done"
    login = _login(world["client"](), PLACEHOLDER, "Invited-pw1")
    assert login.status_code == 302 and login.headers["location"] == "/lab"


def test_a_valid_get_has_no_side_effect(world):
    token = _mint(KNOWN)
    before = _auth_path(world["tmp"], KNOWN).read_bytes()
    tc = world["client"]()
    for _ in range(3):
        assert tc.get(f"/auth/reset/{token}").status_code == 200
    assert _auth_path(world["tmp"], KNOWN).read_bytes() == before


def _expire(world, email):
    p = _auth_path(world["tmp"], email)
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["reset_expires_at"] = time.time() - 10
    p.write_text(json.dumps(rec), encoding="utf-8")


def test_an_expired_token_is_404_on_get_and_post(world):
    token = _mint(KNOWN)
    _expire(world, KNOWN)
    tc = world["client"]()
    r = tc.get(f"/auth/reset/{token}")
    assert r.status_code == 404 and INVALID_LINK in r.text
    r = tc.post(f"/auth/reset/{token}",
                data={"new_password": "Late-pw-123", "confirm_password": "Late-pw-123"},
                follow_redirects=False)
    assert r.status_code == 404 and INVALID_LINK in r.text
    assert local_store.AuthStore().verify_password(KNOWN, KNOWN_PW) == "ok"


def test_a_used_token_is_404_and_changes_nothing(world):
    token = _mint(KNOWN)
    tc = world["client"]()
    first = tc.post(f"/auth/reset/{token}",
                    data={"new_password": "First-pw-1", "confirm_password": "First-pw-1"},
                    follow_redirects=False)
    assert first.status_code == 302
    again = tc.post(f"/auth/reset/{token}",
                    data={"new_password": "Second-pw-2", "confirm_password": "Second-pw-2"},
                    follow_redirects=False)
    assert again.status_code == 404 and INVALID_LINK in again.text
    assert tc.get(f"/auth/reset/{token}").status_code == 404
    store = local_store.AuthStore()
    assert store.verify_password(KNOWN, "First-pw-1") == "ok"
    assert store.verify_password(KNOWN, "Second-pw-2") is None


@pytest.mark.parametrize("token", [
    "A" * 43, "abcdefghij_klmnopqrst-uvwxyzABCDEFGHIJKLMNOP"[:43],
    "A" * 42, "A" * 44, "x", "A" * 42 + "!",
], ids=["wrong", "wrong-2", "42-chars", "44-chars", "short", "bad-char"])
def test_a_wrong_or_malformed_token_is_404(world, token):
    _mint(KNOWN)
    tc = world["client"]()
    r = tc.get(f"/auth/reset/{token}")
    assert r.status_code == 404, (token, r.status_code)
    assert INVALID_LINK in r.text
    r = tc.post(f"/auth/reset/{token}",
                data={"new_password": "Guess-pw-1", "confirm_password": "Guess-pw-1"},
                follow_redirects=False)
    assert r.status_code == 404, (token, r.status_code)
    assert local_store.AuthStore().verify_password(KNOWN, KNOWN_PW) == "ok"


@pytest.mark.parametrize("form,error", [
    ({"new_password": "abcd-1234", "confirm_password": "abcd-9999"}, "Passwords do not match"),
    ({"new_password": "ab", "confirm_password": "ab"}, "Password must be at least 8 characters"),
    ({"new_password": "Abcde-1", "confirm_password": "Abcde-1"},
     "Password must be at least 8 characters"),
], ids=["mismatch", "too_short", "seven_chars"])
def test_a_password_rule_failure_rerenders_and_consumes_nothing(world, form, error):
    token = _mint(KNOWN)
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}", data=form, follow_redirects=False)
    assert r.status_code == 400, r.status_code
    assert error in r.text
    assert tc.get(f"/auth/reset/{token}").status_code == 200
    assert _read_auth(world["tmp"], KNOWN).get("reset_used") is False


# ===========================================================================
# the store
# ===========================================================================
def test_create_reset_token_refuses_an_unknown_address(world):
    assert _store_api("create_reset_token")(UNKNOWN) is None
    assert not (world["tmp"] / "users" / UNKNOWN).exists()


def test_find_and_consume(world):
    token = _mint(KNOWN)
    found = _store_api("find_reset_token")(token)
    assert found is not None
    email, record = found
    assert email == KNOWN
    assert record.get("reset_token_hash") == _sha(token)
    assert _store_api("find_reset_token")("B" * 43) is None
    assert _store_api("consume_reset_token")(token, "Consumed-pw1") == KNOWN
    assert _store_api("consume_reset_token")(token, "Consumed-pw2") is None
    assert local_store.AuthStore().verify_password(KNOWN, "Consumed-pw1") == "ok"
    assert _read_auth(world["tmp"], KNOWN).get("reset_used") is True


def test_the_lookup_survives_an_emptied_index(world):
    """A restart between mint and click empties the in-memory index; the
    lookup falls back to the auth records."""
    token = _mint(KNOWN)
    index = getattr(local_store, "_RESET_TOKEN_INDEX", None)
    if not isinstance(index, dict):
        pytest.fail("local_store._RESET_TOKEN_INDEX missing")
    assert index, "the mint did not fill the index"
    index.clear()
    found = _store_api("find_reset_token")(token)
    assert found is not None and found[0] == KNOWN
    index.clear()
    assert world["client"]().get(f"/auth/reset/{token}").status_code == 200


def test_clear_reset_token(world):
    token = _mint(KNOWN)
    _store_api("clear_reset_token")(KNOWN)
    assert not _read_auth(world["tmp"], KNOWN).get("reset_token_hash")
    assert _store_api("find_reset_token")(token) is None


def test_two_concurrent_consumes_admit_one(world):
    token = _mint(KNOWN)
    consume = _store_api("consume_reset_token")
    barrier = threading.Barrier(2)
    results = []

    def worker(pw):
        barrier.wait()
        results.append(consume(token, pw))

    threads = [threading.Thread(target=worker, args=(pw,)) for pw in ("Race-pw-1", "Race-pw-2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert sorted(results, key=str) == sorted([KNOWN, None], key=str), results


@pytest.mark.parametrize("how", ["set_password", "forced_change", "profile_change"])
def test_setting_a_password_anywhere_drops_an_outstanding_token(world, how):
    if how == "set_password":
        token = _mint(KNOWN)
        local_store.AuthStore().set_password(KNOWN, "Direct-pw-1")
    elif how == "forced_change":
        local_store.AuthStore().set_password(KNOWN, KNOWN_PW, force_change=True)
        tc = world["client"]()
        assert _login(tc, KNOWN, KNOWN_PW).headers["location"] == "/auth/change_password"
        token = _mint(KNOWN)
        r = tc.post("/auth/change_password",
                    data={"new_password": "Forced-pw-1", "confirm_password": "Forced-pw-1"},
                    follow_redirects=False)
        assert r.status_code == 302
    else:
        tc = world["client"]()
        assert _login(tc, KNOWN, KNOWN_PW).status_code == 302
        token = _mint(KNOWN)
        r = tc.post("/auth/password", json={"current_password": KNOWN_PW,
                                            "new_password": "Profile-pw-1"})
        assert r.status_code == 200, r.text
    rec = _read_auth(world["tmp"], KNOWN)
    assert not any(k.startswith("reset_") for k in rec), rec
    assert world["client"]().get(f"/auth/reset/{token}").status_code == 404


def test_auth_records_are_written_atomically(world, monkeypatch):
    """`_write_auth` goes through tmp + os.replace, so a concurrent reader
    (the token scan) can never see a torn record; nothing is left behind."""
    replaced = []
    real_replace = local_store.os.replace

    def spy(src, dst):
        replaced.append(str(dst))
        return real_replace(src, dst)

    monkeypatch.setattr(local_store.os, "replace", spy)
    local_store.AuthStore().set_password(KNOWN, "Atomic-pw-1")
    assert any(p.endswith("auth.json") for p in replaced), replaced
    _mint(KNOWN)
    leftovers = [p.name for p in (world["tmp"] / "users" / KNOWN).iterdir()
                 if ".tmp" in p.name]
    assert leftovers == [], leftovers


def test_a_legacy_temp_password_record_still_signs_in_and_must_change(world):
    """An account mid-reset at upgrade time (the previous release's shape:
    temp_password_hash + must_change_password) keeps working until the user
    sets a new password."""
    from password_utils import generate_password_hash
    p = _auth_path(world["tmp"], KNOWN)
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["temp_password_hash"] = generate_password_hash("Old-temp-pw")
    rec["must_change_password"] = True
    p.write_text(json.dumps(rec), encoding="utf-8")
    r = _login(world["client"](), KNOWN, "Old-temp-pw")
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/change_password"


# ===========================================================================
# the landing page (real app)
# ===========================================================================
def test_the_landing_page_shows_the_reset_done_line(tmp_path, monkeypatch):
    import app as app_mod
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    tc = TestClient(app_mod.app, base_url="https://testserver")
    r = tc.get("/?reset=done", follow_redirects=False)
    assert r.status_code == 200
    assert RESET_DONE in r.text
    plain = tc.get("/", follow_redirects=False)
    assert RESET_DONE not in plain.text


def test_the_landing_footer_no_longer_promises_self_registration(tmp_path, monkeypatch):
    import app as app_mod
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    if "ALLOW_SELF_REGISTRATION" in type(settings).model_fields:
        monkeypatch.setattr(settings, "ALLOW_SELF_REGISTRATION", False)
    r = TestClient(app_mod.app, base_url="https://testserver").get("/", follow_redirects=False)
    assert "The password you enter becomes your password" not in r.text
    assert "Accounts are created by invitation." in r.text


# ===========================================================================
# invitation-only sign-in: the same page, the same work
# ===========================================================================
@pytest.fixture
def verifications(monkeypatch):
    """Count every PBKDF2 verification, whichever module holds the name."""
    import password_utils
    import routes.auth as auth_mod
    real = password_utils.check_password_hash
    calls = []

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(password_utils, "check_password_hash", counting)
    for mod in (auth_mod, local_store):
        if hasattr(mod, "check_password_hash"):
            monkeypatch.setattr(mod, "check_password_hash", counting)
    return calls


@pytest.mark.parametrize("email,password", [
    (UNKNOWN, "any-pw-123"), (KNOWN, "wrong-pw-123"),
    (PLACEHOLDER, "any-pw-123"), (LEGACY, "any-pw-123"),
], ids=["unknown", "wrong_password", "placeholder", "legacy"])
def test_every_sign_in_failure_costs_exactly_one_verification(world, verifications,
                                                              email, password):
    r = _login(world["client"](), email, password)
    assert r.status_code == 401, r.status_code
    assert NEUTRAL_FAILURE in r.text
    assert len(verifications) == 1, len(verifications)


def test_an_unknown_address_cannot_sign_in_and_leaves_no_directory(world):
    r = _login(world["client"](), UNKNOWN, "any-pw-123")
    assert r.status_code == 401
    assert not (world["tmp"] / "users" / UNKNOWN).exists()
    wrong = _login(world["client"](), KNOWN, "wrong-pw-123")
    assert _normalise(r.text, UNKNOWN) == _normalise(wrong.text, KNOWN)


def test_self_registration_only_behind_the_setting(world, monkeypatch):
    _set(monkeypatch, "ALLOW_SELF_REGISTRATION", True)
    r = _login(world["client"](), UNKNOWN, "chosen-pw-1")
    assert r.status_code == 302 and r.headers["location"] == "/lab"
    # A placeholder or legacy account still gets the neutral refusal.
    for email in (PLACEHOLDER, LEGACY):
        refused = _login(world["client"](), email, "chosen-pw-1")
        assert refused.status_code == 401, email
        assert not local_store.AuthStore().get_auth(email).get("password_hash")


# ===========================================================================
# the address pattern
# ===========================================================================
def _email_re():
    import routes.auth as auth_mod
    return auth_mod._EMAIL_RE


@pytest.mark.parametrize("addr", [
    "a@b.co", "first.last@corp.example", "user+tag@mail.example.org",
    "oneil@x.com", "UPPER@CORP.EXAMPLE", "a_b-c%d@sub-domain.example.com",
    ("a" * 64) + "@" + ("b" * 180) + ".example",
])
def test_the_email_pattern_accepts_ordinary_addresses(addr):
    assert len(addr) <= 254
    assert _email_re().fullmatch(addr), addr


@pytest.mark.parametrize("addr", [
    "a<b>@x.com", "a>b@x.com", '"quoted"@x.com', "a/b@x.com", "a\\b@x.com",
    "a b@x.com", " a@x.com", "a@x .com", "a@x", "a@@x.com", "@x.com", "a@x.c",
    "a@x.com<script>", "a;b@x.com", "a@x.com\n",
    ("a" * 64) + "@" + ("b" * 186) + ".com",
], ids=["lt-gt", "gt", "quote", "slash", "backslash", "space", "lead-space",
        "domain-space", "no-tld", "double-at", "no-local", "short-tld",
        "markup-suffix", "semicolon", "trailing-newline", "255-chars"])
def test_the_email_pattern_rejects_markup_paths_and_overlong_addresses(addr):
    """Checked the way every caller must use the pattern: `fullmatch` (a
    `$` anchor under `match` also accepts a trailing newline)."""
    assert not _email_re().fullmatch(addr), addr


def test_no_caller_matches_the_email_pattern_with_a_dollar_anchor():
    r"""Either the pattern ends in `\Z`, or no module calls `_EMAIL_RE.match`
    (callers use `.fullmatch`). The unstripped trailing newline is refused
    either way."""
    import pathlib
    pat = _email_re()
    assert not pat.fullmatch("a@x.com\n")
    if pat.pattern.endswith(r"\Z"):
        assert not pat.match("a@x.com\n")
        return
    root = pathlib.Path(__file__).resolve().parent.parent
    offenders = []
    for path in sorted((root / "routes").glob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "_EMAIL_RE.match(" in text:
            offenders.append(path.name)
    assert offenders == [], (r"use _EMAIL_RE.fullmatch (or end the pattern in \Z)",
                             offenders)


@pytest.mark.parametrize("email", ["a<b>@x.com", "a/b@x.com"])
def test_sign_in_and_reset_refuse_a_markup_address(world, email):
    assert _login(world["client"](), email, "any-pw-123").status_code == 400
    assert _request_reset(world, email).status_code == 400


# ===========================================================================
# the share routes use the same pattern
# ===========================================================================
BAD_RECIPIENT = "a<b>@x.com"
GOOD_RECIPIENT = "ok.friend@corp.example"
CHAT = "c_resetflow00001"


@pytest.fixture
def share_world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    auth = local_store.AuthStore()
    auth.ensure_user(OWNER)
    auth.set_password(OWNER, "owner-pw-1")
    store = local_store.ChatDataStore(CHAT)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["title"] = "Sales"
    meta["sharing"] = {"shared_with": []}
    store.write_meta(meta)
    conv = store.new_conversation("o")
    store.append_history(conv, {"role": "human", "content": "q"})
    auth.record_conversation(OWNER, CHAT, conv, "o")

    import routes.auth as auth_mod
    import routes.chat as chat_mod
    import routes.dashboards as dash_mod

    def fake_mail(**kw):
        return {"smtp_configured": True, "sent": kw.get("to") or [], "failed": []}

    monkeypatch.setattr(brain_client, "send_share_email", fake_mail)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)
    app.include_router(chat_mod.router)
    app.include_router(dash_mod.router)

    @app.post("/_login/{email}")
    async def _login_route(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app, raise_server_exceptions=False)
    tc.post(f"/_login/{OWNER}")
    return {"client": tc, "conv": conv, "tmp": tmp_path}


def _shared_with(route, sw, dash_id=None):
    if route == "dashboard":
        doc = local_store.DashboardStore().get_dashboard(OWNER, dash_id) or {}
        return (doc.get("sharing") or {}).get("shared_with") or []
    return (local_store.ChatDataStore(CHAT).read_meta().get("sharing") or {}).get("shared_with") or []


@pytest.mark.parametrize("route", ["chat", "conversation", "dashboard"])
def test_a_share_route_refuses_a_markup_recipient(share_world, route):
    tc = share_world["client"]
    body = {"emails": [BAD_RECIPIENT, GOOD_RECIPIENT]}
    dash_id = None
    if route == "chat":
        r = tc.post(f"/api/chat/{CHAT}/share", json=body)
    elif route == "conversation":
        r = tc.post(f"/auth/conversations/{share_world['conv']}/share", json=body)
    else:
        dash_id = local_store.DashboardStore().create_dashboard(OWNER, "Board")["dash_id"]
        r = tc.post(f"/api/dashboards/{dash_id}/share", json=body)
    assert r.status_code == 200, r.text
    shared = _shared_with(route, share_world, dash_id)
    assert GOOD_RECIPIENT in shared, shared
    assert BAD_RECIPIENT not in shared, shared
    assert BAD_RECIPIENT not in json.dumps(r.json()), r.json()
    assert not local_store.AuthStore().user_exists(BAD_RECIPIENT)
    users = share_world["tmp"] / "users"
    assert not any("<" in p.name or ">" in p.name for p in users.iterdir())
