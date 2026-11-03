"""Personal connections — the user-removal cascade (task section 2.7).

`POST /api/admin/users/remove` runs a new step `delete_personal_sources`
right BEFORE `release_registrations`: every connection the removed address
OWNS goes, with every table on it (registry rows, snapshot parquets,
profile sidecars, role ids), ladmin's connections untouched. The response
and the `user.removed` audit row gain `personal_connections_deleted` and
`personal_tables_deleted`. A step that raises counts 0, logs
ADMIN_USER_REMOVE_STEP_FAILED and the removal still completes. Chats that
referenced a deleted personal table fall into the existing
`missing_db_tables` "unregistered" path; another user's dashboard tile
pinned from the removed owner's chat answers the existing frozen shape.

Real app, the tests/test_admin_user_lifecycle.py idiom (no lifespan, https
base_url, every brain call stubbed, DATA_ROOT = tmp_path)."""
import pytest
from cryptography.fernet import Fernet
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import db_sources
import local_store
import roles_store
from settings import settings
from tests.conftest import csrf_form

ADMIN = "ladmin"
ADMIN_PW = "Ladmin-passw0rd"
OWNER = "pers.owner@corp.example"
OWNER_PW = "Pers-owner-passw0rd"
RCPT = "pers.rcpt@corp.example"
RCPT_PW = "Pers-rcpt-passw0rd"
CHAT = "c_persowned01"


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", ADMIN)
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    for name, value in (("ALLOW_SELF_REGISTRATION", False), ("PASSWORD_MIN_LENGTH", 8)):
        if name in type(settings).model_fields:
            monkeypatch.setattr(settings, name, value)
    local_store._DATAFRAME_CACHE.invalidate()
    local_store._SESSION_GEN_CACHE.clear()
    local_store._RESET_TOKEN_INDEX.clear()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    store = local_store.AuthStore()
    for email, pw in ((ADMIN, ADMIN_PW), (OWNER, OWNER_PW), (RCPT, RCPT_PW)):
        store.ensure_user(email)
        store.set_password(email, pw)
    store.set_role(ADMIN, "admin")
    roles_store.RolesStore().ensure_base_role()
    yield {"tmp": tmp_path}
    local_store._DATAFRAME_CACHE.invalidate()
    local_store._SESSION_GEN_CACHE.clear()


def _client():
    return TestClient(app_mod.app, base_url="https://testserver",
                      raise_server_exceptions=False)


def _signed_in(email, password, expect):
    tc = _client()
    r = tc.post("/auth/login", data=csrf_form(tc, {"email": email, "password": password}),
                follow_redirects=False)
    assert r.status_code == 302, (r.status_code, r.text[:300])
    assert r.headers["location"] == expect, r.headers["location"]
    return tc


def _admin():
    return _signed_in(ADMIN, ADMIN_PW, "/admin/data_sources")


def _audit(action):
    return [r for r in db_sources.read_audit_tail(500) if r.get("action") == action]


def _conn(name, owner=None):
    kw = {"owner": owner} if owner else {}
    return db_sources.DataSourceStore().create_connection(
        {"name": name, "db_type": "postgresql", "host": "h", "port": 5432,
         "database": "d", "user": "u"}, "pw", actor=owner or ADMIN, **kw)["id"]


def _table(cid, name, registered_by=None):
    doc = {"connection_id": cid, "schema": "s", "table_name": name,
           "display_name": name, "description": "", "is_connector": False,
           "relations": [], "columns": [{"name": "a", "dtype": "INTEGER"}]}
    if registered_by:
        doc["registered_by"] = registered_by
    tid = db_sources.DataSourceStore().upsert_table(doc, actor=ADMIN)["id"]
    local_store.db_snapshot_path(tid).write_bytes(b"parquet")
    local_store.db_profile_path(tid).write_text("{}", encoding="utf-8")
    return tid


@pytest.fixture
def estate(world):
    """OWNER: two personal connections holding three tables; ladmin: one
    connection with one table granted to a role together with a personal
    table id (as a hand-edited roles.json would)."""
    p1 = _conn("P1", owner=OWNER)
    p2 = _conn("P2", owner=OWNER)
    lad = _conn("L")
    tids = {"a": _table(p1, "a", OWNER), "b": _table(p1, "b", OWNER),
            "c": _table(p2, "c", OWNER), "lad": _table(lad, "lad")}
    doc = roles_store.RolesStore().read_doc()
    doc["roles"].append({"id": "aa11bb22cc33dd44", "name": "R",
                         "table_ids": [tids["a"], tids["lad"]],
                         "scope_grants": [{"connection_id": lad, "schema": None}],
                         "manage_grants": [{"connection_id": p1, "schema": None}]})
    roles_store.RolesStore()._write_doc(doc)
    return {"p1": p1, "p2": p2, "lad": lad, "tids": tids}


