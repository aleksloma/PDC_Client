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

import math
import numbers
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
    """A template that does not follow the A2.1 grammar. `kind` names the
    error: "nested" (a section inside a section), "unclosed" (an open section
    never closed) or "mismatched" (a close whose name differs from the open,
    or a close without an open)."""

    def __init__(self, message: str, kind: str = "mismatched"):
        super().__init__(message)
        self.kind = kind


PARSE_ERROR_KINDS = ("nested", "unclosed", "mismatched")


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

# The first-sentence slot cause (A5.1, `first_sentence_na_slot`): in marking
# mode a slot that would render "n/a" because its key is missing, its value is
# None or non-finite renders a private-use marker holding its index instead —
# no ".!?" and no whitespace, so the sentence split matches the "n/a" render.
_MARK_OPEN = ""
_MARK_CLOSE = ""
_MARK_RE = re.compile(_MARK_OPEN + r"(\d{1,4})" + _MARK_CLOSE)
SLOT_CAUSES = ("missing", "none", "nan")
_SLOT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}\Z")

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

# A section-name prefix (A2.2): a lower-case letter, up to 9 more letters /
# digits, "_" — pdc_diag's own `prefix=` rule — then a FINDINGS_ENUM code or
# a registry key. The prefix holds no "_" before its last character, so the
# split is at the first underscore. It resolves only when PASSED.
_PASSED_PREFIX_RE = re.compile(r"[a-z][a-z0-9]{0,9}_\Z")
_SECTION_PREFIX_RE = re.compile(r"([a-z][a-z0-9]{0,9}_)(.+)\Z")
_SECTION_LOG_MAX = 40


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
                raise TemplateError("section inside a section", "nested")
            open_section = (sec, m.group("sname"), [])
            current = open_section[2]
        elif sec == "/":
            if open_section is None:
                raise TemplateError("close without an open section", "mismatched")
            if m.group("sname") != open_section[1]:
                raise TemplateError("close name differs from the open section", "mismatched")
            nodes.append(("section", open_section[0], open_section[1], open_section[2]))
            current = nodes
            open_section = None
        else:
            current.append(("slot", m.group("name"), m.group("kind")))
    if pos < len(template):
        current.append(("text", template[pos:]))
    if open_section is not None:
        raise TemplateError("open section never closed", "unclosed")
    return nodes


