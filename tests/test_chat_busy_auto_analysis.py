"""Questions during a running Auto Analytics job (QA 2.3).

A question used to queue behind the job's exec/pool contention and die ~90s
later as "Analysis service is temporarily unavailable." Now: an immediate,
honest 409 BEFORE any history append; and a BrainTimeoutError (contention)
gets busy wording distinct from the generic outage message.

Brain calls stubbed; no real LLM/network.
"""
import json
import time

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
from brain_client import BrainTimeoutError
from settings import settings

OWNER = "alice@acme.com"
CHAT = "chatbusy1"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()

    # A real chat with one small file, owned by OWNER.
    store = local_store.ChatDataStore(CHAT)
    pd.DataFrame({"a": [1, 2]}).to_csv(store.files_dir / "d.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["files"] = [{"file_name": "d.csv", "schema": {}}]
    store.write_meta(meta)

    import routes.chat as chat_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)  # router already carries /api/chat

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _set_auto_state(status):
    store = local_store.ChatDataStore(CHAT)
    meta = store.read_meta()
    meta["auto_analysis"] = {"status": status}
    store.write_meta(meta)


def _history_files():
    store = local_store.ChatDataStore(CHAT)
    return sorted(store.conversations_dir.glob("*.jsonl"))


def test_stream_blocked_while_auto_analysis_processing(client):
    _set_auto_state("processing")
    resp = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "hi?"})
    assert resp.status_code == 409
    data = resp.json()
    assert data["busy"] == "auto_analysis"
    assert "Auto Analytics" in data["error"]
    assert "temporarily unavailable" not in data["error"]
    # blocked BEFORE any history append — no conversation was created
    assert _history_files() == []


def test_stream_proceeds_after_auto_analysis_done(client, monkeypatch):
    _set_auto_state("done")
    import routes.chat as chat_mod

    def fake_multi_plot(**kw):
        yield {"single_response": True,
               "result": {"text": "ok", "image_base64": None, "table": None,
                          "code": None, "usage": {}}}

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        lambda **kw: fake_multi_plot(**kw))
    resp = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "hi?"})
    assert resp.status_code == 200


def test_edit_regenerate_blocked_too(client):
    _set_auto_state("processing")
    resp = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                       json={"edited_question": "x", "conv_id": "cv_1"})
    assert resp.status_code == 409
    assert resp.json()["busy"] == "auto_analysis"


def test_brain_timeout_gets_busy_wording_not_unavailable(client, monkeypatch):
    _set_auto_state("done")
    import routes.chat as chat_mod

    def raise_timeout(**kw):
        raise BrainTimeoutError("pool timeout")
        yield  # pragma: no cover — make it a generator

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", raise_timeout)
    resp = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "hi?"})
    assert resp.status_code == 200  # SSE stream carries the error event
    body = resp.text
    assert "busy right now" in body
    assert "temporarily unavailable" not in body
