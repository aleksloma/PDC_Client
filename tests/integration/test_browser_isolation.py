"""Chart and table markup, rendered in a real browser, cannot act as the viewer.

What only a browser can show: a chart document runs in a frame with an
opaque origin (it cannot read the viewer's cookie, touch the parent page or
send a request as the viewer), a styled table is inert markup by the time it
reaches the page, a real plotly chart still renders offline with hover inside
that frame, and the pages raise no Content-Security-Policy violation of their
own.

Chart frames load their document from `/charts/{token}` (the page registers
the HTML with `POST /api/charts`); frames are selected by that URL path. That
route is the only one whose policy grants `'unsafe-eval'` — the page policy
never does — and a WebGL chart (`scattergl`) renders in it under a software
GL backend with no violation in either document.

Setup, all through the running stack as a normal user would do it:

* owner A (the package's `account`/`session`) pins three tiles WITHOUT code
  to a dashboard — a chart tile and a table tile whose markup is `PAYLOAD`,
  and a real plotly chart — and shares it with recipient B (a second
  throwaway account). A second dashboard holds only the real chart.
* B opens the dashboard in headless Chromium.
* an AI history row carrying a real plotly document and `PAYLOAD` as its
  `styled_html` is seeded inside the web container on A's chat; A opens it
  at `/c/{conv_id}`; the PDF and PPTX reports render for that conversation.

"No request as the viewer" is observed on the SERVER: the number of
`GET /auth/me` lines the web container logs while the page with the markup
loads must equal the number for a control page without it (both pages call
`/auth/me` once for the profile menu). Browser-side request events also fire
for requests a policy blocks, so they cannot prove a request was not sent.

Gated like the rest of `tests/integration/`: skipped unless `PDC_STACK_URL`
is set; the report case also needs `PDC_STACK_BRAIN`; everything skips with a
reason when Playwright or docker is unavailable. Everything created is
removed: the dashboards through the API, the accounts and chats by the
package teardown.
"""
import re
import secrets
import subprocess
import time
from urllib.parse import urlparse

import httpx
import pytest

from .conftest import (REQUEST_TIMEOUT_S, SESSION_COOKIE, WEB_CONTAINER,
                       docker_available, precreate_account, seed_history_row)

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

PAYLOAD = ("<script>parent.document.title='x'</script>"
           "<img src=x onerror=\"fetch('/auth/me')\">")
HOSTILE_TITLE = "x"
AUTH_ME_LINE = re.compile(r'"GET /auth/me[ ?]')
CSP_TEXT = "Content-Security-Policy"
CDN_PLOTLY_HOST = "cdn.plot.ly"
SETTLE_S = 2.5
PAGE_TIMEOUT_MS = 60_000
PDF_MAGIC = b"%PDF"
PPTX_MAGIC = b"PK"
CHART_PATH_PREFIX = "/charts/"
WEBGL_UNSUPPORTED_TEXT = "WebGL is not supported"
SOFTWARE_GL_ARGS = ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
OVERLAY_TABLE = ('<div class="dash-larger-modal"><table id="T_a"><tr><td>x</td></tr>'
                 '</table></div>')

# Collects `securitypolicyviolation` events in every document, frames too.
_VIOLATION_HOOK = """
(() => {
  window.__cspViolations = [];
  document.addEventListener('securitypolicyviolation', (e) => {
    window.__cspViolations.push(e.violatedDirective + ' ' + (e.blockedURI || ''));
  });
})();
"""
_COOKIE_PROBE = """() => {
  try { return 'VALUE:' + document.cookie; } catch (e) { return 'THROW:' + e.name; }
}"""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _real_plotly_document() -> str:
    import plotly.graph_objects as go

    import plot_utils
    return plot_utils._plotly_to_html(go.Figure(go.Bar(x=["a", "b", "c"], y=[3, 1, 2])))


def _log_anchor() -> str:
    """A fixed `--since` anchor: counts taken against it only ever grow, so a
    before/after difference is immune to a small host/VM clock skew."""
    return str(int(time.time()) - 120)


def _auth_me_hits(anchor: str) -> int:
    result = subprocess.run(["docker", "logs", "--since", anchor, WEB_CONTAINER],
                            capture_output=True, text=True, timeout=120)
    return len(AUTH_ME_LINE.findall((result.stdout or "") + (result.stderr or "")))


