"""The /lab toast no longer covers the chat input (BRB findings 2, item 4).

The regression: `showToast` (static/dashboard.js) — e.g. "Added to
<dashboard>" after a pin — was fixed at `bottom:24px`, i.e. on top of the
chat input, and it took the clicks meant for the input for three seconds.
The fix moves it to the top of the viewport, below the top bar (the bar's
rendered box is measured when the toast is shown, with a fixed fallback
offset), and makes it click-through (`pointer-events:none`); text, colours,
duration and positioning mode are unchanged.

Offline: a text-level scan of the function (the
tests/test_rendering_isolation.py idiom) and, when `node` is on PATH, the
function evaluated against a small DOM stub.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_JS = ROOT / "static" / "dashboard.js"


def _show_toast_body() -> str:
    src = DASHBOARD_JS.read_text(encoding="utf-8")
    m = re.search(r"function\s+showToast\s*\(", src)
    assert m, "function showToast( not found in static/dashboard.js"
    i = src.index("{", m.end())
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    return src[i:]


def _no_space(s: str) -> str:
    return re.sub(r"\s+", "", s)


def test_toast_has_no_bottom_offset():
    body = _show_toast_body()
    assert not re.search(r"(?<![\w-])bottom\s*:", body), body
    assert not re.search(r"\.style\.bottom\s*=", body), body


def test_toast_sits_at_the_top():
    body = _show_toast_body()
    assert re.search(r"(?<![\w-])top\s*:", body) or re.search(r"\.style\.top\s*=", body), body


def test_toast_lets_clicks_through():
    body = _show_toast_body()
    flat = _no_space(body)
    assert ("pointer-events:none" in flat
            or re.search(r"""\.style\.pointerEvents=['"]none['"]""", flat)), body


def test_toast_text_colours_and_duration_unchanged():
    body = _show_toast_body()
    flat = _no_space(body)
    for colour in ("#10b981", "#ef4444", "#f59e0b"):
        assert colour in body, colour
    assert "3000" in body
    assert "position:fixed" in flat
    assert "textContent=text" in flat


# ---------------------------------------------------------------------------
# Below the top bar, measured at show time (review follow-up). A fixed
# `top:` guess sits ON the top bar whenever the bar is taller than the guess
# (wrapped actions, zoom, a narrow window); the offset is computed from the
# bar's rendered box when the toast is shown, with a fixed fallback.
# ---------------------------------------------------------------------------

def _top_level_functions(src: str) -> dict:
    out = {}
    for m in re.finditer(r"^(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(", src, re.M):
        i = src.index("{", m.end())
        depth = 0
        for j in range(i, len(src)):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    out[m.group(1)] = src[m.start():j + 1]
                    break
    return out


def _show_toast_with_helpers() -> str:
    """showToast's declaration plus every top-level dashboard.js function it
    (transitively) calls, so a helper that measures the bar is included."""
    funcs = _top_level_functions(DASHBOARD_JS.read_text(encoding="utf-8"))
    assert "showToast" in funcs
    picked, todo = [], ["showToast"]
    while todo:
        name = todo.pop()
        if name in picked:
            continue
        picked.append(name)
        for callee in re.findall(r"\b([A-Za-z_$][\w$]*)\s*\(", funcs[name]):
            if callee in funcs and callee not in picked:
                todo.append(callee)
    return "\n".join(funcs[n] for n in picked)


def test_toast_measures_the_top_bar_at_show_time():
    code = _show_toast_with_helpers()
    assert (re.search(r"""['"][^'"]*\.top-bar(?![\w-])[^'"]*['"]""", code)
            or re.search(r"""getElementsByClassName\(\s*['"]top-bar['"]""", code)), code
    assert "getBoundingClientRect" in code, code


def test_toast_keeps_a_fallback_top_offset_and_click_through():
    code = _show_toast_with_helpers()
    flat = _no_space(code)
    # A numeric fallback for when there is no bar to measure.
    assert re.search(r"\b\d+\b", re.sub(r"#[0-9a-fA-F]{3,8}", "", code))
    assert ("pointer-events:none" in flat
            or re.search(r"""\.style\.pointerEvents=['"]none['"]""", flat)), code
    assert not re.search(r"(?<![\w-])bottom\s*:", code), code
    assert not re.search(r"\.style\.bottom\s*=", code), code


