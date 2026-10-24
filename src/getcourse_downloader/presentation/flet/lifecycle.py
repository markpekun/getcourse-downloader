from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from typing import Literal

import flet as ft

_LOGGER = logging.getLogger("getcourse_downloader.ui")
_CLOSED_SESSION_ERRORS = {
    "An attempt to fetch destroyed session.",
    "Event loop is closed",
}
CallbackKind = Literal["event", "finished", "failed"]
_MAX_PENDING_CALLBACKS = 256
_UI_BATCH_SECONDS = 0.008


@dataclass(slots=True)
class _Callback:
    handler: Callable[[], None]
    kind: CallbackKind
    queued_at: float
    replace_key: Hashable | None


class UiCallbacks:
    """Keep worker callbacks within the lifetime of their screen and Flet session."""

    def __init__(
        self, page: ft.Page, on_session_lost: Callable[[], None], *, screen_id: int
    ) -> None:
        self._page = page
        self._on_session_lost = on_session_lost
        self._screen_id = screen_id
        self._lock = threading.Condition(threading.RLock())
        self._closed = False
        self._pending: deque[_Callback] = deque()
        self._scheduled = False
        self._coalesced = 0

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._pending.clear()
            self._lock.notify_all()

    def post(
        self,
        handler: Callable[[], None],
        *,
        kind: CallbackKind,
        replace_key: Hashable | None = None,
    ) -> None:
        """Deliver worker callbacks on the UI loop; producers wait if its queue is full."""
        with self._lock:
            if self._closed:
                return
            if replace_key is not None and self._pending:
                previous = self._pending[-1]
                if previous.replace_key == replace_key:
                    previous.handler = handler
                    self._coalesced += 1
                    return
            while len(self._pending) >= _MAX_PENDING_CALLBACKS and not self._closed:
                self._lock.wait(timeout=0.1)
            if self._closed:
                return
            self._pending.append(_Callback(handler, kind, time.monotonic(), replace_key))
            if self._scheduled:
                return
            self._scheduled = True
            try:
                session = getattr(self._page, "session", None)
                if session is not None:
                    connection = session.connection
                    if connection is None:
                        raise RuntimeError("An attempt to fetch destroyed session.")
                    if connection.loop.is_closed():
                        raise RuntimeError("Event loop is closed")
                self._page.run_task(self._drain)
            except RuntimeError as error:
                self._scheduled = False
                self._pending.clear()
                self._lock.notify_all()
                if not self._session_lost(error, kind):
                    raise

    async def _drain(self) -> None:
        batch_started = time.monotonic()
        try:
            while True:
                with self._lock:
                    if self._closed or not self._pending:
                        self._scheduled = False
                        return
                    callback = self._pending.popleft()
                    self._lock.notify_all()
                started = time.monotonic()
                self._invoke(callback.handler, callback.kind)
                finished = time.monotonic()
                _LOGGER.debug(
                    "ui_callback screen=%x kind=%s queue_ms=%.2f handler_ms=%.2f coalesced=%s",
                    self._screen_id,
                    callback.kind,
                    (started - callback.queued_at) * 1000,
                    (finished - started) * 1000,
                    self._coalesced,
                )
                if finished - batch_started >= _UI_BATCH_SECONDS:
                    await asyncio.sleep(0)
                    batch_started = time.monotonic()
        except BaseException:
            with self._lock:
                self._scheduled = False
                self._pending.clear()
                self._lock.notify_all()
            raise

    def _invoke(self, handler: Callable[[], None], kind: CallbackKind) -> None:
        if self._closed:
            return
        try:
            handler()
        except RuntimeError as error:
            with self._lock:
                if not self._session_lost(error, kind):
                    raise

    def _session_lost(self, error: RuntimeError, kind: CallbackKind) -> bool:
        if str(error) not in _CLOSED_SESSION_ERRORS:
            return False
        self._closed = True
        self._pending.clear()
        self._lock.notify_all()
        _LOGGER.warning("ui_session_unavailable screen=%x callback=%s", self._screen_id, kind)
        self._on_session_lost()
        return True
