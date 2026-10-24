"""The narrative template language (DIAGNOSTICS_SPEC A2) and the FINDINGS
validator / merge / history marker (B7.1-B7.3).

The brain writes a TEMPLATE (`{{total:count}}`, `{{#flag}}...{{/flag}}`); the
client fills it after execution with the block's VALUES and FINDINGS. A
rendered text is DISPLAY text: it carries real numbers and is never stored in
`content`, never passed to a `brain_client.*` function and never read by
/v1/title or /v1/report (A5.2 R0). Brain-bound text is built only from
templates — `history_content`, `brain_view` — which carry no value.

FINDINGS cross the data boundary (they ride on /v1/plan and in the history
marker), so `validate_findings` is the Article II guard for them: a key must
be a planner literal or a `pdc_diag` registry key, a value a bool, an INPUT
column name or a `FINDINGS_ENUM` code — never a number or a category value.

Main app only. Imports: stdlib, `pdc_diag`, `logger_utils` (and, lazily inside
`answer_lang_code`, `schema_builder._detect_language`). Every log line carries
constants, counts or `log_safe_value(...)` — never a template, a value or a
findings value. `render`, `fallback_text`, `brain_view` and `value_display`
never raise (Article IV).
"""
from __future__ import annotations

import re

import pdc_diag
from logger_utils import log_safe_value, log_with_sid

KINDS = ("count", "float", "pct", "ratio", "amount", "text", "period")
TEMPLATE_MAX = 4000
CAPTION_MAX = 600
RENDERED_MAX = 8000
CAPTION_RENDERED_MAX = 1200
FINDINGS_MAX_KEYS = 24
FINDINGS_STR_MAX = 64
MARKER_MAX = 400


class TemplateError(ValueError):
    """A template that does not follow the A2.1 grammar."""


_TOKEN_RE = re.compile(
    r"\{\{(?:(?P<sec>[#^/])(?P<sname>[A-Za-z_][A-Za-z0-9_]{0,39})"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_]{0,39})(?::(?P<kind>[a-z]{1,10}))?)\}\}")

_NAME = r"[A-Za-z_][A-Za-z0-9_]{0,39}"
_FALLBACK_BLOCK_RE = re.compile(r"\{\{([#^])(" + _NAME + r")\}\}[\s\S]*?\{\{/\2\}\}")
_FALLBACK_SECTION_TOKEN_RE = re.compile(r"\{\{[#^/]" + _NAME + r"\}\}")
_FALLBACK_SLOT_RE = re.compile(r"\{\{" + _NAME + r"(?::[a-z]{1,10})?\}\}")

_C0_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_SPACES_RE = re.compile(r"[ \t]{2,}")
_NEWLINES_RE = re.compile(r"\n{3,}")
_LEADING_DASH_RE = re.compile(r"^(\s*)-")

_NA = "n/a"
_ELLIPSIS = "…"