def template_error_kind(template) -> str | None:
    """The parse-error kind of `template` ("nested" | "unclosed" |
    "mismatched"), or None when it parses (or is not a str). Never raises."""
    if not isinstance(template, str):
        return None
    try:
        parse(template)
        return None
    except TemplateError as e:
        kind = getattr(e, "kind", None)
        return kind if kind in PARSE_ERROR_KINDS else "mismatched"
    except Exception as e:
        # A crash is no verdict on the template: never worth a brain retry.
        log_with_sid("narrative", "warning",
                     f"NARRATIVE_TEMPLATE_CHECK_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return None


# --------------------------------------------------------- shared helpers

def _flag_shown(flag: str, findings: dict) -> bool:
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


def _is_code(name: str) -> bool:
    return name in pdc_diag.FINDINGS_ENUM or name in pdc_diag.PDC_DIAG_FINDING_KEYS


def passed_prefixes(prefixes) -> frozenset:
    """The PASSED prefixes as a frozenset: every str of the iterable that
    matches `[a-z][a-z0-9]{0,9}_` (pdc_diag's own prefix rule). None, a
    non-iterable or junk -> empty. Never raises."""
    try:
        if prefixes is None:
            return frozenset()
        if isinstance(prefixes, str):
            prefixes = (prefixes,)
        return frozenset(p for p in prefixes
                         if isinstance(p, str) and _PASSED_PREFIX_RE.match(p))
    except Exception:
        return frozenset()


def _resolve_section(name: str, findings: dict, prefixes: frozenset = frozenset()) -> tuple:
    """`(shown, code)` of the section name `name` (A2.2):
    - an exact findings key wins (its own condition, no prefix lookup);
    - a FINDINGS_ENUM code or a `pdc_diag` registry key is itself;
    - otherwise `<p><code>` resolves to <code> (an enum code or a registry
      key) ONLY when `<p>` is one of the PASSED `prefixes` — the prefixes
      the block code hands a pdc_diag helper as `prefix="<p>"`, the brain's
      template-validation rule (`ovl_metric_definition_overlap` ->
      `metric_definition_overlap` when the code passed "ovl_"). A prefix
      the code never passes (`no_`, `not_`, `has_`) never resolves, so a
      negated name is never shown as the bare code;
    - anything else is unknown: `(False, None)`."""
    if name in findings or _is_code(name):
        return _flag_shown(name, findings), name
    if prefixes:
        m = _SECTION_PREFIX_RE.match(name)
        if m is not None and m.group(1) in prefixes and _is_code(m.group(2)):
            code = m.group(2)
            return _flag_shown(code, findings), code
    return False, None


def _section_shown(flag: str, findings: dict, prefixes=None) -> bool:
    """The resolved `{{#flag}}` condition (`_resolve_section`)."""
    return _resolve_section(flag, findings, passed_prefixes(prefixes))[0]


def _md_escape(text: str) -> str:
    """Markdown escaping of a STRING value: `*`, `_` and a leading `-`."""
    text = text.replace("*", "\\*").replace("_", "\\_")
    return _LEADING_DASH_RE.sub(lambda m: m.group(1) + "\\-", text, count=1)


def _fmt_value(value, kind, locale=None) -> str:
    """One value as display text: `pdc_diag.fmt` (unknown kind -> the type's
    default kind) in `locale`, a string value markdown-escaped; numbers never
    escaped."""
    text = pdc_diag.fmt(value, kind if kind in KINDS else None,
                        locale if isinstance(locale, str) else None)
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


def _walk(nodes: list, findings: dict, slot_text, *, shown_sections=None,
          unknown=None, prefixes: frozenset = frozenset()) -> str:
    """The text of `nodes`. `shown_sections` (a list) collects
    `(code, body text)` of every `{{#…}}` section shown; `unknown` (a list)
    collects every section name that resolves to nothing, once each;
    `prefixes` are the passed section-name prefixes (`passed_prefixes`)."""
    out: list = []

    def emit(items, sink):
        for node in items:
            if node[0] == "text":
                sink.append(node[1])
            elif node[0] == "slot":
                sink.append(slot_text(node[1], node[2]))

    for node in nodes:
        if node[0] == "section":
            shown, code = _resolve_section(node[2], findings, prefixes)
            if code is None and unknown is not None and node[2] not in unknown:
                unknown.append(node[2])
            if (node[1] == "#") == shown:
                body: list = []
                emit(node[3], body)
                text = "".join(body)
                out.append(text)
                if node[1] == "#" and code is not None and shown_sections is not None:
                    shown_sections.append((code, text))
        else:
            emit([node], out)
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
           max_len: int = RENDERED_MAX, locale: str | None = None,
           prefixes=None) -> str:
    """The template filled with VALUES (slots, formatted in `locale`) and
    FINDINGS (sections; a prefixed section name resolves only through a
    PASSED prefix in `prefixes`). A missing slot renders "n/a" and logs its
    NAME once per render. On a TemplateError or any other failure:
    `fallback_text`. Never raises."""
    return render_with_sections(template, values, findings, sid=sid, max_len=max_len,
                                locale=locale, prefixes=prefixes)[0]


def _na_slot_cause(values: dict, name: str) -> str | None:
    """Why slot `name` renders "n/a" — "missing" (no key), "none" (None) or
    "nan" (a non-finite number) — or None for any other value (a fmt
    overflow or an unsupported type is no slot cause)."""
    if name not in values:
        return "missing"
    value = values[name]
    if value is None:
        return "none"
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        try:
            return None if math.isfinite(value) else "nan"
        except (TypeError, ValueError, OverflowError):
            return None
    return None


