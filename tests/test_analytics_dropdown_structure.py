"""One "Download Analytics" dropdown replaces the two top-bar buttons.

The /lab top bar used to carry a separate "Run Auto Analytics" button next to
the "Download Analytics" dropdown. The Auto Analytics action is now the first
ITEM of that dropdown, followed by the current conversation's PDF and
PowerPoint. Pinned at the source level (template, script, stylesheet, i18n).
"""
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from test_rendering_isolation import _function_body

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "templates" / "dashboard.html"
JS = ROOT / "static" / "dashboard.js"
CSS = ROOT / "static" / "dashboard.css"
I18N = ROOT / "static" / "i18n.js"

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "param", "source", "track", "wbr"}


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


class _Node:
    def __init__(self, tag, attrs, parent):
        self.tag = tag
        self.attrs = dict(attrs)
        self.parent = parent
        self.children = []
        self.text_parts = []

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def text(self) -> str:
        return "".join(self.text_parts) + "".join(c.text() for c in self.children)

    def descendants(self):
        for c in self.children:
            yield c
            yield from c.descendants()

    def ancestors(self):
        n = self.parent
        while n is not None:
            yield n
            n = n.parent


class _Tree(HTMLParser):
    """A small element tree (stdlib only). Jinja blocks are plain text to it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", [], None)
        self.cur = self.root

    def handle_starttag(self, tag, attrs):
        node = _Node(tag, attrs, self.cur)
        self.cur.children.append(node)
        if tag not in _VOID:
            self.cur = node

    def handle_startendtag(self, tag, attrs):
        self.cur.children.append(_Node(tag, attrs, self.cur))

    def handle_endtag(self, tag):
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data):
        # Text is attributed in document order relative to nothing else; the
        # assertions only need "contains".
        self.cur.text_parts.append(data)


def _tree() -> _Node:
    p = _Tree()
    p.feed(_read(TEMPLATE))
    p.close()
    return p.root


def _by_id(root: _Node, el_id: str) -> list:
    return [n for n in root.descendants() if n.attrs.get("id") == el_id]


def _one(root: _Node, el_id: str) -> _Node:
    found = _by_id(root, el_id)
    assert len(found) == 1, f"#{el_id}: expected exactly one, found {len(found)}"
    return found[0]


def _menu_items(root: _Node) -> list:
    menu = _one(root, "downloadReportMenu")
    return [n for n in menu.descendants() if "dropdown-item" in n.classes]


def _lang_blocks() -> dict:
    src = _read(I18N).replace("\r\n", "\n")
    starts = {lang: src.index(f"\n  {lang}: {{") for lang in ("en", "geo", "ru")}
    order = sorted(starts, key=starts.get)
    out = {}
    for i, lang in enumerate(order):
        end = starts[order[i + 1]] if i + 1 < len(order) else src.index("\n};")
        out[lang] = src[starts[lang]:end]
    return out


def _i18n_value(block: str, key: str):
    m = re.search(rf"""['"]{re.escape(key)}['"]\s*:\s*(['"])(.*?)(?<!\\)\1""",
                  block)
    return m.group(2) if m else None


# ── template ───────────────────────────────────────────────────────────────

def test_the_auto_item_is_unique_and_lives_inside_the_menu():
    html = _read(TEMPLATE)
    assert len(re.findall(r"""id\s*=\s*["']btnAutoAnalytics["']""", html)) == 1
    root = _tree()
    auto = _one(root, "btnAutoAnalytics")
    ancestor_ids = [a.attrs.get("id") for a in auto.ancestors()]
    assert "downloadReportMenu" in ancestor_ids, ancestor_ids
    assert "dropdown-item" in auto.classes
    assert "top-action-btn" not in auto.classes


def test_the_auto_item_keeps_its_icon_and_label_spans():
    auto = _one(_tree(), "btnAutoAnalytics")
    icons = [n for n in auto.descendants()
             if n.tag == "span" and "aa-btn-icon" in n.classes]
    labels = [n for n in auto.descendants()
              if n.tag == "span" and "aa-btn-label" in n.classes]
    assert len(icons) == 1 and len(labels) == 1
    assert "✨" in icons[0].text()


def test_the_auto_item_itself_has_no_data_i18n():
    """applyAll sets textContent on a data-i18n element, which would wipe the
    icon and label spans."""
    auto = _one(_tree(), "btnAutoAnalytics")
    assert "data-i18n" not in auto.attrs


def test_the_menu_sits_in_the_dropdown_next_to_the_download_button():
    root = _tree()
    dropdown = _one(root, "downloadReportDropdown")
    menu = _one(root, "downloadReportMenu")
    button = _one(root, "btnDownloadReport")
    assert dropdown in list(menu.ancestors())
    assert dropdown in list(button.ancestors())
    assert menu not in list(button.ancestors()), "the toggle is not a menu item"
    assert button.attrs.get("data-i18n") == "lab.download_report"


def test_the_menu_holds_exactly_three_items_in_order():
    items = _menu_items(_tree())
    assert len(items) == 3, [(n.tag, n.attrs) for n in items]
    auto, pdf, pptx = items

    assert auto.attrs.get("id") == "btnAutoAnalytics"

    assert pdf.attrs.get("data-format") == "pdf"
    assert pdf.attrs.get("data-i18n") == "lab.current_analytics_pdf"
    assert "📄" in pdf.text() and "Current Analytics" in pdf.text()

    assert pptx.attrs.get("data-format") == "pptx"
    assert pptx.attrs.get("data-i18n") == "lab.current_analytics_pptx"
    assert "📊" in pptx.text() and "Current Analytics" in pptx.text()


def test_only_the_two_current_items_carry_a_format():
    menu = _one(_tree(), "downloadReportMenu")
    formats = [n.attrs["data-format"] for n in menu.descendants()
               if "data-format" in n.attrs]
    assert formats == ["pdf", "pptx"]
    assert "data-format" not in _one(_tree(), "btnAutoAnalytics").attrs


def test_the_old_button_text_is_gone():
    assert "Run Auto Analytics" not in _read(TEMPLATE)


def test_the_analytics_block_has_no_inline_handler():
    root = _tree()
    dropdown = _one(root, "downloadReportDropdown")
    for node in [dropdown, *dropdown.descendants()]:
        inline = [a for a in node.attrs if a.lower().startswith("on")]
        assert not inline, (node.tag, node.attrs)


# ── i18n ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["lab.auto_analytics_option",
                                 "lab.current_analytics_pdf",
                                 "lab.current_analytics_pptx",
                                 "lab.download_report"])