# The quality gate (`drop_na_sentences`): the "n/a" token, not inside a word;
# a sentence ends at ".", "!" or "?" followed by whitespace (or the line end),
# so the decimal point of a rendered number never splits a sentence.
_NA_TOKEN_RE = re.compile(r"(?<!\w)n/a(?!\w)", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(\s+)")

# FINDINGS (B7.1).
_FINDING_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_NEEDS_PHASE2 = "needs_phase2"
# The marker grammar's own characters plus every C0 control and DEL: a value
# holding one could break `[Findings: k=v; ...]` (B7.1 rule 6).
_MARKER_BAD_CHARS = frozenset(";]" + chr(0x2028) + chr(0x2029)
                              + "".join(chr(c) for c in range(0x20)) + chr(0x7f))

_RESULT_LABEL = {"en": "Result", "ka": "შედეგი", "ru": "Результат", "uz": "Natija"}
_LANG_CODES = {"English": "en", "Georgian": "ka", "Russian": "ru", "Uzbek": "uz"}
_VALUE_DISPLAY_MAX_LINES = 20
_VALUE_DISPLAY_KEY_MAX = 80

_CAPTION_INDEX_RE = re.compile(r"([1-9]|1[0-2])\Z")


# ---------------------------------------------------------------- parsing

def parse(template: str) -> list:
    """The template as a node list: ("text", s), ("slot", name, kind|None),
    ("section", "#"|"^", name, [text/slot nodes]). Raises TemplateError for a
    section inside a section, a close without an open, a close whose name
    differs from the open, and an open section never closed."""
    nodes: list = []
    current = nodes
    open_section = None
    pos = 0
    for m in _TOKEN_RE.finditer(template):
        if m.start() > pos:
            current.append(("text", template[pos:m.start()]))
        pos = m.end()
        sec = m.group("sec")
        if sec in ("#", "^"):
            if open_section is not None:
                raise TemplateError("section inside a section")
            open_section = (sec, m.group("sname"), [])
            current = open_section[2]
        elif sec == "/":
            if open_section is None:
                raise TemplateError("close without an open section")
            if m.group("sname") != open_section[1]:
                raise TemplateError("close name differs from the open section")
            nodes.append(("section", open_section[0], open_section[1], open_section[2]))
            current = nodes
            open_section = None
        else:
            current.append(("slot", m.group("name"), m.group("kind")))
    if pos < len(template):
        current.append(("text", template[pos:]))
    if open_section is not None:
        raise TemplateError("open section never closed")
    return nodes


# --------------------------------------------------------- shared helpers

def _section_shown(flag: str, findings: dict) -> bool:
    """The `{{#flag}}` condition (A2.2): (a) findings[flag] is True, (b) flag
    present with a non-bool value, (c) flag is a FINDINGS_ENUM code and some
    finding value equals it."""
    if flag in findings:
        value = findings[flag]
        if value is True or not isinstance(value, bool):
            return True
    if flag in pdc_diag.FINDINGS_ENUM:
        for value in findings.values():
            if isinstance(value, str) and value == flag:
                return True
    return False


def _md_escape(text: str) -> str:
    """Markdown escaping of a STRING value: `*`, `_` and a leading `-`."""
    text = text.replace("*", "\\*").replace("_", "\\_")
    return _LEADING_DASH_RE.sub(lambda m: m.group(1) + "\\-", text, count=1)


def _fmt_value(value, kind) -> str:
    """One value as display text: `pdc_diag.fmt` (unknown kind -> the type's
    default kind), a string value markdown-escaped; numbers never escaped."""
    text = pdc_diag.fmt(value, kind if kind in KINDS else None)
    if isinstance(value, str) and not isinstance(value, bool):
        text = _md_escape(text)
    return text


def _cap(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    limit = max(max_len - 1, 0)
    cut = -1
    for i in range(min(limit, len(text) - 1), -1, -1):
        if text[i].isspace():
            cut = i
            break
    body = text[:cut].rstrip() if cut > 0 else text[:limit]
    return body + _ELLIPSIS


def _post(text: str, max_len: int) -> str:
    """A2.2 post-processing, in order: newlines, C0 strip, space runs,
    newline runs, strip, cap."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _C0_RE.sub("", text)
    text = _SPACES_RE.sub(" ", text)
    text = _NEWLINES_RE.sub("\n\n", text)
    text = text.strip()
    return _cap(text, max_len)


def _walk(nodes: list, findings: dict, slot_text) -> str:
    out: list = []

    def emit(items):
        for node in items:
            if node[0] == "text":
                out.append(node[1])
            elif node[0] == "slot":
                out.append(slot_text(node[1], node[2]))

    for node in nodes:
        if node[0] == "section":
            shown = _section_shown(node[2], findings)
            if (node[1] == "#") == shown:
                emit(node[3])
        else:
            emit([node])
    return "".join(out)


def _fallback(template, max_len: int, placeholder: str) -> str:
    try:
        text = _FALLBACK_BLOCK_RE.sub("", template)
        text = _FALLBACK_SECTION_TOKEN_RE.sub("", text)
        text = _FALLBACK_SLOT_RE.sub(lambda _m: placeholder, text)
        return _post(text, max_len)
    except Exception:
        return ""


# ------------------------------------------------------------- rendering

def render(template: str, values: dict, findings: dict, *, sid: str,
           max_len: int = RENDERED_MAX) -> str:
    """The template filled with VALUES (slots) and FINDINGS (sections).
    A missing slot renders "n/a" and logs its NAME once per render. On a
    TemplateError or any other failure: `fallback_text`. Never raises."""
    try:
        nodes = parse(template)
        vals = values if isinstance(values, dict) else {}
        finds = findings if isinstance(findings, dict) else {}
        missing: list = []

        def slot_text(name, kind):
            if name in vals:
                return _fmt_value(vals[name], kind)
            if name not in missing:
                missing.append(name)
            return _NA

        text = _walk(nodes, finds, slot_text)
        for name in missing:
            log_with_sid(sid, "warning",
                         f"NARRATIVE_SLOT_MISSING name={log_safe_value(name, 40)}")
        return _post(text, max_len)
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"NARRATIVE_RENDER_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return fallback_text(template, max_len=max_len)


def fallback_text(template: str, *, max_len: int = RENDERED_MAX) -> str:
    """The template with every section removed and every slot "n/a"; any
    exception -> "". Never raises."""
    return _fallback(template, max_len, _NA)


def brain_view(template: str, findings: dict) -> str:
    """The text /v1/title and /v1/report see: sections resolved with FINDINGS
    exactly as `render` does, every slot `[name with spaces]` — no value. A
    parse error -> the fallback with "[value]" slots. Never raises."""
    try:
        try:
            nodes = parse(template)
        except Exception:
            return _fallback(template, RENDERED_MAX, "[value]")
        finds = findings if isinstance(findings, dict) else {}
        text = _walk(nodes, finds,
                     lambda name, _kind: "[" + name.replace("_", " ") + "]")
        return _post(text, RENDERED_MAX)
    except Exception:
        return ""


def plain(text: str) -> str:
    """Display text for the local PDF/PPTX: the three markdown escapes of
    string values removed."""
    if not isinstance(text, str):
        return ""
    return text.replace("\\*", "*").replace("\\_", "_").replace("\\-", "-")


def _drop_na_line(line: str) -> tuple:
    """`(kept line, dropped count, emptied)` for one line without "\\n"."""
    parts = _SENTENCE_SPLIT_RE.split(line)
    pieces = []                                  # (sentence, whitespace after)
    for i in range(0, len(parts), 2):
        sep = parts[i + 1] if i + 1 < len(parts) else ""
        pieces.append((parts[i], sep))
    kept = [(text, sep) for text, sep in pieces if not _NA_TOKEN_RE.search(text)]
    dropped = len(pieces) - len(kept)
    if not dropped:
        return line, 0, False
    out = "".join(text + sep for text, sep in kept).rstrip()
    return out, dropped, not out.strip()


def drop_na_sentences(text, *, sid: str, what: str) -> str:
    """The quality gate on a RENDERED text: every sentence (or line) holding
    the "n/a" token — a missing slot, a None value, or a literal the template
    typed — is dropped, then the A2.2 whitespace post-processing runs again.
    A text without the token is returned byte-identical. Logs one
    `NARRATIVE_SENTENCE_DROPPED reason=missing_slot n=<n> what=<what>` line
    when something was dropped — never the text. Non-str -> "". Never raises
    (a failure logs its type and returns the input)."""
    if not isinstance(text, str):
        return ""
    try:
        if not _NA_TOKEN_RE.search(text):
            return text
        lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        kept_lines = []
        total = 0
        for line in lines:
            out, dropped, emptied = _drop_na_line(line)
            total += dropped
            if emptied:
                continue                         # the line goes with its newline
            kept_lines.append(out)
        result = _post("\n".join(kept_lines), max(len(text), 1))
        if total:
            log_with_sid(sid, "info",
                         f"NARRATIVE_SENTENCE_DROPPED reason=missing_slot n={int(total)} "
                         f"what={log_safe_value(what, 20)}")
        return result
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"NARRATIVE_SENTENCE_GATE_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return text


# ---------------------------------------------------- received templates

def clean_template(value, *, max_len: int, sid: str, what: str) -> str | None:
    """A received template, or None when it is not a non-blank str or is
    longer than `max_len` (dropped, never truncated)."""
    if not isinstance(value, str) or not value.strip():
        return None
    if len(value) > max_len:
        log_with_sid(sid, "warning",
                     f"NARRATIVE_TEMPLATE_REJECTED what={log_safe_value(what, 40)} "
                     f"reason=too_long len={len(value)}")
        return None
    return value


def clean_captions(value, *, sid: str) -> dict:
    """{"1": template} -> {1: template}. A key outside 1..12, a non-str or
    blank value and a template over CAPTION_MAX are dropped."""
    out: dict = {}
    if not isinstance(value, dict):
        return out
    for key, template in value.items():
        if not isinstance(key, str) or not _CAPTION_INDEX_RE.match(key):
            log_with_sid(sid, "warning", "CAPTION_REJECTED index=- reason=bad_index")
            continue
        index = int(key)
        if not isinstance(template, str) or not template.strip():
            log_with_sid(sid, "warning", f"CAPTION_REJECTED index={int(index)} reason=empty")
            continue
        if len(template) > CAPTION_MAX:
            log_with_sid(sid, "warning", f"CAPTION_REJECTED index={int(index)} reason=too_long")
            continue
        out[index] = template
    return out


# -------------------------------------------------------------- FINDINGS

def _finding_value_ok(key: str, value, input_columns) -> bool:
    if key == _NEEDS_PHASE2:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return True
    if not isinstance(value, str) or not value or len(value) > FINDINGS_STR_MAX:
        return False
    if any(ch in _MARKER_BAD_CHARS for ch in value):
        return False
    return value in input_columns or value in pdc_diag.FINDINGS_ENUM


def validate_findings(raw, code: str, input_columns: frozenset, sid: str) -> dict:
    """B7.1, per block against that block's executed code: the key matches
    `^[a-z][a-z0-9_]{0,39}$` and appears as a quoted literal in the code or is
    a `pdc_diag` registry key; the value is a bool, an input column name or a
    FINDINGS_ENUM code (`needs_phase2`: a bool only) without a marker
    character. No cap here (merge time only). Drops are logged as a count."""
    if not isinstance(raw, dict):
        return {}
    try:
        text = code if isinstance(code, str) else ""
        columns = input_columns if input_columns is not None else frozenset()
        out: dict = {}
        dropped = 0
        for key, value in raw.items():
            key_ok = (isinstance(key, str) and _FINDING_KEY_RE.match(key) is not None
                      and (key in pdc_diag.PDC_DIAG_FINDING_KEYS
                           or re.search(r"(['\"])" + re.escape(key) + r"\1", text) is not None))
            if key_ok and _finding_value_ok(key, value, columns):
                out[key] = value
            else:
                dropped += 1
        if dropped:
            log_with_sid(sid, "info", f"FINDINGS_DROPPED n={int(dropped)}")
        return out
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"FINDINGS_VALIDATE_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return {}


def merge_values(acc: dict, new: dict, *, sid: str) -> None:
    """Merge a block's VALUES into `acc` in place; the later block wins."""
    if not isinstance(acc, dict) or not isinstance(new, dict):
        return
    try:
        collisions = sum(1 for k in new if k in acc)
        acc.update(new)
        if collisions:
            log_with_sid(sid, "info", f"VALUES_KEY_COLLISION n={int(collisions)}")
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"VALUES_MERGE_FAILED error={log_safe_value(type(e).__name__, 80)}")


def merge_findings(acc: dict, new: dict, *, sid: str) -> None:
    """Merge a block's validated FINDINGS into `acc` in place (callers walk
    the blocks in block_index order); the later block wins. Re-capped at
    FINDINGS_MAX_KEYS with the B7.2 priority: `needs_phase2`, then `pdc_diag`
    registry keys, then planner literals — each in the order first seen."""
    if not isinstance(acc, dict) or not isinstance(new, dict):
        return
    try:
        collisions = sum(1 for k in new if k in acc)
        acc.update(new)
        if collisions:
            log_with_sid(sid, "info", f"FINDINGS_KEY_COLLISION n={int(collisions)}")
        if len(acc) > FINDINGS_MAX_KEYS:
            registry = pdc_diag.PDC_DIAG_FINDING_KEYS
            order = ([k for k in acc if k == _NEEDS_PHASE2]
                     + [k for k in acc if k != _NEEDS_PHASE2 and k in registry]
                     + [k for k in acc if k != _NEEDS_PHASE2 and k not in registry])
            over = order[FINDINGS_MAX_KEYS:]
            for key in over:
                acc.pop(key, None)
            log_with_sid(sid, "info", f"FINDINGS_CAPPED n={len(over)}")
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"FINDINGS_MERGE_FAILED error={log_safe_value(type(e).__name__, 80)}")


