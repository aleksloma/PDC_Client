"""Microsoft SSO identity binding and provisioning (routes/sso.py callback).

- The account is bound to the Entra tenant id + object id (auth.json
  `sso_tid` / `sso_oid`) at its first Microsoft sign-in; afterwards the same
  identity reaches the same account whatever username Entra reports, and a
  different identity presenting a bound address is refused.
- A token without both ids is refused.
- A guest of the tenant (#EXT# in upn / preferred_username) is refused
  unless SSO_ALLOW_GUESTS.
- An identity matching no account is refused unless SSO_AUTO_PROVISION —
  and then nothing is created.
- An invited or share-created placeholder (the SSO-only one included) is
  accepted and bound; an account from before binding binds once.
"""
import pytest

import local_store
from settings import settings
from tests.test_sso_routes import (  # noqa: F401  (pytest fixture import)
    TENANT, USER, _enable, _FakeOAuthClient, _install_fake, _save, client,
)

OID_A = "aaaaaaaa-0000-0000-0000-000000000001"
OID_B = "bbbbbbbb-0000-0000-0000-000000000002"


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    monkeypatch.setattr(settings, "SSO_ALLOW_GUESTS", False)
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", False)
    local_store._SSO_INDEX.clear()


def _sign_in(client, monkeypatch, **claims):
    _save()
    _enable()
    userinfo = {"tid": TENANT, "oid": OID_A, "preferred_username": USER}
    userinfo.update(claims)
    userinfo = {k: v for k, v in userinfo.items() if v is not None}
    _install_fake(monkeypatch, _FakeOAuthClient(token={"userinfo": userinfo}))
    return client.get("/auth/microsoft/callback", follow_redirects=False)


def _who(client):
    return client.get("/_whoami").json()["email"]


# ---------------------------------------------------------------- binding
def test_first_sign_in_binds_the_existing_account(client, monkeypatch):
    r = _sign_in(client, monkeypatch)
    assert r.status_code == 302, r.text[:200]
    assert local_store.AuthStore().sso_identity(USER) == (TENANT, OID_A)


def test_a_new_username_for_the_same_identity_reaches_the_same_account(client, monkeypatch):
    assert _sign_in(client, monkeypatch).status_code == 302
    client.cookies.clear()
    r = _sign_in(client, monkeypatch, preferred_username="renamed@x.com")
    assert r.status_code == 302, r.text[:200]
    assert _who(client) == USER
    assert not local_store.AuthStore().user_exists("renamed@x.com")


def test_a_bound_address_refuses_a_different_identity(client, monkeypatch):
    assert _sign_in(client, monkeypatch).status_code == 302
    client.cookies.clear()
    r = _sign_in(client, monkeypatch, oid=OID_B)
    assert r.status_code == 403
    assert _who(client) is None
    assert local_store.AuthStore().sso_identity(USER) == (TENANT, OID_A)


def test_an_account_from_before_binding_binds_once(client, monkeypatch):
    store = local_store.AuthStore()
    store.mark_sso_login(USER, "microsoft")          # an earlier SSO sign-in
    assert store.sso_identity(USER) == ("", "")
    assert _sign_in(client, monkeypatch).status_code == 302
    assert store.sso_identity(USER) == (TENANT, OID_A)


@pytest.mark.parametrize("claims", [{"oid": None}, {"tid": None}, {"oid": "not-a-guid"}])
def test_a_token_without_both_ids_is_refused(client, monkeypatch, claims):
    r = _sign_in(client, monkeypatch, **claims)
    assert r.status_code == 401
    assert _who(client) is None
    assert local_store.AuthStore().sso_identity(USER) == ("", "")


# ---------------------------------------------------------------- guests
@pytest.mark.parametrize("claim", ["upn", "preferred_username"])
def test_a_guest_is_refused_by_default(client, monkeypatch, claim):
    guest = "user_x.com#EXT#@tenant.onmicrosoft.com"
    r = _sign_in(client, monkeypatch, **{claim: guest})
    assert r.status_code == 403
    assert _who(client) is None


def test_a_guest_is_allowed_with_the_setting(client, monkeypatch):
    monkeypatch.setattr(settings, "SSO_ALLOW_GUESTS", True)
    r = _sign_in(client, monkeypatch, upn="user_x.com#EXT#@tenant.onmicrosoft.com")
    assert r.status_code == 302
    assert _who(client) == USER


# ---------------------------------------------------------------- provisioning
def test_an_unknown_identity_is_refused_and_nothing_is_created(client, monkeypatch):
    r = _sign_in(client, monkeypatch, preferred_username="stranger@x.com")
    assert r.status_code == 401
    assert not local_store.AuthStore().user_exists("stranger@x.com")
    assert _who(client) is None


def test_an_unknown_identity_is_provisioned_and_bound_with_the_setting(client, monkeypatch):
    monkeypatch.setattr(settings, "SSO_AUTO_PROVISION", True)
    r = _sign_in(client, monkeypatch, preferred_username="newbie@x.com", oid=OID_B)
    assert r.status_code == 302
    store = local_store.AuthStore()
    assert store.user_exists("newbie@x.com")
    assert store.sso_identity("newbie@x.com") == (TENANT, OID_B)


@pytest.mark.parametrize("sso_only", [False, True])
def test_an_invited_placeholder_is_accepted_and_bound(client, monkeypatch, sso_only):
    store = local_store.AuthStore()
    assert store.ensure_invited_user("invitee@x.com", USER, sso_only=sso_only)
    r = _sign_in(client, monkeypatch, preferred_username="invitee@x.com", oid=OID_B)
    assert r.status_code == 302
    assert _who(client) == "invitee@x.com"
    assert store.sso_identity("invitee@x.com") == (TENANT, OID_B)


# ---------------------------------------------------------------- the index
def test_a_lookup_scans_the_records_on_a_cache_miss(client, monkeypatch):
    assert _sign_in(client, monkeypatch).status_code == 302
    local_store._SSO_INDEX.clear()
    assert local_store.AuthStore().find_sso_account(TENANT, OID_A) == USER


def test_removing_the_account_drops_its_binding(client, monkeypatch):
    assert _sign_in(client, monkeypatch).status_code == 302
    store = local_store.AuthStore()
    assert store.remove_user(USER)
    assert not [k for k, who in local_store._SSO_INDEX.items() if who == USER]
    assert store.find_sso_account(TENANT, OID_A) is None
    client.cookies.clear()
    r = _sign_in(client, monkeypatch)
    assert r.status_code == 401                      # no account, no provisioning
    assert not store.user_exists(USER)


def test_binding_never_writes_a_missing_account():
    store = local_store.AuthStore()
    assert store.bind_sso_identity("ghost@x.com", TENANT, OID_A) == "missing"
    assert not store.user_exists("ghost@x.com")


def test_a_guest_is_never_identified_by_the_email_claim(client, monkeypatch):
    monkeypatch.setattr(settings, "SSO_ALLOW_GUESTS", True)
    r = _sign_in(client, monkeypatch, preferred_username=None, email=USER,
                 upn="someone_evil.com#EXT#@tenant.onmicrosoft.com")
    assert r.status_code == 400                      # no usable address
    assert _who(client) is None
    assert local_store.AuthStore().sso_identity(USER) == ("", "")


def test_binding_refuses_an_unreadable_auth_record():
    store = local_store.AuthStore()
    path = store._auth_path(USER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert store.bind_sso_identity(USER, TENANT, OID_A) == "failed"
    assert path.read_text(encoding="utf-8") == "{not json"
