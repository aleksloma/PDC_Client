"""An account that signs in with Microsoft cannot obtain a local password.

Microsoft sign-in brings the tenant's MFA and conditional access; a local
password on the same account would be a way around both. The contract
pinned here:

* `AuthStore().is_sso_only(email)` is True exactly when auth.json carries
  `sso_provider` and neither `password_hash` nor `temp_password_hash`. A
  password-less share placeholder or legacy account that later signed in
  with Microsoft counts as SSO-only; a local account (it has a hash) that
  also uses Microsoft does not.
* One text, `routes.auth.SSO_NO_LOCAL_PASSWORD_TEXT` ==
  "This account signs in with Microsoft and has no local password.",
  refused at five places:
  - `POST /auth/reset_password`: the SAME neutral 200 page every address
    gets (the caller must not learn the account type), nothing minted,
    nothing mailed, `SSO_ACCOUNT_RESET_REFUSED` logged;
  - `POST /auth/reset/{token}` with a link minted earlier: 403, the text,
    no password written;
  - `POST /auth/password`: 403 `{"error": text, "code": "SSO_ACCOUNT"}`,
    no password written;
  - `POST /auth/change_password` (a forced-change session): 403, the page
    with the text;
  - `POST /api/admin/users/invite`: 409 `{"error": text, "code":
    "SSO_ACCOUNT"}`, no token, no mail.

Offline: DATA_ROOT is tmp_path, the brain relay is stubbed, the reset
hand-off runs inline. Router-only app with a test sign-in route (the
tests/test_admin_invite.py idiom).
"""
import json
import re

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
import roles_store
from settings import settings
from tests.conftest import csrf_form

TEXT = "This account signs in with Microsoft and has no local password."
NEUTRAL_RESET = "If an account exists for this address, a reset link has been sent."
ADMIN = "ladmin"
SSO = "entra.user@corp.example"
MIXED = "mixed.user@corp.example"
MIXED_PW = "Mixed-passw0rd"
PLACEHOLDER = "was.shared@corp.example"
OWNER = "owner@corp.example"
UNKNOWN = "nobody.here@corp.example"
BASE = "https://pdc.corp.example"
NEW_PW = "Brand-new-passw0rd"

_NONCE_RE = re.compile(r"""nonce(?:-[A-Za-z0-9_\-]+|\s*=\s*["'][^"']*["'])"""
                       r"""|__CSP_NONCE__\s*=\s*["'][^"']*["']""")


def _normalise(text: str, email: str) -> str:
    return _NONCE_RE.sub("nonce", text.replace(email, "EMAIL"))


def _read_auth(tmp, email) -> dict:
    p = tmp / "users" / email / "auth.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _is_sso_only(email) -> bool:
    fn = getattr(local_store.AuthStore, "is_sso_only", None)
    if fn is None:
        pytest.fail("AuthStore.is_sso_only missing")
    return local_store.AuthStore().is_sso_only(email)


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    for name, value in (("PUBLIC_BASE_URL", BASE), ("ALLOW_SELF_REGISTRATION", False),
                        ("PASSWORD_MIN_LENGTH", 8)):
        if name in type(settings).model_fields:
            monkeypatch.setattr(settings, name, value)
    index = getattr(local_store, "_RESET_TOKEN_INDEX", None)
    if isinstance(index, dict):
        index.clear()

    store = local_store.AuthStore()
    store.ensure_user(ADMIN)
    store.set_role(ADMIN, "admin")
    store.ensure_user(SSO)
    store.mark_sso_login(SSO, "microsoft")
    store.ensure_user(MIXED)
    store.set_password(MIXED, MIXED_PW)
    store.mark_sso_login(MIXED, "microsoft")
    store.ensure_user(OWNER)
    store.set_password(OWNER, "Owner-passw0rd")
    roles_store.RolesStore().ensure_base_role()

    sent = []

    def fake_reset_mail(email, reset_url, **kwargs):
        sent.append((email, reset_url))
        return {"ok": True}

    monkeypatch.setattr(brain_client, "send_password_reset_email", fake_reset_mail)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)

    import routes.admin_users as users_mod
    import routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    monkeypatch.setattr(auth_mod, "_run_in_background",
                        lambda fn, *args: fn(*args), raising=False)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(users_mod.router)
    app.include_router(auth_mod.router)

    @app.get("/")
    async def _landing(request: Request):
        # The real app's landing mints the form token (tests.conftest.csrf_form).
        auth_mod.csrf_token(request)
        return {"ok": True}

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        if request.query_params.get("must_change") == "1":
            request.session["must_change_password"] = True
        else:
            request.session.pop("must_change_password", None)
        return {"ok": True}

    def client(who=None, must_change=False):
        tc = TestClient(app, raise_server_exceptions=False)
        if who:
            tc.post(f"/_login/{who}" + ("?must_change=1" if must_change else ""))
        return tc

    return {"tmp": tmp_path, "sent": sent, "client": client, "auth_mod": auth_mod}


