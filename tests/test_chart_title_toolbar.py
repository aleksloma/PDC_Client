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


# ---------------------------------------------------------------------------
# Executed: the title-fit script itself, with a top horizontal legend
# (P29 finding F8, task C3).
#
# The regression: the brain's chart skill places the legend above the plot
# (`legend=dict(orientation='h', yanchor='bottom', y=1.02)`), while the fit
# reserved the top margin for the toolbar and the title only — so the legend
# was drawn over the fitted title. The contract: when the figure has a
# horizontal legend at the top, the reserved top margin grows by the legend's
# drawn height and the legend is placed UNDER the title block (and above the
# plot area), in the same single relayout; a figure without such a legend
# gets exactly today's relayout.
#
# How: node serializes the script out of `PDCViewers.fitChartTitle(doc)` (the
# exact text that lands in the chart document), runs it in a fresh vm context
# per scenario against a fake chart div + fake `Plotly.relayout` (which
# applies the update to `gd.layout` and fires `plotly_afterplot` like
# plotly.js), and reports every relayout call. Geometry of the fake frame:
# 800 x 450 px, toolbar bottom 30 px, a one-line title box 20 px high (font
# 17 px), base margins t=50 / b=40, canvas text width = 8 px per character.
# The legend's drawn height (24 px) is offered every way a fit could read it:
# the `.legend` element's getBBox / getBoundingClientRect / height attribute
# (and its `.bg` child), and `_fullLayout.legend._height` / `_effHeight`.
# Today's fit on it: title.pad.t = 38, margin.t = max(50, 38 + 20 + 8) = 66.
# ---------------------------------------------------------------------------
_FIT_HARNESS = r"""
const fs = require('fs');
const vm = require('vm');
const viewersSrc = fs.readFileSync(process.argv[2], 'utf8');
const chartHtml = fs.readFileSync(process.argv[3], 'utf8');
const scenarios = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

// 1) The script text exactly as fitChartTitle puts it into the document.
const page = { console: { warn() {}, log() {}, error() {} },
               setTimeout, clearTimeout, fetch() { return new Promise(function () {}); },
               document: { createElement() { return {}; }, querySelector() { return null; } } };
page.window = page;
vm.createContext(page);
vm.runInContext(viewersSrc, page);
const fitted = page.window.PDCViewers.fitChartTitle(chartHtml);
const again = page.window.PDCViewers.fitChartTitle(fitted);
const open = fitted.search(/<script\b[^>]*data-pdc-title-fit[^>]*>/i);
if (open < 0) { process.stdout.write(JSON.stringify({ error: 'no marked script' })); process.exit(0); }
const bodyStart = fitted.indexOf('>', open) + 1;
const bodyEnd = fitted.indexOf('</script>', bodyStart);
const scriptSrc = fitted.slice(bodyStart, bodyEnd);

const W = 800, H = 450, TOOLBAR_BOTTOM = 30, TITLE_H = 20, FONT = 17;

function rect(top, height, left, width) {
  return { top: top, bottom: top + height, left: left, right: left + width,
           width: width, height: height, x: left, y: top };
}
function el(r, extra) {
  return Object.assign({
    getBoundingClientRect() { return r; },
    getBBox() { return { x: r.left, y: r.top, width: r.width, height: r.height }; },
    getAttribute(n) {
      if (n === 'height') return String(r.height);
      if (n === 'width') return String(r.width);
      if (n === 'transform') return 'translate(' + r.left + ',' + r.top + ')';
      return null;
    },
    querySelector() { return null; },
    querySelectorAll() { return []; },
    style: {}, classList: { contains() { return false; } },
  }, extra || {});
}
function setPath(obj, path, value) {
  const parts = path.split('.');
  let o = obj;
  for (let i = 0; i < parts.length - 1; i++) {
    if (o[parts[i]] === null || typeof o[parts[i]] !== 'object') o[parts[i]] = {};
    o = o[parts[i]];
  }
  o[parts[parts.length - 1]] = value;
}
function apply(target, upd) {
  Object.keys(upd).forEach(function (k) {
    const v = upd[k];
    if (k.indexOf('.') === -1 && v && typeof v === 'object' && !Array.isArray(v)) {
      if (!target[k] || typeof target[k] !== 'object') target[k] = {};
      apply(target[k], v);
    } else {
      setPath(target, k, v);
    }
  });
}

function runScenario(sc) {
  return new Promise(function (resolve) {
    const calls = [];
    const listeners = {};
    const lg = sc.legend;   // null or {layout, height, width, drawn_top}
    const legendRect = lg ? rect(lg.drawn_top, lg.height, 400, lg.width) : null;
    const legendBg = lg ? el(legendRect) : null;
    const legendEl = lg ? el(legendRect, {
      querySelector() { return legendBg; },
      querySelectorAll() { return [legendBg]; },
    }) : null;
    const modebarBtn = el(rect(4, 26, 600, 26));
    const modebar = el(rect(4, TOOLBAR_BOTTOM - 4, 560, 230), {
      querySelector(sel) { return /modebar-btn/.test(sel) ? modebarBtn : null; },
      querySelectorAll(sel) { return /modebar-btn/.test(sel) ? [modebarBtn] : []; },
    });
    const modebarContainer = el(rect(0, TOOLBAR_BOTTOM, 0, W));
    const titleEl = el(rect(40, TITLE_H, 300, 200));
    const layout = JSON.parse(JSON.stringify(sc.layout));
    const fullLegend = lg ? Object.assign({ x: 0, y: 1, xanchor: 'left', yanchor: 'auto',
                                            yref: 'paper', orientation: 'v' },
                                          JSON.parse(JSON.stringify(lg.full)),
                                          { _height: lg.height, _width: lg.width,
                                            _effHeight: lg.height }) : undefined;
    const gd = {
      id: 'plotly-chart', layout: layout,
      data: [{ type: 'bar' }, { type: 'scatter' }],
      _context: { displayModeBar: true },
      _fullLayout: {
        title: { text: layout.title.text, x: 0.5, xanchor: 'center', automargin: true,
                 font: { size: FONT, family: 'Open Sans' } },
        margin: { t: 50, b: 40, l: 50, r: 20 },
        width: W, height: H, showlegend: !!lg,
        legend: fullLegend,
        _size: { t: 50, b: 40, l: 50, r: 20, w: W - 70, h: H - 90 },
      },
      clientWidth: W, clientHeight: H, offsetWidth: W, offsetHeight: H,
      getBoundingClientRect() { return rect(0, H, 0, W); },
      on(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
      querySelector(sel) {
        if (/legend/.test(sel)) return legendEl;
        if (/modebar-btn/.test(sel)) return modebarBtn;
        if (/modebar-container/.test(sel)) return modebarContainer;
        if (/modebar/.test(sel)) return modebar;
        if (/gtitle/.test(sel)) return titleEl;
        return null;
      },
      querySelectorAll(sel) { const e = this.querySelector(sel); return e ? [e] : []; },
    };
    const Plotly = {
      relayout(g, upd) {
        calls.push(JSON.parse(JSON.stringify(upd)));
        apply(g.layout, upd);
        const fl = {};
        Object.keys(upd).forEach(function (k) {
          if (/^(margin|legend|title)([.]|$)/.test(k)) fl[k] = upd[k];
        });
        apply(g._fullLayout, fl);
        return new Promise(function (res) {
          setTimeout(function () {
            res(g);
            setTimeout(function () {
              (listeners.plotly_afterplot || []).forEach(function (f) { f(); });
            }, 5);
          }, 0);
        });
      },
    };
    const head = { appendChild(n) { n.parentNode = head; return n; },
                   removeChild(n) { n.parentNode = null; return n; } };
    const doc = {
      readyState: 'complete', head: head, documentElement: head,
      createElement(tag) {
        if (tag === 'canvas') {
          return { getContext() { return { font: '',
            measureText(s) { return { width: 8 * String(s).length }; } }; } };
        }
        return { textContent: '', parentNode: null, style: {} };
      },
      getElementById(id) { return id === 'plotly-chart' ? gd : null; },
      querySelector(sel) { return /plotly-graph-div|plotly-chart/.test(sel) ? gd : null; },
      addEventListener() {},
    };
    const frame = { console: { warn() {}, log() {}, error() {} },
                    setTimeout, clearTimeout, performance, document: doc, Plotly: Plotly,
                    addEventListener() {}, innerWidth: W, innerHeight: H };
    frame.window = frame;
    vm.createContext(frame);
    try { vm.runInContext(scriptSrc, frame); }
    catch (e) { resolve({ error: String(e) }); return; }
    const t0 = Date.now();
    (function waitDone() {
      const st = frame.__pdcTitleFit || {};
      if (st.done || Date.now() - t0 > 3000) {
        // A later afterplot (plotly fires one after every relayout, and on
        // re-draws): the fit must not relayout again.
        setTimeout(function () {
          (listeners.plotly_afterplot || []).forEach(function (f) { f(); });
          setTimeout(function () {
            const s = frame.__pdcTitleFit || {};
            resolve({ calls: calls, relayouts: s.relayouts, done: s.done,
                      layout: JSON.parse(JSON.stringify(gd.layout)) });
          }, 150);
        }, 60);
        return;
      }
      setTimeout(waitDone, 20);
    })();
  });
}

(async function () {
  const results = {};
  for (const sc of scenarios) results[sc.name] = await runScenario(sc);
  process.stdout.write(JSON.stringify({ idempotent: again === fitted, results: results }));
  process.exit(0);
})().catch(function (e) {
  process.stdout.write(JSON.stringify({ error: String(e) }));
  process.exit(0);
});
"""

