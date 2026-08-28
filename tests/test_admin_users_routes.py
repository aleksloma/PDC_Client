"""Admin /api/admin/users* + /api/admin/roles* surface: role guard on every
route (401/403 + admin.denied audit), users listing (ladmin excluded, admins
included, resolved role ids + permission), set_role validation + audit,
set_permission (19e per-user permission), role CRUD validation (dup/reserved
names, unknown tables/connections, connector grants), Base protections, and
delete-role's DYNAMIC member revert (count only — no profile rewrite).
Offline."""
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_sources
import local_store
import roles_store
from settings import settings

ADMIN = "ladmin"
USER = "user@x.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "ladmin")
    local_store.AuthStore().ensure_user(ADMIN)
    local_store.AuthStore().set_role(ADMIN, "admin")
    local_store.AuthStore().ensure_user(USER)
    roles_store.RolesStore().ensure_base_role()

    import routes.admin_users as users_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(users_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session.pop("must_change_password", None)
        return {"ok": True}

    return TestClient(app)


@pytest.fixture
def registry():
    """One connection + one normal table + one connector for grant checks."""
    store = db_sources.DataSourceStore()
    conn = store.create_connection(
        {"name": "c", "db_type": "postgresql", "host": "h", "port": 5432,
         "database": "d", "user": "u"}, "pw", actor=ADMIN)
    t = store.upsert_table({
        "connection_id": conn["id"], "schema": "shop", "table_name": "t1",
        "display_name": "t1", "description": "", "is_connector": False,
        "relations": [], "columns": []}, actor=ADMIN)
    c = store.upsert_table({
        "connection_id": conn["id"], "schema": "shop", "table_name": "dict",
        "display_name": "dict", "description": "", "is_connector": True,
        "relations": [], "columns": []}, actor=ADMIN)
    return {"conn": conn["id"], "table": t["id"], "connector": c["id"]}


def test_all_admin_user_routes_reject_non_admin(client):
    import routes.admin_users as users_mod
    paths = [(r.path, sorted(r.methods - {"HEAD", "OPTIONS"})[0])
             for r in users_mod.router.routes]
    for path, method in paths:
        p = path.replace("{rid}", "aa11bb22cc33dd44")
        resp = client.request(method, p)
        assert resp.status_code == 401, (p, resp.status_code)
    client.post(f"/_login/{USER}")
    for path, method in paths:
        p = path.replace("{rid}", "aa11bb22cc33dd44")
        resp = client.request(method, p)
        assert resp.status_code == 403, (p, resp.status_code)
    denied = [r for r in db_sources.read_audit_tail(500)
              if r["action"] == "admin.denied"]
    assert len(denied) == len(paths)


def test_users_listing_fields_and_ladmin_excluded(client):
    client.post(f"/_login/{ADMIN}")
    rows = client.get("/api/admin/users").json()["users"]
    assert [u["email"] for u in rows] == [USER]
    u = rows[0]
    assert u["role_id"] == "base" and u["role_name"] == "Base"
    assert u["permission"] == "standard"
    assert "created_at" in u and "last_login_at" in u


def test_set_role_happy_path_and_audit(client):
    client.post(f"/_login/{ADMIN}")
    role = client.post("/api/admin/roles", json={"name": "Fin"}).json()["role"]
    r = client.post("/api/admin/users/set_role",
                    json={"email": USER, "role_id": role["id"]})
    assert r.status_code == 200
    assert r.json()["user"]["role_id"] == role["id"]
    assert r.json()["user"]["role_ids"] == [role["id"]]
    assert local_store.AuthStore().get_data_role(USER) == role["id"]
    assert any(a["action"] == "user.set_roles" and a["target"] == USER
               for a in db_sources.read_audit_tail(50))


def test_set_role_validation(client):
    client.post(f"/_login/{ADMIN}")
    post = lambda body: client.post("/api/admin/users/set_role", json=body)
    assert post({"email": USER, "role_id": "ffffffffffffffff"}).status_code == 400
    assert post({"email": "ghost@x.com", "role_id": "base"}).status_code == 404
    assert post({"email": ADMIN, "role_id": "base"}).status_code == 400
    # 19g: a PROMOTED admin takes roles like any user — only the bootstrap
    # account above is refused.
    local_store.AuthStore().ensure_user("admin2@x.com")
    local_store.AuthStore().set_role("admin2@x.com", "admin")
    r = post({"email": "admin2@x.com", "role_id": "base"})
    assert r.status_code == 200
    assert r.json()["user"]["role_ids"] == ["base"]


def test_role_create_validation(client, registry):
    client.post(f"/_login/{ADMIN}")
    post = lambda body: client.post("/api/admin/roles", json=body)
    assert post({"name": ""}).status_code == 400
    assert post({"name": "  "}).status_code == 400
    assert post({"name": "BASE"}).status_code == 400          # reserved
    assert post({"name": "Fin"}).status_code == 201
    assert post({"name": "fin"}).status_code == 400           # dup, case-insensitive
    assert post({"name": "X", "table_ids": ["ffffffffffffffff"]}).status_code == 400
    assert post({"name": "X", "table_ids": [registry["connector"]]}).status_code == 400
    assert post({"name": "X", "scope_grants": [
        {"connection_id": "ffffffffffffffff", "schema": None}]}).status_code == 400
    assert post({"name": "X", "scope_grants": ["garbage"]}).status_code == 400
    ok = post({"name": "X", "table_ids": [registry["table"]],
               "scope_grants": [{"connection_id": registry["conn"],
                                 "schema": "shop"}]})
    assert ok.status_code == 201


def test_role_update_base_rename_400_description_ok(client):
    client.post(f"/_login/{ADMIN}")
    assert client.post("/api/admin/roles/base",
                       json={"name": "NotBase"}).status_code == 400
    r = client.post("/api/admin/roles/base", json={"description": "everyone"})
    assert r.status_code == 200
    assert r.json()["role"]["description"] == "everyone"
    assert r.json()["role"]["name"] == "Base"
    assert client.post("/api/admin/roles/ffffffffffffffff",
                       json={"description": "x"}).status_code == 404


def test_role_delete_reverts_members_dynamically(client):
    client.post(f"/_login/{ADMIN}")
    role = client.post("/api/admin/roles", json={"name": "Doomed"}).json()["role"]
    client.post("/api/admin/users/set_role",
                json={"email": USER, "role_id": role["id"]})
    raw_before = local_store.AuthStore().get_profile(USER)
    r = client.post(f"/api/admin/roles/{role['id']}/delete")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "reverted_members": 1}
    # DYNAMIC revert: the profile still carries the dangling id (no rewrite),
    # but every read-out resolves to Base.
    raw_after = local_store.AuthStore().get_profile(USER)
    assert raw_after["data_role"] == role["id"] == raw_before["data_role"]
    rows = client.get("/api/admin/users").json()["users"]
    assert rows[0]["role_id"] == "base"
    # Base cannot be deleted.
    assert client.post("/api/admin/roles/base/delete").status_code == 400