# ===========================================================================
# the classifier and the text
# ===========================================================================
def test_the_refusal_text_is_fixed():
    import routes.auth as auth_mod
    assert getattr(auth_mod, "SSO_NO_LOCAL_PASSWORD_TEXT", None) == TEXT


def test_the_classifier(world):
    assert _is_sso_only(SSO) is True
    assert _is_sso_only(MIXED) is False, "a local account that also uses SSO is not SSO-only"
    assert _is_sso_only(OWNER) is False
    assert _is_sso_only(UNKNOWN) is False


def test_a_legacy_temp_password_is_a_local_password(world):
    from password_utils import generate_password_hash
    p = world["tmp"] / "users" / SSO / "auth.json"
    rec = json.loads(p.read_text(encoding="utf-8"))
    rec["temp_password_hash"] = generate_password_hash("Old-temp-pw")
    p.write_text(json.dumps(rec), encoding="utf-8")
    assert _is_sso_only(SSO) is False


def test_a_placeholder_that_signed_in_with_microsoft_is_sso_only(world):
    store = local_store.AuthStore()
    assert store.ensure_invited_user(PLACEHOLDER, OWNER) is True
    assert _is_sso_only(PLACEHOLDER) is False, "no Microsoft sign-in yet"
    store.mark_sso_login(PLACEHOLDER, "microsoft")
    assert _is_sso_only(PLACEHOLDER) is True


# ===========================================================================
# the anonymous reset request: neutral page, nothing minted
# ===========================================================================
def _record_logs(world, monkeypatch):
    lines = []

    def rec(sid, level, message, **ctx):
        lines.append(f"{sid} {level} {message} {ctx}")

    monkeypatch.setattr(world["auth_mod"], "log_with_sid", rec)
    return lines


def _request_reset(world, email, tc=None):
    tc = tc or world["client"]()
    return tc.post("/auth/reset_password", data=csrf_form(tc, {"email": email}),
                   follow_redirects=False)


def test_a_reset_request_for_an_sso_account_is_neutral_and_mints_nothing(world, monkeypatch):
    lines = _record_logs(world, monkeypatch)
    tc = world["client"]()                 # one browser: one session, one form token
    sso = _request_reset(world, SSO, tc)
    unknown = _request_reset(world, UNKNOWN, tc)
    assert (sso.status_code, unknown.status_code) == (200, 200)
    assert NEUTRAL_RESET in sso.text
    assert TEXT not in sso.text, "the anonymous page must not reveal the account type"
    assert _normalise(sso.text, SSO) == _normalise(unknown.text, UNKNOWN)
    assert not _read_auth(world["tmp"], SSO).get("reset_token_hash")
    assert world["sent"] == [], world["sent"]
    assert any("SSO_ACCOUNT_RESET_REFUSED" in ln for ln in lines), lines


def test_a_reset_request_for_a_placeholder_that_used_microsoft_mints_nothing(world):
    store = local_store.AuthStore()
    store.ensure_invited_user(PLACEHOLDER, OWNER)
    store.mark_sso_login(PLACEHOLDER, "microsoft")
    r = _request_reset(world, PLACEHOLDER)
    assert r.status_code == 200 and NEUTRAL_RESET in r.text
    assert not _read_auth(world["tmp"], PLACEHOLDER).get("reset_token_hash")
    assert world["sent"] == [], world["sent"]


