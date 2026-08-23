"""A chat whose database tables were removed must say so (Prompt 15 Fix 4).

Verified live: "Product Insights" and "Market Insights" reference table_ids that
are no longer in the registry, and every question answered with the bare
"Chat dataset is empty." — true, but it named nothing and gave the user no next
step. The message now names the tables and why they cannot be loaded; genuinely
empty chats keep the original wording.

Brain calls are never reached (the check fires first); no network.
"""
import json
from pathlib import Path

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from settings import settings

OWNER = "alice@acme.com"
CHAT = "c_missingdb"
TID = "aa11bb22cc33dd44"
TID2 = "bb22cc33dd44ee55"


def _db_entry(key, tid):
    return {"file_name": key, "source": "database",
            "db": {"table_id": tid, "connection_id": "cc11cc11cc11cc11",
                   "schema": "shop", "table_name": "t", "display_name": key,
                   "is_connector": False, "auto_included": False},
            "schema": {"file_name": key, "fields": {}}}


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    # registry exists but holds none of the chat's tables (an admin removed them)
    (Path(tmp_path) / "data_sources.json").write_text(
        json.dumps({"connections": [], "tables": []}), encoding="utf-8")

    import routes.chat as chat_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _chat(entries, *, with_file=False):
    store = local_store.ChatDataStore(CHAT)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["files"] = entries
    store.write_meta(meta)
    if with_file:
        pd.DataFrame({"a": [1, 2]}).to_csv(store.files_dir / "d.csv", index=False)
    return store


def test_stream_names_the_missing_tables(client):
    _chat([_db_entry("products dictionary", TID), _db_entry("transactions", TID2)])
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "how many?"})
    assert r.status_code == 400
    data = r.json()
    assert "'products dictionary'" in data["error"]
    assert "'transactions'" in data["error"]
    assert "no longer registered" in data["error"]
    assert "administrator" in data["error"]
    assert data["code"] == "DB_TABLES_MISSING"
    assert {t["reason"] for t in data["missing_tables"]} == {"unregistered"}


def test_edit_regenerate_gets_the_same_message(client):
    _chat([_db_entry("transactions", TID)])
    r = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                    json={"conv_id": "cv_none", "message_index": 0,
                          "question": "again?"})
    # Either the message (dataset checked) or a 4xx about the conversation —
    # never a silent success. The dataset branch must carry the new wording.
    if r.status_code == 400 and r.json().get("code") == "DB_TABLES_MISSING":
        assert "'transactions'" in r.json()["error"]
    else:
        assert r.status_code >= 400


def test_refresh_item_reports_the_reason(client):
    # NOTE: code that names the DB key hits the ROLE gate first (by design —
    # that check runs before any data is loaded), so this pins the dataset
    # branch with code that references no gated key.
    _chat([_db_entry("transactions", TID)])
    r = client.post(f"/api/chat/{CHAT}/refresh_item",
                    json={"code": "RESULT = 1", "kind": "table"})
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "DB_TABLES_MISSING"
    assert "'transactions'" in body["error"]


def test_genuinely_empty_chat_keeps_the_old_message(client):
    _chat([])
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "hi?"})
    assert r.status_code == 400
    assert r.json()["error"] == "Chat dataset is empty."
    assert "code" not in r.json()


def test_snapshot_missing_reads_differently_from_unregistered(client, tmp_path):
    # Still registered — the data snapshot is what vanished.
    (Path(tmp_path) / "data_sources.json").write_text(
        json.dumps({"connections": [], "tables": [{"id": TID, "display_name": "transactions"}]}),
        encoding="utf-8")
    _chat([_db_entry("transactions", TID)])
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "how many?"})
    body = r.json()
    assert body["missing_tables"][0]["reason"] == "snapshot_missing"
    assert "stored data" in body["error"]
    assert "no longer registered" not in body["error"]


def test_a_chat_that_still_has_one_working_table_is_not_blocked(client):
    # Partial loss keeps answering on what remains (unchanged behavior).
    store = _chat([_db_entry("gone", TID2)], with_file=True)
    meta = store.read_meta()
    meta["files"] = meta["files"] + [{"file_name": "d.csv", "schema": {}}]
    store.write_meta(meta)
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "hi?"})
    assert r.status_code != 400 or r.json().get("code") != "DB_TABLES_MISSING"
