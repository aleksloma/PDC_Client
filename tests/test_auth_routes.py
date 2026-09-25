"""Authorization rules owned by the auth side of the app.

* A share to an address that has never signed in creates a PLACEHOLDER
  account (`users/<email>/profile.json` with `invited_by` / `invited_at`, no
  password) through all three share routes — chat, conversation, dashboard —
  via `AuthStore.ensure_invited_user`, which never overwrites an existing
  profile. Sign-in then refuses that address (nothing stored, no session):
  the mailbox owner proves ownership through the mailed reset link instead of
  whoever types the address first. An existing account is unaffected.
* Task 9 (D9-4): every sign-in failure -- placeholder, legacy password-less
  account, unknown address, wrong password -- answers 401 with ONE neutral
  line, so the page never tells an anonymous caller which kind of address it
  typed.
* Only the chat's OWNER may share a conversation of it: the route also grants
  chat-level access, which a recipient must not be able to hand out.
* `local_store._safe_email` never yields `.`, `..` or an empty path segment.

Offline: DATA_ROOT is tmp_path, the mail relays are stubbed, the activity
worker is stubbed by tests/conftest.py.
"""
import json
import re

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from settings import settings

OWNER = "owner@acme.com"
FRIEND = "friend@acme.com"
EXISTING = "existing@acme.com"
EXISTING_PW = "Existing-passw0rd"
NEWBIE = "new.person@corp.example"
CHAT = "c_authroutes0001"

NEUTRAL_FAILURE = ("Sign-in failed. Check your email and password, or use "
                   "“Reset password” if you have not set one yet.")
# The per-request CSP nonce appears as `nonce="..."` attributes and inside the
# policy header; strip every form so two renders of the same page compare equal.
_NONCE_RE = re.compile(r"""nonce(?:-[A-Za-z0-9_\-]+|\s*=\s*["'][^"']*["'])"""
                       r"""|__CSP_NONCE__\s*=\s*["'][^"']*["']""")


def _strip_nonce(text: str) -> str:
    return _NONCE_RE.sub("nonce", text)