def _login_cookie(base_url: str, email: str, password: str) -> str:
    with httpx.Client(base_url=base_url, follow_redirects=False,
                      timeout=REQUEST_TIMEOUT_S) as client:
        r = client.post("/auth/login", data={"email": email, "password": password})
        assert r.status_code in (302, 303), (r.status_code, r.text[:300])
        cookie = client.cookies.get(SESSION_COOKIE)
    assert cookie, "no session cookie after sign-in"
    return cookie


def _new_page(browser, base_url: str, cookie: str):
    context = browser.new_context()
    context.add_cookies([{"name": SESSION_COOKIE, "value": cookie, "url": base_url}])
    context.add_init_script(_VIOLATION_HOOK)
    page = context.new_page()
    seen = {"console": [], "requests": []}
    page.on("console", lambda msg: seen["console"].append(msg.text))
    page.on("request", lambda req: seen["requests"].append(req.url))
    return context, page, seen


def _child_frames(page):
    return [f for f in page.frames if f != page.main_frame]


def _is_chart_url(url: str) -> bool:
    try:
        return urlparse(url or "").path.startswith(CHART_PATH_PREFIX)
    except Exception:
        return False


def _chart_frames(page):
    return [f for f in _child_frames(page) if _is_chart_url(f.url)]


def _plotly_frame(page):
    for frame in _child_frames(page):
        try:
            if frame.locator(".main-svg").count() >= 1:
                return frame
        except Exception:
            continue
    return None


def _wait_for_plotly(page):
    deadline = time.time() + PAGE_TIMEOUT_MS / 1000
    while time.time() < deadline:
        frame = _plotly_frame(page)
        if frame is not None:
            return frame
        page.wait_for_timeout(250)
    return None


def _assert_frames_isolated(page):
    frames = _chart_frames(page)
    assert frames, "no chart frame on the page"
    for frame in frames:
        cookie = frame.evaluate(_COOKIE_PROBE)
        assert cookie in ("VALUE:",) or cookie.startswith("THROW:"), cookie
        origin = frame.evaluate("() => self.origin")
        assert origin == "null", origin


def _assert_hover(page, frame):
    # A mouse move only hovers what is on screen: bring the chart's frame into
    # the viewport first (a later grid tile can sit below the fold).
    frame.frame_element().scroll_into_view_if_needed()
    bar = frame.locator(".bars .point path").first
    box = bar.bounding_box()
    assert box, "no bar to hover"
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.move(cx, cy)
    page.mouse.move(cx + 1, cy + 1)
    frame.wait_for_selector(".hoverlayer .hovertext", timeout=10_000)
    assert frame.locator(".hoverlayer .hovertext").count() >= 1


def _violations(page) -> list:
    out = []
    for frame in page.frames:
        try:
            out += frame.evaluate("() => window.__cspViolations || []")
        except Exception:
            continue
    return out


def _frame_violations(frame) -> list:
    """The frame's own list; the hook must have been installed there, or a
    clean result would be a blind one."""
    installed = frame.evaluate("() => Array.isArray(window.__cspViolations)")
    assert installed, "the violation listener is not installed in the chart frame"
    return frame.evaluate("() => window.__cspViolations")


def _csp_console(seen) -> list:
    return [m for m in seen["console"] if CSP_TEXT in m or "Refused to" in m]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def playwright_driver():
    # ONE driver per module: a second concurrent sync_playwright() refuses to
    # start inside the first one's event loop.
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright_driver):
    b = playwright_driver.chromium.launch()
    yield b
    b.close()


@pytest.fixture(scope="module")
def webgl_browser(playwright_driver):
    """Headless Chromium with a software GL backend, so a WebGL trace can
    render where the machine has no GPU."""
    b = playwright_driver.chromium.launch(args=SOFTWARE_GL_ARGS)
    yield b
    b.close()


@pytest.fixture(scope="module")
def recipient(base_url, session_scoped_extra_emails) -> dict:
    email = f"integration-rcpt-{secrets.token_hex(4)}@example.invalid"
    password = f"pw-{secrets.token_hex(8)}"
    session_scoped_extra_emails.append(email)
    if not precreate_account(email, password):
        pytest.skip("docker is not on PATH: the recipient account cannot be created "
                    "without a brain-relayed welcome mail")
    return {"email": email, "password": password,
            "cookie": _login_cookie(base_url, email, password)}