def test_the_keys_exist_in_all_three_languages(key):
    src = _read(I18N)
    pattern = rf"""['"]{re.escape(key)}['"]\s*:"""
    assert len(re.findall(pattern, src)) == 3, key
    for lang, block in _lang_blocks().items():
        assert re.search(pattern, block), (key, lang)


def test_the_english_values():
    en = _lang_blocks()["en"]
    assert _i18n_value(en, "lab.auto_analytics_option") == "Auto Analytics"
    pdf = _i18n_value(en, "lab.current_analytics_pdf")
    pptx = _i18n_value(en, "lab.current_analytics_pptx")
    assert pdf and "Current Analytics" in pdf, pdf
    assert pptx and "Current Analytics" in pptx, pptx
    assert pdf != pptx


# ── script ─────────────────────────────────────────────────────────────────

def test_the_idle_label_uses_the_new_key():
    src = _read(JS)
    assert "lab.auto_analytics_option" in src


def test_the_done_state_is_shown_on_the_download_button():
    """The Auto item is hidden inside a closed menu, so the "ready" highlight
    has to land on the visible toggle."""
    src = _read(JS)
    hits = [m.start() for m in re.finditer(r"auto-analytics-done", src)]
    assert hits, "auto-analytics-done is no longer applied"
    near = [h for h in hits
            if "btnDownloadReport" in src[max(0, h - 600):h + 600]]
    assert near, "auto-analytics-done is never applied near btnDownloadReport"


@pytest.mark.parametrize("name", ["downloadReport", "_applyOwnerOnlyActions",
                                  "showTopBarActions",
                                  "updateReportButtonVisibility"])
def test_the_existing_functions_remain(name):
    assert _function_body(_read(JS), name), f"{name} missing"


def test_owner_only_actions_still_hide_the_auto_item():
    body = _function_body(_read(JS), "_applyOwnerOnlyActions")
    assert body, "_applyOwnerOnlyActions missing"
    assert "btnAutoAnalytics" in body
    assert "hidden" in body


# ── stylesheet ─────────────────────────────────────────────────────────────

def test_a_hidden_dropdown_item_is_not_displayed():
    """dashboard.css has no generic `.hidden`; the Auto item is hidden for a
    share recipient, so the menu needs its own rule."""
    css = re.sub(r"/\*.*?\*/", "", _read(CSS), flags=re.S)
    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", css)
    hiding = [sel for sel, decl in rules
              if ".dropdown-item.hidden" in sel
              and re.search(r"display\s*:\s*none", decl)]
    assert hiding, "no `.dropdown-item.hidden { display: none }` rule"