_NODE_TOAST_HARNESS = r"""
const vm = require('vm');
const fs = require('fs');
const code = fs.readFileSync(process.argv[2], 'utf8');
function camel(p) { return p.trim().replace(/-([a-z])/g, (_, c) => c.toUpperCase()); }
function makeStyle() {
  const s = {};
  Object.defineProperty(s, 'cssText', {
    set(v) {
      for (const k of Object.keys(s)) delete s[k];
      String(v).split(';').forEach((decl) => {
        const i = decl.indexOf(':');
        if (i > 0) s[camel(decl.slice(0, i))] = decl.slice(i + 1).trim();
      });
    },
    get() { return ''; }, enumerable: false, configurable: true,
  });
  return s;
}
function run(barBottom) {
  const appended = [];
  const bar = barBottom === null ? null : {
    getBoundingClientRect() {
      return { top: 0, left: 0, right: 1200, bottom: barBottom, width: 1200, height: barBottom };
    },
    offsetHeight: barBottom, clientHeight: barBottom,
  };
  const document = {
    createElement() {
      const el = { style: makeStyle(), remove() {}, classList: { add() {} },
                   offsetHeight: 44, offsetWidth: 220 };
      el.getBoundingClientRect = function () {
        const top = parseFloat(el.style.top) || 0;
        return { top: top, bottom: top + 44, left: 490, right: 710, width: 220, height: 44 };
      };
      return el;
    },
    querySelector(sel) { return /\.top-bar$/.test(String(sel).trim()) ? bar : null; },
    querySelectorAll(sel) { return /\.top-bar$/.test(String(sel).trim()) && bar ? [bar] : []; },
    getElementsByClassName(n) { return n === 'top-bar' && bar ? [bar] : []; },
    getElementById() { return null; },
    body: { appendChild(el) { appended.push(el); } },
    documentElement: { clientHeight: 900, clientWidth: 1200 },
  };
  const ctx = { document, setTimeout() { return 0; }, clearTimeout() {},
                console: { warn() {}, log() {}, error() {} },
                getComputedStyle() { return {}; } };
  ctx.window = ctx;
  vm.createContext(ctx);
  vm.runInContext(code + '\nshowToast("Added to Q3", false);', ctx);
  const el = appended[0];
  return { top: el ? String(el.style.top || '') : null,
           pointerEvents: el ? String(el.style.pointerEvents || '') : null,
           bottom: el ? String(el.style.bottom || '') : null,
           text: el ? el.textContent : null };
}
process.stdout.write(JSON.stringify({ b51: run(51), b120: run(120), none: run(null) }));
"""


@pytest.fixture
def toast_run(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not on PATH")
    harness = tmp_path / "toast.js"
    harness.write_text(_NODE_TOAST_HARNESS, encoding="utf-8")
    code = tmp_path / "show_toast.js"
    code.write_text(_show_toast_with_helpers(), encoding="utf-8")
    proc = subprocess.run([node, str(harness), str(code)],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout)


def _px(value: str) -> float:
    m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)px\s*", value or "")
    assert m, f"top offset is not a px length: {value!r}"
    return float(m.group(1))


def test_node_toast_top_is_the_bar_bottom_plus_a_constant_gap(toast_run):
    t51 = _px(toast_run["b51"]["top"])
    t120 = _px(toast_run["b120"]["top"])
    gap = t51 - 51
    assert gap > 0, toast_run
    assert t120 - 120 == pytest.approx(gap), toast_run
    for case in ("b51", "b120", "none"):
        assert toast_run[case]["pointerEvents"] == "none", toast_run
        assert toast_run[case]["bottom"] == "", toast_run
        assert toast_run[case]["text"] == "Added to Q3", toast_run


def test_node_toast_without_a_top_bar_uses_the_fallback(toast_run):
    assert _px(toast_run["none"]["top"]) > 0, toast_run