FIT_W, FIT_H = 800, 450
FIT_TOOLBAR_BOTTOM = 30
FIT_TITLE_H = 20
FIT_FONT = 17
FIT_MARGIN_B = 40
FIT_CAP_SHIFT = 0.7
FIT_LINE_SPACING = 1.3
LEGEND_H = 24
LEGEND_W = 300

SHORT_FIT_TITLE = "Revenue and conversion rate by month"
# 8 px per character, available width 2 * 0.5 * 800 - 2 * 12 = 776 px
# → ~97 characters per line: this title wraps onto two lines.
LONG_FIT_TITLE = ("Monthly revenue in USD and the share of visits that converted, "
                  "by region and sales channel, for the twenty-four months "
                  "ending in October 2026")

# What the fit sends today (no legend): exactly these keys.
_TODAY_KEYS = {"title.text", "title.yref", "title.y", "title.yanchor",
               "title.pad.t", "title.automargin", "margin.t"}

TOP_LEGENDS = {
    # The brain chart skill's own placement (analytics_viz.yaml).
    "skill_default": {"orientation": "h", "yanchor": "bottom", "y": 1.02},
    "right_aligned": {"orientation": "h", "yanchor": "bottom", "y": 1.02,
                      "xanchor": "right", "x": 1},
    "at_plot_top": {"orientation": "h", "yanchor": "bottom", "y": 1.0},
    "well_above": {"orientation": "h", "y": 1.12},
}

