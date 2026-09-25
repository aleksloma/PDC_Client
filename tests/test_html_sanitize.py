"""Styled-table HTML is reduced to inert table markup before it is served.

`styled_html` is the rendered form of a pandas Styler: a `<style>` block
scoped to a `T_<uuid>` table id plus the table itself. It is produced inside
the analysis sandbox and rendered by the browser as live markup in the chat
and dashboard pages, so it must carry nothing but table structure, cell text
and a bounded set of presentation properties.

The contract pinned here (`html_sanitize.py`, a leaf module):

* `clean_styled_html(html) -> str | None` — allowlist sanitiser (nh3).
  Tags: table thead tbody tfoot tr th td caption colgroup col span div br.
  Attributes: `class` (only the Styler's tokens: `T_<hex/underscore>`,
  `col<n>`, `row<n>`, `level<n>`, `data`, `index_name`, `blank`,
  `col_heading`, `row_heading`; other tokens dropped), `id`
  (`^T_[A-Za-z0-9_]+$` only), `colspan`/`rowspan` (digits), `scope`, `style`
  (property allowlist).
  `<style>` rules survive only when EVERY selector starts with `#T_`; at-rules
  are dropped; declarations pass a property allowlist and a value filter: a
  value with `(` survives only when every function is rgb/rgba/hsl/hsla;
  width/height and their min/max take `auto` or one non-negative length
  within 2000px / 100em / 100rem / 100%; margin*/padding* take up to four
  non-negative lengths within 200px / 20em / 20rem (margins also `auto`).
  The result is at most one `<style>` element followed by the fragment.
  Non-string / blank / oversize input, a missing nh3, or any internal error
  → `None` (the pages then fall back to the plain `{columns, rows}` table).
  Idempotent.
* `clean_table(table)` / `clean_tables(tables)` — shallow copies with
  `styled_html` replaced by the cleaned value, or removed when it cleans to
  `None`; the input is never mutated; other shapes pass through.
* `run_chat_local._styler_to_html` returns sanitised output, so every new
  `styled_html` (stream, regenerate, refresh) is clean at the source.

The module must hold nh3 as a module attribute named `nh3` (it may be `None`
when the import failed) and import `log_with_sid` into its own namespace —
the two seams these tests patch.

Offline: pure functions, no app, no storage.
"""
import importlib
import re

import pandas as pd
import pytest

PAYLOAD = ("<script>parent.document.title='x'</script>"
           "<img src=x onerror=\"fetch('/auth/me')\">")

# `run_chat_local._STYLED_MAX_CHARS` — the cap on a rendered Styler. Restated
# here rather than imported so this file does not load the chat pipeline.
STYLED_MAX_CHARS = 500_000

FORBIDDEN_MARKUP = ["<script", "onerror", "<img", "javascript:", "<iframe",
                    "<svg", "<link", "<meta", "<object", "<a ", "<a>", "href=",
                    "src=", "<!--"]


def _mod():
    try:
        return importlib.import_module("html_sanitize")
    except ImportError as e:
        pytest.fail(f"html_sanitize module is missing (clean_styled_html / "
                    f"clean_table / clean_tables): {e}")


def _clean(html):
    return _mod().clean_styled_html(html)


def _assert_inert(out):
    """`None` or a string carrying none of the active-markup needles."""
    if out is None:
        return
    assert isinstance(out, str), type(out)
    low = out.lower()
    for needle in FORBIDDEN_MARKUP:
        assert needle not in low, (needle, out[:400])
    assert not re.search(r"\son[a-z]+\s*=", low), out[:400]


def _cell(inner: str) -> str:
    return f"<table id=\"T_abc\"><tbody><tr><td>{inner}</td></tr></tbody></table>"


def _style_block(out: str) -> str:
    m = re.search(r"<style[^>]*>(.*?)</style>", out or "", re.S | re.I)
    return m.group(1) if m else ""


