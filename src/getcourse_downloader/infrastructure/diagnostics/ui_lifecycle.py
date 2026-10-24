from __future__ import annotations

import logging
import queue
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from logging.handlers import RotatingFileHandler

from getcourse_downloader.infrastructure.platform.paths import AppPaths

_CONFIGURATION_LOCK = threading.Lock()
_LOGGER = logging.getLogger("getcourse_downloader.ui")
_MAX_PENDING_DEBUG = 2048
_SAFE_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,64}\Z")


class _QueuedLifecycleHandler(logging.Handler):
    """Keep disk I/O on one writer; bound DEBUG backlog without losing INFO+."""

    def __init__(self, target: RotatingFileHandler) -> None:
        super().__init__()
        self.baseFilename = target.baseFilename
        self._target = target
        self._queue: queue.Queue[logging.LogRecord | threading.Event | None] = queue.Queue()
        self._state_lock = threading.Lock()
        self._closing = False
        self._pending_debug = 0
        self._dropped_debug = 0
        self._writer = threading.Thread(
            target=self._write_records, name="ui-log-writer", daemon=True
        )
        self._writer.start()

    @property
    def active(self) -> bool:
        with self._state_lock:
            return not self._closing

    def emit(self, record: logging.LogRecord) -> None:
        with self._state_lock:
            if self._closing:
                return
            if record.levelno <= logging.DEBUG:
                if self._pending_debug >= _MAX_PENDING_DEBUG:
                    self._dropped_debug += 1
                    return
                self._pending_debug += 1
            self._queue.put_nowait(record)

    def _write_records(self) -> None:
        try:
            while True:
                item = self._queue.get()
                if isinstance(item, logging.LogRecord) and item.levelno <= logging.DEBUG:
                    with self._state_lock:
                        self._pending_debug -= 1
                with self._state_lock:
                    dropped, self._dropped_debug = self._dropped_debug, 0
                if dropped:
                    self._write_record(
                        logging.LogRecord(
                            _LOGGER.name,
                            logging.WARNING,
                            "",
                            0,
                            "ui_log_debug_dropped count=%s",
                            (dropped,),
                            None,
                        )
                    )
                if item is None:
                    break
                if isinstance(item, threading.Event):
                    with suppress(OSError, ValueError):
                        self._target.flush()
                    item.set()
                else:
                    self._write_record(item)
        finally:
            with suppress(OSError, ValueError):
                self._target.close()

    def _write_record(self, record: logging.LogRecord) -> None:
        # A failed diagnostic sink must not stop the writer or strand a flush barrier.
        with suppress(Exception):
            self._target.handle(record)

    def flush(self) -> None:
        with self._state_lock:
            closing = self._closing
            barrier = threading.Event()
            if not closing:
                self._queue.put_nowait(barrier)
        if threading.current_thread() is self._writer:
            return
        if closing:
            self._writer.join()
        else:
            barrier.wait()

    def close(self) -> None:
        with self._state_lock:
            if not self._closing:
                self._closing = True
                self._queue.put_nowait(None)
        if threading.current_thread() is not self._writer:
            self._writer.join()
        super().close()


def configure_ui_lifecycle_logging(paths: AppPaths) -> None:
    """Configure only the application's safe UI event logger, never Flet protocol logs."""
    with _CONFIGURATION_LOCK:
        target = str(paths.ui_lifecycle_log_file.resolve())
        if any(
            isinstance(handler, _QueuedLifecycleHandler)
            and handler.baseFilename == target
            and handler.active
            for handler in _LOGGER.handlers
        ):
            return
        try:
            file_handler = RotatingFileHandler(
                target, maxBytes=1_000_000, backupCount=2, encoding="utf-8", delay=True
            )
        except OSError as error:
            _LOGGER.warning("ui_log_unavailable error_type=%s", type(error).__name__)
            return
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s pid=%(process)d thread=%(threadName)s %(message)s")
        )
        for handler in tuple(_LOGGER.handlers):
            if isinstance(handler, _QueuedLifecycleHandler):
                _LOGGER.removeHandler(handler)
                handler.close()
        _LOGGER.addHandler(_QueuedLifecycleHandler(file_handler))
        _LOGGER.setLevel(logging.DEBUG)
        _LOGGER.propagate = False


def flush_ui_lifecycle_logging() -> None:
    """Wait for queued records to reach disk; use at explicit shutdown or in checks."""
    for handler in tuple(_LOGGER.handlers):
        if isinstance(handler, _QueuedLifecycleHandler):
            handler.flush()


@contextmanager
def trace_operation(name: str, **safe_fields: str | int | float | bool | None) -> Iterator[None]:
    """Trace a caller-selected action and safe scalar metadata, never arguments or text."""
    operation = name if _SAFE_TOKEN.fullmatch(name) else "redacted"
    fields = []
    for key, value in safe_fields.items():
        if not _SAFE_TOKEN.fullmatch(key):
            continue
        if isinstance(value, str):
            value = value if _SAFE_TOKEN.fullmatch(value) else "redacted"
        elif value is not None and type(value) not in {int, float, bool}:
            continue
        fields.append(f"{key}={value}")
    metadata = " ".join(fields)
    started_at = time.monotonic()
    _LOGGER.info("operation_started operation=%s %s", operation, metadata)
    outcome, error_type = "completed", "none"
    try:
        yield
    except BaseException as error:
        outcome, error_type = "failed", type(error).__name__
        raise
    finally:
        _LOGGER.info(
            "operation_finished operation=%s outcome=%s duration_ms=%s error_type=%s %s",
            operation,
            outcome,
            round((time.monotonic() - started_at) * 1000),
            error_type,
            metadata,
        )