NOT_TOP_LEGENDS = {
    "vertical_right": {"orientation": "v", "x": 1.02, "y": 1, "yanchor": "top"},
    "horizontal_bottom": {"orientation": "h", "yanchor": "top", "y": -0.2},
}


def _scenario(name, title, legend_layout=None, *, drawn=True):
    lay = {"title": {"text": title, "x": 0.5, "xanchor": "center", "automargin": True},
           "margin": {"l": 50, "r": 20, "t": 50, "b": 40}}
    sc = {"name": name, "layout": lay, "legend": None}
    if legend_layout is not None:
        lay["legend"] = dict(legend_layout)
        lay["showlegend"] = bool(drawn)
        if drawn:
            # `_fullLayout.legend` as plotly.js 2.35 fills it: a horizontal
            # legend without a yanchor gets "top" (no range slider).
            full = dict(legend_layout)
            if full.get("orientation") == "h":
                full.setdefault("yanchor", "top")
                full.setdefault("x", 0)
                full.setdefault("xanchor", "left")
            # Drawn where it lands today: over the fitted title.
            sc["legend"] = {"layout": legend_layout, "full": full, "height": LEGEND_H,
                            "width": LEGEND_W, "drawn_top": 40}
    return sc


def _scenarios():
    out = [_scenario("no_legend", SHORT_FIT_TITLE),
           _scenario("no_legend_long", LONG_FIT_TITLE),
           _scenario("long_top_legend", LONG_FIT_TITLE, TOP_LEGENDS["skill_default"]),
           # A top legend in the layout but hidden (showlegend False, nothing drawn).
           _scenario("hidden_top_legend", SHORT_FIT_TITLE, TOP_LEGENDS["skill_default"],
                     drawn=False)]
    for k, lg in TOP_LEGENDS.items():
        out.append(_scenario("top_" + k, SHORT_FIT_TITLE, lg))
    for k, lg in NOT_TOP_LEGENDS.items():
        out.append(_scenario("not_top_" + k, SHORT_FIT_TITLE, lg))
    return out


