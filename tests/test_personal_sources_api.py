"""Personal (self-service) database connections — the /api/admin/* surface.

Pins section 2.3–2.6 of the personal-connections task: the guard resolves
`roles_store.manager_scope_for` (power grants ∪ owned connections), the
connection lifecycle routes accept a self-service owner (ladmin keeps
non-personal connections, never touches personal ones: PERSONAL_OWNER_ONLY),
the table rules on a personal connection (no roles, no connector, the
per-user cap), the relation rule (both sides on the same owner's
connections), the non-admin test-connection rate limit + audit, the
actor_kind "personal" label, the role-grant refusal and the Users-window
switch. Offline through the hidden sqlite dialect (connections created at
store level, like test_power_user_routes)."""
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
OWNER = "owner@x.com"
OTHER = "other@x.com"
POWER = "power@x.com"
ADMIN2 = "boss@x.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN, raising=False)
    auth = local_store.AuthStore()
    auth.ensure_user(ADMIN)
    auth.set_role(ADMIN, "admin")
    auth.ensure_user(ADMIN2)
    auth.set_role(ADMIN2, "admin")
    auth.ensure_user(OWNER)
    auth.ensure_user(OTHER)

    import routes.admin_data as admin_mod
    import routes.admin_users as users_mod
    admin_mod._reset_test_limiter()

    monkeypatch.setattr(admin_mod.brain_client, "schema_autofill",
                        lambda **kw: {"file_description": "AI desc",
                                      "columns": {"a": "AI a", "b": "AI b"}})

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(admin_mod.router)
    app.include_router(users_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session.pop("must_change_password", None)
        return {"ok": True}

    yield TestClient(app)
    admin_mod._reset_test_limiter()


def _login(client, email):
    client.post(f"/_login/{email}")


def _sqlite_db(tmp_path, dbfile, tables=("t", "u")):
    from sqlalchemy import create_engine, text
    db = tmp_path / dbfile
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        for t in tables:
            conn.execute(text(f"CREATE TABLE {t} (a INTEGER PRIMARY KEY, b TEXT)"))
            conn.execute(text(f"INSERT INTO {t} VALUES (1, 'x'), (2, 'y')"))
    eng.dispose()
    return db


def _mk_conn(tmp_path, name, dbfile, owner=None, tables=("t", "u")):
    db = _sqlite_db(tmp_path, dbfile, tables)
    kw = {"owner": owner} if owner else {}
    masked = db_sources.DataSourceStore().create_connection(
        {"name": name, "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor=owner or ADMIN, **kw)
    return masked["id"]


@pytest.fixture
def conns(client, tmp_path):
    """mine: OWNER's personal connection (tables t, u, v, w); theirs:
    OTHER's personal connection; lad: ladmin's connection (tables t, u)."""
    return {
        "mine": _mk_conn(tmp_path, "Mine", "mine.db", owner=OWNER,
                         tables=("t", "u", "v", "w")),
        "theirs": _mk_conn(tmp_path, "Theirs", "theirs.db", owner=OTHER),
        "lad": _mk_conn(tmp_path, "Lad", "lad.db"),
    }


def _body(cid, table="t", **extra):
    body = {"connection_id": cid, "schema": "", "table_name": table,
            "display_name": f"{table} on {cid[:4]}", "description": "d",
            "columns": [
                {"name": "a", "dtype": "INTEGER", "description": "col a",
                 "indexed": True, "pk": True},
                {"name": "b", "dtype": "TEXT", "description": "col b",
                 "indexed": False}],
            "is_connector": False, "relations": [], "confirm": True}
    body.update(extra)
    return body


def _register(client, cid, table="t", **extra):
    r = client.post("/api/admin/tables", json=_body(cid, table, **extra))
    assert r.status_code == 201, r.json()
    return r.json()["table"]


def _store_table(cid, table, registered_by=None):
    doc = {"connection_id": cid, "schema": "", "table_name": table,
           "display_name": f"{table}-{cid[:4]}", "description": "",
           "is_connector": False, "relations": [],
           "columns": [{"name": "a", "dtype": "INTEGER"},
                       {"name": "b", "dtype": "TEXT"}]}
    if registered_by:
        doc["registered_by"] = registered_by
    return db_sources.DataSourceStore().upsert_table(doc, actor=ADMIN)


def _grant_power(email, grants):
    role = roles_store.RolesStore().create_role(
        {"name": f"PU {email}", "manage_grants": grants}, actor=ADMIN)
    auth = local_store.AuthStore()
    auth.ensure_user(email)
    auth.set_role(email, "power")
    auth.set_data_role(email, role["id"])
    return role


def _audit(action):
    return [r for r in db_sources.read_audit_tail(1000) if r["action"] == action]


def _pg_body(**extra):
    body = {"name": "My PG", "db_type": "postgresql", "host": "db.local",
            "port": 5432, "database": "d", "user": "reader", "password": "s3cret"}
    body.update(extra)
    return body


# ── guard ───────────────────────────────────────────────────────────────────

def test_self_service_user_passes_the_guard_and_creates(client):
    _login(client, OWNER)
    r = client.get("/api/admin/connections")
    assert r.status_code == 200 and r.json()["connections"] == []
    r = client.post("/api/admin/connections", json=_pg_body())
    assert r.status_code == 201, r.json()
    assert r.json()["connection"]["owner"] == OWNER
    assert not _audit("admin.denied")


def test_switched_off_user_is_refused_with_reason(client):
    local_store.AuthStore().set_self_service(OWNER, False)
    _login(client, OWNER)
    r = client.get("/api/admin/connections")
    assert r.status_code == 403
    rows = _audit("admin.denied")
    assert len(rows) == 1
    assert rows[0]["detail"]["reason"] == "self_service_disabled"
    # A user who is neither power nor self-service keeps the old reason only
    # when the switch is on but the capability is unavailable — not the case
    # for a standard user, whose switch is the only gate.
    r = client.post("/api/admin/connections", json=_pg_body())
    assert r.status_code == 403
    assert len(_audit("admin.denied")) == 2


def test_create_refused_for_switched_off_power_user(client, conns):
    _grant_power(POWER, [{"connection_id": conns["lad"], "schema": None}])
    local_store.AuthStore().set_self_service(POWER, False)
    _login(client, POWER)
    # The power user still reaches the guard (power grants)...
    assert client.get("/api/admin/connections").status_code == 200
    # ...but cannot create a personal connection.
    r = client.post("/api/admin/connections", json=_pg_body())
    assert r.status_code == 403
    assert r.json()["code"] == "SELF_SERVICE_DISABLED"
    assert not _audit("admin.denied")


def test_admin_permission_user_creates_admin_connection_without_owner(client):
    """Sandro, 2026-11-03: an admin-permission user with the switch ON still
    creates an administrator connection — no owner, never personal."""
    assert local_store.AuthStore().get_self_service(ADMIN2) is True
    _login(client, ADMIN2)
    r = client.post("/api/admin/connections", json=_pg_body())
    assert r.status_code == 201
    conn = r.json()["connection"]
    assert "owner" not in conn
    row = next(c for c in client.get("/api/admin/connections").json()["connections"]
               if c["id"] == conn["id"])
    assert row["personal"] is False and row.get("owner") is None
    assert db_sources.DataSourceStore().personal_connection_ids() == set()
    rows = _audit("connection.create")
    assert "actor_kind" not in (rows[0].get("detail") or {})


def test_user_create_drops_url_override_and_refuses_hidden_dialect(client):
    _login(client, OWNER)
    r = client.post("/api/admin/connections",
                    json=_pg_body(url_override="sqlite+pysqlite:////etc/x.db"))
    assert r.status_code == 201
    raw = db_sources.DataSourceStore().get_connection(
        r.json()["connection"]["id"], with_secret=True)
    assert raw["url_override"] is None
    r = client.post("/api/admin/connections",
                    json=_pg_body(db_type="sqlite"))
    assert r.status_code == 400
    rows = _audit("connection.create")
    assert rows[0]["detail"]["actor_kind"] == "personal"


# ── test-connection ─────────────────────────────────────────────────────────

@pytest.fixture
def fake_probe(monkeypatch):
    import routes.admin_data as admin_mod
    calls = []

    def probe(cfg, password, sid=None):
        calls.append(dict(cfg))
        return {"ok": True}

    monkeypatch.setattr(admin_mod.db_connector, "test_connection", probe)
    return calls


def test_test_connection_ownership_rules(client, conns, fake_probe):
    _login(client, OWNER)
    assert client.post("/api/admin/connections/test",
                       json={"connection_id": conns["mine"]}).json()["ok"] is True
    r = client.post("/api/admin/connections/test",
                    json={"connection_id": conns["lad"]})
    assert r.status_code == 403
    r = client.post("/api/admin/connections/test",
                    json={"connection_id": conns["theirs"]})
    assert r.status_code == 403
    # The draft form works for a self-service user; url_override is dropped
    # and a hidden dialect refused.
    r = client.post("/api/admin/connections/test",
                    json=_pg_body(url_override="sqlite+pysqlite:///x.db"))
    assert r.status_code == 200 and r.json()["ok"] is True
    assert fake_probe[-1]["url_override"] is None
    assert client.post("/api/admin/connections/test",
                       json=_pg_body(db_type="sqlite")).status_code == 400
    # Admin may test ladmin's connection, never a personal one.
    _login(client, ADMIN)
    assert client.post("/api/admin/connections/test",
                       json={"connection_id": conns["lad"]}).json()["ok"] is True
    r = client.post("/api/admin/connections/test",
                    json={"connection_id": conns["mine"]})
    assert r.status_code == 403 and r.json()["code"] == "PERSONAL_OWNER_ONLY"


def test_test_connection_rate_limit_and_audit(client, fake_probe):
    _login(client, OWNER)
    for _ in range(10):
        assert client.post("/api/admin/connections/test",
                           json=_pg_body()).status_code == 200
    r = client.post("/api/admin/connections/test", json=_pg_body())
    assert r.status_code == 429
    assert r.json()["code"] == "TOO_MANY_TESTS"
    assert int(r.headers["Retry-After"]) >= 1
    rows = [x for x in _audit("connection.test") if x["actor"] == OWNER]
    assert len(rows) == 10
    for x in rows:
        d = x["detail"]
        assert d["db_type"] == "postgresql" and d["host"] == "db.local"
        assert d["port"] == 5432 and d["ok"] is True
        assert "user" not in d and "password" not in d
        assert "reader" not in str(x) and "s3cret" not in str(x)
    # The admin is never limited.
    _login(client, ADMIN)
    for _ in range(12):
        assert client.post("/api/admin/connections/test",
                           json=_pg_body()).status_code == 200


def test_test_limiter_window_expires(client, fake_probe, monkeypatch):
    import routes.admin_data as admin_mod
    now = [1000.0]
    monkeypatch.setattr(admin_mod, "_test_clock", lambda: now[0])
    monkeypatch.setattr(admin_mod, "_TEST_LIMIT", 2)
    _login(client, OWNER)
    assert client.post("/api/admin/connections/test", json=_pg_body()).status_code == 200
    assert client.post("/api/admin/connections/test", json=_pg_body()).status_code == 200
    assert client.post("/api/admin/connections/test", json=_pg_body()).status_code == 429
    now[0] += admin_mod._TEST_WINDOW_S + 1
    assert client.post("/api/admin/connections/test", json=_pg_body()).status_code == 200


# ── edit / delete / refresh ─────────────────────────────────────────────────

def test_connection_lifecycle_owner_only(client, conns):
    cid = conns["mine"]
    _login(client, OWNER)
    r = client.post(f"/api/admin/connections/{cid}", json={"name": "Renamed"})
    assert r.status_code == 200 and r.json()["connection"]["name"] == "Renamed"
    assert client.post(f"/api/admin/connections/{cid}/refresh",
                       json={}).status_code == 200
    # Another user: outside their scope.
    _login(client, OTHER)
    for path in (f"/api/admin/connections/{cid}",
                 f"/api/admin/connections/{cid}/delete",
                 f"/api/admin/connections/{cid}/refresh"):
        r = client.post(path, json={"name": "x"})
        assert r.status_code == 403 and r.json()["code"] == "OUT_OF_SCOPE", path
    # Admin: personal connections are owner-only.
    _login(client, ADMIN)
    for path in (f"/api/admin/connections/{cid}",
                 f"/api/admin/connections/{cid}/delete",
                 f"/api/admin/connections/{cid}/refresh"):
        r = client.post(path, json={"name": "x"})
        assert r.status_code == 403 and r.json()["code"] == "PERSONAL_OWNER_ONLY", path
    assert db_sources.DataSourceStore().get_connection(cid)["name"] == "Renamed"
    _login(client, OWNER)
    assert client.post(f"/api/admin/connections/{cid}/delete",
                       json={}).status_code == 200
    assert db_sources.DataSourceStore().get_connection(cid) is None
    kinds = {r["detail"].get("actor_kind") for r in
             _audit("connection.update") + _audit("connection.delete")}
    assert kinds == {"personal"}


def test_non_admin_cannot_change_ladmins_connection(client, conns):
    _grant_power(POWER, [{"connection_id": conns["lad"], "schema": None}])
    _login(client, POWER)
    for path in (f"/api/admin/connections/{conns['lad']}",
                 f"/api/admin/connections/{conns['lad']}/delete",
                 f"/api/admin/connections/{conns['lad']}/refresh"):
        r = client.post(path, json={"name": "x"})
        assert r.status_code == 403, path
    assert db_sources.DataSourceStore().get_connection(conns["lad"])["name"] == "Lad"
    # Ladmin keeps full control of its own connection.
    _login(client, ADMIN)
    assert client.post(f"/api/admin/connections/{conns['lad']}",
                       json={"name": "Lad2"}).status_code == 200


def test_connection_listing_flags(client, conns):
    _login(client, ADMIN)
    rows = {c["id"]: c for c in client.get("/api/admin/connections").json()["connections"]}
    assert rows[conns["mine"]]["owner"] == OWNER and rows[conns["mine"]]["personal"] is True
    assert rows[conns["lad"]]["personal"] is False
    _login(client, OWNER)
    rows = {c["id"]: c for c in client.get("/api/admin/connections").json()["connections"]}
    assert set(rows) == {conns["mine"]}
    assert rows[conns["mine"]]["owned"] is True and rows[conns["mine"]]["personal"] is True
    # A power user sees owned + granted rows, flagged apart.
    _grant_power(OTHER, [{"connection_id": conns["lad"], "schema": None}])
    _login(client, OTHER)
    rows = {c["id"]: c for c in client.get("/api/admin/connections").json()["connections"]}
    assert set(rows) == {conns["theirs"], conns["lad"]}
    assert rows[conns["theirs"]]["owned"] is True
    assert rows[conns["lad"]]["owned"] is False and rows[conns["lad"]]["personal"] is False


# ── tables on a personal connection ─────────────────────────────────────────

def test_personal_table_registration_rules(client, conns):
    _login(client, OWNER)
    t = _register(client, conns["mine"], "t", access_role_ids=[])
    assert t["registered_by"] == OWNER and t["is_connector"] is False
    role = roles_store.RolesStore().create_role({"name": "R"}, actor=ADMIN)
    r = client.post("/api/admin/tables",
                    json=_body(conns["mine"], "u", access_role_ids=[role["id"]]))
    assert r.status_code == 400 and r.json()["code"] == "PERSONAL_NO_ROLES"
    r = client.post("/api/admin/tables",
                    json=_body(conns["mine"], "u", is_connector=True))
    assert r.status_code == 400 and r.json()["code"] == "PERSONAL_NO_CONNECTOR"
    assert len(db_sources.DataSourceStore().list_tables()) == 1
    rows = _audit("table.save")
    assert rows[0]["detail"]["actor_kind"] == "personal"
    assert not _audit("role.set_tables")


def test_personal_table_cap(client, conns, monkeypatch):
    monkeypatch.setattr(settings, "PERSONAL_TABLES_MAX", 2)
    _login(client, OWNER)
    t = _register(client, conns["mine"], "t")
    _register(client, conns["mine"], "u")
    r = client.post("/api/admin/tables", json=_body(conns["mine"], "v"))
    assert r.status_code == 400
    assert r.json() == {"error": r.json()["error"], "code": "PERSONAL_TABLE_CAP", "max": 2}
    # An edit of an existing personal table still passes.
    r = client.post(f"/api/admin/tables/{t['id']}",
                    json=_body(conns["mine"], "t", description="edited"))
    assert r.status_code == 200, r.json()
    # Another user's cap is their own.
    _login(client, OTHER)
    _register(client, conns["theirs"], "t")


def test_admin_cannot_write_personal_tables_owner_can(client, conns):
    _login(client, OWNER)
    t = _register(client, conns["mine"], "t")
    tid = t["id"]
    _login(client, ADMIN)
    calls = [
        ("/api/admin/tables/" + tid, _body(conns["mine"], "t", description="x")),
        (f"/api/admin/tables/{tid}/refresh", {}),
        (f"/api/admin/tables/{tid}/mode", {"mode": "live"}),
        (f"/api/admin/tables/{tid}/schedule", {"schedule": None}),
        (f"/api/admin/tables/{tid}/dismiss_drift", {}),
        (f"/api/admin/tables/{tid}/delete", {}),
    ]
    for path, body in calls:
        r = client.post(path, json=body)
        assert r.status_code == 403 and r.json()["code"] == "PERSONAL_OWNER_ONLY", path
    # A NEW registration by the admin on a personal connection is refused too.
    r = client.post("/api/admin/tables", json=_body(conns["mine"], "u"))
    assert r.status_code == 403 and r.json()["code"] == "PERSONAL_OWNER_ONLY"
    _login(client, OWNER)
    assert client.post(f"/api/admin/tables/{tid}",
                       json=_body(conns["mine"], "t", description="x")).status_code == 200
    assert client.post(f"/api/admin/tables/{tid}/refresh", json={}).json()["ok"] is True
    assert client.post(f"/api/admin/tables/{tid}/schedule",
                       json={"schedule": None}).status_code == 200
    assert client.post(f"/api/admin/tables/{tid}/dismiss_drift",
                       json={}).status_code == 404       # no drift recorded
    assert client.post(f"/api/admin/tables/{tid}/delete", json={}).status_code == 200
    # Another user never reaches the owner's table.
    _login(client, OWNER)
    t2 = _register(client, conns["mine"], "u")
    _login(client, OTHER)
    r = client.post(f"/api/admin/tables/{t2['id']}/delete", json={})
    assert r.status_code == 403 and r.json()["code"] == "OUT_OF_SCOPE"


def test_personal_relations_same_owner_only(client, conns):
    _login(client, OWNER)
    t = _register(client, conns["mine"], "t")
    u = _register(client, conns["mine"], "u")
    r = client.post("/api/admin/relations/accept", json={"relations": [
        {"table_id": t["id"], "related_table_id": u["id"], "join_keys": [["a", "a"]]}]})
    assert r.status_code == 200 and r.json()["accepted"] == 1
    lad = _store_table(conns["lad"], "t")
    _grant_power(OWNER, [{"connection_id": conns["lad"], "schema": None}])
    for child, parent in ((t["id"], lad["id"]), (lad["id"], t["id"])):
        r = client.post("/api/admin/relations/accept", json={"relations": [
            {"table_id": child, "related_table_id": parent, "join_keys": [["a", "a"]]}]})
        assert r.status_code == 403 and r.json()["code"] == "OUT_OF_SCOPE"
    # A table save relating a personal table to ladmin's is refused the same way.
    r = client.post(f"/api/admin/tables/{u['id']}", json=_body(
        conns["mine"], "u", relations=[{"related_table_id": lad["id"],
                                        "join_keys": [["a", "a"]]}]))
    assert r.status_code == 403 and r.json()["code"] == "OUT_OF_SCOPE"
    # Admin: a personal side is owner-only.
    _login(client, ADMIN)
    r = client.post("/api/admin/relations/accept", json={"relations": [
        {"table_id": u["id"], "related_table_id": t["id"], "join_keys": [["a", "a"]]}]})
    assert r.status_code == 403 and r.json()["code"] == "PERSONAL_OWNER_ONLY"
    r = client.post("/api/admin/relations/delete", json={
        "table_id": t["id"], "related_table_id": u["id"], "join_keys": [["a", "a"]]})
    assert r.status_code == 403 and r.json()["code"] == "PERSONAL_OWNER_ONLY"
    r = client.post("/api/admin/relations/dismiss", json={
        "table_id": t["id"], "related_table_id": u["id"], "join_keys": [["a", "a"]]})
    assert r.status_code == 403 and r.json()["code"] == "PERSONAL_OWNER_ONLY"
    _login(client, OWNER)
    r = client.post("/api/admin/relations/delete", json={
        "table_id": t["id"], "related_table_id": u["id"], "join_keys": [["a", "a"]]})
    assert r.status_code == 200 and r.json()["removed"] == 1


def test_recommendation_accept_on_personal_connection(client, conns, monkeypatch):
    import routes.admin_data as admin_mod
    store = db_sources.DataSourceStore()
    recs = store.upsert_recommendations([{
        "connection_id": conns["mine"], "schema": "", "table": "v",
        "source": "sql", "frequency": 1, "evidence": []}], actor=OWNER)
    rid = (recs[0] if isinstance(recs, list) and recs else store.list_recommendations()[0])["id"]
    _login(client, ADMIN)
    r = client.post("/api/admin/relations/recommendations/accept",
                    json={"id": rid, "chosen_type": "normal"})
    assert r.status_code == 403 and r.json()["code"] == "PERSONAL_OWNER_ONLY"
    _login(client, OWNER)
    r = client.post("/api/admin/relations/recommendations/accept",
                    json={"id": rid, "chosen_type": "connector"})
    assert r.status_code == 400 and r.json()["code"] == "PERSONAL_NO_CONNECTOR"
    monkeypatch.setattr(settings, "PERSONAL_TABLES_MAX", 1)
    _register(client, conns["mine"], "t")
    r = client.post("/api/admin/relations/recommendations/accept",
                    json={"id": rid, "chosen_type": "normal"})
    assert r.status_code == 400 and r.json()["code"] == "PERSONAL_TABLE_CAP"
    monkeypatch.setattr(settings, "PERSONAL_TABLES_MAX", 15)
    r = client.post("/api/admin/relations/recommendations/accept",
                    json={"id": rid, "chosen_type": "normal"})
    assert r.status_code == 200 and r.json()["ok"] is True, r.json()
    doc = r.json()["table"]
    assert doc["is_connector"] is False and doc["registered_by"] == OWNER


# ── audit labels ────────────────────────────────────────────────────────────

def test_actor_kind_labels(client, conns):
    _grant_power(POWER, [{"connection_id": conns["lad"], "schema": None}])
    _login(client, POWER)
    _register(client, conns["lad"], "t")
    _login(client, OWNER)
    _register(client, conns["mine"], "t")
    _login(client, ADMIN)
    _register(client, conns["lad"], "u")
    kinds = [r["detail"].get("actor_kind") for r in reversed(_audit("table.save"))]
    assert kinds == ["power_user", "personal", None]


# ── picker (routes/upload.py, unchanged) ────────────────────────────────────

def test_picker_lists_personal_tables_for_the_owner_only(client, conns, tmp_path):
    _login(client, OWNER)
    t = _register(client, conns["mine"], "t")
    from routes import upload as upload_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)

    @app.post("/_login/{email}")
    async def _l(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_" + "0" * 16
        return {"ok": True}

    up = TestClient(app)
    up.post(f"/_login/{OWNER}")
    ids = {r["table_id"] for r in up.get("/api/db_tables").json()["tables"]}
    assert t["id"] in ids
    up.post(f"/_login/{OTHER}")
    ids = {r["table_id"] for r in up.get("/api/db_tables").json()["tables"]}
    assert t["id"] not in ids
    r = up.post("/session/db_tables", json={"table_ids": [t["id"]]})
    assert r.status_code == 403 and r.json()["code"] == "ROLE_DENIED"


# ── roles ───────────────────────────────────────────────────────────────────

def test_roles_refuse_personal_grants(client, conns):
    pt = _store_table(conns["mine"], "t", registered_by=OWNER)
    _login(client, ADMIN)
    msg = "Personal connections cannot be granted to roles."
    for body in ({"name": "A", "scope_grants": [{"connection_id": conns["mine"], "schema": None}]},
                 {"name": "B", "manage_grants": [{"connection_id": conns["mine"], "schema": None}]},
                 {"name": "C", "table_ids": [pt["id"]]}):
        r = client.post("/api/admin/roles", json=body)
        assert r.status_code == 400 and r.json()["error"] == msg, body
    ok = client.post("/api/admin/roles", json={"name": "D"}).json()
    rid = (ok.get("role") or ok)["id"]
    r = client.post(f"/api/admin/roles/{rid}",
                    json={"scope_grants": [{"connection_id": conns["mine"], "schema": None}]})
    assert r.status_code == 400 and r.json()["error"] == msg


# ── users window switch ─────────────────────────────────────────────────────

def test_users_rows_carry_self_service_and_switch(client):
    _login(client, ADMIN)
    rows = {u["email"]: u for u in client.get("/api/admin/users").json()["users"]}
    assert rows[OWNER]["self_service"] is True
    r = client.post("/api/admin/users/set_self_service",
                    json={"email": OWNER, "allowed": False})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "email": OWNER, "self_service": False}
    rows = {u["email"]: u for u in client.get("/api/admin/users").json()["users"]}
    assert rows[OWNER]["self_service"] is False
    a = _audit("user.set_self_service")
    assert a[0]["target"] == OWNER and a[0]["detail"] == {"allowed": False}
    assert roles_store.self_service_allowed(OWNER) is False


def test_set_self_service_refusals(client):
    _login(client, ADMIN)
    for body, code in (({"email": ADMIN, "allowed": False}, 400),
                       ({"email": "not-an-address", "allowed": False}, 400),
                       ({"email": OWNER, "allowed": "no"}, 400),
                       ({"email": OWNER}, 400),
                       ({"email": "ghost@x.com", "allowed": False}, 404)):
        r = client.post("/api/admin/users/set_self_service", json=body)
        assert r.status_code == code, body
    assert not _audit("user.set_self_service")
    _login(client, OWNER)
    assert client.post("/api/admin/users/set_self_service",
                       json={"email": OWNER, "allowed": True}).status_code == 403


# ── switching off keeps existing personal tables working ────────────────────

def test_switched_off_user_keeps_personal_tables_in_chats_and_schedule(client, conns):
    """Sandro, 2026-11-03: switching a user off blocks the page and the routes
    only — their existing personal tables still load in their chats (the
    ownership read) and still refresh on the schedule."""
    _login(client, OWNER)
    t = _register(client, conns["mine"], "t")
    local_store.AuthStore().set_self_service(OWNER, False)
    assert client.get("/api/admin/connections").status_code == 403
    # Chat: the table stays readable for the owner and loads from its snapshot.
    assert t["id"] in roles_store.allowed_table_ids_for(OWNER)
    chat = local_store.ChatDataStore("c_personal1")
    meta = chat.read_meta()
    meta["owner"] = OWNER
    meta["files"] = [{
        "file_name": "my t", "file_description": "", "source": "database",
        "db": {"table_id": t["id"], "connection_id": conns["mine"],
               "schema": "", "table_name": "t", "display_name": "my t",
               "is_connector": False, "auto_included": False,
               "refreshed_at": t.get("refreshed_at")},
        "schema": {"file_name": "my t", "fields": {}}}]
    chat.write_meta(meta)
    dfs = local_store.ChatDataStore("c_personal1").load_dataframes()
    assert list(dfs) == ["my t"] and len(dfs["my t"]) == 2
    assert local_store.missing_db_tables(chat.read_meta()) == []
    # Schedule: a due run refreshes the personal table like any other.
    import db_scheduler
    res = db_scheduler.run_all_due(table_ids=[t["id"]], force=True)
    assert [r["table_id"] for r in res["results"]] == [t["id"]]
    assert res["results"][0]["ok"] is True, res
