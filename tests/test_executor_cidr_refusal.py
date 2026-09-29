"""The web app refuses to start when the sandbox is configured but its subnet
is not.

`BackendNetworkGuard` refuses any request arriving from the analysis
sandbox's subnet; with the subnet unknown it would stand down silently. So
while EXECUTOR_URL is set, an empty or malformed EXECUTOR_NETWORK_CIDR stops
the boot (`app._refuse_on_missing_executor_cidr`). Only EXECUTOR_URL=""
(no sandbox at all) may leave it empty. The value is never logged.
"""
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import app as app_mod
from settings import settings

from tests.test_backend_network_guard import BAD_CIDRS

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("value", [""] + BAD_CIDRS)
def test_an_empty_or_malformed_cidr_is_refused(monkeypatch, caplog, value):
    monkeypatch.setattr(settings, "EXECUTOR_URL", "http://pdc-executor:8090")
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", value)
    with caplog.at_level(logging.ERROR):
        with pytest.raises(SystemExit) as exc:
            app_mod._refuse_on_missing_executor_cidr()
    assert exc.value.code == 1
    assert "EXECUTOR_CIDR_UNSET" in caplog.text
    if value.strip():
        assert value not in caplog.text, "the configured value was logged"


@pytest.mark.parametrize("value", ["192.168.255.240/28", "127.0.0.0/8", "fd00::/64",
                                   "10.1.2.3/24"])
def test_a_valid_cidr_is_accepted(monkeypatch, value):
    monkeypatch.setattr(settings, "EXECUTOR_URL", "http://pdc-executor:8090")
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", value)
    app_mod._refuse_on_missing_executor_cidr()


def test_no_sandbox_means_no_cidr_is_needed(monkeypatch):
    monkeypatch.setattr(settings, "EXECUTOR_URL", "")
    monkeypatch.setattr(settings, "EXECUTOR_NETWORK_CIDR", "")
    app_mod._refuse_on_missing_executor_cidr()


def test_a_boot_without_the_cidr_exits_non_zero(tmp_path):
    env = dict(os.environ)
    env.update({"DATA_ROOT": str(tmp_path), "EXECUTOR_NETWORK_CIDR": "",
                "EXECUTOR_URL": "http://pdc-executor:8090",
                "SECRET_KEY": "a1b2c3d4" * 8})
    code = ("import asyncio, app\n"
            "async def main():\n"
            "    async with app.lifespan(app.app):\n"
            "        pass\n"
            "asyncio.run(main())\n")
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(ROOT), env=env,
                          capture_output=True, text=True, timeout=180)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-3000:]
    assert "EXECUTOR_CIDR_UNSET" in out
    assert "CLIENT_STARTED" not in out
