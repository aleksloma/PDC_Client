"""Count-matrix redraw (Prompt 15 Fix 1b).

Verified live: for 13 cities x 5 products the planner drew px.imshow with the
counts printed in the cells, when its own rule says grouped bar. Prompt 14's
prompt fix alone did not stop it, so the client now checks the RENDERED result
and asks for ONE redraw — through the same regeneration mechanism the
multi-axes and non-interactive guards already use, so the chart that reaches
the user comes with its own code and stays refreshable.

Brain calls are stubbed (fake-client pattern); rendering is stubbed so the test
can hand-craft the matrix shape (test_retry_loop.py idiom).
"""
import pandas as pd
import pytest

import run_chat_local

MATRIX_CODE = ("piv = dfs['m.csv'].pivot_table(index='city', columns='product', "
               "values='n')\nfig = px.imshow(piv, text_auto=True)")
BAR_CODE = ("fig = px.bar(dfs['m.csv'], x='city', y='n', color='product', "
            "barmode='group')")

CITIES = [f"city{i}" for i in range(13)]
PRODUCTS = ["Milk", "Cheese", "Butter", "Yoghurt", "Sour Cream"]


def _matrix_chart_data():
    return {"columns": ["product", "city", "record_count"],
            "rows": [{"product": p, "city": c,
                      "record_count": CITIES.index(c) + PRODUCTS.index(p)}
                     for c in CITIES for p in PRODUCTS],
            "total_rows": len(CITIES) * len(PRODUCTS)}


def _bar_chart_data():
    return {"columns": ["Series", "city", "n"],
            "rows": [{"Series": p, "city": c, "n": CITIES.index(c) + PRODUCTS.index(p)}
                     for c in CITIES for p in PRODUCTS],
            "total_rows": len(CITIES) * len(PRODUCTS)}


@pytest.fixture
def dfs():
    return {"m.csv": pd.DataFrame({"city": ["a", "b"], "product": ["x", "y"],
                                   "n": [1, 2]})}


@pytest.fixture(autouse=True)
def _stub_schema(monkeypatch):
    monkeypatch.setattr(run_chat_local, "build_schema_text", lambda *a, **k: "schema")


def _wire(monkeypatch, render_by_code, retry_impl):
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: {
        "raw_text": "", "kind": "PLOT_CODE", "code": MATRIX_CODE, "usage": {}})
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        lambda code, dfs, sid, **kw: render_by_code(code))
    retries = []

    def fake_retry(**kw):
        retries.append(kw)
        return retry_impl(kw)

    monkeypatch.setattr(run_chat_local.brain_client, "retry", fake_retry)
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **k: {"text": "described", "usage": {}})
    return retries


def _render(code):
    if "imshow" in code:
        return {"ok": True, "image": "IMG_MATRIX", "is_plotly": True,
                "plotly_html": "<div>matrix</div>", "chart_data": _matrix_chart_data()}
    return {"ok": True, "image": "IMG_BAR", "is_plotly": True,
            "plotly_html": "<div>bar</div>", "chart_data": _bar_chart_data()}


def _run(dfs):
    return list(run_chat_local.run_chat_multi_plot(
        sid="t", dfs=dfs, schema_docs={}, question="products per city",
        history_rows=[], user_email="alice@acme.com"))


def test_count_matrix_is_redrawn_as_a_grouped_bar(monkeypatch, dfs):
    retries = _wire(monkeypatch, _render,
                    lambda kw: {"kind": "PLOT_CODE", "code": BAR_CODE, "usage": {}})
    events = _run(dfs)

    assert len(retries) == 1, "expected exactly one redraw request"
    assert "UnreadableMatrixError" in retries[0]["error_msg"]
    assert retries[0]["failed_code"] == MATRIX_CODE
    charts = [e for e in events if e.get("partial")]
    assert len(charts) == 1
    # the emitted chart is the BAR, and its stored code matches what was drawn
    assert charts[0]["code"] == BAR_CODE
    assert "bar" in charts[0]["image_base64"]


def test_redraw_is_attempted_only_once(monkeypatch, dfs):
    # The brain answers with another matrix — the loop must not ping-pong.
    retries = _wire(monkeypatch, _render,
                    lambda kw: {"kind": "PLOT_CODE", "code": MATRIX_CODE + "\n# again",
                                "usage": {}})
    events = _run(dfs)
    assert len(retries) == 1
    charts = [e for e in events if e.get("partial")]
    assert len(charts) == 1     # the matrix is kept rather than lost


def test_failed_redraw_keeps_the_original_chart(monkeypatch, dfs):
    retries = _wire(monkeypatch, _render,
                    lambda kw: {"kind": "NO_CODE", "code": "", "usage": {}})
    events = _run(dfs)
    assert len(retries) == 1
    charts = [e for e in events if e.get("partial")]
    assert len(charts) == 1 and charts[0]["code"] == MATRIX_CODE


def test_readable_chart_never_triggers_a_redraw(monkeypatch, dfs):
    monkeypatch.setattr(run_chat_local.brain_client, "plan", lambda **k: {
        "raw_text": "", "kind": "PLOT_CODE", "code": BAR_CODE, "usage": {}})
    monkeypatch.setattr(run_chat_local, "render_plot_safe",
                        lambda code, dfs, sid, **kw: _render(code))
    retries = []
    monkeypatch.setattr(run_chat_local.brain_client, "retry",
                        lambda **kw: retries.append(kw) or {"kind": "NO_CODE", "code": ""})
    monkeypatch.setattr(run_chat_local.brain_client, "describe",
                        lambda **k: {"text": "described", "usage": {}})
    _run(dfs)
    assert retries == []


def test_user_asking_for_a_heatmap_is_never_overridden(monkeypatch, dfs):
    # The planner rules allow a matrix when the user asks for one; silently
    # redrawing a requested chart would be worse than the grid.
    retries = _wire(monkeypatch, _render,
                    lambda kw: {"kind": "PLOT_CODE", "code": BAR_CODE, "usage": {}})
    for q in ("show me a heatmap of products per city",
              "სითბური რუკა დამიხატე",
              "покажи тепловую карту"):
        retries.clear()
        list(run_chat_local.run_chat_multi_plot(
            sid="t", dfs=dfs, schema_docs={}, question=q,
            history_rows=[], user_email="alice@acme.com"))
        assert retries == [], f"redrew a heatmap the user asked for: {q}"


def test_matrix_code_without_the_matrix_shape_is_left_alone(monkeypatch, dfs):
    # A heatmap over MEASURED values (non-integer cells) is a legitimate choice:
    # the code sniff alone must never spend a brain call.
    def render(code):
        out = _render(code)
        if "imshow" in code:
            for r in out["chart_data"]["rows"]:
                r["record_count"] = r["record_count"] + 0.5
        return out

    retries = _wire(monkeypatch, render,
                    lambda kw: {"kind": "PLOT_CODE", "code": BAR_CODE, "usage": {}})
    _run(dfs)
    assert retries == []