def _marker_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def findings_marker(findings: dict) -> str:
    """`[Findings: k=v; ...]` with the keys sorted, bools as true/false;
    "" when empty; at most MARKER_MAX chars (keys added in sorted order while
    the line, including "; …]", fits)."""
    if not isinstance(findings, dict) or not findings:
        return ""
    try:
        parts = [f"{key}={_marker_value(findings[key])}"
                 for key in sorted(findings, key=str)]
        full = "[Findings: " + "; ".join(parts) + "]"
        if len(full) <= MARKER_MAX:
            return full
        kept: list = []
        for part in parts:
            candidate = "[Findings: " + "; ".join(kept + [part]) + "; " + _ELLIPSIS + "]"
            if len(candidate) > MARKER_MAX:
                break
            kept.append(part)
        if not kept:
            return "[Findings: " + _ELLIPSIS + "]"
        return "[Findings: " + "; ".join(kept) + "; " + _ELLIPSIS + "]"
    except Exception:
        return ""


def history_content(template: str, findings: dict) -> str:
    """The brain-safe `content` of a narrative turn: the marker FIRST (the
    brain trims history), then the template — never a rendered value."""
    text = template if isinstance(template, str) else ""
    marker = findings_marker(findings)
    return marker + "\n" + text if marker else text