@pytest.fixture
def app_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    auth = local_store.AuthStore()
    for email in (OWNER, FRIEND):
        auth.ensure_user(email)
        auth.set_password(email, "pw-" + email)
    auth.ensure_user(EXISTING)
    auth.set_password(EXISTING, EXISTING_PW)

    store = local_store.ChatDataStore(CHAT)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["title"] = "Sales"
    meta["sharing"] = {"shared_with": [FRIEND]}
    store.write_meta(meta)
    owner_conv = store.new_conversation("o")
    store.append_history(owner_conv, {"role": "human", "content": "q"})
    auth.record_conversation(OWNER, CHAT, owner_conv, "o")
    friend_conv = store.new_conversation("f")
    store.append_history(friend_conv, {"role": "human", "content": "q"})
    auth.record_conversation(FRIEND, CHAT, friend_conv, "f")

    import routes.auth as auth_mod
    import routes.chat as chat_mod
    import routes.dashboards as dash_mod

    def fake_mail(**kw):
        return {"smtp_configured": True, "sent": kw.get("to") or [], "failed": []}

    for mod in (auth_mod, chat_mod, dash_mod):
        monkeypatch.setattr(mod.brain_client, "send_share_email", fake_mail)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)
    app.include_router(chat_mod.router)
    app.include_router(dash_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    return {"client": tc, "app": app, "owner_conv": owner_conv,
            "friend_conv": friend_conv, "tmp": tmp_path}


def _profile_path(tmp, email):
    return tmp / "users" / email / "profile.json"


def _share_chat(tc, emails):
    return tc.post(f"/api/chat/{CHAT}/share", json={"emails": emails})


def _share_conv(tc, conv_id, emails):
    return tc.post(f"/auth/conversations/{conv_id}/share", json={"emails": emails})


def _share_dashboard(tc, emails):
    dash_id = local_store.DashboardStore().create_dashboard(OWNER, "Board")["dash_id"]
    return tc.post(f"/api/dashboards/{dash_id}/share", json={"emails": emails})


def _share_via(route, world, emails):
    tc = world["client"]
    if route == "chat":
        return _share_chat(tc, emails)
    if route == "conversation":
        return _share_conv(tc, world["owner_conv"], emails)
    return _share_dashboard(tc, emails)


ROUTES = ["chat", "conversation", "dashboard"]


# ===========================================================================
# placeholder accounts
# ===========================================================================
def test_ensure_invited_user_creates_a_passwordless_profile(app_client):
    tmp = app_client["tmp"]
    local_store.AuthStore().ensure_invited_user(NEWBIE, OWNER)
    profile = json.loads(_profile_path(tmp, NEWBIE).read_text(encoding="utf-8"))
    assert profile["email"] == NEWBIE
    assert profile["invited_by"] == OWNER
    assert profile.get("invited_at")
    assert not local_store.AuthStore().get_auth(NEWBIE).get("password_hash")


def test_ensure_invited_user_never_overwrites_an_existing_profile(app_client):
    tmp = app_client["tmp"]
    before = _profile_path(tmp, EXISTING).read_text(encoding="utf-8")
    local_store.AuthStore().ensure_invited_user(EXISTING, OWNER)
    assert _profile_path(tmp, EXISTING).read_text(encoding="utf-8") == before


@pytest.mark.parametrize("route", ROUTES)
def test_share_to_an_unknown_address_creates_a_placeholder(app_client, route):
    r = _share_via(route, app_client, [NEWBIE])
    assert r.status_code == 200, r.text
    path = _profile_path(app_client["tmp"], NEWBIE)
    assert path.exists(), f"{route} share left no profile for the recipient"
    profile = json.loads(path.read_text(encoding="utf-8"))
    assert profile.get("invited_by") == OWNER
    assert profile.get("invited_at")


@pytest.mark.parametrize("route", ROUTES)
def test_placeholder_address_cannot_be_claimed_at_first_sign_in(app_client, route):
    assert _share_via(route, app_client, [NEWBIE]).status_code == 200
    stranger = TestClient(app_client["app"])
    r = stranger.post("/auth/login", data={"email": NEWBIE, "password": "attacker-pw"},
                      follow_redirects=False)
    assert r.status_code == 401, (r.status_code, r.headers.get("location"))
    assert NEUTRAL_FAILURE in r.text
    assert "shared with this address" not in r.text
    assert not local_store.AuthStore().get_auth(NEWBIE).get("password_hash")
    assert stranger.get("/auth/me").status_code == 401


@pytest.mark.parametrize("route", ROUTES)
def test_share_leaves_an_existing_account_untouched(app_client, route):
    tmp = app_client["tmp"]
    before = _profile_path(tmp, EXISTING).read_text(encoding="utf-8")
    assert _share_via(route, app_client, [EXISTING]).status_code == 200
    assert _profile_path(tmp, EXISTING).read_text(encoding="utf-8") == before
    other = TestClient(app_client["app"])
    r = other.post("/auth/login", data={"email": EXISTING, "password": EXISTING_PW},
                   follow_redirects=False)
    assert r.status_code == 302, r.text[:200]


# ===========================================================================
# conversation share is owner-only
# ===========================================================================
def test_recipient_cannot_share_a_conversation_of_the_owners_chat(app_client):
    tc = app_client["client"]
    tc.post(f"/_login/{FRIEND}")
    r = _share_conv(tc, app_client["friend_conv"], ["eve@acme.com"])
    assert r.status_code == 403, r.text
    assert r.json() == {"error": "Access denied"}
    meta = local_store.ChatDataStore(CHAT).read_meta()
    assert meta["sharing"]["shared_with"] == [FRIEND]
    assert local_store.AuthStore().list_conversations("eve@acme.com") == []


def test_owner_shares_a_conversation(app_client):
    tc = app_client["client"]
    r = _share_conv(tc, app_client["owner_conv"], ["eve@acme.com"])
    assert r.status_code == 200, r.text
    meta = local_store.ChatDataStore(CHAT).read_meta()
    assert "eve@acme.com" in meta["sharing"]["shared_with"]
    assert len(local_store.AuthStore().list_conversations("eve@acme.com")) == 1


# ===========================================================================
# _safe_email never yields a dot segment
# ===========================================================================
@pytest.mark.parametrize("raw", ["..", ".", "", "  ..  ", " . "])
def test_safe_email_never_returns_a_dot_or_empty_segment(raw):
    out = local_store._safe_email(raw)
    assert out not in ("", ".", ".."), repr(out)
    assert out.startswith("_"), repr(out)


def test_safe_email_leaves_a_normal_address_unchanged():
    assert local_store._safe_email("Alice.Smith@Acme.com") == "alice.smith@acme.com"
    assert local_store._safe_email("a/b@x.com") == "a_b@x.com"


def test_a_legacy_email_only_profile_gets_the_neutral_refusal(app_client):
    """A profile written by the email-only build ({email, created_at}, no
    password, no invitation fields) is refused with the ONE neutral line
    (401) -- no "existed before passwords" text that would tell a caller the
    address is a legacy account -- and the typed password is not adopted."""
    legacy = "legacy-user@example.com"
    path = _profile_path(app_client["tmp"], legacy)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"email": legacy, "created_at": "2025-01-01T00:00:00Z"}),
                    encoding="utf-8")
    stranger = TestClient(app_client["app"])
    r = stranger.post("/auth/login", data={"email": legacy, "password": "typed-pw"},
                      follow_redirects=False)
    assert r.status_code == 401
    assert NEUTRAL_FAILURE in r.text
    assert "existed before passwords" not in r.text
    assert "shared with this address" not in r.text
    assert not local_store.AuthStore().get_auth(legacy).get("password_hash")
    assert stranger.get("/auth/me").status_code == 401


