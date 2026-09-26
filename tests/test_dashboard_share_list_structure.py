"""The dashboard page's sharing controls, pinned at the source level.

Two elements share the title row of `dashboard_view.html`:

- `#dashSharedBadge` — "Shared by <owner>", for a RECIPIENT only;
- `#btnSharedWith` + `#sharedWithMenu` — the OWNER's "Shared with N" button and
  its dropdown, where unticking an address calls `POST .../unshare`.

The page loads no generic `.hidden` rule, so the owner used to see the
recipient badge as an empty green pill: the element kept its `hidden` class
and nothing hid it. The scoped rule in `dashboard_view.css` is what makes the
`hidden` class mean something in that row.
"""
import re
from pathlib import Path

from test_rendering_isolation import _function_body

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "dashboard_view.html"
JS = ROOT / "static" / "dashboard_view.js"
CSS = ROOT / "static" / "dashboard_view.css"
I18N = ROOT / "static" / "i18n.js"

KEYS = ("dash.shared_with", "dash.shared_with_hint", "dash.unshare_ok",
        "dash.unshare_failed")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _lang_blocks() -> dict:
    src = _read(I18N).replace("\r\n", "\n")
    starts = {lang: src.index(f"\n  {lang}: {{") for lang in ("en", "geo", "ru")}
    order = sorted(starts, key=starts.get)
    out = {}
    for i, lang in enumerate(order):
        end = starts[order[i + 1]] if i + 1 < len(order) else src.index("\n};")
        out[lang] = src[starts[lang]:end]
    return out


def test_the_template_carries_the_owner_dropdown_and_the_recipient_badge():
    html = _read(TEMPLATE)
    assert 'id="btnSharedWith"' in html
    assert 'id="sharedWithMenu"' in html
    assert 'id="sharedWithList"' in html
    assert 'id="dashSharedBadge"' in html
    # All start hidden; the page script reveals the one that applies.
    assert re.search(r'id="btnSharedWith"[^>]*class="[^"]*\bhidden\b', html)
    assert re.search(r'id="sharedWithMenu"[^>]*class="[^"]*\bhidden\b', html)
    assert re.search(r'id="dashSharedBadge"[^>]*class="[^"]*\bhidden\b', html)


def test_the_title_row_has_a_hidden_rule():
    css = _read(CSS)
    assert re.search(r"\.dash-top-bar\s+\.hidden\s*\{\s*display:\s*none;?\s*\}", css)


def test_the_recipient_badge_is_filled_only_for_a_recipient():
    src = _read(JS)
    uses = [m.start() for m in re.finditer(r"getElementById\('dashSharedBadge'\)", src)]
    assert len(uses) == 1, "the badge must be touched in one place only"
    branch = src.rfind("if (!isOwner) {", 0, uses[0])
    assert branch != -1 and uses[0] - branch < 200


def test_the_owner_list_is_built_from_text_and_posts_unshare():
    src = _read(JS)
    render = _function_body(src, "renderSharedWith")
    assert render, "renderSharedWith missing"
    assert "isOwner" in render
    assert "textContent" in render and "innerHTML" not in render
    unshare = _function_body(src, "unshareAddress")
    assert "/unshare" in unshare
    # A failed call puts the tick back.
    assert "box.checked = true" in unshare


def test_the_owner_dropdown_closes_on_a_backdrop_and_escape():
    src = _read(JS)
    opener = _function_body(src, "openSharedMenu")
    assert "dash-shared-menu-backdrop" in opener
    assert "keydown" in opener
    assert "aria-expanded" in opener


def test_the_i18n_keys_exist_in_every_language():
    blocks = _lang_blocks()
    for lang, block in blocks.items():
        for key in KEYS:
            assert f"'{key}':" in block, (lang, key)
        assert re.search(r"'dash\.shared_with': '[^']*\{n\}", block), lang