@pytest.fixture(scope="module")
def plotly_html() -> str:
    html = _real_plotly_document()
    assert html.lstrip().startswith("<") and "plotly" in html
    return html


@pytest.fixture(scope="module")
def dashboards(session, chat, recipient, plotly_html):
    """Two dashboards owned by A and shared with B: `hostile` (payload chart,
    payload table, real chart) and `clean` (real chart only)."""
    created = []

    def make(name, tiles):
        r = session.post("/api/dashboards", json={"name": name})
        assert r.status_code == 200, r.text[:300]
        dash_id = r.json()["dash_id"]
        created.append(dash_id)
        for tile in tiles:
            r = session.post(f"/api/dashboards/{dash_id}/tiles", json={"chat_id": chat, **tile})
            assert r.status_code == 200, (tile.get("kind"), r.status_code, r.text[:300])
        r = session.post(f"/api/dashboards/{dash_id}/share",
                         json={"emails": [recipient["email"]]})
        assert r.status_code == 200, r.text[:300]
        return dash_id, name

    real_chart = {"kind": "chart", "image_base64": plotly_html, "is_plotly": True,
                  "description": "Real chart"}
    hostile = make("Isolation hostile", [
        {"kind": "chart", "image_base64": PAYLOAD, "is_plotly": True,
         "description": "Chart markup"},
        {"kind": "table", "description": "Table markup",
         "table": {"columns": ["a"], "rows": [["1"]], "total_rows": 1,
                   "styled_html": PAYLOAD}},
        real_chart,
    ])
    clean = make("Isolation clean", [real_chart])
    yield {"hostile": hostile, "clean": clean}
    for dash_id in created:
        session.post(f"/api/dashboards/{dash_id}/delete", json={})


@pytest.fixture(scope="module")
def seeded_conversation(session, chat, account, plotly_html) -> str:
    return seed_history_row(chat, {
        "role": "ai", "content": "Seeded chart and table.",
        "image_base64": plotly_html,
        "table": {"columns": ["a"], "rows": [{"a": "1"}], "total_rows": 1,
                  "styled_html": PAYLOAD},
    }, email=account["email"])


def _require_docker():
    if not docker_available():
        pytest.skip("docker is not on PATH: the server-side request count needs "
                    "the web container's log")


def _load_dashboard(browser, base_url, cookie, dash_id, expected_tiles):
    context, page, seen = _new_page(browser, base_url, cookie)
    page.goto(f"{base_url}/dashboards/{dash_id}", wait_until="load", timeout=PAGE_TIMEOUT_MS)
    # A locator poll, not `page.wait_for_function`: Playwright evaluates a
    # polled predicate with eval() on every animation frame, and under the
    # page's policy each evaluation raises a script-src report of its own.
    deadline = time.time() + PAGE_TIMEOUT_MS / 1000
    while (page.locator(".pdc-tile-body").count() < expected_tiles
           and time.time() < deadline):
        page.wait_for_timeout(250)
    assert page.locator(".pdc-tile-body").count() >= expected_tiles, "tiles did not render"
    frame = _wait_for_plotly(page)
    page.wait_for_timeout(int(SETTLE_S * 1000))
    return context, page, seen, frame


# ---------------------------------------------------------------------------
# dashboard as the recipient
# ---------------------------------------------------------------------------
def test_recipient_dashboard_markup_has_no_effect_on_the_viewer(
        base_url, browser, recipient, dashboards):
    _require_docker()
    anchor = _log_anchor()
    clean_id, _ = dashboards["clean"]
    hostile_id, hostile_name = dashboards["hostile"]

    before = _auth_me_hits(anchor)
    ctx, page, _, _ = _load_dashboard(browser, base_url, recipient["cookie"], clean_id, 1)
    ctx.close()
    control = _auth_me_hits(anchor) - before
    if control < 1:
        pytest.skip("the web container's access log shows no /auth/me line for the "
                    "control page; the server-side count cannot be taken")

    before = _auth_me_hits(anchor)
    ctx, page, seen, _ = _load_dashboard(browser, base_url, recipient["cookie"], hostile_id, 3)
    try:
        title = page.title()
        assert title != HOSTILE_TITLE, title
        assert title == f"{hostile_name} - PowerDataChat", title
        _assert_frames_isolated(page)
        bodies = page.eval_on_selector_all(".pdc-tile-body", "els => els.map(e => e.innerHTML)")
        table_bodies = [b for b in bodies if "pdc-tile-tablewrap" in b or "<table" in b]
        assert table_bodies, "the table tile did not render"
        for html in table_bodies:
            low = html.lower()
            assert "<img" not in low and "onerror" not in low, html[:300]
            assert "<script" not in low, html[:300]
    finally:
        ctx.close()
    extra = _auth_me_hits(anchor) - before - control
    assert extra == 0, f"{extra} /auth/me request(s) beyond the page's own"