def _real_styler_html():
    df = pd.DataFrame({"revenue": [10, 250, 990], "region": ["North", "South", "West"]})
    return df.style.background_gradient(subset=["revenue"]).to_html()


# ---------------------------------------------------------------------------
# the task's markup
# ---------------------------------------------------------------------------
def test_the_payload_alone_cleans_to_inert_markup():
    _assert_inert(_clean(PAYLOAD))


def test_the_payload_inside_a_table_cell_keeps_the_table_and_loses_the_markup():
    out = _clean(_cell("safe-text " + PAYLOAD))
    assert out is not None
    _assert_inert(out)
    assert "<table" in out and "<td" in out, out
    assert "safe-text" in out, out


def test_a_real_styler_keeps_its_ids_text_classes_and_scoped_colours():
    html = _real_styler_html()
    table_id = re.search(r'<table id="(T_[A-Za-z0-9_]+)"', html).group(1)
    out = _clean(html)
    assert out is not None
    _assert_inert(out)
    assert f'id="{table_id}"' in out, out[:600]
    for text in ("North", "South", "West", "990"):
        assert text in out, (text, out[:600])
    assert "data row0 col0" in out, out[:600]
    css = _style_block(out)
    assert f"#{table_id}_row0_col0" in css, css
    assert "background-color" in css, css


def test_the_style_element_comes_first_and_only_once():
    out = _clean(_real_styler_html())
    assert out is not None
    assert out.lower().count("<style") == 1, out[:400]
    assert out.lstrip().lower().startswith("<style"), out[:200]


# ---------------------------------------------------------------------------
# <style> rules
# ---------------------------------------------------------------------------
def _with_css(css: str) -> str:
    return f"<style type=\"text/css\">{css}</style>" + _cell("v")


def test_a_rule_outside_the_table_scope_is_dropped():
    out = _clean(_with_css("body { display: none; } #T_abc td { color: red; }"))
    css = _style_block(out)
    assert "body" not in css, css
    assert "display" not in css, css
    assert "#T_abc td" in css and "red" in css, css


def test_a_rule_is_dropped_when_any_selector_escapes_the_scope():
    out = _clean(_with_css("#T_abc td, .page-header { color: red; }"))
    css = _style_block(out)
    assert "page-header" not in css, css
    assert "red" not in css, css


def test_a_class_only_rule_is_dropped():
    out = _clean(_with_css(".chat-input { background-color: red; }"))
    assert "chat-input" not in (out or ""), out


@pytest.mark.parametrize("at_rule", [
    "@import url('/auth/me');",
    "@media screen { #T_abc td { color: red; } }",
    "@font-face { font-family: x; src: url('/auth/me'); }",
])
def test_at_rules_are_dropped(at_rule):
    out = _clean(_with_css(at_rule + " #T_abc td { color: blue; }"))
    low = (out or "").lower()
    assert "@import" not in low and "@media" not in low and "@font-face" not in low, out
    assert "url(" not in low, out


@pytest.mark.parametrize("decl", [
    "background: url('/auth/me')",
    "background-image: url(data:image/png;base64,AAAA)",
    "width: expression(alert(1))",
    "background: javascript:alert(1)",
    "color: \\72 ed",
    "font-family: '<b>'",
])
def test_declarations_with_unsafe_values_are_dropped(decl):
    out = _clean(_with_css(f"#T_abc td {{ {decl}; color: green; }}"))
    css = _style_block(out).lower()
    for needle in ("url(", "expression(", "javascript", "\\", "<", ">"):
        assert needle not in css, (needle, css)
    assert "green" in css, css


@pytest.mark.parametrize("prop", ["position: fixed", "z-index: 9999",
                                  "transform: scale(40)", "content: 'x'",
                                  "filter: blur(3px)"])
def test_layout_escaping_properties_are_never_allowed(prop):
    name = prop.split(":")[0]
    out = _clean(_with_css(f"#T_abc td {{ {prop}; color: green; }}"))
    css = _style_block(out).lower()
    assert name not in css, (name, css)
    assert "green" in css, css


