"""Task 14 item 7 and Task 12b follow-up (Part A #8) — small front-end pins.

* The "data as of" pill in the /lab top bar (`#dataAsOfBadge`, rendered with
  the `hidden` class on file-only chats) is actually hidden: dashboard.css has
  no generic `.hidden`, so the badge needs its own element-specific rule
  `.data-as-of-badge.hidden { display: none }`.
* The sign-in page carries the favicon link the other pages have (no more
  404 for /favicon.ico), without the `?v={{ ts }}` stamp (`ts` is not in the
  landing context).
* A `ROLE_DENIED` answer on the Excel download (and the full-table fetch) of
  the chat page maps to the existing localized `lab.refresh_no_access`
  message instead of the generic "Export failed" — the server's `code` is
  the contract, its text is unchanged.

Offline: text-level scans (the tests/test_rendering_isolation.py idiom).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"
TEMPLATES = ROOT / "templates"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _css_rules(css: str) -> list[tuple[str, str]]:
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _function_body(src: str, name: str) -> str:
    """The tests/test_rendering_isolation.py helper (declaration, `name =
    function`, or arrow form), braces matched naively; "" when absent."""
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
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return src[start:]


# ===========================================================================
# item 7 — the empty pill and the sign-in favicon
# ===========================================================================
def test_the_data_as_of_badge_has_its_own_hidden_rule():
    rules = _css_rules(_read(STATIC / "dashboard.css"))
    hits = [body for sel, body in rules
            if any(re.fullmatch(r"\.data-as-of-badge\.hidden", s.strip())
                   for s in sel.split(","))]
    assert hits, "dashboard.css has no `.data-as-of-badge.hidden` rule"
    assert any(re.search(r"display\s*:\s*none", body) for body in hits), hits


def test_the_badge_still_starts_hidden_in_the_template():
    html = _read(TEMPLATES / "dashboard.html")
    assert re.search(r'id="dataAsOfBadge"[^>]*class="[^"]*\bdata-as-of-badge\b[^"]*\bhidden\b',
                     html)


def test_the_sign_in_page_links_the_favicon():
    html = _read(TEMPLATES / "auth_landing.html")
    links = re.findall(r"<link\b[^>]*>", html, re.I)
    icons = [ln for ln in links if re.search(r"""rel\s*=\s*["'](?:shortcut\s+)?icon["']""", ln, re.I)]
    assert icons, "auth_landing.html has no rel=icon link"
    assert any("/static/logo.png" in ln for ln in icons), icons
    for ln in icons:
        assert "{{ ts }}" not in ln and "{{ts}}" not in ln, \
            f"`ts` is not in the landing context: {ln}"


# ===========================================================================
# Part A #8 — a role denial on Excel / full table shows the localized text
# ===========================================================================
def _region_after(src: str, needle: str, end_marker: str, span: int = 4000) -> str:
    start = src.find(needle)
    assert start >= 0, f"{needle!r} not found"
    end = src.find(end_marker, start)
    if end < 0 or end - start > span:
        end = start + span
    return src[start:end]


def _mentions_role_denied_message(region: str, src: str) -> bool:
    if "ROLE_DENIED" in region and "lab.refresh_no_access" in region:
        return True
    # The mapping may live in a named helper the handler calls.
    for name in set(re.findall(r"([A-Za-z_$][\w$]*)\s*\(", region)):
        body = _function_body(src, name)
        if body and "ROLE_DENIED" in body and "lab.refresh_no_access" in body:
            return True
    return False


def test_the_excel_download_maps_a_role_denial_to_the_localized_message():
    src = _read(STATIC / "dashboard.js")
    region = _region_after(src, "/download_excel/", "actionBar.appendChild(downloadBtn)")
    assert _mentions_role_denied_message(region, src), \
        "the Download Excel handler does not map ROLE_DENIED to lab.refresh_no_access"


def test_the_full_table_fetch_maps_a_role_denial_to_the_localized_message():
    src = _read(STATIC / "dashboard.js")
    region = _region_after(src, "/full_table/", "bar.appendChild(dataBtn)")
    assert _mentions_role_denied_message(region, src), \
        "the full-table fetch does not map ROLE_DENIED to lab.refresh_no_access"