def test_every_sign_in_failure_renders_the_same_page(app_client):
    """Invited placeholder, legacy account, unknown address and wrong password
    all get the same status and the byte-identical body once the per-request
    nonce is stripped (the email field echoes the typed address, so every case
    types the SAME address shape through its own account)."""
    tmp = app_client["tmp"]
    invited = "invited-case@example.com"
    legacy = "legacy-case@example.com"
    unknown = "unknown-case@example.com"
    local_store.AuthStore().ensure_invited_user(invited, OWNER)
    lp = _profile_path(tmp, legacy)
    lp.parent.mkdir(parents=True, exist_ok=True)
    lp.write_text(json.dumps({"email": legacy, "created_at": "2025-01-01T00:00:00Z"}),
                  encoding="utf-8")

    def attempt(email, password):
        tc = TestClient(app_client["app"])
        r = tc.post("/auth/login", data={"email": email, "password": password},
                    follow_redirects=False)
        # The typed address is echoed into the form; neutralise it so only
        # the refusal itself is compared.
        return r.status_code, _strip_nonce(r.text.replace(email, "EMAIL"))

    results = {
        "invited": attempt(invited, "attacker-pw"),
        "legacy": attempt(legacy, "attacker-pw"),
        "unknown": attempt(unknown, "attacker-pw"),
        "wrong_password": attempt(EXISTING, "not-the-password"),
    }
    for name, (status, body) in results.items():
        assert status == 401, (name, status)
        assert NEUTRAL_FAILURE in body, name
        assert "shared with this address" not in body, name
        assert "existed before passwords" not in body, name
    bodies = {name: body for name, (_, body) in results.items()}
    ref = bodies["wrong_password"]
    for name, body in bodies.items():
        assert body == ref, f"{name} page differs from the wrong-password page"
    assert not (tmp / "users" / unknown).exists()
