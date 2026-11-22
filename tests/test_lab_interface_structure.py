"""C4 (p29, finding F9) — the /lab interface, pinned at the source level.

Three changes, each pinned by its observable contract rather than by exact
code (the browser half lives in tests/integration/test_lab_interface_browser.py):

1. ONE dismiss helper in static/dashboard.js. Escape closes the topmost open
   layer (modal or dropdown); a click outside closes it; opening a menu closes
   the others. The layers named in F9 — the View / Edit Descriptions modal
   (`#schemaViewerModal`), the profile dropdown (`#profileDropdown`), the
   Download Analytics dropdown (`#downloadReportMenu`) and the sidebar "⋯"
   menu (`showItemMenu`) — go through it, and so do the Rename / Share /
   Delete modals, each closing through its OWN close function (Rename and
   Delete resolve their promise there). The Profile, Download and sidebar "⋯"
   openers no longer hide their click from the others with `stopPropagation`
   — or the helper handles it centrally (its outside-click listener runs in
   the capture phase, or the opener hands itself to the helper).
   The helper is found by NAME: a top-level definition whose name contains
   "dismiss" or "layer" (case-insensitive). dashboard.js had none before C4.
2. The top bar does not wrap (`nowrap`), and below a breakpoint near 1200 px
   (1100 < N <= 1300) its buttons shorten to icons: the label element is
   hidden while the button keeps a `title` / `aria-label` that i18n fills.
   The breakpoint does not reach mobile (<= 768 px), which stays unchanged.
3. The data-type label in the Descriptions view reads Text / Number / Date /
   Yes-No through i18n keys (en / geo / ru) instead of the raw pandas dtype;
   the keys are found by their English values. The label is display only:
   the schema payload the page saves keeps the stored technical description.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
JS_PATH = ROOT / "static" / "dashboard.js"
CSS_PATH = ROOT / "static" / "dashboard.css"
I18N_PATH = ROOT / "static" / "i18n.js"
TEMPLATE_PATH = ROOT / "templates" / "dashboard.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


JS = _read(JS_PATH)
CSS = _read(CSS_PATH)
I18N = _read(I18N_PATH)
HTML = _read(TEMPLATE_PATH)

LANGS = ("en", "geo", "ru")
TOP_BAR_BUTTONS = ("btnDashboards", "btnDownloadReport", "btnAddData", "btnSchema")
NEAR_LINES = 30


# ---------------------------------------------------------------------------
# helpers — JS
# ---------------------------------------------------------------------------
def _strip_comments(src: str) -> str:
    """Block comments and `//` line comments (only when the `//` follows
    whitespace or starts a line, so `'http://…'` in a string survives).
    Offsets are NOT preserved — use only for name discovery."""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(^|[ \t])//[^\n]*", r"\1", src, flags=re.M)


def _match_from(src: str, start: int, open_ch: str, close_ch: str) -> int:
    """Index one past the bracket that closes the `open_ch` at/after `start`."""
    i = src.find(open_ch, start)
    if i < 0:
        return -1
    depth = 0
    for j in range(i, len(src)):
        if src[j] == open_ch:
            depth += 1
        elif src[j] == close_ch:
            depth -= 1
            if depth == 0:
                return j + 1
    return len(src)


def _function_body(src: str, name: str) -> str:
    m = re.search(
        rf"(function\s+{re.escape(name)}\s*\(|\b{re.escape(name)}\s*[:=]\s*(?:async\s+)?function\s*\(|"
        rf"\b{re.escape(name)}\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*=>)", src)
    if not m:
        return ""
    # Skip the parameter list, then take the brace-matched body.
    params_end = _match_from(src, m.end() - 1, "(", ")") if src[m.end() - 1] == "(" else m.end()
    start = src.find("{", params_end)
    if start < 0:
        return ""
    end = _match_from(src, start, "{", "}")
    return src[start:end]


_TOP_DEF = re.compile(
    r"^(?:async\s+)?(?:function\s*\*?\s*|(?:const|let|var|class)\s+|window\.)([A-Za-z_$][\w$]*)",
    re.M)
_HELPER_WORD = re.compile(r"dismiss|layer", re.I)


def _helper_names() -> list:
    names = {n for n in _TOP_DEF.findall(_strip_comments(JS)) if _HELPER_WORD.search(n)}
    return sorted(names)


def _helper_spans() -> list:
    """(start, end) of every top-level helper definition in the raw source."""
    spans = []
    for name in _helper_names():
        for m in re.finditer(
                rf"^(?:async\s+)?(?:function\s*\*?\s*|(?:const|let|var|class)\s+|window\.){re.escape(name)}\b",
                JS, re.M):
            end = _match_from(JS, m.end(), "{", "}")
            if end > 0:
                spans.append((m.start(), end))
    return spans


def _helper_src() -> str:
    return "\n".join(JS[a:b] for a, b in _helper_spans())


def _refs_helper(text: str) -> bool:
    return any(re.search(rf"\b{re.escape(n)}\b", text) for n in _helper_names())


def _in_helper(pos: int) -> bool:
    return any(a <= pos < b for a, b in _helper_spans())


def _listener_calls(events) -> list:
    """(pos, call_text) of every document/window addEventListener for one of
    `events` — the whole call, inline handler included."""
    out = []
    pat = re.compile(r"\b(?:document|window)\s*\.\s*addEventListener\s*\(\s*['\"](%s)['\"]"
                     % "|".join(events))
    for m in pat.finditer(JS):
        open_paren = JS.index("(", m.start())
        end = _match_from(JS, open_paren, "(", ")")
        out.append((m.start(), JS[m.start():end]))
    return out


def _helper_listener_calls(events) -> list:
    return [(pos, text) for pos, text in _listener_calls(events)
            if _in_helper(pos) or _refs_helper(text)]


def _line_of(pos: int) -> int:
    return JS.count("\n", 0, pos)


def _wired_near(anchors) -> bool:
    """An anchor occurrence lies inside a helper definition, or a helper name
    is used within NEAR_LINES lines of one."""
    lines = JS.split("\n")
    for anchor in anchors:
        for m in re.finditer(re.escape(anchor), JS):
            if _in_helper(m.start()):
                return True
            ln = _line_of(m.start())
            window = "\n".join(lines[max(0, ln - NEAR_LINES):ln + NEAR_LINES + 1])
            if _refs_helper(window):
                return True
    return False


def _add_listener_call(pos: int) -> str:
    """The whole `addEventListener(...)` call whose name starts at/after `pos`."""
    at = JS.index("addEventListener", pos)
    return JS[at:_match_from(JS, JS.index("(", at), "(", ")")]


def _opener_handlers() -> dict:
    """The click handler calls of the three openers named in F9, as found."""
    found = {}
    # Profile: `profileIconBtn.addEventListener('click', …)` or by id.
    for m in re.finditer(r"(?:\bprofileIconBtn|getElementById\(\s*['\"]btnProfileIcon['\"]\s*\))"
                         r"\s*\??\.\s*addEventListener\s*\(\s*['\"]click['\"]", JS):
        found.setdefault("profile", []).append(_add_listener_call(m.start()))
    # Download: `dlBtn.addEventListener('click', …)` or by id.
    for m in re.finditer(r"(?:\bdlBtn|getElementById\(\s*['\"]btnDownloadReport['\"]\s*\))"
                         r"\s*\??\.\s*addEventListener\s*\(\s*['\"]click['\"]", JS):
        found.setdefault("download", []).append(_add_listener_call(m.start()))
    # Sidebar ⋯: the click listener attached right after a menu-button query
    # of the live renderer (`renderUnifiedChatList` → attachUnifiedListeners;
    # `.item-menu-btn` belongs to the renderers nothing calls).
    for m in re.finditer(r"querySelectorAll\(\s*['\"]\.(?:chat-menu-btn|conversation-menu-btn)['\"]\s*\)",
                         JS):
        tail = JS[m.end():m.end() + 400]
        a = re.search(r"addEventListener\s*\(\s*['\"]click['\"]", tail)
        if not a:
            continue
        found.setdefault("sidebar", []).append(_add_listener_call(m.end() + a.start()))
    return found


# ---------------------------------------------------------------------------
# helpers — CSS / template
# ---------------------------------------------------------------------------
def _media_blocks(css: str) -> list:
    """(header, body) of every @media block."""
    out = []
    for m in re.finditer(r"@media([^{]*)\{", css):
        end = _match_from(css, m.end() - 1, "{", "}")
        out.append((m.group(1), css[m.end():end - 1]))
    return out


def _without_media(css: str) -> str:
    out, i = [], 0
    for m in re.finditer(r"@media[^{]*\{", css):
        if m.start() < i:
            continue
        out.append(css[i:m.start()])
        i = _match_from(css, m.end() - 1, "{", "}")
    out.append(css[i:])
    return "".join(out)


def _rules(css: str) -> list:
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [(sel.strip(), body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]


def _icon_media_blocks() -> list:
    """Media blocks whose max-width breakpoint lies near 1200 px (and covers 1100)."""
    out = []
    for header, body in _media_blocks(CSS):
        for w in re.findall(r"max-width\s*:\s*(\d+)px", header):
            if 1100 < int(w) <= 1300:
                out.append((header, body))
                break
    return out


_HIDE_DECL = re.compile(r"display\s*:\s*none|font-size\s*:\s*0(?:px)?\s*[;}]?|clip(?:-path)?\s*:")


def _label_hiding_rules() -> list:
    """(selector, body) of rules in the icon breakpoint that hide something
    inside the top bar's buttons."""
    out = []
    for _header, body in _icon_media_blocks():
        for sel, decls in _rules(body):
            if re.search(r"top-bar|top-action-btn|#btn(?:Dashboards|DownloadReport|AddData|Schema)", sel) \
                    and _HIDE_DECL.search(decls):
                out.append((sel, decls))
    return out


