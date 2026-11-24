"""Built-in HTTPS: a TLS configuration that cannot be served stops the boot.

`TLS_CERT_FILE` and `TLS_KEY_FILE` both empty is plain HTTP, as it always was.
Both set to a certificate and its matching, passphrase-less key is HTTPS.
Everything else -- one of the two, a missing / unreadable / non-regular file,
a pair that does not match, a key behind a passphrase -- must refuse to start:
one error line, exit code 1, no key material in any log, never a prompt for a
passphrase and never a silent fall back to plain HTTP. The launcher
(`serve.tls_files_or_refuse`) checks before uvicorn runs, and the app lifespan
checks again so a start that bypasses the launcher refuses the same way -- and
so does a start with a VALID pair whose server was not told to serve TLS
(`TLS_NOT_SERVED`), which would otherwise answer in plain HTTP.
"""
import ast
import asyncio
import logging
import os
import ssl
import subprocess
import sys
import time
from pathlib import Path

import pytest

import app as app_mod
import serve
import settings as settings_module
from settings import settings
from tls_test_certs import (PASSPHRASE, assert_no_key_material, cert_pem, key_pem,
                            new_key, write_pair)

ROOT = Path(__file__).resolve().parent.parent
# Root ignores file modes and Windows has none to deny a read with.
CANNOT_DENY_READ = os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0)


def _configure(monkeypatch, cert="", key=""):
    monkeypatch.setattr(settings, "TLS_CERT_FILE", str(cert))
    monkeypatch.setattr(settings, "TLS_KEY_FILE", str(key))


def _refused(caplog, capfd=None) -> str:
    """Call the check, require the refusal, return everything it logged."""
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as exc:
            serve.tls_files_or_refuse()
    assert exc.value.code == 1
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "the refusal was not logged at error"
    text = caplog.text
    if capfd is not None:
        out = capfd.readouterr()
        text += out.out + out.err
    return text


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def test_both_settings_are_empty_by_default(monkeypatch):
    monkeypatch.delenv("TLS_CERT_FILE", raising=False)
    monkeypatch.delenv("TLS_KEY_FILE", raising=False)
    fresh = settings_module.Settings()
    assert (fresh.TLS_CERT_FILE, fresh.TLS_KEY_FILE) == ("", "")


def test_the_settings_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("TLS_CERT_FILE", "/certs/a.crt")
    monkeypatch.setenv("TLS_KEY_FILE", "/certs/a.key")
    fresh = settings_module.Settings()
    assert (fresh.TLS_CERT_FILE, fresh.TLS_KEY_FILE) == ("/certs/a.crt", "/certs/a.key")


# ---------------------------------------------------------------------------
# What is served
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cert,key", [("", ""), ("  ", ""), ("", "\t"), (" ", " ")])
def test_both_empty_is_plain_http(monkeypatch, cert, key):
    _configure(monkeypatch, cert, key)
    assert serve.tls_files_or_refuse() is None


@pytest.mark.parametrize("kind,traditional", [("ec", False), ("ec", True), ("rsa", False)])
def test_a_matching_pair_turns_tls_on(monkeypatch, tmp_path, caplog, kind, traditional):
    cert, key = write_pair(tmp_path, kind=kind, traditional=traditional)
    _configure(monkeypatch, cert, key)
    with caplog.at_level(logging.DEBUG):
        assert serve.tls_files_or_refuse() == (str(cert), str(key))
    assert_no_key_material(caplog.text, key.read_bytes())


def test_surrounding_whitespace_in_the_paths_is_ignored(monkeypatch, tmp_path):
    cert, key = write_pair(tmp_path)
    _configure(monkeypatch, f" {cert} ", f"{key}\n")
    assert serve.tls_files_or_refuse() == (str(cert), str(key))


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("blank", ["", "   "])
@pytest.mark.parametrize("which", ["cert", "key"])
def test_exactly_one_setting_is_refused(monkeypatch, tmp_path, caplog, which, blank):
    cert, key = write_pair(tmp_path)
    _configure(monkeypatch, cert if which == "cert" else blank,
               key if which == "key" else blank)
    text = _refused(caplog)
    assert "TLS_CONFIG_INCOMPLETE" in text
    assert_no_key_material(text, key.read_bytes())


@pytest.mark.parametrize("which", ["cert", "key"])
def test_a_missing_file_is_refused(monkeypatch, tmp_path, caplog, which):
    cert, key = write_pair(tmp_path)
    (cert if which == "cert" else key).unlink()
    _configure(monkeypatch, cert, key)
    text = _refused(caplog)
    assert "TLS_FILE_UNREADABLE" in text
    assert f"which={which}" in text


