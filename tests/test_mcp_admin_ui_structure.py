"""MCP stage 5 — the "MCP access" section of the local admin page.

Admin mode only: the power page is byte-identical to the render taken before
the section existed (`tests/fixtures/admin_data_sources_power_mode*.html`;
regenerate ONLY when the power page changes on purpose:

    .venv\\Scripts\\python -c "from tests.test_mcp_admin_ui_structure import regenerate_power; regenerate_power()"

). The section is translated (en / geo / ru) from the browser's stored
PowerDataChat language; the rest of the page stays English, so `i18n.js` is
loaded but never applied to the document.

The contract of the markup (ids and classes the script and these tests use):

    nav      .adm-nav-item[data-section="mcp"], its label a child element with
             data-i18n (not the button: that would wipe the icon)
    section  #secMcp
    form     #mcpName, #mcpMode (select: per_user first, shared_account),
             #mcpAccountWrap > #mcpAccount, #mcpSharedNote
             (admin.mcp.shared_note), #mcpPerUserNote
             (admin.mcp.per_user_note), #btnMcpCreate
    once     #mcpTokenBox (hidden), #mcpTokenValue, #mcpEndpoint, #btnMcpCopy
    list     #mcpTokensList, one .mcp-revoke button per active token
    keys     admin.mcp.* only; admin.mcp.mode_per_user,
             admin.mcp.mode_shared_account, admin.mcp.state_active,
             admin.mcp.state_revoked are the words of the list

The behaviour is run, not read: the rendered page and its own scripts in node
(`tests/fixtures/admin_page_runner.js`, a small DOM and a stub server), skipped
without node on PATH. Offline.
"""
import ast
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from starlette.middleware.sessions import SessionMiddleware
from starlette.testclient import TestClient