def render_with_sections(template: str, values: dict, findings: dict, *, sid: str,
                         max_len: int = RENDERED_MAX,
                         locale: str | None = None, prefixes=None,
                         _marks: list | None = None) -> tuple:
    """`(text, sections)`: the `render` text, and `(resolved code, body text)`
    of every `{{#…}}` section the render showed (a `{{^…}}` section is never
    listed — it says something is NOT the case). `prefixes`: the section-name
    prefixes the code passed (None / empty -> no prefixed name resolves). A
    section name resolving to nothing logs `NARRATIVE_SECTION_UNKNOWN name=`
    once per render. On a failure: `(fallback_text, [])`. Never raises.

    `_marks` (private, `first_sentence_na_slot` only): a list that turns on
    MARKING mode — a slot with a slot cause (`_na_slot_cause`) renders a
    private-use marker instead of "n/a" and appends `(name, cause)`; the
    missing / unknown log lines are not repeated for this diagnostic pass."""
    try:
        passed = passed_prefixes(prefixes)
        nodes = parse(template)
        vals = values if isinstance(values, dict) else {}
        finds = findings if isinstance(findings, dict) else {}
        missing: list = []
        sections: list = []
        unknown: list = []

        def slot_text(name, kind):
            if _marks is not None:
                cause = _na_slot_cause(vals, name)
                if cause is not None:
                    _marks.append((name, cause))
                    return f"{_MARK_OPEN}{len(_marks) - 1}{_MARK_CLOSE}"
            if name in vals:
                return _fmt_value(vals[name], kind, locale)
            if name not in missing:
                missing.append(name)
            return _NA

        text = _walk(nodes, finds, slot_text, shown_sections=sections, unknown=unknown,
                     prefixes=passed)
        if _marks is None:
            for name in missing:
                log_with_sid(sid, "warning",
                             f"NARRATIVE_SLOT_MISSING name={log_safe_value(name, 40)}")
            for name in unknown:
                log_with_sid(sid, "info",
                             f"NARRATIVE_SECTION_UNKNOWN name={log_safe_value(name, _SECTION_LOG_MAX)}")
        shown = [(code, _post(body, max(len(body), 1))) for code, body in sections]
        return _post(text, max_len), shown
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"NARRATIVE_RENDER_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return fallback_text(template, max_len=max_len), []


def locale_for(lang) -> str | None:
    """The `pdc_diag.fmt` locale of an answer language code: "uz" / "ru"
    for those languages, None (the default locale) for every other."""
    return lang if isinstance(lang, str) and lang in ("uz", "ru") else None


def fallback_text(template: str, *, max_len: int = RENDERED_MAX) -> str:
    """The template with every section removed and every slot "n/a"; any
    exception -> "". Never raises."""
    return _fallback(template, max_len, _NA)


def brain_view(template: str, findings: dict, *, prefixes=None) -> str:
    """The text /v1/title and /v1/report see: sections resolved with FINDINGS
    exactly as `render` does, every slot `[name with spaces]` — no value. Its
    callers pass no `prefixes`, so a prefixed section is false there (the
    safe direction). A parse error -> the fallback with "[value]" slots.
    Never raises."""
    try:
        try:
            nodes = parse(template)
        except Exception:
            return _fallback(template, RENDERED_MAX, "[value]")
        finds = findings if isinstance(findings, dict) else {}
        text = _walk(nodes, finds,
                     lambda name, _kind: "[" + name.replace("_", " ") + "]",
                     prefixes=passed_prefixes(prefixes))
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


# ------------------------------------------------- first sentence, coverage

_WS_RUN_RE = re.compile(r"\s+")


def _first_sentence(text: str) -> str | None:
    """The first sentence of `text`: the first non-blank line, cut at the
    first sentence end; None when every line is blank."""
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        return _SENTENCE_SPLIT_RE.split(line.strip(), maxsplit=1)[0]
    return None


