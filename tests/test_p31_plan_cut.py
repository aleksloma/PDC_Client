"""P31 Part A (client side) — a CUT plan is an error, not "Try rephrasing".

A `/v1/plan` reply of kind NO_CODE whose `usage.finish_reason` is
"MAX_TOKENS" was produced by a model that ran out of output tokens mid-reply:
the code was cut, the brain labelled what was left NO_CODE, and /lab answered
"I couldn't generate analysis code for this question. Try rephrasing." —
rephrasing does not help there. Both NO_CODE handlers of `run_chat_local`
(the single `run_chat` flow and the multi flow `run_chat_multi_plot` →
`_run_single_body`) now raise `AnalysisFailedError("llm_unavailable")` for
such a plan — AFTER the existing `_raise_if_analysis_failed` and only when the
turn is not a greeting — logging
`ANALYSIS_FAILED error_code=llm_unavailable where=plan_cut`; the stream route
turns it into the translated `lab.analysis_failed` error event (the P29 C2
path), even against an older brain that sends no `error_code`.

Unchanged: a complete NO_CODE reply (finish STOP / no finish reason) keeps
the "Try rephrasing" sentence; a greeting stays a greeting (even when cut);
a plan WITH code that was cut still runs ("Nothing else changes").

Offline: every brain call stubbed on `run_chat_local.brain_client`; python
runs in process (conftest).
"""
import json

import pandas as pd
import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import local_store
import run_chat_local
from conftest import JSON_HEADERS
from settings import settings

OWNER = "alice@acme.com"
CHAT = "c_p31plancut0001"

FAILED_TEXT = "The analysis could not be completed. Please try again."
FAILED_KEY = "lab.analysis_failed"
NO_CODE_TEXT = "I couldn't generate analysis code for this question. Try rephrasing."
GREETING_TEXT = "Hello! Ask me anything about your data."


def _plan(finish=None, greeting=False, kind="NO_CODE", code="", usage_extra=None):
    usage = {} if finish is None else {"finish_reason": finish}
    usage.update(usage_extra or {})
    return {"raw_text": "", "kind": kind, "code": code, "usage": usage,
            "context_decision": {"is_greeting": True} if greeting else {}}


@pytest.fixture
def brain(monkeypatch):
    calls = {"plan": [], "retry": [], "greeting": [], "describe": [], "summarize": []}
    state = {"plan": None}
    bc = run_chat_local.brain_client

    def plan(**kw):
        calls["plan"].append(kw)
        return json.loads(json.dumps(state["plan"]))

    def retry(**kw):
        calls["retry"].append(kw)
        return {"kind": "NO_CODE", "code": "", "usage": {}}

    def greeting(*a, **kw):
        calls["greeting"].append((a, kw))
        return {"text": GREETING_TEXT, "usage": {}}

    def describe(**kw):
        calls["describe"].append(kw)
        return {"text": "described", "usage": {}}

    def summarize(**kw):
        calls["summarize"].append(kw)
        return {"text": "summarized", "usage": {}}

    for name, fn in (("plan", plan), ("retry", retry), ("greeting", greeting),
                     ("describe", describe), ("summarize", summarize)):
        monkeypatch.setattr(bc, name, fn)
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")
    return calls, state


@pytest.fixture
def logs(monkeypatch):
    lines = []
    real = run_chat_local.log_with_sid

    def capture(sid, level, message, **ctx):
        lines.append(str(message))
        return real(sid, level, message, **ctx)

    monkeypatch.setattr(run_chat_local, "log_with_sid", capture)
    return lines


def _dfs():
    return {"d.csv": pd.DataFrame({"a": [1, 2, 3], "b": [10.5, 20.5, 30.5]})}


def _run_single():
    return run_chat_local.run_chat(
        sid="t", dfs=_dfs(), schema_docs={}, question="how many?",
        history_rows=[], user_email=OWNER)


def _run_multi():
    events = []
    for ev in run_chat_local.run_chat_multi_plot(
            sid="t", dfs=_dfs(), schema_docs={}, question="how many?",
            history_rows=[], user_email=OWNER):
        events.append(ev)
    return events


def _single_result(events):
    hits = [e for e in events if e.get("single_response")]
    assert len(hits) == 1, events
    return hits[0]["result"]


# ---------------------------------------------------------------------------
# the cut plan raises in BOTH flows
# ---------------------------------------------------------------------------
def test_single_flow_cut_no_code_plan_raises_analysis_failed(brain, logs):
    _calls, state = brain
    state["plan"] = _plan("MAX_TOKENS")
    with pytest.raises(run_chat_local.AnalysisFailedError) as exc:
        _run_single()
    assert exc.value.error_code == "llm_unavailable"
    assert any("ANALYSIS_FAILED" in l and "error_code=llm_unavailable" in l
               and "where=plan_cut" in l for l in logs), logs


