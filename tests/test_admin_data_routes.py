"""Admin /api/admin/* surface: role guard on every route, masked credentials
in every response, draft-persists-nothing, the four mandatory-confirm locks,
schema-drift 409, refresh-now failure shape, settings validation, audit rows.
Offline: driven end-to-end through the hidden sqlite dialect."""
import json

import pandas as pd
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_sources
import local_store
from settings import settings

ADMIN = "ladmin"
USER = "user@x.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store.AuthStore().ensure_user(ADMIN)
    local_store.AuthStore().set_role(ADMIN, "admin")
    local_store.AuthStore().ensure_user(USER)

    import routes.admin_data as admin_mod
    autofill_payloads = []

    def fake_autofill(**kw):
        autofill_payloads.append(kw)
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

    @app.post("/_login_forced/{email}")
    async def _login_forced(request: Request, email: str):
        request.session["email"] = email
        request.session["must_change_password"] = True
        return {"ok": True}

    tc = TestClient(app)
    tc._autofill_payloads = autofill_payloads
    return tc


@pytest.fixture
def sqlite_conn(client, tmp_path):
    """A saved sqlite connection with a seeded source table. Created at the
    STORE level — the API rejects the hidden sqlite dialect by design."""
    from sqlalchemy import create_engine, text
    db = tmp_path / "src.db"
    eng = create_engine(f"sqlite+pysqlite:///{db}")
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE t (a INTEGER PRIMARY KEY, b TEXT)"))
        conn.execute(text("INSERT INTO t VALUES (1, 'x'), (2, 'y')"))
    eng.dispose()
    client.post(f"/_login/{ADMIN}")
    masked = db_sources.DataSourceStore().create_connection(
        {"name": "S", "db_type": "sqlite",
         "url_override": f"sqlite+pysqlite:///{db}"}, "pw", actor=ADMIN)
    return db, masked["id"]


def _table_body(cid, confirm=True, cols=None):
    return {"connection_id": cid, "schema": "", "table_name": "t",
            "display_name": "test table", "description": "desc",
            "columns": cols if cols is not None else [
                {"name": "a", "dtype": "INTEGER", "description": "col a",
                 "indexed": True, "pk": True},
                {"name": "b", "dtype": "TEXT", "description": "col b",
                 "indexed": False}],
            "is_connector": False, "relations": [], "confirm": confirm}


def test_all_admin_routes_reject_non_admin(client):
    import routes.admin_data as admin_mod
    paths = [(r.path, sorted(r.methods - {"HEAD", "OPTIONS"})[0])
             for r in admin_mod.router.routes]
    # Unauthenticated → 401 everywhere.
    for path, method in paths:
        p = path.replace("{cid}", "aa11bb22cc33dd44").replace("{tid}", "aa11bb22cc33dd44")
        resp = client.request(method, p)
        assert resp.status_code == 401, (p, resp.status_code)
    # Plain user → 403 everywhere + audit rows.
    client.post(f"/_login/{USER}")
    for path, method in paths:
        p = path.replace("{cid}", "aa11bb22cc33dd44").replace("{tid}", "aa11bb22cc33dd44")
        resp = client.request(method, p)
        assert resp.status_code == 403, (p, resp.status_code)
    denied = [r for r in db_sources.read_audit_tail(500)
              if r["action"] == "admin.denied"]
    assert len(denied) == len(paths)


def test_admin_blocked_while_must_change_password(client):
    client.post(f"/_login_forced/{ADMIN}")
    assert client.get("/api/admin/connections").status_code == 403


def test_connection_responses_never_carry_password(client, sqlite_conn):
    _, cid = sqlite_conn
    listing = client.get("/api/admin/connections").json()
    row = listing["connections"][0]
    assert "password_enc" not in row and "url_override" not in row
    assert row["password_set"] is True and row["table_count"] == 0
    text = json.dumps(listing)
    assert "pw" != row.get("password_masked") and "password_enc" not in text


def test_connection_test_saved_and_draft(client, sqlite_conn, tmp_path):
    db, cid = sqlite_conn
    ok = client.post("/api/admin/connections/test",
                     json={"connection_id": cid}).json()
    assert ok["ok"] is True
    # Unsaved draft (Test-before-Save), including a failing one → 200 ok:false.
    bad = client.post("/api/admin/connections/test",
                      json={"db_type": "sqlite", "password": "secret-pw",
                            "url_override": f"sqlite+pysqlite:///{tmp_path}/no/x.db"})
    assert bad.status_code == 200
    body = bad.json()
    assert body["ok"] is False and "secret-pw" not in (body.get("error") or "")