def first_sentence_has_na(text) -> bool:
    """True when the FIRST sentence of a rendered text (the first non-blank
    line, cut at the first sentence end) holds the "n/a" token — the
    headline of the answer would be lost to the quality gate, so the
    template counts as invalid (A5.1). Never raises."""
    try:
        if not isinstance(text, str):
            return False
        first = _first_sentence(text)
        return first is not None and _NA_TOKEN_RE.search(first) is not None
    except Exception as e:
        log_with_sid("narrative", "warning",
                     "NARRATIVE_FIRST_SENTENCE_CHECK_FAILED "
                     f"error={log_safe_value(type(e).__name__, 80)}")
        return False


def first_sentence_na_slot(template, values, findings, *, locale=None, prefixes=None,
                           sid: str = "") -> tuple | None:
    """`(name, cause)` of the FIRST slot that renders "n/a" inside the first
    sentence (the `first_sentence_has_na` rule) because its key is missing
    ("missing"), its value is None ("none") or a non-finite number ("nan");
    None when no such slot is there — a clean first sentence, or "n/a" for
    another reason (a literal the template typed, a fmt overflow, an
    unsupported value type). Same sections / locale / passed prefixes as the
    render. `name` is a planner literal of the template grammar, never a
    value. A template that does not parse -> None. Never raises."""
    try:
        if not isinstance(template, str) or template_error_kind(template) is not None:
            return None
        marks: list = []
        text, _sections = render_with_sections(template, values, findings,
                                                sid=sid or "narrative", locale=locale,
                                                prefixes=prefixes, _marks=marks)
        if not marks or not isinstance(text, str):
            return None
        first = _first_sentence(text)
        m = _MARK_RE.search(first) if first is not None else None
        if m is None:
            return None
        index = int(m.group(1))
        return marks[index] if 0 <= index < len(marks) else None
    except Exception as e:
        log_with_sid(log_safe_value(sid or "narrative", 200), "warning",
                     "NARRATIVE_SLOT_CAUSE_FAILED "
                     f"error={log_safe_value(type(e).__name__, 80)}")
        return None


def invalid_error_msg(kind, slot=None) -> str:
    """The narrative-only retry's `error_msg` (PROTOCOL /v1/retry):
    `narrative_invalid: <kind>`, plus ` slot=<name> cause=<cause>` for a
    "first_sentence" kind whose `slot` is a `(name, cause)` of the template
    grammar (`[A-Za-z_][A-Za-z0-9_]{0,39}`) and one of SLOT_CAUSES. No
    quotes, so `brain_client._scrub_error_msg` leaves it unchanged."""
    msg = f"narrative_invalid: {kind}"
    if kind == "first_sentence" and isinstance(slot, tuple) and len(slot) == 2:
        name, cause = slot
        if isinstance(name, str) and _SLOT_NAME_RE.match(name) and cause in SLOT_CAUSES:
            msg += f" slot={name} cause={cause}"
    return msg


def _norm_ws(text: str) -> str:
    return _WS_RUN_RE.sub(" ", text).strip()


def _sentences(text: str) -> list:
    out = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        parts = _SENTENCE_SPLIT_RE.split(line)
        for i in range(0, len(parts), 2):
            piece = parts[i].strip()
            if piece:
                out.append(piece)
    return out