# ---------------------------------------------------------------------------
# attributes
# ---------------------------------------------------------------------------
def test_inline_style_keeps_allowed_properties_only():
    out = _clean('<table id="T_abc"><tr><td style="position:fixed;top:0;'
                 'color:red;background-color:#fee">x</td></tr></table>')
    assert out is not None
    low = out.lower()
    assert "position" not in low, out
    assert "color:red" in low.replace(" ", ""), out
    assert "#fee" in low, out


def test_an_id_outside_the_styler_prefix_is_dropped():
    """A table must not be able to take over the page's own element ids."""
    out = _clean('<table id="btnSave"><tr><td id="chatInput">x</td></tr></table>')
    assert out is not None
    assert "btnSave" not in out and "chatInput" not in out, out
    assert ">x<" in out.replace(" ", ""), out


def test_a_styler_id_is_kept():
    out = _clean('<table id="T_abc12"><tr><td id="T_abc12_row0_col0">x</td></tr></table>')
    assert 'id="T_abc12"' in out and 'id="T_abc12_row0_col0"' in out, out


def test_a_class_with_unexpected_characters_is_dropped():
    out = _clean('<table id="T_a"><tr><td class="x:y" >1</td><td class="data row0">2</td></tr></table>')
    assert "x:y" not in out, out
    assert 'class="data row0"' in out, out


def test_span_attributes_must_be_digits():
    out = _clean('<table id="T_a"><tr><td colspan="2" rowspan="x">1</td></tr></table>')
    assert 'colspan="2"' in out, out
    assert "rowspan" not in out, out


def test_event_attributes_and_links_are_removed_everywhere():
    out = _clean('<table id="T_a" onclick="x()"><tr><td onmouseover="y()">'
                 '<a href="javascript:z()">l</a><span onfocus="w()">s</span>'
                 '</td></tr></table>')
    _assert_inert(out)
    assert out is not None
    assert re.search(r">\s*l\s*<", out) and re.search(r">\s*s\s*<", out), out


@pytest.mark.parametrize("markup", [
    "<iframe src='/auth/me'></iframe>",
    "<svg onload='x()'><circle/></svg>",
    "<link rel='stylesheet' href='/x.css'>",
    "<meta http-equiv='refresh' content='0;url=/x'>",
    "<object data='/x'></object>",
    "<form action='/auth/logout'><button>b</button></form>",
    "<!-- note -->",
])
def test_non_table_elements_do_not_survive(markup):
    _assert_inert(_clean(_cell("t" + markup)))


# ---------------------------------------------------------------------------
# input shapes and failure
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("value", [None, 123, b"<table></table>", ["<table/>"],
                                   {"html": "x"}, "", "   \n\t"])
def test_non_string_and_blank_input_is_none(value):
    assert _clean(value) is None


def test_oversize_input_is_none():
    body = "<table id=\"T_a\"><tr><td>" + "x" * STYLED_MAX_CHARS + "</td></tr></table>"
    assert len(body) > STYLED_MAX_CHARS
    assert _clean(body) is None


def test_a_missing_nh3_answers_none(monkeypatch):
    mod = _mod()
    assert hasattr(mod, "nh3"), "html_sanitize must hold nh3 as a module attribute `nh3`"
    monkeypatch.setattr(mod, "nh3", None)
    assert mod.clean_styled_html(_real_styler_html()) is None


