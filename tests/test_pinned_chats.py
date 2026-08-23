"""Pinned chats (QA 3.2) + the chat-rename field fix.

Pin flag is an additive key on the user's own active_chats.jsonl rows:
pinned chats sort first, everything else keeps the existing newest-first
order, and pre-feature rows (no `pinned` key) behave exactly as before.
Chat rename used to be fully broken (JS sent {name}, the endpoint reads
{title}) — the round-trip here pins the working contract.
"""
import json

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from local_store import AuthStore
from settings import settings

OWNER = "alice@acme.com"


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(data_root):
    import routes.auth as auth_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    return tc


# --- store level -------------------------------------------------------------

def test_pin_sorts_first_and_unpin_restores(data_root):
    store = AuthStore()
    store.record_active_chat(OWNER, "chA", "Old chat", [])
    store.record_active_chat(OWNER, "chB", "New chat", [])
    assert [r["chat_id"] for r in store.list_active_chats(OWNER)] == ["chB", "chA"]

    assert store.set_chat_pinned(OWNER, "chA", True)
    rows = store.list_active_chats(OWNER)
    assert [r["chat_id"] for r in rows] == ["chA", "chB"]
    assert rows[0]["pinned"] is True
    assert "pinned" not in rows[1]

    assert store.set_chat_pinned(OWNER, "chA", False)
    rows = store.list_active_chats(OWNER)
    assert [r["chat_id"] for r in rows] == ["chB", "chA"]
    assert all("pinned" not in r for r in rows)


def test_pin_unknown_chat_returns_false(data_root):
    store = AuthStore()
    store.record_active_chat(OWNER, "chA", "A", [])
    assert store.set_chat_pinned(OWNER, "nope", True) is False


def test_old_shape_rows_sort_exactly_as_before(data_root):
    # Hand-written pre-feature rows (no pinned key anywhere).
    p = data_root / "users" / OWNER / "active_chats.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(
        json.dumps({"chat_id": "c1", "title": "first", "files": [],
                    "created_at": "2026-01-01T00:00:00+00:00"}) + "\n" +
        json.dumps({"chat_id": "c2", "title": "second", "files": [],
                    "created_at": "2026-02-01T00:00:00+00:00"}) + "\n",
        encoding="utf-8")
    rows = AuthStore().list_active_chats(OWNER)
    assert [r["chat_id"] for r in rows] == ["c2", "c1"]  # newest first, unchanged


# --- endpoint ----------------------------------------------------------------

def test_pin_endpoint_roundtrip(client, data_root):
    AuthStore().record_active_chat(OWNER, "chA", "A", [])
    AuthStore().record_active_chat(OWNER, "chB", "B", [])

    resp = client.post("/auth/active_chats/pin", json={"chat_id": "chA", "pinned": True})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "pinned": True}

    rows = client.get("/auth/active_chats").json()["active_chats"]
    assert rows[0]["chat_id"] == "chA"
    assert rows[0]["pinned"] is True

    resp = client.post("/auth/active_chats/pin", json={"chat_id": "chA", "pinned": False})
    assert resp.json() == {"ok": True, "pinned": False}
    rows = client.get("/auth/active_chats").json()["active_chats"]
    assert rows[0]["chat_id"] == "chB"


def test_pin_endpoint_errors(client, data_root):
    assert client.post("/auth/active_chats/pin", json={}).status_code == 400
    assert client.post("/auth/active_chats/pin",
                       json={"chat_id": "nope", "pinned": True}).status_code == 404


def test_pin_endpoint_requires_auth(data_root):
    import routes.auth as auth_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(auth_mod.router)
    tc = TestClient(app)
    assert tc.post("/auth/active_chats/pin",
                   json={"chat_id": "x", "pinned": True}).status_code == 401


# --- chat rename (user-approved fix: {title} not {name}) ---------------------

def test_chat_rename_roundtrip_with_title_field(client, data_root):
    AuthStore().record_active_chat(OWNER, "chA", "Old name", [])
    resp = client.post("/auth/active_chats/rename",
                       json={"chat_id": "chA", "title": "New name"})
    assert resp.status_code == 200 and resp.json()["ok"]
    rows = client.get("/auth/active_chats").json()["active_chats"]
    assert rows[0]["title"] == "New name"
    assert rows[0]["name"] == "New name"   # the alias the sidebar JS reads
    # the legacy {name} body the old JS sent is still a 400 (missing title)
    assert client.post("/auth/active_chats/rename",
                       json={"chat_id": "chA", "name": "x"}).status_code == 400


def test_rename_preserves_pin(client, data_root):
    AuthStore().record_active_chat(OWNER, "chA", "A", [])
    AuthStore().record_active_chat(OWNER, "chB", "B", [])
    client.post("/auth/active_chats/pin", json={"chat_id": "chA", "pinned": True})
    client.post("/auth/active_chats/rename", json={"chat_id": "chA", "title": "A2"})
    rows = client.get("/auth/active_chats").json()["active_chats"]
    assert rows[0]["chat_id"] == "chA" and rows[0]["title"] == "A2"
    assert rows[0]["pinned"] is True
