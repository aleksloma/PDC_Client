"""Centralized logging setup writing both to file and console."""
import io
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from settings import settings

class ScannerFilter(logging.Filter):
    """Filter out scanner/bot requests from uvicorn access logs."""
    def filter(self, record):
        scanner_patterns = [
            "/wp-admin", "/wordpress", "/wp-content", 
            "/.env", "/phpmyadmin", "/.git", "/xmlrpc"
        ]
        msg = record.getMessage().lower()
        return not any(pattern in msg for pattern in scanner_patterns)


_RESET_LINK_PREFIX = "/auth/reset/"


class ResetLinkRedactor(logging.Filter):
    """Keep reset-link tokens out of uvicorn's access log.

    uvicorn logs `(client, method, path, http_version, status)`; when the
    path starts with `/auth/reset/`, everything after that prefix (the token
    and any query string) becomes `<redacted>`. Any other record, or a
    record of another shape, passes unchanged. Never drops a record and never
    raises."""
    def filter(self, record):
        try:
            args = record.args
            if isinstance(args, tuple) and len(args) == 5:
                path = args[2]
                if isinstance(path, str) and path.startswith(_RESET_LINK_PREFIX):
                    record.args = (args[0], args[1], _RESET_LINK_PREFIX + "<redacted>",
                                   args[3], args[4])
        except Exception as e:
            # Never break logging: the record goes out as it is, and the
            # failure is reported on the application log (not this logger).
            try:
                log_with_sid("logging", "warning",
                             f"ACCESS_LOG_REDACT_FAILED {type(e).__name__}")
            except Exception:
                pass
        return True

def _log_dir() -> Path:
    """The directory the log file lives in.

    `<DATA_ROOT>/logs` — the log used to be written inside the image
    (`<module dir>/logs` = `/app/logs`), which a read-only container rootfs
    cannot hold; the mounted data volume is the only writable persistent
    location. `settings.DATA_ROOT` and `__file__` are read at CALL time so
    tests (and a relocated install) can redirect either.

    Falls back to the old module-relative `logs/` when DATA_ROOT cannot be
    created or is not writable — that is the non-container dev run, never the
    container.
    """
    candidate = Path(settings.DATA_ROOT) / "logs"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        if os.access(str(candidate), os.W_OK):
            return candidate
        # Exists but is not writable — `mkdir(exist_ok=True)` raises nothing
        # here, so this branch has to announce itself too. It is the skipped-
        # chown upgrade case once the directory already exists.
        print(f"WARNING: log directory {candidate} exists but is not writable; "
              f"falling back to the module-relative logs directory")
    except OSError as e:
        # Any OSError subclass: a DATA_ROOT under a regular file raises
        # FileNotFoundError / FileExistsError / NotADirectoryError depending
        # on the platform, a read-only mount raises PermissionError. Say WHICH
        # path failed before falling back — in a container the next failure is
        # the read-only rootfs, and an operator reading `docker logs` would
        # otherwise see only that second error and go looking in the image
        # instead of at the data volume's ownership.
        print(f"WARNING: log directory {candidate} is unusable ({e}); "
              f"falling back to the module-relative logs directory")
    fallback = Path(__file__).parent / "logs"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _in_executor() -> bool:
    """True inside the analysis-sandbox image (`PDC_EXECUTOR=1`, set by
    executor/Dockerfile and passed to every runner). Read at call time."""
    return (os.environ.get("PDC_EXECUTOR") or "").strip().lower() in ("1", "true", "yes", "on")