def covered_codes(text, sections) -> set:
    """The resolved codes of the shown sections (`render_with_sections`)
    that one of their body sentences still states in `text` — the text AFTER
    the quality gate, so a section whose every sentence was dropped covers
    nothing. Never raises."""
    covered: set = set()
    try:
        if not isinstance(text, str) or not isinstance(sections, (list, tuple)):
            return covered
        final = _norm_ws(text)
        for item in sections:
            if not (isinstance(item, tuple) and len(item) == 2):
                continue
            code, body = item
            if not isinstance(code, str) or not isinstance(body, str):
                continue
            for sentence in _sentences(body):
                if _NA_TOKEN_RE.search(sentence):
                    continue
                if _norm_ws(sentence) in final:
                    covered.add(code)
                    break
        return covered
    except Exception as e:
        log_with_sid("narrative", "warning",
                     f"NARRATIVE_COVERED_CHECK_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return covered


# ------------------------------------------------------- FINDING sentences
# A5.1: the fixed sentence of every finding no rendered section states.
# `{finding: {lang: (base, paren)}}` — `base` holds "{paren}" where the
# parenthesis goes; `paren` is its content, parts separated by "; ", each
# part kept only when every placeholder in it has a value (no part -> no
# parenthesis at all). Display text only (R0): never in `content`, never in
# a brain payload.

FINDING_SENTENCES = {
    "mixed_units": {
        "en": ("Amounts carry more than one currency or unit label, so they are reported "
               "per label and never added together.", ""),
        "uz": ("Summalar bir nechta valyuta yoki o'lchov birligida berilgan, shuning uchun "
               "ular har bir belgi bo'yicha alohida ko'rsatiladi va hech qachon bir-biriga "
               "qo'shilmaydi.", ""),
        "ru": ("Суммы указаны в нескольких валютах или единицах измерения, поэтому они "
               "приводятся отдельно по каждой из них и никогда не складываются.", ""),
        "ka": ("თანხები ერთზე მეტი ვალუტით ან ერთეულით არის მოცემული, ამიტომ ისინი "
               "თითოეული ნიშნულის მიხედვით ცალ-ცალკე არის ნაჩვენები და არასოდეს ჯამდება.",
               ""),
    },
    "coverage_change": {
        "en": ("The set of values in one dimension changes between periods{paren}, so totals "
               "are not comparable across periods.", "{dim}"),
        "uz": ("Bitta o'lchovdagi qiymatlar to'plami davrlar orasida o'zgaradi{paren}, "
               "shuning uchun jami ko'rsatkichlarni davrlar bo'yicha solishtirib bo'lmaydi.",
               "{dim}"),
        "ru": ("Набор значений одного измерения меняется от периода к периоду{paren}, "
               "поэтому итоги за разные периоды несопоставимы.", "{dim}"),
        "ka": ("ერთი განზომილების მნიშვნელობების ნაკრები პერიოდებს შორის იცვლება{paren}, "
               "ამიტომ ჯამები პერიოდებს შორის შეუდარებელია.", "{dim}"),
    },
    "thin_period": {
        # one entry for both gap causes (thin rows, or a collapsed value in a
        # pre-aggregated frame), so the wording names both
        "en": ("One period looks incomplete{paren}: it has far fewer rows or almost no value "
               "compared with the other periods, so it is treated as a data gap, not a "
               "business change.", "{period}"),
        "uz": ("Bir davr to'liq emasga o'xshaydi{paren}: unda boshqa davrlarga qaraganda "
               "qatorlar ancha kam yoki qiymat deyarli yo'q, shuning uchun u biznesdagi "
               "o'zgarish emas, balki ma'lumotlardagi bo'shliq deb hisoblanadi.", "{period}"),
        "ru": ("Один период выглядит неполным{paren}: в нём намного меньше строк или почти "
               "нет значения по сравнению с остальными периодами, поэтому он считается "
               "пробелом в данных, а не изменением в бизнесе.", "{period}"),
        "ka": ("ერთი პერიოდი არასრული ჩანს{paren}: მასში დანარჩენ პერიოდებთან შედარებით "
               "გაცილებით ნაკლები ჩანაწერია ან მნიშვნელობა თითქმის არ არის, ამიტომ ის "
               "მონაცემთა ხარვეზად ითვლება და არა ბიზნესის ცვლილებად.", "{period}"),
    },
    "unknown_dominated": {
        "en": ("Many rows have no value in one dimension{paren}, so that dimension cannot "
               "explain any change.", "{dim}; up to {share} Unknown"),
        "uz": ("Ko'p qatorlarda bitta o'lchov bo'yicha qiymat yo'q{paren}, shuning uchun bu "
               "o'lchov hech qanday o'zgarishni tushuntira olmaydi.",
               "{dim}; {share} gacha noma'lum"),
        "ru": ("Во многих строках у одного измерения нет значения{paren}, поэтому это "
               "измерение не может объяснить изменения.", "{dim}; до {share} неизвестных"),
        "ka": ("ბევრ ჩანაწერში ერთ განზომილებას მნიშვნელობა არ აქვს{paren}, ამიტომ ეს "
               "განზომილება ვერცერთ ცვლილებას ვერ ხსნის.", "{dim}; {share}-მდე უცნობი"),
    },
    "metric_definition_overlap": {
        "en": ("In most rows this metric equals another metric of the data{paren}, so it "
               "counts a stock, not new items.", "{share} of rows"),
        "uz": ("Ko'pchilik qatorlarda bu ko'rsatkich ma'lumotlardagi boshqa ko'rsatkichga "
               "teng{paren}, shuning uchun u yangi qo'shilganlarni emas, balki mavjud "
               "qoldiqni hisoblaydi.", "qatorlarning {share}"),
        "ru": ("В большинстве строк этот показатель совпадает с другим показателем "
               "данных{paren}, поэтому он отражает остаток, а не новые позиции.",
               "{share} строк"),
        "ka": ("ჩანაწერების უმეტესობაში ეს მაჩვენებელი მონაცემების სხვა მაჩვენებლის "
               "ტოლია{paren}, ამიტომ ის ნაშთს ასახავს და არა ახალ ერთეულებს.",
               "ჩანაწერების {share}"),
    },
    "latest_is_past_period": {
        "en": ("The data ends before the current month{paren}.", "latest period: {period}"),
        "uz": ("Ma'lumotlar joriy oydan oldin tugaydi{paren}.", "oxirgi davr: {period}"),
        "ru": ("Данные заканчиваются раньше текущего месяца{paren}.",
               "последний период: {period}"),
        "ka": ("მონაცემები მიმდინარე თვემდე მთავრდება{paren}.", "ბოლო პერიოდი: {period}"),
    },
}

# The order the sentences are added in, and the resolved section codes
# (`render_with_sections`) whose stated body already covers each finding.
_FINDING_ORDER = ("mixed_units", "coverage_change", "thin_period", "unknown_dominated",
                  "metric_definition_overlap", "latest_is_past_period")
_FINDING_COVERED_BY = {
    "mixed_units": frozenset({"mixed_units", "units"}),
    "coverage_change": frozenset({"coverage_change"}),
    "thin_period": frozenset({"thin_period", "data_gap"}),
    "unknown_dominated": frozenset({"unknown_dominated"}),
    "metric_definition_overlap": frozenset({"metric_definition_overlap", "metrics_identical",
                                            "metric_relation"}),
    "latest_is_past_period": frozenset({"latest_is_past_period"}),
}
_PLACEHOLDER_RE = re.compile(r"\{(dim|share|period)\}")
_VALUE_PREFIX_MAX = 10

# A READING GUIDE names a visual: the added paragraph goes before it.
_READING_GUIDE_RE = {
    "en": re.compile(r"\b(?:tables?|charts?|graphs?|bars?|waterfalls?)\b", re.IGNORECASE),
    "uz": re.compile(r"\b(?:jadval|grafik|diagramma)", re.IGNORECASE),
    "ru": re.compile(r"\b(?:таблиц|график|диаграмм)", re.IGNORECASE),
    "ka": re.compile(r"(?:ცხრილ|გრაფიკ|დიაგრამ)"),
}
_LAST_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])[ \t]+|\n+")


