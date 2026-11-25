"""The MCP conversation of the day: one per acting user, per chat, per UTC
calendar day, titled "MCP YYYY-MM-DD" and registered in the user's own index,
so it is a normal conversation on the web.

Which conversation is today's is kept in `users/<email>/mcp_conversations.json`:
`{"version": 1, "chats": {<chat_id>: {"day": "YYYY-MM-DD", "conv_id": "cv_…"}}}`
— the newest day per chat. It is written last, atomically, under
`local_store._LOCK` and only while the account exists, so a removed account's
folder is never re-created; it disappears with the account.

The file is never trusted: the stored conversation must be in the caller's OWN
index for this chat and its file must exist under this chat. Anything else
(also an unreadable file) reads as "no conversation yet".

The steps of a new conversation (id, index row, welcome row) are the ones of
`routes/chat.py` `chat_stream`; only the title differs.

The chat access (`routes.chat._require_chat`) is checked by the caller.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import local_store
import routes.chat as _chat
from logger_utils import log_safe_value, log_with_sid

_DOC_NAME = "mcp_conversations.json"

# Looking today's conversation up and creating it is one step: two questions
# at once must not create two.
_CREATE_LOCK = threading.Lock()


def _now() -> datetime:
    """The clock of the calendar day (UTC); the tests replace it."""
    return datetime.now(timezone.utc)


def _today() -> str:
    return _now().date().isoformat()


def _path(email: str) -> Path:
    return local_store._data_root() / "users" / local_store._safe_email(email) / _DOC_NAME


def _read_chats(email: str) -> dict:
    """The stored {chat_id: entry} map; {} for a missing, unreadable or
    wrong-shape file."""
    try:
        try:
            raw = _path(email).read_bytes()
        except FileNotFoundError:
            return {}
        doc = json.loads(raw.decode("utf-8"))
    except Exception as e:
        # The type only: a decoder quotes the content it choked on.
        log_with_sid("mcp", "warning",
                     f"MCP_CONVERSATIONS_READ_FAILED "
                     f"error={log_safe_value(type(e).__name__, 80)}",
                     user=log_safe_value(email, 254))
        return {}
    chats = doc.get("chats") if isinstance(doc, dict) else None
    if not isinstance(chats, dict):
        log_with_sid("mcp", "warning", "MCP_CONVERSATIONS_READ_FAILED error=wrong_shape",
                     user=log_safe_value(email, 254))
        return {}
    return chats


def _lookup(email: str, chat_id: str, day: str) -> Optional[str]:
    entry = _read_chats(email).get(chat_id)
    if not isinstance(entry, dict) or entry.get("day") != day:
        return None
    conv_id = entry.get("conv_id")
    if not local_store.valid_conv_id(conv_id):
        return None
    if not _chat._conv_in_index(email, chat_id, conv_id):
        return None
    return conv_id if _chat._conv_of_chat(chat_id, conv_id) else None


def conversation_for_today(email: str, chat_id: str) -> Optional[str]:
    """Today's conversation, or None. Creates and writes nothing."""
    return _lookup(email, chat_id, _today())


def _remember(email: str, chat_id: str, day: str, conv_id: str) -> bool:
    """Write the mapping. False only when the account is gone (logged by the
    write guard). A write failure is logged and the conversation is used
    anyway; the next question then starts a new one."""
    with local_store._LOCK:
        if not local_store.AuthStore()._account_present(email, "mcp_conversation"):
            return False
        chats = {**_read_chats(email), chat_id: {"day": day, "conv_id": conv_id}}
        try:
            local_store._write_json_atomic(_path(email), {"version": 1, "chats": chats})
        except Exception as e:
            log_with_sid("mcp", "error",
                         f"MCP_CONVERSATIONS_WRITE_FAILED "
                         f"error={log_safe_value(type(e).__name__, 80)}",
                         user=log_safe_value(email, 254),
                         chat_id=log_safe_value(chat_id, 80))
    return True


def _create(email: str, chat_id: str, day: str, store) -> Optional[str]:
    # A removed account is refused before the conversation file exists.
    if not local_store.AuthStore()._account_present(email, "mcp_conversation"):
        return None
    title = f"MCP {day}"
    conv_id = store.new_conversation(title=title)
    if not local_store.AuthStore().record_conversation(email, chat_id, conv_id, title=title):
        return None
    welcome = store.get_welcome()
    if welcome:
        store.append_history(conv_id, {"role": "ai", "content": welcome, "ts": time.time()})
    return conv_id if _remember(email, chat_id, day, conv_id) else None


def make_factory(email: str, chat_id: str) -> Callable:
    """The `new_conversation` callable of `chat_turn.prepare_turn`: today's
    conversation of this user in this chat, created when there is none. None
    when it cannot be used (the account is gone, the store failed)."""
    def factory(store) -> Optional[str]:
        try:
            with _CREATE_LOCK:
                day = _today()
                return _lookup(email, chat_id, day) or _create(email, chat_id, day, store)
        except Exception as e:
            log_with_sid("mcp", "error",
                         f"MCP_CONVERSATION_FAILED "
                         f"error={log_safe_value(type(e).__name__, 80)}",
                         user=log_safe_value(email, 254),
                         chat_id=log_safe_value(chat_id, 80))
            return None
    return factory
