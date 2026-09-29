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


def test_every_chart_render_passes_its_reference():
    fn = _function(DASH, "appendMessage")
    assert "(extras && extras.chartRef) || null" in fn
    # history reload + recovery use the stored row; live responses use chart_ref
    assert DASH.count("chartRef: { conv_id: convId, ai_index: rowIdx") == 2
    assert DASH.count("ai_index: history.length - 1") == 2
    assert DASH.count("chartRef: data.chart_ref ? { chart_ref: data.chart_ref } : null") == 4
    assert "container._setChartHtml(newContent, data.chart_ref ? { chart_ref: data.chart_ref } : null)" in DASH


def test_the_tile_download_uses_the_tile_route_and_sends_no_markup():
    assert "export_plotly_png" not in VIEW
    i = VIEW.index("/tiles/${tile.tile_id}/export_png")
    call = VIEW[i:i + 300]
    assert "JSON.stringify({ filename })" in call, call
