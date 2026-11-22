"""C2 (p29) — `error_code` from the brain (C2_build_spec §5; tests §6).

`llm_budget_exhausted` and `llm_unavailable` on a `/v1/plan` or `/v1/retry`
response: the turn answers the translated `lab.analysis_failed` ("The
analysis could not be completed. Please try again.") instead of "I couldn't
generate analysis code for this question. Try rephrasing.", saved and sent as
an ERROR turn exactly like a BrainTimeoutError:
  * stream: one error event `{"error", "error_key": "lab.analysis_failed",
    "conv_id", "done": True}` and the AI row's content is the English text;
  * edit-regenerate: `{"error", "error_key", "conv_id"}` with the same status
    as the brain-timeout answer (503), the AI row persisted the same way.
Any other / absent `error_code` keeps today's behaviour. `brain_client`
passes the response through unchanged.

Every brain call is stubbed on `run_chat_local.brain_client`; the REAL flow
(`run_chat_multi_plot`, single path) runs behind the route; the python runs in
process (conftest). Scope reading: the retry case is pinned on the single flow
(a planned table / scalar answer whose execution fails); the multi-chart
worklist's per-block retry is left to the implementation.
"""
import json
import time

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import brain_client
import local_store
import run_chat_local
from conftest import JSON_HEADERS
from settings import settings

OWNER = "alice@acme.com"
CHAT = "c_c2errcode00001"
CONV = "cv_0123456789abcdef"

FAILED_TEXT = "The analysis could not be completed. Please try again."
FAILED_KEY = "lab.analysis_failed"
NO_CODE_TEXT = "I couldn't generate analysis code for this question. Try rephrasing."
RUN_FAILED_TEXT = "I couldn't run this analysis with your current data. Try rephrasing."
CODES = ["llm_budget_exhausted", "llm_unavailable"]

BAD_CODE = "RESULT = undefined_name_xyz"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    store = local_store.ChatDataStore(CHAT)
    pd.DataFrame({"a": [1, 2]}).to_csv(store.files_dir / "d.csv", index=False)
    meta = store.read_meta()
    meta["owner"] = OWNER
    meta["files"] = [{"file_name": "d.csv", "schema": {}}]
    store.write_meta(meta)

    import routes.chat as chat_mod
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}", headers=JSON_HEADERS)
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


@pytest.fixture
def brain(monkeypatch):
    """Stubs on run_chat_local's brain_client binding; `state` holds the plan
    and retry replies, `calls` records every call."""
    calls = {"plan": [], "retry": [], "describe": [], "summarize": []}
    state = {"plan": None, "retry": None}
    bc = run_chat_local.brain_client

    def plan(**kw):
        calls["plan"].append(kw)
        return dict(state["plan"])

    def retry(**kw):
        calls["retry"].append(kw)
        return dict(state["retry"] or {"kind": "NO_CODE", "code": "", "usage": {}})

    def describe(**kw):
        calls["describe"].append(kw)
        return {"text": "described", "usage": {}}

    def summarize(**kw):
        calls["summarize"].append(kw)
        return {"text": "summarized", "usage": {}}

    for name, fn in (("plan", plan), ("retry", retry), ("describe", describe),
                     ("summarize", summarize)):
        monkeypatch.setattr(bc, name, fn)
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")
    return calls, state


def _no_code(error_code=None):
    out = {"raw_text": "", "kind": "NO_CODE", "code": "", "usage": {},
           "context_decision": {}}
    if error_code is not None:
        out["error_code"] = error_code
    return out


def _failing_python_plan():
    return {"raw_text": "", "kind": "PYTHON", "code": BAD_CODE, "usage": {},
            "context_decision": {}}


def _sse(body):
    return [json.loads(line[6:]) for line in body.splitlines() if line.startswith("data: ")]


def _stream(client):
    r = client.post(f"/api/chat/{CHAT}/chat/stream", json={"question": "how many?"},
                    headers=JSON_HEADERS)
    assert r.status_code == 200, r.text[:300]
    evs = _sse(r.text)
    conv = evs[0].get("conv_id")
    assert conv
    return r.text, evs, conv


def _ai_rows(conv):
    return [r for r in local_store.ChatDataStore(CHAT).get_history(conv)
            if r.get("role") == "ai"]


def _assert_error_turn(body, evs, conv):
    errors = [e for e in evs if "error" in e]
    assert errors == [{"error": FAILED_TEXT, "error_key": FAILED_KEY,
                       "conv_id": conv, "done": True}], evs
    assert NO_CODE_TEXT not in body
    assert not [e for e in evs if e.get("done") and "error" not in e], evs
    rows = _ai_rows(conv)
    assert rows and rows[-1]["content"] == FAILED_TEXT, rows


def _seed_conv():
    store = local_store.ChatDataStore(CHAT)
    store.append_history(CONV, {"role": "human", "content": "first?", "ts": time.time()})
    store.append_history(CONV, {"role": "ai", "content": "an answer", "ts": time.time()})
    return store


def _edit(client):
    return client.post(f"/api/chat/{CHAT}/edit-regenerate",
                       json={"edited_question": "edited?", "conv_id": CONV},
                       headers=JSON_HEADERS)