def test_a_reset_request_for_a_local_account_that_also_uses_microsoft_still_mints(world):
    r = _request_reset(world, MIXED)
    assert r.status_code == 200
    assert [to for to, _ in world["sent"]] == [MIXED], world["sent"]
    assert _read_auth(world["tmp"], MIXED).get("reset_token_hash")


# ===========================================================================
# a link minted earlier
# ===========================================================================
def test_an_earlier_link_cannot_set_a_password_on_an_sso_account(world):
    token = local_store.AuthStore().create_reset_token(SSO)
    assert token
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}",
                data=csrf_form(tc, {"new_password": NEW_PW, "confirm_password": NEW_PW}),
                follow_redirects=False)
    assert r.status_code == 403, (r.status_code, r.headers.get("location"), r.text[:300])
    assert TEXT in r.text, r.text[:600]
    rec = _read_auth(world["tmp"], SSO)
    assert not rec.get("password_hash"), rec
    assert local_store.AuthStore().verify_password(SSO, NEW_PW) is None


def test_a_link_for_a_local_account_that_also_uses_microsoft_works(world):
    token = local_store.AuthStore().create_reset_token(MIXED)
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}",
                data=csrf_form(tc, {"new_password": NEW_PW, "confirm_password": NEW_PW}),
                follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert local_store.AuthStore().verify_password(MIXED, NEW_PW) == "ok"


# ===========================================================================
# the profile change and the forced change
# ===========================================================================
def test_the_profile_change_refuses_an_sso_account(world):
    tc = world["client"](SSO)
    r = tc.post("/auth/password", json={"current_password": "", "new_password": NEW_PW})
    assert r.status_code == 403, (r.status_code, r.text[:300])
    assert r.json() == {"error": TEXT, "code": "SSO_ACCOUNT"}, r.json()
    rec = _read_auth(world["tmp"], SSO)
    assert not rec.get("password_hash") and not rec.get("temp_password_hash"), rec


def test_the_profile_change_works_for_a_local_account_that_also_uses_microsoft(world):
    tc = world["client"](MIXED)
    r = tc.post("/auth/password", json={"current_password": MIXED_PW, "new_password": NEW_PW})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert local_store.AuthStore().verify_password(MIXED, NEW_PW) == "ok"


def test_the_forced_change_refuses_an_sso_account(world):
    tc = world["client"](SSO, must_change=True)
    r = tc.post("/auth/change_password",
                data=csrf_form(tc, {"new_password": NEW_PW, "confirm_password": NEW_PW}),
                follow_redirects=False)
    assert r.status_code == 403, (r.status_code, r.headers.get("location"), r.text[:300])
    assert TEXT in r.text, r.text[:600]
    assert not _read_auth(world["tmp"], SSO).get("password_hash")


# ===========================================================================
# the admin invite
# ===========================================================================
def test_the_invite_refuses_an_sso_account(world):
    r = world["client"](ADMIN).post("/api/admin/users/invite", json={"email": SSO})
    assert r.status_code == 409, (r.status_code, r.text[:300])
    assert r.json() == {"error": TEXT, "code": "SSO_ACCOUNT"}, r.json()
    assert not _read_auth(world["tmp"], SSO).get("reset_token_hash")
    assert world["sent"] == [], world["sent"]


def test_the_invite_refuses_a_placeholder_that_used_microsoft(world):
    store = local_store.AuthStore()
    store.ensure_invited_user(PLACEHOLDER, OWNER)
    store.mark_sso_login(PLACEHOLDER, "microsoft")
    r = world["client"](ADMIN).post("/api/admin/users/invite", json={"email": PLACEHOLDER})
    assert r.status_code == 409, (r.status_code, r.text[:300])
    assert r.json().get("code") == "SSO_ACCOUNT", r.json()
    assert world["sent"] == []


