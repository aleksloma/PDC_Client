"""C4 (p29, finding F9) — the /lab layers and the top bar, in a real browser
against the local stack (the browser half of tests/test_lab_interface_structure.py).

What only a browser can show:

* Escape closes each layer named in F9 — the View / Edit Descriptions modal,
  the profile dropdown, the Download Analytics dropdown and a sidebar "⋯"
  menu — and so does a click outside it;
* Escape in a Descriptions row in edit mode cancels that edit and leaves the
  modal open (N2); the next Escape closes the modal;
* opening one menu closes the one that is open (the openers used to call
  `stopPropagation`, hiding their click from the others' close listeners);
* at 1100 px the top bar does not wrap: every button sits on one row, no
  button's text breaks onto a second line, nothing overflows, and the button
  labels are hidden while each button keeps a localized `title` /
  `aria-label`; at 1400 px the labels are shown; at 700 px (mobile) the
  labels are unchanged (shown).

Uses the shared integration fixtures (`session`, `chat` — a real chat over
the sample CSV, created and removed by tests/integration/conftest.py; no
brain needed). Gated like the rest of `tests/integration/` (skipped unless
`PDC_STACK_URL` is set); skips with a reason when Playwright or a Chromium
build is unavailable.
"""
import itertools
import re

import pytest

from .conftest import SESSION_COOKIE

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

PAGE_TIMEOUT_MS = 60_000
STEP_TIMEOUT_MS = 5_000
SETTLE_MS = 300
TOP_BAR_BUTTONS = ("btnDashboards", "btnDownloadReport", "btnAddData", "btnSchema")
MENUS = ("profile", "download", "sidebar")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def playwright_driver():
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(playwright_driver):
    errors = []
    for kwargs in ({}, {"channel": "chrome"}, {"channel": "msedge"}):
        try:
            b = playwright_driver.chromium.launch(**kwargs)
        except Exception as e:               # pragma: no cover - env dependent
            errors.append(f"{kwargs or 'bundled'}: {type(e).__name__}")
            continue
        yield b
        b.close()
        return
    pytest.skip("no Chromium build can be launched: " + "; ".join(errors))


@pytest.fixture
def lab(browser, base_url, session, chat):
    """A /lab page at 1400 x 900 with the fixture chat open (owner view, so
    Add Data, View / Edit Descriptions and the Download Analytics dropdown
    are shown)."""
    ctx, page = _open_lab(browser, base_url, session, chat, width=1400)
    try:
        yield page
    finally:
        ctx.close()


def _open_lab(browser, base_url, session, chat_id, width):
    cookie = session.cookies.get(SESSION_COOKIE)
    assert cookie, "no session cookie"
    ctx = browser.new_context(viewport={"width": width, "height": 900})
    ctx.add_cookies([{"name": SESSION_COOKIE, "value": cookie, "url": base_url}])
    page = ctx.new_page()
    page.set_default_timeout(PAGE_TIMEOUT_MS)
    page.goto(f"{base_url}/lab", wait_until="load")
    _open_chat(page, chat_id, mobile=width <= 768)
    return ctx, page


def _open_chat(page, chat_id, mobile=False):
    row = f'.chat-item-row[data-chat-id="{chat_id}"]'
    page.wait_for_selector(row, state="attached")
    if not mobile:
        page.click(row)
        start = f'.conversation-item.start-new[data-chat-id="{chat_id}"]'
        page.wait_for_selector(start, state="visible")
        page.click(start)
    else:                                    # mobile: the drawer is off-screen
        page.evaluate("(id) => openChat(id)", chat_id)
    page.wait_for_selector("#btnSchema:not(.hidden)", state="attached")
    page.wait_for_selector("#downloadReportDropdown:not(.hidden)", state="attached")
    page.wait_for_timeout(SETTLE_MS)


# ---------------------------------------------------------------------------
# layers
# ---------------------------------------------------------------------------
def _is_open(page, layer) -> bool:
    if layer == "profile":
        return page.is_visible("#profileDropdown")
    if layer == "download":
        return page.is_visible("#downloadReportMenu")
    if layer == "sidebar":
        return page.locator(".item-context-menu:visible").count() > 0
    if layer == "descriptions":
        return page.is_visible("#schemaViewerModal")
    raise AssertionError(layer)


def _wait_state(page, layer, open_: bool):
    deadline_steps = STEP_TIMEOUT_MS // 100
    for _ in range(deadline_steps):
        if _is_open(page, layer) == open_:
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"{layer} did not become {'open' if open_ else 'closed'}")