class _ExplodingNh3:
    """Delegates to the real nh3 except `clean`, which raises."""

    def __init__(self, real):
        self._real = real

    def clean(self, *a, **k):
        raise RuntimeError("cell-value-that-must-not-be-logged")

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_an_internal_error_answers_none_and_logs_the_type_only(monkeypatch):
    mod = _mod()
    assert hasattr(mod, "nh3") and mod.nh3 is not None, "nh3 must be importable here"
    assert hasattr(mod, "log_with_sid"), "html_sanitize must import log_with_sid"
    lines = []
    monkeypatch.setattr(mod, "log_with_sid",
                        lambda sid, level, msg, *a, **k: lines.append((level, str(msg), k)))
    monkeypatch.setattr(mod, "nh3", _ExplodingNh3(mod.nh3))
    assert mod.clean_styled_html(_real_styler_html()) is None
    joined = " | ".join(m for _, m, _ in lines)
    assert "STYLED_HTML_SANITIZE_FAILED" in joined, lines
    assert "RuntimeError" in joined, lines
    assert "cell-value-that-must-not-be-logged" not in repr(lines), lines
    assert any(level == "warning" for level, m, _ in lines
               if "STYLED_HTML_SANITIZE_FAILED" in m), lines


@pytest.mark.parametrize("html", [
    "real",
    _cell("a " + PAYLOAD),
    _with_css("body{display:none} #T_abc td{color:red;position:fixed}"),
    '<table id="btnSave"><tr><td colspan="3" style="color:blue">x</td></tr></table>',
])
def test_cleaning_is_idempotent(html):
    if html == "real":
        html = _real_styler_html()
    once = _clean(html)
    assert once is not None
    assert _clean(once) == once, (once[:300], _clean(once)[:300] if _clean(once) else None)


# ---------------------------------------------------------------------------
# table helpers
# ---------------------------------------------------------------------------
def test_clean_table_returns_a_cleaned_shallow_copy():
    mod = _mod()
    rows = [{"a": 1}]
    table = {"columns": ["a"], "rows": rows, "total_rows": 1,
             "styled_html": _cell("x" + PAYLOAD)}
    snapshot = dict(table)
    out = mod.clean_table(table)
    assert table == snapshot, "clean_table mutated its input"
    assert out is not table
    assert out["rows"] is rows
    assert out["columns"] == ["a"] and out["total_rows"] == 1
    _assert_inert(out.get("styled_html"))
    assert out.get("styled_html"), out


def test_clean_table_removes_styled_html_that_cleans_to_nothing():
    mod = _mod()
    table = {"columns": ["a"], "rows": [], "styled_html": "x" * (STYLED_MAX_CHARS + 1)}
    out = mod.clean_table(table)
    assert "styled_html" not in out, sorted(out)
    assert "styled_html" in table


def test_clean_table_without_styled_html_is_unchanged_in_content():
    mod = _mod()
    table = {"columns": ["a"], "rows": [{"a": 1}]}
    assert mod.clean_table(table) == table


@pytest.mark.parametrize("value", [None, "x", 3, ["a"]])
def test_clean_table_passes_non_dicts_through(value):
    assert _mod().clean_table(value) is value


def test_clean_tables_cleans_each_member_without_mutation():
    mod = _mod()
    tables = [{"title": "t1", "columns": ["a"], "rows": [], "styled_html": _cell(PAYLOAD)},
              {"title": "t2", "columns": ["a"], "rows": []}]
    before = [dict(t) for t in tables]
    out = mod.clean_tables(tables)
    assert tables == before, "clean_tables mutated its input"
    assert isinstance(out, list) and len(out) == 2
    _assert_inert(out[0].get("styled_html"))
    assert out[1] == tables[1]


@pytest.mark.parametrize("value", [None, "x", {"a": 1}])
def test_clean_tables_passes_non_lists_through(value):
    assert _mod().clean_tables(value) is value


# ---------------------------------------------------------------------------
# the source: run_chat_local._styler_to_html
# ---------------------------------------------------------------------------
def test_styler_with_a_hostile_cell_renders_inert():
    import run_chat_local
    df = pd.DataFrame({"label": ["ok", PAYLOAD], "v": [1, 2]})
    out = run_chat_local._styler_to_html(df.style)
    assert out is not None, "a small Styler must still render"
    _assert_inert(out)
    assert "ok" in out, out[:400]