import app as app_mod
import local_store
import mcp_tokens
import routes.mcp as mcp_routes
from settings import settings
from tests.test_power_page_profile_menu import GOLDEN_CTX

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
RUNNER = FIXTURES / "admin_page_runner.js"
TEMPLATE = (ROOT / "templates" / "admin_data_sources.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "admin_data_sources.js").read_text(encoding="utf-8")
# Without comments: what the script does, not what it says.
JS_CODE = re.sub(r"^\s*/\*.*?\*/|(?<=\s)//[^\n]*", "", JS, flags=re.S | re.M)
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")

LANGS = ("en", "geo", "ru")
PREFIX = "admin.mcp."
GEORGIAN = re.compile(r"[ა-ჿ]")
CYRILLIC = re.compile(r"[Ѐ-ӿ]")
LATIN_WORD = re.compile(r"[A-Za-z]+(?:-[A-Za-z]+)*")
# The only Latin words a Georgian or Russian text may hold.
LATIN_ALLOWED = {"MCP", "PowerDataChat", "URL", "HTTP", "Authorization", "Bearer",
                 "X-PDC-User-Email", "API"}
# English words in Georgian letters; the interface has Georgian words for them.
GEO_TRANSLITERATED = ("ტოკენ", "ენდპოინტ", "ენდფოინთ", "აქაუნთ", "ექაუნთ", "იუზერ",
                      "იმეილ", "ლინკ", "ქოფი", "რევოქ", "ფილდ")

RAW = "pdcmcp_" + "R" * 43


# ===========================================================================
# rendering
# ===========================================================================
def _render(**over) -> str:
    return app_mod.templates.env.get_template("admin_data_sources.html").render(
        {**GOLDEN_CTX, **over})


ADMIN_HTML = _render()

_POWER = {"manager_mode": "power", "back_to_chat": False, "username": "power@x.com",
          "raw_username": "power@x.com", "display_name": "power@x.com",
          "is_power_user": True, "is_admin_user": False}
POWER_CONTEXTS = {
    "admin_data_sources_power_mode.html": {
        **_POWER, "self_service": False, "read_beyond_manage": True,
        "manage_scope_summary": "You can manage: DWH (whole connection)"},
    "admin_data_sources_power_mode_self_service.html": {
        **_POWER, "self_service": True, "is_self_service": True,
        "manage_scope_summary": "", "read_beyond_manage": False},
}


def _unix(text: str) -> str:
    return text.replace("\r\n", "\n")


def regenerate_power() -> None:
    for name, ctx in POWER_CONTEXTS.items():
        (FIXTURES / name).write_text(_unix(_render(**ctx)), encoding="utf-8", newline="")


class _Tree(HTMLParser):
    """The elements of a page: {tag, attrs, parent, text, children}."""
    VOID = {"br", "hr", "img", "input", "link", "meta"}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = {"tag": "", "attrs": {}, "parent": None, "text": "", "children": []}
        self.cur = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        el = {"tag": tag, "attrs": {k: v or "" for k, v in attrs}, "parent": self.cur,
              "text": "", "children": []}
        self.cur["children"].append(el)
        if tag not in self.VOID:
            self.cur = el

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.cur = self.cur["parent"]

    def handle_endtag(self, tag):
        el = self.cur
        while el is not self.root and el["tag"] != tag:
            el = el["parent"]
        if el is not self.root:
            self.cur = el["parent"]

    def handle_data(self, data):
        self.cur["text"] += data


def _walk(el):
    for child in el["children"]:
        yield child
        yield from _walk(child)


def _by_id(el, wanted):
    return next((e for e in _walk(el) if e["attrs"].get("id") == wanted), None)


TREE = _Tree(ADMIN_HTML).root


def _section():
    sec = _by_id(TREE, "secMcp")
    assert sec is not None, "the admin page has no #secMcp"
    return sec


def _nav_button():
    found = [e for e in _walk(TREE) if e["attrs"].get("data-section") == "mcp"]
    assert len(found) == 1, "one navigation button with data-section='mcp' is expected"
    return found[0]


def _el(wanted):
    el = _by_id(_section(), wanted)
    assert el is not None, f"#secMcp holds no #{wanted}"
    return el


def _starts_hidden(el):
    # The section itself is hidden until it is opened.
    while el is not None and el["attrs"].get("id") != "secMcp":
        if "hidden" in el["attrs"] or "hidden" in el["attrs"].get("class", "").split():
            return True
        el = el["parent"]
    return False


def _foreign_words(value: str) -> list:
    """The Latin words of a text that the allowlist does not hold."""
    value = re.sub(r"\{[A-Za-z_]+\}", " ", value)           # a value put in later
    value = re.sub(r"[\w.+-]+@[\w.-]+", " ", value)         # an address
    return [w for w in LATIN_WORD.findall(value) if w not in LATIN_ALLOWED]


# ===========================================================================
# i18n values
# ===========================================================================
def _lang_block(lang: str) -> str:
    starts = {k: I18N.index(f"\n  {k}: {{") for k in LANGS}
    order = sorted(starts, key=starts.get)
    i = order.index(lang)
    end = starts[order[i + 1]] if i + 1 < len(order) else I18N.index("window.i18n = {")
    return I18N[starts[lang]:end]


def _values(lang: str) -> dict:
    """The admin.mcp.* keys of one language; a key written twice fails."""
    out = {}
    pattern = r"""(['"])(admin\.mcp\.[A-Za-z0-9_.]+)\1\s*:\s*(['"`])(.*?)(?<!\\)\3"""
    for m in re.finditer(pattern, _lang_block(lang)):
        assert m.group(2) not in out, f"{m.group(2)} is written twice in {lang}"
        out[m.group(2)] = m.group(4).replace("\\'", "'").replace('\\"', '"')
    return out


def _template_keys() -> set:
    attrs = ("data-i18n", "data-i18n-title", "data-i18n-placeholder", "data-i18n-aria-label")
    els = [_section(), *_walk(_section()), *_walk(_nav_button())]
    return {e["attrs"][a] for e in els for a in attrs if a in e["attrs"]}


def _js_keys() -> tuple:
    """(whole keys, prefixes completed at run time) written in the script."""
    found = set(re.findall(r"""['"`](admin\.mcp\.[A-Za-z0-9_.]*)""", JS))
    return ({k for k in found if not k.endswith(("_", "."))},
            {k for k in found if k.endswith(("_", "."))})


# ===========================================================================
# the template
# ===========================================================================
def test_the_section_and_its_button_are_on_the_admin_page_only():
    assert _section()["tag"] == "section"
    assert "hidden" in _section()["attrs"], "a section starts hidden, like the others"
    assert "adm-section" in _section()["attrs"]["class"].split()
    assert "adm-nav-item" in _nav_button()["attrs"]["class"].split()
    assert ADMIN_HTML.count('id="secMcp"') == 1
    for ctx in POWER_CONTEXTS.values():
        html = _render(**ctx)
        for gone in ("secMcp", 'data-section="mcp"', "admin.mcp.", "i18n.js", "mcpName",
                     "btnMcp", "mcpToken"):
            assert gone not in html, gone


@pytest.mark.parametrize("name", sorted(POWER_CONTEXTS))
def test_the_power_page_is_byte_identical(name):
    expected = (FIXTURES / name).read_bytes().decode("utf-8")
    assert _unix(_render(**POWER_CONTEXTS[name])) == _unix(expected)


def test_i18n_is_loaded_in_admin_mode_only_before_the_page_script():
    srcs = [e["attrs"]["src"] for e in _walk(TREE)
            if e["tag"] == "script" and "src" in e["attrs"]]
    assert srcs == ["/static/i18n.js?v=0", "/static/admin_data_sources.js?v=0"]
    assert TEMPLATE.count("/static/i18n.js?v={{ ts }}") == 1
    assert TEMPLATE.count("/static/i18n.js") == 1


def test_the_navigation_label_is_translated_and_keeps_its_icon():
    button = _nav_button()
    assert "data-i18n" not in button["attrs"], "a translation would wipe the icon"
    labels = [e for e in _walk(button) if "data-i18n" in e["attrs"]]
    assert len(labels) == 1 and labels[0]["text"].strip()
    icons = [e for e in _walk(button) if "adm-nav-ico" in e["attrs"].get("class", "").split()]
    assert icons and icons[0]["text"].strip()
    assert "data-i18n" not in icons[0]["attrs"]


def test_the_form_has_its_fields():
    assert _el("mcpName")["tag"] == "input"
    mode = _el("mcpMode")
    assert mode["tag"] == "select"
    assert [o["attrs"].get("value") for o in _walk(mode) if o["tag"] == "option"] == \
        ["per_user", "shared_account"]
    account = _el("mcpAccount")
    while account is not None and account["attrs"].get("id") != "mcpAccountWrap":
        account = account["parent"]
    assert account is not None, "#mcpAccount is not inside #mcpAccountWrap"
    assert _el("btnMcpCreate")["tag"] == "button"
    assert _el("btnMcpCopy")["tag"] == "button"
    _el("mcpTokensList")


def test_the_notes_carry_their_keys():
    assert _el("mcpSharedNote")["attrs"].get("data-i18n") == "admin.mcp.shared_note"
    assert _el("mcpPerUserNote")["attrs"].get("data-i18n") == "admin.mcp.per_user_note"


def test_what_starts_hidden():
    """per_user is the first mode, and no token has been created yet."""
    assert _starts_hidden(_el("mcpAccountWrap"))
    assert _starts_hidden(_el("mcpSharedNote"))
    assert not _starts_hidden(_el("mcpPerUserNote"))
    assert _starts_hidden(_el("mcpTokenBox"))
    for inner in ("mcpTokenValue", "mcpEndpoint", "btnMcpCopy"):
        el = _el(inner)
        while el is not None and el["attrs"].get("id") != "mcpTokenBox":
            el = el["parent"]
        assert el is not None, f"#{inner} is not inside #mcpTokenBox"
    assert _el("mcpTokenValue")["tag"] not in ("input", "textarea"), \
        "the token is text, not a form value a browser remembers"
    assert _el("mcpTokenValue")["text"].strip() == ""


def test_no_inline_handler_and_no_html_translation():
    for el in (_section(), _nav_button(), *_walk(_section()), *_walk(_nav_button())):
        for attr, value in el["attrs"].items():
            assert not attr.startswith("on"), (attr, el["tag"])
            assert not value.strip().lower().startswith("javascript:")
        assert "data-i18n-html" not in el["attrs"]


def test_a_translated_element_holds_text_only():
    """`textContent` replaces everything inside it."""
    for el in (*_walk(_section()), *_walk(_nav_button())):
        if "data-i18n" in el["attrs"]:
            assert el["children"] == [], el["attrs"]["data-i18n"]


def test_every_text_of_the_section_is_translated_and_starts_as_the_english_text():
    en = _values("en")
    assert en, "no admin.mcp.* key"
    for el in (_section(), *_walk(_section()), *_walk(_nav_button())):
        text = " ".join(el["text"].split())
        key = el["attrs"].get("data-i18n")
        if key:
            assert text == en[key], key
        elif "adm-nav-ico" not in el["attrs"].get("class", "").split():
            assert _foreign_words(text) == [], f"not translated: {text!r}"
        for attr in ("title", "placeholder", "aria-label"):
            value = el["attrs"].get(attr, "")
            if re.search(r"[A-Za-z]", value) and "@" not in value:
                assert value == en[el["attrs"].get(f"data-i18n-{attr}")], (attr, value)


# ===========================================================================
# the texts
# ===========================================================================
def test_every_key_exists_in_the_three_languages():
    sets = {lang: set(_values(lang)) for lang in LANGS}
    assert sets["en"], "no admin.mcp.* key in i18n.js"
    assert sets["en"] == sets["geo"] == sets["ru"]
    used = _template_keys()
    assert used and all(k.startswith(PREFIX) for k in used), used
    whole, prefixes = _js_keys()
    assert whole | prefixes, "the script uses no admin.mcp.* key"
    assert (used | whole) <= sets["en"], (used | whole) - sets["en"]
    for p in prefixes:
        assert any(k.startswith(p) and k != p for k in sets["en"]), p
    assert {PREFIX + k for k in (
        "shared_note", "per_user_note", "mode_per_user", "mode_shared_account",
        "state_active", "state_revoked")} <= sets["en"]


def test_no_key_is_left_unused():
    whole, prefixes = _js_keys()
    for key in _values("en"):
        assert key in _template_keys() or key in whole or key.startswith(tuple(prefixes)), key


@pytest.mark.parametrize("lang", LANGS)
def test_no_text_is_empty_or_markup(lang):
    values = _values(lang)
    assert values
    for key, value in values.items():
        assert value.strip(), key
        assert value == value.strip(), key
        assert "<" not in value and " " not in value, key


def test_the_english_texts_are_english():
    values = _values("en")
    assert values
    for key, value in values.items():
        assert not GEORGIAN.search(value) and not CYRILLIC.search(value), key
        assert re.search(r"[A-Za-z]{2}", value), key


@pytest.mark.parametrize("lang", ["geo", "ru"])
def test_the_translations_are_in_their_own_language(lang):
    letters, other = (GEORGIAN, CYRILLIC) if lang == "geo" else (CYRILLIC, GEORGIAN)
    values = _values(lang)
    assert values
    for key, value in values.items():
        assert letters.search(value), (key, value)
        assert not other.search(value), (key, value)
        assert _foreign_words(value) == [], (key, value)


def test_the_georgian_texts_use_georgian_words():
    values = _values("geo")
    assert values
    for key, value in values.items():
        for word in GEO_TRANSLITERATED:
            assert word not in value, (key, value)


@pytest.mark.parametrize("lang", LANGS)
def test_the_placeholders_are_the_same_in_every_language(lang):
    en, values = _values("en"), _values(lang)
    assert en
    for key in en:
        assert sorted(re.findall(r"\{\w+\}", values[key])) == \
            sorted(re.findall(r"\{\w+\}", en[key])), key


def test_the_shared_account_note_says_what_is_shared():
    note = _values("en").get("admin.mcp.shared_note", "").lower()
    for word in ("conversation", "chat", "day", "question", "history", "link", "signed in",
                 "account"):
        assert word in note, word
    for lang in ("geo", "ru"):
        assert len(_values(lang)["admin.mcp.shared_note"]) > 80


def test_the_per_user_note_says_whom_the_holder_can_act_as():
    note = _values("en").get("admin.mcp.per_user_note", "").lower()
    for word in ("any user", "domain"):
        assert word in note, word
    for lang in ("geo", "ru"):
        assert len(_values(lang)["admin.mcp.per_user_note"]) > 40


def _refused(*args, **kwargs) -> str:
    with pytest.raises(ValueError) as caught:
        mcp_tokens.create_token(*args, **kwargs)
    return str(caught.value)


def _route_texts(token_file) -> list:
    """The sentences the revoke and create routes answer by themselves."""
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="test-secret")
    app.include_router(mcp_routes.admin_router)

    @app.post("/_login")
    async def _login(request: Request):
        request.session["email"] = "admin2@corp.example"

    local_store.AuthStore().ensure_user("admin2@corp.example")
    local_store.AuthStore().set_role("admin2@corp.example", "admin")
    client = TestClient(app)
    client.post("/_login")
    unknown = client.post("/api/admin/mcp/tokens/0123456789abcdef/revoke", json={})
    token_file.write_text("{broken", encoding="utf-8")
    store = client.post("/api/admin/mcp/tokens", json={"name": "x", "mode": "per_user"})
    assert (unknown.status_code, store.status_code) == (404, 500)
    return [unknown.json()["error"], store.json()["error"]]


