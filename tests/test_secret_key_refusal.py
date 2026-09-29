"""The web app refuses to start without a real SECRET_KEY.

SECRET_KEY signs the session cookie, and the session's `sid` names a folder
under DATA_ROOT: with a known key anyone can sign any session. So an empty
value, the historical placeholder `replace-me-in-prod`, or anything shorter
than 32 characters stops the boot (`app._refuse_on_weak_secret_key`, called
first in the lifespan). The value itself never reaches the log.
"""
import ast
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app as app_mod
import settings as settings_module
from settings import settings

ROOT = Path(__file__).resolve().parent.parent
STRONG = "a1b2c3d4" * 8


@pytest.mark.parametrize("value", ["", "   ", "replace-me-in-prod", "short", "x" * 31])
def test_weak_key_is_refused(monkeypatch, caplog, value):
    monkeypatch.setattr(settings, "SECRET_KEY", value)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc:
            app_mod._refuse_on_weak_secret_key()
    assert exc.value.code == 1
    text = caplog.text
    assert "SECRET_KEY_UNSET" in text
    if value.strip():
        assert value not in text.replace("replace-me-in-prod-", ""), "the key value was logged"


def test_a_32_char_key_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", "k" * 32)
    app_mod._refuse_on_weak_secret_key()      # no raise


def test_a_generated_key_is_accepted(monkeypatch):
    monkeypatch.setattr(settings, "SECRET_KEY", STRONG)
    app_mod._refuse_on_weak_secret_key()


def test_the_lifespan_refuses_before_anything_else(monkeypatch):
    """Entering the lifespan with the placeholder raises SystemExit before any
    startup work (no CLIENT_STARTED, no admin bootstrap)."""
    monkeypatch.setattr(settings, "SECRET_KEY", settings_module.SECRET_KEY_PLACEHOLDER)
    called = []
    monkeypatch.setattr(app_mod.AuthStore, "ensure_local_admin",
                        lambda self: called.append("bootstrap"))

    async def enter():
        async with app_mod.lifespan(app_mod.app):
            pass

    with pytest.raises(SystemExit):
        asyncio.run(enter())
    assert called == []


def test_the_refusals_are_the_first_statements_of_the_lifespan():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    first = [ast.unparse(s) for s in fn.body[:2]]
    assert first == ["_refuse_on_weak_secret_key()", "_refuse_on_missing_executor_cidr()"], first


def test_there_is_no_usable_default_in_the_env_examples():
    for name in (".env.example", "client.env.example", "client.local.env.example"):
        lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
        values = [ln.split("=", 1)[1].strip() for ln in lines
                  if ln.startswith("SECRET_KEY=")]
        assert values == [""], (name, values)


def _boot(env_overrides: dict) -> subprocess.CompletedProcess:
    """Run the real lifespan in a fresh interpreter; return the process."""
    env = dict(os.environ)
    env.update(env_overrides)
    code = (
        "import asyncio, app\n"
        "async def main():\n"
        "    async with app.lifespan(app.app):\n"
        "        pass\n"
        "asyncio.run(main())\n"
    )
    return subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("value", ["", "replace-me-in-prod", "too-short"])
def test_a_boot_with_a_weak_key_exits_non_zero(tmp_path, value):
    proc = _boot({"SECRET_KEY": value, "DATA_ROOT": str(tmp_path),
                  "EXECUTOR_NETWORK_CIDR": "192.168.255.240/28"})
    assert proc.returncode != 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert "SECRET_KEY_UNSET" in proc.stdout + proc.stderr
    assert "CLIENT_STARTED" not in proc.stdout + proc.stderr
