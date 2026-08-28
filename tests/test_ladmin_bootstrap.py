"""ladmin: idempotent bootstrap with forced change, role field on
profile.json (legacy profiles default to "user"), non-email login accepted
for ladmin ONLY, reset refused for ladmin, and the profile flag being
is_local_admin (NOT is_admin — that key feeds the B2C Publish menu whose
routes 400 on-prem)."""
import json

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from settings import settings


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    monkeypatch.setattr(settings, "LOCAL_ADMIN_PASSWORD", "boot-pw-123")


@pytest.fixture
def client(monkeypatch):
    import routes.auth as auth_mod
    monkeypatch.setattr(auth_mod.brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)
    return TestClient(app)


def test_bootstrap_creates_admin_with_forced_change(tmp_path):
    local_store.AuthStore().ensure_local_admin()
    store = local_store.AuthStore()
    assert store.get_role("ladmin") == "admin"
    auth = store.get_auth("ladmin")
    assert auth.get("password_hash")
    assert auth.get("must_change_password") is True
    # Only the HASH is stored — the plaintext never appears on disk.
    raw = (tmp_path / "users" / "ladmin" / "auth.json").read_text(encoding="utf-8")
    assert "boot-pw-123" not in raw


def test_bootstrap_idempotent_never_resets_existing_password(monkeypatch):
    store = local_store.AuthStore()
    store.ensure_local_admin()
    # Admin changes their password; the env var must NOT re-assert itself
    # on the next boot (that would be a permanent backdoor).
    store.set_password("ladmin", "chosen-by-admin")
    store.ensure_local_admin()
    assert store.verify_password("ladmin", "chosen-by-admin") == "ok"
    assert store.verify_password("ladmin", "boot-pw-123") is None
    assert store.get_auth("ladmin").get("must_change_password") is False


def test_bootstrap_skipped_without_password(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_ADMIN_PASSWORD", "")
    store = local_store.AuthStore()
    store.ensure_local_admin()
    assert store.get_role("ladmin") == "admin"     # role still assigned
    assert not store.get_auth("ladmin")            # but no credential invented


def test_legacy_profile_defaults_to_user_role(tmp_path):
    udir = tmp_path / "users" / "old@x.com"
    udir.mkdir(parents=True)
    (udir / "profile.json").write_text(
        json.dumps({"email": "old@x.com", "created_at": "2026-01-01"}),
        encoding="utf-8")
    store = local_store.AuthStore()
    assert store.get_role("old@x.com") == "user"
    assert store.is_admin("old@x.com") is False


def test_set_role_preserves_profile_keys(tmp_path):
    store = local_store.AuthStore()
    store.ensure_user("a@x.com")
    store.set_role("a@x.com", "admin")
    prof = store.get_profile("a@x.com")
    assert prof["role"] == "admin" and prof["email"] == "a@x.com" and prof["created_at"]


def test_login_accepts_ladmin_and_forces_change(client):
    local_store.AuthStore().ensure_local_admin()
    r = client.post("/auth/login",
                    data={"email": "ladmin", "password": "boot-pw-123"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/change_password"


def test_login_still_rejects_non_email_for_others(client):
    r = client.post("/auth/login",
                    data={"email": "notanemail", "password": "x"},
                    follow_redirects=False)
    assert r.status_code == 400


def test_reset_password_refuses_ladmin(client):
    local_store.AuthStore().ensure_local_admin()
    r = client.post("/auth/reset_password", data={"email": "ladmin"},
                    follow_redirects=False)
    assert r.status_code == 403
    # No unusable temp credential was written.
    assert not local_store.AuthStore().get_auth("ladmin").get("temp_password_hash")


def test_ladmin_login_without_bootstrap_password_gets_server_hint(client, monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_ADMIN_PASSWORD", "")
    local_store.AuthStore().ensure_local_admin()
    r = client.post("/auth/login", data={"email": "ladmin", "password": "x"},
                    follow_redirects=False)
    assert r.status_code == 403
    assert b"LOCAL_ADMIN_PASSWORD" in r.content


def test_ladmin_login_lands_on_admin_page(client):
    """The local admin is config-only: after the password is set, login goes
    straight to /admin/data_sources, never the /lab chat UI."""
    local_store.AuthStore().ensure_local_admin()
    local_store.AuthStore().set_password("ladmin", "final-pw")
    r = client.post("/auth/login", data={"email": "ladmin", "password": "final-pw"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/data_sources"


def test_ladmin_forced_change_lands_on_admin_page(client):
    """Completing the forced bootstrap change also targets the admin page."""
    local_store.AuthStore().ensure_local_admin()
    client.post("/auth/login", data={"email": "ladmin", "password": "boot-pw-123"},
                follow_redirects=False)
    r = client.post("/auth/change_password",
                    data={"new_password": "final-pw", "confirm_password": "final-pw"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/data_sources"


def test_normal_user_login_still_lands_on_lab(client):
    store = local_store.AuthStore()
    store.ensure_user("u@x.com")
    store.set_password("u@x.com", "user-pw")
    r = client.post("/auth/login", data={"email": "u@x.com", "password": "user-pw"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/lab"


def test_profile_exposes_is_local_admin_not_is_admin(client):
    local_store.AuthStore().ensure_local_admin()
    local_store.AuthStore().set_password("ladmin", "final-pw")
    client.post("/auth/login", data={"email": "ladmin", "password": "final-pw"},
                follow_redirects=False)
    prof = client.get("/auth/profile").json()
    assert prof["is_local_admin"] is True
    assert prof["is_admin_user"] is False   # 19g: bootstrap is NOT a promoted admin
    assert "is_admin" not in prof   # guards the dashboard.js Publish-menu trap


def test_is_bootstrap_admin_is_identity_not_permission(monkeypatch):
    """19g: only the configured ladmin identity is bootstrap — a PROMOTED
    admin (permission "admin" on a normal account) is not."""
    store = local_store.AuthStore()
    assert store.is_bootstrap_admin("ladmin") is True
    assert store.is_bootstrap_admin("LADMIN ") is True     # normalized compare
    store.ensure_user("promoted@x.com")
    store.set_role("promoted@x.com", "admin")
    assert store.is_admin("promoted@x.com") is True
    assert store.is_bootstrap_admin("promoted@x.com") is False
    assert store.is_bootstrap_admin("") is False
    # Empty LOCAL_ADMIN_USERNAME ⇒ nobody is bootstrap (never matches "").
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "")
    assert store.is_bootstrap_admin("ladmin") is False
    assert store.is_bootstrap_admin("") is False


def test_promoted_admin_login_lands_on_lab(client):
    """19g: a promoted admin is a full analysis user — login targets /lab,
    never the admin page (that stays bootstrap-only)."""
    store = local_store.AuthStore()
    store.ensure_user("promoted@x.com")
    store.set_password("promoted@x.com", "admin-pw")
    store.set_role("promoted@x.com", "admin")
    r = client.post("/auth/login",
                    data={"email": "promoted@x.com", "password": "admin-pw"},
                    follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/lab"
