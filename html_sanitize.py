"""html_sanitize.py — reduce styled-table HTML to inert table markup.

`styled_html` is the rendered form of a pandas Styler: a `<style>` block
scoped to the table's `T_<uuid>` id followed by the table. The chat and
dashboard pages insert it into the page itself (it needs no script), so it
must carry nothing but table structure, cell text and a bounded set of
presentation properties. This module is the one place that decides what
survives:

* elements: table thead tbody tfoot tr th td caption colgroup col span div br
  (everything else is unwrapped to its text; script/style-like containers
  are dropped with their content);
* attributes: `class` (only the tokens a Styler generates — `T_<hex/_>`,
  `col<n>`, `row<n>`, `level<n>`, `data`, `index_name`, `blank`,
  `col_heading`, `row_heading`, and `row_trim` / `col_trim` on a truncated
  Styler's elided row and column; every other token is dropped, so a table
  cannot borrow one of the page's own classes), `id` (the Styler's `T_`
  prefix only, so a table cannot take over one of the page's own ids),
  `colspan`/`rowspan`/`span` (digits), `scope`, and `style` through the
  declaration filter below — no event handler, no link, no URL anywhere;
* `<style>` rules: at-rules are dropped, and a rule survives only when EVERY
  selector starts with `#T_` and is built from element/class/id tokens,
  descendant/child combinators and simple pseudo-classes; its declarations
  pass the property allowlist and the value filter; the surviving rules are
  re-emitted as ONE `<style>` element in front of the fragment;
* declaration values (inline `style` and scoped rules alike): no `!`
  (so no `!important`); a value with `(` survives only when every function
  in it is rgb/rgba/hsl/hsla; width/height and their min/max take `auto` or
  one non-negative length within 2000px / 100em / 100rem / 100%;
  margin*/padding* take one to four non-negative lengths within 200px /
  20em / 20rem (margins also `auto`); `font-size` takes a size keyword or
  one length within 72px / 5em / 5rem / 500%; `line-height` takes `normal`,
  a unitless number up to 10 or a length within 200px / 10em / 10rem; every
  length in a border width (`border-width`, `border-<side>-width` and the
  `border` / `border-<side>` shorthands) is within 20px / 2em / 2rem;
  `border-spacing` within 50px / 5em / 5rem; `border-radius` and the four
  `border-<corner>-radius` longhands take one to four lengths within 50px /
  5em / 5rem / 50%; `text-indent` within 0 ... 200px / 20em / 20rem.
  `text-shadow`, the `font` shorthand, `border-image` and its longhands,
  `font-size-adjust`, `text-decoration-thickness` and
  `text-underline-offset` are denied.

Positioning, stacking, transforms, generated content and filters are never
allowed, and box, font, line, border, spacing and indent sizes are bounded:
once script is gone, an overlay drawn from a table cell is the remaining way
markup could reach outside its own box (the pages also clip the container).
Only colour functions are allowed because any other function may make the
browser fetch something on its own.

Failure is closed: non-string, blank or oversize input, a missing `nh3`, or
any internal error answers None, and the pages then fall back to the plain
`{columns, rows}` table they already render when `styled_html` is absent.
Idempotent: cleaning a cleaned value returns it unchanged.

Leaf module: nh3 + re + logger_utils only. Main app only — the sandbox image
does not carry nh3.
"""
import re

try:  # the seam the tests patch; None when the wheel is absent
    import nh3
except Exception:  # pragma: no cover - exercised by patching the attribute
    nh3 = None

from logger_utils import log_with_sid

_SID = "sanitize"
_MAX_CHARS = 500_000
_MAX_SELECTOR_CHARS = 300

_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "th", "td", "caption",
         "colgroup", "col", "span", "div", "br"}
# Dropped WITH their content (never unwrapped to text).
_CLEAN_CONTENT_TAGS = {"script", "style", "noscript", "template", "iframe",
                       "object", "embed", "svg", "math", "textarea", "select",
                       "title", "xmp", "noembed", "noframes"}