@pytest.mark.parametrize("which", ["cert", "key"])
def test_a_directory_in_place_of_a_file_is_refused(monkeypatch, tmp_path, caplog, which):
    """Docker creates a DIRECTORY when a bind-mounted file does not exist."""
    cert, key = write_pair(tmp_path)
    folder = tmp_path / "made-by-docker"
    folder.mkdir()
    _configure(monkeypatch, folder if which == "cert" else cert,
               folder if which == "key" else key)
    text = _refused(caplog)
    assert "TLS_FILE_UNREADABLE" in text
    assert f"which={which}" in text


@pytest.mark.skipif(CANNOT_DENY_READ, reason="a file mode cannot deny the read here")
@pytest.mark.parametrize("which", ["cert", "key"])
def test_an_unreadable_file_is_refused(monkeypatch, tmp_path, caplog, which):
    cert, key = write_pair(tmp_path)
    pem = key.read_bytes()
    target = cert if which == "cert" else key
    target.chmod(0)
    try:
        _configure(monkeypatch, cert, key)
        text = _refused(caplog)
    finally:
        target.chmod(0o600)
    assert "TLS_FILE_UNREADABLE" in text
    assert f"which={which}" in text
    assert_no_key_material(text, pem)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs on this platform")
def test_a_fifo_is_refused_without_blocking(monkeypatch, tmp_path, caplog):
    cert, _key = write_pair(tmp_path)
    fifo = tmp_path / "fifo.key"
    os.mkfifo(fifo)
    _configure(monkeypatch, cert, fifo)
    assert "TLS_FILE_UNREADABLE" in _refused(caplog)


@pytest.mark.parametrize("kinds", [("ec", "ec"), ("rsa", "rsa"), ("rsa", "ec")])
def test_a_pair_that_does_not_match_is_refused(monkeypatch, tmp_path, caplog, capfd, kinds):
    cert, _ = write_pair(tmp_path, kind=kinds[0], name="one")
    _, key = write_pair(tmp_path, kind=kinds[1], name="two")
    _configure(monkeypatch, cert, key)
    text = _refused(caplog, capfd)
    assert "TLS_PAIR_INVALID" in text
    assert_no_key_material(text, key.read_bytes())


@pytest.mark.parametrize("case", ["swapped", "cert_not_pem", "key_not_pem",
                                  "cert_empty", "key_empty", "key_is_cert"])
def test_files_that_are_not_a_pair_are_refused(monkeypatch, tmp_path, caplog, capfd, case):
    cert, key = write_pair(tmp_path)
    pem = key.read_bytes()
    if case == "swapped":
        cert, key = key, cert
    elif case == "cert_not_pem":
        cert.write_bytes(b"\x30\x82\x00\x00 not a pem")
    elif case == "key_not_pem":
        key.write_bytes(b"not a pem at all")
    elif case == "cert_empty":
        cert.write_bytes(b"")
    elif case == "key_empty":
        key.write_bytes(b"")
    else:
        key = cert
    _configure(monkeypatch, cert, key)
    text = _refused(caplog, capfd)
    assert "TLS_PAIR_INVALID" in text
    assert_no_key_material(text, pem)


@pytest.mark.parametrize("kind,traditional", [("ec", False), ("rsa", False), ("rsa", True)])
def test_a_passphrase_protected_key_is_refused(monkeypatch, tmp_path, caplog, capfd,
                                               kind, traditional):
    cert, key = write_pair(tmp_path, kind=kind, passphrase=PASSPHRASE,
                           traditional=traditional)
    _configure(monkeypatch, cert, key)
    started = time.monotonic()
    text = _refused(caplog, capfd)
    assert time.monotonic() - started < 30
    assert "TLS_PAIR_INVALID" in text
    assert "enter pem pass phrase" not in text.lower()
    assert_no_key_material(text, key.read_bytes())


def test_the_key_is_never_loaded_with_openssls_own_prompt(monkeypatch, tmp_path, caplog):
    """`load_cert_chain` without a password lets OpenSSL ask for one on the
    terminal, so every load must hand it an answer."""
    cert, key = write_pair(tmp_path, passphrase=PASSPHRASE)
    _configure(monkeypatch, cert, key)
    seen = []
    real = ssl.SSLContext.load_cert_chain

    def spy(self, certfile, keyfile=None, password=None):
        seen.append(password)
        return real(self, certfile, keyfile, password)

    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", spy)
    _refused(caplog)
    assert seen and all(p is not None for p in seen), seen


