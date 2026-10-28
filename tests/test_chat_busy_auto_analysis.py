"""Questions during a running Auto Analytics job (QA 2.3).

A question used to queue behind the job's exec/pool contention and die ~90s
later as "Analysis service is temporarily unavailable." Now: an immediate,
honest 409 BEFORE any history append.

A BrainTimeoutError (the brain did not answer within BRAIN_REQUEST_TIMEOUT)
gets its own wording, `routes.chat.BRAIN_TIMEOUT_TEXT` ("The analysis is
taking longer than expected. Please try again."), plus the i18n key
`error_key: "lab.brain_timeout"` on the stream's error event and on the
edit-regenerate 503. It is neither the generic outage message nor the
sandbox-queue "busy right now" text (`run_chat_local._INFRA_BUSY_TEXT`, which
stays as it is). Every other error event keeps exactly {error, conv_id, done}.

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


BRAIN_TIMEOUT_TEXT = "The analysis is taking longer than expected. Please try again."
CONV = "cv_0123456789abcdef"


def _sse_events(body: str) -> list:
    """Every `data: ` line of an SSE body, parsed as JSON."""
    return [json.loads(line[len("data: "):])
            for line in body.splitlines() if line.startswith("data: ")]


def _raising(exc):
    def gen(**kw):
        raise exc
        yield  # pragma: no cover — make it a generator
    return gen


def test_brain_timeout_constants():
    import routes.chat as chat_mod
    assert chat_mod.BRAIN_TIMEOUT_TEXT == BRAIN_TIMEOUT_TEXT
    assert chat_mod.BRAIN_TIMEOUT_KEY == "lab.brain_timeout"


def test_brain_timeout_gets_slow_wording_with_key(client, monkeypatch):
    _set_auto_state("done")
    import routes.chat as chat_mod

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        _raising(BrainTimeoutError("read timeout")))
    resp = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "hi?"})
    assert resp.status_code == 200  # SSE stream carries the error event
    body = resp.text
    assert "busy right now" not in body
    assert "temporarily unavailable" not in body

    events = _sse_events(body)
    conv_id = events[0].get("conv_id")
    assert conv_id
    errors = [e for e in events if "error" in e]
    assert errors == [{"error": BRAIN_TIMEOUT_TEXT,
                       "error_key": "lab.brain_timeout",
                       "conv_id": conv_id,
                       "done": True}]

    history = local_store.ChatDataStore(CHAT).get_history(conv_id)
    ai_rows = [r for r in history if r.get("role") == "ai"]
    assert ai_rows and ai_rows[-1]["content"] == BRAIN_TIMEOUT_TEXT


def test_plain_brain_error_event_has_no_error_key(client, monkeypatch):
    """Only the timeout carries a translation key; every other error event
    keeps exactly the keys it had."""
    _set_auto_state("done")
    import routes.chat as chat_mod
    from brain_client import BrainError

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        _raising(BrainError("HTTP 502")))
    resp = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "hi?"})
    assert resp.status_code == 200
    events = _sse_events(resp.text)
    errors = [e for e in events if "error" in e]
    assert len(errors) == 1
    assert set(errors[0]) == {"error", "conv_id", "done"}
    assert errors[0]["error"] == "Analysis service is temporarily unavailable."
    assert errors[0]["done"] is True
    assert "lab.brain_timeout" not in resp.text


def test_edit_regenerate_brain_timeout_gets_slow_wording_with_key(client, monkeypatch):
    _set_auto_state("done")
    import routes.chat as chat_mod

    store = local_store.ChatDataStore(CHAT)
    store.append_history(CONV, {"role": "human", "content": "first?", "ts": time.time()})
    store.append_history(CONV, {"role": "ai", "content": "an answer", "ts": time.time()})

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        _raising(BrainTimeoutError("read timeout")))
    resp = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                       json={"edited_question": "edited?", "conv_id": CONV})
    assert resp.status_code == 503
    assert resp.json() == {"error": BRAIN_TIMEOUT_TEXT,
                           "error_key": "lab.brain_timeout",
                           "conv_id": CONV}
    assert "busy right now" not in resp.text

    history = local_store.ChatDataStore(CHAT).get_history(CONV)
    assert [r.get("role") for r in history] == ["human", "ai"]
    assert history[0]["content"] == "edited?"
    assert history[-1]["content"] == BRAIN_TIMEOUT_TEXT


def test_edit_regenerate_plain_brain_error_has_no_error_key(client, monkeypatch):
    _set_auto_state("done")
    import routes.chat as chat_mod
    from brain_client import BrainError

    store = local_store.ChatDataStore(CHAT)
    store.append_history(CONV, {"role": "human", "content": "first?", "ts": time.time()})
    store.append_history(CONV, {"role": "ai", "content": "an answer", "ts": time.time()})

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot",
                        _raising(BrainError("HTTP 502")))
    resp = client.post(f"/api/chat/{CHAT}/edit-regenerate",
                       json={"edited_question": "edited?", "conv_id": CONV})
    assert resp.status_code == 503
    assert resp.json() == {"error": "Analysis service is temporarily unavailable.",
                           "conv_id": CONV}
