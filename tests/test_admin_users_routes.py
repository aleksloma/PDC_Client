"""Admin /api/admin/users* + /api/admin/roles* surface: role guard on every
route (401/403 + admin.denied audit), users listing (ladmin excluded, resolved
role ids), set_role validation + audit, role CRUD validation (dup/reserved
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
    assert "created_at" in u and "last_login_at" in u


def test_set_role_happy_path_and_audit(client):
    client.post(f"/_login/{ADMIN}")
    role = client.post("/api/admin/roles", json={"name": "Fin"}).json()["role"]
    r = client.post("/api/admin/users/set_role",
                    json={"email": USER, "role_id": role["id"]})
    assert r.status_code == 200
    assert r.json()["user"]["role_id"] == role["id"]
    assert local_store.AuthStore().get_data_role(USER) == role["id"]
    assert any(a["action"] == "user.set_role" and a["target"] == USER
               for a in db_sources.read_audit_tail(50))


def test_set_role_validation(client):
    client.post(f"/_login/{ADMIN}")
    post = lambda body: client.post("/api/admin/users/set_role", json=body)
    assert post({"email": USER, "role_id": "ffffffffffffffff"}).status_code == 400
    assert post({"email": "ghost@x.com", "role_id": "base"}).status_code == 404
    assert post({"email": ADMIN, "role_id": "base"}).status_code == 400
    # A non-ladmin admin account is also refused.
    local_store.AuthStore().ensure_user("admin2@x.com")
    local_store.AuthStore().set_role("admin2@x.com", "admin")
    assert post({"email": "admin2@x.com", "role_id": "base"}).status_code == 400


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