def test_styler_background_gradient_keeps_its_formatting():
    import run_chat_local
    df = pd.DataFrame({"revenue": [10, 250, 990], "region": ["North", "South", "West"]})
    out = run_chat_local._styler_to_html(df.style.background_gradient(subset=["revenue"]))
    assert out is not None
    assert re.search(r'<table id="T_[A-Za-z0-9_]+"', out), out[:400]
    assert "North" in out and "990" in out, out[:400]
    assert "background-color" in _style_block(out), out[:400]


def test_a_transported_styler_with_hostile_markup_renders_inert():
    """The sandbox returns a Styler as its frame plus the HTML it rendered;
    that HTML is what the source sanitiser must clean."""
    import exec_transport
    import run_chat_local
    frame = pd.DataFrame({"a": [1]})
    styled = exec_transport.StyledFrame(frame, _cell("7" + PAYLOAD)
                                        + "<style>body{display:none}</style>")
    out = run_chat_local._styler_to_html(styled)
    _assert_inert(out)
    assert "display" not in (out or ""), out


def test_table_builders_carry_the_clean_markup():
    """`_build_table_from_result` / `_build_tables_from_result` build the
    `table` / `tables` the chat stream's `done` event and the history row
    carry verbatim, so they must hold the cleaned markup."""
    import exec_transport
    import run_chat_local
    frame = pd.DataFrame({"a": [1, 2]})
    hostile = exec_transport.StyledFrame(frame, _cell("7" + PAYLOAD))
    table = run_chat_local._build_table_from_result(hostile)
    assert table is not None and table["rows"], table
    _assert_inert(table.get("styled_html"))
    tables = run_chat_local._build_tables_from_result(
        {"first": hostile, "second": exec_transport.StyledFrame(frame, _cell(PAYLOAD))})
    assert tables and len(tables) == 2, tables
    for t in tables:
        _assert_inert(t.get("styled_html"))


# ---------------------------------------------------------------------------
# class tokens: only the names pandas Styler generates
#
# A class attribute keeps the Styler's own tokens — `T_<hex/underscore>`,
# `col<n>`, `row<n>`, `level<n>`, `data`, `index_name`, `blank`,
# `col_heading`, `row_heading` — and drops every other token, so a table
# cannot borrow one of the page's own classes (a page class can carry a
# fixed, full-viewport layout the table's own rules could never express).
# ---------------------------------------------------------------------------
_CLASS_ATTR_RE = re.compile(r'\bclass\s*=\s*"([^"]*)"')


def _class_tokens(out: str) -> list:
    return [tok for value in _CLASS_ATTR_RE.findall(out or "") for tok in value.split()]


def _classed_cell(cls: str, extra: str = "") -> str:
    return (f'<table id="T_abc"><tbody><tr><td class="{cls}"{extra}>v</td>'
            f'</tr></tbody></table>')


@pytest.mark.parametrize("cls,extra,gone", [
    ("dash-larger-modal", "", "dash-larger-modal"),
    ("sidebar-backdrop", ' style="display:block"', "sidebar-backdrop"),
], ids=["page-modal-class", "page-backdrop-class"])
def test_a_page_class_does_not_survive(cls, extra, gone):
    out = _clean(_classed_cell(cls, extra))
    assert out is not None, "the table itself must still render"
    assert gone not in out, out
    assert ">v<" in out.replace(" ", ""), out


def test_unknown_tokens_are_dropped_and_styler_tokens_kept():
    out = _clean(_classed_cell("data row0 col1 evil"))
    assert out is not None
    assert "evil" not in out, out
    assert _class_tokens(out) == ["data", "row0", "col1"], out


@pytest.mark.parametrize("cls", [
    "row_heading level0 row3",
    "col_heading level1 col12",
    "index_name level0",
    "blank level0",
    "blank",
    "T_abc12_",
    "data row10 col0",
])
def test_styler_class_tokens_survive(cls):
    out = _clean(_classed_cell(cls))
    assert out is not None
    assert _class_tokens(out) == cls.split(), out