def test_introspect_returns_columns_and_preview(client, sqlite_conn):
    _, cid = sqlite_conn
    r = client.post("/api/admin/tables/introspect",
                    json={"connection_id": cid, "table": "t"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert [c["name"] for c in body["introspection"]["columns"]] == ["a", "b"]
    assert body["preview"]["rows"]


def test_draft_descriptions_persists_nothing_and_payload_clean(client, sqlite_conn):
    _, cid = sqlite_conn
    r = client.post("/api/admin/tables/draft_descriptions",
                    json={"connection_id": cid, "table": "t"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["confirmed"] is False
    assert body["draft"]["table_description"] == "AI table desc"
    # NOTHING persisted — no table row exists.
    assert db_sources.DataSourceStore().list_tables() == []
    # Article II: the brain payload carries no credentials/host/connection id,
    # and the language is fixed English.
    payload = client._autofill_payloads[0]
    blob = json.dumps({k: v for k, v in payload.items()})
    assert "pw" not in blob.split()  # password never present
    for forbidden in ("password", "host", cid):
        assert forbidden not in blob
    assert payload["lang_name"] == "English"


def test_save_without_confirm_is_400(client, sqlite_conn):
    _, cid = sqlite_conn
    r = client.post("/api/admin/tables", json=_table_body(cid, confirm=False))
    assert r.status_code == 400
    assert r.json()["code"] == "CONFIRM_REQUIRED"
    assert db_sources.DataSourceStore().list_tables() == []


def test_save_rejects_on_schema_drift(client, sqlite_conn):
    _, cid = sqlite_conn
    body = _table_body(cid, cols=[{"name": "a", "dtype": "INTEGER"},
                                  {"name": "GONE", "dtype": "TEXT"}])
    r = client.post("/api/admin/tables", json=body)
    assert r.status_code == 409
    assert r.json()["code"] == "SCHEMA_DRIFT"


def test_save_snapshots_and_stamps_server_side_identity(client, sqlite_conn):
    _, cid = sqlite_conn
    body = _table_body(cid)
    body["descriptions_confirmed_by"] = "attacker@evil"  # must be ignored
    r = client.post("/api/admin/tables", json=body)
    assert r.status_code == 201
    out = r.json()
    assert out["snapshot"]["ok"] is True and out["snapshot"]["rows"] == 2
    t = out["table"]
    assert t["descriptions_confirmed_by"] == ADMIN     # session identity wins
    assert t["refreshed_at"]
    assert local_store.db_snapshot_path(t["id"]).exists()
    assert pd.read_parquet(local_store.db_snapshot_path(t["id"])).shape == (2, 2)


def test_refresh_now_unknown_table(client, sqlite_conn):
    r = client.post("/api/admin/tables/aa11bb22cc33dd44/refresh")
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_refresh_settings_roundtrip_and_validation(client, sqlite_conn):
    assert client.post("/api/admin/refresh_settings",
                       json={"refresh_time": "25:00", "refresh_enabled": True}
                       ).status_code == 400
    ok = client.post("/api/admin/refresh_settings",
                     json={"refresh_time": "02:15", "refresh_enabled": False})
    assert ok.status_code == 200
    got = client.get("/api/admin/refresh_settings").json()
    assert got["refresh_time"] == "02:15" and got["refresh_enabled"] is False


def test_delete_connection_conflict_then_cascade(client, sqlite_conn):
    _, cid = sqlite_conn
    client.post("/api/admin/tables", json=_table_body(cid))
    assert client.post(f"/api/admin/connections/{cid}/delete",
                       json={}).status_code == 409
    ok = client.post(f"/api/admin/connections/{cid}/delete",
                     json={"cascade": True})
    assert ok.status_code == 200 and ok.json()["deleted_tables"]


def test_hidden_dialect_rejected_via_api(client):
    """The hidden sqlite dialect (allow_url_override) is tests-only — the API
    must refuse it too, or an arbitrary SQLAlchemy URL could be stored."""
    client.post(f"/_login/{ADMIN}")
    r = client.post("/api/admin/connections",
                    json={"name": "X", "db_type": "sqlite", "password": "pw",
                          "url_override": "sqlite+pysqlite:///:memory:"})
    assert r.status_code == 400


def test_bad_row_cap_degrades_not_500(client, sqlite_conn):
    _, cid = sqlite_conn
    body = _table_body(cid)
    body["row_cap"] = "not-a-number"
    r = client.post("/api/admin/tables", json=body)
    assert r.status_code in (200, 201)          # saved; garbage cap → None
    assert r.json()["table"]["row_cap"] is None


def test_audit_rows_written_for_mutations(client, sqlite_conn):
    _, cid = sqlite_conn
    client.post("/api/admin/tables", json=_table_body(cid))
    actions = {r["action"] for r in db_sources.read_audit_tail(500)}
    assert {"connection.create", "table.save", "table.refresh"} <= actions
