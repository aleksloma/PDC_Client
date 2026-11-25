"""The answer of an MCP question, as MCP content items.

Built from the saved history record, with the DISPLAY text only (the text the
web page shows, in the order of static/dashboard.js `renderAiRecord`). The
record's `content` can hold a template with unfilled slots, so it is used only
when the record has no template.

For now the answer is its text; tables, chart images and the link follow.
"""
from __future__ import annotations

from typing import Any

import narrative

NO_TEXT = "The answer is in the conversation in PowerDataChat."
NOT_SAVED_TEXT = "This answer could not be saved in PowerDataChat."


def _filled(value: Any) -> bool:
    return isinstance(value, str) and value != ""


def _joined(parts: list) -> str:
    return "\n\n".join(p for p in parts if isinstance(p, str) and p.strip())


def _single_text(record: dict) -> Any:
    """The text of a record with at most one visual."""
    if _filled(record.get("caption_template")):
        return record.get("caption_rendered")
    if _filled(record.get("narrative_template")) or "narrative_rendered" in record:
        return record.get("caption_described")
    if isinstance(record.get("caption_rendered"), str):
        return record["caption_rendered"]
    return _joined([record.get("content"), record.get("value_display")])


def _text(record: dict) -> str:
    parts = [record.get("narrative_rendered") or record.get("headline_display")]
    images = record.get("images")
    if isinstance(images, list) and images:
        parts += [img.get("answer") for img in images if isinstance(img, dict)]
    else:
        parts.append(_single_text(record))
    captions = record.get("table_captions")
    if isinstance(captions, list):
        parts += captions
    phase2 = record.get("phase2")
    if isinstance(phase2, dict):
        parts.append(phase2.get("narrative_rendered"))
    return narrative.plain(_joined(parts))


def _text_item(text: str) -> dict:
    return {"type": "text", "text": text}


def build_content(record: dict, *, chat_id: str, conv_id: str, email: str,
                  sid: str, saved: bool = True) -> list[dict]:
    """The content items of a finished answer. Not `saved`: there is nothing
    to look up in PowerDataChat."""
    text = _text(record)
    if saved:
        return [_text_item(text or NO_TEXT)]
    return [_text_item(t) for t in (text, NOT_SAVED_TEXT) if t]


def error_content(text: str) -> list[dict]:
    """The content items of a failed one."""
    return [_text_item(text)]