def _value_lookup(values: dict, name: str):
    """VALUES[name], else the first `<prefix>_<name>` key (a `pdc_diag`
    `prefix=`), else None."""
    if name in values:
        return values[name]
    pattern = re.compile(r"[a-z][a-z0-9]{0,%d}_%s\Z" % (_VALUE_PREFIX_MAX - 1, re.escape(name)))
    for key in values:
        if isinstance(key, str) and pattern.match(key):
            return values[key]
    return None


def _part(value, kind, locale) -> str | None:
    """One formatted parenthesis value, or None (missing, None or n/a)."""
    if value is None:
        return None
    text = _fmt_value(value, kind, locale)
    if not isinstance(text, str) or not text.strip() or _NA_TOKEN_RE.search(text):
        return None
    return text


def _finding_fires(finding: str, findings: dict) -> bool:
    if finding == "mixed_units":
        return findings.get("units") == "mixed_units"
    if finding == "coverage_change":
        return findings.get("coverage") == "coverage_change"
    if finding == "thin_period":
        return findings.get("thin_period") is True or findings.get("period_status") == "data_gap"
    if finding == "metric_definition_overlap":
        return findings.get("metric_relation") == "metric_definition_overlap"
    return findings.get(finding) is True


def _finding_parts(finding: str, findings: dict, values: dict, locale) -> dict:
    """The placeholder texts available for `finding`."""
    parts: dict = {}
    if finding == "coverage_change":
        dim = _part(_value_lookup(values, "coverage_dim_label"), "text", locale)
        if dim is None:
            dim = _part(findings.get("coverage_dim"), "text", locale)
        parts["dim"] = dim
    elif finding == "thin_period":
        period = _part(_value_lookup(values, "thin_period_label"), "text", locale)
        if period is None:
            period = _part(_value_lookup(values, "data_gap_period"), "text", locale)
        parts["period"] = period
    elif finding == "unknown_dominated":
        dim = _part(_value_lookup(values, "unknown_dim_label"), "text", locale)
        if dim is not None:
            parts["dim"] = dim
            parts["share"] = _part(_value_lookup(values, "unknown_share_max"), "pct", locale)
        else:
            # the max share belongs to coverage_check's dim, not this one
            parts["dim"] = _part(findings.get("unknown_dim"), "text", locale)
    elif finding == "metric_definition_overlap":
        parts["share"] = _part(_value_lookup(values, "overlap_equal_share"), "pct", locale)
    elif finding == "latest_is_past_period":
        parts["period"] = _part(_value_lookup(values, "latest_period"), "text", locale)
    return {k: v for k, v in parts.items() if v is not None}


