from __future__ import annotations

import logging
import threading
from logging.handlers import RotatingFileHandler

from getcourse_downloader.infrastructure.platform.paths import AppPaths

_CONFIGURATION_LOCK = threading.Lock()


def configure_ui_lifecycle_logging(paths: AppPaths) -> None:
    """Configure only the application's safe UI event logger, never Flet protocol logs."""
    logger = logging.getLogger("getcourse_downloader.ui")
    with _CONFIGURATION_LOCK:
        target = str(paths.ui_lifecycle_log_file.resolve())
        if any(
            isinstance(handler, RotatingFileHandler) and handler.baseFilename == target
            for handler in logger.handlers
        ):
            return
        try:
            handler = RotatingFileHandler(
                target, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
            )
        except OSError as error:
            logger.warning("ui_log_unavailable error_type=%s", type(error).__name__)
            return
        handler.setFormatter(
            logging.Formatter("%(asctime)s pid=%(process)d thread=%(threadName)s %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
