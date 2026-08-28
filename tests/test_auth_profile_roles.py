"""AuthStore data_role / permission / last_login_at / list_users + the
login-time stamp: an OLD-shape profile.json (no data_role, no last_login_at)
keeps loading and resolves to Base; set_data_role preserves the other profile
keys; the 19e permission normalization (get_role → user/power/admin, garbage
→ user; is_power); BOTH login branches (new-user first-password and
returning-password) stamp last_login_at through the _start_session funnel;
list_users includes admin-permission users (19e) but skips the bootstrap
ladmin and unreadable/profileless dirs. Offline — brain calls stubbed."""
import json

import pytest
from fastapi import FastAPI
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
from settings import settings


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    yield


def test_old_profile_without_data_role_reads_base(tmp_path):
    """Pre-feature profile shape (customers upgrade in place)."""
    udir = tmp_path / "users" / "old@x.com"
    udir.mkdir(parents=True)
    (udir / "profile.json").write_text(json.dumps(
        {"email": "old@x.com", "created_at": "2025-01-01T00:00:00+00:00"}),
        encoding="utf-8")
    auth = local_store.AuthStore()
    assert auth.get_data_role("old@x.com") == "base"
    assert auth.get_data_roles("old@x.com") == []      # raw: nothing held
    rows = auth.list_users()
    assert rows == [{"email": "old@x.com", "role": "user",
                     "data_role": "base", "data_roles": [],
                     "created_at": "2025-01-01T00:00:00+00:00",
                     "last_login_at": None}]


def test_set_data_role_persists_and_preserves_other_keys():
    auth = local_store.AuthStore()
    auth.ensure_user("u@x.com")
    created = auth.get_profile("u@x.com")["created_at"]
    auth.set_role("u@x.com", "user")
    auth.set_data_role("u@x.com", "aa11bb22cc33dd44")
    prof = auth.get_profile("u@x.com")
    assert prof["data_role"] == "aa11bb22cc33dd44"
    assert prof["created_at"] == created
    assert prof["role"] == "user"
    assert auth.get_data_role("u@x.com") == "aa11bb22cc33dd44"


def test_set_data_roles_writes_list_and_legacy_mirror():
    """19c: `data_roles` is the source of truth; the single `data_role` is
    mirrored to the FIRST id so an older build on the same DATA_ROOT keeps
    working (stored-shape compat both directions)."""
    auth = local_store.AuthStore()
    auth.ensure_user("m@x.com")
    auth.set_data_roles("m@x.com", ["aa11bb22cc33dd44", "cc33dd44ee55ff66",
                                    "aa11bb22cc33dd44"])   # dupes collapse
    prof = auth.get_profile("m@x.com")
    assert prof["data_roles"] == ["aa11bb22cc33dd44", "cc33dd44ee55ff66"]
    assert prof["data_role"] == "aa11bb22cc33dd44"
    assert auth.get_data_roles("m@x.com") == ["aa11bb22cc33dd44",
                                              "cc33dd44ee55ff66"]
    assert auth.get_data_role("m@x.com") == "aa11bb22cc33dd44"   # shim = first
    # Empty list: raw [] + "base" mirror.
    auth.set_data_roles("m@x.com", [])
    prof = auth.get_profile("m@x.com")
    assert prof["data_roles"] == [] and prof["data_role"] == "base"
    # A LEGACY single-role profile reads as a one-element list.
    auth.set_data_role("l@x.com", "bb22cc33dd44ee55")
    p = auth.get_profile("l@x.com")
    del p["data_roles"]                       # simulate a pre-19c write
    import json as _json
    (local_store._data_root() / "users" / "l@x.com" / "profile.json").write_text(
        _json.dumps(p), encoding="utf-8")
    assert auth.get_data_roles("l@x.com") == ["bb22cc33dd44ee55"]


def test_touch_last_login_stamps_profile():
    auth = local_store.AuthStore()
    auth.ensure_user("u@x.com")
    assert auth.get_profile("u@x.com").get("last_login_at") is None
    auth.touch_last_login("u@x.com")
    assert auth.get_profile("u@x.com")["last_login_at"]


def test_list_users_includes_admins_skips_ladmin_and_broken_dirs(tmp_path):
    """19e: admin-permission users ARE listed (they must stay demotable);
    only the bootstrap ladmin account itself is excluded."""
    auth = local_store.AuthStore()
    auth.ensure_user("a@x.com")
    auth.ensure_user("ladmin")
    auth.set_role("ladmin", "admin")
    auth.ensure_user("admin2@x.com")
    auth.set_role("admin2@x.com", "admin")
    (tmp_path / "users" / "bare_dir").mkdir(parents=True)   # no profile.json
    broken = tmp_path / "users" / "broken@x.com"
    broken.mkdir(parents=True)
    (broken / "profile.json").write_text("{not json", encoding="utf-8")
    rows = auth.list_users()
    assert [u["email"] for u in rows] == ["a@x.com", "admin2@x.com"]
    assert {u["email"]: u["role"] for u in rows} == {
        "a@x.com": "user", "admin2@x.com": "admin"}


def test_permission_normalization_and_is_power():
    auth = local_store.AuthStore()
    auth.ensure_user("p@x.com")
    assert auth.get_role("p@x.com") == "user"        # absent → standard
    auth.set_role("p@x.com", "power")
    assert auth.get_role("p@x.com") == "power"
    assert auth.is_power("p@x.com") and not auth.is_admin("p@x.com")
    auth.set_role("p@x.com", "admin")
    assert auth.is_admin("p@x.com") and not auth.is_power("p@x.com")
    # Legacy/garbage stored values read as "user" (stored-shape compat).
    auth.set_role("p@x.com", "superuser")
    assert auth.get_role("p@x.com") == "user"
    assert not auth.is_power("p@x.com") and not auth.is_admin("p@x.com")


# ── login stamps (through the real /auth/login handler) ────────────────────

@pytest.fixture
def client(monkeypatch):
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)
    return TestClient(app)


def test_login_stamps_last_login_on_both_branches(client):
    auth = local_store.AuthStore()
    # New-user branch (no user folder → entered password becomes theirs).
    r = client.post("/auth/login",
                    data={"email": "new@x.com", "password": "pw123"},
                    follow_redirects=False)
    assert r.status_code == 302
    first = auth.get_profile("new@x.com")["last_login_at"]
    assert first
    # Returning-user branch (password verify path).
    r2 = client.post("/auth/login",
                     data={"email": "new@x.com", "password": "pw123"},
                     follow_redirects=False)
    assert r2.status_code == 302
    second = auth.get_profile("new@x.com")["last_login_at"]
    assert second and second >= first