def test_multi_flow_cut_no_code_plan_raises_analysis_failed(brain, logs):
    _calls, state = brain
    state["plan"] = _plan("MAX_TOKENS")
    with pytest.raises(run_chat_local.AnalysisFailedError) as exc:
        _run_multi()
    assert exc.value.error_code == "llm_unavailable"
    assert any("ANALYSIS_FAILED" in l and "error_code=llm_unavailable" in l
               and "where=plan_cut" in l for l in logs), logs


@pytest.mark.parametrize("runner", [_run_single, _run_multi], ids=["single", "multi"])
def test_cut_plan_is_an_analysis_failed_error_not_a_generic_brain_error(brain, runner):
    _calls, state = brain
    state["plan"] = _plan("MAX_TOKENS")
    with pytest.raises(run_chat_local.AnalysisFailedError) as exc:
        runner()
    assert str(exc.value) == FAILED_TEXT or FAILED_TEXT in str(exc.value)


@pytest.mark.parametrize("runner", [_run_single, _run_multi], ids=["single", "multi"])
def test_an_explicit_error_code_still_wins_over_the_cut_check(brain, logs, runner):
    """`_raise_if_analysis_failed` runs FIRST: a plan carrying
    `llm_budget_exhausted` keeps that code even when it was also cut."""
    _calls, state = brain
    plan = _plan("MAX_TOKENS")
    plan["error_code"] = "llm_budget_exhausted"
    state["plan"] = plan
    with pytest.raises(run_chat_local.AnalysisFailedError) as exc:
        runner()
    assert exc.value.error_code == "llm_budget_exhausted"


# ---------------------------------------------------------------------------
# unchanged: complete NO_CODE, greetings, cut plans WITH code
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("finish", ["STOP", None, "", "SAFETY"])
def test_single_flow_complete_no_code_keeps_try_rephrasing(brain, finish):
    _calls, state = brain
    state["plan"] = _plan(finish)
    out = _run_single()
    assert out["text"] == NO_CODE_TEXT


@pytest.mark.parametrize("finish", ["STOP", None])
def test_multi_flow_complete_no_code_keeps_try_rephrasing(brain, finish):
    _calls, state = brain
    state["plan"] = _plan(finish)
    result = _single_result(_run_multi())
    assert result["text"] == NO_CODE_TEXT


def test_usage_missing_or_not_a_dict_keeps_try_rephrasing(brain):
    _calls, state = brain
    plan = _plan(None)
    plan["usage"] = None
    state["plan"] = plan
    assert _run_single()["text"] == NO_CODE_TEXT
    assert _single_result(_run_multi())["text"] == NO_CODE_TEXT


@pytest.mark.parametrize("runner", ["single", "multi"])
@pytest.mark.parametrize("finish", ["MAX_TOKENS", "STOP"])
def test_a_greeting_stays_a_greeting_even_when_cut(brain, runner, finish):
    calls, state = brain
    state["plan"] = _plan(finish, greeting=True)
    if runner == "single":
        out = _run_single()
    else:
        out = _single_result(_run_multi())
    assert out["text"] == GREETING_TEXT
    assert len(calls["greeting"]) == 1


@pytest.mark.parametrize("runner", ["single", "multi"])
def test_a_cut_plan_with_code_still_runs(brain, runner):
    """Only a NO_CODE plan is judged: a PYTHON plan whose usage says
    MAX_TOKENS but whose code is complete runs as before."""
    _calls, state = brain
    state["plan"] = _plan("MAX_TOKENS", kind="PYTHON", code="RESULT = int(dfs['d.csv']['a'].sum())")
    if runner == "single":
        out = _run_single()
    else:
        out = _single_result(_run_multi())
    assert out.get("code"), out
    assert out["text"] != FAILED_TEXT


# ---------------------------------------------------------------------------
# the stream route answers the translated error event
# ---------------------------------------------------------------------------
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


def test_stream_cut_plan_is_the_analysis_failed_error_event(client, brain):
    calls, state = brain
    state["plan"] = _plan("MAX_TOKENS")
    body, evs, conv = _stream(client)
    errors = [e for e in evs if "error" in e]
    assert errors == [{"error": FAILED_TEXT, "error_key": FAILED_KEY,
                       "conv_id": conv, "done": True}], evs
    assert NO_CODE_TEXT not in body
    assert calls["describe"] == [] and calls["summarize"] == []
    rows = [r for r in local_store.ChatDataStore(CHAT).get_history(conv)
            if r.get("role") == "ai"]
    assert rows and rows[-1]["content"] == FAILED_TEXT, rows


def test_stream_complete_no_code_keeps_try_rephrasing(client, brain):
    _calls, state = brain
    state["plan"] = _plan("STOP")
    body, evs, conv = _stream(client)
    assert not [e for e in evs if "error" in e], evs
    assert FAILED_KEY not in body
    dones = [e for e in evs if e.get("done")]
    assert len(dones) == 1 and dones[0]["answer"] == NO_CODE_TEXT