def _finding_sentence(finding: str, lang: str, findings: dict, values: dict, locale) -> str:
    base, paren = FINDING_SENTENCES[finding][lang]
    available = _finding_parts(finding, findings, values, locale)
    kept = []
    for piece in paren.split("; ") if paren else []:
        names = _PLACEHOLDER_RE.findall(piece)
        if names and all(n in available for n in names):
            kept.append(_PLACEHOLDER_RE.sub(lambda m: available[m.group(1)], piece))
    inside = "; ".join(kept)
    return base.replace("{paren}", f" ({inside})" if inside else "")


def _place_paragraph(text: str, paragraph: str, lang: str) -> str:
    """`paragraph` as a new paragraph of `text`: before the last sentence
    when that sentence is a reading guide and not the only one, else at the
    end."""
    body = text.rstrip()
    if not body:
        return paragraph
    splits = list(_LAST_SENTENCE_SPLIT_RE.finditer(body))
    if splits:
        last = splits[-1]
        tail = body[last.end():]
        guide = _READING_GUIDE_RE.get(lang, _READING_GUIDE_RE["en"])
        if tail.strip() and guide.search(tail):
            head = body[:last.start()].rstrip()
            if head:
                return head + "\n\n" + paragraph + "\n\n" + tail.strip()
    return body + "\n\n" + paragraph


