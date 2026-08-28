"""Role-gate enforcement on the DB-table picker + session selection, and the
wizard save's access_role_ids reconcile: /api/db_tables filtered to the
requester's effective access (explicit + scope grants, later-registered tables
covered), /session/db_tables 403 ROLE_DENIED on non-allowed seeds with the
connector closure exempt, save_table writing role grants (absent field = no
writes), delete_table pruning role table_ids. Offline."""
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
OWNER = "user@x.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(ADMIN)
    local_store.AuthStore().set_role(ADMIN, "admin")
    local_store.AuthStore().ensure_user(OWNER)
    roles_store.RolesStore().ensure_base_role()

    import routes.upload as upload_mod
    import routes.admin_data as admin_mod
    monkeypatch.setattr(upload_mod.brain_client, "post_activity",
                        lambda *a, **k: None)
    monkeypatch.setattr(admin_mod.brain_client, "schema_autofill",
                        lambda **k: {"file_description": "", "columns": {}})

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)
    app.include_router(admin_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_rolegate"
        request.session.pop("must_change_password", None)
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def registry(client):
    """Two normal tables + one connector related to the first (same shape as
    test_db_session_flow's registry — but NO Base grant: the requester starts
    with zero access)."""
    import pandas as pd
    store = db_sources.DataSourceStore()
    conn = store.create_connection(
        {"name": "c", "db_type": "postgresql", "host": "h", "port": 5432,
         "database": "d", "user": "u"}, "pw", actor=ADMIN)
    ids = {"conn": conn["id"]}
    specs = [
        ("cl_info", "clients information", False,
         [{"related_table": "city_dict", "join_keys": [["city_code", "city_code"]]}]),
        ("tr_data", "transactions", False, []),
        ("city_dict", "cities dictionary", True, []),
    ]
    for tname, disp, is_conn, rels in specs:
        t = store.upsert_table({
            "connection_id": conn["id"], "schema": "shop", "table_name": tname,
            "display_name": disp, "description": "", "is_connector": is_conn,
            "relations": rels,
            "columns": [{"name": "city_code", "dtype": "int",
                         "description": "", "indexed": True}],
        }, actor=ADMIN)
        ids[tname] = t["id"]
        pd.DataFrame({"city_code": [1, 2]}).to_parquet(
            local_store.db_snapshot_path(t["id"]))
    return ids


def _grant(role_fields):
    """Create a role with the given grants and assign OWNER to it."""
    role = roles_store.RolesStore().create_role(role_fields, actor=ADMIN)
    local_store.AuthStore().set_data_role(OWNER, role["id"])
    return role


# ── picker (/api/db_tables) ────────────────────────────────────────────────

def test_api_db_tables_empty_for_base_role(client, registry):
    r = client.get("/api/db_tables")
    assert r.status_code == 200
    assert r.json()["tables"] == []


def test_api_db_tables_filtered_by_explicit_grant(client, registry):
    _grant({"name": "R", "table_ids": [registry["cl_info"]]})
    names = {t["display_name"] for t in client.get("/api/db_tables").json()["tables"]}
    assert names == {"clients information"}


def test_api_db_tables_scope_grant_covers_connection(client, registry):
    _grant({"name": "R", "scope_grants": [
        {"connection_id": registry["conn"], "schema": None}]})
    names = {t["display_name"] for t in client.get("/api/db_tables").json()["tables"]}
    # Both normal tables; the connector stays hidden as before.
    assert names == {"clients information", "transactions"}


def test_api_db_tables_union_across_multiple_roles(client, registry):
    """19c: a user holding SEVERAL roles sees the UNION of their grants — the
    route path (chat/upload gates ride the same allowed_table_ids_for)."""
    store = roles_store.RolesStore()
    a = store.create_role({"name": "A", "table_ids": [registry["cl_info"]]},
                          actor=ADMIN)
    b = store.create_role({"name": "B", "table_ids": [registry["tr_data"]]},
                          actor=ADMIN)
    local_store.AuthStore().set_data_roles(OWNER, [a["id"], b["id"]])
    names = {t["display_name"] for t in client.get("/api/db_tables").json()["tables"]}
    assert names == {"clients information", "transactions"}


def test_new_table_under_granted_schema_visible_without_role_edit(client, registry):
    import pandas as pd
    _grant({"name": "R", "scope_grants": [
        {"connection_id": registry["conn"], "schema": "shop"}]})
    t = db_sources.DataSourceStore().upsert_table({
        "connection_id": registry["conn"], "schema": "shop",
        "table_name": "orders", "display_name": "orders", "description": "",
        "is_connector": False, "relations": [], "columns": []}, actor=ADMIN)
    pd.DataFrame({"a": [1]}).to_parquet(local_store.db_snapshot_path(t["id"]))
    names = {r["display_name"] for r in client.get("/api/db_tables").json()["tables"]}
    assert "orders" in names


# ── selection (/session/db_tables) ─────────────────────────────────────────

def test_session_db_tables_403_on_non_allowed_seed(client, registry):
    r = client.post("/session/db_tables",
                    json={"table_ids": [registry["cl_info"]]})
    assert r.status_code == 403
    body = r.json()
    assert body["code"] == "ROLE_DENIED"
    assert "clients information" in body["error"]


def test_session_db_tables_connector_closure_exempt(client, registry):
    """The role grants ONE table; its connector joins in through the closure
    exactly as before — gating connectors would silently break allowed joins."""
    _grant({"name": "R", "table_ids": [registry["cl_info"]]})
    r = client.post("/session/db_tables",
                    json={"table_ids": [registry["cl_info"]]})
    assert r.status_code == 200
    keys = {row["df_key"] for row in r.json()["tables"]}
    assert keys == {"clients information", "cities dictionary"}


def test_session_db_tables_partial_denial_names_denied_only(client, registry):
    _grant({"name": "R", "table_ids": [registry["cl_info"]]})
    r = client.post("/session/db_tables",
                    json={"table_ids": [registry["cl_info"], registry["tr_data"]]})
    assert r.status_code == 403
    assert "transactions" in r.json()["error"]
    assert "clients information" not in r.json()["error"]


# ── wizard save → role grants (canonical on the ROLE record) ───────────────

@pytest.fixture
def sqlite_conn(client, tmp_path):
    """A saved sqlite connection with a seeded source table (store-level —
    the API rejects the hidden sqlite dialect by design)."""
    from sqlalchemy import create_engine, text
    db = tmp_path / "src.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)"))
        conn.execute(text("INSERT INTO t VALUES (1, 'x'), (2, 'y')"))
    eng.dispose()
    masked = db_sources.DataSourceStore().create_connection(
        {"name": "S", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor=ADMIN)
    return masked["id"]


def _table_body(cid, **extra):
    body = {"connection_id": cid, "schema": "", "table_name": "t",
            "display_name": "test table", "description": "desc",
            "columns": [{"name": "a", "dtype": "INTEGER", "description": "col a",
                         "indexed": True, "pk": True},
                        {"name": "b", "dtype": "TEXT", "description": "col b",
                         "indexed": False}],
            "is_connector": False, "relations": [], "confirm": True}
    body.update(extra)
    return body


def test_save_table_access_role_ids_reconciles_roles(client, sqlite_conn):
    client.post(f"/_login/{ADMIN}")
    rs = roles_store.RolesStore()
    a = rs.create_role({"name": "A"}, actor=ADMIN)
    b = rs.create_role({"name": "B"}, actor=ADMIN)

    r = client.post("/api/admin/tables",
                    json=_table_body(sqlite_conn, access_role_ids=[a["id"]]))
    assert r.status_code == 201
    tid = r.json()["table"]["id"]
    assert tid in rs.get_role(a["id"])["table_ids"]
    assert tid not in rs.get_role(b["id"])["table_ids"]
    # The table doc itself never stores access (canonical on the role).
    assert "access_role_ids" not in r.json()["table"]

    # Edit WITHOUT the field → no role writes (rec-accept / old payloads).
    r2 = client.post(f"/api/admin/tables/{tid}", json=_table_body(sqlite_conn))
    assert r2.status_code == 200
    assert tid in rs.get_role(a["id"])["table_ids"]

    # Edit moving access A → B (exact reconcile).
    r3 = client.post(f"/api/admin/tables/{tid}",
                     json=_table_body(sqlite_conn, access_role_ids=[b["id"]]))
    assert r3.status_code == 200
    assert tid not in rs.get_role(a["id"])["table_ids"]
    assert tid in rs.get_role(b["id"])["table_ids"]

    # Delete prunes the id from every role.
    assert client.post(f"/api/admin/tables/{tid}/delete", json={}).status_code == 200
    assert tid not in rs.get_role(b["id"])["table_ids"]