@pytest.fixture(scope="module")
def fit_run(tmp_path_factory):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    d = tmp_path_factory.mktemp("titlefit")
    harness = d / "fit_harness.js"
    harness.write_text(_FIT_HARNESS, encoding="utf-8")
    chart = d / "chart.html"
    chart.write_text(_CHART_HTML, encoding="utf-8")
    scen = d / "scenarios.json"
    scen.write_text(json.dumps(_scenarios()), encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(VIEWERS), str(chart), str(scen)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout)
    assert "error" not in out, out
    for name, res in out["results"].items():
        assert "error" not in res, (name, res)
    return out


def _flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flat(v, key))
        else:
            out[key] = v
    return out


def _only_call(res):
    assert res["done"] is True, res
    assert res["relayouts"] == 1, res
    assert len(res["calls"]) == 1, res["calls"]
    return _flat(res["calls"][0])


def _title_block_bottom(pad_t, lines):
    """Bottom of the title block in container px, as the fit itself accounts
    for it (pad + one-line box + one line step per extra line; a multi-line
    title's pad includes the cap shift, so it is taken back out)."""
    h = FIT_TITLE_H + (lines - 1) * FIT_LINE_SPACING * FIT_FONT
    shift = FIT_CAP_SHIFT * FIT_FONT if lines > 1 else 0
    return pad_t - shift + h


def _legend_box(flat, legend_layout, margin_t):
    """(top, bottom) of the legend in container px from the relayout values
    (falling back to the figure's own legend settings). Paper y is measured
    over the plot area of the NEW top margin; container y over the frame."""
    def get(key, default=None):
        if "legend." + key in flat:
            return flat["legend." + key]
        return legend_layout.get(key, default)
    y = get("y")
    assert isinstance(y, (int, float)), f"legend y not set: {flat}"
    yref = get("yref", "paper")
    yanchor = get("yanchor", "auto")
    if yanchor == "auto":
        yanchor = "top" if y >= 2 / 3 else ("bottom" if y <= 1 / 3 else "middle")
    if yref == "container":
        py = (1 - y) * FIT_H
    else:
        plot_h = FIT_H - margin_t - FIT_MARGIN_B
        py = margin_t + (1 - y) * plot_h
    if yanchor == "bottom":
        top = py - LEGEND_H
    elif yanchor == "middle":
        top = py - LEGEND_H / 2
    else:
        top = py
    return top, top + LEGEND_H