def _assert_edit_error(resp):
    assert resp.status_code == 503, resp.text[:300]
    assert resp.json() == {"error": FAILED_TEXT, "error_key": FAILED_KEY, "conv_id": CONV}
    assert NO_CODE_TEXT not in resp.text
    history = local_store.ChatDataStore(CHAT).get_history(CONV)
    assert [r.get("role") for r in history] == ["human", "ai"]
    assert history[0]["content"] == "edited?"
    assert history[-1]["content"] == FAILED_TEXT


# ---------------------------------------------------------------------------
# the plan carries the code
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", CODES)
def test_plan_error_code_stream_is_an_error_turn(client, brain, code):
    calls, state = brain
    state["plan"] = _no_code(code)
    body, evs, conv = _stream(client)
    _assert_error_turn(body, evs, conv)
    assert calls["describe"] == [] and calls["summarize"] == []


@pytest.mark.parametrize("code", CODES)
def test_plan_error_code_edit_regenerate_is_an_error_turn(client, brain, code):
    _calls, state = brain
    _seed_conv()
    state["plan"] = _no_code(code)
    _assert_edit_error(_edit(client))


# ---------------------------------------------------------------------------
# a retry carries the code
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", CODES)
def test_retry_error_code_stream_is_an_error_turn(client, brain, code):
    calls, state = brain
    state["plan"] = _failing_python_plan()
    state["retry"] = _no_code(code)
    body, evs, conv = _stream(client)
    assert calls["retry"], "the failing execution never reached /v1/retry"
    _assert_error_turn(body, evs, conv)
    assert RUN_FAILED_TEXT not in body


@pytest.mark.parametrize("code", CODES)
def test_retry_error_code_edit_regenerate_is_an_error_turn(client, brain, code):
    calls, state = brain
    _seed_conv()
    state["plan"] = _failing_python_plan()
    state["retry"] = _no_code(code)
    resp = _edit(client)
    assert calls["retry"]
    _assert_edit_error(resp)


# ---------------------------------------------------------------------------
# any other / absent error_code: today's behaviour
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("code", [None, "", "something_else"])
def test_other_plan_error_code_keeps_todays_answer(client, brain, code):
    _calls, state = brain
    state["plan"] = _no_code(code)
    body, evs, conv = _stream(client)
    assert not [e for e in evs if "error" in e], evs
    assert FAILED_KEY not in body
    dones = [e for e in evs if e.get("done")]
    assert len(dones) == 1 and dones[0]["answer"] == NO_CODE_TEXT
    assert _ai_rows(conv)[-1]["content"] == NO_CODE_TEXT


@pytest.mark.parametrize("code", [None, "something_else"])
def test_other_retry_error_code_keeps_retrying(client, brain, code):
    calls, state = brain
    state["plan"] = _failing_python_plan()
    state["retry"] = _no_code(code)
    body, evs, conv = _stream(client)
    assert len(calls["retry"]) == 3                       # today's escalation
    assert not [e for e in evs if "error" in e], evs
    assert FAILED_KEY not in body
    dones = [e for e in evs if e.get("done")]
    assert len(dones) == 1 and dones[0]["answer"] == RUN_FAILED_TEXT
    assert _ai_rows(conv)[-1]["content"] == RUN_FAILED_TEXT


def test_other_error_code_edit_regenerate_keeps_todays_answer(client, brain):
    _calls, state = brain
    _seed_conv()
    state["plan"] = _no_code("something_else")
    resp = _edit(client)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["answer"] == NO_CODE_TEXT
    assert "error_key" not in resp.json()


def test_brain_timeout_wording_is_unchanged(client, monkeypatch):
    """The new mapping adds a key; the timeout keeps its own text and key."""
    import routes.chat as chat_mod
    from brain_client import BrainTimeoutError

    def gen(**kw):
        raise BrainTimeoutError("read timeout")
        yield  # pragma: no cover

    monkeypatch.setattr(chat_mod.run_chat_local, "run_chat_multi_plot", gen)
    body, evs, conv = _stream(client)
    errors = [e for e in evs if "error" in e]
    assert errors == [{"error": chat_mod.BRAIN_TIMEOUT_TEXT,
                       "error_key": chat_mod.BRAIN_TIMEOUT_KEY,
                       "conv_id": conv, "done": True}]
    assert FAILED_KEY not in body


# ---------------------------------------------------------------------------
# brain_client passes the field through
# ---------------------------------------------------------------------------
_PLAN_ARGS = dict(sid="s", question="q", schema_text="s", df_names=["a"],
                  history_rows=[], common_fields=[], user_email="u@x.com")
_RETRY_ARGS = dict(sid="s", question="q", schema_text="s", df_names=["a"],
                   df_columns={"a": ["x"]}, history_rows=[], error_msg="e",
                   failed_code="c", use_pro=False, use_search=False,
                   user_email="u@x.com")


@pytest.mark.parametrize("code", CODES)
def test_brain_client_passes_error_code_through(monkeypatch, code):
    reply = {"kind": "NO_CODE", "code": "", "usage": {}, "error_code": code}
    monkeypatch.setattr(brain_client, "_post", lambda path, payload, sid, *a, **kw: dict(reply))
    assert brain_client.plan(**_PLAN_ARGS).get("error_code") == code
    assert brain_client.retry(**_RETRY_ARGS).get("error_code") == code
