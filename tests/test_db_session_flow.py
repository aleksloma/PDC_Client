"""Session wiring for database tables: /session/db_tables selection +
connector closure freezing, /upload preserving DB entries across reset_all,
DB-only /generate_chatdata and /add_data_to_chat (the two relaxed 400 guards),
and autofill leaving ladmin-confirmed descriptions untouched."""
import io
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

OWNER = "user@x.com"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY",
                        Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()

    import routes.upload as upload_mod
    # Brain calls stubbed — offline.
    monkeypatch.setattr(upload_mod.brain_client, "post_activity",
                        lambda *a, **k: None)
    monkeypatch.setattr(upload_mod.brain_client, "chat_metadata",
                        lambda **k: {"name": "Chat", "welcome_message": "hi",
                                     "suggested_questions": ["q1"]})
    monkeypatch.setattr(upload_mod.brain_client, "file_description",
                        lambda **k: {"description": ""})
    autofill_calls = []
    monkeypatch.setattr(upload_mod.brain_client, "schema_autofill",
                        lambda **k: autofill_calls.append(k) or
                        {"file_description": "auto", "columns": {}})

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = "s_dbflow"
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    tc._autofill_calls = autofill_calls
    local_store._DATAFRAME_CACHE.invalidate()
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def registry(tmp_path):
    """Two normal tables + one connector related to the first, with snapshots."""
    store = db_sources.DataSourceStore()
    conn = store.create_connection(
        {"name": "c", "db_type": "postgresql", "host": "h", "port": 5432,
         "database": "d", "user": "u"}, "pw", actor="ladmin")
    ids = {}
    specs = [
        ("cl_info", "clients information", False,
         [{"related_table": "city_dict", "join_keys": [["city_code", "city_code"]]}]),
        ("tr_data", "transactions", False, []),
        ("city_dict", "cities dictionary", True, []),
    ]
    for tname, disp, is_conn, rels in specs:
        t = store.upsert_table({
            "connection_id": conn["id"], "schema": "shop", "table_name": tname,
            "display_name": disp, "description": f"{disp} desc",
            "is_connector": is_conn, "relations": rels,
            "columns": [{"name": "city_code", "dtype": "int",
                         "description": "code", "indexed": True}],
        }, actor="ladmin")
        ids[tname] = t["id"]
        pd.DataFrame({"city_code": [1, 2]}).to_parquet(
            local_store.db_snapshot_path(t["id"]))
    return ids


def test_session_db_tables_freezes_connector_closure(client, registry):
    r = client.post("/session/db_tables",
                    json={"table_ids": [registry["cl_info"]]})
    assert r.status_code == 200
    rows = r.json()["tables"]
    keys = {row["df_key"]: row for row in rows}
    assert set(keys) == {"clients information", "cities dictionary"}
    assert keys["cities dictionary"]["auto_included"] is True
    # Frozen into session meta.
    meta = local_store.UserStore("s_dbflow").read_meta()
    entries = local_store.db_entries_from_meta(meta)
    assert {e["file_name"] for e in entries} == {"clients information",
                                                "cities dictionary"}
    rel = [e for e in entries if e["file_name"] == "clients information"][0]
    assert rel["db"]["relations"][0]["related_df_key"] == "cities dictionary"


def test_direct_connector_selection_is_400(client, registry):
    r = client.post("/session/db_tables",
                    json={"table_ids": [registry["city_dict"]]})
    assert r.status_code == 400


def test_unknown_table_id_is_400(client, registry):
    r = client.post("/session/db_tables",
                    json={"table_ids": ["ffffffffffffffff"]})
    assert r.status_code == 400


def test_api_db_tables_hides_connectors(client, registry):
    r = client.get("/api/db_tables")
    assert r.status_code == 200
    names = {t["display_name"] for t in r.json()["tables"]}
    assert names == {"clients information", "transactions"}


