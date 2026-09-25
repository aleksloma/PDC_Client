"""Chart and table markup render isolated from the page that shows them.

Two kinds of stored or generated markup reach the browser:

* CHART documents (plotly HTML: a full document with inline scripts). They
  run inside an iframe sandboxed with `allow-scripts` ONLY, so the document
  has an opaque origin: no cookies, no storage, no access to the parent page,
  no request carrying the user's session. Every frame is built by ONE helper,
  `PDCViewers.setChartFrame(iframe, html)` in `static/vendor/viewers.js`,
  which sets the sandbox, registers the document with `POST /api/charts`
  (after the offline plotly rewrite) and points the frame's `src` at the
  `/charts/{token}` URL it gets back; that route serves the document under
  its own policy (tests/test_chart_frames.py). No frame is filled through
  `srcdoc` and no page script builds a frame policy or nonce of its own. No
  other file sets `sandbox`, and nothing reads into a frame (`contentWindow`
  / `contentDocument`).
* STYLED TABLES (`styled_html`). They are inserted into the page itself, so
  they are cleaned server-side (`html_sanitize`): at the source, at pin
  ingress (`_capped_table`, which also covers the refresh write-back) and at
  every egress of stored rows — both chat history routes and the dashboard
  document read. Stored files are NOT rewritten; cleaning is applied to the
  response only.

Also pinned: the Show-code viewer escapes `<` in its JSON island and its
inline scripts carry the page nonce; no template carries an inline event
handler; the only inline handlers left in `dashboard.js` are the B2C
subscription dialogs, which are unreachable on this edition (the plan cards
are hidden and `/auth/subscription` is the constant plan) — an explicit
allowlist below; matplotlib PNGs never go through `innerHTML`; `nh3` is
pinned for the main app and absent from the sandbox image.

The chat stream's `done` event carries the `table` / `tables` that
`run_chat_local` built, verbatim; the stream route adds no markup of its own.
That path is covered at the source in `tests/test_html_sanitize.py`
(`test_table_builders_carry_the_clean_markup`) rather than through the route.

Offline: text-level scans of the JS/templates (no JS runner in this suite),
and route tests on a minimal FastAPI app with DATA_ROOT on tmp_path.
"""
import json
import re
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

import local_store
from settings import settings

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"
VIEWERS = STATIC / "vendor" / "viewers.js"
DASHBOARD_JS = STATIC / "dashboard.js"
DASHBOARD_VIEW_JS = STATIC / "dashboard_view.js"
CHAT_JS = STATIC / "chat.js"
FRAME_FILES = [DASHBOARD_JS, DASHBOARD_VIEW_JS, CHAT_JS]

# Third-party bundles vendored verbatim: not our code, excluded by name.
VENDORED_LIBS = ("plotly", "gridstack", "cytoscape", "cytoscape-dagre", "dagre")

PAYLOAD = ("<script>parent.document.title='x'</script>"
           "<img src=x onerror=\"fetch('/auth/me')\">")
STYLED_HOSTILE = ("<style>body{display:none}</style>"
                  "<table id=\"T_x\"><tr><td style=\"color:red\">cell-7</td>"
                  f"<td>{PAYLOAD}</td></tr></table>")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _own_js_files() -> list[Path]:
    out = []
    for p in sorted(STATIC.rglob("*.js")):
        rel = p.relative_to(STATIC).parts
        if len(rel) >= 2 and rel[0] == "vendor" and rel[1] in VENDORED_LIBS:
            continue
        out.append(p)
    return out


def _templates() -> list[Path]:
    return sorted(TEMPLATES.rglob("*.html"))


def _templates_referencing(file_name: str) -> list[str]:
    """Templates whose source names `file_name` as a path segment (a script
    src, a url_for, or a bare quoted name)."""
    pat = re.compile(r"""(?:["'/=]|^)""" + re.escape(file_name) + r"""(?:["'?#\s>]|$)""", re.M)
    return [str(t.relative_to(ROOT)) for t in _templates() if pat.search(_read(t))]


