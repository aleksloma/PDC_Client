"""Task 2 — the log file lives under DATA_ROOT, never inside the image.

`logger_utils.get_logger()` used to build its directory from
`Path(__file__).parent / "logs"`, i.e. `/app/logs` in the container — a write
into the image's own filesystem, which is exactly what a read-only rootfs
forbids (and why the local compose needed a second volume just for the log).
Wanted: `Path(settings.DATA_ROOT) / "logs"` (created with parents=True,
exist_ok=True), falling back to the old `<module dir>/logs` ONLY when the
DATA_ROOT directory cannot be created or is not writable (non-Docker dev runs).
Everything else about the handler set-up (RotatingFileHandler bounds from
settings, formatter, stdout StreamHandler, ScannerFilter) stays as it is.

Every assertion binds the value to a LOCAL first: pytest's assertion rewrite
would otherwise print the whole `Settings` repr (SECRET_KEY included) on
failure — same rule as tests/test_log_settings_rotation.py.

Fully offline; DATA_ROOT is monkeypatched under tmp_path; the process-wide
"datachat" logger is reset by the `clean_datachat_logger` fixture (copied from
tests/test_log_settings_rotation.py) so the rest of the session is untouched.
"""
import logging
import logging.handlers
from pathlib import Path

import pytest

import logger_utils

# The REAL module directory, bound at import time — before any test monkeypatches
# `logger_utils.__file__` — so the "never the repo tree" checks compare against
# the right place.
REAL_MODULE_LOGS = Path(logger_utils.__file__).resolve().parent / "logs"


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


def _rotating_handlers(lg):
    return [h for h in lg.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]


def _stdout_handlers(lg):
    # RotatingFileHandler subclasses StreamHandler — exclude every file handler.
    return [
        h for h in lg.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]


def _dir_snapshot(d: Path):
    if not d.exists():
        return None
    return sorted((p.name, p.stat().st_size) for p in d.iterdir())


# ---------------------------------------------------------------------------
# (1) DATA_ROOT does not exist yet -> DATA_ROOT/logs/datachat.log is created
# ---------------------------------------------------------------------------
def test_get_logger_creates_log_dir_under_data_root(tmp_path, monkeypatch, clean_datachat_logger):
    """A fresh DATA_ROOT (first boot on an empty volume) gets `logs/datachat.log`
    created under it, the single RotatingFileHandler points there, its bounds
    still come from settings, and the stdout StreamHandler is still attached."""
    root = tmp_path / "root"
    assert not root.exists()
    monkeypatch.setattr(logger_utils.settings, "DATA_ROOT", str(root))
    expected_max_bytes = logger_utils.settings.LOG_MAX_BYTES
    expected_backup_count = logger_utils.settings.LOG_BACKUP_COUNT

    lg = logger_utils.get_logger()

    log_dir = root / "logs"
    log_file = log_dir / "datachat.log"
    file_exists = log_file.is_file()
    assert file_exists, f"expected {log_file} to be created (dir exists={log_dir.exists()})"

    rotating = _rotating_handlers(lg)
    n_rotating = len(rotating)
    assert n_rotating == 1, [type(h).__name__ for h in lg.handlers]
    base_parent = Path(rotating[0].baseFilename).resolve()
    assert base_parent.parent == log_dir.resolve(), base_parent
    assert base_parent.name == "datachat.log"

    bounds = (rotating[0].maxBytes, rotating[0].backupCount)
    assert bounds == (expected_max_bytes, expected_backup_count)

    n_stdout = len(_stdout_handlers(lg))
    assert n_stdout == 1, [type(h).__name__ for h in lg.handlers]


# ---------------------------------------------------------------------------
# (2) DATA_ROOT cannot be created -> fall back to <module dir>/logs, not the repo
# ---------------------------------------------------------------------------
def test_get_logger_falls_back_to_module_logs_when_data_root_unwritable(
        tmp_path, monkeypatch, clean_datachat_logger):
    """DATA_ROOT under a regular FILE can never be created (both OSes). The
    logger must then fall back to the module-relative `logs/` — pointed at
    tmp_path/"repo" here via `logger_utils.__file__` — and never write into
    the real repository tree."""
    blocker = tmp_path / "blocker.txt"
    blocker.write_text("not a directory", encoding="utf-8")
    root = blocker / "root"
    monkeypatch.setattr(logger_utils.settings, "DATA_ROOT", str(root))
    fake_repo = tmp_path / "repo"
    monkeypatch.setattr(logger_utils, "__file__", str(fake_repo / "logger_utils.py"))
    before = _dir_snapshot(REAL_MODULE_LOGS)

    lg = logger_utils.get_logger()

    fallback_file = fake_repo / "logs" / "datachat.log"
    fallback_exists = fallback_file.is_file()
    assert fallback_exists, f"expected fallback log at {fallback_file}"
    rotating = _rotating_handlers(lg)
    n_rotating = len(rotating)
    assert n_rotating == 1, [type(h).__name__ for h in lg.handlers]
    base = Path(rotating[0].baseFilename).resolve()
    assert base == fallback_file.resolve(), base
    assert base.parent != REAL_MODULE_LOGS

    # nothing under DATA_ROOT could have been created (its parent is a file)
    assert blocker.is_file()
    assert not (root / "logs").exists()
    # and nothing was created or changed in the real module's logs/ directory
    after = _dir_snapshot(REAL_MODULE_LOGS)
    assert after == before


# ---------------------------------------------------------------------------
# (3) a log_with_sid line lands in the DATA_ROOT file
# ---------------------------------------------------------------------------
def test_log_with_sid_line_lands_in_data_root_file(tmp_path, monkeypatch, clean_datachat_logger):
    root = tmp_path / "root"
    monkeypatch.setattr(logger_utils.settings, "DATA_ROOT", str(root))

    lg = logger_utils.get_logger()
    logger_utils.log_with_sid("s_dr", "info", "DATA_ROOT_LOG_PROBE")
    for h in lg.handlers:
        h.flush()

    log_file = root / "logs" / "datachat.log"
    file_exists = log_file.is_file()
    assert file_exists, f"expected {log_file}"
    text = log_file.read_text(encoding="utf-8")
    assert "[sid=s_dr] DATA_ROOT_LOG_PROBE" in text
    assert "INFO" in text