def _button(btn_id: str):
    m = re.search(rf'<button\s+id="{btn_id}"([^>]*)>(.*?)</button>', HTML, re.S)
    assert m, f"#{btn_id} is missing from dashboard.html"
    return m.group(1), m.group(2)


# ---------------------------------------------------------------------------
# helpers — i18n
# ---------------------------------------------------------------------------
def _lang_block(lang: str) -> str:
    starts = {k: I18N.index(f"\n  {k}: {{") for k in LANGS}
    order = sorted(starts, key=starts.get)
    i = order.index(lang)
    end = starts[order[i + 1]] if i + 1 < len(order) else len(I18N)
    return I18N[starts[lang]:end]


def _pairs(lang: str) -> dict:
    return {k: v for _q, k, _q2, v in
            re.findall(r"""(['"])([\w.\-]+)\1\s*:\s*(['"])(.*?)(?<!\\)\3""", _lang_block(lang))}


_KIND_RE = {
    "text": re.compile(r"^text$", re.I),
    "number": re.compile(r"^number$", re.I),
    "date": re.compile(r"^date$", re.I),
    "yesno": re.compile(r"^yes\s*[/\-–—]\s*no$", re.I),
}


def _type_keys() -> dict:
    """The four type-label keys, found by their English text and sharing one
    namespace (the part before the first dot)."""
    en = _pairs("en")
    cands = {kind: [k for k, v in en.items() if rx.match(v.strip())] for kind, rx in _KIND_RE.items()}
    missing = [kind for kind, ks in cands.items() if not ks]
    assert not missing, (f"no en i18n key whose text is the type label for {missing} "
                         "(expected Text / Number / Date / Yes-No)")
    for ns in sorted({k.split(".")[0] for ks in cands.values() for k in ks}):
        pick = {kind: next((k for k in ks if k.split(".")[0] == ns), None)
                for kind, ks in cands.items()}
        if all(pick.values()):
            return pick
    raise AssertionError(f"the type-label keys do not share a namespace: {cands}")


