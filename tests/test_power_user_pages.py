"""/power/data_sources page route + is_power_user surfaces (prompt 19):
per-audience redirects, the shared template rendered in power mode, the /lab
profile-dropdown "DB config" item only for power users, and is_power_user on
/auth/profile. Uses the REAL app WITHOUT entering the TestClient context
manager — that would run the lifespan and start the db_scheduler thread
(see test_version_endpoint)."""
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
import roles_store
from settings import settings

USER = "plain@x.com"
POWER = "power@x.com"
ADMIN2 = "admin2@x.com"
PW = "secret-pw"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    import routes.auth as auth_mod
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    auth = local_store.AuthStore()
    for email in (USER, POWER, ADMIN2):
        auth.ensure_user(email)
        auth.set_password(email, PW)
    auth.set_role(ADMIN2, "admin")
    auth.set_role(POWER, "power")            # 19e: permission on the user
    role = roles_store.RolesStore().create_role(
        {"name": "PU", "scope_grants": []}, actor=ADMIN2)
    auth.set_data_role(POWER, role["id"])
    return TestClient(app_mod.app)


def _login(client, email):
    r = client.post("/auth/login", data={"email": email, "password": PW},
                    follow_redirects=False)
    assert r.status_code in (302, 303), (r.status_code, r.text)
    return r


