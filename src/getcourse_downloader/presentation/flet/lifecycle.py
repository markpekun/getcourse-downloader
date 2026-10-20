from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from functools import partial
from typing import Literal

import flet as ft

_LOGGER = logging.getLogger("getcourse_downloader.ui")
_CLOSED_SESSION_ERRORS = {
    "An attempt to fetch destroyed session.",
    "Event loop is closed",
}
CallbackKind = Literal["event", "finished", "failed"]


class UiCallbacks:
    """Keep worker callbacks within the lifetime of their screen and Flet session."""

    def __init__(
        self, page: ft.Page, on_session_lost: Callable[[], None], *, screen_id: int
    ) -> None:
        self._page = page
        self._on_session_lost = on_session_lost
        self._screen_id = screen_id
        self._lock = threading.RLock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            self._closed = True

    def post(self, handler: Callable[[], None], *, kind: CallbackKind) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                self._page.run_thread(partial(self._invoke, handler, kind))
            except RuntimeError as error:
                if not self._session_lost(error, kind):
                    raise

    def _invoke(self, handler: Callable[[], None], kind: CallbackKind) -> None:
        with self._lock:
            if self._closed:
                return
            try:
                handler()
            except RuntimeError as error:
                if not self._session_lost(error, kind):
                    raise

    def _session_lost(self, error: RuntimeError, kind: CallbackKind) -> bool:
        if str(error) not in _CLOSED_SESSION_ERRORS:
            return False
        self._closed = True
        _LOGGER.warning("ui_session_unavailable screen=%x callback=%s", self._screen_id, kind)
        self._on_session_lost()
        return True