def test_every_server_sentence_of_the_form_has_its_translation(tmp_path, monkeypatch):
    """The script translates a server sentence by looking its text up among the
    English admin.mcp.error_* values, so a reworded sentence would stay
    English. Only the mode sentence has none: the form cannot cause it."""
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "LOCAL_ADMIN_USERNAME", "boss@corp.example")
    store = local_store.AuthStore()
    store.ensure_user("undated@corp.example")
    profile = tmp_path / "users" / "undated@corp.example" / "profile.json"
    data = json.loads(profile.read_text(encoding="utf-8"))
    del data["created_at"]
    profile.write_text(json.dumps(data), encoding="utf-8")
    mcp_tokens.create_token("Taken", "per_user", "ladmin")

    def shared(account):
        return _refused("bot", "shared_account", "ladmin", account=account)

    raised = [
        _refused("  ", "per_user", "ladmin"),
        _refused("x" * (mcp_tokens.NAME_MAX_CHARS + 1), "per_user", "ladmin"),
        _refused("two\nlines", "per_user", "ladmin"),
        _refused("TAKEN", "per_user", "ladmin"),
        shared("not-an-address"),
        shared("boss@corp.example"),
        shared("nobody@corp.example"),
        shared("undated@corp.example"),
    ]
    assert len(set(raised)) == len(raised), raised
    # Nothing the store can say is missing above: every `raise ValueError` of
    # the module, the mode sentence included.
    tree = ast.parse((ROOT / "mcp_tokens.py").read_text(encoding="utf-8"))
    raises = [n for n in ast.walk(tree) if isinstance(n, ast.Raise)
              and isinstance(n.exc, ast.Call) and getattr(n.exc.func, "id", "") == "ValueError"]
    assert len(raises) == len(raised) + 1, "a new sentence: add it here and to i18n.js"

    sentences = raised + _route_texts(tmp_path / "mcp_tokens.json")
    values = [v for k, v in _values("en").items() if k.startswith(PREFIX + "error_")]
    for sentence in sentences:
        assert values.count(sentence) == 1, sentence
    assert sorted(values) == sorted(sentences), "an error key no server sentence uses"
    for lang in LANGS:
        numbers = re.findall(r"\d+", _values(lang)[PREFIX + "error_name_length"])
        assert numbers == [str(mcp_tokens.NAME_MAX_CHARS)], lang