_ATTRIBUTES = {
    "*": {"class", "id", "style"},
    "th": {"colspan", "rowspan", "scope"},
    "td": {"colspan", "rowspan", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
}

_ID_RE = re.compile(r"^T_[A-Za-z0-9_]+$")
_CLASS_TOKEN_RE = re.compile(
    r"^(?:T_[0-9a-f_]+|col\d+|row\d+|level\d+|data|index_name|blank"
    r"|col_heading|row_heading|row_trim|col_trim)$")
_DIGITS_RE = re.compile(r"^\d{1,3}$")
_SCOPE_RE = re.compile(r"^(?:row|col|rowgroup|colgroup)$")

_ALLOWED_PROPERTIES = frozenset({
    "color", "background", "background-color",
    "vertical-align", "white-space", "line-height", "opacity", "display",
    "border-collapse", "border-spacing", "table-layout", "caption-side",
    "width", "height", "min-width", "max-width", "min-height", "max-height",
})
_ALLOWED_PROPERTY_PREFIXES = ("font-", "text-", "border", "padding", "margin")
_DENIED_PROPERTIES = frozenset({
    "text-shadow", "font",
    # Paint or offset content outside the cell; a table never needs them.
    "border-image", "border-image-source", "border-image-width",
    "border-image-outset", "border-image-slice", "border-image-repeat",
    "font-size-adjust", "text-decoration-thickness", "text-underline-offset",
})
_PROPERTY_RE = re.compile(r"^[a-z][a-z-]{0,40}$")
_VALUE_RE = re.compile(r"^[A-Za-z0-9#%.,()\s'\"+\-/_]{1,200}$")
_VALUE_FORBIDDEN = ("url(", "expression(", "javascript", "@", "\\", "<", ">",
                    "{", "}", ";")

_FUNCTION_RE = re.compile(r"([A-Za-z_-][A-Za-z0-9_-]*)\s*\(")
_ALLOWED_FUNCTIONS = frozenset({"rgb", "rgba", "hsl", "hsla"})

_SIZE_PROPERTIES = frozenset({"width", "min-width", "max-width",
                              "height", "min-height", "max-height"})
_BOX_PREFIXES = ("margin", "padding")
_LENGTH_RE = re.compile(r"^(\d+(?:\.\d+)?|\.\d+)(px|em|rem|%)?$")
_SIZE_LIMITS = {"px": 2000, "em": 100, "rem": 100, "%": 100}
_BOX_LIMITS = {"px": 200, "em": 20, "rem": 20}
_FONT_SIZE_LIMITS = {"px": 72, "em": 5, "rem": 5, "%": 500}
_FONT_SIZE_KEYWORDS = frozenset({"xx-small", "x-small", "small", "medium", "large",
                                 "x-large", "xx-large", "xxx-large", "smaller",
                                 "larger"})
_LINE_HEIGHT_LIMITS = {"px": 200, "em": 10, "rem": 10}
_LINE_HEIGHT_UNITLESS_MAX = 10
_BORDER_WIDTH_LIMITS = {"px": 20, "em": 2, "rem": 2}
_BORDER_SPACING_LIMITS = {"px": 50, "em": 5, "rem": 5}
_BORDER_RADIUS_LIMITS = {"px": 50, "em": 5, "rem": 5, "%": 50}
_BORDER_RADIUS_PROPERTIES = frozenset({"border-radius", "border-top-left-radius",
                                       "border-top-right-radius",
                                       "border-bottom-left-radius",
                                       "border-bottom-right-radius"})
_TEXT_INDENT_LIMITS = {"px": 200, "em": 20, "rem": 20}
# The shorthands whose width is bounded, besides every `*-width` of a border.
_BORDER_SHORTHANDS = frozenset({"border", "border-top", "border-right",
                                "border-bottom", "border-left", "border-block",
                                "border-inline", "border-block-start",
                                "border-block-end", "border-inline-start",
                                "border-inline-end"})
_NUMERIC_START_RE = re.compile(r"^[+\-.\d]")
_COLOUR_FUNCTION_RE = re.compile(r"[A-Za-z-]+\([^)]*\)")

_SELECTOR_RE = re.compile(
    r"#T_[A-Za-z0-9_-]+"
    r"(?:[A-Za-z0-9_#.-]"
    r"|:[A-Za-z-]+(?:\([A-Za-z0-9+ -]*\))?"
    r"| > "
    r"| )*")
_STYLE_BLOCK_RE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.I | re.S)

