"""Power-user (prompt 19) behavior on /api/admin/*: scoped registration with
`registered_by` ownership, access_role_ids ignored, filtered listings, schema-
level scope, relation scope, refresh/schedule scope, the delete ownership
matrix, and actor_kind-labeled audit rows. Offline via the hidden sqlite
dialect, same fixture idiom as test_admin_data_routes."""
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
POWER = "power@x.com"
POWER2 = "power2@x.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store.AuthStore().ensure_user(ADMIN)
    local_store.AuthStore().set_role(ADMIN, "admin")

    import routes.admin_data as admin_mod

    def fake_autofill(**kw):
        return {"file_description": "AI table desc",
                "columns": {"a": "AI col a", "b": "AI col b"}}

    monkeypatch.setattr(admin_mod.brain_client, "schema_autofill", fake_autofill)

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(admin_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session.pop("must_change_password", None)
        return {"ok": True}

    return TestClient(app)


def _mk_sqlite_conn(tmp_path, name, dbfile, tables=("t",)):
    """A saved sqlite connection whose source db holds the given tables
    (each with columns a INTEGER pk / b TEXT)."""
    from sqlalchemy import create_engine, text
    db = tmp_path / dbfile
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        for t in tables:
            conn.execute(text(f"CREATE TABLE {t} (a INTEGER PRIMARY KEY, b TEXT)"))
            conn.execute(text(f"INSERT INTO {t} VALUES (1, 'x'), (2, 'y')"))
    eng.dispose()
    masked = db_sources.DataSourceStore().create_connection(
        {"name": name, "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor=ADMIN)
    return masked["id"]


@pytest.fixture
def two_conns(client, tmp_path):
    """cid1 (granted below) with tables t+u, cid2 (never granted) with v."""
    cid1 = _mk_sqlite_conn(tmp_path, "S1", "src1.db", tables=("t", "u"))
    cid2 = _mk_sqlite_conn(tmp_path, "S2", "src2.db", tables=("v",))
    return cid1, cid2


def _grant_power(email, grants, name=None):
    """19f: the power PERMISSION on the user + a role whose MANAGE grants
    are the WHERE (scope_grants are the separate read axis)."""
    role = roles_store.RolesStore().create_role(
        {"name": name or f"PU {email}", "manage_grants": grants}, actor=ADMIN)
    auth = local_store.AuthStore()
    auth.ensure_user(email)
    auth.set_role(email, "power")
    auth.set_data_role(email, role["id"])
    return role


def _table_body(cid, table="t", schema="", display=None, cols=None, **extra):
    body = {"connection_id": cid, "schema": schema, "table_name": table,
            "display_name": display or f"{table} table", "description": "d",
            "columns": cols if cols is not None else [
                {"name": "a", "dtype": "INTEGER", "description": "col a",
                 "indexed": True, "pk": True},
                {"name": "b", "dtype": "TEXT", "description": "col b",
                 "indexed": False}],
            "is_connector": False, "relations": [], "confirm": True}
    body.update(extra)
    return body


# ── registration + ownership ───────────────────────────────────────────────

def test_in_scope_register_stamps_registered_by(client, two_conns):
    cid1, _ = two_conns
    _grant_power(POWER, [{"connection_id": cid1}])
    client.post(f"/_login/{POWER}")
    r = client.post("/api/admin/tables", json=_table_body(cid1))
    assert r.status_code == 201, r.json()
    out = r.json()
    assert out["snapshot"]["ok"] is True
    assert out["table"]["registered_by"] == POWER
    # The stored doc carries it too.
    doc = db_sources.DataSourceStore().get_table(out["table"]["id"])
    assert doc["registered_by"] == POWER


def test_out_of_scope_register_403(client, two_conns):
    _, cid2 = two_conns
    _grant_power(POWER, [{"connection_id": two_conns[0]}])
    client.post(f"/_login/{POWER}")
    r = client.post("/api/admin/tables", json=_table_body(cid2, table="v"))
    assert r.status_code == 403
    assert r.json()["code"] == "OUT_OF_SCOPE"
    assert db_sources.DataSourceStore().list_tables() == []


def test_access_role_ids_subset_of_held_roles(client, two_conns):
    """19f publish+share: a power user may share only with roles they HOLD —
    an outside id is a 403 up-front (nothing registered), a held id is
    reconciled into the role."""
    cid1, _ = two_conns
    pu_role = _grant_power(POWER, [{"connection_id": cid1}])
    other_role = roles_store.RolesStore().create_role({"name": "Readers"},
                                                      actor=ADMIN)
    client.post(f"/_login/{POWER}")
    r = client.post("/api/admin/tables",
                    json=_table_body(cid1, access_role_ids=[other_role["id"]]))
    assert r.status_code == 403
    assert r.json()["code"] == "ROLE_NOT_HELD"
    assert db_sources.DataSourceStore().list_tables() == []   # nothing saved
    # A HELD role is accepted and reconciled.
    r = client.post("/api/admin/tables",
                    json=_table_body(cid1, access_role_ids=[pu_role["id"]]))
    assert r.status_code == 201, r.json()
    tid = r.json()["table"]["id"]
    assert tid in roles_store.RolesStore().get_role(pu_role["id"])["table_ids"]
    assert tid not in roles_store.RolesStore().get_role(other_role["id"])["table_ids"]


def test_register_without_read_grant_visible_only_to_owner(client, two_conns):
    """19f: manage grant WITHOUT any read grant — registration succeeds, the
    table reaches the registerer's chat picker via the ownership read but NOT
    another member of the same role."""
    cid1, _ = two_conns
    role = _grant_power(POWER, [{"connection_id": cid1}])
    member = "member@x.com"
    local_store.AuthStore().ensure_user(member)
    local_store.AuthStore().set_data_role(member, role["id"])
    client.post(f"/_login/{POWER}")
    r = client.post("/api/admin/tables", json=_table_body(cid1))
    assert r.status_code == 201, r.json()
    tid = r.json()["table"]["id"]
    assert tid in roles_store.allowed_table_ids_for(POWER)       # owner
    assert tid not in roles_store.allowed_table_ids_for(member)  # not shared
    # Sharing with the held role exposes it to the member too.
    r = client.post(f"/api/admin/tables/{tid}",
                    json=_table_body(cid1, access_role_ids=[role["id"]]))
    assert r.status_code == 200, r.json()
    assert tid in roles_store.allowed_table_ids_for(member)


def test_my_roles_returns_held_roles_only(client, two_conns):
    cid1, _ = two_conns
    role = _grant_power(POWER, [{"connection_id": cid1}])
    roles_store.RolesStore().create_role({"name": "Unheld"}, actor=ADMIN)
    client.post(f"/_login/{POWER}")
    rows = client.get("/api/admin/my_roles").json()["roles"]
    assert [r["id"] for r in rows] == [role["id"]]
    assert set(rows[0]) >= {"id", "name", "is_base", "table_ids", "scope_grants"}


def test_base_role_never_shareable_by_power_user(client, two_conns):
    """Base = everyone: it is excluded from my_roles even when it is the
    user's (fallback) role, and save_table refuses it — publishing to the
    whole platform stays ladmin's action."""
    cid1, _ = two_conns
    roles_store.RolesStore().ensure_base_role()
    # Power permission with NO custom roles → held resolves to [Base].
    auth = local_store.AuthStore()
    auth.ensure_user(POWER)
    auth.set_role(POWER, "power")
    role = roles_store.RolesStore().create_role(
        {"name": "MG", "manage_grants": [{"connection_id": cid1}]}, actor=ADMIN)
    auth.set_data_roles(POWER, [role["id"]])
    client.post(f"/_login/{POWER}")
    rows = client.get("/api/admin/my_roles").json()["roles"]
    assert "base" not in [r["id"] for r in rows]
    r = client.post("/api/admin/tables",
                    json=_table_body(cid1, access_role_ids=["base"]))
    assert r.status_code == 403 and r.json()["code"] == "ROLE_NOT_HELD"
    assert db_sources.DataSourceStore().list_tables() == []


def test_pu_edit_preserves_unheld_roles_membership(client, two_conns):
    """set_table_roles is an exact reconcile — a power user's edit-save must
    NOT strip an unheld role's ladmin-granted access (their reconcile is
    limited to the held subset)."""
    cid1, _ = two_conns
    held = _grant_power(POWER, [{"connection_id": cid1}])
    rs = roles_store.RolesStore()
    unheld = rs.create_role({"name": "Readers"}, actor=ADMIN)
    client.post(f"/_login/{POWER}")
    tid = client.post("/api/admin/tables",
                      json=_table_body(cid1)).json()["table"]["id"]
    # Ladmin grants the table to a role the PU does not hold.
    rs.set_table_roles(tid, [unheld["id"]], actor=ADMIN)
    # PU edit-save sharing with their held role: the unheld grant survives.
    r = client.post(f"/api/admin/tables/{tid}",
                    json=_table_body(cid1, access_role_ids=[held["id"]]))
    assert r.status_code == 200, r.json()
    assert tid in rs.get_role(unheld["id"])["table_ids"]
    assert tid in rs.get_role(held["id"])["table_ids"]
    # PU un-sharing their held role removes ONLY that role's membership.
    r = client.post(f"/api/admin/tables/{tid}",
                    json=_table_body(cid1, access_role_ids=[]))
    assert r.status_code == 200
    assert tid not in rs.get_role(held["id"])["table_ids"]
    assert tid in rs.get_role(unheld["id"])["table_ids"]
    # Ladmin's reconcile stays EXACT (unchanged semantics).
    client.post(f"/_login/{ADMIN}")
    r = client.post(f"/api/admin/tables/{tid}",
                    json=_table_body(cid1, access_role_ids=[]))
    assert r.status_code == 200
    assert tid not in rs.get_role(unheld["id"])["table_ids"]


def test_ladmin_edit_save_keeps_registered_by(client, two_conns):
    cid1, _ = two_conns
    _grant_power(POWER, [{"connection_id": cid1}])
    client.post(f"/_login/{POWER}")
    tid = client.post("/api/admin/tables",
                      json=_table_body(cid1)).json()["table"]["id"]
    client.post(f"/_login/{ADMIN}")
    r = client.post(f"/api/admin/tables/{tid}",
                    json=_table_body(cid1, display="renamed by ladmin"))
    assert r.status_code == 200
    assert r.json()["table"]["registered_by"] == POWER   # edit never re-stamps


# ── filtered listings ──────────────────────────────────────────────────────

def test_tables_and_connections_filtered_to_scope(client, two_conns):
    cid1, cid2 = two_conns
    client.post(f"/_login/{ADMIN}")
    assert client.post("/api/admin/tables",
                       json=_table_body(cid1)).status_code == 201
    assert client.post("/api/admin/tables",
                       json=_table_body(cid2, table="v",
                                        display="v table")).status_code == 201
    _grant_power(POWER, [{"connection_id": cid1}])
    client.post(f"/_login/{POWER}")
    tables = client.get("/api/admin/tables").json()["tables"]
    assert [t["connection_id"] for t in tables] == [cid1]
    conns = client.get("/api/admin/connections").json()["connections"]
    assert [c["id"] for c in conns] == [cid1]
    assert conns[0]["table_count"] == 1
    # Browsing the out-of-scope connection is refused outright.
    assert client.get(f"/api/admin/connections/{cid2}/tables").status_code == 403
    assert client.get(f"/api/admin/connections/{cid2}/schemas").status_code == 403


def test_schema_level_grant_filters_schema_listing(client, two_conns):
    cid1, _ = two_conns
    _grant_power(POWER, [{"connection_id": cid1, "schema": "main"}])
    client.post(f"/_login/{POWER}")
    res = client.get(f"/api/admin/connections/{cid1}/schemas").json()
    assert res["ok"] is True and res["schemas"] == ["main"]
    # A grant on a schema this connection does not have → empty listing.
    _grant_power(POWER2, [{"connection_id": cid1, "schema": "other"}])
    client.post(f"/_login/{POWER2}")
    res = client.get(f"/api/admin/connections/{cid1}/schemas").json()
    assert res["ok"] is True and res["schemas"] == []
    assert res["default_schema"] is None


def test_introspect_out_of_scope_403(client, two_conns):
    cid1, cid2 = two_conns
    _grant_power(POWER, [{"connection_id": cid1}])
    client.post(f"/_login/{POWER}")
    ok = client.post("/api/admin/tables/introspect",
                     json={"connection_id": cid1, "table": "t"})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    bad = client.post("/api/admin/tables/introspect",
                      json={"connection_id": cid2, "table": "v"})
    assert bad.status_code == 403 and bad.json()["code"] == "OUT_OF_SCOPE"
    # The unsaved-draft path (no connection_id) is inherently out of scope.
    draft = client.post("/api/admin/tables/introspect",
                        json={"db_type": "sqlite", "table": "t"})
    assert draft.status_code == 403


# ── relations ──────────────────────────────────────────────────────────────

def test_relation_accept_with_one_side_out_of_scope_403(client, two_conns):
    cid1, cid2 = two_conns
    client.post(f"/_login/{ADMIN}")
    a = client.post("/api/admin/tables",
                    json=_table_body(cid1)).json()["table"]["id"]
    b = client.post("/api/admin/tables",
                    json=_table_body(cid2, table="v",
                                     display="v table")).json()["table"]["id"]
    _grant_power(POWER, [{"connection_id": cid1}])
    client.post(f"/_login/{POWER}")
    r = client.post("/api/admin/relations/accept", json={"relations": [
        {"table_id": a, "related_table_id": b, "join_keys": [["a", "a"]]}]})
    assert r.status_code == 403 and r.json()["code"] == "OUT_OF_SCOPE"
    assert not db_sources.DataSourceStore().get_table(a)["relations"]
    # Both sides in scope → accepted.
    c = client.post("/api/admin/tables",
                    json=_table_body(cid1, table="u",
                                     display="u table")).json()["table"]["id"]
    ok = client.post("/api/admin/relations/accept", json={"relations": [
        {"table_id": a, "related_table_id": c, "join_keys": [["a", "a"]]}]})
    assert ok.status_code == 200 and ok.json()["accepted"] == 1


def test_pu_analyze_sql_never_persists_out_of_scope_recommendations(
        client, tmp_path):
    """The WRITE is scope-bounded too (reviewer finding): a power user's
    Analyze-SQL must not create a persistent recommendation for a physical
    table outside their management scope — even when the name resolves."""
    cid = _mk_sqlite_conn(tmp_path, "S1", "src1.db")
    client.post(f"/_login/{ADMIN}")
    assert client.post("/api/admin/tables",
                       json=_table_body(cid)).status_code == 201
    # Power user with an EMPTY scope: with one connection in the store the
    # unknown name resolves unambiguously, so pre-fix this persisted a rec.
    _grant_power(POWER, [])
    client.post(f"/_login/{POWER}")
    r = client.post("/api/admin/relations/analyze_sql", json={
        "db_type": "sqlite",
        "sql": "SELECT 1 FROM t JOIN regions r ON t.a = r.region_id"})
    assert r.status_code == 200
    assert db_sources.DataSourceStore().list_recommendations() == []
    # Ladmin behavior unchanged: the same paste DOES persist the rec.
    client.post(f"/_login/{ADMIN}")
    r = client.post("/api/admin/relations/analyze_sql", json={
        "db_type": "sqlite",
        "sql": "SELECT 1 FROM t JOIN regions r ON t.a = r.region_id"})
    assert r.status_code == 200
    recs = db_sources.DataSourceStore().list_recommendations()
    assert [rec["table"] for rec in recs] == ["regions"]


def test_permission_stacked_on_access_roles(client, two_conns):
    """The 19e/19f model end-to-end: capability from the per-user POWER
    permission, READ reach from a table_ids role, and the MANAGEMENT scope
    from a second held role's schema MANAGE grant — registration succeeds
    inside that role's schema."""
    cid1, _ = two_conns
    client.post(f"/_login/{ADMIN}")
    read_tid = client.post("/api/admin/tables",
                           json=_table_body(cid1)).json()["table"]["id"]
    store = roles_store.RolesStore()
    read_role = store.create_role({"name": "Read", "table_ids": [read_tid]},
                                  actor=ADMIN)
    scope_role = store.create_role({"name": "Scope", "manage_grants": [
        {"connection_id": cid1, "schema": "main"}]}, actor=ADMIN)
    auth = local_store.AuthStore()
    auth.ensure_user(POWER)
    auth.set_role(POWER, "power")
    auth.set_data_roles(POWER, [read_role["id"], scope_role["id"]])
    client.post(f"/_login/{POWER}")
    r = client.post("/api/admin/tables",
                    json=_table_body(cid1, table="u", schema="main",
                                     display="u table"))
    assert r.status_code == 201, r.json()
    assert r.json()["table"]["registered_by"] == POWER
    # The management listing shows only the schema-scoped registration: the
    # read role's table lives at schema "" ≠ "main", so it stays READABLE
    # (table_ids) but is outside the MANAGEMENT scope.
    tables = client.get("/api/admin/tables").json()["tables"]
    assert [t["schema"] for t in tables] == ["main"]
    # Same roles at STANDARD permission → guard 403 everywhere on /power APIs.
    auth.set_role(POWER, "user")
    assert client.get("/api/admin/tables").status_code == 403
    assert client.post("/api/admin/tables", json=_table_body(
        cid1, table="u", schema="main")).status_code == 403


# ── refresh + schedule ─────────────────────────────────────────────────────

def test_refresh_and_schedule_scope(client, two_conns):
    cid1, cid2 = two_conns
    client.post(f"/_login/{ADMIN}")
    tid1 = client.post("/api/admin/tables",
                       json=_table_body(cid1)).json()["table"]["id"]
    tid2 = client.post("/api/admin/tables",
                       json=_table_body(cid2, table="v",
                                        display="v table")).json()["table"]["id"]
    _grant_power(POWER, [{"connection_id": cid1}])
    client.post(f"/_login/{POWER}")
    assert client.post(f"/api/admin/tables/{tid1}/refresh",
                       json={}).json()["ok"] is True
    assert client.post(f"/api/admin/tables/{tid2}/refresh",
                       json={}).status_code == 403
    ok = client.post(f"/api/admin/tables/{tid1}/schedule",
                     json={"schedule": None})
    assert ok.status_code == 200 and ok.json()["ok"] is True
    assert client.post(f"/api/admin/tables/{tid2}/schedule",
                       json={"schedule": None}).status_code == 403


# ── delete ownership matrix ────────────────────────────────────────────────

def test_delete_matrix(client, two_conns):
    cid1, cid2 = two_conns
    store = db_sources.DataSourceStore()
    # Ladmin registers "t" on cid1; POWER registers "u" on cid1;
    # POWER2 (scoped to cid2) registers "v" on cid2.
    client.post(f"/_login/{ADMIN}")
    ladmin_tid = client.post("/api/admin/tables",
                             json=_table_body(cid1)).json()["table"]["id"]
    _grant_power(POWER, [{"connection_id": cid1}])
    _grant_power(POWER2, [{"connection_id": cid2}])
    client.post(f"/_login/{POWER}")
    own_tid = client.post("/api/admin/tables",
                          json=_table_body(cid1, table="u",
                                           display="u table")).json()["table"]["id"]
    client.post(f"/_login/{POWER2}")
    other_tid = client.post("/api/admin/tables",
                            json=_table_body(cid2, table="v",
                                             display="v table")).json()["table"]["id"]
    client.post(f"/_login/{POWER}")
    # Ladmin-registered, in scope → NOT_OWNER.
    r = client.post(f"/api/admin/tables/{ladmin_tid}/delete", json={})
    assert r.status_code == 403 and r.json()["code"] == "NOT_OWNER"
    # Another power user's table, outside scope → OUT_OF_SCOPE.
    r = client.post(f"/api/admin/tables/{other_tid}/delete", json={})
    assert r.status_code == 403 and r.json()["code"] == "OUT_OF_SCOPE"
    # Legacy doc with no registered_by (pre-feature) → NOT_OWNER.
    legacy = store.get_table(ladmin_tid)
    legacy.pop("registered_by", None)
    store.upsert_table(legacy, actor=ADMIN)
    r = client.post(f"/api/admin/tables/{ladmin_tid}/delete", json={})
    assert r.status_code == 403 and r.json()["code"] == "NOT_OWNER"
    # Own in-scope table → deleted, snapshot dropped.
    snap = local_store.db_snapshot_path(own_tid)
    assert snap.exists()
    r = client.post(f"/api/admin/tables/{own_tid}/delete", json={})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert store.get_table(own_tid) is None
    assert not snap.exists()
    # Ladmin still deletes anything, unchanged.
    client.post(f"/_login/{ADMIN}")
    assert client.post(f"/api/admin/tables/{ladmin_tid}/delete",
                       json={}).status_code == 200


# ── audit labeling ─────────────────────────────────────────────────────────

def test_power_user_writes_audited_with_actor_kind(client, two_conns):
    cid1, _ = two_conns
    _grant_power(POWER, [{"connection_id": cid1}])
    client.post(f"/_login/{POWER}")
    assert client.post("/api/admin/tables",
                       json=_table_body(cid1)).status_code == 201
    rows = db_sources.read_audit_tail(100)
    by_action = {}
    for r in rows:
        by_action.setdefault(r["action"], r)
    for action in ("table.save", "table.refresh"):
        row = by_action[action]
        assert row["actor"] == POWER
        assert row["detail"]["actor_kind"] == "power_user", (action, row)
    # Ladmin rows stay unlabeled (byte-identical to before the feature).
    client.post(f"/_login/{ADMIN}")
    client.post("/api/admin/tables",
                json=_table_body(cid1, table="u", display="u table"))
    latest_save = next(r for r in db_sources.read_audit_tail(100)
                       if r["action"] == "table.save")
    assert latest_save["actor"] == ADMIN
    assert "actor_kind" not in (latest_save.get("detail") or {})