def _open(page, layer, chat_id):
    if layer == "profile":
        page.click("#btnProfileIcon")
    elif layer == "download":
        page.click("#btnDownloadReport")
    elif layer == "sidebar":
        row = f'.chat-item-row[data-chat-id="{chat_id}"]'
        page.hover(row)
        page.click(f'.chat-menu-btn[data-chat-id="{chat_id}"]')
    elif layer == "descriptions":
        page.click("#btnSchema")
    else:
        raise AssertionError(layer)
    _wait_state(page, layer, True)
    page.wait_for_timeout(SETTLE_MS)       # a menu's own deferred listeners settle


def _click_outside(page, layer):
    if layer == "descriptions":
        # The modal's backdrop, outside its content box.
        page.locator("#schemaViewerModal").click(position={"x": 5, "y": 5})
        return
    box = page.locator("#chatMessages").bounding_box()
    assert box and box["width"] > 0 and box["height"] > 0, box
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)


@pytest.mark.parametrize("layer", ["descriptions", "profile", "download", "sidebar"])
def test_escape_closes_the_layer(lab, chat, layer):
    _open(lab, layer, chat)
    lab.keyboard.press("Escape")
    _wait_state(lab, layer, False)


@pytest.mark.parametrize("layer", ["descriptions", "profile", "download", "sidebar"])
def test_a_click_outside_closes_the_layer(lab, chat, layer):
    _open(lab, layer, chat)
    _click_outside(lab, layer)
    _wait_state(lab, layer, False)


@pytest.mark.parametrize("first,second", list(itertools.permutations(MENUS, 2)))
def test_opening_one_menu_closes_the_other(lab, chat, first, second):
    _open(lab, first, chat)
    _open(lab, second, chat)
    _wait_state(lab, first, False)
    assert _is_open(lab, second), f"{second} closed itself while {first} was closing"


def test_escape_closes_only_the_topmost_layer(lab, chat):
    """A sidebar menu open over nothing else: one Escape closes it; a second
    Escape with nothing open is harmless (the chat stays open)."""
    _open(lab, "sidebar", chat)
    lab.keyboard.press("Escape")
    _wait_state(lab, "sidebar", False)
    lab.keyboard.press("Escape")
    lab.wait_for_timeout(SETTLE_MS)
    assert lab.is_visible("#chatState"), "Escape with no layer open closed the chat"


def test_the_descriptions_modal_reopens_after_escape(lab, chat):
    """The helper unregisters a closed layer: the modal opens and closes again."""
    for _ in range(2):
        _open(lab, "descriptions", chat)
        lab.keyboard.press("Escape")
        _wait_state(lab, "descriptions", False)


def test_escape_in_an_editor_row_cancels_the_edit_and_keeps_the_modal(lab, chat):
    """N2: the first Escape in a Descriptions row in edit mode cancels that
    edit (the row returns to read mode, the typed text is dropped) and the
    modal stays open; the next Escape closes the modal."""
    _open(lab, "descriptions", chat)
    edit = lab.locator("#schemaViewerModal .editor-edit-btn").first
    edit.click()
    field = lab.locator("#schemaViewerModal .editor-edit-input").first
    field.wait_for(state="visible", timeout=STEP_TIMEOUT_MS)
    field.fill("typed but not saved")
    field.focus()
    lab.keyboard.press("Escape")
    lab.wait_for_timeout(SETTLE_MS)
    assert _is_open(lab, "descriptions"), "Escape in an editing row closed the whole modal"
    assert lab.locator("#schemaViewerModal .editor-edit-input").count() == 0, \
        "the row is still in edit mode after Escape"
    texts = lab.locator("#schemaViewerModal .editor-value-text").all_inner_texts()
    assert not any("typed but not saved" in t for t in texts), texts
    lab.keyboard.press("Escape")
    _wait_state(lab, "descriptions", False)


