"""The /lab sidebar's desktop controls, pinned at the source level.

On desktop the sidebar collapses to a 56px rail (`#btnSidebarCollapse`) and
its width is dragged between 240 and 480 px (`#sidebarResizer`). Both states
persist in localStorage under two keys and are applied by a nonced head
script before first paint. Every rule for them sits under
`@media (min-width: 769px)`, so the mobile drawer (`#btnHamburger`,
`#btnCloseSidebar`, `#sidebarBackdrop`) is unchanged.
"""
import re
from pathlib import Path

from test_rendering_isolation import _function_body

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "dashboard.html"
JS = ROOT / "static" / "dashboard.js"
CSS = ROOT / "static" / "dashboard.css"
I18N = ROOT / "static" / "i18n.js"

KEYS = ("pdc_sidebar_collapsed", "pdc_sidebar_width")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def _head_script():
    html = _read(TEMPLATE)
    head = html[:html.index("</head>")]
    scripts = re.findall(r"<script([^>]*)>(.*?)</script>", head, re.S)
    mine = [(attrs, body) for attrs, body in scripts if "pdc_sidebar_collapsed" in body]
    assert len(mine) == 1, "exactly one head script applies the sidebar state"
    return mine[0]


def _desktop_block(css: str) -> str:
    m = re.search(r"@media\s*\(min-width:\s*769px\)\s*\{", css)
    assert m, "desktop media block missing"
    depth = 0
    for j in range(m.end() - 1, len(css)):
        if css[j] == "{":
            depth += 1
        elif css[j] == "}":
            depth -= 1
            if depth == 0:
                return css[m.end():j]
    raise AssertionError("unterminated desktop media block")


def test_the_template_carries_the_desktop_controls_and_keeps_the_mobile_ones():
    html = _read(TEMPLATE)
    for needle in ('id="btnSidebarCollapse"', 'id="sidebarResizer"',
                   'class="sidebar-logo-mark"', 'id="btnCreateNew"',
                   'id="btnHamburger"', 'id="btnCloseSidebar"', 'id="sidebarBackdrop"'):
        assert needle in html, needle
    button = re.search(r'<button id="btnSidebarCollapse"[^>]*>', html).group(0)
    assert 'aria-label="' in button and 'aria-expanded="true"' in button
    assert 'type="button"' in button
    assert not re.search(r"\son[a-z]+\s*=", button)


def test_the_head_script_is_nonced_and_guards_every_storage_read():
    attrs, body = _head_script()
    assert 'nonce="{{ request.state.csp_nonce }}"' in attrs
    for key in KEYS:
        assert key in body
    reads = body.count("localStorage")
    assert reads == 2 and body.count("try {") == reads
    assert "240" in body and "480" in body


def test_dashboard_js_touches_storage_only_through_the_guarded_helpers():
    src = _read(JS)
    get_body, set_body = _function_body(src, "_lsGet"), _function_body(src, "_lsSet")
    assert "try" in get_body and "catch" in get_body
    assert "try" in set_body and "catch" in set_body
    rest = src.replace(get_body, "").replace(set_body, "")
    assert "localStorage" not in rest
    for key in KEYS:
        assert f"'{key}'" in src
    assert "SIDEBAR_MIN_WIDTH = 240" in src and "SIDEBAR_MAX_WIDTH = 480" in src


def test_the_collapse_and_resize_rules_are_desktop_only():
    css = _read(CSS)
    desktop = _desktop_block(css)
    for needle in ("html.sidebar-collapsed .sidebar", "width: 56px",
                   ".sidebar-resizer", "cursor: col-resize", ".sidebar-collapse-btn"):
        assert needle in desktop, needle
    outside = css.replace(desktop, "")
    assert "sidebar-collapsed" not in outside
    # Outside the desktop block the controls are hidden, so the mobile
    # drawer never shows them.
    assert re.search(r"\.sidebar-collapse-btn,\s*\.sidebar-resizer,\s*"
                     r"\.sidebar-logo-mark\s*\{\s*display:\s*none;", outside)
    assert "width: var(--sidebar-width, 320px)" in css


def test_the_labels_exist_in_every_language():
    src = _read(I18N)
    for key in ("lab.sidebar_collapse", "lab.sidebar_expand", "lab.sidebar_resize"):
        assert src.count(f"'{key}':") == 3, key