def _function_body(src: str, name: str) -> str:
    """Source of the function `name` (declaration, `name = function`, or
    `name: function` / arrow), braces matched naively. "" when absent."""
    m = re.search(
        rf"(function\s+{name}\s*\(|\b{name}\s*[:=]\s*(?:async\s+)?function\s*\(|"
        rf"\b{name}\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*=>)", src)
    if not m:
        return ""
    start = src.find("{", m.end())
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(src)):
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return src[start:]


def _assert_inert(html):
    if html is None:
        return
    low = str(html).lower()
    for needle in ("<script", "onerror", "<img", "javascript:"):
        assert needle not in low, (needle, str(html)[:300])
    assert "display:none" not in low.replace(" ", ""), str(html)[:300]


# ===========================================================================
# 1. chart frames
# ===========================================================================
def test_the_structural_helpers_find_known_samples():
    """Self-test of the scanners, so a green run cannot be a blind one."""
    sample = "var a = 1;\nfunction setChartFrame(iframe, html) { if (x) { y(); } return 1; }\n"
    assert _function_body(sample, "setChartFrame").startswith("{ if (x)")
    assert _function_body(sample, "missing") == ""
    assert _own_js_files(), "no JS found under static/"
    assert DASHBOARD_JS in _own_js_files() and VIEWERS in _own_js_files()
    assert not any("plotly.min.js" in str(p) for p in _own_js_files())


