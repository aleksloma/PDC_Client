"""C2 (p29) — structural pins on the /lab page and the stream route source
(C2_build_spec §1, §2, §3, §5; tests §6: "a structural test that every new
i18n key exists in en / geo / ru and that dashboard.js handles `message_key`
and the `caption` event").

* static/i18n.js: every new key in the en / geo / ru blocks, non-empty; the
  Georgian text in Georgian script, the Russian in Cyrillic; the English text
  as the spec gives it.
* static/dashboard.js `sendMessage`: the generic progress branch shows
  `_t(data.message_key, data.message)` (unknown keys fall back to `message`
  through `_t` itself); a branch for the `caption` event fills a chart's
  caption slot from `data.index` / `data.text`, without writing the text as
  raw HTML. The caption check must not swallow a single answer's done event,
  which carries `caption` as a STRING (the `data.narrative === true` lesson).
* `lab.stage_fixing`: when its text carries a placeholder for the attempt,
  the page substitutes `data.attempt`.
* routes/chat.py: the opening "Working..." event is keyed `lab.stage_working`.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DASH = (ROOT / "static" / "dashboard.js").read_text(encoding="utf-8")
I18N = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
CHAT_PY = (ROOT / "routes" / "chat.py").read_text(encoding="utf-8")

NEW_KEYS = ("lab.stage_planning", "lab.stage_running", "lab.stage_fixing",
            "lab.stage_describing", "lab.stage_summary", "lab.stage_working",
            "lab.taking_longer", "lab.analysis_failed")

EN_EXACT = {
    "lab.stage_planning": "Planning the analysis…",
    "lab.stage_running": "Running the analysis…",
    "lab.stage_working": "Working...",
    "lab.taking_longer": "This is taking longer than usual. The analysis is still running.",
    "lab.analysis_failed": "The analysis could not be completed. Please try again.",
}


def _lang_block(lang: str) -> str:
    starts = {k: I18N.index(f"\n  {k}: {{") for k in ("en", "geo", "ru")}
    order = sorted(starts, key=starts.get)
    i = order.index(lang)
    end = starts[order[i + 1]] if i + 1 < len(order) else len(I18N)
    return I18N[starts[lang]:end]


def _value(lang: str, key: str):
    m = re.search(rf"""['"]{re.escape(key)}['"]\s*:\s*(['"])(.+?)(?<!\\)\1""",
                  _lang_block(lang))
    return m.group(2) if m else None


def _function_body(src: str, name: str) -> str:
    m = re.search(
        rf"(function\s+{name}\s*\(|\b{name}\s*[:=]\s*(?:async\s+)?function\s*\(|"
        rf"\b{name}\s*[:=]\s*(?:async\s*)?\([^)]*\)\s*=>)", src)
    if not m:
        return ""
    start = src.find("{", m.end())
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return src[start:]


def _block_at(src: str, m) -> str:
    """The brace-matched block opened right after match `m`."""
    start = src.find("{", m.end())
    if start < 0:
        return ""
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
    return src[start:]


# A check that recognises the caption EVENT and not a single done's `caption`
# string: `=== true`, a typeof boolean test, or an explicit `!data.done` /
# index guard.
_CAPTION_CHECK = re.compile(
    r"if\s*\(\s*(?:"
    r"data\.caption\s*===\s*true"
    r"|typeof\s+data\.caption\s*===\s*['\"]boolean['\"]"
    r"|data\.caption\s*&&\s*!\s*data\.done"
    r"|data\.caption\s*&&\s*(?:typeof\s+)?data\.index"
    r")[^)]*\)")


def test_helpers_find_what_they_look_for():
    assert _function_body(DASH, "sendMessage")
    assert _value("en", "lab.brain_timeout")
    assert _value("geo", "lab.brain_timeout")
    assert _value("ru", "lab.brain_timeout")


def test_every_new_key_in_every_language():
    missing = [(lang, key) for lang in ("en", "geo", "ru") for key in NEW_KEYS
               if not (_value(lang, key) or "").strip()]
    assert not missing, missing


def test_english_texts_are_the_spec_texts():
    for key, text in EN_EXACT.items():
        assert _value("en", key) == text, (key, _value("en", key))
    fixing = _value("en", "lab.stage_fixing") or ""
    assert fixing.startswith("Fixing an error"), fixing


def test_translations_are_in_their_script_and_distinct():
    for key in NEW_KEYS:
        en, geo, ru = (_value(lang, key) or "" for lang in ("en", "geo", "ru"))
        assert any("ა" <= ch <= "ჿ" for ch in geo), (key, geo)
        assert any("А" <= ch <= "я" or ch in "Ёё" for ch in ru), (key, ru)
        assert len({en, geo, ru}) == 3, (key, en, geo, ru)


def test_fixing_placeholder_is_substituted_by_the_page():
    texts = [_value(lang, "lab.stage_fixing") or "" for lang in ("en", "geo", "ru")]
    if any(re.search(r"\{[^}]*\}|%\w", t) for t in texts):
        assert "data.attempt" in _function_body(DASH, "sendMessage"), \
            "lab.stage_fixing carries a placeholder but sendMessage never reads data.attempt"


def test_generic_progress_branch_shows_the_keyed_text():
    send = _function_body(DASH, "sendMessage")
    m = re.search(r"if\s*\(\s*data\.progress\s*\)", send)
    assert m, "the generic `if (data.progress)` branch is gone"
    branch = _block_at(send, m)
    assert re.search(r"_t\(\s*data\.message_key\s*,\s*data\.message", branch), branch


def test_send_message_handles_the_caption_event():
    send = _function_body(DASH, "sendMessage")
    m = _CAPTION_CHECK.search(send)
    assert m, ("sendMessage has no branch for the caption event "
               "(`data.caption === true`, or a guard that excludes the single done)")
    branch = _block_at(send, m)
    assert "data.index" in branch, branch[:600]
    assert "data.text" in branch, branch[:600]
    assert not re.search(r"innerHTML\s*=\s*data\.text\b", branch), branch[:600]
    assert re.search(r"\bcontinue\s*;", branch), "the caption branch must not fall through"


def test_opening_stream_event_is_keyed_working():
    i = CHAT_PY.find("\"Working...\"")
    assert i >= 0, "routes/chat.py no longer sends the Working... text"
    window = CHAT_PY[max(0, i - 300):i + 300]
    assert "lab.stage_working" in window, window