def test_roles_listing_member_count_and_order(client):
    client.post(f"/_login/{ADMIN}")
    fin = client.post("/api/admin/roles", json={"name": "Fin"}).json()["role"]
    client.post("/api/admin/users/set_role",
                json={"email": USER, "role_id": fin["id"]})
    roles = client.get("/api/admin/roles").json()["roles"]
    assert roles[0]["id"] == "base" and roles[0]["is_base"] is True
    by_id = {r["id"]: r for r in roles}
    assert by_id[fin["id"]]["member_count"] == 1
    assert by_id["base"]["member_count"] == 0


# ── power user (prompts 19 + 19e) ──────────────────────────────────────────

def test_builtin_role_edit_with_restated_name_succeeds(client):
    """19c bug regression: the UI save payload carries the UNCHANGED name for
    every role — a built-in edit must not 400 on it (it used to, so a
    built-in's description/grants could never be saved)."""
    client.post(f"/_login/{ADMIN}")
    r = client.post("/api/admin/roles/base",
                    json={"name": "Base", "description": "everyone",
                          "table_ids": [], "scope_grants": []})
    assert r.status_code == 200, r.json()
    assert r.json()["role"]["description"] == "everyone"
    assert r.json()["role"]["name"] == "Base"
    # A GENUINE rename still 400s.
    assert client.post("/api/admin/roles/base",
                       json={"name": "base2"}).status_code == 400