_nh3_missing_logged = False


def _property_allowed(prop: str) -> bool:
    if not _PROPERTY_RE.match(prop) or prop in _DENIED_PROPERTIES:
        return False
    return prop in _ALLOWED_PROPERTIES or prop.startswith(_ALLOWED_PROPERTY_PREFIXES)


def _functions_allowed(value: str) -> bool:
    """True when every `(` in the value opens a colour function."""
    if "(" not in value:
        return True
    names = _FUNCTION_RE.findall(value)
    if len(names) != value.count("("):
        return False
    return all(name.lower() in _ALLOWED_FUNCTIONS for name in names)


def _length_within(token: str, limits: dict) -> bool:
    """One non-negative length whose unit is in `limits` and within it; a
    unitless value is accepted only when it is zero."""
    m = _LENGTH_RE.match(token.lower())
    if not m:
        return False
    number, unit = float(m.group(1)), m.group(2)
    if unit is None:
        return number == 0
    return unit in limits and number <= limits[unit]


def _lengths_within(value: str, limits: dict, count: range) -> bool:
    parts = value.split()
    return len(parts) in count and all(_length_within(p, limits) for p in parts)


def _border_width_allowed(value: str) -> bool:
    """Every numeric token of a border width / shorthand (colour functions
    set aside) is a length within the border bounds."""
    tokens = _COLOUR_FUNCTION_RE.sub(" ", value).split()
    return all(_length_within(t, _BORDER_WIDTH_LIMITS)
               for t in tokens if _NUMERIC_START_RE.match(t))


def _line_height_allowed(value: str) -> bool:
    low = value.lower()
    if low == "normal":
        return True
    m = _LENGTH_RE.match(low)
    if m and m.group(2) is None:
        return float(m.group(1)) <= _LINE_HEIGHT_UNITLESS_MAX
    return _length_within(low, _LINE_HEIGHT_LIMITS)


def _size_allowed(prop: str, value: str) -> bool:
    if prop in _SIZE_PROPERTIES:
        return value.lower() == "auto" or _length_within(value, _SIZE_LIMITS)
    if prop == "font-size":
        return value.lower() in _FONT_SIZE_KEYWORDS or _length_within(value, _FONT_SIZE_LIMITS)
    if prop == "line-height":
        return _line_height_allowed(value)
    if prop == "text-indent":
        return _length_within(value, _TEXT_INDENT_LIMITS)
    if prop == "border-spacing":
        return _lengths_within(value, _BORDER_SPACING_LIMITS, range(1, 3))
    if prop in _BORDER_RADIUS_PROPERTIES:
        return _lengths_within(value, _BORDER_RADIUS_LIMITS, range(1, 5))
    if prop in _BORDER_SHORTHANDS or (prop.startswith("border") and prop.endswith("-width")):
        return _border_width_allowed(value)
    if prop.startswith(_BOX_PREFIXES):
        parts = value.split()
        if not 1 <= len(parts) <= 4:
            return False
        allow_auto = prop.startswith("margin")
        return all((allow_auto and p.lower() == "auto") or _length_within(p, _BOX_LIMITS)
                   for p in parts)
    return True


def _value_allowed(prop: str, value: str) -> bool:
    low = value.lower()
    if any(bad in low for bad in _VALUE_FORBIDDEN):
        return False
    if not _VALUE_RE.match(value):
        return False
    return _functions_allowed(value) and _size_allowed(prop, value)


def _clean_declarations(text: str) -> list:
    """`prop:value` strings that pass both filters, in input order."""
    out = []
    for part in text.split(";"):
        if ":" not in part:
            continue
        prop, value = part.split(":", 1)
        prop = prop.strip().lower()
        value = " ".join(value.split())
        if not value or not _property_allowed(prop) or not _value_allowed(prop, value):
            continue
        out.append(f"{prop}:{value}")
    return out


def _selector_allowed(selector: str) -> bool:
    if not selector or len(selector) > _MAX_SELECTOR_CHARS:
        return False
    return _SELECTOR_RE.fullmatch(selector) is not None