# ===========================================================================
# the script
# ===========================================================================
def test_the_section_is_known_to_the_admin_list_only():
    m = re.search(r"const SECTIONS = POWER\s*\?\s*\[([^\]]*)\]\s*:\s*\[([^\]]*)\]", JS)
    assert m, "SECTIONS is not where it was"
    assert "'mcp'" not in m.group(1)
    assert "'mcp'" in m.group(2)


def test_the_document_is_never_translated_as_a_whole():
    for call in ("applyAll", "i18n.init", "i18n?.init", "setLang", "adjustFontSizes"):
        assert call not in JS_CODE, call


def test_nothing_is_kept_in_browser_storage():
    for store in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
        assert store not in JS_CODE, store


def test_every_mcp_request_goes_through_the_api_helper():
    """`api()` sends the JSON header; the only other request is the sign-out."""
    assert "/api/admin/mcp" in JS_CODE, "the script calls no MCP route"
    assert re.findall(r"\bfetch\(([^,]+),", JS_CODE) == ["path", "'/auth/logout'"]
    assert "XMLHttpRequest" not in JS_CODE and "sendBeacon" not in JS_CODE


# ===========================================================================
# behaviour (node)
# ===========================================================================
def _run(tmp_path, scenario, *, html=ADMIN_HTML, **options):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    page, script = tmp_path / "page.html", tmp_path / "scenario.js"
    page.write_text(html, encoding="utf-8")
    script.write_text(scenario, encoding="utf-8")
    proc = subprocess.run([node, str(RUNNER), str(page), str(ROOT / "static"), str(script),
                           json.dumps(options)], capture_output=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = json.loads(proc.stdout)
    assert out["failed"] is None, out["failed"]
    assert out["errors"] == [], out["errors"]
    assert out["storageWrites"] == [], "the page wrote to the browser storage"
    return out


def _row(**over):
    row = {"id": "a" * 16, "name": "Helpdesk bot", "mode": "per_user", "account": None,
           "created_at": "2026-11-20T10:00:00+00:00", "created_by": "ladmin",
           "last_used_at": "2026-11-21T10:00:00+00:00", "revoked_at": None, "revoked_by": None,
           "active": True}
    row.update(over)
    return row


ACTIVE = _row()
REVOKED = _row(id="b" * 16, name="Old kiosk", mode="shared_account", account="bot@corp.example",
               last_used_at=None, revoked_at="2026-11-22T10:00:00+00:00", revoked_by="ladmin",
               active=False)


def test_the_runner_drives_the_page(tmp_path):
    """The harness itself: an existing section opens, and its requests show."""
    out = _run(tmp_path, """
      await h.open('users');
      return [h.visible('secUsers'), h.visible('secConnections'),
              h.calls.map((c) => c.path).includes('/api/admin/users')];""")
    assert out["result"] == [True, False, True]


def test_the_power_page_runs_without_the_section(tmp_path):
    html = _render(**POWER_CONTEXTS["admin_data_sources_power_mode.html"])
    out = _run(tmp_path, """
      return [h.doc.getElementById('secMcp'), h.mcpCalls().length,
              h.visible('secConnections'), h.win.location.hash];""", html=html, hash="#mcp")
    assert out["result"] == [None, 0, True, "#connections"]
    assert out["loaded"] == ["/static/admin_data_sources.js"]


def test_the_list_is_fetched_when_the_section_opens(tmp_path):
    out = _run(tmp_path, """
      const before = h.mcpCalls().length;
      await h.open('mcp');
      return [before, h.mcpCalls().map((c) => c.method + ' ' + c.path), h.visible('secMcp'),
              h.win.location.hash];""", tokens=[ACTIVE])
    assert out["result"] == [0, ["GET /api/admin/mcp/tokens"], True, "#mcp"]


def test_the_section_opens_from_the_address(tmp_path):
    out = _run(tmp_path, "return [h.visible('secMcp'), h.mcpCalls().length];", hash="#mcp")
    assert out["result"] == [True, 1]


def test_the_list_shows_the_tokens(tmp_path):
    out = _run(tmp_path, """
      await h.open('mcp');
      return [h.texts(h.$('mcpTokensList')).join(' | '), h.revokeButtons().length];""",
               tokens=[ACTIVE, REVOKED])
    text, buttons = out["result"]
    en = _values("en")
    for part in ("Helpdesk bot", "Old kiosk", "bot@corp.example", "2026",
                 en["admin.mcp.mode_per_user"], en["admin.mcp.mode_shared_account"],
                 en["admin.mcp.state_active"], en["admin.mcp.state_revoked"]):
        assert part in text, part
    assert "per_user" not in text and "shared_account" not in text
    for junk in ("undefined", "null", "NaN", "Invalid Date", "[object"):
        assert junk not in text, junk
    assert buttons == 1, "only an active token can be revoked"


def test_an_empty_list_says_so(tmp_path):
    out = _run(tmp_path, "await h.open('mcp'); return h.text('mcpTokensList');", lang="geo")
    assert GEORGIAN.search(out["result"]), out["result"]


def test_a_failed_list_has_a_message_of_its_own(tmp_path):
    """Not "there are no tokens"."""
    out = _run(tmp_path, """
      const texts = () => h.texts(h.$('secMcp')).concat(h.toasts());
      await h.open('mcp');
      const empty = texts();
      await h.open('users');
      h.server.list = [500, {}];
      await h.open('mcp');
      return texts().filter((t) => !empty.includes(t));""", lang="geo")
    assert [t for t in out["result"] if GEORGIAN.search(t)], out["result"]


def test_the_account_field_follows_the_mode(tmp_path):
    out = _run(tmp_path, """
      await h.open('mcp');
      const look = () => [h.visible('mcpAccount'), h.visible('mcpSharedNote'),
                          h.visible('mcpPerUserNote')];
      const seen = [look()];
      for (const mode of ['shared_account', 'per_user']) { h.set('mcpMode', mode); seen.push(look()); }
      return seen;""")
    first, shared, per_user = out["result"]
    assert first == per_user == [False, False, True]
    assert shared[:2] == [True, True]


@pytest.mark.parametrize("mode,typed,sent", [
    ("per_user", "left.over@corp.example", None),
    ("shared_account", "bot@corp.example", "bot@corp.example")])
def test_create_posts_json(tmp_path, mode, typed, sent):
    out = _run(tmp_path, f"""
      await h.open('mcp');
      h.set('mcpMode', 'shared_account');
      await h.create('Helpdesk bot', {mode!r}, {typed!r});
      return h.posts();""")
    assert len(out["result"]) == 1
    call = out["result"][0]
    assert call["path"] == "/api/admin/mcp/tokens"
    assert call["headers"].get("Content-Type") == "application/json"
    body = json.loads(call["body"])
    assert (body["name"], body["mode"], body.get("account") or None) == \
        ("Helpdesk bot", mode, sent)
    assert set(body) <= {"name", "mode", "account"}


def test_the_new_token_is_shown_once_with_the_endpoint(tmp_path):
    out = _run(tmp_path, """
      await h.open('mcp');
      const before = h.visible('mcpTokenBox');
      await h.create('Helpdesk bot', 'per_user');
      return [before, h.visible('mcpTokenBox'), h.text('mcpTokenValue'), h.text('mcpEndpoint'),
              h.text('mcpTokensList'), h.$('mcpName').value, h.everything().split(h.server.raw).length - 1];
      """)
    before, after, value, endpoint, listed, name, times = out["result"]
    assert (before, after) == (False, True)
    assert value == RAW
    assert "https://pdc.corp.example/mcp" in endpoint
    assert "Helpdesk bot" in listed, "the new token is not in the list"
    assert name == "", "the form is ready for the next one"
    assert times == 1, "the token is on the page more than once"
    assert not [w for w in out["markupWrites"] if "pdcmcp_" in w or "pdc.corp.example" in w]


def test_without_a_public_address_the_endpoint_is_not_a_broken_text(tmp_path):
    out = _run(tmp_path, """
      await h.open('mcp');
      await h.create('Helpdesk bot', 'per_user');
      return [h.text('mcpTokenValue'), h.visible('mcpEndpoint'), h.text('mcpEndpoint')];""",
               endpoint="", lang="geo")
    value, visible, text = out["result"]
    assert value == RAW
    assert not visible or GEORGIAN.search(text), text


def test_the_token_is_gone_after_the_section_is_left(tmp_path):
    out = _run(tmp_path, """
      await h.open('mcp');
      await h.create('Helpdesk bot', 'per_user');
      await h.open('users');
      const left = h.everything().includes(h.server.raw);
      await h.click('btnMcpCopy');
      const copied = h.copied.slice();
      await h.open('mcp');
      return [left, copied, h.visible('mcpTokenBox'), h.text('mcpTokenValue'),
              h.everything().includes(h.server.raw)];""")
    left, copied, box, value, back = out["result"]
    assert (left, box, value, back) == (False, False, "", False)
    assert RAW not in copied


def test_a_create_answer_after_the_section_was_left_shows_no_token(tmp_path):
    """The token is in the list, to be revoked, but never on the page."""
    out = _run(tmp_path, """
      await h.open('mcp');
      let release;
      h.server.hold = new Promise((r) => { release = r; });
      await h.create('Late bot', 'per_user');
      await h.open('users');
      const lists = () => h.mcpCalls().filter((c) => c.method === 'GET').length;
      const before = lists();
      release();
      await h.flush();
      const away = [h.everything().includes(h.server.raw), lists() - before,
                    h.text('mcpTokensList').includes('Late bot')];
      await h.open('mcp');
      return [away, h.everything().includes(h.server.raw), h.visible('mcpTokenBox'),
              h.posts().length];""")
    away, back, box, posts = out["result"]
    assert away == [False, 1, True], "the list is not refreshed, or the token is on the page"
    assert (back, box, posts) == (False, False, 1)
    assert not [w for w in out["markupWrites"] if "pdcmcp_" in w]


def test_a_new_token_replaces_the_one_shown(tmp_path):
    out = _run(tmp_path, """
      await h.open('mcp');
      await h.create('One', 'per_user');
      const first = h.server.raw;
      h.server.raw = 'pdcmcp_' + 'S'.repeat(43);
      await h.create('Two', 'per_user');
      return [h.text('mcpTokenValue') === h.server.raw, h.everything().includes(first)];""")
    assert out["result"] == [True, False]


@pytest.mark.parametrize("options,clipboard", [({}, True), ({"noClipboard": True}, False),
                                               ({"clipboardFails": True}, False)])
def test_copy_takes_the_token_also_over_plain_http(tmp_path, options, clipboard):
    out = _run(tmp_path, """
      await h.open('mcp');
      await h.create('Helpdesk bot', 'per_user');
      await h.click('btnMcpCopy');
      return [h.copied, h.doc.execCalls, h.doc.querySelectorAll('textarea')
              .filter((t) => t.value.includes('pdcmcp_')).length, h.text('mcpTokenValue')];""",
               **options)
    copied, executed, left, value = out["result"]
    if clipboard:
        assert copied == [RAW] and executed == []
    else:
        assert copied == [] and executed == [["copy", RAW]]
    assert left == 0, "the copy field stays on the page"
    assert value == RAW


def test_a_refused_create_shows_the_reason_and_keeps_the_form(tmp_path):
    reason = "An active token with this name already exists."
    out = _run(tmp_path, f"""
      await h.open('mcp');
      h.server.create = [400, {{ error: {reason!r} }}];
      await h.create('Helpdesk bot', 'per_user');
      return [h.texts(h.$('secMcp')).concat(h.toasts()).join(' | '), h.visible('mcpTokenBox'),
              h.$('mcpName').value];""")
    text, box, name = out["result"]
    assert reason in text
    assert box is False
    assert name == "Helpdesk bot"


@pytest.mark.parametrize("action", ["create", "revoke"])
def test_a_failure_without_a_reason_has_a_translated_message(tmp_path, action):
    out = _run(tmp_path, f"""
      await h.open('mcp');
      const texts = () => h.texts(h.$('secMcp')).concat(h.toasts());
      const before = texts();
      h.server.{action} = [500, {{}}];
      if ({action!r} === 'create') await h.create('Bot', 'per_user');
      else {{ await h.click(h.revokeButtons()[0]); await h.click('btnUserActionGo'); }}
      return texts().filter((t) => !before.includes(t));""", tokens=[ACTIVE], lang="geo")
    assert [t for t in out["result"] if GEORGIAN.search(t)], out["result"]


def test_revoke_asks_first(tmp_path):
    out = _run(tmp_path, """
      await h.open('mcp');
      await h.click(h.revokeButtons()[0]);
      const asked = [h.visible('userActionModal'), h.posts().length, h.text('userActionText')];
      await h.click('btnUserActionCancel');
      const cancelled = [h.visible('userActionModal'), h.posts().length, h.revokeButtons().length];
      return [asked, cancelled];""", tokens=[ACTIVE])
    asked, cancelled = out["result"]
    assert asked[:2] == [True, 0]
    assert "Helpdesk bot" in asked[2], "the question does not name the token"
    assert cancelled == [False, 0, 1]


def test_a_confirmed_revoke_is_posted_and_the_row_changes(tmp_path):
    out = _run(tmp_path, """
      await h.open('mcp');
      await h.click(h.revokeButtons()[0]);
      await h.click('btnUserActionGo');
      return [h.posts(), h.revokeButtons().length, h.text('mcpTokensList')];""", tokens=[ACTIVE])
    posts, buttons, text = out["result"]
    assert [c["path"] for c in posts] == [f"/api/admin/mcp/tokens/{ACTIVE['id']}/revoke"]
    assert posts[0]["headers"].get("Content-Type") == "application/json"
    assert buttons == 0
    assert _values("en")["admin.mcp.state_revoked"] in text


def test_each_button_revokes_its_own_token(tmp_path):
    two = _row(id="c" * 16, name="Second bot")
    out = _run(tmp_path, """
      await h.open('mcp');
      await h.click(h.revokeButtons()[1]);
      const question = h.text('userActionText');
      await h.click('btnUserActionGo');
      return [question, h.posts().map((c) => c.path)];""", tokens=[ACTIVE, two])
    question, paths = out["result"]
    assert "Second bot" in question
    assert paths == [f"/api/admin/mcp/tokens/{'c' * 16}/revoke"]


def test_server_values_are_text_never_markup(tmp_path):
    name = "<img src=x onerror=alert(1)>ZQNAME"
    account = "\"><script>1</script>ZQACC"
    out = _run(tmp_path, """
      await h.open('mcp');
      await h.click(h.revokeButtons()[0]);
      const list = h.$('mcpTokensList');
      return [list.textContent, list.querySelectorAll('img').length,
              list.querySelectorAll('script').length, h.text('userActionText')];""",
               tokens=[_row(name=name, mode="shared_account", account=account)])
    text, images, scripts, question = out["result"]
    assert name in text and account in text and name in question
    assert (images, scripts) == (0, 0)
    assert not [w for w in out["markupWrites"] if "ZQNAME" in w or "ZQACC" in w]


# ---------------------------------------------------------------- language
_EVERYTHING = """
  await h.open('mcp');
  h.set('mcpMode', 'shared_account');
  const texts = h.texts(h.$('secMcp'));
  await h.create('Fresh bot', 'per_user');
  texts.push(...h.texts(h.$('secMcp')), ...h.toasts());
  await h.click('btnMcpCopy');
  texts.push(...h.toasts());
  await h.click(h.revokeButtons()[0]);
  texts.push(h.text('userActionTitle'), h.text('userActionText'), h.text('btnUserActionGo'));
  await h.click('btnUserActionGo');
  texts.push(...h.texts(h.$('secMcp')), ...h.toasts());
  const nav = h.doc.querySelector('.adm-nav-item[data-section="mcp"]');
  return [texts, h.texts(nav), h.texts(h.$('secUsers')).concat(h.texts(h.doc.querySelector(
    '.adm-nav-item[data-section="users"]')))];"""
_SERVER_VALUES = ("https://pdc.corp.example/mcp", RAW, "Helpdesk bot", "Old kiosk", "Fresh bot",
                  "bot@corp.example", "ladmin")


@pytest.mark.parametrize("lang", ["geo", "ru"])
def test_the_whole_section_speaks_the_stored_language(tmp_path, lang):
    out = _run(tmp_path, _EVERYTHING, tokens=[ACTIVE, REVOKED], lang=lang)
    texts, nav, _ = out["result"]
    assert (GEORGIAN if lang == "geo" else CYRILLIC).search(" ".join(nav)), nav
    assert len(texts) > 20
    for text in texts:
        rest = text
        for value in _SERVER_VALUES:
            rest = rest.replace(value, " ")
        rest = re.sub(r"\d[\d./:,\sTZ+-]*(AM|PM)?( ?(UTC|GMT))?", " ", rest)      # a date
        assert _foreign_words(rest) == [], text


def test_the_rest_of_the_page_stays_as_it_is(tmp_path):
    english = _run(tmp_path, _EVERYTHING, tokens=[ACTIVE, REVOKED], lang="en")
    georgian = _run(tmp_path, _EVERYTHING, tokens=[ACTIVE, REVOKED], lang="geo")
    assert georgian["result"][2] == english["result"][2]
    assert georgian["docLang"] == english["docLang"] == "en"


@pytest.mark.parametrize("lang", [None, "xx"])
def test_english_is_the_language_without_a_known_one(tmp_path, lang):
    out = _run(tmp_path, "await h.open('mcp'); return h.texts(h.$('secMcp')).join(' ');",
               **({"lang": lang} if lang else {}))
    assert _values("en")["admin.mcp.per_user_note"] in out["result"]
    assert not GEORGIAN.search(out["result"]) and not CYRILLIC.search(out["result"])


def test_without_i18n_the_section_stays_english_and_shows_no_key(tmp_path):
    """The script was blocked or failed to load."""
    tag = '<script src="/static/i18n.js?v=0"></script>'
    assert ADMIN_HTML.count(tag) == 1
    scenario = """
      const els = h.$('secMcp').querySelectorAll('[data-i18n]').concat(h.doc.querySelectorAll(
        '.adm-nav-item[data-section="mcp"] [data-i18n]'));
      const statics = els.map((e) => [e.getAttribute('data-i18n'), e.textContent.trim()]);
      """ + _EVERYTHING.replace("return [", "return [statics, ")
    out = _run(tmp_path, scenario, html=ADMIN_HTML.replace(tag, ""),
               tokens=[ACTIVE, REVOKED], lang="geo")
    statics, texts, nav, _ = out["result"]
    assert out["loaded"] == ["/static/admin_data_sources.js"]
    en = _values("en")
    assert statics and all(" ".join(text.split()) == en[key] for key, text in statics), statics
    assert nav and len(texts) > 20
    for text in texts + nav:
        assert PREFIX not in text and "undefined" not in text, text
        assert not GEORGIAN.search(text), text
