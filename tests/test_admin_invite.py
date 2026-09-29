"""POST /api/admin/users/invite -- the admin invite (Task 9, D9-1 / D9-3).

Sign-in no longer creates accounts, so a password-only install onboards its
users through this route (besides a share placeholder and Microsoft SSO).

The contract pinned here:

* ladmin only (`_require_admin`: 401 without a session, 403 + an
  `admin.denied` audit row for anyone else -- the router-wide guard test in
  tests/test_admin_users_routes.py enumerates this route automatically).
* Body `{email}`; the address passes `routes.auth._EMAIL_RE` (else 400) and is
  lower-cased; the bootstrap account is refused (400); an account that
  already HAS a password answers 409 `{"code": "USER_EXISTS"}`.
* Otherwise a placeholder is created with `invited_by` = the admin when the
  address has no account (a legacy or placeholder account without a password
  is accepted as it is), a reset token is minted and the link is mailed
  SYNCHRONOUSLY through `brain_client.send_password_reset_email(email,
  reset_url)`; the answer is `{ok: true, email, created, mail_sent}` plus
  `mail_error` when the relay failed -- still 200, the account exists and
  "Reset password" works for the invitee.
* With PUBLIC_BASE_URL unset (or not http(s)) the account is still created,
  nothing is minted or mailed, and the answer is 200 with `mail_sent: false`
  and a fixed `mail_error` naming the setting (D9-26).
* Audited as `user.invite` (target = the address).

Offline: DATA_ROOT is tmp_path, the brain relay is stubbed, router-only app
(the tests/test_admin_users_routes.py idiom).
"""
import hashlib
import json

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import db_sources
import local_store
import roles_store
from settings import settings
from tests.conftest import csrf_form

ADMIN = "ladmin"
USER = "user@corp.example"
USER_PW = "User-passw0rd"
INVITEE = "new.person@corp.example"
BASE = "https://pdc.corp.example"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    for name, value in (("PUBLIC_BASE_URL", BASE), ("ALLOW_SELF_REGISTRATION", False)):
        if name in type(settings).model_fields:
            monkeypatch.setattr(settings, name, value)
    index = getattr(local_store, "_RESET_TOKEN_INDEX", None)
    if isinstance(index, dict):
        index.clear()
    store = local_store.AuthStore()
    store.ensure_user(ADMIN)
    store.set_role(ADMIN, "admin")
    store.ensure_user(USER)
    store.set_password(USER, USER_PW)
    roles_store.RolesStore().ensure_base_role()

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
        request.session.pop("must_change_password", None)
        return {"ok": True}

    def client(who=None):
        tc = TestClient(app, raise_server_exceptions=False)
        if who:
            tc.post(f"/_login/{who}")
        return tc

    return {"tmp": tmp_path, "sent": sent, "sent_kwargs": sent_kwargs, "fail": fail,
            "client": client}


def _invite(world, email, who=ADMIN):
    return world["client"](who).post("/api/admin/users/invite", json={"email": email})


def _audit_rows(action):
    return [r for r in db_sources.read_audit_tail(500) if r.get("action") == action]


def _profile(tmp, email):
    p = tmp / "users" / email / "profile.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def _auth(tmp, email):
    p = tmp / "users" / email / "auth.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