def get_logger():
    """Return a configured logger writing to <DATA_ROOT>/logs/datachat.log and stdout.

    Inside the sandbox image the FILE handler is never added: every job runs
    as the uid that would own that file, so a later job — another user's —
    could open() it and read what earlier jobs logged. The sandbox logs to
    stdout only (Docker collects it); the web service keeps the durable copy
    of each job's outcome.
    """
    logger = logging.getLogger("datachat")
    if not logger.handlers and _in_executor():
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(fmt)
        logger.addHandler(ch)
        logger.setLevel(logging.INFO)
    if not logger.handlers:
        try:
            log_dir = _log_dir()
            log_path = log_dir / "datachat.log"
            
            # Rotating file handler: the log file is bounded, so it can never
            # fill the container's disk (LOG_MAX_BYTES / LOG_BACKUP_COUNT).
            fh = logging.handlers.RotatingFileHandler(
                str(log_path),
                maxBytes=settings.LOG_MAX_BYTES,
                backupCount=settings.LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
            fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
            fh.setFormatter(fmt)
            logger.addHandler(fh)
            logger.setLevel(logging.INFO)
            
            # Also log to console
            # NOTE: Do NOT use io.TextIOWrapper(sys.stdout.buffer) — it creates a
            # second wrapper sharing the same buffer, causing OSError on flush.
            # Use sys.stdout directly (reconfigured to UTF-8 in app.py).
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(fmt)
            logger.addHandler(ch)
            
            # Test write to verify logging works
            logger.info("Logger initialized successfully")
        except Exception as e:
            # Fallback to console only if file logging fails
            print(f"ERROR: Failed to initialize file logger: {e}")
            ch = logging.StreamHandler(sys.stdout)
            fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
            ch.setFormatter(fmt)
            logger.addHandler(ch)
            logger.setLevel(logging.INFO)
    
    # Apply scanner filter to uvicorn access logger
    uvicorn_access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, ScannerFilter) for f in uvicorn_access.filters):
        uvicorn_access.addFilter(ScannerFilter())
    if not any(isinstance(f, ResetLinkRedactor) for f in uvicorn_access.filters):
        uvicorn_access.addFilter(ResetLinkRedactor())
    
    return logger

def log_with_sid(sid_or_id: str, level: str, message: str, **context):
    """Log a message tagged with session/chat id and optional context.

    Example: log_with_sid(chat_id, 'info', 'CHAT_REQ', user='alice', route='/api/chat/...')
    """
    logger = get_logger()
    extra = "".join([f" [{k}={v}]" for k, v in context.items() if v is not None and v != ""])
    tag = f"[sid={sid_or_id}]" + extra
    line = f"{tag} {message}"
    if level == "error":
        logger.error(line)
    elif level == "warning":
        logger.warning(line)
    else:
        logger.info(line)


# ---------------------------------------------------------------------------
# Log-injection guard. The client log is NEWLINE-DELIMITED: a line break in a
# value a user or a file controls (a sheet name, a file name, a display name,
# a library's exception text quoting such a value) would start a forged
# record. `log_safe_value` renders such a value as ONE bounded line: every
# line break `str.splitlines` recognises, every other C0 / C1 control
# character and DEL become visible escapes (CR and LF keep their "\r" / "\n"
# renderings; a TAB passes). `exec_transport.log_safe_text` is this function
# (the name every older call site uses).
# ---------------------------------------------------------------------------
LOG_TEXT_MAX_CHARS = 2000

_LOG_ESCAPES = {c: "\\x%02x" % c for c in range(0x20) if c != 0x09}
_LOG_ESCAPES[0x7f] = "\\x7f"
_LOG_ESCAPES.update({c: "\\x%02x" % c for c in range(0x80, 0xa0)})
_LOG_ESCAPES.update({0x0d: "\\r", 0x0a: "\\n",
                     0x2028: "\\u2028", 0x2029: "\\u2029"})


def log_safe_value(value, max_chars: int = LOG_TEXT_MAX_CHARS, *,
                   tail: bool = False) -> str:
    """ONE line of an untrusted string, length-capped, for a log field.

    `tail=True` keeps the LAST `max_chars` (where an exception's own message
    sits); the default keeps the first. Escaping happens between the two
    cuts, so the field stays bounded by the cap although an escape widens a
    character. A non-string or empty value renders as "" (callers pass
    `str(x)` for a number they want shown). Never raises (Article IV).
    """
    try:
        if not isinstance(value, str) or not value:
            return ""
        limit = (max_chars if isinstance(max_chars, int) and max_chars > 0
                 else LOG_TEXT_MAX_CHARS)
        cut = value[-limit:] if tail else value[:limit]
        cut = cut.translate(_LOG_ESCAPES)
        return cut[-limit:] if tail else cut[:limit]
    except Exception:
        return ""