def test_manage_grants_validation_and_round_trip(client, registry):
    """19f: manage_grants validate exactly like scope_grants and persist
    independently of them."""
    client.post(f"/_login/{ADMIN}")
    post = lambda body: client.post("/api/admin/roles", json=body)
    assert post({"name": "M1", "manage_grants": "nope"}).status_code == 400
    assert post({"name": "M1", "manage_grants": ["garbage"]}).status_code == 400
    assert post({"name": "M1", "manage_grants": [
        {"connection_id": "ffffffffffffffff"}]}).status_code == 400   # unknown conn
    r = post({"name": "M1",
              "scope_grants": [{"connection_id": registry["conn"], "schema": "shop"}],
              "manage_grants": [{"connection_id": registry["conn"], "schema": None}]})
    assert r.status_code == 201, r.json()
    role = r.json()["role"]
    assert role["manage_grants"] == [{"connection_id": registry["conn"],
                                      "schema": None}]
    assert role["scope_grants"] == [{"connection_id": registry["conn"],
                                     "schema": "shop"}]
    # Edit replaces manage_grants without touching scope_grants (and back).
    r = client.post(f"/api/admin/roles/{role['id']}", json={"manage_grants": []})
    assert r.status_code == 200
    out = r.json()["role"]
    assert out["manage_grants"] == [] and out["scope_grants"] == role["scope_grants"]


def test_stray_power_user_key_ignored(client):
    """The 19c-era power_user role flag is gone — a stray body key must be
    silently ignored (old payloads keep working), never stored or 400d."""
    client.post(f"/_login/{ADMIN}")
    r = client.post("/api/admin/roles", json={"name": "PU", "power_user": True})
    assert r.status_code == 201
    role = r.json()["role"]
    assert "power_user" not in role
    r = client.post(f"/api/admin/roles/{role['id']}", json={"power_user": "yes"})
    assert r.status_code == 200
    assert "power_user" not in roles_store.RolesStore().get_role(role["id"])


def test_set_roles_list_and_legacy_body(client):
    client.post(f"/_login/{ADMIN}")
    fin = client.post("/api/admin/roles", json={"name": "Fin"}).json()["role"]
    ops = client.post("/api/admin/roles", json={"name": "Ops"}).json()["role"]
    # New shape: a LIST of held roles.
    r = client.post("/api/admin/users/set_role",
                    json={"email": USER, "role_ids": [fin["id"], ops["id"]]})
    assert r.status_code == 200
    assert r.json()["user"]["role_ids"] == [fin["id"], ops["id"]]
    assert r.json()["user"]["role_names"] == ["Fin", "Ops"]
    prof = local_store.AuthStore().get_profile(USER)
    assert prof["data_roles"] == [fin["id"], ops["id"]]
    assert prof["data_role"] == fin["id"]          # downgrade mirror = first
    # Legacy body shape still accepted.
    r = client.post("/api/admin/users/set_role",
                    json={"email": USER, "role_id": fin["id"]})
    assert r.status_code == 200
    assert r.json()["user"]["role_ids"] == [fin["id"]]
    # Empty list reverts to Base.
    r = client.post("/api/admin/users/set_role",
                    json={"email": USER, "role_ids": []})
    assert r.status_code == 200
    assert r.json()["user"]["role_ids"] == ["base"]
    # Unknown id anywhere in the list → 400, nothing changed.
    assert client.post("/api/admin/users/set_role",
                       json={"email": USER,
                             "role_ids": ["ffffffffffffffff"]}).status_code == 400
    # LEGACY shape with a missing/empty role_id keeps its pre-19c 400 — it
    # must never silently clear the held roles (only an EXPLICIT empty
    # role_ids list means "revert to Base").
    client.post("/api/admin/users/set_role",
                json={"email": USER, "role_ids": [fin["id"]]})
    assert client.post("/api/admin/users/set_role",
                       json={"email": USER}).status_code == 400
    assert client.post("/api/admin/users/set_role",
                       json={"email": USER, "role_id": ""}).status_code == 400
    assert local_store.AuthStore().get_data_roles(USER) == [fin["id"]]


def test_member_counts_and_revert_with_multi_roles(client):
    client.post(f"/_login/{ADMIN}")
    fin = client.post("/api/admin/roles", json={"name": "Fin"}).json()["role"]
    ops = client.post("/api/admin/roles", json={"name": "Ops"}).json()["role"]
    client.post("/api/admin/users/set_role",
                json={"email": USER, "role_ids": [fin["id"], ops["id"]]})
    by_id = {r["id"]: r for r in client.get("/api/admin/roles").json()["roles"]}
    # A user with two roles counts as a member of BOTH.
    assert by_id[fin["id"]]["member_count"] == 1
    assert by_id[ops["id"]]["member_count"] == 1
    assert by_id["base"]["member_count"] == 0
    # Deleting one held role counts the holder once; the other role survives.
    r = client.post(f"/api/admin/roles/{fin['id']}/delete")
    assert r.status_code == 200 and r.json()["reverted_members"] == 1
    rows = client.get("/api/admin/users").json()["users"]
    assert rows[0]["role_ids"] == [ops["id"]]      # dangling Fin dropped