def test_upload_preserves_db_entries_across_reset(client, registry):
    client.post("/session/db_tables", json={"table_ids": [registry["tr_data"]]})
    r = client.post("/upload",
                    files=[("files", ("data.csv", io.BytesIO(b"a,b\n1,2\n"), "text/csv"))])
    assert r.status_code == 200
    meta = local_store.UserStore("s_dbflow").read_meta()
    keys = [e.get("file_name") for e in meta["files"]]
    assert "data.csv" in keys and "transactions" in keys
    assert meta["db_table_ids"] == [registry["tr_data"]]


def test_generate_chatdata_db_only(client, registry):
    client.post("/session/db_tables", json={"table_ids": [registry["cl_info"]]})
    r = client.post("/generate_chatdata", json={})
    assert r.status_code == 200
    chat_id = r.json()["chat_id"]
    chat = local_store.ChatDataStore(chat_id)
    dfs = chat.load_dataframes()
    assert set(dfs) == {"clients information", "cities dictionary"}
    # Sidebar file list carries the display names.
    rows = local_store.AuthStore().list_active_chats(OWNER)
    row = [c for c in rows if c["chat_id"] == chat_id][0]
    assert "clients information" in row.get("files", [])


def test_generate_chatdata_still_400_with_nothing(client, registry):
    r = client.post("/generate_chatdata", json={})
    assert r.status_code == 400


def test_autofill_skips_db_entries(client, registry):
    client.post("/session/db_tables", json={"table_ids": [registry["cl_info"]]})
    r = client.post("/schema_autofill_full")
    assert r.status_code == 200          # DB-only session: ok, nothing to fill
    assert client._autofill_calls == []  # brain never called for DB tables
    meta = local_store.UserStore("s_dbflow").read_meta()
    entry = [e for e in local_store.db_entries_from_meta(meta)
             if e["file_name"] == "clients information"][0]
    assert entry["file_description"] == "clients information desc"  # untouched


def test_add_data_to_chat_db_only(client, registry, monkeypatch):
    # Existing chat owned by OWNER with one csv.
    chat = local_store.ChatDataStore("c_add1")
    (chat.files_dir / "old.csv").write_text("x\n1\n", encoding="utf-8")
    meta = chat.read_meta()
    meta["files"] = [{"file_name": "old.csv", "file_description": "",
                      "schema": {"file_name": "old.csv", "fields": {}}}]
    meta["owner"] = OWNER
    chat.write_meta(meta)
    monkeypatch.setattr(local_store, "chat_exists", lambda cid: True)
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: OWNER)

    client.post("/session/db_tables", json={"table_ids": [registry["tr_data"]]})
    r = client.post("/add_data_to_chat", json={"chat_id": "c_add1"})
    assert r.status_code == 200
    body = r.json()
    assert "transactions" in body["added"]
    dfs = local_store.ChatDataStore("c_add1").load_dataframes()
    assert set(dfs) == {"old.csv", "transactions"}


def test_add_data_merge_keeps_user_edits_on_same_table(client, registry, monkeypatch):
    monkeypatch.setattr(local_store, "chat_exists", lambda cid: True)
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: OWNER)
    client.post("/session/db_tables", json={"table_ids": [registry["cl_info"]]})
    r = client.post("/generate_chatdata", json={})
    chat_id = r.json()["chat_id"]
    # User edits a column description in the chat.
    chat = local_store.ChatDataStore(chat_id)
    meta = chat.read_meta()
    for e in meta["files"]:
        if e.get("file_name") == "clients information":
            e["schema"]["fields"]["city_code"]["description"] = "USER EDIT"
    chat.write_meta(meta)
    # Re-add the same table via Add Data.
    client.post("/session/db_tables", json={"table_ids": [registry["cl_info"]]})
    r2 = client.post("/add_data_to_chat", json={"chat_id": chat_id})
    assert r2.status_code == 200
    meta2 = local_store.ChatDataStore(chat_id).read_meta()
    entry = [e for e in meta2["files"] if e.get("file_name") == "clients information"][0]
    assert entry["schema"]["fields"]["city_code"]["description"] == "USER EDIT"
    # Not duplicated.
    assert sum(1 for e in meta2["files"]
               if (e.get("db") or {}).get("table_id") == registry["cl_info"]) == 1