def test_the_logged_path_cannot_forge_a_log_record(monkeypatch, tmp_path, caplog):
    _cert, key = write_pair(tmp_path)
    _configure(monkeypatch, "/nope/x.crt\n2026-01-01 | INFO | FORGED", key)
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit):
            serve.tls_files_or_refuse()
    assert caplog.records
    for record in caplog.records:
        assert "\n" not in record.getMessage(), record.getMessage()


@pytest.mark.parametrize("form", ["multi_line", "escaped_newlines", "body_only"])
@pytest.mark.parametrize("which", ["cert", "key"])
def test_a_key_pasted_into_a_setting_is_not_logged(monkeypatch, tmp_path, caplog, capfd,
                                                   which, form):
    """The settings want a PATH; an operator who pastes the key itself into
    one of them is refused, and the key must not reach the log on the way."""
    cert, key = write_pair(tmp_path)
    pem = key.read_bytes()
    value = pem.decode()
    if form == "escaped_newlines":            # one line, as an env file holds it
        value = value.replace("\n", "\\n")
    elif form == "body_only":
        value = "".join(ln for ln in value.splitlines() if not ln.startswith("-----"))
    _configure(monkeypatch, value if which == "cert" else cert,
               value if which == "key" else key)
    text = _refused(caplog, capfd)
    assert_no_key_material(text, pem)


# ---------------------------------------------------------------------------
# The lifespan (a start that bypasses the launcher)
# ---------------------------------------------------------------------------
def test_the_tls_refusal_is_the_third_statement_of_the_lifespan():
    tree = ast.parse((ROOT / "app.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan")
    first = [ast.unparse(s) for s in fn.body[:3]]
    assert first[:2] == ["_refuse_on_weak_secret_key()", "_refuse_on_missing_executor_cidr()"]
    assert "_refuse_on_bad_tls_config()" in first[2], first


def _bad_settings(tmp_path, case):
    """(cert, key) for one bad configuration."""
    cert, key = write_pair(tmp_path)
    if case == "cert_only":
        return str(cert), ""
    if case == "key_only":
        return "", str(key)
    if case == "missing":
        return str(tmp_path / "nope.crt"), str(key)
    if case == "mismatch":
        key.write_bytes(key_pem(new_key()))
        return str(cert), str(key)
    if case == "encrypted":
        private = new_key()
        cert.write_bytes(cert_pem(private))
        key.write_bytes(key_pem(private, passphrase=PASSPHRASE))
        return str(cert), str(key)
    raise AssertionError(case)


CASES = {"cert_only": "TLS_CONFIG_INCOMPLETE", "key_only": "TLS_CONFIG_INCOMPLETE",
         "missing": "TLS_FILE_UNREADABLE", "mismatch": "TLS_PAIR_INVALID",
         "encrypted": "TLS_PAIR_INVALID"}


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_lifespan_refuses_before_any_startup_work(monkeypatch, tmp_path, case):
    cert, key = _bad_settings(tmp_path, case)
    _configure(monkeypatch, cert, key)
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path / "data"))
    called = []
    monkeypatch.setattr(app_mod.AuthStore, "ensure_local_admin",
                        lambda self: called.append("bootstrap"))

    async def enter():
        async with app_mod.lifespan(app_mod.app):
            called.append("serving")

    with pytest.raises(SystemExit) as exc:
        asyncio.run(enter())
    assert exc.value.code == 1
    assert called == []


def _env(tmp_path, cert, key) -> dict:
    env = dict(os.environ)
    env.update({"DATA_ROOT": str(tmp_path / "data"), "TLS_CERT_FILE": cert,
                "TLS_KEY_FILE": key, "SECRET_KEY": "a1b2c3d4" * 8,
                "EXECUTOR_NETWORK_CIDR": "192.168.255.240/28"})
    return env


