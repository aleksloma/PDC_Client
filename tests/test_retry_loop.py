"""Unit tests for the multi-plot retry loop in run_chat_local.run_chat_multi_plot.

Everything that touches the brain is mocked so these tests never hit the live
tenant token. We assert the resilience contract from the fix:
  (a) a retry that returns prose (NO_CODE) does NOT abort the block,
  (b) a retry that returns runnable PYTHON producing an image is accepted,
  (c) PLOT_CODE that keeps failing is retried 3 times with pro/search
      escalation from the 2nd retry, and zero rendered charts yields the new
      fallback message — while a success on the 2nd retry renders normally.
"""
import pandas as pd
import pytest

import run_chat_local


def _plan_with_blocks(n: int) -> dict:
    """Build a planner raw_text with `n` ###NEXT_PLOT###-separated plot blocks.

    A trailing delimiter is always appended so even a single block is routed
    through the multi-plot path (the empty trailing segment is ignored by
    `_extract_multi_plot_blocks`)."""
    blocks = "\n###NEXT_PLOT###\n".join(
        f"```plot_code\nplot_block_{i}\n```" for i in range(n)
    )
    blocks += "\n###NEXT_PLOT###\n"
    return {"raw_text": blocks, "usage": {}, "kind": "PLOT_CODE"}


@pytest.fixture
def dfs():
    return {"f.csv": pd.DataFrame({"a": [1, 2, 3]})}


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    # Keep schema_text trivial — the planner is mocked anyway.
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


def _run(dfs):
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="dash",
        history_rows=[], user_email="alice@acme.com",
    ))


def test_retry_prose_does_not_abort_and_keeps_escalating(monkeypatch, dfs):
    """(a) Every retry returns prose (NO_CODE). The loop must NOT stop after the
    first prose — it keeps retrying 3 times, then skips the block (0 charts)."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: _plan_with_blocks(1))
    # Single block always fails to render.
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        lambda code, dfs, sid: {"error": "SparseChartError"})

    calls = []

    def fake_retry(**kw):
        calls.append(kw)
        return {"kind": "NO_CODE", "code": "", "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **k: {"text": "x", "usage": {}})

    events = _run(dfs)

    # 3 retry attempts despite prose on every one (no early abort).
    assert len(calls) == 3
    # Escalation: use_pro/use_search False on the 1st retry, True from the 2nd.
    assert [c["use_pro"] for c in calls] == [False, True, True]
    assert [c["use_search"] for c in calls] == [False, True, True]
    # No chart rendered → the new fallback message, never "Analysis complete."
    done = [e for e in events if e.get("done")][0]
    assert done["combined_answer"] == "Something went wrong with this analysis. Please try again."
    assert not any(e.get("partial") for e in events)


def test_retry_python_with_image_is_accepted(monkeypatch, dfs):
    """(b) A retry rewrites the block as PYTHON; safe_execute yields an image →
    the chart is accepted and rendered."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: _plan_with_blocks(1))
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        lambda code, dfs, sid: {"error": "NameError: foo"})

    retry_calls = []

    def fake_retry(**kw):
        retry_calls.append(kw)
        return {"kind": "PYTHON", "code": "result = df.head()", "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)
    # safe_execute returns a chart image for the PYTHON rewrite.
    monkeypatch.setattr(run_chat_local, "safe_execute",
                        lambda code, dfs, sid: {"error": None, "image_base64": "IMG", "result": None})
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **k: {"text": "described", "usage": {}})

    events = _run(dfs)

    assert len(retry_calls) == 1  # accepted on the first retry
    partials = [e for e in events if e.get("partial")]
    assert len(partials) == 1
    assert partials[0]["image_base64"] == "IMG"
    assert partials[0]["answer"] == "described"
    done = [e for e in events if e.get("done")][0]
    assert done["combined_answer"] == "described"


def test_plot_code_failing_three_times_yields_fallback(monkeypatch, dfs):
    """(c) PLOT_CODE keeps failing: exactly 3 retries, pro/search escalation from
    the 2nd, zero charts → the new fallback message."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: _plan_with_blocks(1))
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        lambda code, dfs, sid: {"error": "SparseChartError"})

    calls = []

    def fake_retry(**kw):
        calls.append(kw)
        return {"kind": "PLOT_CODE", "code": f"retry_{len(calls)}", "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **k: {"text": "x", "usage": {}})

    events = _run(dfs)

    assert len(calls) == 3
    assert [c["use_pro"] for c in calls] == [False, True, True]
    done = [e for e in events if e.get("done")][0]
    assert done["combined_answer"] == "Something went wrong with this analysis. Please try again."


def test_success_on_second_retry_renders_normally(monkeypatch, dfs):
    """A success on the 2nd retry renders normally (PLOT_CODE that fails once,
    then succeeds)."""
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: _plan_with_blocks(1))

    state = {"render_calls": 0}

    def fake_render(code, dfs, sid):
        state["render_calls"] += 1
        # initial render + 1st retry render fail; 2nd retry render succeeds.
        if state["render_calls"] <= 2:
            return {"error": "SparseChartError"}
        return {"is_plotly": False, "image": "GOOD_IMG"}

    monkeypatch.setattr(run_chat_local, "render_plot_safe", fake_render)

    calls = []

    def fake_retry(**kw):
        calls.append(kw)
        return {"kind": "PLOT_CODE", "code": f"retry_{len(calls)}", "usage": {}}

    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **k: {"text": "ok", "usage": {}})

    events = _run(dfs)

    # 2 retries (1st fails, 2nd succeeds); escalation correct.
    assert len(calls) == 2
    assert [c["use_pro"] for c in calls] == [False, True]
    partials = [e for e in events if e.get("partial")]
    assert len(partials) == 1
    assert partials[0]["image_base64"] == "GOOD_IMG"
    done = [e for e in events if e.get("done")][0]
    assert done["combined_answer"] == "ok"
