"""Launcher of the web container: starts uvicorn, with TLS when configured.

The image's CMD is `python serve.py`. With `TLS_CERT_FILE` and `TLS_KEY_FILE`
both empty (the default) the process becomes exactly the uvicorn command the
image always ran — plain HTTP on port 8000. With both set, the same command
gains `--ssl-certfile` / `--ssl-keyfile` / `--ssl-ciphers`, so port 8000
serves HTTPS.

A half-set pair, an unreadable file, a certificate and key that do not belong
together or a passphrase-protected key refuse the start before uvicorn runs.
`app.py`'s lifespan calls the same check, so a start that bypasses this
launcher refuses too.

The environment uvicorn gets is the one this process was STARTED with:
importing `settings` loads a `.env` file into `os.environ`, and uvicorn reads
its own configuration (FORWARDED_ALLOW_IPS, UVICORN_*) from the environment,
so `main()` puts the start environment back before it hands over. The
application then loads `.env` itself, as it always did.

Importing this module changes nothing (it only copies the environment).
"""
from __future__ import annotations

import os
import ssl
import stat

# Taken BEFORE the imports below, which load `.env` into os.environ.
_START_ENV = dict(os.environ)

from settings import settings
from logger_utils import log_safe_value, log_with_sid

# The command the image has always run. With TLS off nothing is added; with
# TLS on only the three --ssl-* options are. uvicorn keeps reading its proxy
# settings (--proxy-headers, FORWARDED_ALLOW_IPS) from its own defaults and
# the environment, in both modes.
_BASE_ARGV = ("uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000",
              "--workers", "1")

# uvicorn's own default cipher string is "TLSv1", which leaves TLS 1.2 with
# the CBC-SHA1 suites only — a current client offering just AEAD suites
# cannot connect at all. This string selects the forward-secret AEAD suites
# for TLS 1.2 (the TLS 1.3 suites are not governed by it and stay enabled).
TLS_CIPHERS = "ECDHE+AESGCM:ECDHE+CHACHA20"


def _refuse(message: str) -> None:
    log_with_sid("startup", "error", message)
    raise SystemExit(1)


def _no_passphrase(*_args, **_kwargs):
    """Password callback for `load_cert_chain`: an encrypted key must refuse
    the start, never wait for a passphrase on a terminal nobody is at."""
    raise ValueError("passphrase-protected key")


def _check_readable(path: str, which: str, setting: str) -> None:
    """Refuse unless `path` is a regular file this process can open. The
    configured value is never logged (it may be pasted key material)."""
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            raise IsADirectoryError("not a regular file")
        with open(path, "rb") as fh:
            fh.read(1)
    except Exception as e:
        _refuse(f"TLS_FILE_UNREADABLE refusing to start: which={which} "
                f"error={log_safe_value(type(e).__name__, 80)} "
                f"setting={setting} (the file it names must exist inside "
                "the container and be readable by the application user)")


def tls_files_or_refuse() -> tuple[str, str] | None:
    """The configured (certificate, key) paths, or None when TLS is off.

    Raises `SystemExit(1)` after one error log line when the configuration
    cannot be served: `TLS_CONFIG_INCOMPLETE` (exactly one of the two
    settings), `TLS_FILE_UNREADABLE which=cert|key` (missing, not a regular
    file, not openable), `TLS_PAIR_INVALID` (not PEM, key does not match the
    certificate, key needs a passphrase). Neither file content nor the
    configured value is ever logged — exception types and the setting NAME
    only.
    """
    cert = str(getattr(settings, "TLS_CERT_FILE", "") or "").strip()
    key = str(getattr(settings, "TLS_KEY_FILE", "") or "").strip()
    if not cert and not key:
        return None
    if not cert or not key:
        _refuse("TLS_CONFIG_INCOMPLETE refusing to start: set both "
                "TLS_CERT_FILE and TLS_KEY_FILE for built-in HTTPS, or neither "
                f"(missing={'TLS_CERT_FILE' if not cert else 'TLS_KEY_FILE'})")
    _check_readable(cert, "cert", "TLS_CERT_FILE")
    _check_readable(key, "key", "TLS_KEY_FILE")
    try:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key, password=_no_passphrase)
        ctx.set_ciphers(TLS_CIPHERS)
    except Exception as e:
        _refuse("TLS_PAIR_INVALID refusing to start: "
                f"error={log_safe_value(type(e).__name__, 80)} (TLS_CERT_FILE "
                "and TLS_KEY_FILE must be a PEM certificate and its matching "
                "private key, without a passphrase)")
    return cert, key


def build_argv(tls: tuple[str, str] | None) -> list[str]:
    """The uvicorn command line: the fixed base and, with TLS on, the
    certificate, key and cipher options."""
    argv = list(_BASE_ARGV)
    if tls:
        cert, key = tls
        argv += ["--ssl-certfile", cert, "--ssl-keyfile", key,
                 "--ssl-ciphers", TLS_CIPHERS]
    return argv


def _option_given(argv, option: str) -> bool:
    """True when `option` has a value; its LAST occurrence decides, as in
    uvicorn's own command line."""
    args = [a for a in (argv or []) if isinstance(a, str)]
    value = ""
    for i, arg in enumerate(args):
        if arg == option:
            value = args[i + 1] if i + 1 < len(args) else ""
        elif arg.startswith(option + "="):
            value = arg[len(option) + 1:]
    return bool(value)


def tls_is_served(argv) -> bool:
    """True when uvicorn was told to serve TLS: BOTH the certificate and the
    key were given in `argv`, each as `--ssl-certfile X` / `--ssl-certfile=X`
    (`--ssl-keyfile` likewise) — what this launcher always passes. Environment
    variables do not count: `.env` loading can add `UVICORN_SSL_*` after
    uvicorn read its own. Never raises."""
    try:
        return (_option_given(argv, "--ssl-certfile")
                and _option_given(argv, "--ssl-keyfile"))
    except Exception:
        return False


def main() -> None:
    """Check the TLS settings, then REPLACE this process with uvicorn.

    Right before the hand-over the environment is put back to exactly what
    this process was started with: whatever the imports added or changed
    (the `.env` file, a library's own variables) is removed, so uvicorn
    reads the same environment as when the image ran it directly.
    """
    argv = build_argv(tls_files_or_refuse())
    os.environ.clear()
    os.environ.update(_START_ENV)
    os.execvp("uvicorn", argv)


if __name__ == "__main__":
    main()