# ---------------------------------------------------------- display line

def value_display(safe_preview, lang: str) -> str | None:
    """The local value line(s) of the summarize fallback (A6.4): a scalar ->
    "**Result:** v"; a flat dict -> up to 20 "- key: v" lines then "- …";
    anything else -> None. Display only (R0). Never raises."""
    try:
        label = _RESULT_LABEL.get(lang) if isinstance(lang, str) else None
        label = label or _RESULT_LABEL["en"]
        if isinstance(safe_preview, (bool, int, float, str)):
            text = f"**{label}:** {_fmt_value(safe_preview, None)}"
            log_with_sid("narrative", "info", "VALUE_DISPLAY_APPENDED kind=scalar")
            return _post(text, RENDERED_MAX)
        if isinstance(safe_preview, dict) and safe_preview:
            items = list(safe_preview.items())
            lines = [f"- {_md_escape(str(k)[:_VALUE_DISPLAY_KEY_MAX])}: {_fmt_value(v, None)}"
                     for k, v in items[:_VALUE_DISPLAY_MAX_LINES]]
            if len(items) > _VALUE_DISPLAY_MAX_LINES:
                lines.append("- " + _ELLIPSIS)
            log_with_sid("narrative", "info", "VALUE_DISPLAY_APPENDED kind=dict")
            return _post("\n".join(lines), RENDERED_MAX)
        return None
    except Exception as e:
        log_with_sid("narrative", "warning",
                     f"VALUE_DISPLAY_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return None


def answer_lang_code(context_decision: dict | None, question: str) -> str:
    """The answer language code ("en" | "ka" | "ru" | "uz"): the brain's
    `answer_language` when it is a known name, else the question's script
    (en/ka/ru only, so Uzbek comes only from the context decision).
    Unknown -> "en". Never raises."""
    try:
        if isinstance(context_decision, dict):
            name = context_decision.get("answer_language")
            if isinstance(name, str) and name in _LANG_CODES:
                return _LANG_CODES[name]
        # run_chat_local._detect_answer_language is this same function
        # (an import alias); imported lazily so this module stays light.
        from schema_builder import _detect_language
        code = _detect_language(question if isinstance(question, str) else "")
        return code if code in ("en", "ka", "ru", "uz") else "en"
    except Exception:
        return "en"
