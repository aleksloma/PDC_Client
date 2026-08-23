"""The ANSWER and MISSING_DATA plan kinds (plain-text planner outcomes) must
be returned to the user as text — exactly like CLARIFICATION — in BOTH
plan-consuming paths (run_chat and, via run_chat_multi_plot's single_response
delegation, _run_single_from_plan), and must NEVER be executed as Python.

An ANSWER coming back from /v1/retry inside the exec-retry loop is treated
like NO_CODE/CLARIFICATION prose (a failed attempt — the loop keeps escalating
and finally keeps the error fallback) — a code-requiring flow never runs the
answer text through the executor.

Brain calls are stubbed (fake-client pattern); executors are stubbed to
detect any accidental execution.
"""
import pandas as pd
import pytest

import run_chat_local

ANSWER_TEXT = "The mapping used the shared city_id column between the two tables."


@pytest.fixture
def dfs():
    return {"sales.csv": pd.DataFrame({"city": ["A", "B"], "amount": [10, 20]})}


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


@pytest.fixture
def exec_guard(monkeypatch):
    """Fail the test if any executor runs; returns the call log."""
    calls = []

    def _no_exec(code, dfs, sid, **kw):
        calls.append(code)
        raise AssertionError(f"executor must not run for this kind (code={code!r})")

    monkeypatch.setattr(run_chat_local, "safe_execute", _no_exec)
    monkeypatch.setattr(run_chat_local, "render_plot_safe", _no_exec)
    return calls


def _stub_plan(monkeypatch, kind, code):
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: {
        "raw_text": "", "kind": kind, "code": code, "usage": {"total_tokens": 7},
        "context_decision": {}})


# --- run_chat (Auto Analytics path) ----------------------------------------

def test_run_chat_answer_returns_text_without_exec(monkeypatch, dfs, exec_guard):
    _stub_plan(monkeypatch, "ANSWER", ANSWER_TEXT)
    out = run_chat_local.run_chat(
        sid="t", dfs=dfs, schema_docs={}, question="how did you map these?",
        history_rows=[], user_email="alice@acme.com")
    assert out["text"] == ANSWER_TEXT
    assert out["image_base64"] is None and out["table"] is None and out["code"] is None
    assert out["usage"] == {"total_tokens": 7}
    assert exec_guard == []


def test_run_chat_clarification_still_returns_text(monkeypatch, dfs, exec_guard):
    _stub_plan(monkeypatch, "CLARIFICATION", "Which column should I use?")
    out = run_chat_local.run_chat(
        sid="t", dfs=dfs, schema_docs={}, question="chart",
        history_rows=[], user_email="alice@acme.com")
    assert out["text"] == "Which column should I use?"
    assert exec_guard == []


# --- run_chat_multi_plot → _run_single_from_plan (live chat SSE path) -------

def _run_multi(dfs):
    events = list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="how did you compute this?",
        history_rows=[], user_email="alice@acme.com"))
    assert len(events) == 1 and events[0]["single_response"]
    return events[0]["result"]


def test_single_from_plan_answer_returns_text_without_exec(monkeypatch, dfs, exec_guard):
    _stub_plan(monkeypatch, "ANSWER", ANSWER_TEXT)
    result = _run_multi(dfs)
    assert result["text"] == ANSWER_TEXT
    assert result["image_base64"] is None and result["table"] is None and result["code"] is None
    assert exec_guard == []


def test_single_from_plan_clarification_still_returns_text(monkeypatch, dfs, exec_guard):
    _stub_plan(monkeypatch, "CLARIFICATION", "Which metric?")
    result = _run_multi(dfs)
    assert result["text"] == "Which metric?"
    assert exec_guard == []


# --- MISSING_DATA (same text-back contract as ANSWER/CLARIFICATION) ----------

MISSING_TEXT = "No branch-to-city table is loaded. Add the 'branches dictionary' table."


def test_run_chat_missing_data_returns_text_without_exec(monkeypatch, dfs, exec_guard):
    _stub_plan(monkeypatch, "MISSING_DATA", MISSING_TEXT)
    out = run_chat_local.run_chat(
        sid="t", dfs=dfs, schema_docs={}, question="revenue by city",
        history_rows=[], user_email="alice@acme.com")
    assert out["text"] == MISSING_TEXT
    assert out["image_base64"] is None and out["table"] is None and out["code"] is None
    assert exec_guard == []


def test_single_from_plan_missing_data_returns_text_without_exec(monkeypatch, dfs, exec_guard):
    _stub_plan(monkeypatch, "MISSING_DATA", MISSING_TEXT)
    result = _run_multi(dfs)
    assert result["text"] == MISSING_TEXT
    assert exec_guard == []


def test_retry_missing_data_is_never_executed(monkeypatch, dfs):
    _stub_plan(monkeypatch, "PYTHON", "RESULT = boom")
    executed, retries = [], []

    def failing_exec(code, dfs, sid, **kw):
        executed.append(code)
        return {"error": "NameError: boom", "result": None}

    def fake_retry(**k):
        retries.append(k)
        return {"raw_text": "", "kind": "MISSING_DATA", "code": MISSING_TEXT, "usage": {}}

    monkeypatch.setattr(run_chat_local, "safe_execute", failing_exec)
    monkeypatch.setattr(run_chat_local, "render_plot_safe", failing_exec)
    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)

    out = run_chat_local.run_chat(
        sid="t", dfs=dfs, schema_docs={}, question="q",
        history_rows=[], user_email="alice@acme.com")
    assert executed == ["RESULT = boom"]
    assert len(retries) == 3
    assert "couldn't run this analysis" in out["text"]


# --- retry loop: an ANSWER from /v1/retry is a failed attempt, never exec'd --

def test_retry_answer_is_never_executed(monkeypatch, dfs):
    _stub_plan(monkeypatch, "PYTHON", "RESULT = boom")
    executed, retries = [], []

    def failing_exec(code, dfs, sid, **kw):
        executed.append(code)
        return {"error": "NameError: boom", "result": None}

    def fake_retry(**k):
        retries.append(k)
        return {"raw_text": "", "kind": "ANSWER", "code": ANSWER_TEXT, "usage": {}}

    monkeypatch.setattr(run_chat_local, "safe_execute", failing_exec)
    monkeypatch.setattr(run_chat_local, "render_plot_safe", failing_exec)
    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)

    out = run_chat_local.run_chat(
        sid="t", dfs=dfs, schema_docs={}, question="q",
        history_rows=[], user_email="alice@acme.com")
    # Only the original failing code ran — the answer text never reached exec.
    assert executed == ["RESULT = boom"]
    assert ANSWER_TEXT not in executed
    # Each ANSWER counted as a FAILED attempt: all 3 retries were consumed
    # (escalation stays reachable), then the flow kept its error fallback.
    assert len(retries) == 3
    assert "couldn't run this analysis" in out["text"]