@pytest.mark.parametrize("token", [
    "evil", "hidden", "modal", "rowx", "col1a", "level", "T_ghost", "row-1",
    "data-row", "Data",
])
def test_tokens_outside_the_styler_vocabulary_are_dropped(token):
    out = _clean(_classed_cell(f"data {token}"))
    assert out is not None
    assert _class_tokens(out) == ["data"], out


def test_a_real_styler_keeps_every_class_token():
    df = pd.DataFrame({"revenue": [10, 250, 990], "region": ["N", "S", "W"]})
    df.index.name = "idx"
    html = df.style.background_gradient(subset=["revenue"]).to_html()
    before = sorted(_class_tokens(html))
    out = _clean(html)
    assert out is not None
    assert sorted(_class_tokens(out)) == before, (before, out[:600])
    for tok in ("row_heading", "col_heading", "index_name", "blank", "data", "level0"):
        assert tok in before, (tok, before)


# ---------------------------------------------------------------------------
# CSS functions: only colour functions
#
# A value containing `(` survives only when every function in it is `rgb`,
# `rgba`, `hsl` or `hsla`. Image and reference functions can make the browser
# send a request of its own; the rest are not needed by a styled table.
# ---------------------------------------------------------------------------
FORBIDDEN_FUNCTION_VALUES = [
    ("image-set", "image-set('/auth/me' 1x)"),
    ("-webkit-image-set", "-webkit-image-set('/auth/me' 1x)"),
    ("src(", "src('/x')"),
    ("-moz-element", "-moz-element(#a)"),
    ("var(", "var(--x)"),
    ("attr(", "attr(title)"),
    ("calc(", "calc(1px + 2px)"),
    ("linear-gradient", "linear-gradient(red, blue)"),
    ("image-set", "rgb(1,2,3) image-set('/x' 1x)"),
    ("linear-gradient", "linear-gradient(rgb(1,2,3), blue)"),
]
ALLOWED_FUNCTION_VALUES = ["rgb(1,2,3)", "rgba(1,2,3,0.5)", "hsl(1,2%,3%)",
                           "hsla(1,2%,3%,.4)", "RGB(1,2,3)"]


def _flat(text) -> str:
    return (text or "").replace(" ", "").lower()


@pytest.mark.parametrize("needle,value", FORBIDDEN_FUNCTION_VALUES,
                         ids=[v for _, v in FORBIDDEN_FUNCTION_VALUES])
def test_a_forbidden_function_in_a_style_attribute_is_dropped(needle, value):
    out = _clean(f'<table id="T_abc"><tr><td style="background: {value}; '
                 f'color: green">v</td></tr></table>')
    assert out is not None
    assert needle.replace(" ", "") not in _flat(out), out
    assert "/auth/me" not in out and "'/x'" not in out, out
    assert "color:green" in _flat(out), out


@pytest.mark.parametrize("needle,value", FORBIDDEN_FUNCTION_VALUES,
                         ids=[v for _, v in FORBIDDEN_FUNCTION_VALUES])
def test_a_forbidden_function_in_a_scoped_rule_is_dropped(needle, value):
    out = _clean(_with_css(f"#T_abc td {{ background: {value}; color: green; }}"))
    css = _flat(_style_block(out))
    assert needle.replace(" ", "") not in css, css
    assert "/auth/me" not in css, css
    assert "green" in css, css


@pytest.mark.parametrize("value", ALLOWED_FUNCTION_VALUES)
def test_colour_functions_survive_in_a_style_attribute(value):
    out = _clean(f'<table id="T_abc"><tr><td style="background-color: {value}">v</td>'
                 f'</tr></table>')
    assert _flat(value) in _flat(out), out


@pytest.mark.parametrize("value", ALLOWED_FUNCTION_VALUES)
def test_colour_functions_survive_in_a_scoped_rule(value):
    out = _clean(_with_css(f"#T_abc td {{ background-color: {value}; }}"))
    assert _flat(value) in _flat(_style_block(out)), out


