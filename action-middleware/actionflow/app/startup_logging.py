from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from actionflow.app.paths import startup_log_path

STARTUP_LOG_PATH = startup_log_path()


def configure_startup_log_path(path: Path) -> None:
    global STARTUP_LOG_PATH
    STARTUP_LOG_PATH = path
    logger = logging.getLogger("actionflow.startup")
    logger.handlers.clear()


def get_startup_logger() -> logging.Logger:
    logger = logging.getLogger("actionflow.startup")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False

    STARTUP_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        STARTUP_LOG_PATH,
        maxBytes=500_000,
        backupCount=2,
        encoding="utf-8",
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if getattr(sys.stdout, "write", None) and sys.stdout not in (None,):
        try:
            stream_handler = logging.StreamHandler(sys.stdout)
            stream_handler.setFormatter(formatter)
            logger.addHandler(stream_handler)
        except Exception:
            pass

    return logger


def log_startup_exception(message: str, exc: Exception) -> None:
    logger = get_startup_logger()
    logger.exception("%s: %s", message, exc)
