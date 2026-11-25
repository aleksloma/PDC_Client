"""The answer of an MCP question, as MCP content items.

Built from the saved history record, with the DISPLAY text only (the text the
web page shows), in the order of static/dashboard.js `renderAiRecord`: the
narrative, the charts and the tables with their captions, then phase 2. The
record's `content` can hold templates with unfilled slots, so it is shown only
where the web shows it, and without the caption templates of its tables.

* a table is Markdown: its stored rows up to `MCP_TABLE_MAX_ROWS`, identifiers
  as plain digits, numbers as on the web (at most two decimals);
* a chart is a PNG: a stored PNG as it is, a Plotly chart through the report
  renderer, at most `MAX_IMAGES` per answer;
* the last item says where the conversation is: a link when PUBLIC_BASE_URL is
  set (never derived from the request), else the names of the chat and the
  conversation. An answer that was not saved says that instead.

Renders run one at a time, each on its own daemon thread, at most
`RENDER_TIMEOUT_S` from its start. A render past that is left behind and its
result is never used; while `MAX_STUCK_RENDERS` of them are alive no chart is
rendered.

The record comes from memory, before the JSON of the history file: a cell can
be a pandas / numpy value. Nothing of it is changed. A failing part costs that
part only, and the log gets the exception's type only.
"""
from __future__ import annotations

import base64
import decimal
import json
import re
import threading
from typing import Any, Callable

import pandas as pd

import ident_rule
import local_store
import narrative
import routes.auth as _auth
import routes.chat as _chat
import routes.report as _report
from logger_utils import log_safe_value, log_with_sid
from settings import settings

MAX_IMAGES = 6
RENDER_TIMEOUT_S = 60
MAX_STUCK_RENDERS = 3

GENERIC_TEXT = "Something went wrong while processing your request."
EMPTY_TEXT = "This answer has no text, table or chart."
NOT_SAVED_TEXT = "This answer could not be saved in PowerDataChat."
STOPPED_TEXT = "Response stopped by user."
CHART_NOTE = "A chart could not be included here as an image."
TABLE_NOTE = "A table could not be included here."

_CONV_ID_RE = re.compile(r"cv_[0-9a-f]{16}")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
# The line breaks and other control characters of a table cell.
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")
# What could end a cell, or start a tag, a code span, a link or an image in it.
_MARKDOWN_RE = re.compile(r"[\\|`<\[]")
_PARA = "\n\n"

_RENDER_LOCK = threading.Lock()             # one render at a time
_stuck: list[threading.Thread] = []         # under _RENDER_LOCK


def build_content(record: dict, *, chat_id: str, conv_id: str, email: str,
                  sid: str, saved: bool = True) -> list[dict]:
    """The content items of a finished answer. Not `saved`: there is nothing
    to look up in PowerDataChat."""
    link = _link(conv_id, sid) if saved else ""
    if link:
        pointer, last = " {} behind the link below.", f"Open in PowerDataChat: {link}"
    elif saved:
        pointer, last = " {} in PowerDataChat.", _place(chat_id, conv_id, email, sid)
    else:
        pointer, last = "", NOT_SAVED_TEXT

    items: list[dict] = []
    texts: list[str] = []       # since the last image

    def add_text(text: str) -> None:
        if text.strip():
            texts.append(text)

    def end_text() -> None:
        if texts:
            items.append(_text_item(_PARA.join(texts)))
            texts.clear()

    charts = 0
    for kind, value in _safely(sid, "record", [("text", GENERIC_TEXT)], _parts, record):
        if kind == "text":
            add_text(_safely(sid, "text", value, narrative.plain, value))
        elif kind == "table":
            add_text(_safely(sid, "table", TABLE_NOTE, _table, value, pointer, sid))
        else:
            charts += 1
            if charts > MAX_IMAGES:
                continue
            png = _safely(sid, "chart", None, _png, value, sid)
            if png is None:
                add_text(CHART_NOTE)
            else:
                end_text()
                items.append({"type": "image", "data": png, "mimeType": "image/png"})
    if charts > MAX_IMAGES:
        add_text(f"Only the first {MAX_IMAGES} of {charts} charts are included."
                 + pointer.format("The others are"))
    if not items and not texts:
        texts.append(EMPTY_TEXT)
    end_text()
    items.append(_text_item(last))
    return items


def error_content(text: str) -> list[dict]:
    """The content items of a failed one."""
    return [_text_item(text if _filled(text) else GENERIC_TEXT)]


def _safely(sid: str, part: str, default: Any, fn: Callable, *args) -> Any:
    """`fn(*args)`, or `default` when it raises."""
    try:
        return fn(*args)
    except Exception as e:
        # The type only: the message can quote a label or a value.
        log_with_sid(log_safe_value(sid, 254) or "mcp", "warning",
                     f"MCP_ANSWER_PART_FAILED part={log_safe_value(part, 20)} "
                     f"error={log_safe_value(type(e).__name__, 80)}")
        return default


