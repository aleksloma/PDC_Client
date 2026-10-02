"""One "Download Analytics" dropdown replaces the two top-bar buttons.

The /lab top bar used to carry a separate "Run Auto Analytics" button next to
the "Download Analytics" dropdown. The Auto Analytics action is now the first
ITEM of that dropdown, followed by the current conversation's PDF and
PowerPoint. Pinned at the source level (template, script, stylesheet, i18n).

The item reads "Run Auto Analytics" while no presentation exists and "Download
Auto Analytics" once one does (same sparkle icon in both states); a click in
the done state only downloads, and a run that finishes while the chat is open
downloads its presentation by itself.
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

POPUP_BODY_EN = ("Auto analysis is running in the background. It can take "
                 "several minutes. The presentation will be downloaded "
                 "automatically when it is ready.")

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


def _brace_block(src: str, pos: int) -> str:
    """The `{ ... }` block that starts at the first `{` at or after `pos`,
    braces matched naively. "" when there is none."""
    start = src.find("{", pos)
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


def _module_body(src: str) -> str:
    """Source of the `const AutoAnalytics = (function () { ... })();` module."""
    m = re.search(r"\bAutoAnalytics\s*=\s*\(\s*(?:async\s+)?function\s*\(\s*\)", src)
    return _brace_block(src, m.end()) if m else ""


def _inner_function(module: str, name: str) -> str:
    """Body of the function `name` declared INSIDE the module (the file has
    other functions with the same plain names, e.g. `refresh`)."""
    return _function_body(module, name)


def _done_branch(body: str) -> str:
    """The block guarded by the `status === 'done'` comparison (either order
    of the operands) inside `body`. "" when there is none."""
    m = re.search(r"""(?:\bstatus\s*===?\s*['"]done['"]|['"]done['"]\s*===?\s*[\w.]*status\b)""",
                  body)
    return _brace_block(body, m.end()) if m else ""


_JS_STRING_OR_COMMENT = re.compile(
    r"""('(?:\\.|[^'\\\n])*'|"(?:\\.|[^"\\\n])*"|`(?:\\.|[^`\\])*`)"""
    r"""|/\*.*?\*/|//[^\n]*""",
    re.DOTALL)


def _strip_js_comments(src: str) -> str:
    """`src` with every `// ...` and `/* ... */` comment replaced by a space.
    String and template literals are skipped, so a `//` inside one survives
    (and an apostrophe inside a comment cannot open a fake string)."""
    return _JS_STRING_OR_COMMENT.sub(
        lambda m: m.group(1) if m.group(1) is not None else " ", src)


_HIDDEN_CHECK = re.compile(r"""\.classList\.contains\(\s*(['"`])hidden\1\s*\)""")


def _calls(body: str, name: str) -> list:
    return [m.start() for m in re.finditer(rf"\b{re.escape(name)}\s*\(", body)]


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


def test_the_item_label_reads_run_auto_analytics():
    """The idle label: no finished presentation yet, so the item offers to run
    the analysis. (It used to read "Auto Analytics".)"""
    auto = _one(_tree(), "btnAutoAnalytics")
    labels = [n for n in auto.descendants()
              if n.tag == "span" and "aa-btn-label" in n.classes]
    assert len(labels) == 1
    assert " ".join(labels[0].text().split()) == "Run Auto Analytics"


def test_the_popup_paragraph_promises_the_automatic_download():
    root = _tree()
    paras = [n for n in root.descendants()
             if n.attrs.get("data-i18n") == "lab.auto_analytics_popup_body"]
    assert len(paras) == 1, [(n.tag, n.attrs) for n in paras]
    assert " ".join(paras[0].text().split()) == POPUP_BODY_EN
    popup = _one(root, "autoAnalyticsPopup")
    assert popup in list(paras[0].ancestors())


def test_the_analytics_block_has_no_inline_handler():
    root = _tree()
    dropdown = _one(root, "downloadReportDropdown")
    for node in [dropdown, *dropdown.descendants()]:
        inline = [a for a in node.attrs if a.lower().startswith("on")]
        assert not inline, (node.tag, node.attrs)


# ── i18n ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["lab.run_auto_analytics",
                                 "lab.download_auto_analytics",
                                 "lab.auto_analytics_popup_body",
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
    assert _i18n_value(en, "lab.run_auto_analytics") == "Run Auto Analytics"
    assert _i18n_value(en, "lab.download_auto_analytics") == "Download Auto Analytics"
    pdf = _i18n_value(en, "lab.current_analytics_pdf")
    pptx = _i18n_value(en, "lab.current_analytics_pptx")
    assert pdf and "Current Analytics" in pdf, pdf
    assert pptx and "Current Analytics" in pptx, pptx
    assert pdf != pptx


def test_the_popup_body_promises_the_automatic_download_in_every_language():
    blocks = _lang_blocks()
    en = _i18n_value(blocks["en"], "lab.auto_analytics_popup_body")
    geo = _i18n_value(blocks["geo"], "lab.auto_analytics_popup_body")
    ru = _i18n_value(blocks["ru"], "lab.auto_analytics_popup_body")
    assert en == POPUP_BODY_EN, en
    assert geo and "ავტომატურად" in geo, geo
    assert ru and "автоматически" in ru, ru


def test_the_popup_body_no_longer_promises_a_notification():
    blocks = _lang_blocks()
    en = _i18n_value(blocks["en"], "lab.auto_analytics_popup_body") or ""
    geo = _i18n_value(blocks["geo"], "lab.auto_analytics_popup_body") or ""
    ru = _i18n_value(blocks["ru"], "lab.auto_analytics_popup_body") or ""
    assert "be notified" not in en and "notified" not in en, en
    assert "შეგატყობინებთ" not in geo, geo
    assert "мы сообщим" not in ru, ru


# ── script ─────────────────────────────────────────────────────────────────

def test_the_structural_helpers_find_the_module_and_its_functions():
    """Self-test of the scanners, so a green run cannot be a blind one."""
    sample = ("const AutoAnalytics = (function () {\n"
              "  function a() { if (data.status === 'done') { download(x); } }\n"
              "  return { a };\n"
              "})();\n"
              "function refresh() { download(1); }\n")
    module = _module_body(sample)
    assert module.startswith("{") and module.endswith("}")
    assert "function refresh" not in module
    assert _inner_function(module, "a").startswith("{ if (")
    assert _inner_function(module, "refresh") == ""
    assert _done_branch(_inner_function(module, "a")) == "{ download(x); }"
    assert _done_branch("if ('done' === data.status) { y(); }") == "{ y(); }"
    assert _done_branch("nothing here") == ""
    assert _strip_js_comments(
        "a(); // item hidden\nb('//x'); /* hidden\n it's */ c(\"/*y*/\");") \
        == "a();  \nb('//x');   c(\"/*y*/\");"
    assert _HIDDEN_CHECK.search("if (!b.classList.contains('hidden')) x();")
    assert _HIDDEN_CHECK.search('b.classList.contains( "hidden" )')
    assert not _HIDDEN_CHECK.search("b.classList.contains('hidden\")")
    assert _calls("download(x); _download(y); download (z)", "download") \
        and len(_calls("download(x); _download(y); download (z)", "download")) == 2

    module = _module_body(_read(JS))
    assert module, "the AutoAnalytics module is missing"
    for name in ("onClick", "startPolling", "refresh", "showPopup",
                 "download", "start", "setState"):
        assert _inner_function(module, name), f"{name} missing from the module"


def test_the_idle_label_uses_the_run_key():
    module = _module_body(_read(JS))
    assert "lab.run_auto_analytics" in module


def test_the_done_label_uses_the_download_key():
    module = _module_body(_read(JS))
    assert "lab.download_auto_analytics" in module


def test_the_icon_is_the_same_sparkle_in_every_state():
    module = _module_body(_read(JS))
    assert "📥" not in module
    assert "✨" in module


def test_a_click_in_the_done_state_only_downloads():
    body = _inner_function(_module_body(_read(JS)), "onClick")
    assert body, "onClick missing"
    done = re.search(r"""['"]done['"]""", body)
    assert done, "onClick has no done branch"
    downloads = [p for p in _calls(body, "download") if p > done.start()]
    starts = _calls(body, "start")
    assert downloads, "the done branch does not call download("
    assert starts, "onClick never calls start("
    assert downloads[0] < starts[0], "download( must come before start("
    assert re.search(r"\breturn\b", body[downloads[0]:starts[0]]), \
        "the done branch must return before start("


def test_a_run_that_finishes_while_the_chat_is_open_downloads_by_itself():
    module = _module_body(_read(JS))
    body = _inner_function(module, "startPolling")
    assert body, "startPolling missing"
    # Comments are stripped first: the branch EXPLAINS the hidden-item rule
    # in a comment, which must not stand in for the guard itself.
    done = _done_branch(_strip_js_comments(body))
    assert done, "startPolling has no status === 'done' branch"
    downloads = _calls(done, "download")
    assert downloads, "the done branch does not call download("
    guard = _HIDDEN_CHECK.search(done)
    assert guard, \
        "the automatic download must be conditioned on the item not being " \
        "hidden (a classList.contains('hidden') check in the done branch)"
    assert guard.start() < downloads[0], \
        "the classList.contains('hidden') check must come before download("


def test_an_already_finished_run_found_on_chat_open_is_not_downloaded():
    body = _inner_function(_module_body(_read(JS)), "refresh")
    assert body, "refresh missing"
    assert not _calls(body, "download"), body


def test_the_popup_still_auto_dismisses_after_five_seconds():
    body = _inner_function(_module_body(_read(JS)), "showPopup")
    assert body, "showPopup missing"
    assert "5000" in body


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
