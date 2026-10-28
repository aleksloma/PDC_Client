"""The env templates document the client-wide brain timeout's real default.

`settings.BRAIN_REQUEST_TIMEOUT` defaults to 300 s (a chat turn chains several
brain calls); the two env templates must show that value commented out, and
no line may still name the old 120 / 180 defaults next to the variable.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ("client.env.example", ".env.example")
_LINE = re.compile(r"^#\s*BRAIN_REQUEST_TIMEOUT=300\s*$")


def _lines(name: str) -> list:
    return (ROOT / name).read_text(encoding="utf-8").splitlines()


def test_templates_show_the_300_default():
    for name in TEMPLATES:
        assert any(_LINE.match(line) for line in _lines(name)), name


def test_client_env_comment_names_default_300():
    lines = _lines("client.env.example")
    idx = next(i for i, line in enumerate(lines) if _LINE.match(line))
    assert idx > 0
    assert "default 300" in lines[idx - 1], lines[idx - 1]


def test_no_stale_120_or_180_default():
    for name in TEMPLATES:
        lines = _lines(name)
        for i, line in enumerate(lines):
            if "BRAIN_REQUEST_TIMEOUT" not in line:
                continue
            assert not re.search(r"\b(120|180)\b", line), (name, line)
        # The comment above the client template's line is part of its doc.
        if name == "client.env.example":
            idx = next(i for i, line in enumerate(lines) if "BRAIN_REQUEST_TIMEOUT" in line)
            assert not re.search(r"\b(120|180)\b", lines[idx - 1]), lines[idx - 1]
