"""Task 14 item 5 — a 401 sends the browser to the sign-in page.

The contract pinned here (Task 14 item 5, D14-6, D14-14):

* ONE shared helper, `static/http.js`, exposes `window.pdcFetch`. It is a
  TRANSPARENT pass-through: every argument goes to `fetch` unchanged
  (streamed chat bodies, blob downloads, headers, credentials, abort
  signals) and the ORIGINAL `Response` comes back for every status except
  401 — it never reads, clones or rebuilds the body.
* On 401 — except for `/auth/password`, whose 401 means "wrong current
  password" (dashboard.js shows `profile.pw_wrong_current` for it) — it
  navigates to `/?next=<encodeURIComponent(location.pathname + search)>`
  and returns a promise that never settles, so no caller shows a
  "Not authenticated" toast. Sign-in ignores `next` (D14-6).
* `templates/dashboard.html` and `templates/dashboard_view.html` load
  `/static/http.js` before their page script, and no local script loaded
  before it calls `fetch`.
* `static/dashboard.js` and `static/dashboard_view.js` call no bare
  `fetch(` any more (every call goes through `pdcFetch`), and the four
  ad-hoc "Session expired, redirecting to login" blocks are gone.

Offline: text-level scans (the tests/test_rendering_isolation.py idiom — no
JS runner in this suite).
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"
HTTP_JS = STATIC / "http.js"
PAGES = {
    "dashboard.html": "dashboard.js",
    "dashboard_view.html": "dashboard_view.js",
}
PAGE_SCRIPTS = [STATIC / "dashboard.js", STATIC / "dashboard_view.js"]

_SCRIPT_SRC_RE = re.compile(r"""<script\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""", re.I)
# `fetch(` not preceded by an identifier character: catches `fetch(`,
# `window.fetch(`, `self.fetch(` — never `pdcFetch(` (capital F).
_BARE_FETCH_RE = re.compile(r"(?<![A-Za-z0-9_$])fetch\s*\(")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_js_comments(src: str) -> str:
    """Block comments and `//` line comments that start a line or follow
    whitespace (so a `https://` inside a string survives)."""
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    return re.sub(r"(?m)(^|[\s;{}()])//[^\n]*", r"\1", src)


def _http_src() -> str:
    if not HTTP_JS.is_file():
        pytest.fail("static/http.js does not exist")
    return _strip_js_comments(_read(HTTP_JS))


def _local_script_path(src: str):
    path = src.split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/static/"):
        return None
    return STATIC / path[len("/static/"):]


# ===========================================================================
# the helper
# ===========================================================================
def test_the_shared_helper_exists_and_is_exposed_as_pdc_fetch():
    src = _http_src()
    assert re.search(r"window\.pdcFetch\s*=", src), "window.pdcFetch is not defined"


def test_the_helper_intercepts_status_401_only():
    src = _http_src()
    assert re.search(r"\bstatus\s*={2,3}\s*401\b", src), "no status === 401 check"
    compared = re.findall(r"\bstatus\s*[!=]={1,2}\s*(\d{3})\b", src)
    assert set(compared) == {"401"}, f"the helper reacts to other statuses: {compared}"
    assert not re.search(r"\.ok\b", src), "the helper must not branch on res.ok"


def test_the_helper_exempts_the_password_change_endpoint():
    """`/auth/password` answers 401 for a wrong CURRENT password — the page
    shows that message; the helper must not sign the user out for it."""
    assert "/auth/password" in _http_src()


def test_the_helper_sends_the_browser_to_sign_in_with_the_current_path():
    src = _http_src()
    assert "/?next=" in src, "the redirect target is not the sign-in page with ?next="
    assert "location.pathname" in src
    assert "encodeURIComponent" in src
    assert re.search(r"location\.(assign|replace)\s*\(|location\.href\s*=|"
                     r"window\.location\s*=", src), "no navigation to sign-in"


def test_the_helper_returns_a_promise_that_never_settles_on_401():
    src = _http_src()
    assert re.search(r"new\s+Promise\s*\(\s*(?:function\s*\(\s*\)\s*\{\s*\}|"
                     r"\(\s*\)\s*=>\s*\{\s*\})\s*\)", src), \
        "no never-settling promise for the 401 case"


def test_the_helper_is_a_transparent_pass_through():
    """D14-14: arguments forwarded unchanged, the original Response returned
    — no body read, no clone, no rebuilt Response."""
    src = _http_src()
    assert re.search(r"\bfetch\s*\(\s*(?:\.\.\.\s*[A-Za-z_$][\w$]*|"
                     r"[A-Za-z_$][\w$]*\s*,\s*[A-Za-z_$][\w$]*)\s*\)", src), \
        "fetch is not called with the caller's own arguments"
    for reader in ("json", "text", "blob", "clone", "arrayBuffer", "formData"):
        assert not re.search(rf"\.{reader}\s*\(", src), f"the helper calls .{reader}()"
    assert not re.search(r"new\s+Response\s*\(", src), "the helper rebuilds the Response"


# ===========================================================================
# the templates
# ===========================================================================
@pytest.mark.parametrize("template, page_script", sorted(PAGES.items()))
def test_both_pages_load_the_helper_before_their_page_script(template, page_script):
    srcs = _SCRIPT_SRC_RE.findall(_read(TEMPLATES / template))
    names = [s.split("?", 1)[0] for s in srcs]
    assert "/static/http.js" in names, f"{template} does not load /static/http.js"
    assert f"/static/{page_script}" in names, (template, names)
    http_at = names.index("/static/http.js")
    assert http_at < names.index(f"/static/{page_script}"), \
        f"{template} loads http.js after {page_script}"
    for earlier in srcs[:http_at]:
        path = _local_script_path(earlier)
        if path is None or not path.is_file():
            continue
        assert not _BARE_FETCH_RE.search(_strip_js_comments(_read(path))), \
            f"{template}: {earlier} calls fetch but loads before http.js"


# ===========================================================================
# the page scripts
# ===========================================================================
@pytest.mark.parametrize("path", PAGE_SCRIPTS, ids=lambda p: p.name)
def test_page_scripts_call_no_bare_fetch(path):
    src = _strip_js_comments(_read(path))
    lines = [ln.strip() for ln in src.splitlines() if _BARE_FETCH_RE.search(ln)]
    assert not lines, f"{path.name} still calls fetch directly: {lines[:5]}"
    assert src.count("pdcFetch(") >= 10, \
        f"{path.name} does not route its requests through pdcFetch"


@pytest.mark.parametrize("path", PAGE_SCRIPTS, ids=lambda p: p.name)
def test_the_ad_hoc_401_redirects_are_gone(path):
    assert "Session expired, redirecting to login" not in _read(path)


def test_the_password_change_keeps_its_wrong_password_message():
    """The one caller that reads a 401 itself still does (the helper exempts
    `/auth/password`)."""
    src = _read(STATIC / "dashboard.js")
    assert "profile.pw_wrong_current" in src
    assert re.search(r"status\s*===\s*401\s*\?\s*t\('profile\.pw_wrong_current'", src)
