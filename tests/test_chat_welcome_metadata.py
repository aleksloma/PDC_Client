"""The chat welcome: what a chat built from database tables sends to the
brain's /v1/chat_metadata, and that the brain's welcome is what
GET /api/chat/{chat_id}/welcome serves (CLIENT_FIX 3).

The hosted symptom (the client's fallback greeting) was traced to the brain
answering 200 with an EMPTY welcome — docs/WELCOME_INVESTIGATION.md. These
tests pin the client half: a DB-only chat sends non-empty context, and a
non-empty brain answer reaches the page unchanged.

Offline: DATA_ROOT is tmp_path, the brain is stubbed, router-only app.
"""
import pandas as pd
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import db_sources
import local_store
import roles_store
from settings import settings

OWNER = "user@x.com"
SID = "s_0000000000000e01"
WELCOME = "Salom! Bu chatda mijozlar va filiallar haqida ma’lumot bor."
QUESTIONS = ["Qaysi filialda mijozlar eng ko‘p?", "Mijozlar soni oylar bo‘yicha?",
             "Faol mijozlar ulushi qancha?"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "CLIENT_ENCRYPTION_KEY", Fernet.generate_key().decode())
    local_store._DATAFRAME_CACHE.invalidate()
    local_store.AuthStore().ensure_user(OWNER)

    import routes.chat as chat_mod
    import routes.upload as upload_mod
    calls = []

    def fake_chat_metadata(**kwargs):
        calls.append(kwargs)
        return {"name": "Mijozlar", "welcome_message": WELCOME,
                "suggested_questions": list(QUESTIONS)}

    monkeypatch.setattr(upload_mod.brain_client, "chat_metadata", fake_chat_metadata)
    monkeypatch.setattr(upload_mod.brain_client, "post_activity", lambda *a, **k: None)

    store = db_sources.DataSourceStore()
    conn = store.create_connection(
        {"name": "BRB Core Banking", "db_type": "postgresql", "host": "h",
         "port": 5432, "database": "d", "user": "u"}, "pw", actor="ladmin")
    table = store.upsert_table({
        "connection_id": conn["id"], "schema": "core", "table_name": "customers",
        "display_name": "customers", "description": "Bank customers, one row each",
        "is_connector": False, "relations": [],
        "columns": [
            {"name": "customer_id", "dtype": "int", "description": "Customer key",
             "indexed": True},
            {"name": "branch_code", "dtype": "str", "description": "Home branch",
             "indexed": False},
        ],
    }, actor="ladmin")
    pd.DataFrame({"customer_id": [1, 2], "branch_code": ["ZQX1", "ZQX2"]}).to_parquet(
        local_store.db_snapshot_path(table["id"]))
    rs = roles_store.RolesStore()
    rs.ensure_base_role()
    rs.update_role(roles_store.BASE_ROLE_ID,
                   {"scope_grants": [{"connection_id": conn["id"], "schema": None}]},
                   actor="ladmin")

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(upload_mod.router)
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        request.session["sid"] = SID
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    yield {"client": tc, "calls": calls, "table_id": table["id"]}
    local_store._DATAFRAME_CACHE.invalidate()


def _db_only_chat(world):
    tc = world["client"]
    r = tc.post("/session/db_tables", json={"table_ids": [world["table_id"]]})
    assert r.status_code == 200, r.text[:300]
    r = tc.post("/generate_chatdata", json={})
    assert r.status_code == 200, r.text[:300]
    return r.json()["chat_id"]


def test_a_db_table_chat_sends_context_to_chat_metadata(world):
    _db_only_chat(world)
    assert len(world["calls"]) == 1, world["calls"]
    sent = world["calls"][0]
    assert sent["files_info"] == ["customers"], sent["files_info"]
    assert sent["file_descriptions"] == {"customers": "Bank customers, one row each"}
    context = sent["context"]
    assert "customers" in context
    assert "Bank customers, one row each" in context
    assert "customer_id" in context and "Customer key" in context
    assert "branch_code" in context and "Home branch" in context


def test_a_db_table_chat_sends_no_row_values(world):
    _db_only_chat(world)
    # Article II: the snapshot's cell values never reach the payload.
    assert "ZQX" not in repr(world["calls"][0])


def test_the_brain_welcome_is_what_the_welcome_route_serves(world):
    chat_id = _db_only_chat(world)
    r = world["client"].get(f"/api/chat/{chat_id}/welcome")
    assert r.status_code == 200, r.text[:300]
    body = r.json()
    assert body["message"] == WELCOME
    assert body["suggested_questions"] == QUESTIONS