# ===========================================================================
# the happy path
# ===========================================================================
def test_an_admin_invites_a_new_address(world):
    r = _invite(world, INVITEE)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    body = r.json()
    assert body.get("ok") is True, body
    assert body.get("email") == INVITEE, body
    assert body.get("created") is True, body
    assert body.get("mail_sent") is True, body
    assert "mail_error" not in body, body
    profile = _profile(world["tmp"], INVITEE)
    assert profile and profile.get("invited_by") == ADMIN, profile
    assert not _auth(world["tmp"], INVITEE).get("password_hash")
    assert [to for to, _ in world["sent"]] == [INVITEE], world["sent"]
    url = world["sent"][0][1]
    assert url.startswith(BASE + "/auth/reset/"), "the link is not based on PUBLIC_BASE_URL"
    token = url.rsplit("/", 1)[1]
    assert len(token) == 43
    assert _auth(world["tmp"], INVITEE).get("reset_token_hash") == \
        hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_the_invite_mail_is_of_kind_invite(world):
    """The invitee gets invitation wording, not "a password reset was
    requested": the route passes kind="invite" to the brain relay."""
    assert _invite(world, INVITEE).status_code == 200
    assert world["sent_kwargs"], "no invitation mail"
    assert world["sent_kwargs"][-1].get("kind") == "invite", world["sent_kwargs"]


def test_a_reset_requested_by_the_invitee_is_of_kind_reset(world):
    assert _invite(world, INVITEE).status_code == 200
    tc = world["client"]()
    r = tc.post("/auth/reset_password", data=csrf_form(tc, {"email": INVITEE}),
                follow_redirects=False)
    assert r.status_code == 200
    assert len(world["sent_kwargs"]) == 2, world["sent_kwargs"]
    assert world["sent_kwargs"][-1].get("kind", "reset") == "reset", world["sent_kwargs"]


def test_the_invite_is_audited(world):
    assert _invite(world, INVITEE).status_code == 200
    rows = _audit_rows("user.invite")
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.get("actor") == ADMIN and row.get("target") == INVITEE, row
    assert row.get("ok") is True, row
    dumped = json.dumps(row)
    assert "/auth/reset/" not in dumped, "the audit row must not carry the link"


def test_the_address_is_lower_cased(world):
    r = _invite(world, "New.Person@Corp.Example")
    assert r.status_code == 200, r.text[:300]
    assert r.json().get("email") == INVITEE
    assert _profile(world["tmp"], INVITEE) is not None


