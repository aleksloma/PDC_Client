"""The chart title and the Plotly toolbar do not overlap, in a real browser
(BRB findings 2, item 3 — the browser half of tests/test_chart_title_toolbar.py).

The regression: a long, centred chart title ran underneath the modebar in the
top-right corner of the chart frame. The fix: `PDCViewers.setChartFrame`
(static/vendor/viewers.js) inserts one `data-pdc-title-fit` script into the
chart document; inside the frame it fits the title beside the toolbar and
publishes `window.__pdcTitleFit = {relayouts, done}`.

What only a browser can show:

* at frame widths ~380, ~730 and ~1400 px, for a ~90-character and a
  ~30-character title, the title text box (`.gtitle`) and the toolbar box
  (`.modebar`) do not intersect, the title lies horizontally inside the
  frame, and the toolbar still has its buttons;
* a page with six chart frames (a dashboard with six tiles) settles with at
  most ONE relayout per frame and every frame reporting `done`.

Self-contained: the documents are produced by `plot_utils._plotly_to_html`
and put into sandboxed frames by the REAL viewers.js `setChartFrame`; the
two server routes it talks to (`POST /api/charts`, `GET /charts/{token}`)
and the static files are answered by Playwright request interception on a
fake origin, with the chart route's real policy header
(`routes.charts._POLICY`). No account, chat or volume is touched.

Gated like the rest of `tests/integration/` (skipped unless `PDC_STACK_URL`
is set); skips with a reason when Playwright or a Chromium build is
unavailable.
"""
import json
import time
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
except Exception as _e:                      # pragma: no cover - env dependent
    sync_playwright = None
    _PLAYWRIGHT_ERROR = repr(_e)
else:
    _PLAYWRIGHT_ERROR = ""

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sync_playwright is None,
                       reason=f"playwright is not importable: {_PLAYWRIGHT_ERROR}"),
]

ROOT = Path(__file__).resolve().parents[2]
VIEWERS = ROOT / "static" / "vendor" / "viewers.js"
ORIGIN = "http://pdc-title-fit.test"
PLOTLY_PATH = "/static/vendor/plotly/plotly.min.js"

LONG_TITLE = ("Monthly New USD Deposit Volume vs Official USD/UZS Rate "
              "(2024 - 2026) by Region and Branch")
SHORT_TITLE = "Deposits by Region, 2024-2026"
WIDTHS = (380, 730, 1400)
FRAME_HEIGHT = 450
SETTLE_TIMEOUT_S = 20.0


def _plotly_bundle() -> bytes:
    local = ROOT / "static" / "vendor" / "plotly" / "plotly.min.js"
    if local.is_file():
        return local.read_bytes()
    import plotly
    return (Path(plotly.__file__).parent / "package_data" / "plotly.min.js").read_bytes()


def _chart_document(title: str) -> str:
    import pandas as pd
    import plotly.express as px

    import plot_utils
    df = pd.DataFrame({
        "month": [f"2025-{m:02d}" for m in range(1, 13)] * 2,
        "region": ["Tashkent"] * 12 + ["Samarkand"] * 12,
        "volume": [1200.5 + 37 * i for i in range(24)],
    })
    fig = px.line(df, x="month", y="volume", color="region", title=title)
    return plot_utils._plotly_to_html(fig)


def _chart_policy() -> str:
    from routes import charts
    return charts._POLICY


