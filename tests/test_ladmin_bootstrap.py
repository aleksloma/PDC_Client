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


NEUTRAL_RESET = "If an account exists for this address, a reset link has been sent."


def _reset_seams(monkeypatch):
    import routes.auth as auth_mod
    sent = []
    monkeypatch.setattr(auth_mod.brain_client, "send_password_reset_email",
                        lambda *a, **k: sent.append((a, k)))
    monkeypatch.setattr(auth_mod, "_run_in_background",
                        lambda fn, *args: fn(*args), raising=False)
    return sent


def test_reset_password_refuses_ladmin(client, monkeypatch):
    """Task 9 (plan §2): the non-email bootstrap id is not an address, so
    the reset answers 400 "Please enter a valid email" like any non-email id --
    nothing minted (no reset token, no temp password), no mail attempted."""
    sent = _reset_seams(monkeypatch)
    local_store.AuthStore().ensure_local_admin()
    r = client.post("/auth/reset_password", data={"email": "ladmin"},
                    follow_redirects=False)
    assert r.status_code == 400, r.status_code
    assert "Please enter a valid email" in r.text
    auth = local_store.AuthStore().get_auth("ladmin")
    assert not auth.get("reset_token_hash")
    assert not auth.get("temp_password_hash")
    assert sent == []


def test_reset_password_for_an_email_shaped_ladmin_is_neutral(client, monkeypatch):
    """Task 9 (D9-6): an email-shaped bootstrap account gets the SAME neutral
    200 page as any address, but nothing is minted and no mail is attempted
    (the account has no mailbox)."""
    sent = _reset_seams(monkeypatch)
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "admin@corp.example")
    local_store.AuthStore().ensure_local_admin()
    r = client.post("/auth/reset_password", data={"email": "admin@corp.example"},
                    follow_redirects=False)
    assert r.status_code == 200, r.status_code
    assert NEUTRAL_RESET in r.text
    auth = local_store.AuthStore().get_auth("admin@corp.example")
    assert not auth.get("reset_token_hash")
    assert not auth.get("temp_password_hash")
    assert sent == []


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


# ---------------------------------------------------------------------------
# a bootstrap password shorter than the password rule is flagged in the log
#
# The bootstrap password is exempt from the length rule (refusing it would
# lock a fresh install out), so a short one is created all the same and a
# `LADMIN_BOOTSTRAP_WEAK` warning is logged -- only when the password is
# actually CREATED, never when the account already has one, and never with
# the password in the line.
# ---------------------------------------------------------------------------
WEAK_BOOT_PW = "wk-pw-7"          # 7 characters
LONG_BOOT_PW = "long-enough-boot-pw"


def _capture_store_log(monkeypatch):
    lines = []

    def rec(sid, level, message, **ctx):
        lines.append((str(level), " ".join([str(sid), str(level), str(message)]
                                           + [f"{k}={v}" for k, v in ctx.items()])))

    monkeypatch.setattr(local_store, "log_with_sid", rec)
    return lines


def _weak_lines(lines):
    return [(lvl, ln) for lvl, ln in lines if "LADMIN_BOOTSTRAP_WEAK" in ln]


def test_a_short_bootstrap_password_is_created_and_flagged(monkeypatch):
    monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 8)
    monkeypatch.setattr(settings, "LOCAL_ADMIN_PASSWORD", WEAK_BOOT_PW)
    lines = _capture_store_log(monkeypatch)
    store = local_store.AuthStore()
    store.ensure_local_admin()
    assert store.verify_password("ladmin", WEAK_BOOT_PW) == "ok", \
        "the bootstrap must still happen with a short password"
    weak = _weak_lines(lines)
    assert len(weak) == 1, [ln for _, ln in lines]
    level, line = weak[0]
    assert level == "warning", weak
    assert WEAK_BOOT_PW not in line, "the bootstrap password reached the log"


def test_a_long_enough_bootstrap_password_is_not_flagged(monkeypatch):
    monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 8)
    monkeypatch.setattr(settings, "LOCAL_ADMIN_PASSWORD", LONG_BOOT_PW)
    lines = _capture_store_log(monkeypatch)
    store = local_store.AuthStore()
    store.ensure_local_admin()
    assert store.verify_password("ladmin", LONG_BOOT_PW) == "ok"
    assert not _weak_lines(lines), [ln for _, ln in lines]


def test_the_weak_line_follows_the_setting(monkeypatch):
    """The comparison reads PASSWORD_MIN_LENGTH: 19 characters are short
    under a minimum of 20."""
    monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 20)
    monkeypatch.setattr(settings, "LOCAL_ADMIN_PASSWORD", LONG_BOOT_PW)
    lines = _capture_store_log(monkeypatch)
    local_store.AuthStore().ensure_local_admin()
    assert len(_weak_lines(lines)) == 1, [ln for _, ln in lines]


def test_no_weak_line_when_the_admin_already_has_a_password(monkeypatch):
    monkeypatch.setattr(settings, "PASSWORD_MIN_LENGTH", 8)
    store = local_store.AuthStore()
    store.ensure_local_admin()                       # created with boot-pw-123
    store.set_password("ladmin", "chosen-by-admin")
    monkeypatch.setattr(settings, "LOCAL_ADMIN_PASSWORD", WEAK_BOOT_PW)
    lines = _capture_store_log(monkeypatch)
    store.ensure_local_admin()
    assert not _weak_lines(lines), [ln for _, ln in lines]
    assert store.verify_password("ladmin", "chosen-by-admin") == "ok"