def _common_prefix(keys) -> str:
    keys = list(keys)
    p = keys[0]
    for k in keys[1:]:
        while not k.startswith(p):
            p = p[:-1]
    return p


def _type_label_functions(keys: dict) -> list:
    """Top-level JS functions whose body references the type-label keys."""
    prefix = _common_prefix(keys.values())
    names = []
    for m in re.finditer(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", JS, re.M):
        body = _function_body(JS, m.group(1))
        hits = sum(1 for k in keys.values() if k in body)
        if hits >= 2 or (len(prefix) > len(prefix.split(".")[0]) + 1 and f"'{prefix}" in body):
            names.append(m.group(1))
    for m in re.finditer(r"^(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", JS, re.M):
        end = _match_from(JS, m.end(), "{", "}")
        body = JS[m.end():end] if end > 0 else ""
        if sum(1 for k in keys.values() if k in body) >= 2:
            names.append(m.group(1))
    return names


# ===========================================================================
# sanity: the helpers find what they look for
# ===========================================================================
def test_the_helpers_find_existing_code():
    assert _function_body(JS, "showItemMenu")
    assert _function_body(JS, "handleRename")
    assert _function_body(JS, "_persistChatSchema")
    assert _media_blocks(CSS)
    assert _pairs("en") and _pairs("geo") and _pairs("ru")
    _button("btnSchema")


# ===========================================================================
# 1. the dismiss helper
# ===========================================================================
def test_one_dismiss_helper_is_defined():
    names = _helper_names()
    assert names, ("dashboard.js defines no dismiss helper (a top-level function/object "
                   "whose name contains 'dismiss' or 'layer')")
    assert _helper_spans(), names


def test_escape_closes_through_the_helper():
    calls = _helper_listener_calls(["keydown", "keyup"])
    assert calls, "no document/window keydown listener belongs to (or calls) the dismiss helper"
    helper = _helper_src()
    assert any("Escape" in text or "Escape" in helper for _pos, text in calls), \
        "the helper's key listener never checks for Escape"


def test_outside_click_closes_through_the_helper():
    calls = _helper_listener_calls(["click", "mousedown", "pointerdown"])
    assert calls, ("no document/window click / mousedown / pointerdown listener belongs to "
                   "(or calls) the dismiss helper")
    helper = _helper_src()
    assert any(re.search(r"\.contains\(|\.closest\(|composedPath\(", text + helper)
               for _pos, text in calls), \
        "the helper's outside-click listener never tests whether the click is inside the layer"


def test_there_is_one_global_escape_listener_for_the_layers():
    """The helper owns Escape for the layers: no second permanent document
    listener re-implements it for the profile / download / sidebar menus."""
    for _pos, text in _listener_calls(["keydown"]):
        if "Escape" not in text or _refs_helper(text) or _in_helper(_pos):
            continue
        for anchor in ("profileDropdown", "downloadReportMenu", "dlMenu",
                       "item-context-menu", "schemaViewerModal"):
            assert anchor not in text, (
                f"a separate Escape listener handles {anchor}; route it through the helper")


@pytest.mark.parametrize("layer,anchors", [
    ("descriptions modal", ("schemaViewerModal",)),
    ("profile dropdown", ("profileDropdown",)),
    ("download dropdown", ("downloadReportMenu", "dlMenu")),
])
def test_the_f9_layers_are_registered_with_the_helper(layer, anchors):
    assert _helper_names(), "no dismiss helper"
    assert _wired_near(anchors), (
        f"the {layer} ({', '.join(anchors)}) is not wired to the dismiss helper")


def test_the_sidebar_item_menu_is_registered_with_the_helper():
    body = _function_body(JS, "showItemMenu")
    assert body, "showItemMenu is gone"
    assert _helper_names(), "no dismiss helper"
    assert _refs_helper(body) or _wired_near(("item-context-menu",)), \
        "the sidebar ⋯ menu (showItemMenu) does not go through the dismiss helper"


@pytest.mark.parametrize("fn", ["handleRename", "handleShare", "handleDelete"])
def test_the_rename_share_delete_modals_close_through_their_own_close_function(fn):
    body = _function_body(JS, fn)
    assert body, f"{fn} is gone"
    assert _helper_names(), "no dismiss helper"
    assert _refs_helper(body), f"{fn}'s modal is not registered with the dismiss helper"
    # The helper is handed the modal's OWN close function (which, for Rename
    # and Delete, resolves the promise), not a bare classList toggle.
    near = []
    for n in _helper_names():
        for m in re.finditer(rf"\b{re.escape(n)}\b", body):
            near.append(body[max(0, m.start() - 200):m.end() + 300])
    assert any("closeModal" in w for w in near), (
        f"{fn} does not hand its closeModal to the dismiss helper")


@pytest.mark.parametrize("fn", ["handleRename", "handleDelete"])
def test_the_promise_modals_still_resolve_on_close(fn):
    body = _function_body(JS, fn)
    assert "new Promise" in body, fn
    close = _function_body(body, "closeModal")
    assert close and "resolve(" in close, f"{fn}'s closeModal no longer resolves its promise"


def test_openers_do_not_hide_their_click_from_the_other_layers():
    handlers = _opener_handlers()
    assert "profile" in handlers and "download" in handlers, (
        f"profile / download opener handlers not found: {sorted(handlers)}")
    # Central handling: the helper's CLOSING listener (one that tests where
    # the click landed, or calls the helper) runs in the capture phase, so a
    # bubbling stopPropagation cannot hide the click from it. A capture
    # listener that only records the target does not count.
    capture = any(re.search(r",\s*true\s*\)\s*;?\s*$|capture\s*:\s*true", text.strip())
                  and (re.search(r"\.contains\(|\.closest\(|composedPath\(", text)
                       or _refs_helper(text[len("addEventListener"):]))
                  for _pos, text in _helper_listener_calls(["click", "mousedown", "pointerdown"]))
    bad = []
    for which, blocks in handlers.items():
        for block in blocks:
            if "stopPropagation" not in block:
                continue
            if capture or _refs_helper(block):
                continue        # the helper handles it centrally
            bad.append((which, block[:160]))
    assert not bad, ("openers still call stopPropagation, hiding their click from the "
                     f"other layers' close listeners: {bad}")


# ===========================================================================
# 2. the top bar
# ===========================================================================
def test_the_top_bar_does_not_wrap():
    rules = [(sel, body) for sel, body in _rules(_without_media(CSS))
             if re.search(r"\.top-bar\b|\.top-bar-actions\b|\.top-action-btn\b", sel)]
    assert rules, "no .top-bar rules found"
    assert any(re.search(r"(?:white-space|flex-wrap)\s*:\s*nowrap", body) for _s, body in rules), \
        "no top-bar rule sets nowrap (white-space or flex-wrap)"
    for sel, body in rules:
        assert not re.search(r"flex-wrap\s*:\s*wrap\b", body), (sel, body)


def test_a_breakpoint_near_1200px_hides_the_button_labels():
    blocks = _icon_media_blocks()
    assert blocks, "no @media (max-width: N px) with 1100 < N <= 1300 in dashboard.css"
    assert _label_hiding_rules(), (
        "the ~1200 px breakpoint hides nothing inside the top-bar buttons")


def test_the_icon_breakpoint_does_not_reach_mobile():
    """Mobile (<= 768 px) is unchanged: the icon-only rules start above it."""
    blocks = _icon_media_blocks()
    assert blocks
    for header, _body in blocks:
        m = re.search(r"min-width\s*:\s*(\d+)px", header)
        assert m and int(m.group(1)) >= 769, (
            f"@media{header.strip()} also applies at <= 768 px — mobile must stay unchanged")


def test_the_existing_mobile_blocks_are_still_there():
    mobile = [h for h, _b in _media_blocks(CSS) if re.search(r"max-width\s*:\s*768px", h)]
    assert len(mobile) >= 4, mobile


@pytest.mark.parametrize("btn_id", TOP_BAR_BUTTONS)
def test_each_top_bar_button_keeps_a_localized_name(btn_id):
    attrs, _inner = _button(btn_id)
    has_name = re.search(r'\b(?:title|aria-label)="[^"]+"', attrs)
    has_i18n = re.search(r"\bdata-i18n-(?:title|aria-label)=\"[\w.\-]+\"", attrs)
    js_sets = re.search(
        rf"['\"]{btn_id}['\"][\s\S]{{0,400}}?(?:\.title\s*=|setAttribute\(\s*['\"](?:title|aria-label)['\"])"
        rf"[\s\S]{{0,120}}?_t\(", JS)
    assert (has_name and has_i18n) or js_sets, (
        f"#{btn_id} has no localized title / aria-label (template data-i18n-title / "
        "data-i18n-aria-label, or set through _t in dashboard.js)")


@pytest.mark.parametrize("btn_id", TOP_BAR_BUTTONS)
def test_each_top_bar_button_has_a_separate_hideable_label(btn_id):
    attrs, inner = _button(btn_id)
    # i18n's applyAll replaces the textContent of a [data-i18n] element, so a
    # data-i18n on the button itself would wipe the icon / label split.
    assert not re.search(r'\bdata-i18n="', attrs), (
        f"#{btn_id} carries data-i18n itself; the label must be a child element")
    assert re.search(r'<span[^>]*\bdata-i18n="[\w.\-]+"', inner), (
        f"#{btn_id} has no child label element with data-i18n")
    hiding = _label_hiding_rules()
    classes = set()
    for sel, _decls in hiding:
        for part in sel.split(","):
            last = part.strip().split()[-1] if part.strip() else ""
            classes.update(re.findall(r"\.([\w-]+)", last))
    classes.discard("top-action-btn")
    if classes:
        assert any(re.search(rf'class="[^"]*\b{re.escape(c)}\b', inner) for c in classes), (
            f"#{btn_id}'s label carries none of the classes the breakpoint hides: {sorted(classes)}")


@pytest.mark.parametrize("btn_id", TOP_BAR_BUTTONS)
def test_each_top_bar_button_keeps_an_icon(btn_id):
    """Shortened to an icon, not to nothing: something besides the label stays."""
    _attrs, inner = _button(btn_id)
    label_free = re.sub(r'<span[^>]*\bdata-i18n="[^"]*"[^>]*>.*?</span>', "", inner, flags=re.S)
    assert re.search(r"<svg|<img|class=\"[^\"]*icon|[^\x00-\x7f]", label_free), (
        f"#{btn_id} has nothing left to show once its label is hidden: {label_free.strip()[:120]}")


# ===========================================================================
# 3. type labels
# ===========================================================================
def test_the_type_label_keys_exist_in_every_language():
    keys = _type_keys()
    for lang in LANGS:
        pairs = _pairs(lang)
        missing = [k for k in keys.values() if not (pairs.get(k) or "").strip()]
        assert not missing, (lang, missing)


def test_the_type_labels_are_real_translations():
    keys = _type_keys()
    en, geo, ru = (_pairs(lang) for lang in LANGS)
    for kind, key in keys.items():
        g, r, e = geo[key], ru[key], en[key]
        assert any("ა" <= ch <= "ჿ" for ch in g), (key, g)
        assert any("А" <= ch <= "я" or ch in "Ёё" for ch in r), (key, r)
        assert len({e, g, r}) == 3, (key, e, g, r)
    for lang, pairs in (("en", en), ("geo", geo), ("ru", ru)):
        texts = [pairs[k] for k in keys.values()]
        assert len(set(texts)) == 4, (lang, texts)


def test_dashboard_js_uses_the_type_label_keys():
    keys = _type_keys()
    prefix = _common_prefix(keys.values())
    literal = all(k in JS for k in keys.values())
    built = len(prefix) > len(prefix.split(".")[0]) + 1 and re.search(
        rf"['\"`]{re.escape(prefix)}", JS)
    assert literal or built, (f"dashboard.js references neither the keys {sorted(keys.values())} "
                              f"nor their prefix '{prefix}'")


def test_the_descriptions_view_shows_the_label_not_the_raw_dtype():
    keys = _type_keys()
    start = JS.find("async function openSchemaViewer")
    end = JS.find("async function _persistChatSchema")
    assert 0 <= start < end, "the Descriptions view functions moved"
    region = JS[start:end]
    names = _type_label_functions(keys)
    assert any(k in region for k in keys.values()) or \
        any(re.search(rf"\b{re.escape(n)}\b", region) for n in names), (
            "the Descriptions view never maps the dtype to the Text / Number / Date / Yes-No "
            f"labels (label functions found: {names})")


def test_the_saved_schema_keeps_the_stored_technical_description():
    """Display only: the page posts the stored technical description back,
    never the shown label."""
    keys = _type_keys()
    body = _function_body(JS, "_persistChatSchema")
    assert re.search(r"technical_description\s*:\s*\(?\s*info\s*&&\s*info\.technical_description",
                     body), body[:600]
    for k in keys.values():
        assert k not in body, k
    for n in _type_label_functions(keys):
        assert not re.search(rf"\b{re.escape(n)}\b", body), n


# ===========================================================================
# follow-ups N1, N2, N5
# ===========================================================================
def _selector_rules(selector: str) -> list:
    """Bodies of every rule (any media block included) one of whose comma-
    separated selectors is exactly `selector` (whitespace-normalised)."""
    want = " ".join(selector.split())
    return [body for sel, body in _rules(CSS)
            if any(" ".join(part.split()) == want for part in sel.split(","))]


@pytest.mark.parametrize("selector", ["#dashboardsDropdown", "#btnDashboards svg"])
def test_n1_the_dashboards_control_never_shrinks(selector):
    """N1: the Dashboards dropdown and its icon keep their size when the bar
    is tight (a squeezed flex item clipped the icon)."""
    bodies = _selector_rules(selector)
    assert bodies, f"no CSS rule for {selector}"
    assert any(re.search(r"flex-shrink\s*:\s*0\b", b) for b in bodies), (selector, bodies)


def _escape_branches(body: str) -> list:
    """Text around every `Escape` check in `body`."""
    return [body[max(0, m.start() - 300):m.end() + 400]
            for m in re.finditer(r"['\"]Escape['\"]", body)]


def test_n2_escape_in_an_editor_row_cancels_the_edit():
    """N2: Escape in a Descriptions row in edit mode cancels THAT edit (back to
    read mode) and calls preventDefault, so the dismiss helper leaves the
    modal open."""
    body = _function_body(JS, "_buildEditorRow")
    assert body, "_buildEditorRow is gone"
    assert re.search(r"keydown", body), "the editor row has no keydown handler"
    branches = _escape_branches(body)
    assert branches, "the editor row never checks for Escape"
    assert any("preventDefault" in b for b in branches), \
        "the editor's Escape branch does not call preventDefault"
    assert any(re.search(r"renderRead\s*\(|cancelBtn\.click\s*\(", b) for b in branches), \
        "the editor's Escape branch does not return the row to read mode"


def test_n2_the_helper_leaves_a_prevented_escape_alone():
    calls = [text for _pos, text in _helper_listener_calls(["keydown"]) if "Escape" in text]
    assert calls, "no helper keydown listener checks Escape"
    for text in calls:
        assert "defaultPrevented" in text, "the helper closes on an Escape a field already used"
        # A capture-phase listener would run BEFORE the field's own handler,
        # so its preventDefault would come too late.
        assert not re.search(r",\s*true\s*\)\s*;?\s*$|capture\s*:\s*true", text.strip()), \
            "the helper's Escape listener runs in the capture phase"


def test_n5_escape_ignores_an_ime_composition():
    calls = [text for _pos, text in _helper_listener_calls(["keydown"]) if "Escape" in text]
    assert calls, "no helper keydown listener checks Escape"
    assert all("isComposing" in text for text in calls), \
        "the helper's Escape listener does not ignore isComposing"


def _generating_key() -> str:
    en = _pairs("en")
    keys = [k for k, v in en.items() if re.match(r"generating report", v.strip(), re.I)]
    assert keys, "no en i18n key whose text is 'Generating Report...'"
    return keys[0]


def test_n5_generating_report_is_an_i18n_key_in_every_language():
    key = _generating_key()
    geo, ru = _pairs("geo").get(key, ""), _pairs("ru").get(key, "")
    assert any("ა" <= ch <= "ჿ" for ch in geo), (key, geo)
    assert any("А" <= ch <= "я" or ch in "Ёё" for ch in ru), (key, ru)


def test_n5_the_page_shows_generating_report_through_the_key():
    key = _generating_key()
    assert re.search(rf"_t\(\s*['\"]{re.escape(key)}['\"]", JS), f"dashboard.js never calls _t('{key}', …)"
    assert not re.search(r"textContent\s*=\s*['\"]Generating Report", JS), \
        "a hard-coded 'Generating Report...' is still assigned"