def _text_item(text: str) -> dict:
    # A lone surrogate cannot be sent.
    return {"type": "text", "text": text.encode("utf-8", "replace").decode("utf-8")}


def _filled(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _at(values: list, i: int) -> Any:
    return values[i] if i < len(values) else None


# ---------------------------------------------------------------------------
# the record, in the order of the web page
# ---------------------------------------------------------------------------
def _parts(record: Any) -> list[tuple[str, Any]]:
    """The ("text" | "chart" | "table", value) parts of a record."""
    if not isinstance(record, dict):
        return [("text", GENERIC_TEXT)]
    images = _list(record.get("images"))
    out = [("text", record.get("narrative_rendered") or record.get("headline_display"))]
    if images:
        out += _chart_parts(images, 1)
    else:
        out += [("text", _single_text(record)), ("chart", record.get("image_base64")),
                ("table", record.get("table"))]
    out += _table_parts(record, 1)
    phase2 = record.get("phase2")
    if isinstance(phase2, dict):
        out.append(("text", phase2.get("narrative_rendered")))
    out += _chart_parts(images, 2) + _table_parts(record, 2)
    out = [(kind, value) for kind, value in out
           if (_filled(value) if kind == "text" else value)]
    # The web shows the stop in the record's text only, which two charts hide.
    if record.get("stopped") is True and not any(
            kind == "text" and STOPPED_TEXT in value for kind, value in out):
        out.append(("text", STOPPED_TEXT))
    return out


def _phase(value: Any) -> int:
    return 2 if value == 2 else 1


def _chart_parts(images: list, phase: int) -> list[tuple[str, Any]]:
    out: list = []
    for img in images:
        if isinstance(img, dict) and _phase(img.get("phase")) == phase:
            out += [("text", img.get("answer")), ("chart", img.get("image_base64"))]
    return out


def _table_parts(record: dict, phase: int) -> list[tuple[str, Any]]:
    captions, phases = _list(record.get("table_captions")), _list(record.get("table_phases"))
    out: list = []
    for i, table in enumerate(_list(record.get("tables"))):
        if _phase(_at(phases, i)) == phase:
            out += [("text", _at(captions, i)), ("table", table)]
    return out


def _single_text(record: dict) -> Any:
    """The text of a record with at most one chart. The PRESENCE of a template
    decides, never the truthiness of its rendering."""
    if _has_template(record, "caption_template"):
        return record.get("caption_rendered")
    if _has_template(record, "narrative_template") or "narrative_rendered" in record:
        return record.get("caption_described")
    if isinstance(record.get("caption_rendered"), str):
        return record["caption_rendered"]
    return _PARA.join(t for t in (_content(record), record.get("value_display"))
                      if _filled(t))


def _has_template(record: dict, key: str) -> bool:
    return isinstance(record.get(key), str) and record[key] != ""


def _content(record: dict) -> Any:
    """The record's content without the captions of its tables: a turn whose
    narrative was dropped joins the caption TEMPLATES into it, and the rendered
    captions stand above the tables."""
    content, captions = record.get("content"), _list(record.get("table_captions"))
    templates = _list(record.get("table_caption_templates"))
    if not isinstance(content, str) or not (captions or templates):
        return content
    text = _PARA + content + _PARA
    for i in range(len(_list(record.get("tables")))):
        template = _at(templates, i)
        piece = template if _filled(template) else _at(captions, i)
        if _filled(piece):
            text = text.replace(_PARA + piece + _PARA, _PARA, 1)
    return _PARA.join(p for p in text.split(_PARA) if p.strip() and "{{" not in p)


# ---------------------------------------------------------------------------
# tables
# ---------------------------------------------------------------------------
def _table(table: Any, pointer: str, sid: str) -> str:
    """A table as Markdown, "" for something that is none."""
    if not isinstance(table, dict):
        return ""
    columns, rows = table.get("columns"), table.get("rows")
    if not isinstance(columns, list) or not columns or not isinstance(rows, list):
        return ""
    labels = [str(c) for c in columns]
    listed = [c for c in _list(table.get("identifier_columns")) if isinstance(c, str)]
    idents = set(_safely(sid, "identifiers", (), ident_rule.identifier_columns, labels, listed))
    shown = rows[:settings.MCP_TABLE_MAX_ROWS]
    lines = [_row([_cell(label) for label in labels]), _row(["---"] * len(labels))]
    for row in shown:
        # The keys can still be the frame's own labels (a number, a date).
        cells = {str(k): v for k, v in row.items()} if isinstance(row, dict) else {}
        lines.append(_row([_cell(_value_text(cells.get(label), label in idents))
                           for label in labels]))
    parts = ["\n".join(lines)]
    title = table.get("title")
    if _filled(title):
        parts.insert(0, _cell(title))
    total = table.get("total_rows")
    if not isinstance(total, int) or total < len(rows):
        total = len(rows)
    if total > len(shown):
        parts.append(f"Showing {len(shown)} of {total} rows."
                     + pointer.format("The whole table is"))
    return _PARA.join(parts)


def _row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def _cell(text: str) -> str:
    return _MARKDOWN_RE.sub(r"\\\g<0>", _CONTROL_RE.sub(" ", text).strip())


def _value_text(value: Any, is_ident: bool) -> str:
    """A value as the web shows it."""
    if value is None or (pd.api.types.is_scalar(value) and pd.isna(value)):
        return ""
    if not isinstance(value, decimal.Decimal):
        value = local_store._json_safe(value)
    if value is None:
        return ""
    if is_ident:
        return ident_rule.plain_text(value) or ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, (float, decimal.Decimal)):
        number = f"{value:,.2f}".rstrip("0").rstrip(".")
        return "0" if number == "-0" else number
    return str(value)