def test_the_invite_of_a_local_account_that_also_uses_microsoft_is_unchanged(world):
    """It has a password: the existing USER_EXISTS answer, not the SSO one."""
    r = world["client"](ADMIN).post("/api/admin/users/invite", json={"email": MIXED})
    assert r.status_code == 409, (r.status_code, r.text[:300])
    assert r.json().get("code") == "USER_EXISTS", r.json()


# ===========================================================================
# Task 14 item 6 — while SSO is ENABLED, an account whose record carries
# `sso_provider` cannot use a local password (it would bypass Entra MFA);
# the bootstrap local admin is exempt; disabling SSO restores the password
# ===========================================================================
LADMIN_PW = "Ladmin-passw0rd"
LOCAL = "local.only@corp.example"
LOCAL_PW = "Local-passw0rd"
SSO_TENANT = "11111111-2222-3333-4444-555555555555"


def _sso_on():
    import sso_store
    sso_store.save({"tenant_id": SSO_TENANT, "client_id": "app-client-id",
                    "client_secret": "s3cret-sso", "public_base_url": "",
                    "auto_redirect": False}, ADMIN)
    sso_store.record_test_ok(ADMIN)
    sso_store.set_enabled(True, ADMIN)
    assert sso_store.is_enabled() is True


def _sso_off():
    import sso_store
    sso_store.set_enabled(False, ADMIN)
    assert sso_store.is_enabled() is False


@pytest.fixture
def quiet_login(world, monkeypatch):
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    return world


def _login(world, email, password, tc=None):
    tc = tc or world["client"]()
    return tc.post("/auth/login",
                   data=csrf_form(tc, {"email": email, "password": password}),
                   follow_redirects=False)


def _record_auth_logs(world, monkeypatch):
    """Like `_record_logs`, but tolerant of a `sid=` keyword (the successful
    sign-in line passes one)."""
    lines = []

    def rec(*args, **ctx):
        lines.append(" ".join(str(a) for a in args) + f" {ctx}")

    monkeypatch.setattr(world["auth_mod"], "log_with_sid", rec)
    return lines


def _count_hash_checks(monkeypatch):
    import password_utils
    calls = []
    real = password_utils.check_password_hash

    def counting(*a, **kw):
        calls.append(1)
        return real(*a, **kw)
    monkeypatch.setattr(password_utils, "check_password_hash", counting)
    return calls


def test_a_dual_account_cannot_sign_in_locally_while_sso_is_enabled(
        quiet_login, monkeypatch):
    """MIXED has a local password AND `sso_provider`. With SSO enabled the
    right password is refused with the ONE neutral sign-in failure page
    (401, the same page a wrong password gets), after exactly one PBKDF2
    verification; `SSO_ENFORCED_LOCAL_REFUSED` is logged."""
    world = quiet_login
    _sso_on()
    lines = _record_auth_logs(world, monkeypatch)
    checks = _count_hash_checks(monkeypatch)
    tc = world["client"]()                 # one browser: one session, one form token
    r = _login(world, MIXED, MIXED_PW, tc)
    assert r.status_code == 401, (r.status_code, r.headers.get("location"), r.text[:300])
    assert 'data-i18n="auth.signin_failed"' in r.text
    assert TEXT not in r.text, "the sign-in page must not reveal the account type"
    assert len(checks) == 1, f"{len(checks)} password verifications on the refused sign-in"
    assert any("SSO_ENFORCED_LOCAL_REFUSED" in ln for ln in lines), lines
    wrong = _login(world, OWNER, "Wrong-passw0rd", tc)
    assert wrong.status_code == 401
    assert _normalise(r.text, MIXED) == _normalise(wrong.text, OWNER)


def test_the_dual_account_signs_in_again_once_sso_is_disabled(quiet_login):
    world = quiet_login
    _sso_on()
    assert _login(world, MIXED, MIXED_PW).status_code == 401
    _sso_off()
    r = _login(world, MIXED, MIXED_PW)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == "/lab"


