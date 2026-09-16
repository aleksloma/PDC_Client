"""C3 — log-rotation settings must be tolerant, plus a REAL rollover test.

`settings.py` parsed LOG_MAX_BYTES / LOG_BACKUP_COUNT with a bare `int()`:

  * `LOG_MAX_BYTES=50MB` (the obvious thing an operator types) raises
    ValueError inside the Field default_factory, so `import settings` blows
    up — the container crash-loops with nothing in the log it just failed to
    configure;
  * `LOG_MAX_BYTES=0` parses fine and silently turns rotation OFF
    (RotatingFileHandler treats maxBytes=0 as "never roll over"), which is
    exactly the unbounded-log finding the rotation fix closed.

Wanted: parse failures fall back to the default, and the values are clamped
to a sane minimum so rotation can never be switched off from the environment.
Valid values pass through unchanged and the defaults do not move.

Every assertion binds the value to a LOCAL first: pytest's assertion rewrite
would otherwise print the whole `Settings` repr (SECRET_KEY included) on
failure — same rule as tests/test_upload_filename_sanitize.py.
"""
import logging
import logging.handlers
from pathlib import Path

import pytest

from settings import Settings, settings

DEFAULT_MAX_BYTES = 52428800          # 50 MiB
DEFAULT_BACKUP_COUNT = 5
MIN_MAX_BYTES = 1048576               # 1 MiB — rotation must stay on
MIN_BACKUP_COUNT = 1


def _fresh(monkeypatch, **env) -> Settings:
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return Settings()


# ---------------------------------------------------------------------------
# Defaults (already true — the pin)
# ---------------------------------------------------------------------------
def test_defaults_unchanged_when_env_is_absent(monkeypatch):
    cfg = _fresh(monkeypatch, LOG_MAX_BYTES=None, LOG_BACKUP_COUNT=None)
    max_bytes = cfg.LOG_MAX_BYTES
    backup_count = cfg.LOG_BACKUP_COUNT
    assert max_bytes == DEFAULT_MAX_BYTES
    assert backup_count == DEFAULT_BACKUP_COUNT


@pytest.mark.parametrize("raw,expected", [
    ("1048576", 1048576),
    ("10485760", 10485760),
    ("104857600", 104857600),
    (" 2097152 ", 2097152),           # surrounding whitespace is not a failure
])
def test_valid_max_bytes_passes_through(monkeypatch, raw, expected):
    value = _fresh(monkeypatch, LOG_MAX_BYTES=raw).LOG_MAX_BYTES
    assert value == expected


@pytest.mark.parametrize("raw,expected", [("1", 1), ("3", 3), ("20", 20)])
def test_valid_backup_count_passes_through(monkeypatch, raw, expected):
    value = _fresh(monkeypatch, LOG_BACKUP_COUNT=raw).LOG_BACKUP_COUNT
    assert value == expected


# ---------------------------------------------------------------------------
# Unparsable input must not crash the import
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["50MB", "50 MB", "", "   ", "abc", "1e6", "1_000_000", "5.5"])
def test_unparsable_max_bytes_falls_back_to_the_default(monkeypatch, raw):
    cfg = _fresh(monkeypatch, LOG_MAX_BYTES=raw)   # must not raise
    value = cfg.LOG_MAX_BYTES
    assert value == DEFAULT_MAX_BYTES


@pytest.mark.parametrize("raw", ["five", "", "3.5", "5x"])
def test_unparsable_backup_count_falls_back_to_the_default(monkeypatch, raw):
    cfg = _fresh(monkeypatch, LOG_BACKUP_COUNT=raw)   # must not raise
    value = cfg.LOG_BACKUP_COUNT
    assert value == DEFAULT_BACKUP_COUNT


# ---------------------------------------------------------------------------
# Rotation can never be switched off from the environment
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["0", "-1", "1", "1024", "1048575"])
def test_max_bytes_is_clamped_to_the_minimum(monkeypatch, raw):
    value = _fresh(monkeypatch, LOG_MAX_BYTES=raw).LOG_MAX_BYTES
    assert value >= MIN_MAX_BYTES


@pytest.mark.parametrize("raw", ["0", "-2"])
def test_backup_count_is_clamped_to_the_minimum(monkeypatch, raw):
    value = _fresh(monkeypatch, LOG_BACKUP_COUNT=raw).LOG_BACKUP_COUNT
    assert value >= MIN_BACKUP_COUNT


# ---------------------------------------------------------------------------
# T-rotation-real — the handler actually rolls over and keeps the cap
# ---------------------------------------------------------------------------
@pytest.fixture
def clean_datachat_logger():
    """`get_logger()` configures the process-wide "datachat" logger once and
    keeps its handlers forever — take them off, restore them afterwards so the
    rest of the session logs exactly as before."""
    logger = logging.getLogger("datachat")
    saved_handlers = list(logger.handlers)
    saved_level = logger.level
    logger.handlers = []
    yield logger
    for h in list(logger.handlers):
        if h not in saved_handlers:
            h.close()
    logger.handlers = saved_handlers
    logger.setLevel(saved_level)


def test_get_logger_rotates_and_keeps_only_backup_count_backups(
        tmp_path, monkeypatch, clean_datachat_logger):
    """Drive the REAL `logger_utils.get_logger()` at a tiny maxBytes.

    The log directory is `Path(settings.DATA_ROOT) / "logs"` (Task 2 — the
    log lives on the data volume, not in the image), so redirecting it is a
    matter of pointing DATA_ROOT at tmp_path (monkeypatch restores it).
    """
    import logger_utils
    monkeypatch.setattr(logger_utils.settings, "DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(logger_utils.settings, "LOG_MAX_BYTES", 2048, raising=False)
    monkeypatch.setattr(logger_utils.settings, "LOG_BACKUP_COUNT", 2, raising=False)

    lg = logger_utils.get_logger()
    log_dir = tmp_path / "logs"
    assert (log_dir / "datachat.log").is_file(), sorted(p.name for p in log_dir.iterdir())
    rotating = [h for h in lg.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(rotating) == 1
    assert (rotating[0].maxBytes, rotating[0].backupCount) == (2048, 2)

    for i in range(200):
        logger_utils.log_with_sid("s_rot", "info", f"ROTATION_PROBE {i} " + "x" * 200)
    for h in lg.handlers:
        h.flush()

    names = sorted(p.name for p in log_dir.iterdir())
    assert "datachat.log.1" in names, names                 # a rollover happened
    assert "datachat.log.3" not in names, names             # backupCount honored
    assert names == ["datachat.log", "datachat.log.1", "datachat.log.2"], names
    assert (log_dir / "datachat.log").stat().st_size <= 2048 + 4096   # bounded
    assert Path(rotating[0].baseFilename).parent == log_dir           # tmp_path, not repo logs/
