"""Styled-table sanitiser: the remaining decorative properties, and the
dashboard tile container.

The contract pinned here (`html_sanitize.clean_styled_html`):

* `border-image` and every `border-image-*` longhand, `font-size-adjust`,
  `text-decoration-thickness` and `text-underline-offset` are DENIED — they
  can paint or offset content well outside the cell they sit on, and a
  table never needs them.
* `border-radius` and the four `border-<corner>-radius` longhands survive
  only when EVERY token is a non-negative length within 50px / 5em / 5rem /
  50% (a bare `0` included) — the `border-spacing` idiom; a larger value or
  a negative one drops the declaration.
* The same filter applies to an inline `style` attribute and to a
  `#T_`-scoped `<style>` rule; ordinary properties are unaffected.
* The dashboard tile's table wrapper carries `styled-table-container` (next
  to `pdc-tile-tablewrap`), the class `static/dashboard.css` clips with
  `contain: paint`, so a pinned styled table is clipped like the chat's.

Offline: pure functions plus two source-text checks.
"""
import re
from pathlib import Path

import pytest

import html_sanitize

ROOT = Path(__file__).resolve().parent.parent


def _flat(text) -> str:
    return (text or "").replace(" ", "").lower()


def _style_block(out: str) -> str:
    m = re.search(r"<style[^>]*>(.*?)</style>", out or "", re.S | re.I)
    return m.group(1) if m else ""


def _in_rule(decl: str) -> str:
    out = html_sanitize.clean_styled_html(
        f"<style>#T_x td {{ {decl}; color: green; }}</style>"
        f'<table id="T_x"><tr><td>1</td></tr></table>')
    assert out is not None
    return _flat(_style_block(out))


def _in_attr(decl: str) -> str:
    out = html_sanitize.clean_styled_html(
        f'<table id="T_x"><tr><td style="{decl}; color: green">1</td></tr></table>')
    assert out is not None
    return _flat(out)


PLACES = [pytest.param(_in_rule, id="rule"), pytest.param(_in_attr, id="attr")]

DENIED = [
    "border-image:none 30 stretch",
    "border-image-source:none",
    "border-image-width:2px",
    "border-image-outset:9999px",
    "border-image-slice:30",
    "border-image-repeat:stretch",
    "font-size-adjust:0.5",
    "text-decoration-thickness:9999px",
    "text-underline-offset:9999px",
]

RADIUS_KEPT = [
    "border-radius:4px", "border-radius:4px 8px", "border-radius:50%",
    "border-radius:0", "border-radius:50px", "border-radius:5em",
    "border-radius:5rem",
    "border-top-left-radius:4px", "border-top-right-radius:50%",
    "border-bottom-left-radius:5em", "border-bottom-right-radius:5rem",
]

RADIUS_DROPPED = [
    "border-radius:51px", "border-radius:6em", "border-radius:6rem",
    "border-radius:60%", "border-radius:999px 1px", "border-radius:-1px",
    "border-radius:4px -4px",
    "border-top-left-radius:51px", "border-top-right-radius:60%",
    "border-bottom-left-radius:-2px", "border-bottom-right-radius:9999px",
]

STILL_KEPT = ["color:red", "border-width:2px", "background-color:#eee",
              "border-spacing:4px", "font-weight:bold"]


@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("decl", DENIED)
def test_a_denied_decorative_property_is_dropped(place, decl):
    out = place(decl)
    prop = decl.split(":")[0]
    assert f"{prop}:" not in out, out
    assert "color:green" in out, out


@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("decl", RADIUS_KEPT)
def test_a_radius_within_bounds_survives(place, decl):
    """(Passes today — a guard that the bound does not over-reach.)"""
    assert _flat(decl) in place(decl)


@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("decl", RADIUS_DROPPED)
def test_a_radius_beyond_bounds_is_dropped(place, decl):
    out = place(decl)
    prop = decl.split(":")[0]
    assert f"{prop}:" not in out, out
    assert "color:green" in out, out


@pytest.mark.parametrize("place", PLACES)
@pytest.mark.parametrize("decl", STILL_KEPT)
def test_ordinary_properties_still_pass(place, decl):
    """(Passes today — regression guard.)"""
    assert _flat(decl) in place(decl)


def test_the_dashboard_tile_table_wrap_is_a_styled_table_container():
    src = (ROOT / "static" / "dashboard_view.js").read_text(encoding="utf-8")
    assignments = re.findall(r"className\s*=\s*['\"]([^'\"]*pdc-tile-tablewrap[^'\"]*)['\"]", src)
    assert assignments, "no className assignment for the tile table wrap"
    for classes in assignments:
        tokens = classes.split()
        assert "pdc-tile-tablewrap" in tokens and "styled-table-container" in tokens, classes


def test_the_container_class_is_clipped_in_the_stylesheet():
    """(Passes today — the rule the tile now relies on.)"""
    css = (ROOT / "static" / "dashboard.css").read_text(encoding="utf-8")
    m = re.search(r"\.styled-table-container\s*\{([^}]*)\}", css)
    assert m, "no .styled-table-container rule"
    body = _flat(m.group(1))
    assert "contain:paint" in body, body
    assert "overflow:auto" in body, body
