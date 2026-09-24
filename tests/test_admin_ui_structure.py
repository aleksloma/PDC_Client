"""Structural pins on the admin connection form's TLS guidance.

The connection modal pre-fills a dialect's TLS defaults (ClickHouse: port 9440
with "SSL / Encrypt" ticked) ONLY when a NEW connection is being created — a
stored connection's values are never overwritten — and shows a warning line
when the port is the dialect's plaintext port or SSL is unticked. The badge is
recomputed whenever the dialect, the port or the SSL box changes.

There is no JS test runner in this repo, so these are text-level pins on the
served template and script; they fail if the badge element, its updater or its
wiring is removed.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HTML = (ROOT / "templates" / "admin_data_sources.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "admin_data_sources.js").read_text(encoding="utf-8")


def _function_body(src: str, name: str) -> str:
    """Body of `function name(...) { ... }` by brace matching (strings in
    this file do not contain unbalanced braces inside these functions)."""
    m = re.search(r"function\s+" + re.escape(name) + r"\s*\([^)]*\)\s*\{", src)
    assert m, f"function {name} not found"
    i = m.end()
    depth = 1
    while depth and i < len(src):
        c = src[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        i += 1
    return src[m.end():i - 1]


def _tag_with_id(html: str, elem_id: str) -> str:
    m = re.search(r"<[a-zA-Z]+[^>]*\bid=\"" + re.escape(elem_id) + r"\"[^>]*>", html)
    assert m, f'no element with id="{elem_id}"'
    return m.group(0)


def _listener_wires_badge(src: str, elem_id: str, events: tuple) -> bool:
    """True when `$('<id>')...addEventListener('<event>', X)` exists for one
    of `events` and X is `_updateTlsBadge` or a handler whose body calls it."""
    pat = re.compile(
        r"\$\(\s*['\"]" + re.escape(elem_id) + r"['\"]\s*\)\??\.addEventListener\(\s*"
        r"['\"](" + "|".join(events) + r")['\"]\s*,\s*([^;]*?)\)\s*;", re.S)
    for m in pat.finditer(src):
        handler = m.group(2)
        if "_updateTlsBadge" in handler:
            return True
        name = re.fullmatch(r"\s*([A-Za-z_$][\w$]*)\s*", handler)
        if name:
            try:
                if "_updateTlsBadge" in _function_body(src, name.group(1)):
                    return True
            except AssertionError:
                pass
    return False


def test_tls_warning_element_exists_hidden_by_default():
    tag = _tag_with_id(HTML, "connTlsWarn")
    cls = re.search(r'class="([^"]*)"', tag)
    assert cls, tag
    classes = cls.group(1).split()
    assert "adm-alert" in classes and "warn" in classes, tag
    assert "hidden" in classes, "the warning must be hidden until the updater shows it"


def test_tls_warning_sits_inside_the_connection_modal():
    modal = HTML.index('id="connModal"')
    warn = HTML.index('id="connTlsWarn"')
    ssl_box = HTML.index('id="connSsl"')
    test_btn = HTML.index('id="btnTestConn"')
    assert modal < warn < test_btn
    assert ssl_box < warn, "the warning belongs under the SSL / trust checkboxes"


def test_badge_updater_defined_and_reads_the_registry_fields():
    body = _function_body(JS, "_updateTlsBadge")
    assert "connTlsWarn" in body
    assert "plaintext_port" in body
    assert "connSsl" in body and "connPort" in body


def test_ssl_default_is_referenced():
    assert "ssl_default" in JS
    assert "plaintext_port" in JS


def test_badge_recomputes_on_dialect_port_and_ssl_changes():
    assert _listener_wires_badge(JS, "connType", ("change", "input")), \
        "connType change must refresh the TLS badge"
    assert _listener_wires_badge(JS, "connPort", ("change", "input")), \
        "connPort edits must refresh the TLS badge"
    assert _listener_wires_badge(JS, "connSsl", ("change", "input")), \
        "toggling SSL must refresh the TLS badge"


def test_badge_computed_after_the_form_loads():
    """Opening the modal (create or edit) must leave the badge in the right
    state — directly, or through onDialectChange which openConnModal calls."""
    open_body = _function_body(JS, "openConnModal")
    dialect_body = _function_body(JS, "onDialectChange")
    assert "_updateTlsBadge" in open_body or (
        "onDialectChange" in open_body and "_updateTlsBadge" in dialect_body)


def test_ssl_default_ticks_ssl_only_for_a_new_connection():
    """Every place that reads `ssl_default` must sit behind a guard on the
    stored-connection id (`editingConnId`), so editing an existing connection
    never flips its SSL box."""
    hits = [m.start() for m in re.finditer(r"\bssl_default\b", JS)]
    assert hits, "ssl_default is never read"
    guard = re.compile(r"!\s*editingConnId\b|editingConnId\s*(?:===?|==)\s*(?:null|undefined)"
                       r"|editingConnId\s*\)\s*return|if\s*\(\s*editingConnId\s*\)")
    for pos in hits:
        window = JS[max(0, pos - 400):pos + 200]
        assert guard.search(window), \
            "ssl_default used without a create-mode guard:\n" + window
        assert re.search(r"connSsl['\"]\s*\)\s*\.checked\s*=", window), \
            "ssl_default should drive the SSL checkbox:\n" + window


def test_a_stored_blank_port_connection_is_shown_on_its_plaintext_port():
    """The server resolves a blank port to the plaintext port while SSL is
    off, so the form must pre-fill the same port for a STORED connection —
    otherwise opening and re-saving it would move it to the TLS port."""
    body = JS[JS.index("function onDialectChange"):]
    body = body[:body.index("_prevDialectKey = d.key")]
    assert re.search(r"editingConnId\s*&&\s*d\.plaintext_port\s*&&\s*!\$\('connSsl'\)\.checked",
                     body), "stored blank-port fallback to the plaintext port is missing"