def _run(args, env, timeout=180):
    return subprocess.run([sys.executable, *args], cwd=str(ROOT), env=env,
                          stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


LIFESPAN_BOOT = (
    "import asyncio, app\n"
    "async def main():\n"
    "    async with app.lifespan(app.app):\n"
    "        print('SERVING')\n"
    "asyncio.run(main())\n"
)


@pytest.mark.parametrize("case", ["cert_only", "mismatch", "encrypted"])
def test_a_lifespan_boot_in_a_fresh_process_exits_non_zero(tmp_path, case):
    cert, key = _bad_settings(tmp_path, case)
    proc = _run(["-c", LIFESPAN_BOOT], _env(tmp_path, cert, key))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out[-2000:]
    assert CASES[case] in out
    assert "CLIENT_STARTED" not in out and "SERVING" not in out
    if key:
        assert_no_key_material(out, Path(key).read_bytes())


def test_uvicorn_started_without_the_launcher_does_not_serve_plain_http(tmp_path):
    """The exact bypass: `uvicorn app:app` by hand on a TLS-configured install
    whose pair is bad must exit, not come up on plain HTTP."""
    cert, key = _bad_settings(tmp_path, "mismatch")
    proc = _run(["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "0"],
                _env(tmp_path, cert, key))
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-2000:]
    assert "TLS_PAIR_INVALID" in out
    assert "CLIENT_STARTED" not in out and "Uvicorn running on" not in out


# ---------------------------------------------------------------------------
# A VALID pair, but the server was not told to serve TLS
# ---------------------------------------------------------------------------
BYPASSES = {
    "no_options": ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"],
    "python_m": ["/usr/lib/python3/uvicorn/__main__.py", "app:app"],
    "cert_only": ["uvicorn", "app:app", "--ssl-certfile", "{crt}"],
    "key_only": ["uvicorn", "app:app", "--ssl-keyfile", "{key}"],
    "empty_value": ["uvicorn", "app:app", "--ssl-certfile", "{crt}", "--ssl-keyfile", ""],
    "ciphers_only": ["uvicorn", "app:app", "--ssl-ciphers", "ECDHE+AESGCM"],
    "embedded": ["-c"],
}


def _enter_lifespan(monkeypatch, tmp_path, calls):
    """Enter and leave the REAL lifespan with the slow steps stubbed."""
    import db_connector
    import db_scheduler
    import executor_client
    import http_redirect
    import plot_utils

    async def no_listener(*_a, **_k):
        calls.append("redirect_start")
        return None

    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(app_mod.AuthStore, "ensure_local_admin",
                        lambda self: calls.append("bootstrap"))
    monkeypatch.setattr(executor_client, "startup", lambda: None)
    monkeypatch.setattr(plot_utils, "ensure_plotly_js_asset", lambda: None)
    monkeypatch.setattr(db_connector, "ensure_oracle_thick_mode", lambda: False)
    monkeypatch.setattr(db_scheduler, "start", lambda: calls.append("scheduler_start"))
    monkeypatch.setattr(db_scheduler, "stop", lambda *a, **k: calls.append("scheduler_stop"))
    monkeypatch.setattr(http_redirect, "start", no_listener)

    async def enter():
        async with app_mod.lifespan(app_mod.app):
            calls.append("serving")

    asyncio.run(enter())


@pytest.mark.parametrize("env_options", [False, True], ids=["", "uvicorn-env"])
@pytest.mark.parametrize("bypass", sorted(BYPASSES))
def test_a_valid_pair_that_is_not_served_refuses(monkeypatch, tmp_path, caplog, capfd,
                                                 bypass, env_options):
    cert, key = write_pair(tmp_path, name="secretly-named")
    _configure(monkeypatch, cert, key)
    monkeypatch.setattr(sys, "argv", [a.replace("{crt}", str(cert)).replace("{key}", str(key))
                                      for a in BYPASSES[bypass]])
    if env_options:
        monkeypatch.setenv("UVICORN_SSL_CERTFILE", str(cert))
        monkeypatch.setenv("UVICORN_SSL_KEYFILE", str(key))
    calls = []
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit) as exc:
            _enter_lifespan(monkeypatch, tmp_path, calls)
    assert exc.value.code == 1
    assert calls == [], "nothing may start or be served"
    out = capfd.readouterr()
    text = caplog.text + out.out + out.err
    assert [r for r in caplog.records
            if r.levelno >= logging.ERROR and "TLS_NOT_SERVED" in r.getMessage()]
    assert "CLIENT_STARTED" not in text
    assert "secretly-named" not in text, "a configured value was logged"
    assert_no_key_material(text, key.read_bytes())


def test_a_bad_pair_keeps_its_own_refusal_under_the_launchers_argv(monkeypatch, tmp_path, caplog):
    """The argv never vouches for the files."""
    cert, key = _bad_settings(tmp_path, "mismatch")
    _configure(monkeypatch, cert, key)
    monkeypatch.setattr(sys, "argv", serve.build_argv((cert, key)))
    calls = []
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(SystemExit):
            _enter_lifespan(monkeypatch, tmp_path, calls)
    assert calls == []
    assert "TLS_PAIR_INVALID" in caplog.text