def test_remove_deletes_personal_connections_and_tables(world, estate):
    r = _admin().post("/api/admin/users/remove", json={"email": OWNER})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["personal_connections_deleted"] == 2
    assert body["personal_tables_deleted"] == 3
    # Nothing of the owner's personal tables was left to release.
    assert body["registrations_released"] == 0
    store = db_sources.DataSourceStore()
    assert [c["id"] for c in store.list_connections()] == [estate["lad"]]
    assert [t["id"] for t in store.list_tables()] == [estate["tids"]["lad"]]
    for key in ("a", "b", "c"):
        tid = estate["tids"][key]
        assert not local_store.db_snapshot_path(tid).exists()
        assert not local_store.db_profile_path(tid).exists()
    assert local_store.db_snapshot_path(estate["tids"]["lad"]).exists()
    role = roles_store.RolesStore().get_role("aa11bb22cc33dd44")
    assert role["table_ids"] == [estate["tids"]["lad"]]
    assert role["manage_grants"] == []
    assert role["scope_grants"] == [{"connection_id": estate["lad"], "schema": None}]
    rows = [x for x in _audit("user.removed") if x.get("target") == OWNER]
    assert rows
    detail = rows[0]["detail"]
    assert detail["personal_connections_deleted"] == 2
    assert detail["personal_tables_deleted"] == 3
    assert _audit("personal_sources.deleted")


def test_failing_cascade_step_still_removes_and_audits(world, estate, monkeypatch):
    import routes.admin_users as admin_users
    logged = []
    monkeypatch.setattr(admin_users, "log_with_sid",
                        lambda sid, level, msg, *a, **k: logged.append(msg))

    def _boom(self, email, *, actor):
        raise RuntimeError("cascade failed")
    monkeypatch.setattr(db_sources.DataSourceStore, "delete_personal_sources", _boom)
    r = _admin().post("/api/admin/users/remove", json={"email": OWNER})
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["ok"] is True
    assert body["personal_connections_deleted"] == 0
    assert body["personal_tables_deleted"] == 0
    assert not local_store.AuthStore().user_exists(OWNER)
    assert any("ADMIN_USER_REMOVE_STEP_FAILED step=delete_personal_sources" in m
               for m in logged), logged
    rows = [x for x in _audit("user.removed") if x.get("target") == OWNER]
    assert rows and rows[0]["detail"]["personal_tables_deleted"] == 0


def test_chat_on_a_deleted_personal_table_reports_unregistered(world, estate):
    """A chat whose meta still names a personal table the cascade deleted
    falls into the existing missing_db_tables "unregistered" path."""
    tid = estate["tids"]["a"]
    chat = local_store.ChatDataStore("c_persleft01")
    meta = chat.read_meta()
    meta["owner"] = RCPT
    meta["files"] = [{
        "file_name": "a", "file_description": "", "source": "database",
        "db": {"table_id": tid, "connection_id": estate["p1"], "schema": "s",
               "table_name": "a", "display_name": "a", "is_connector": False,
               "auto_included": False, "refreshed_at": None},
        "schema": {"file_name": "a", "fields": {}}}]
    chat.write_meta(meta)
    assert db_sources.DataSourceStore().delete_personal_sources(OWNER, actor=ADMIN) == {
        "connections": 2, "tables": 3}
    missing = local_store.missing_db_tables(chat.read_meta())
    assert [(m["table_id"], m["reason"]) for m in missing] == [(tid, "unregistered")]
    assert local_store.ChatDataStore("c_persleft01").load_dataframes() == {}


def test_recipient_tile_from_removed_owner_chat_freezes(world, estate):
    """RCPT pinned a tile from OWNER's shared chat (which read OWNER's
    personal table). After OWNER's removal the tile refresh answers the
    existing frozen shape — no crash."""
    store = local_store.ChatDataStore(CHAT)
    meta = store.read_meta()
    meta["owner"] = OWNER
    store.write_meta(meta)
    store.add_share_recipients([RCPT])
    ds = local_store.DashboardStore()
    dash = ds.create_dashboard(RCPT, "Mine")
    tile = ds.add_tile(RCPT, dash["dash_id"], {
        "chat_id": CHAT, "kind": "chart", "code": "fig = None",
        "snapshot": {"image_base64": "iVBORfakepng"}})
    assert _admin().post("/api/admin/users/remove",
                         json={"email": OWNER}).status_code == 200
    rc = _signed_in(RCPT, RCPT_PW, "/lab")
    r = rc.post(f"/api/dashboards/{dash['dash_id']}/tiles/{tile['tile_id']}/refresh",
                json={})
    assert r.status_code == 200, r.text[:300]
    out = r.json()
    assert out["ok"] is False and out["frozen"] is True, out
    assert out["reason"] in ("source_deleted", "access_revoked"), out