def test_real_plotly_tile_renders_offline_with_hover_and_no_violation(
        base_url, browser, recipient, dashboards):
    clean_id, _ = dashboards["clean"]
    ctx, page, seen, frame = _load_dashboard(browser, base_url, recipient["cookie"],
                                             clean_id, 1)
    try:
        assert frame is not None, "the plotly chart did not render in its frame"
        assert frame.locator(".main-svg").count() >= 1
        _assert_hover(page, frame)
        assert not [u for u in seen["requests"] if CDN_PLOTLY_HOST in u], seen["requests"]
        assert _violations(page) == [], _violations(page)
        assert _csp_console(seen) == [], _csp_console(seen)
    finally:
        ctx.close()


def test_the_hostile_dashboard_still_renders_the_real_chart(
        base_url, browser, recipient, dashboards):
    hostile_id, _ = dashboards["hostile"]
    ctx, page, seen, frame = _load_dashboard(browser, base_url, recipient["cookie"],
                                             hostile_id, 3)
    try:
        assert frame is not None, "the real chart did not render next to the others"
        _assert_hover(page, frame)
        assert not [u for u in seen["requests"] if CDN_PLOTLY_HOST in u], seen["requests"]
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# chat page as the owner
# ---------------------------------------------------------------------------
def _load_conversation(browser, base_url, cookie, conv_id):
    context, page, seen = _new_page(browser, base_url, cookie)
    page.goto(f"{base_url}/c/{conv_id}", wait_until="load", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_selector(".pdc-table-block", timeout=PAGE_TIMEOUT_MS)
    frame = _wait_for_plotly(page)
    page.wait_for_timeout(int(SETTLE_S * 1000))
    return context, page, seen, frame


def _server_title(session, path) -> str:
    r = session.get(path)
    assert r.status_code == 200, r.status_code
    m = re.search(r"<title>(.*?)</title>", r.text, re.S | re.I)
    assert m, r.text[:300]
    return m.group(1).strip()


def test_chat_page_seeded_markup_has_no_effect_on_the_viewer(
        base_url, browser, session, seeded_conversation):
    _require_docker()
    anchor = _log_anchor()
    cookie = session.cookies.get(SESSION_COOKIE)
    assert cookie
    expected_title = _server_title(session, "/lab")

    before = _auth_me_hits(anchor)
    ctx, page, _ = _new_page(browser, base_url, cookie)
    page.goto(f"{base_url}/lab", wait_until="load", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(int(SETTLE_S * 1000))
    ctx.close()
    control = _auth_me_hits(anchor) - before
    if control < 1:
        pytest.skip("the web container's access log shows no /auth/me line for the "
                    "control page; the server-side count cannot be taken")

    before = _auth_me_hits(anchor)
    ctx, page, seen, frame = _load_conversation(browser, base_url, cookie, seeded_conversation)
    try:
        title = page.title()
        assert title != HOSTILE_TITLE, title
        assert title == expected_title, (title, expected_title)
        _assert_frames_isolated(page)
        blocks = page.eval_on_selector_all(".pdc-table-block", "els => els.map(e => e.innerHTML)")
        assert blocks, "the seeded table did not render"
        for html in blocks:
            low = html.lower()
            assert "<img" not in low and "onerror" not in low and "<script" not in low, html[:300]
    finally:
        ctx.close()
    extra = _auth_me_hits(anchor) - before - control
    assert extra == 0, f"{extra} /auth/me request(s) beyond the page's own"


def test_chat_page_real_chart_renders_offline_with_hover_and_no_violation(
        base_url, browser, session, seeded_conversation):
    cookie = session.cookies.get(SESSION_COOKIE)
    ctx, page, seen, frame = _load_conversation(browser, base_url, cookie, seeded_conversation)
    try:
        assert frame is not None, "the plotly chart did not render in its frame"
        _assert_hover(page, frame)
        assert not [u for u in seen["requests"] if CDN_PLOTLY_HOST in u], seen["requests"]
        assert _violations(page) == [], _violations(page)
        assert _csp_console(seen) == [], _csp_console(seen)
    finally:
        ctx.close()


# ---------------------------------------------------------------------------
# WebGL chart: the eval grant lives on the chart route only
# ---------------------------------------------------------------------------
def _scattergl_document() -> str:
    import numpy as np
    import pandas as pd
    import plotly.express as px

    import plot_utils
    df = pd.DataFrame({"x": np.arange(1500), "y": np.random.RandomState(1).randn(1500)})
    fig = px.scatter(df, x="x", y="y")
    assert fig.data[0].type == "scattergl", fig.data[0].type
    return plot_utils._plotly_to_html(fig)


@pytest.fixture(scope="module")
def webgl_dashboard(session, chat):
    r = session.post("/api/dashboards", json={"name": "Isolation webgl"})
    assert r.status_code == 200, r.text[:300]
    dash_id = r.json()["dash_id"]
    try:
        r = session.post(f"/api/dashboards/{dash_id}/tiles",
                         json={"chat_id": chat, "kind": "chart", "is_plotly": True,
                               "image_base64": _scattergl_document(),
                               "description": "WebGL chart"})
        assert r.status_code == 200, (r.status_code, r.text[:300])
        yield dash_id
    finally:
        session.post(f"/api/dashboards/{dash_id}/delete", json={})


def _wait_for_canvas_frame(page):
    deadline = time.time() + PAGE_TIMEOUT_MS / 1000
    while time.time() < deadline:
        for frame in _chart_frames(page):
            try:
                if frame.locator("canvas").count() >= 1:
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(250)
    return None


def test_a_webgl_chart_renders_in_its_frame_with_no_violation(
        base_url, webgl_browser, session, webgl_dashboard):
    cookie = session.cookies.get(SESSION_COOKIE)
    assert cookie
    parent_header = session.get(f"/dashboards/{webgl_dashboard}").headers.get(CSP_TEXT, "")
    assert parent_header, "the dashboard page carries no policy"
    assert "'unsafe-eval'" not in parent_header, parent_header

    context, page, seen = _new_page(webgl_browser, base_url, cookie)
    chart_responses = []
    page.on("response", lambda resp: chart_responses.append(resp)
            if _is_chart_url(resp.url) else None)
    try:
        page.goto(f"{base_url}/dashboards/{webgl_dashboard}", wait_until="load",
                  timeout=PAGE_TIMEOUT_MS)
        frame = _wait_for_canvas_frame(page)
        page.wait_for_timeout(int(SETTLE_S * 1000))
        assert frame is not None, "no chart frame with a canvas"
        trace = frame.evaluate(
            "() => document.getElementById('plotly-chart').data[0].type")
        assert trace == "scattergl", trace
        text = frame.locator("body").inner_text()
        assert WEBGL_UNSUPPORTED_TEXT not in text, text[:300]
        assert frame.locator("canvas").count() >= 1
        assert page.evaluate("() => window.__cspViolations || []") == []
        assert _frame_violations(frame) == []
        assert _csp_console(seen) == [], _csp_console(seen)
        assert chart_responses, "the chart document was not loaded from /charts/"
        frame_header = chart_responses[-1].all_headers().get("content-security-policy", "")
        assert "'unsafe-eval'" in frame_header, frame_header
    finally:
        context.close()


# ---------------------------------------------------------------------------
# a table cannot borrow a page class
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def overlay_dashboard(session, chat):
    r = session.post("/api/dashboards", json={"name": "Isolation overlay"})
    assert r.status_code == 200, r.text[:300]
    dash_id = r.json()["dash_id"]
    try:
        r = session.post(f"/api/dashboards/{dash_id}/tiles",
                         json={"chat_id": chat, "kind": "table", "description": "Overlay",
                               "table": {"columns": ["a"], "rows": [["x"]],
                                         "total_rows": 1, "styled_html": OVERLAY_TABLE}})
        assert r.status_code == 200, (r.status_code, r.text[:300])
        yield dash_id
    finally:
        session.post(f"/api/dashboards/{dash_id}/delete", json={})


def test_a_table_tile_does_not_take_over_the_page(base_url, browser, session,
                                                  overlay_dashboard):
    cookie = session.cookies.get(SESSION_COOKIE)
    assert cookie
    context, page, seen = _new_page(browser, base_url, cookie)
    try:
        page.goto(f"{base_url}/dashboards/{overlay_dashboard}", wait_until="load",
                  timeout=PAGE_TIMEOUT_MS)
        deadline = time.time() + PAGE_TIMEOUT_MS / 1000
        while page.locator(".pdc-tile-body").count() < 1 and time.time() < deadline:
            page.wait_for_timeout(250)
        assert page.locator(".pdc-tile-body").count() >= 1, "the tile did not render"
        page.wait_for_timeout(int(SETTLE_S * 1000))
        assert page.locator(".dash-larger-modal").count() == 0
        inside_tile = page.evaluate("""() => {
            const el = document.elementFromPoint(window.innerWidth / 2, 10);
            return !!(el && el.closest('.pdc-tile-body'));
        }""")
        assert inside_tile is False, "the top of the page is covered by the tile"
    finally:
        context.close()


# ---------------------------------------------------------------------------
# reports rasterise server-side and are unaffected
# ---------------------------------------------------------------------------
@pytest.mark.needs_brain
@pytest.mark.parametrize("route,magic", [("download_report", PDF_MAGIC),
                                         ("download_pptx", PPTX_MAGIC)],
                         ids=["pdf", "pptx"])
def test_reports_render_for_the_seeded_conversation(session, chat, seeded_conversation,
                                                    route, magic):
    r = session.post(f"/api/chat/{chat}/conversation/{seeded_conversation}/{route}", json={})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    assert r.content.startswith(magic), r.content[:16]


# ---------------------------------------------------------------------------
# a stored chart document cannot load script from another host
# ---------------------------------------------------------------------------
EXTERNAL_MARK = "EXTERNAL-RAN"
INLINE_MARK = "INLINE-RAN"
_FRAME_ON_THIS_ORIGIN = """(src) => {
  const f = document.createElement('iframe');
  f.setAttribute('sandbox', 'allow-scripts');
  f.src = src;
  document.body.appendChild(f);
}"""


@pytest.fixture
def beacon_server():
    """A throwaway local script host: every request is recorded and answered
    with a script that would set the document title."""
    import http.server
    import threading

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                          # noqa: N802 - stdlib name
            hits.append(self.path)
            body = f"document.title='{EXTERNAL_MARK}';".encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"port": server.server_address[1], "hits": hits}
    finally:
        server.shutdown()
        server.server_close()


def test_a_chart_document_cannot_load_script_from_another_host(
        base_url, browser, session, beacon_server):
    port = beacon_server["port"]
    doc = ("<!DOCTYPE html><html><head><meta charset=\"utf-8\"></head><body>"
           "<div id=\"out\">NOT-RUN</div>"
           f"<script src=\"http://127.0.0.1:{port}/beacon.js\"></script>"
           "<script>document.getElementById('out').textContent = "
           f"'{INLINE_MARK}|' + document.title;</script>"
           "</body></html>")
    r = session.post("/api/charts", json={"html": doc})
    assert r.status_code == 200, (r.status_code, r.text[:300])
    chart_url = r.json()["url"]
    assert chart_url.startswith(CHART_PATH_PREFIX), chart_url

    cookie = session.cookies.get(SESSION_COOKIE)
    assert cookie
    context, page, seen = _new_page(browser, base_url, cookie)
    try:
        page.goto(f"{base_url}/health", wait_until="load", timeout=PAGE_TIMEOUT_MS)
        page.evaluate(_FRAME_ON_THIS_ORIGIN, chart_url)
        # A locator poll, not `page.wait_for_function` (see `_load_dashboard`).
        text, frame = "", None
        deadline = time.time() + PAGE_TIMEOUT_MS / 1000
        while time.time() < deadline:
            frames = _chart_frames(page)
            if frames:
                frame = frames[0]
                try:
                    text = frame.locator("#out").inner_text(timeout=1_000)
                except Exception:
                    text = ""
                if text.startswith(INLINE_MARK):
                    break
            page.wait_for_timeout(250)
        assert frame is not None, "the chart frame did not load"
        page.wait_for_timeout(int(SETTLE_S * 1000))
        text = frame.locator("#out").inner_text()
        assert text.startswith(INLINE_MARK), f"the inline script did not run: {text!r}"
        assert EXTERNAL_MARK not in text, text
        assert beacon_server["hits"] == [], (
            f"the chart document fetched script from another host: {beacon_server['hits']}")
    finally:
        context.close()