def test_a_password_only_account_is_unaffected_while_sso_is_enabled(quiet_login):
    world = quiet_login
    local_store.AuthStore().ensure_user(LOCAL)
    local_store.AuthStore().set_password(LOCAL, LOCAL_PW)
    _sso_on()
    r = _login(world, LOCAL, LOCAL_PW)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == "/lab"


def test_the_local_admin_signs_in_with_its_password_while_sso_is_enabled(quiet_login):
    """The bootstrap local admin is exempt — even with an `sso_provider`
    stamp on its record."""
    world = quiet_login
    store = local_store.AuthStore()
    store.set_password(ADMIN, LADMIN_PW)
    store.mark_sso_login(ADMIN, "microsoft")
    _sso_on()
    r = _login(world, ADMIN, LADMIN_PW)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == "/admin/data_sources"


def test_a_reset_request_for_a_dual_account_mints_nothing_while_sso_is_enabled(
        quiet_login, monkeypatch):
    world = quiet_login
    _sso_on()
    lines = _record_auth_logs(world, monkeypatch)
    tc = world["client"]()                 # one browser: one session, one form token
    dual = _request_reset(world, MIXED, tc)
    unknown = _request_reset(world, UNKNOWN, tc)
    assert (dual.status_code, unknown.status_code) == (200, 200)
    assert NEUTRAL_RESET in dual.text
    assert TEXT not in dual.text
    assert _normalise(dual.text, MIXED) == _normalise(unknown.text, UNKNOWN)
    assert not _read_auth(world["tmp"], MIXED).get("reset_token_hash")
    assert world["sent"] == [], world["sent"]
    assert any("RESET_REFUSED" in ln for ln in lines), lines


def test_a_reset_request_for_a_dual_account_mints_again_once_sso_is_disabled(quiet_login):
    world = quiet_login
    _sso_on()
    _request_reset(world, MIXED)
    assert world["sent"] == []
    _sso_off()
    r = _request_reset(world, MIXED)
    assert r.status_code == 200
    assert [to for to, _ in world["sent"]] == [MIXED], world["sent"]


def test_an_earlier_link_cannot_set_a_dual_accounts_password_while_sso_is_enabled(
        quiet_login):
    """A link minted while SSO was disabled, used after it was enabled: 403
    with the Task 9b wording (D14-7), and the password is unchanged."""
    world = quiet_login
    token = local_store.AuthStore().create_reset_token(MIXED)
    assert token
    _sso_on()
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}",
                data=csrf_form(tc, {"new_password": NEW_PW, "confirm_password": NEW_PW}),
                follow_redirects=False)
    assert r.status_code == 403, (r.status_code, r.headers.get("location"), r.text[:300])
    assert TEXT in r.text, r.text[:600]
    store = local_store.AuthStore()
    assert store.verify_password(MIXED, NEW_PW) is None
    assert store.verify_password(MIXED, MIXED_PW) == "ok"


def test_a_password_only_accounts_reset_is_unaffected_while_sso_is_enabled(quiet_login):
    world = quiet_login
    local_store.AuthStore().ensure_user(LOCAL)
    local_store.AuthStore().set_password(LOCAL, LOCAL_PW)
    _sso_on()
    r = _request_reset(world, LOCAL)
    assert r.status_code == 200
    assert [to for to, _ in world["sent"]] == [LOCAL], world["sent"]
    token = local_store.AuthStore().create_reset_token(LOCAL)
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}",
                data=csrf_form(tc, {"new_password": NEW_PW, "confirm_password": NEW_PW}),
                follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert local_store.AuthStore().verify_password(LOCAL, NEW_PW) == "ok"


def test_an_sso_only_account_keeps_its_refusals_while_sso_is_enabled(quiet_login):
    """Unchanged: an SSO-only account's reset link is still refused with the
    same text when SSO is on."""
    world = quiet_login
    token = local_store.AuthStore().create_reset_token(SSO)
    _sso_on()
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}",
                data=csrf_form(tc, {"new_password": NEW_PW, "confirm_password": NEW_PW}),
                follow_redirects=False)
    assert r.status_code == 403
    assert TEXT in r.text
