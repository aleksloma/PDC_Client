"""The chart title no longer runs under the Plotly toolbar (BRB findings 2,
item 3) — the structural half.

The regression: a long chart title (centred, `title.automargin`) was drawn
underneath the modebar in the top-right corner of every chart frame, so the
title's end and the toolbar buttons overlapped. The fix lives in
`static/vendor/viewers.js`: `PDCViewers.setChartFrame(iframe, html)` inserts
ONE inline script marked `data-pdc-title-fit` into the chart document BEFORE
it registers the document with `POST /api/charts`; inside the chart frame the
script fits the title beside the toolbar and exposes
`window.__pdcTitleFit = {relayouts, done}`. Inserting it into a document that
already carries it is a no-op (a re-pinned / re-rendered chart must not
collect a second copy).

Pinned here: the marker exists, setChartFrame applies the fit to the document
it POSTs (before the fetch), the frame guarantees of tests/
test_rendering_isolation.py are not undone by the change, and — when `node`
is on PATH — executing viewers.js proves the posted document carries exactly
one marked script, also after a second pass over the posted document.

The browser half (title box vs toolbar box at three widths, no relayout
storm across six tiles) is tests/integration/
test_chart_title_toolbar_browser.py.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
VIEWERS = ROOT / "static" / "vendor" / "viewers.js"
MARKER = "data-pdc-title-fit"
FETCH_RE = re.compile(r"""fetch\(\s*['"]/api/charts['"]""")


def _src() -> str:
    return VIEWERS.read_text(encoding="utf-8")


def _body_from(src: str, start: int) -> str:
    """The brace-matched body starting at the first `{` after `start`."""
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return src[i:]


def _function_body(src: str, name: str) -> str:
    m = re.search(rf"function\s+{name}\s*\(", src)
    assert m, f"function {name} not found in viewers.js"
    return _body_from(src, m.end())


def _marker_carriers(src: str) -> set[str]:
    """Identifiers that carry the title-fit marker: named functions whose
    body contains it and variables whose initializer contains it."""
    names = set()
    for m in re.finditer(r"function\s+([A-Za-z_$][\w$]*)\s*\(", src):
        if m.group(1) == "setChartFrame":
            continue
        try:
            if MARKER in _function_body(src, m.group(1)):
                names.add(m.group(1))
        except AssertionError:
            continue
    for m in re.finditer(r"\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=", src):
        stmt_end = src.find(";\n", m.end())
        stmt = src[m.end(): stmt_end if stmt_end != -1 else m.end() + 4000]
        if MARKER in stmt:
            names.add(m.group(1))
    return names


def test_viewers_js_carries_the_title_fit_marker():
    assert MARKER in _src()


def test_set_chart_frame_applies_the_title_fit_before_registering():
    src = _src()
    body = _function_body(src, "setChartFrame")
    fm = FETCH_RE.search(body)
    assert fm, "setChartFrame must still register through POST /api/charts"
    before_fetch = body[:fm.start()]
    carriers = _marker_carriers(src)
    applied = MARKER in before_fetch or any(
        re.search(rf"\b{re.escape(n)}\b", before_fetch) for n in carriers)
    assert applied, (
        "setChartFrame must apply the title fit to the document BEFORE it is "
        f"POSTed (carriers found: {sorted(carriers)})")


def test_the_frame_guarantees_still_hold():
    src = _src()
    body = _function_body(src, "setChartFrame")
    sandbox_values = re.findall(
        r"""setAttribute\(\s*['"]sandbox['"]\s*,\s*['"]([^'"]*)['"]\s*\)""", body)
    assert sandbox_values == ["allow-scripts"], sandbox_values
    assert "srcdoc" not in src


# ---------------------------------------------------------------------------
# Executed: node runs viewers.js with a stubbed fetch and captures the POST.
# ---------------------------------------------------------------------------
_NODE_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const src = fs.readFileSync(process.argv[2], 'utf8');
const chartHtml = fs.readFileSync(process.argv[3], 'utf8');
const posted = [];
let n = 0;
const sandbox = {
  console: { warn: function () {}, log: function () {}, error: function () {} },
  setTimeout: setTimeout, clearTimeout: clearTimeout,
  fetch: function (url, opts) {
    posted.push({ url: url, body: JSON.parse(opts.body).html });
    n += 1;
    return Promise.resolve({ ok: true, status: 200,
      json: function () { return Promise.resolve({ url: '/charts/tok' + n }); } });
  },
  document: { createElement: function () { return {}; },
              querySelector: function () { return null; } },
};
sandbox.window = sandbox;
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
function frame() {
  return { attrs: {}, setAttribute: function (k, v) { this.attrs[k] = v; }, src: '' };
}
(async function () {
  const V = sandbox.window.PDCViewers;
  const f1 = frame();
  await V.setChartFrame(f1, chartHtml);
  const first = posted[0] ? posted[0].body : null;
  const f2 = frame();
  await V.setChartFrame(f2, first);
  const second = posted[1] ? posted[1].body : null;
  process.stdout.write(JSON.stringify({ first: first, second: second,
                                        src1: f1.src, sandbox1: f1.attrs.sandbox }));
})().catch(function (e) { process.stdout.write(JSON.stringify({ error: String(e) })); });
"""

_CHART_HTML = (
    "<html>\n<head><meta charset=\"utf-8\" /></head>\n<body>\n"
    "<div id=\"plotly-chart\" class=\"plotly-graph-div\"></div>\n"
    "<script type=\"text/javascript\">window.PLOTLYENV=window.PLOTLYENV || {};"
    "</script>\n</body>\n</html>"
)

_MARKED_SCRIPT_RE = re.compile(r"<script\b[^>]*\b" + MARKER + r"\b[^>]*>", re.I)


@pytest.fixture
def node_run(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    harness = tmp_path / "harness.js"
    harness.write_text(_NODE_HARNESS, encoding="utf-8")
    chart = tmp_path / "chart.html"
    chart.write_text(_CHART_HTML, encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(VIEWERS), str(chart)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout)
    assert "error" not in out, out
    return out


def test_node_the_posted_document_carries_one_title_fit_script(node_run):
    first = node_run["first"]
    assert isinstance(first, str)
    assert len(_MARKED_SCRIPT_RE.findall(first)) == 1, first[:2000]
    # The chart itself is still in the document.
    assert 'id="plotly-chart"' in first
    assert node_run["src1"] == "/charts/tok1"
    assert node_run["sandbox1"] == "allow-scripts"


def test_node_applying_the_fit_twice_inserts_it_once(node_run):
    second = node_run["second"]
    assert isinstance(second, str)
    assert len(_MARKED_SCRIPT_RE.findall(second)) == 1, second[:2000]
    assert second == node_run["first"]
