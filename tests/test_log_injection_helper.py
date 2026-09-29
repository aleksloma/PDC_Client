"""The log-injection guard: `logger_utils.log_safe_value` and a call site.

The client log is newline-delimited, so a value a user or a file controls (a
workbook's sheet name ends up in a data-frame key; a file name; a display
name) must reach a log line as ONE line. The helper escapes line breaks and
the other control characters; `exec_transport.log_safe_text` is the same
function under its older name. The structural guard
(`tests/test_executor_infra_short_circuit.py`) now also covers
routes/upload.py, local_store.py and routes/sso.py.
"""
import asyncio
import logging

import pandas as pd
import pytest

import exec_transport
import local_store
import logger_utils
from settings import settings


@pytest.mark.parametrize("raw, expected", [
    ("a\nb", "a\\nb"),
    ("a\rb", "a\\rb"),
    ("a\x00b", "a\\x00b"),
    ("a\x1bb", "a\\x1bb"),
    ("a\x7fb", "a\\x7fb"),
    ("a\x85b", "a\\x85b"),
    ("a b", "a\\u2028b"),
    ("a\tb", "a\tb"),
    ("Sheet 1", "Sheet 1"),
    ("ლარი", "ლარი"),
])
def test_control_characters_become_visible_escapes(raw, expected):
    assert logger_utils.log_safe_value(raw) == expected


def test_the_value_is_capped_and_never_raises():
    assert logger_utils.log_safe_value("x" * 50, 10) == "x" * 10
    assert logger_utils.log_safe_value("abcdef", 3, tail=True) == "def"
    assert logger_utils.log_safe_value(None) == ""
    assert logger_utils.log_safe_value(12) == ""


def test_the_transport_name_is_the_same_helper():
    for raw in ("a\nb", "a\x00b", "a b"):
        assert exec_transport.log_safe_text(raw) == logger_utils.log_safe_value(raw)


def test_a_sheet_name_with_a_newline_yields_one_upload_log_line(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    import routes.upload as upload_mod

    store = local_store.UserStore("s_" + "a" * 16)
    (store.files_dir / "book.xlsx").write_bytes(b"x")
    forged = "book.xlsx::Q1\n2026-09-29 | INFO | [sid=admin] ADMIN_GRANTED_ALL"
    frame = pd.DataFrame({"a": [1]})
    monkeypatch.setattr(store, "load_dataframes_with_report",
                        lambda: ({forged: frame}, []))
    with caplog.at_level(logging.INFO, logger="datachat"):
        asyncio.run(upload_mod._finish_upload(store, "u@x.com", store.sid,
                                              ["book.xlsx"], {}))
    lines = [r.getMessage() for r in caplog.records if "UPLOAD_OK" in r.getMessage()]
    assert lines, [r.getMessage() for r in caplog.records]
    for line in lines:
        assert "\n" not in line
        assert "Q1\\n2026-09-29" in line