@pytest.mark.parametrize("path", _own_js_files() + _templates(),
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_no_frame_is_granted_the_page_origin(path):
    assert "allow-same-origin" not in _read(path), path


def test_viewers_defines_and_exposes_set_chart_frame():
    src = _read(VIEWERS)
    body = _function_body(src, "setChartFrame")
    assert body, "static/vendor/viewers.js must define setChartFrame(iframe, html)"
    exposed = src[src.rfind("window.PDCViewers"):]
    assert re.search(r"\bsetChartFrame\b", exposed), \
        "setChartFrame must be exposed on window.PDCViewers"


# CONTRACT REPLACED: setChartFrame no longer writes
# the document into `srcdoc` with a frame-level meta policy and the page
# nonce. It sets the sandbox, registers the HTML with `POST /api/charts` and
# sets the frame's `src` to the returned `/charts/{token}`; the route owns the
# frame's policy. The two tests below state the new contract in place of the
# srcdoc / meta-policy assertions they replace.
def test_set_chart_frame_sandboxes_with_scripts_only():
    body = _function_body(_read(VIEWERS), "setChartFrame")
    assert body, "setChartFrame is missing"
    assert "setAttribute('sandbox', 'allow-scripts')" in body, \
        "setChartFrame must set sandbox to allow-scripts"
    values = re.findall(r"setAttribute\(\s*['\"]sandbox['\"]\s*,\s*(['\"])(.*?)\1\s*\)", body)
    assert values, "setChartFrame must set the sandbox attribute"
    assert [v for _, v in values] == ["allow-scripts"] * len(values), values


def test_set_chart_frame_registers_the_document_and_loads_the_chart_route():
    body = _function_body(_read(VIEWERS), "setChartFrame")
    assert body, "setChartFrame is missing"
    assert "/api/charts" in body, "setChartFrame must register the HTML with POST /api/charts"
    assert "fixPlotlyOffline" in body, "setChartFrame must keep the offline plotly rewrite"
    assert re.search(r"\.src\s*=", body), "setChartFrame must set the frame's src"
    assert "srcdoc" not in body, "setChartFrame must not fill the frame through srcdoc"
    assert "Content-Security-Policy" not in body, \
        "the frame's policy belongs to the /charts/ route, not to a meta element"
    assert "'nonce-" not in body, "setChartFrame must not build a nonce source"


def test_only_viewers_sets_a_sandbox_attribute():
    offenders = []
    for path in _own_js_files():
        if path == VIEWERS:
            continue
        src = _read(path)
        if re.search(r"setAttribute\(\s*['\"]sandbox['\"]", src) or \
                re.search(r"\.sandbox\s*=(?!=)", src):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders


@pytest.mark.parametrize("path", FRAME_FILES + [VIEWERS], ids=lambda p: p.name)
def test_page_scripts_never_touch_srcdoc(path):
    """No `srcdoc` token at all, viewers.js included: no write, and no read
    (the pin and the download take the unwrapped HTML from the page's own
    state, never from a frame)."""
    hits = [f"{i}: {ln.strip()[:120]}" for i, ln in enumerate(_read(path).splitlines(), 1)
            if re.search(r"srcdoc", ln, re.I)]
    assert hits == [], hits


@pytest.mark.parametrize("path", FRAME_FILES, ids=lambda p: p.name)
def test_page_scripts_never_reach_into_a_frame(path):
    src = _read(path)
    assert "contentWindow" not in src and "contentDocument" not in src, path


@pytest.mark.parametrize("path,minimum", [(DASHBOARD_JS, 3), (DASHBOARD_VIEW_JS, 2)],
                         ids=["dashboard.js", "dashboard_view.js"])
def test_every_chart_frame_goes_through_the_shared_helper(path, minimum):
    count = len(re.findall(r"\bsetChartFrame\(", _read(path)))
    assert count >= minimum, (path.name, count)


def test_the_pin_no_longer_reads_the_frame_document():
    assert "image = iframe.srcdoc" not in _read(DASHBOARD_JS)


# ===========================================================================
# 2. Show-code / Show-data viewer tabs
# ===========================================================================
def test_code_viewer_escapes_less_than_in_its_json_island():
    body = _function_body(_read(VIEWERS), "codeDocument")
    assert body, "codeDocument is missing"
    assert re.search(r"JSON\.stringify\(blocks\)\s*\.replace\(", body), \
        "the JSON island must be escaped after JSON.stringify(blocks)"
    assert re.search(r"\\\\?u003c", body, re.I), "`<` must become \\u003c"


def test_viewer_documents_nonce_their_inline_scripts():
    src = _read(VIEWERS)
    code = _function_body(src, "codeDocument")
    data = _function_body(src, "dataDocument") + _function_body(src, "sortScript")
    assert code and data
    assert "nonce=" in code, "codeDocument's <script> must carry the page nonce"
    assert "nonce=" in data, "the data viewer's <script> must carry the page nonce"


# ===========================================================================
# 3. inline handlers
# ===========================================================================
TEMPLATE_HANDLER_RE = re.compile(r"\bon(click|change|submit|load|error|input)\s*=", re.I)
# `on<event>="…"` inside a JS string literal (markup built as a string).
JS_HANDLER_RE = re.compile(r"""\bon[a-z]+=(\\?["'])(.*?)\1""", re.S)
CALLED_NAME_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")

# The B2C subscription dialogs (plan change / cancel / reactivate / payment
# method). Unreachable on this edition: the plan cards are hidden and
# `/auth/subscription` answers the constant plan. Removing that code is a
# separate cleanup; until then these are the ONLY inline handlers allowed.
B2C_SUBSCRIPTION_HANDLERS = frozenset({
    "reactivateSubscription", "_closeSubModal", "_doReactivate",
    "loadUserProfile", "selectPlan", "_confirmAdminChange",
    "_updatePaymentMethod", "_confirmPlanChange",
})


def _inline_handler_calls(src: str) -> list[tuple[int, str]]:
    out = []
    for m in JS_HANDLER_RE.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        for name in CALLED_NAME_RE.findall(m.group(2)):
            out.append((line, name))
    return out


def test_the_inline_handler_scanner_finds_a_known_sample():
    sample = ("x = '<button onclick=\"removeFile(1)\">×</button>';\n"
              "y = '<a onclick=\"_closeSubModal(); loadUserProfile();\">';\n"
              "z = `<b onchange='evil()'>`;\n")
    names = [n for _, n in _inline_handler_calls(sample)]
    assert names == ["removeFile", "_closeSubModal", "loadUserProfile", "evil"], names
    assert TEMPLATE_HANDLER_RE.search('<button onclick="x()">')
    assert not TEMPLATE_HANDLER_RE.search('<button data-onclick-x="1">')


@pytest.mark.parametrize("path", _templates(), ids=lambda p: str(p.relative_to(ROOT)))
def test_templates_carry_no_inline_event_handler(path):
    hits = [f"{i}: {ln.strip()[:120]}" for i, ln in enumerate(_read(path).splitlines(), 1)
            if TEMPLATE_HANDLER_RE.search(ln)]
    assert hits == [], hits


def test_dashboard_js_inline_handlers_are_only_the_b2c_subscription_ones():
    calls = _inline_handler_calls(_read(DASHBOARD_JS))
    extra = sorted({(line, name) for line, name in calls
                    if name not in B2C_SUBSCRIPTION_HANDLERS})
    assert extra == [], extra


def test_the_file_remove_button_is_bound_not_inline():
    src = _read(DASHBOARD_JS)
    assert not re.search(r"""onclick=\\?["'][^"']*removeFile\(""", src)


@pytest.mark.parametrize("path", [p for p in _own_js_files() if p != DASHBOARD_JS],
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_other_live_scripts_build_no_inline_handlers(path):
    if path.name in ("chat.js", "config.js", "login.js", "register.js", "index_new.js"):
        # The skip trusts the file NAME, so prove the claim first: a template
        # that starts loading one of these files turns this into a failure
        # instead of a silent skip (Task 8b Part A #20).
        loaders = _templates_referencing(path.name)
        assert loaders == [], (path.name, "is loaded by", loaders)
        pytest.skip("not loaded by any template")
    assert _inline_handler_calls(_read(path)) == [], path


# ===========================================================================
# 4. matplotlib PNGs never pass through innerHTML
# ===========================================================================
@pytest.mark.parametrize("path", [DASHBOARD_JS, DASHBOARD_VIEW_JS], ids=lambda p: p.name)
def test_png_charts_are_never_assigned_through_inner_html(path):
    src = _read(path)
    hits = re.findall(r"innerHTML\s*\+?=\s*[^;]{0,400}?image_base64", src)
    assert hits == [], hits[:3]


# ===========================================================================
# 5. styled_html: egress of stored chat rows
# ===========================================================================
OWNER = "owner@acme.com"
CHAT = "c_isolation00001"


@pytest.fixture
def chat_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    local_store._DATAFRAME_CACHE.invalidate()
    import routes.chat as chat_mod

    local_store.AuthStore().ensure_user(OWNER)
    store = local_store.ChatDataStore(CHAT)
    meta = store.read_meta()
    meta["owner"] = OWNER
    store.write_meta(meta)
    conv = store.new_conversation("t")
    local_store.AuthStore().record_conversation(OWNER, CHAT, conv, "t")
    store.append_history(conv, {"role": "human", "content": "q"})
    store.append_history(conv, {
        "role": "ai", "content": "a",
        "table": {"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1,
                  "styled_html": STYLED_HOSTILE},
        "tables": [{"title": "t1", "columns": ["a"], "rows": [{"a": 1}],
                    "total_rows": 1, "styled_html": STYLED_HOSTILE},
                   {"title": "t2", "columns": ["a"], "rows": [{"a": 2}],
                    "total_rows": 1}],
    })

    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{OWNER}")
    yield {"client": tc, "store": store, "conv": conv}
    local_store._DATAFRAME_CACHE.invalidate()


def _ai_rows(history):
    return [r for r in history if r.get("role") == "ai"]


def _assert_rows_inert(history):
    rows = _ai_rows(history)
    assert rows, history
    for row in rows:
        table = row.get("table")
        if isinstance(table, dict):
            _assert_inert(table.get("styled_html"))
            assert table.get("rows") == [{"a": 1}], table
        for t in row.get("tables") or []:
            _assert_inert(t.get("styled_html"))


def _assert_file_untouched(world):
    raw = (world["store"].conversations_dir / f"{world['conv']}.jsonl").read_text(encoding="utf-8")
    assert "onerror" in raw, "the stored row must not be rewritten by a read"


def test_conversation_history_serves_stored_styled_html_inert(chat_client):
    r = chat_client["client"].get(f"/api/chat/{CHAT}/conversation/{chat_client['conv']}/history")
    assert r.status_code == 200, r.text
    _assert_rows_inert(r.json()["history"])
    _assert_file_untouched(chat_client)


def test_legacy_history_serves_stored_styled_html_inert(chat_client):
    r = chat_client["client"].get(f"/api/chat/{CHAT}/history")
    assert r.status_code == 200, r.text
    _assert_rows_inert(r.json()["history"])
    _assert_file_untouched(chat_client)


def test_history_keeps_the_allowed_formatting(chat_client):
    """Cleaning keeps the cell text and the allowlisted colour."""
    r = chat_client["client"].get(f"/api/chat/{CHAT}/conversation/{chat_client['conv']}/history")
    row = _ai_rows(r.json()["history"])[-1]
    styled = (row.get("table") or {}).get("styled_html") or ""
    assert "cell-7" in styled, styled
    assert "color:red" in styled.replace(" ", ""), styled
    assert "styled_html" not in row["tables"][1], "a table without markup gains none"


# ===========================================================================
# 6. styled_html: dashboard pin ingress, stored-tile egress, refresh
# ===========================================================================
DASH_OWNER = "alice@acme.com"


@pytest.fixture
def dash_client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    import routes.chat as chat_mod
    import routes.dashboards as dash_mod
    monkeypatch.setattr(local_store, "chat_exists", lambda cid: True)
    monkeypatch.setattr(local_store, "get_chat_meta_owner", lambda cid: DASH_OWNER)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(dash_mod.router)
    app.include_router(chat_mod.router)

    @app.post("/_login/{email}")
    async def _login(request: Request, email: str):
        request.session["email"] = email
        return {"ok": True}

    tc = TestClient(app)
    tc.post(f"/_login/{DASH_OWNER}")
    dash = tc.post("/api/dashboards", json={"name": "Iso"}).json()["dash_id"]
    return {"client": tc, "dash": dash, "mod": dash_mod}


def _doc_on_disk(dash_id):
    store = local_store.DashboardStore()
    return json.loads(store._doc_path(DASH_OWNER, dash_id).read_text(encoding="utf-8"))


def test_pinning_a_table_stores_inert_markup(dash_client):
    tc, dash = dash_client["client"], dash_client["dash"]
    r = tc.post(f"/api/dashboards/{dash}/tiles",
                json={"chat_id": "c1", "kind": "table",
                      "table": {"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1,
                                "styled_html": STYLED_HOSTILE}})
    assert r.status_code == 200, r.text
    _assert_inert(r.json()["tile"]["snapshot"]["table"].get("styled_html"))
    stored = _doc_on_disk(dash)["tiles"][0]["snapshot"]["table"]
    _assert_inert(stored.get("styled_html"))
    assert "cell-7" in (stored.get("styled_html") or ""), stored


def test_a_stored_raw_tile_is_served_inert_and_left_on_disk(dash_client):
    tc, dash = dash_client["client"], dash_client["dash"]
    local_store.DashboardStore().add_tile(DASH_OWNER, dash, {
        "chat_id": "c1", "kind": "table", "title": "T", "description": "",
        "code": None, "chart_data": None, "full_table_key": None,
        "snapshot": {"rendered_at": "2026-01-01T00:00:00Z",
                     "table": {"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1,
                               "styled_html": STYLED_HOSTILE}}})
    r = tc.get(f"/api/dashboards/{dash}")
    assert r.status_code == 200, r.text
    tile = r.json()["tiles"][0]
    _assert_inert(tile["snapshot"]["table"].get("styled_html"))
    assert tile["snapshot"]["table"]["rows"] == [{"a": 1}]
    raw = _doc_on_disk(dash)["tiles"][0]["snapshot"]["table"]["styled_html"]
    assert "onerror" in raw, "a read must not rewrite the stored document"


def test_refresh_write_back_stores_inert_markup(dash_client, monkeypatch):
    from conftest import seed_history
    tc, dash, dash_mod = dash_client["client"], dash_client["dash"], dash_client["mod"]
    seed_history("c1", "RESULT = dfs")
    tile = tc.post(f"/api/dashboards/{dash}/tiles",
                   json={"chat_id": "c1", "kind": "table", "code": "RESULT = dfs",
                         "table": {"columns": ["a"], "rows": [{"a": 1}],
                                   "total_rows": 1}}).json()["tile"]

    async def fake(chat_id, code, kind, sid, *, drop_df_keys=None):
        return {"ok": True, "kind": "table",
                "table": {"columns": ["a"], "rows": [{"a": 9}], "total_rows": 1,
                          "styled_html": STYLED_HOSTILE}}

    monkeypatch.setattr(dash_mod, "run_item_refresh", fake)
    r = tc.post(f"/api/dashboards/{dash}/tiles/{tile['tile_id']}/refresh", json={})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    _assert_inert(r.json()["tile"]["snapshot"]["table"].get("styled_html"))
    stored = _doc_on_disk(dash)["tiles"][0]["snapshot"]["table"]
    _assert_inert(stored.get("styled_html"))
    assert stored["rows"] == [{"a": 9}], stored


# Page classes and image functions: a table must not borrow one of the page's
# own classes nor make the browser fetch anything through a CSS function.
OVERLAY_STYLED = (
    "<style>#T_a td { background: image-set('/auth/me' 1x); color: green; }</style>"
    '<div class="dash-larger-modal" style="margin:-9999px">'
    '<table id="T_a"><tr><td class="data row0 col0" '
    "style=\"background: image-set('/auth/me' 1x)\">cell-9</td></tr></table></div>")


def test_pinning_a_table_with_page_classes_and_image_functions_stores_neither(dash_client):
    tc, dash = dash_client["client"], dash_client["dash"]
    r = tc.post(f"/api/dashboards/{dash}/tiles",
                json={"chat_id": "c1", "kind": "table",
                      "table": {"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1,
                                "styled_html": OVERLAY_STYLED}})
    assert r.status_code == 200, r.text
    served = r.json()["tile"]["snapshot"]["table"].get("styled_html") or ""
    stored = _doc_on_disk(dash)["tiles"][0]["snapshot"]["table"].get("styled_html") or ""
    for html in (served, stored):
        assert "dash-larger-modal" not in html, html
        assert "image-set" not in html.lower(), html
        assert "/auth/me" not in html, html
    assert "cell-9" in stored, stored


# ===========================================================================
# 7. the sanitiser dependency
# ===========================================================================
def _pins(path: Path) -> dict:
    out = {}
    for raw in _read(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if "==" in line:
            name, ver = line.split("==", 1)
            out[name.strip().lower()] = ver.strip()
    return out


def test_nh3_is_pinned_exactly_for_the_main_app():
    assert _pins(ROOT / "requirements.txt").get("nh3") == "0.3.7"


def test_nh3_is_not_in_the_sandbox_image():
    text = _read(ROOT / "executor" / "requirements.txt").lower()
    assert "nh3" not in text


# ===========================================================================
# Task 8b Part A: refresh race, sidebar escaping, the policy opt-out pin
# ===========================================================================
_STAMP_WRITE_RE = re.compile(
    r"\biframe\s*(?:\.\s*dataset)?\s*(?:\.\s*([A-Za-z_$][\w$]*)|\[\s*['\"]([\w$-]+)['\"]\s*\])"
    r"\s*(?:=(?!=)|\+\+|\+=)")
_STAMP_ATTR_RE = re.compile(r"\biframe\s*\.\s*setAttribute\(\s*['\"](data-[\w-]+)['\"]")


def _generation_stamps(body: str) -> set:
    names = {a or b for a, b in _STAMP_WRITE_RE.findall(body)}
    names |= set(_STAMP_ATTR_RE.findall(body))
    names -= {"src", "sandbox"}
    return names


def _compared_before_src(body: str, name: str) -> bool:
    """`name` takes part in an (in)equality test that comes before the
    frame's `.src =` assignment, i.e. a stale resolution is recognised
    before it could overwrite a newer chart."""
    src_at = [m.start() for m in re.finditer(r"\.src\s*=(?!=)", body)]
    if not src_at:
        return False
    last_src = src_at[-1]
    key = re.escape(name)
    cmp_re = re.compile(rf"(?:\b{key}\b|['\"]{key}['\"])[^;\n]*?(?:!==|===|!=|==)"
                        rf"|(?:!==|===|!=|==)[^;\n]*?(?:\b{key}\b|['\"]{key}['\"])")
    return any(m.start() < last_src for m in cmp_re.finditer(body))


def test_the_generation_stamp_scanner_finds_a_known_sample():
    sample = ("{ var gen = (iframe.__pdcChartGen || 0) + 1; iframe.__pdcChartGen = gen;\n"
              "  return fetch(u).then(function (d) {\n"
              "    if (iframe.__pdcChartGen !== gen) return;\n"
              "    iframe.src = d.url; }); }")
    assert _generation_stamps(sample) == {"__pdcChartGen"}
    assert _compared_before_src(sample, "__pdcChartGen")
    unguarded = "{ iframe.__g = 1; fetch(u).then(function (d) { iframe.src = d.url; }); }"
    assert not _compared_before_src(unguarded, "__g")
    ds = ("{ iframe.dataset.chartGen = String(n);\n"
          "  p.then(function () { if (iframe.dataset.chartGen !== String(n)) return;\n"
          "  iframe.src = x; }); }")
    assert _generation_stamps(ds) == {"chartGen"}
    assert _compared_before_src(ds, "chartGen")


def test_set_chart_frame_ignores_a_registration_that_resolves_late():
    """Two rapid refreshes race on `iframe.src`: the first registration may
    resolve after the second. `setChartFrame` stamps a per-frame generation
    when it starts and compares it before assigning `src`, so a stale answer
    is dropped (Task 8b Part A #7)."""
    body = _function_body(_read(VIEWERS), "setChartFrame")
    assert body, "setChartFrame is missing"
    stamps = _generation_stamps(body)
    assert stamps, "setChartFrame stores no per-frame generation on the iframe"
    assert any(_compared_before_src(body, name) for name in stamps), (
        "setChartFrame never compares the frame's generation before setting src", stamps)


SIDEBAR_RENDERERS = ("renderConversations", "renderMyChats", "renderSharedChats")
UNESCAPED_SIDEBAR_SINKS = ("${chat.owner", "${chat.files", "${conv.chat_name",
                           "${chat.chat_id}", "${conv.chat_id}", "${conv.conv_id}",
                           "${chat.slug")


def _template_expressions(body: str) -> list:
    """The source of every `${...}` in `body`, braces matched."""
    out = []
    i = 0
    while True:
        start = body.find("${", i)
        if start < 0:
            return out
        depth = 0
        end = None
        for j in range(start + 1, len(body)):
            if body[j] == "{":
                depth += 1
            elif body[j] == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end is None:
            out.append(body[start + 2:])
            return out
        out.append(body[start + 2:end])
        i = end + 1


def _strip_escaped_calls(expr: str) -> str:
    """`expr` with every `escapeHtml(...)` call (parens matched) removed."""
    out, i = [], 0
    while True:
        k = expr.find("escapeHtml(", i)
        if k < 0:
            out.append(expr[i:])
            return "".join(out)
        out.append(expr[i:k])
        depth = 0
        end = len(expr) - 1
        for j in range(k + len("escapeHtml"), len(expr)):
            if expr[j] == "(":
                depth += 1
            elif expr[j] == ")":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        i = end + 1


# After removing the escaped calls, what is left may only be a condition that
# selects between string literals / escaped values: `a === b ? ' active' : ''`
# or `chat.files ? <escaped> : ''`. The condition itself is never rendered.
_CONSTANT_REST_RE = re.compile(
    r"^\s*(?:[\w.$]+\s*(?:===|!==)?\s*[\w.$]*\s*\?\s*)?(?:'[^']*'|\"[^\"]*\")?\s*"
    r"(?::\s*(?:'[^']*'|\"[^\"]*\")?\s*)?$")


def test_the_sidebar_escape_scanner_reads_a_known_sample():
    sample = ("function renderX(c) { return `<b data-id=\"${escapeHtml(c.id)}\""
              " class=\"${c.id === cur ? ' active' : ''}\">"
              "${c.files ? escapeHtml(c.files.join(', ')) : ''}"
              "${c.owner}</b>`; }")
    exprs = _template_expressions(_function_body(sample, "renderX"))
    assert exprs == ["escapeHtml(c.id)", "c.id === cur ? ' active' : ''",
                     "c.files ? escapeHtml(c.files.join(', ')) : ''", "c.owner"], exprs
    verdicts = [bool(_CONSTANT_REST_RE.match(_strip_escaped_calls(e))) for e in exprs]
    assert verdicts == [True, True, True, False], verdicts


@pytest.mark.parametrize("name", SIDEBAR_RENDERERS)
def test_the_sidebar_renderers_escape_every_interpolated_value(name):
    """A chat's owner address, file names and the conversation's chat name
    are user-controlled; the sidebar builds its rows through innerHTML, so
    every interpolated value -- text and `data-*` attribute alike -- passes
    `escapeHtml` (Task 8b Part A #11; Article XV rule 4). The one exemption
    is a condition that only chooses between string literals (the `active`
    class), which renders no user value."""
    body = _function_body(_read(DASHBOARD_JS), name)
    assert body, f"{name} is missing from static/dashboard.js"
    raw = [sink for sink in UNESCAPED_SIDEBAR_SINKS if sink in body]
    assert raw == [], (name, raw)
    bad = [e.strip() for e in _template_expressions(body)
           if not _CONSTANT_REST_RE.match(_strip_escaped_calls(e))]
    assert bad == [], (name, bad)


LIVE_SIDEBAR_RENDERER = "renderUnifiedChatList"
LIVE_SIDEBAR_ID_SINKS = ("${chatId}", "${chat.slug", "${conv.conv_id}", "${conv.chat_id}")


def test_the_live_sidebar_renderer_escapes_its_ids_and_subtitle():
    """`renderUnifiedChatList` is the sidebar the page actually builds (the
    three renderers above are no longer called). Its subtitle -- the sharer's
    address or the file names -- goes through `escapeHtml` at render time, and
    the chat / conversation ids in its `data-*` attributes do too."""
    body = _function_body(_read(DASHBOARD_JS), LIVE_SIDEBAR_RENDERER)
    assert body, f"{LIVE_SIDEBAR_RENDERER} is missing from static/dashboard.js"
    raw = [sink for sink in LIVE_SIDEBAR_ID_SINKS if sink in body]
    assert raw == [], raw
    assert "escapeHtml(subtitle)" in body
    assert "${subtitle}" not in body


_CSP_NAME = "content-security-policy"
_SKIP_DIRS = {"tests", ".venv", "venv", "node_modules", "__pycache__", ".git",
              "docs", ".claude"}


def _application_modules() -> list:
    out = []
    for p in sorted(ROOT.rglob("*.py")):
        rel = p.relative_to(ROOT).parts
        if any(part in _SKIP_DIRS for part in rel[:-1]):
            continue
        out.append(p)
    return out


def _policy_header_literals(path: Path) -> list:
    """Line numbers of string/bytes literals (docstrings excluded) that name
    the Content-Security-Policy header."""
    import ast
    tree = ast.parse(_read(path))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.add(id(first.value))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and id(node) not in docstrings:
            value = node.value
            if isinstance(value, bytes):
                value = value.decode("latin-1")
            if isinstance(value, str) and _CSP_NAME in value.lower():
                hits.append(node.lineno)
    return hits


def test_only_the_chart_route_sets_its_own_policy_header():
    """The page-policy middleware leaves any HTML response that already
    carries a policy alone, so a module that sets one opts its page out of
    the nonce policy. Outside the middleware (`app.py`) the ONLY such module
    is the chart-document route (Task 8b Part A #13/#21)."""
    owners = {str(p.relative_to(ROOT)).replace("\\", "/"): _policy_header_literals(p)
              for p in _application_modules()}
    owners = {k: v for k, v in owners.items() if v}
    assert "routes/charts.py" in owners, "the scanner no longer sees the chart route"
    extra = {k: v for k, v in owners.items() if k not in ("app.py", "routes/charts.py")}
    assert extra == {}, extra
