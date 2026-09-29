"""Inside the sandbox image the logger never opens a log FILE.

Every job runs as the uid that would own `<DATA_ROOT>/logs/datachat.log`, so a
later job - another user's - could open() it and read what earlier jobs logged.
`PDC_EXECUTOR=1` (executor/Dockerfile, and every runner's environment) makes
`logger_utils.get_logger` attach a stdout handler only.
"""
import logging
import logging.handlers

import pytest

import logger_utils
from settings import settings


@pytest.fixture
def fresh_logger(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "DATA_ROOT", str(tmp_path))
    logger = logging.getLogger("datachat")
    saved = list(logger.handlers)
    logger.handlers.clear()
    yield logger, tmp_path
    for h in list(logger.handlers):
        logger.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    logger.handlers.extend(saved)


def test_inside_the_sandbox_there_is_no_file_handler(fresh_logger, monkeypatch):
    logger, root = fresh_logger
    monkeypatch.setenv("PDC_EXECUTOR", "1")
    logger_utils.get_logger().info("probe")
    kinds = [type(h) for h in logger.handlers]
    assert logging.handlers.RotatingFileHandler not in kinds, kinds
    assert logging.StreamHandler in kinds, kinds
    assert not (root / "logs").exists()


def test_the_web_side_keeps_its_rotating_file(fresh_logger, monkeypatch):
    logger, root = fresh_logger
    monkeypatch.delenv("PDC_EXECUTOR", raising=False)
    logger_utils.get_logger().info("probe")
    kinds = [type(h) for h in logger.handlers]
    assert logging.handlers.RotatingFileHandler in kinds, kinds
    assert (root / "logs" / "datachat.log").exists()