# ── per-user permission (prompt 19e) ───────────────────────────────────────

def _perm(client, email, permission):
    return client.post("/api/admin/users/set_permission",
                       json={"email": email, "permission": permission})


def test_set_permission_happy_paths_and_audit(client):
    client.post(f"/_login/{ADMIN}")
    auth = local_store.AuthStore()
    r = _perm(client, USER, "power")
    assert r.status_code == 200
    assert r.json()["user"]["permission"] == "power"
    assert auth.get_role(USER) == "power" and auth.is_power(USER)
    r = _perm(client, USER, "admin")
    assert r.status_code == 200 and auth.is_admin(USER)
    assert not auth.is_power(USER)                 # admin is NOT power
    r = _perm(client, USER, "standard")
    assert r.status_code == 200
    assert auth.get_role(USER) == "user"           # "standard" stored as "user"
    assert r.json()["user"]["permission"] == "standard"
    rows = [a for a in db_sources.read_audit_tail(50)
            if a["action"] == "user.set_permission" and a["target"] == USER]
    # read_audit_tail is newest-first.
    assert [a["detail"]["old"] + ">" + a["detail"]["new"] for a in rows] == [
        "admin>standard", "power>admin", "standard>power"]


def test_set_permission_refusals(client):
    client.post(f"/_login/{ADMIN}")
    assert _perm(client, ADMIN, "power").status_code == 400      # bootstrap ladmin
    assert _perm(client, "ghost@x.com", "power").status_code == 404
    assert _perm(client, USER, "root").status_code == 400        # invalid value
    assert client.post("/api/admin/users/set_permission",
                       json={"permission": "power"}).status_code == 400
    # No self-demotion: a promoted second admin cannot change their own row.
    local_store.AuthStore().ensure_user("admin2@x.com")
    local_store.AuthStore().set_role("admin2@x.com", "admin")
    client.post("/_login/admin2@x.com")
    assert _perm(client, "admin2@x.com", "standard").status_code == 400
    assert _perm(client, USER, "power").status_code == 200       # others: fine


def test_admin_keeps_data_roles_through_demote_roundtrip(client):
    """19g: promoting to admin keeps data_roles ACTIVE and editable (a
    promoted admin is a full analysis user); a demote round-trip preserves
    the held list."""
    client.post(f"/_login/{ADMIN}")
    fin = client.post("/api/admin/roles", json={"name": "Fin"}).json()["role"]
    client.post("/api/admin/users/set_role",
                json={"email": USER, "role_ids": [fin["id"]]})
    assert _perm(client, USER, "admin").status_code == 200
    # While admin: listed with permission=admin, roles kept AND editable.
    rows = client.get("/api/admin/users").json()["users"]
    row = next(u for u in rows if u["email"] == USER)
    assert row["permission"] == "admin"
    assert row["role_ids"] == [fin["id"]]
    ops = client.post("/api/admin/roles", json={"name": "Ops"}).json()["role"]
    r = client.post("/api/admin/users/set_role",
                    json={"email": USER, "role_ids": [fin["id"], ops["id"]]})
    assert r.status_code == 200
    assert _perm(client, USER, "standard").status_code == 200
    assert local_store.AuthStore().get_data_roles(USER) == [fin["id"], ops["id"]]


def test_users_listing_includes_admins_and_permission(client):
    client.post(f"/_login/{ADMIN}")
    local_store.AuthStore().ensure_user("admin2@x.com")
    local_store.AuthStore().set_role("admin2@x.com", "admin")
    local_store.AuthStore().ensure_user("pu@x.com")
    local_store.AuthStore().set_role("pu@x.com", "power")
    rows = client.get("/api/admin/users").json()["users"]
    by_email = {u["email"]: u for u in rows}
    assert set(by_email) == {USER, "admin2@x.com", "pu@x.com"}   # no ladmin
    assert by_email[USER]["permission"] == "standard"
    assert by_email["admin2@x.com"]["permission"] == "admin"
    assert by_email["pu@x.com"]["permission"] == "power"
