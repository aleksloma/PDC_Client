"""The browser never sends chart markup to the PNG export.

Structural pins on static/dashboard.js and static/dashboard_view.js: the
Download button posts a REFERENCE (`chart_ref`, or the stored row's
`conv_id`/`ai_index`/`image_index`), is disabled for a chart that has none
(that chart only), and every place a chart is rendered hands its reference
over. The tile download uses the dashboard tile route.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
VIEW = (ROOT / "static" / "dashboard_view.js").read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    start = src.index(f"function {name}(")
    depth, i = 0, src.index("{", start)
    while True:
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1


def test_the_export_request_carries_a_reference_not_markup():
    fn = _function(DASH, "createPlotlyContainer")
    call = fn[fn.index("export_plotly_png"):]
    start = call.index("body:")
    body = call[start:call.index("\n", start)]
    assert "currentRef" in body, body
    assert "html" not in body and "currentHtml" not in body, body


def test_the_download_button_is_disabled_without_a_reference():
    fn = _function(DASH, "createPlotlyContainer")
    assert "downloadBtn.disabled = !currentRef" in fn
    assert "if (!currentRef) return;" in fn
    # A refresh brings its own reference (or none) for THIS chart only.
    assert re.search(r"_setChartHtml = \(h, newRef\)", fn)
    assert "currentRef = _validChartRef(newRef)" in fn


def _calls(body: str, name: str) -> list:
    """Every `name(...)` call in `body`, parentheses matched naively."""
    out = []
    for m in re.finditer(rf"\b{re.escape(name)}\(", body):
        depth, i = 0, m.end() - 1
        while i < len(body):
            if body[i] == "(":
                depth += 1
            elif body[i] == ")":
                depth -= 1
                if depth == 0:
                    out.append(body[m.start():i + 1])
                    break
            i += 1
    return out


LIVE_REF = "chartRef: data.chart_ref ? { chart_ref: data.chart_ref } : null"
STORED_REF = "chartRef: { conv_id: convId, ai_index: rowIdx"


def test_every_chart_render_passes_its_reference():
    fn = _function(DASH, "appendMessage")
    assert "(extras && extras.chartRef) || null" in fn
    # renderAiRecord is the one record renderer for reload/recover/edit
    # (docs/DIAGNOSTICS_SPEC.md A5.6): every chart it draws from a stored row
    # gets the row's {conv_id, ai_index, image_index} reference ...
    rec = _function(DASH, "renderAiRecord")
    assert "chartRef: { conv_id: convId, ai_index: rowIdx, image_index: idx }" in rec
    assert "chartRef: { conv_id: convId, ai_index: rowIdx, image_index: 0 }" in rec
    img_calls = [c for c in _calls(rec, "appendMessage") if "image_base64" in c]
    assert img_calls, rec[:400]
    assert all(STORED_REF in c for c in img_calls), img_calls
    # ... and each caller hands it the row's real index: the reload loop its
    # row index, recovery the last row, edit-regenerate the persisted
    # record's index.
    assert re.search(r"renderAiRecord\(\s*msg\s*,\s*convId\s*,\s*rowIdx\s*\)",
                     _function(DASH, "openConversation"))
    assert any("history.length - 1" in c
               for c in _calls(_function(DASH, "_tryRecoverResponse"), "renderAiRecord"))
    assert any("record_view" in c and "ai_index" in c
               for c in _calls(_function(DASH, "editAndRegenerate"), "renderAiRecord"))
    # No stored-row reference is built anywhere else.
    outside = DASH.replace(rec, "")
    assert "ai_index: rowIdx" not in outside
    assert "ai_index: history.length - 1" not in outside
    # Live responses: every streamed chart (partial, single done, non-SSE
    # fallback) carries data.chart_ref.
    send = _function(DASH, "sendMessage")
    live_img_calls = [c for c in _calls(send, "appendMessage") if "data.image_base64" in c]
    assert len(live_img_calls) >= 3, live_img_calls
    assert all(LIVE_REF in c for c in live_img_calls), live_img_calls
    assert "container._setChartHtml(newContent, data.chart_ref ? { chart_ref: data.chart_ref } : null)" in DASH


def test_the_tile_download_uses_the_tile_route_and_sends_no_markup():
    assert "export_plotly_png" not in VIEW
    i = VIEW.index("/tiles/${tile.tile_id}/export_png")
    call = VIEW[i:i + 300]
    assert "JSON.stringify({ filename })" in call, call