def test_a_valid_pair_under_the_launchers_argv_boots(monkeypatch, tmp_path, caplog):
    cert, key = write_pair(tmp_path)
    _configure(monkeypatch, cert, key)
    monkeypatch.setattr(sys, "argv", serve.build_argv((str(cert), str(key))))
    calls = []
    with caplog.at_level(logging.DEBUG):
        _enter_lifespan(monkeypatch, tmp_path, calls)
    assert calls == ["bootstrap", "scheduler_start", "redirect_start", "serving",
                     "scheduler_stop"]
    assert "TLS_NOT_SERVED" not in caplog.text


@pytest.mark.parametrize("argv", [["uvicorn", "app:app"], ["-c"], [],
                                  ["uvicorn", "app:app", "--ssl-certfile", "/x",
                                   "--ssl-keyfile", "/y"]])
def test_with_tls_off_the_command_line_does_not_matter(monkeypatch, tmp_path, caplog, argv):
    _configure(monkeypatch)
    monkeypatch.setattr(sys, "argv", argv)
    calls = []
    with caplog.at_level(logging.DEBUG):
        _enter_lifespan(monkeypatch, tmp_path, calls)
    assert calls == ["bootstrap", "scheduler_start", "serving", "scheduler_stop"]
    assert "TLS_" not in caplog.text


@pytest.mark.parametrize("env_options", [False, True], ids=["", "half-env"])
def test_uvicorn_by_hand_with_a_valid_pair_does_not_serve_plain_http(tmp_path, env_options):
    cert, key = write_pair(tmp_path, name="secretly-named")
    env = _env(tmp_path, str(cert), str(key))
    env.pop("UVICORN_SSL_CERTFILE", None)
    env.pop("UVICORN_SSL_KEYFILE", None)
    if env_options:
        # Empty is what an env file with the bare names yields.
        env.update({"UVICORN_SSL_CERTFILE": "", "UVICORN_SSL_KEYFILE": ""})
    proc = _run(["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "0"], env)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, out[-2000:]
    assert "TLS_NOT_SERVED" in out
    assert "CLIENT_STARTED" not in out and "Uvicorn running on" not in out
    assert "secretly-named" not in out
    assert_no_key_material(out, key.read_bytes())


def test_a_lifespan_boot_with_a_valid_pair_and_no_launcher_exits_one(tmp_path):
    cert, key = write_pair(tmp_path)
    proc = _run(["-c", LIFESPAN_BOOT], _env(tmp_path, str(cert), str(key)))
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out[-2000:]
    assert "TLS_NOT_SERVED" in out
    assert "CLIENT_STARTED" not in out and "SERVING" not in out


@pytest.mark.parametrize("case", sorted(CASES))
def test_the_launcher_process_exits_one_and_never_reaches_uvicorn(tmp_path, case):
    """`python serve.py` -- the image's CMD -- with a bad configuration."""
    cert, key = _bad_settings(tmp_path, case)
    proc = _run(["serve.py"], _env(tmp_path, cert, key), timeout=120)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 1, out[-2000:]
    assert CASES[case] in out
    assert "Uvicorn running" not in out and "Started server process" not in out
    if key:
        log = tmp_path / "data" / "logs" / "datachat.log"
        logged = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        assert_no_key_material(out + logged, Path(key).read_bytes())


@pytest.mark.skipif(os.name == "nt", reason="needs a pseudo-terminal")
def test_an_encrypted_key_never_prompts_on_a_terminal(tmp_path):
    """With a real controlling terminal OpenSSL WOULD ask for the passphrase;
    the launcher must exit instead of waiting for an answer."""
    import pty
    import select
    import signal

    cert, key = _bad_settings(tmp_path, "encrypted")
    env = _env(tmp_path, cert, key)
    pid, fd = pty.fork()
    if pid == 0:                                           # the child
        # Whatever goes wrong before the exec, the forked copy of pytest must
        # end here and never run on as a second test session.
        try:
            os.chdir(str(ROOT))
            os.execvpe(sys.executable, [sys.executable, "serve.py"], env)
        finally:
            os._exit(127)
    out, deadline, status = b"", time.monotonic() + 90, None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if not ready:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:                                # EIO: the child is gone
                chunk = b""
            if not chunk:
                _, status = os.waitpid(pid, 0)
                break
            out += chunk
    finally:
        if status is None:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(fd)
    text = out.decode("utf-8", "replace")
    assert status is not None, f"the launcher waited on the terminal: {text[-500:]}"
    assert os.waitstatus_to_exitcode(status) == 1, text[-500:]
    assert "Enter PEM pass phrase" not in text
    assert "TLS_PAIR_INVALID" in text
