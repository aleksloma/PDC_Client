"""Centralized logging setup writing both to file and console."""
import io
import logging
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

def get_logger():
    """Return a configured logger writing to logs/datachat.log and stdout."""
    logger = logging.getLogger("datachat")
    if not logger.handlers:
        try:
            # Use absolute path to logs directory
            log_dir = Path(__file__).parent / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "datachat.log"
            
            # Create file handler with explicit error handling
            fh = logging.FileHandler(str(log_path), encoding="utf-8", mode='a')
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
