"""MCP stage 3 — the two settings.

* MCP_ANSWER_WAIT_SECONDS (float, default 20, 0–60): how long get_answer waits
  on the server before it answers "running".
* MCP_TABLE_MAX_ROWS (int, default 50, 1–50): the rows of a table in an answer.
  Saved answers hold only the first 50 rows, so more is never possible.

Both are read inside a Field default_factory: an unreadable value falls back
to the default and never raises, an out-of-range one is clamped (Article IV).

Every value is bound to a local first, so a failure never prints the whole
Settings object.
"""
import pytest

from settings import Settings, settings

WAIT = "MCP_ANSWER_WAIT_SECONDS"
ROWS = "MCP_TABLE_MAX_ROWS"


def _value(monkeypatch, name, raw):
    if raw is None:
        monkeypatch.delenv(name, raising=False)
    else:
        monkeypatch.setenv(name, raw)
    value = getattr(Settings(), name, "not defined")
    return value


def test_the_shipped_defaults(monkeypatch):
    wait, rows = _value(monkeypatch, WAIT, None), _value(monkeypatch, ROWS, None)
    assert wait == 20.0 and isinstance(wait, float)
    assert rows == 50 and type(rows) is int


def test_the_live_settings_object_has_both():
    wait, rows = getattr(settings, WAIT, None), getattr(settings, ROWS, None)
    assert isinstance(wait, float) and 0.0 <= wait <= 60.0
    assert type(rows) is int and 1 <= rows <= 50


@pytest.mark.parametrize("raw,expected", [
    ("0", 0.0), ("0.5", 0.5), ("20", 20.0), ("59.9", 59.9), ("60", 60.0), (" 12 ", 12.0),
    # clamped
    ("-1", 0.0), ("-0.001", 0.0), ("60.1", 60.0), ("3600", 60.0), ("1e9", 60.0),
    # unreadable
    ("", 20.0), ("   ", 20.0), ("20s", 20.0), ("abc", 20.0), ("nan", 20.0),
    ("inf", 20.0), ("-inf", 20.0), ("1,5", 20.0), ("None", 20.0),
])
def test_the_wait(monkeypatch, raw, expected):
    wait = _value(monkeypatch, WAIT, raw)
    assert wait == expected and isinstance(wait, float)


@pytest.mark.parametrize("raw,expected", [
    ("1", 1), ("10", 10), ("50", 50), (" 25 ", 25),
    # clamped
    ("0", 1), ("-5", 1), ("51", 50), ("1000000", 50),
    # unreadable
    ("", 50), ("   ", 50), ("ten", 50), ("10.5", 50), ("1e1", 50), ("None", 50),
])
def test_the_rows(monkeypatch, raw, expected):
    rows = _value(monkeypatch, ROWS, raw)
    assert rows == expected and type(rows) is int