def test_power_page_redirects_per_audience(client):
    # Anonymous → landing.
    r = client.get("/power/data_sources", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/"
    # Plain user → /lab.
    _login(client, USER)
    r = client.get("/power/data_sources", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/lab"


def test_power_page_redirects_ladmin_to_admin_page(client):
    _login(client, ADMIN2)
    r = client.get("/power/data_sources", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/data_sources"


def test_power_page_renders_power_mode_for_power_user(client):
    # Login target stays /lab — a power user is a normal chat user.
    assert _login(client, POWER).headers["location"] == "/lab"
    r = client.get("/power/data_sources")
    assert r.status_code == 200
    assert '__MANAGER_MODE__ = "power"' in r.text
    # Ladmin-only chrome is not rendered in power mode.
    assert 'data-section="users"' not in r.text
    assert 'data-section="audit"' not in r.text
    assert 'id="btnAddConnection"' not in r.text
    # 19f: the wizard share panel IS rendered in power mode, retitled.
    assert 'id="twAccessRoles"' in r.text
    assert 'Share with your roles' in r.text
    assert 'Back to chat' in r.text
    # 19f scope summary: this power user holds no manage grants.
    assert 'You have no manage grants yet' in r.text


def test_admin_page_still_renders_admin_mode(client):
    _login(client, ADMIN2)
    r = client.get("/admin/data_sources")
    assert r.status_code == 200
    assert '__MANAGER_MODE__ = "admin"' in r.text
    assert 'data-section="users"' in r.text
    assert 'id="btnAddConnection"' in r.text
    # 19g: a PROMOTED admin is a chat user too — the footer links back.
    assert 'Back to chat' in r.text


def test_promoted_admin_is_full_lab_user(client):
    """19g: a promoted admin renders /lab (with the B2C is_admin flag still
    False — the Publish-menu trap), reaches their chats, and the dropdown's
    DB config points at the FULL admin page."""
    _login(client, ADMIN2)
    r = client.get("/lab")
    assert r.status_code == 200
    assert 'window.__IS_ADMIN__ = false' in r.text
    assert 'id="btnDbConfig"' in r.text
    assert 'data-target="/admin/data_sources"' in r.text
    # The chat sidebar API answers (regression for the pre-19g lockout).
    assert client.get("/auth/active_chats").status_code == 200
    # /power still redirects any admin to the full page.
    r = client.get("/power/data_sources", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/data_sources"


def test_bootstrap_ladmin_stays_config_only(client, monkeypatch):
    """19g: the bootstrap account keeps the old behavior byte-identically —
    /lab redirects away and its admin page has no Back-to-chat link."""
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    auth = local_store.AuthStore()
    auth.ensure_user("ladmin")
    auth.set_password("ladmin", PW)
    auth.set_role("ladmin", "admin")
    _login(client, "ladmin")
    r = client.get("/lab", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/admin/data_sources"
    r = client.get("/admin/data_sources")
    assert r.status_code == 200
    assert 'Back to chat' not in r.text


def test_lab_dropdown_db_config_only_for_power_users(client):
    _login(client, POWER)
    r = client.get("/lab")
    assert r.status_code == 200 and 'id="btnDbConfig"' in r.text
    assert 'data-target="/power/data_sources"' in r.text   # 19g: power keeps /power
    client.post("/auth/logout")
    _login(client, USER)
    r = client.get("/lab")
    assert r.status_code == 200 and 'id="btnDbConfig"' not in r.text


def test_profile_carries_is_power_user(client):
    _login(client, POWER)
    prof = client.get("/auth/profile").json()
    assert prof["is_power_user"] is True
    assert prof["is_admin_user"] is False    # power is not admin (19g flag)
    assert "is_admin" not in prof            # the B2C Publish-menu trap stays shut
    client.post("/auth/logout")
    _login(client, USER)
    prof = client.get("/auth/profile").json()
    assert prof["is_power_user"] is False
    assert prof["is_admin_user"] is False
    client.post("/auth/logout")
    _login(client, ADMIN2)
    prof = client.get("/auth/profile").json()
    assert prof["is_admin_user"] is True     # promoted admin (19g)
    assert prof["is_power_user"] is False
    assert "is_admin" not in prof


# ── 19f: scope summary + template QA assertions ────────────────────────────

def test_power_scope_summary_names_grants_and_read_beyond(client):
    """The /power header names the manage scope from manage_grants and warns
    when read access reaches beyond it."""
    import db_sources
    import roles_store
    store = db_sources.DataSourceStore()
    conn = store.create_connection(
        {"name": "warehouse", "db_type": "postgresql", "host": "h",
         "port": 5432, "database": "d", "user": "u"}, "pw", actor=ADMIN2)
    read_tbl = store.upsert_table({
        "connection_id": conn["id"], "schema": "hr", "table_name": "payroll",
        "display_name": "payroll", "description": "", "is_connector": False,
        "relations": [], "columns": []}, actor=ADMIN2)
    rs = roles_store.RolesStore()
    role = rs.create_role(
        {"name": "MG", "manage_grants": [{"connection_id": conn["id"],
                                          "schema": "demo"}],
         "table_ids": [read_tbl["id"]]}, actor=ADMIN2)
    local_store.AuthStore().set_data_roles(POWER, [role["id"]])
    _login(client, POWER)
    r = client.get("/power/data_sources")
    assert r.status_code == 200
    assert "You can manage: warehouse / demo" in r.text
    # payroll (hr) is readable but outside the manage scope → the hint shows.
    assert "Read-only roles give chat access, not management" in r.text
    # Whole-connection grant wording.
    rs.update_role(role["id"], {"manage_grants": [
        {"connection_id": conn["id"], "schema": None}], "table_ids": []},
        actor=ADMIN2)
    r = client.get("/power/data_sources")
    assert "You can manage: warehouse — all schemas" in r.text
    assert "Read-only roles give chat access" not in r.text


def test_admin_template_qa_markers(client):
    """19f QA: the user-search input can never be Chrome-autofilled, the
    wizard offers the system-schema reveal, and the admin Access panel keeps
    its wording."""
    _login(client, ADMIN2)
    html = client.get("/admin/data_sources").text
    assert 'id="userSearch"' in html
    assert 'autocomplete="off"' in html
    assert 'name="pdc-user-filter"' in html
    assert 'Search by email' not in html         # placeholder dropped the word
    assert 'Filter users…' in html
    assert 'id="twShowSystemSchemas"' in html
    assert 'id="twAccessRoles"' in html
    assert '<h4>Access ' in html                 # admin heading unchanged
    assert 'Share with your roles' not in html   # power-only heading
