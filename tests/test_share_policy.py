"""Sharing: allowed domains, placeholders, revocation.

- A recipient's domain must be in SHARE_ALLOWED_DOMAINS or, when that is
  empty, among the domains of the existing administrator accounts. An
  out-of-domain recipient is refused (400 RECIPIENT_DOMAIN_NOT_ALLOWED) and
  NO account is created.
- A same-domain recipient without an account gets the password-less
  placeholder as before, and after activating it (a reset link) sees the chat.
  With Microsoft SSO on, the placeholder is SSO-only: the reset page refuses
  it (the user signs in with Microsoft instead).
- The chat owner can revoke one recipient (DELETE /api/chat/{id}/share/{email}).
- A dashboard unshare revokes the source-chat grants THAT share created —
  not a pre-existing grant, not one another shared dashboard still needs;
  a dashboard doc from before the record revokes nothing.
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
import routes.auth as auth_mod
import routes.chat as chat_mod
import routes.dashboards as dash_mod
from settings import settings
from tests.conftest import csrf_form

OWNER = "owner@acme.com"
CHAT = "c_sharepolicy1"
PW = "a-good-password-1"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "SHARE_ALLOWED_DOMAINS", "")
    monkeypatch.setattr(settings, "PUBLIC_BASE_URL", "https://pdc.acme.com")
    import auth_limiter
    auth_limiter.reset()
    store = local_store.AuthStore()
    store.create_account(OWNER)
    store.set_password(OWNER, PW)
    store.set_role(OWNER, "admin")            # the admin whose domain is allowed
    chat = local_store.ChatDataStore(CHAT)
    meta = chat.read_meta()
    meta["owner"] = OWNER
    meta["title"] = "Sales"
    chat.write_meta(meta)
    sent = []
    monkeypatch.setattr(chat_mod.brain_client, "send_share_email",
                        lambda **k: sent.append(k) or {"smtp_configured": True, "sent": k["to"]})
    monkeypatch.setattr(auth_mod.brain_client, "send_password_reset_email",
                        lambda *a, **k: sent.append(("reset", a)) or {"ok": True})
    monkeypatch.setattr(auth_mod, "_run_in_background", lambda fn, *a: fn(*a))
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="t" * 40)

    @app.get("/")
    def _landing(request: Request):
        auth_mod.csrf_token(request)
        return {"ok": True}

    @app.post("/_login/{email}")
    def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_" + "1" * 16
        return {"ok": True}

    app.include_router(auth_mod.router)
    app.include_router(chat_mod.router)
    app.include_router(dash_mod.router)
    client = TestClient(app)
    client.post(f"/_login/{OWNER}")
    return client, sent


def _share(client, emails):
    return client.post(f"/api/chat/{CHAT}/share", json={"emails": emails})


# ---------------------------------------------------------------- domains
def test_an_out_of_domain_recipient_is_refused_and_no_account_is_created(world):
    client, sent = world
    r = _share(client, ["mallory@gmail.com"])
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "RECIPIENT_DOMAIN_NOT_ALLOWED"
    assert "allowed sharing domains" in r.json()["error"]
    assert not local_store.AuthStore().user_exists("mallory@gmail.com")
    assert local_store.ChatDataStore(CHAT).read_meta().get("sharing", {}).get("shared_with", []) == []
    assert sent == []


def test_one_out_of_domain_address_refuses_the_whole_request(world):
    client, _ = world
    r = _share(client, ["colleague@acme.com", "mallory@gmail.com"])
    assert r.status_code == 400
    assert not local_store.AuthStore().user_exists("colleague@acme.com")


def test_the_configured_domains_replace_the_derived_ones(world, monkeypatch):
    client, _ = world
    monkeypatch.setattr(settings, "SHARE_ALLOWED_DOMAINS", "Partner.ge, @bank.ge")
    assert _share(client, ["x@acme.com"]).status_code == 400
    assert _share(client, ["y@bank.ge"]).status_code == 200


def test_no_admin_domain_and_no_setting_refuses_every_share(world):
    client, _ = world
    local_store.AuthStore().set_role(OWNER, "user")
    r = _share(client, ["colleague@acme.com"])
    assert r.status_code == 400 and "SHARE_ALLOWED_DOMAINS" in r.json()["error"]


@pytest.mark.parametrize("route", ["dashboard", "conversation"])
def test_the_other_share_routes_apply_the_same_rule(world, route):
    client, _ = world
    if route == "dashboard":
        dash = client.post("/api/dashboards", json={"name": "D"}).json()
        dash_id = dash.get("dash_id") or dash.get("dashboard", {}).get("dash_id")
        r = client.post(f"/api/dashboards/{dash_id}/share", json={"emails": ["m@gmail.com"]})
    else:
        conv = local_store.ChatDataStore(CHAT).new_conversation("t")
        local_store.AuthStore().record_conversation(OWNER, conv, CHAT, "t") \
            if hasattr(local_store.AuthStore, "record_conversation") else None
        r = client.post(f"/auth/conversations/{conv}/share",
                        json={"allowed_emails": ["m@gmail.com"]})
    assert r.status_code == 400, (route, r.status_code, r.text[:200])
    assert not local_store.AuthStore().user_exists("m@gmail.com")


# ---------------------------------------------------------------- placeholders
def test_a_same_domain_recipient_gets_a_placeholder_and_sees_the_chat_after_activation(world):
    client, _ = world
    new = "newcomer@acme.com"
    assert _share(client, [new]).status_code == 200
    store = local_store.AuthStore()
    assert store.user_exists(new) and not store.has_password(new)
    token = store.create_reset_token(new)
    guest = TestClient(client.app)
    page = guest.get(f"/auth/reset/{token}")
    assert page.status_code == 200
    done = guest.post(f"/auth/reset/{token}",
                      data=csrf_form(guest, {"new_password": PW, "confirm_password": PW}),
                      follow_redirects=False)
    assert done.status_code == 302, done.text[:200]
    login = guest.post("/auth/login", data=csrf_form(guest, {"email": new, "password": PW}),
                       follow_redirects=False)
    assert login.status_code == 302, login.text[:200]
    assert guest.get(f"/api/chat/{CHAT}/schema").status_code == 200


def test_an_existing_placeholder_is_a_valid_recipient(world):
    client, _ = world
    local_store.AuthStore().ensure_invited_user("pending@acme.com", OWNER)
    assert _share(client, ["pending@acme.com"]).status_code == 200


def test_with_sso_on_a_share_placeholder_is_refused_at_the_reset_page(world, monkeypatch):
    client, _ = world
    import sso_store
    monkeypatch.setattr(sso_store, "is_enabled", lambda: True)
    new = "ssouser@acme.com"
    assert _share(client, [new]).status_code == 200
    store = local_store.AuthStore()
    assert store.is_sso_only(new)
    token = store.create_reset_token(new)
    guest = TestClient(client.app)
    guest.get("/")
    r = guest.post(f"/auth/reset/{token}",
                   data=csrf_form(guest, {"new_password": PW, "confirm_password": PW}),
                   follow_redirects=False)
    assert r.status_code == 403
    assert not store.has_password(new)


# ---------------------------------------------------------------- revocation
def test_the_owner_revokes_a_recipient_who_then_loses_access(world):
    client, _ = world
    friend = "friend@acme.com"
    local_store.AuthStore().create_account(friend)
    _share(client, [friend])
    other = TestClient(client.app)
    other.post(f"/_login/{friend}")
    assert other.get(f"/api/chat/{CHAT}/schema").status_code == 200
    r = client.delete(f"/api/chat/{CHAT}/share/{friend}")
    assert r.status_code == 200 and r.json()["shared_with"] == []
    assert other.get(f"/api/chat/{CHAT}/schema").status_code in (403, 404)
    rows = local_store.AuthStore().list_active_chats(friend)
    assert not [row for row in rows if row.get("chat_id") == CHAT]
    again = client.delete(f"/api/chat/{CHAT}/share/{friend}")
    assert again.status_code == 200


def test_only_the_owner_revokes_and_the_address_is_validated(world):
    client, _ = world
    friend = "friend@acme.com"
    local_store.AuthStore().create_account(friend)
    _share(client, [friend])
    other = TestClient(client.app)
    other.post(f"/_login/{friend}")
    assert other.delete(f"/api/chat/{CHAT}/share/{friend}").status_code == 403
    assert client.delete(f"/api/chat/{CHAT}/share/not-an-address").status_code == 400


def test_share_get_reports_the_true_owner(world):
    client, _ = world
    friend = "friend@acme.com"
    local_store.AuthStore().create_account(friend)
    _share(client, [friend])
    other = TestClient(client.app)
    other.post(f"/_login/{friend}")
    data = other.get(f"/api/chat/{CHAT}/share").json()
    assert data["owner"] == OWNER and data["is_owner"] is False


# ---------------------------------------------------------------- dashboard grants
def _dashboard_with_tile(client, chat_id=CHAT, name="D"):
    store = dash_mod._dash_store
    dash_id = store.create_dashboard(OWNER, name)["dash_id"]
    doc = store.get_dashboard(OWNER, dash_id)
    doc["tiles"] = [{"tile_id": "t1", "kind": "chart", "chat_id": chat_id, "code": "x",
                     "snapshot": {}}]
    store._write_doc(OWNER, doc)
    return dash_id


def _recipients(chat_id=CHAT):
    return local_store.ChatDataStore(chat_id).read_meta().get("sharing", {}).get("shared_with", [])


def test_dashboard_unshare_revokes_the_chat_grant_it_created(world):
    client, _ = world
    friend = "friend@acme.com"
    local_store.AuthStore().create_account(friend)
    dash_id = _dashboard_with_tile(client)
    assert client.post(f"/api/dashboards/{dash_id}/share", json={"emails": [friend]}).status_code == 200
    assert friend in _recipients()
    assert client.post(f"/api/dashboards/{dash_id}/unshare", json={"email": friend}).status_code == 200
    assert friend not in _recipients()


def test_dashboard_unshare_keeps_a_grant_that_predated_the_share(world):
    client, _ = world
    friend = "friend@acme.com"
    local_store.AuthStore().create_account(friend)
    _share(client, [friend])                        # the chat itself was shared first
    dash_id = _dashboard_with_tile(client)
    client.post(f"/api/dashboards/{dash_id}/share", json={"emails": [friend]})
    client.post(f"/api/dashboards/{dash_id}/unshare", json={"email": friend})
    assert friend in _recipients()


def test_dashboard_unshare_keeps_a_grant_another_shared_dashboard_needs(world):
    client, _ = world
    friend = "friend@acme.com"
    local_store.AuthStore().create_account(friend)
    first = _dashboard_with_tile(client, name="A")
    second = _dashboard_with_tile(client, name="B")
    client.post(f"/api/dashboards/{first}/share", json={"emails": [friend]})
    client.post(f"/api/dashboards/{second}/share", json={"emails": [friend]})
    client.post(f"/api/dashboards/{first}/unshare", json={"email": friend})
    assert friend in _recipients()
    # The record moved to the second dashboard: its unshare ends the grant.
    client.post(f"/api/dashboards/{second}/unshare", json={"email": friend})
    assert friend not in _recipients()


def test_an_old_dashboard_doc_without_grants_revokes_nothing(world):
    client, _ = world
    friend = "friend@acme.com"
    local_store.AuthStore().create_account(friend)
    dash_id = _dashboard_with_tile(client)
    client.post(f"/api/dashboards/{dash_id}/share", json={"emails": [friend]})
    store = dash_mod._dash_store
    doc = store.get_dashboard(OWNER, dash_id)
    doc["sharing"].pop("chat_grants", None)         # the shape before the record
    store._write_doc(OWNER, doc)
    client.post(f"/api/dashboards/{dash_id}/unshare", json={"email": friend})
    assert friend in _recipients()


def test_a_direct_share_after_a_dashboard_share_survives_the_dashboard_unshare(world):
    client, _ = world
    friend = "friend@acme.com"
    local_store.AuthStore().create_account(friend)
    dash_id = _dashboard_with_tile(client)
    client.post(f"/api/dashboards/{dash_id}/share", json={"emails": [friend]})
    assert _share(client, [friend]).status_code == 200   # now shared on purpose
    client.post(f"/api/dashboards/{dash_id}/unshare", json={"email": friend})
    assert friend in _recipients()