# ---------------------------------------------------------------------------
# the top bar
# ---------------------------------------------------------------------------
_BAR_GEOMETRY = """(ids) => {
  const bar = document.querySelector('.top-bar');
  const visible = (el) => {
    if (!el) return false;
    const r = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none';
  };
  const letters = (btn) => {
    // Width of the LETTER text actually drawn inside the button (icons and
    // emoji are not letters), and the number of distinct line tops it uses.
    let width = 0; const tops = new Set();
    const walker = document.createTreeWalker(btn, NodeFilter.SHOW_TEXT);
    for (let n = walker.nextNode(); n; n = walker.nextNode()) {
      if (!/\\p{L}/u.test(n.nodeValue || '')) continue;
      const parent = n.parentElement;
      if (!visible(parent)) continue;
      const range = document.createRange();
      range.selectNodeContents(n);
      for (const r of range.getClientRects()) {
        if (r.width < 1 || r.height < 1) continue;
        width += r.width; tops.add(Math.round(r.top));
      }
    }
    return {width, lines: tops.size};
  };
  const r = bar.getBoundingClientRect();
  const buttons = [...bar.querySelectorAll('button')].filter(visible).map((b) => {
    const br = b.getBoundingClientRect();
    const l = letters(b);
    return {id: b.id, cls: b.className, top: br.top, bottom: br.bottom, height: br.height,
            left: br.left, right: br.right, mid: (br.top + br.bottom) / 2,
            letterWidth: l.width, lines: l.lines,
            title: b.getAttribute('title') || '', aria: b.getAttribute('aria-label') || ''};
  });
  return {height: r.height, scrollWidth: bar.scrollWidth, clientWidth: bar.clientWidth,
          viewport: window.innerWidth, buttons,
          shown: ids.filter((id) => visible(document.getElementById(id)))};
}"""


def _geometry(page):
    return page.evaluate(_BAR_GEOMETRY, list(TOP_BAR_BUTTONS))


def _assert_one_row(g, width):
    assert g["scrollWidth"] <= g["clientWidth"] + 1, f"the top bar overflows at {width}px: {g}"
    action = [b for b in g["buttons"] if "top-action-btn" in b["cls"] or b["id"] == "btnProfileIcon"]
    assert action, g
    mids = [b["mid"] for b in action]
    assert max(mids) - min(mids) <= 4, f"top-bar buttons sit on more than one row at {width}px: {action}"
    for b in g["buttons"]:
        # The profile button shows name + plan on two lines by design; only
        # the action buttons must keep their text on one line.
        if "top-action-btn" in b["cls"]:
            assert b["lines"] <= 1, f"#{b['id']} text wraps onto {b['lines']} lines at {width}px: {b}"
        assert b["right"] <= g["viewport"] + 0.5, f"#{b['id']} is cut off at {width}px: {b}"


def test_the_top_bar_does_not_wrap_at_1100px(browser, base_url, session, chat):
    ctx, page = _open_lab(browser, base_url, session, chat, width=1100)
    try:
        g = _geometry(page)
        assert set(TOP_BAR_BUTTONS) <= set(g["shown"]), g["shown"]
        _assert_one_row(g, 1100)
    finally:
        ctx.close()


def test_at_1100px_the_labels_are_hidden_and_the_names_kept(browser, base_url, session, chat):
    ctx, page = _open_lab(browser, base_url, session, chat, width=1100)
    try:
        g = _geometry(page)
        by_id = {b["id"]: b for b in g["buttons"]}
        for bid in TOP_BAR_BUTTONS:
            b = by_id.get(bid)
            assert b, (bid, g["shown"])
            assert b["letterWidth"] <= 1, f"#{bid} still shows its label at 1100px: {b}"
            name = (b["aria"] or b["title"]).strip()
            assert name, f"#{bid} has no title / aria-label once its label is hidden: {b}"
            assert not re.fullmatch(r"[\w-]+\.[\w.-]+", name), (
                f"#{bid}'s name is an untranslated i18n key: {name!r}")
    finally:
        ctx.close()


def test_at_1400px_the_labels_are_shown_on_one_row(browser, base_url, session, chat):
    ctx, page = _open_lab(browser, base_url, session, chat, width=1400)
    try:
        g = _geometry(page)
        _assert_one_row(g, 1400)
        by_id = {b["id"]: b for b in g["buttons"]}
        for bid in TOP_BAR_BUTTONS:
            assert by_id.get(bid) and by_id[bid]["letterWidth"] > 1, (bid, by_id.get(bid))
    finally:
        ctx.close()


def test_mobile_keeps_its_labels(browser, base_url, session, chat):
    """Mobile (<= 768 px) is unchanged: the icon-only breakpoint does not
    reach it, so the shown buttons keep their labels."""
    ctx, page = _open_lab(browser, base_url, session, chat, width=700)
    try:
        g = _geometry(page)
        by_id = {b["id"]: b for b in g["buttons"]}
        shown = [bid for bid in TOP_BAR_BUTTONS if bid in by_id]
        assert shown, g
        for bid in shown:
            assert by_id[bid]["letterWidth"] > 1, (bid, by_id[bid])
    finally:
        ctx.close()
