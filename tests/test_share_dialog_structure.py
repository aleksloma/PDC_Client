"""The share dialog's Save button runs exactly one handler.

The dialog is shared by the sidebar's chat and conversation share. A second,
permanently bound handler used to post a CHAT share for whichever chat was
open at the time, so every sidebar share also shared the open chat with the
same recipients, without any notice. The only handler now is the one
`handleShare` assigns for the item the user actually chose.
"""
import re
from pathlib import Path

JS = Path(__file__).resolve().parent.parent / "static" / "dashboard.js"


def _src() -> str:
    return JS.read_text(encoding="utf-8")


def test_the_save_button_has_no_permanently_bound_click_handler():
    src = _src()
    assert not re.search(
        r"getElementById\(\s*['\"]btnSaveShare['\"]\s*\)\??\s*\.addEventListener", src)


def test_the_open_chat_share_side_effect_is_gone():
    src = _src()
    assert "saveShareSettings" not in src
    assert "openShareModal" not in src


def test_handle_share_assigns_the_only_save_handler():
    src = _src()
    body = src[src.index("async function handleShare("):]
    body = body[:body.index("\n}\n")]
    assert "btnSave.onclick = saveShare" in body