def test_fit_marker_is_idempotent_on_a_legend_document(fit_run):
    """fitChartTitle over its own output inserts nothing more."""
    assert fit_run["idempotent"] is True


@pytest.mark.parametrize("name", ["no_legend", "hidden_top_legend",
                                  *("not_top_" + k for k in NOT_TOP_LEGENDS)])
def test_fit_without_a_top_legend_is_unchanged(fit_run, name):
    """No top horizontal legend → exactly today's relayout: the same seven
    keys, pad 38, margin 66, nothing about the legend."""
    flat = _only_call(fit_run["results"][name])
    assert set(flat) == _TODAY_KEYS, sorted(flat)
    assert flat["title.pad.t"] == 38
    assert flat["margin.t"] == 66
    assert flat["title.text"] == SHORT_FIT_TITLE


def test_fit_long_title_without_legend_is_unchanged(fit_run):
    flat = _only_call(fit_run["results"]["no_legend_long"])
    assert set(flat) == _TODAY_KEYS, sorted(flat)
    assert flat["title.text"].count("<br>") == 1, flat["title.text"]
    # pad = ceil(30 + 8 + 0.7 * 17) = 50; margin = ceil(50 + 20 + 1.3 * 17 + 8) = 101
    assert flat["title.pad.t"] == 50
    assert flat["margin.t"] == 101


@pytest.mark.parametrize("key", sorted(TOP_LEGENDS))
def test_fit_reserves_the_top_legend_height(fit_run, key):
    base = _only_call(fit_run["results"]["no_legend"])
    flat = _only_call(fit_run["results"]["top_" + key])
    # The title itself stays where the toolbar rule puts it.
    assert flat["title.pad.t"] == base["title.pad.t"]
    assert flat["title.text"] == base["title.text"]
    assert flat["margin.t"] >= base["margin.t"] + LEGEND_H, (flat["margin.t"], base["margin.t"])


@pytest.mark.parametrize("key", sorted(TOP_LEGENDS))
def test_fit_places_the_top_legend_under_the_title(fit_run, key):
    flat = _only_call(fit_run["results"]["top_" + key])
    margin_t = flat["margin.t"]
    top, bottom = _legend_box(flat, TOP_LEGENDS[key], margin_t)
    title_bottom = _title_block_bottom(flat["title.pad.t"], 1)
    assert top >= title_bottom - 1, (
        f"legend top {top:.1f}px is above the title block bottom {title_bottom:.1f}px")
    assert bottom <= margin_t + 1, (
        f"legend bottom {bottom:.1f}px runs into the plot area (top margin {margin_t}px)")


def test_fit_places_the_top_legend_under_a_wrapped_title(fit_run):
    base = _only_call(fit_run["results"]["no_legend_long"])
    flat = _only_call(fit_run["results"]["long_top_legend"])
    assert flat["title.text"] == base["title.text"]
    assert flat["title.pad.t"] == base["title.pad.t"]
    assert flat["margin.t"] >= base["margin.t"] + LEGEND_H
    top, bottom = _legend_box(flat, TOP_LEGENDS["skill_default"], flat["margin.t"])
    assert top >= _title_block_bottom(flat["title.pad.t"], 2) - 1, top
    assert bottom <= flat["margin.t"] + 1, bottom


@pytest.mark.parametrize("key", sorted(TOP_LEGENDS))
def test_fit_with_a_top_legend_keeps_its_horizontal_placement(fit_run, key):
    """One relayout (also after a later afterplot); the legend stays
    horizontal and keeps the figure's own x / xanchor."""
    res = fit_run["results"]["top_" + key]
    flat = _only_call(res)
    assert flat.get("legend.orientation", "h") == "h"
    lg = TOP_LEGENDS[key]
    for k in ("x", "xanchor"):
        if k in lg:
            assert res["layout"]["legend"].get(k) == lg[k], res["layout"]["legend"]