# ---------------------------------------------------------------------------
# charts
# ---------------------------------------------------------------------------
def _png(image: Any, sid: str) -> str | None:
    """A chart as a base64 PNG; None when it cannot be had. Raises for text that
    is not base64."""
    if not isinstance(image, str):
        return None
    if _report._is_plotly_html(image):
        rendered = _render(image, sid)
        image = rendered[0] if isinstance(rendered, tuple) and rendered else None
        if not isinstance(image, str):
            return None
    raw = base64.b64decode(image, validate=True)
    return image if raw.startswith(_PNG_MAGIC) else None


def _render(image: str, sid: str) -> Any:
    """`routes.report._resolve_chart_image`, bounded in time; None when it
    failed or did not end."""
    result: list = []

    def work() -> None:
        try:
            result.append(_report._resolve_chart_image(image, sid))
        except BaseException as e:      # one that escaped would be printed
            log_with_sid(log_safe_value(sid, 254) or "mcp", "warning",
                         f"MCP_CHART_FAILED error={log_safe_value(type(e).__name__, 80)}")

    with _RENDER_LOCK:
        _stuck[:] = [t for t in _stuck if t.is_alive()]
        if len(_stuck) >= MAX_STUCK_RENDERS:
            log_with_sid(log_safe_value(sid, 254) or "mcp", "warning",
                         f"MCP_CHART_SKIPPED stuck={len(_stuck)}")
            return None
        thread = threading.Thread(target=work, name="mcp-chart", daemon=True)
        thread.start()
        thread.join(RENDER_TIMEOUT_S)
        if thread.is_alive():
            _stuck.append(thread)
            log_with_sid(log_safe_value(sid, 254) or "mcp", "warning",
                         f"MCP_CHART_TIMEOUT stuck={len(_stuck)}")
            return None
    return result[0] if result else None


# ---------------------------------------------------------------------------
# where the conversation is
# ---------------------------------------------------------------------------
def _link(conv_id: Any, sid: str) -> str:
    """The address of the conversation, "" without a public address."""
    if not isinstance(conv_id, str) or not _CONV_ID_RE.fullmatch(conv_id):
        return ""
    base = _safely(sid, "link", "", _auth._public_base)
    return f"{base}/c/{conv_id}" if base else ""


def _place(chat_id: Any, conv_id: Any, email: Any, sid: str) -> str:
    """Where to find the conversation when there is no link."""
    chat = (_safely(sid, "title", "", _own_title, email, chat_id)
            or _safely(sid, "title", "", _meta_title, chat_id) or "Chat")
    conversation = _safely(sid, "title", "", _conversation_title, email, chat_id, conv_id)
    if conversation:
        return (f'Open PowerDataChat, chat "{_cell(chat)}", '
                f'conversation "{_cell(conversation)}".')
    return f'Open PowerDataChat, chat "{_cell(chat)}".'


def _one_line(value: Any) -> str:
    return " ".join(value.split()) if isinstance(value, str) else ""


def _own_title(email: Any, chat_id: Any) -> str:
    """The user's own name for the chat."""
    return _one_line(local_store.AuthStore().active_chat_title(email, chat_id))


def _meta_title(chat_id: Any) -> str:
    """The title in the chat's meta file. Not through ChatDataStore, which
    would create the folder of a deleted chat."""
    if not isinstance(chat_id, str) or not _chat._CHAT_ID_RE.fullmatch(chat_id):
        return ""
    path = local_store._data_root() / "chatdata" / chat_id / "meta.json"
    if not path.exists():
        return ""
    meta = json.loads(path.read_text(encoding="utf-8"))
    return _one_line(meta.get("title")) if isinstance(meta, dict) else ""


def _conversation_title(email: Any, chat_id: Any, conv_id: Any) -> str:
    """The title of the conversation in the user's own index."""
    for row in local_store.AuthStore().list_conversations(email):
        if row.get("conv_id") == conv_id and row.get("chat_id") == chat_id:
            return _one_line(row.get("title"))
    return ""