# ---------------------------------------------------------------------------
# size bounds
#
# width / min-width / max-width / height / min-height / max-height: `auto`
# or ONE non-negative length within 2000px / 100em / 100rem / 100%.
# margin* / padding*: non-negative lengths within 200px / 20em / 20rem, up to
# four values; margins also accept `auto` (table centring).
# ---------------------------------------------------------------------------
SIZE_DROPPED = [
    "width:99999px", "height:2001px", "width:101%", "width:-5px", "width:10vw",
    "width:1px 2px", "max-width:2001px", "min-height:101em", "max-height:101rem",
    "min-width:-1px", "height:1e5px",
]
SIZE_KEPT = [
    "width:120px", "max-width:100%", "height:2em", "width:auto", "width:2000px",
    "height:100rem", "min-width:100em", "max-height:50%",
]
BOX_DROPPED = [
    "margin:-9999px", "margin-left:-1px", "padding:500px", "padding:201px",
    "margin:21em", "padding-top:21rem", "margin:1px 2px 3px 4px 5px", "margin:10%",
    "padding:1px -1px",
]
BOX_KEPT = [
    "padding:4px 8px", "margin:0 auto", "margin:0", "padding:200px",
    "margin:1px 2px 3px 4px", "padding-left:20em", "margin-top:20rem",
]


def _decl_in_attr(decl: str):
    return _clean(f'<table id="T_abc"><tr><td style="{decl}; color: green">v</td>'
                  f'</tr></table>')


def _decl_in_rule(decl: str):
    return _style_block(_clean(_with_css(f"#T_abc td {{ {decl}; color: green; }}")))


@pytest.mark.parametrize("decl", SIZE_DROPPED + BOX_DROPPED)
def test_an_out_of_bounds_size_is_dropped_from_a_style_attribute(decl):
    out = _decl_in_attr(decl)
    assert out is not None
    prop = decl.split(":")[0]
    assert f"{prop}:" not in _flat(out), out
    assert "color:green" in _flat(out), out


@pytest.mark.parametrize("decl", SIZE_DROPPED + BOX_DROPPED)
def test_an_out_of_bounds_size_is_dropped_from_a_scoped_rule(decl):
    css = _flat(_decl_in_rule(decl))
    prop = decl.split(":")[0]
    assert f"{prop}:" not in css, css
    assert "green" in css, css


@pytest.mark.parametrize("decl", SIZE_KEPT + BOX_KEPT)
def test_an_in_bounds_size_survives_in_a_style_attribute(decl):
    assert _flat(decl) in _flat(_decl_in_attr(decl))


@pytest.mark.parametrize("decl", SIZE_KEPT + BOX_KEPT)
def test_an_in_bounds_size_survives_in_a_scoped_rule(decl):
    assert _flat(decl) in _flat(_decl_in_rule(decl))


# ---------------------------------------------------------------------------
# the overlay markup end to end through clean_table
# ---------------------------------------------------------------------------
OVERLAY_STYLED = (
    "<style>#T_a td { background: image-set('/auth/me' 1x); color: green; }</style>"
    '<div class="dash-larger-modal" style="margin:-9999px">'
    '<table id="T_a"><tr><td class="data row0 col0" '
    "style=\"background: image-set('/auth/me' 1x)\">cell-9</td></tr></table></div>")


def test_the_overlay_markup_is_cleaned_through_clean_table():
    table = {"columns": ["a"], "rows": [{"a": 1}], "total_rows": 1,
             "styled_html": OVERLAY_STYLED}
    out = _mod().clean_table(table)
    styled = out.get("styled_html") or ""
    assert "dash-larger-modal" not in styled, styled
    assert "image-set" not in styled.lower(), styled
    assert "/auth/me" not in styled, styled
    assert "-9999" not in styled, styled
    assert "cell-9" in styled, styled
    assert table["styled_html"] == OVERLAY_STYLED, "the input must not be mutated"