def test_the_invitee_signs_in_through_the_link(world):
    assert _invite(world, INVITEE).status_code == 200
    token = world["sent"][-1][1].rsplit("/", 1)[1]
    tc = world["client"]()
    r = tc.post(f"/auth/reset/{token}",
                data=csrf_form(tc, {"new_password": "Invitee-pw1",
                                    "confirm_password": "Invitee-pw1"}),
                follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/?reset=done"
    fresh = world["client"]()
    login = fresh.post("/auth/login",
                       data=csrf_form(fresh, {"email": INVITEE, "password": "Invitee-pw1"}),
                       follow_redirects=False)
    assert login.status_code == 302 and login.headers["location"] == "/lab"


def test_an_uninvited_address_cannot_sign_in_and_leaves_nothing(world):
    tc = world["client"]()
    r = tc.post("/auth/login",
                data=csrf_form(tc, {"email": "stranger@corp.example", "password": "pw-123"}),
                follow_redirects=False)
    assert r.status_code == 401
    assert not (world["tmp"] / "users" / "stranger@corp.example").exists()


# ===========================================================================
# existing accounts
# ===========================================================================
def test_an_account_with_a_password_is_409(world):
    r = _invite(world, USER)
    assert r.status_code == 409, (r.status_code, r.text[:300])
    assert r.json().get("code") == "USER_EXISTS", r.json()
    assert world["sent"] == []
    assert not _auth(world["tmp"], USER).get("reset_token_hash")


def test_a_legacy_password_less_account_is_accepted_as_it_is(world):
    legacy = "legacy.user@corp.example"
    d = world["tmp"] / "users" / legacy
    d.mkdir(parents=True)
    original = {"email": legacy, "created_at": "2025-01-01T00:00:00Z"}
    (d / "profile.json").write_text(json.dumps(original), encoding="utf-8")
    r = _invite(world, legacy)
    assert r.status_code == 200, r.text[:300]
    assert r.json().get("created") is False, r.json()
    assert r.json().get("mail_sent") is True
    assert _profile(world["tmp"], legacy) == original, "the legacy profile was rewritten"
    assert _auth(world["tmp"], legacy).get("reset_token_hash")


def test_an_existing_placeholder_is_invited_again(world):
    local_store.AuthStore().ensure_invited_user(INVITEE, USER)
    r = _invite(world, INVITEE)
    assert r.status_code == 200, r.text[:300]
    assert r.json().get("created") is False, r.json()
    assert _profile(world["tmp"], INVITEE).get("invited_by") == USER
    assert [to for to, _ in world["sent"]] == [INVITEE]


# ===========================================================================
# refusals
# ===========================================================================
def test_the_bootstrap_account_is_refused(world, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "boot.admin@corp.example")
    store = local_store.AuthStore()
    store.ensure_user("boot.admin@corp.example")
    store.set_role("boot.admin@corp.example", "admin")
    r = _invite(world, "boot.admin@corp.example", who="boot.admin@corp.example")
    assert r.status_code == 400, (r.status_code, r.text[:300])
    assert world["sent"] == []
    assert not _auth(world["tmp"], "boot.admin@corp.example").get("reset_token_hash")


@pytest.mark.parametrize("email", ["ladmin", "", "not-an-email", "a<b>@x.com",
                                   "a/b@corp.example", "a b@corp.example"])
def test_an_invalid_address_is_400(world, email):
    r = _invite(world, email)
    assert r.status_code == 400, (email, r.status_code, r.text[:300])
    assert world["sent"] == []
    names = sorted(p.name for p in (world["tmp"] / "users").iterdir())
    assert names == sorted([ADMIN, USER]), names


def test_a_non_admin_is_403_and_audited(world):
    r = _invite(world, INVITEE, who=USER)
    assert r.status_code == 403, (r.status_code, r.text[:300])
    assert _profile(world["tmp"], INVITEE) is None
    assert world["sent"] == []
    denied = _audit_rows("admin.denied")
    assert any(row.get("target") == "/api/admin/users/invite" for row in denied), denied


def test_no_session_is_401(world):
    r = world["client"]().post("/api/admin/users/invite", json={"email": INVITEE})
    assert r.status_code == 401
    assert _profile(world["tmp"], INVITEE) is None


# ===========================================================================
# the mail relay fails
# ===========================================================================
def test_a_failed_mail_still_answers_200_and_keeps_the_account(world):
    world["fail"]["on"] = True
    r = _invite(world, INVITEE)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    body = r.json()
    assert body.get("ok") is True and body.get("created") is True, body
    assert body.get("mail_sent") is False, body
    assert isinstance(body.get("mail_error"), str) and body["mail_error"], body
    assert _profile(world["tmp"], INVITEE) is not None
    # The invitee can still ask for a link from the sign-in page.
    world["fail"]["on"] = False
    tc = world["client"]()
    again = tc.post("/auth/reset_password", data=csrf_form(tc, {"email": INVITEE}),
                    follow_redirects=False)
    assert again.status_code == 200
    assert [to for to, _ in world["sent"]] == [INVITEE]


# ===========================================================================
# PUBLIC_BASE_URL unset (D9-26)
# ===========================================================================
def test_without_a_public_base_url_the_account_exists_but_nothing_is_mailed(world, monkeypatch):
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "")
    r = _invite(world, INVITEE)
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.json() == {
        "ok": True, "email": INVITEE, "created": True, "mail_sent": False,
        "mail_error": "PUBLIC_BASE_URL is not set, so no invitation link can be mailed.",
    }, r.json()
    profile = _profile(world["tmp"], INVITEE)
    assert profile and profile.get("invited_by") == ADMIN, profile
    assert not _auth(world["tmp"], INVITEE).get("reset_token_hash")
    assert world["sent"] == [], world["sent"]
    rows = _audit_rows("user.invite")
    assert len(rows) == 1, rows
    assert (rows[0].get("detail") or {}).get("mail_sent") is False, rows[0]