def _normalize_selector(selector: str) -> str:
    selector = " ".join(selector.split())
    return re.sub(r"\s*>\s*", " > ", selector)


def _block_end(css: str, open_at: int) -> int:
    """Index just past the brace that closes the block opened at `open_at`
    (nested blocks included); len(css) when it never closes."""
    depth = 0
    for i in range(open_at, len(css)):
        ch = css[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(css)


def _clean_css(css: str) -> list:
    """The surviving rules of one stylesheet, each as `selectors{decls}`."""
    css = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    cut = css.find("/*")
    if cut >= 0:
        css = css[:cut]
    rules = []
    i, n = 0, len(css)
    while i < n:
        if css[i].isspace() or css[i] == ";" or css[i] == "}":
            i += 1
            continue
        if css[i] == "@":
            j = i
            while j < n and css[j] not in ";{":
                j += 1
            if j >= n:
                break
            i = j + 1 if css[j] == ";" else _block_end(css, j)
            continue
        brace = css.find("{", i)
        if brace < 0:
            break
        end = _block_end(css, brace)
        selector_text = css[i:brace]
        body = css[brace + 1:end - 1] if end <= n and css[end - 1:end] == "}" else ""
        i = end
        if "{" in body or "}" in selector_text or ";" in selector_text:
            continue
        selectors = [_normalize_selector(s) for s in selector_text.split(",")]
        if not selectors or not all(_selector_allowed(s) for s in selectors):
            continue
        decls = _clean_declarations(body)
        if not decls:
            continue
        rules.append(", ".join(selectors) + " {" + "; ".join(decls) + "}")
    return rules


def _attribute_filter(element, attribute, value):
    value = value if isinstance(value, str) else ""
    if attribute == "id":
        return value if _ID_RE.match(value) else None
    if attribute == "class":
        tokens = [t for t in value.split() if _CLASS_TOKEN_RE.match(t)]
        return " ".join(tokens) if tokens else None
    if attribute in ("colspan", "rowspan", "span"):
        return value if _DIGITS_RE.match(value.strip()) else None
    if attribute == "scope":
        return value if _SCOPE_RE.match(value.strip().lower()) else None
    if attribute == "style":
        decls = _clean_declarations(value)
        return ";".join(decls) if decls else None
    return None


def clean_styled_html(html):
    """Inert styled-table markup, or None (see the module docstring)."""
    global _nh3_missing_logged
    if not isinstance(html, str) or not html.strip() or len(html) > _MAX_CHARS:
        return None
    if nh3 is None:
        if not _nh3_missing_logged:
            _nh3_missing_logged = True
            log_with_sid(_SID, "warning",
                         "STYLED_HTML_SANITIZE_FAILED error=SanitizerUnavailable")
        return None
    try:
        rules = []
        for block in _STYLE_BLOCK_RE.findall(html):
            rules.extend(_clean_css(block))
        fragment = _STYLE_BLOCK_RE.sub("", html)
        cleaned = nh3.clean(
            fragment,
            tags=_TAGS,
            clean_content_tags=_CLEAN_CONTENT_TAGS,
            attributes=_ATTRIBUTES,
            attribute_filter=_attribute_filter,
            strip_comments=True,
            link_rel=None,
            url_schemes=set(),
        )
        style = ("<style>" + " ".join(rules) + "</style>") if rules else ""
        out = style + cleaned
        return out if out.strip() else None
    except Exception as e:
        log_with_sid(_SID, "warning",
                     f"STYLED_HTML_SANITIZE_FAILED error={type(e).__name__}")
        return None


def clean_table(table):
    """A shallow copy of a `{columns, rows, ...}` table whose `styled_html`
    is cleaned (removed when it cleans to nothing). Non-dicts, and dicts
    without `styled_html`, pass through unchanged in content."""
    if not isinstance(table, dict) or "styled_html" not in table:
        return table
    out = dict(table)
    cleaned = clean_styled_html(table.get("styled_html"))
    if cleaned is None:
        out.pop("styled_html", None)
    else:
        out["styled_html"] = cleaned
    return out


def clean_tables(tables):
    """`clean_table` over a list of tables; non-lists pass through."""
    if not isinstance(tables, list):
        return tables
    return [clean_table(t) for t in tables]