_HOST_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>title fit</title>
<style>body{margin:0} iframe{border:0;display:block;margin:0 0 8px 0}</style>
<script src="/static/vendor/viewers.js"></script></head>
<body><div id="frames"></div></body></html>"""


class _Server:
    """Answers the fake origin: the host page, viewers.js, the plotly bundle,
    POST /api/charts (stores the posted document) and GET /charts/{token}."""

    def __init__(self):
        self.docs = {}
        self.viewers = VIEWERS.read_text(encoding="utf-8")
        self.plotly = _plotly_bundle()
        self.policy = _chart_policy()

    def handle(self, route):
        req = route.request
        path = req.url[len(ORIGIN):].split("?")[0]
        if path in ("/", "/host.html"):
            return route.fulfill(status=200, content_type="text/html", body=_HOST_PAGE)
        if path == "/static/vendor/viewers.js":
            return route.fulfill(status=200, content_type="application/javascript",
                                 body=self.viewers)
        if path == PLOTLY_PATH:
            return route.fulfill(status=200, content_type="application/javascript",
                                 body=self.plotly)
        if path == "/api/charts" and req.method == "POST":
            html = json.loads(req.post_data or "{}").get("html") or ""
            token = f"t{len(self.docs) + 1}"
            self.docs[token] = html
            return route.fulfill(status=200, content_type="application/json",
                                 body=json.dumps({"url": f"/charts/{token}"}))
        if path.startswith("/charts/"):
            html = self.docs.get(path[len("/charts/"):])
            if html is None:
                return route.fulfill(status=404, content_type="text/plain", body="Not found")
            return route.fulfill(status=200, content_type="text/html; charset=utf-8",
                                 headers={"Content-Security-Policy": self.policy,
                                          "Cache-Control": "no-store"},
                                 body=html)
        return route.fulfill(status=404, content_type="text/plain", body="Not found")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def playwright_driver():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright_driver):
    errors = []
    for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            b = playwright_driver.chromium.launch(**kwargs)
        except Exception as e:               # pragma: no cover - env dependent
            errors.append(f"{kwargs or 'bundled'}: {type(e).__name__}")
            continue
        yield b
        b.close()
        return
    pytest.skip("no Chromium build can be launched: " + "; ".join(errors))


@pytest.fixture(scope="module")
def documents():
    return {"long": _chart_document(LONG_TITLE), "short": _chart_document(SHORT_TITLE)}


def _open_host(browser, viewport_width: int):
    server = _Server()
    page = browser.new_page(viewport={"width": max(viewport_width + 40, 600), "height": 900})
    page.route(f"{ORIGIN}/**", server.handle)
    page.goto(f"{ORIGIN}/host.html")
    page.wait_for_function("() => !!(window.PDCViewers && window.PDCViewers.setChartFrame)")
    return page


def _add_frames(page, html: str, widths) -> None:
    page.evaluate(
        """([html, widths, height]) => {
            const box = document.getElementById('frames');
            return Promise.all(widths.map((w) => {
              const f = document.createElement('iframe');
              f.style.width = w + 'px';
              f.style.height = height + 'px';
              box.appendChild(f);
              return window.PDCViewers.setChartFrame(f, html);
            }));
        }""",
        [html, list(widths), FRAME_HEIGHT])


def _chart_frames(page, expected: int):
    deadline = time.monotonic() + SETTLE_TIMEOUT_S
    while time.monotonic() < deadline:
        frames = [f for f in page.frames if "/charts/" in (f.url or "")]
        if len(frames) >= expected:
            return frames
        page.wait_for_timeout(100)
    pytest.fail(f"expected {expected} chart frames, found "
                f"{[f.url for f in page.frames]}")


def _wait_done(frame) -> None:
    frame.wait_for_function(
        "() => !!document.querySelector('.gtitle') && !!document.querySelector('.modebar')"
        " && window.__pdcTitleFit && window.__pdcTitleFit.done === true",
        # Interval polling: requestAnimationFrame (the default "raf") does not
        # run in an offscreen cross-origin frame, e.g. the sixth tile below
        # the viewport.
        polling=100, timeout=SETTLE_TIMEOUT_S * 1000)


def _geometry(frame) -> dict:
    return frame.evaluate(
        """() => {
            const r = (el) => { const b = el.getBoundingClientRect();
                return {left: b.left, top: b.top, right: b.right, bottom: b.bottom,
                        width: b.width, height: b.height}; };
            const t = document.querySelector('.gtitle');
            const m = document.querySelector('.modebar');
            return {title: t ? r(t) : null, modebar: m ? r(m) : null,
                    buttons: document.querySelectorAll('.modebar-btn').length,
                    width: document.documentElement.clientWidth,
                    fit: window.__pdcTitleFit || null};
        }""")


def _intersect(a: dict, b: dict) -> bool:
    return (a["left"] < b["right"] and b["left"] < a["right"]
            and a["top"] < b["bottom"] and b["top"] < a["bottom"])


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("width", WIDTHS)
@pytest.mark.parametrize("which", ["long", "short"])
def test_title_and_toolbar_do_not_overlap(browser, documents, which, width):
    page = _open_host(browser, width)
    try:
        _add_frames(page, documents[which], [width])
        frame = _chart_frames(page, 1)[0]
        _wait_done(frame)
        g = _geometry(frame)
        assert g["title"] and g["modebar"], g
        assert g["title"]["width"] > 0 and g["title"]["height"] > 0, g
        assert not _intersect(g["title"], g["modebar"]), (
            f"title box overlaps the toolbar at {width}px: {g}")
        assert g["title"]["left"] >= -0.5, g
        assert g["title"]["right"] <= g["width"] + 0.5, g
        assert g["buttons"] > 0, g
    finally:
        page.close()


def test_six_tiles_settle_without_a_relayout_storm(browser, documents):
    page = _open_host(browser, 1400)
    try:
        widths = [440, 440, 440, 660, 660, 1300]
        page.evaluate("() => { document.getElementById('frames').style.display = 'block'; }")
        # Mixed long and short titles, like a real dashboard.
        for i, w in enumerate(widths):
            _add_frames(page, documents["long" if i % 2 == 0 else "short"], [w])
        frames = _chart_frames(page, len(widths))
        for f in frames:
            _wait_done(f)
        page.wait_for_timeout(1500)          # let any late relayout land
        fits = [_geometry(f)["fit"] for f in frames]
        for fit in fits:
            assert fit is not None, fits
            assert fit.get("done") is True, fits
            assert isinstance(fit.get("relayouts"), int), fits
            assert fit["relayouts"] <= 1, fits
    finally:
        page.close()


def test_the_harness_document_reaches_the_frame_through_set_chart_frame(browser, documents):
    """Control: the frames really are filled by viewers.js (the POSTed
    document carries the title-fit script), so a clean geometry result above
    is not a frame that was never fitted."""
    page = _open_host(browser, 730)
    try:
        captured = []
        page.on("request", lambda r: captured.append(r.post_data)
                if r.url.endswith("/api/charts") and r.method == "POST" else None)
        _add_frames(page, documents["long"], [730])
        _chart_frames(page, 1)
        assert captured, "setChartFrame posted nothing"
        html = json.loads(captured[0])["html"]
        assert "data-pdc-title-fit" in html
    finally:
        page.close()


# ---------------------------------------------------------------------------
# No visible title move (review follow-up). On a chart whose toolbar has no
# buttons (pie, sunburst, treemap through `_plotly_to_html`) — and on a plain
# bar chart — the title must never be SEEN in two places: whenever `.gtitle`
# is visible its top equals the final top (±1 px). A fit that first paints
# the title and then relayouts it would show a jump.
# ---------------------------------------------------------------------------
PROBE_INTERVAL_MS = 50
PROBE_WINDOW_S = 2.5


def _nobutton_document(kind: str) -> str:
    import pandas as pd
    import plotly.express as px

    import plot_utils
    df = pd.DataFrame({"region": ["Tashkent", "Tashkent", "Samarkand", "Bukhara"],
                       "branch": ["T1", "T2", "S1", "B1"],
                       "volume": [1200, 800, 650, 400]})
    if kind == "pie":
        fig = px.pie(df, names="branch", values="volume", title=LONG_TITLE)
    elif kind == "sunburst":
        fig = px.sunburst(df, path=["region", "branch"], values="volume", title=LONG_TITLE)
    elif kind == "treemap":
        fig = px.treemap(df, path=["region", "branch"], values="volume", title=LONG_TITLE)
    else:
        fig = px.bar(df, x="branch", y="volume", title=LONG_TITLE)
    return plot_utils._plotly_to_html(fig)


_TITLE_PROBE = """() => {
    const t = document.querySelector('.gtitle');
    if (!t) return {visible: false, top: null};
    const plot = t.closest('.js-plotly-plot') || document.body;
    for (let el = t; el; el = el.parentElement) {
        const cs = getComputedStyle(el);
        if (cs.visibility === 'hidden' || cs.display === 'none'
            || parseFloat(cs.opacity) === 0) return {visible: false, top: null};
        if (el === document.documentElement) break;
    }
    const pcs = getComputedStyle(plot);
    if (pcs.visibility === 'hidden' || parseFloat(pcs.opacity) === 0)
        return {visible: false, top: null};
    const b = t.getBoundingClientRect();
    if (!(b.width > 0 && b.height > 0)) return {visible: false, top: null};
    return {visible: true, top: b.top};
}"""


def _probe(frame):
    try:
        return frame.evaluate(_TITLE_PROBE)
    except Exception:                         # navigating / not ready yet
        return None


@pytest.mark.parametrize("kind", ["pie", "sunburst", "treemap", "bar"])
def test_the_title_is_never_seen_in_two_places(browser, kind):
    html = _nobutton_document(kind)
    page = _open_host(browser, 730)
    try:
        _add_frames(page, html, [730])
        frame = _chart_frames(page, 1)[0]
        samples = []
        end = time.monotonic() + PROBE_WINDOW_S
        while time.monotonic() < end:
            s = _probe(frame)
            if s and s.get("visible"):
                samples.append(s["top"])
            page.wait_for_timeout(PROBE_INTERVAL_MS)
        frame.wait_for_function(
            "() => !!document.querySelector('.gtitle')"
            " && window.__pdcTitleFit && window.__pdcTitleFit.done === true",
            polling=100, timeout=SETTLE_TIMEOUT_S * 1000)
        final = _probe(frame)
        assert final and final.get("visible"), f"title not visible at the end: {final}"
        # Not a blind result: the title was sampled while the frame settled.
        assert samples, "the title was never sampled during the probe window"
        moved = [t for t in samples if abs(t - final["top"]) > 1]
        assert not moved, (f"{kind}: title seen at {sorted(set(moved))} before settling "
                           f"at {final['top']}")
        fit = frame.evaluate("() => window.__pdcTitleFit")
        assert fit.get("done") is True, fit
        assert isinstance(fit.get("relayouts"), int) and fit["relayouts"] <= 1, fit
    finally:
        page.close()