def add_finding_sentences(text, findings, values, covered, lang, *, sid: str) -> str:
    """`text` with one new paragraph holding the fixed FINDING_SENTENCES
    sentence of every fired finding no shown section covers (`covered` =
    `covered_codes`), in the table order, in `lang` (unknown -> en); the
    parenthesis values come from VALUES (exact key, else a prefixed one) or
    the findings, formatted in the language's locale, and a missing one
    drops its part — never "n/a". Nothing fires -> `text` unchanged. Logs
    `FINDING_SENTENCES_ADDED n= lang=` (a count). Never raises."""
    if not isinstance(text, str):
        text = ""
    try:
        finds = findings if isinstance(findings, dict) else {}
        vals = values if isinstance(values, dict) else {}
        cov = set(covered) if covered else set()
        code = lang if isinstance(lang, str) and lang in ("en", "uz", "ru", "ka") else "en"
        locale = locale_for(code)
        added = []
        for finding in _FINDING_ORDER:
            if not _finding_fires(finding, finds):
                continue
            if cov & _FINDING_COVERED_BY[finding]:
                continue
            added.append(_finding_sentence(finding, code, finds, vals, locale))
        if not added:
            return text
        out = _place_paragraph(text, " ".join(added), code)
        log_with_sid(sid, "info",
                     f"FINDING_SENTENCES_ADDED n={len(added)} lang={log_safe_value(code, 8)}")
        return out
    except Exception as e:
        log_with_sid(sid, "warning",
                     f"FINDING_SENTENCES_FAILED error={log_safe_value(type(e).__name__, 80)}")
        return text


# ---------------------------------------------------------- display line

def value_display(safe_preview, lang: str) -> str | None:
    """The local value line(s) of the summarize fallback (A6.4): a scalar ->
    "**Result:** v"; a flat dict -> up to 20 "- key: v" lines then "- …";
    anything else -> None. Display only (R0). Never raises."""
    try:
        label = _RESULT_LABEL.get(lang) if isinstance(lang, str) else None
        label = label or _RESULT_LABEL["en"]
        locale = locale_for(lang)
        if isinstance(safe_preview, (bool, int, float, str)):
            text = f"**{label}:** {_fmt_value(safe_preview, None, locale)}"
            log_with_sid("narrative", "info", "VALUE_DISPLAY_APPENDED kind=scalar")
            return _post(text, RENDERED_MAX)
        if isinstance(safe_preview, dict) and safe_preview:
            items = list(safe_preview.items())
            lines = [f"- {_md_escape(str(k)[:_VALUE_DISPLAY_KEY_MAX])}: {_fmt_value(v, None, locale)}"
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


def headline_sentence(values, lang, *, sid: str = "") -> str | None:
    """The local headline of a DROPPED narrative (A5.1, `NARRATIVE_INVALID_TWICE`):
    `"<Result label>: <amount>."` in `lang` (unknown -> en) over the first of
    VALUES "result", "total", then the keys ending "_total" (VALUES order)
    whose value is a finite int / float (never a bool, None, NaN / inf or a
    str). None when no key qualifies. Logs `NARRATIVE_HEADLINE_FALLBACK key=
    lang=` — the key, never the value. Display text only (R0): never in
    `content`, never in a brain payload. Never raises."""
    try:
        if not isinstance(values, dict):
            return None
        code = lang if isinstance(lang, str) and lang in _RESULT_LABEL else "en"
        keys = [k for k in ("result", "total") if k in values]
        keys += [k for k in values if isinstance(k, str) and k.endswith("_total")]
        for key in keys:
            value = values[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if isinstance(value, float) and not math.isfinite(value):
                continue
            shown = pdc_diag.fmt(value, "amount", locale_for(code))
            if not isinstance(shown, str) or not shown.strip() or _NA_TOKEN_RE.search(shown):
                continue
            log_with_sid(log_safe_value(sid or "narrative", 200), "info",
                         f"NARRATIVE_HEADLINE_FALLBACK key={log_safe_value(key, 40)} "
                         f"lang={log_safe_value(code, 8)}")
            return f"{_RESULT_LABEL[code]}: {shown}."
        return None
    except Exception as e:
        log_with_sid(log_safe_value(sid or "narrative", 200), "warning",
                     f"NARRATIVE_HEADLINE_FAILED error={log_safe_value(type(e).__name__, 80)}")
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
