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


def get_logger():
    """Return a configured logger writing to <DATA_ROOT>/logs/datachat.log and stdout."""
    logger = logging.getLogger("datachat")
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
