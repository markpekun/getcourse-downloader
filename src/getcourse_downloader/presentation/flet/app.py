from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any

import flet as ft

from getcourse_downloader.bootstrap import AppContainer, build_container
from getcourse_downloader.infrastructure.diagnostics.ui_lifecycle import (
    flush_ui_lifecycle_logging,
    trace_operation,
)
from getcourse_downloader.presentation.flet.screens.courses.controller import CoursesController
from getcourse_downloader.presentation.flet.screens.courses.view import CoursesScreen
from getcourse_downloader.presentation.flet.screens.start.controller import StartController
from getcourse_downloader.presentation.flet.screens.start.view import StartScreen
from getcourse_downloader.presentation.flet.theme import Color, build_theme

_START_WIN_W, _START_WIN_H = 680, 460
_COURSES_WIN_W, _COURSES_WIN_H = 1400, 850
_LOGGER = logging.getLogger("getcourse_downloader.ui")


class App:
    def __init__(self, page: ft.Page, container: AppContainer | None = None) -> None:
        self.page = page
        self.container = container or build_container()
        _LOGGER.info("app_created app=%x", id(self))
        self._screen: Any = None
        self._closing = False
        page.title = "GetCourse Video Downloader"
        page.dark_theme = build_theme()
        page.theme_mode = ft.ThemeMode.DARK
        page.padding = 0
        page.spacing = 0
        page.bgcolor = Color.BG_DARK
        page.window.min_width = 520
        page.window.min_height = 420
        page.window.prevent_close = True
        page.window.on_event = self._on_window_event
        page.on_close = self._on_page_closed
        page.on_disconnect = self._on_page_closed
        page.on_error = self._on_page_error
        page.on_app_lifecycle_state_change = self._on_observation_event
        page.on_resize = self._on_observation_event
        page.on_platform_brightness_change = self._on_observation_event

    def _on_observation_event(self, event) -> None:
        """System observations do not change controls, so never patch the whole Page."""
        ft.context.disable_auto_update()
        _LOGGER.debug(
            "page_observation app=%x event=%s",
            id(self),
            type(event).__name__,
        )

    def _on_page_error(self, event) -> None:
        ft.context.disable_auto_update()
        message = str(getattr(event, "data", ""))
        kind = next(
            (
                name
                for name in (
                    "RangeError",
                    "StateError",
                    "AssertionError",
                    "TypeError",
                    "ValueError",
                )
                if name in message
            ),
            "unknown",
        )
        fingerprint = hashlib.sha256(message.encode("utf-8")).hexdigest()[:12]
        _LOGGER.error(
            "page_client_error app=%x kind=%s fingerprint=%s length=%s",
            id(self),
            kind,
            fingerprint,
            len(message),
        )

    async def _dispose_screen(self, *, wait: bool = False) -> None:
        screen = self._screen
        self._screen = None
        if screen is None:
            return
        with trace_operation("screen_dispose", app=id(self), screen=id(screen), wait=wait):
            dispose = getattr(screen, "dispose", None)
            if dispose:
                dispose()
            shutdown = getattr(screen, "shutdown", None)
            if wait and shutdown:
                await asyncio.to_thread(shutdown, 6.0)

    async def _on_window_event(self, event) -> None:
        ft.context.disable_auto_update()
        if event.type != ft.WindowEventType.CLOSE or self._closing:
            return
        _LOGGER.info("window_close_requested app=%x", id(self))
        self._closing = True
        await self._dispose_screen(wait=True)
        await asyncio.to_thread(flush_ui_lifecycle_logging)
        self.page.window.prevent_close = False
        # The close signal has already been handled; do not send it through interception again.
        await self.page.window.destroy()

    async def _on_page_closed(self, _event) -> None:
        reason = "disconnect" if getattr(_event, "name", None) == "disconnect" else "close"
        _LOGGER.info("page_session_ended app=%x reason=%s", id(self), reason)
        if self._closing:
            return
        self._closing = True
        await self._dispose_screen(wait=True)
        await asyncio.to_thread(flush_ui_lifecycle_logging)

    async def show_initial_screen(self) -> None:
        if self.container.courses.has_courses():
            await self.show_courses()
        else:
            await self.show_start()

    async def show_courses(self) -> None:
        _LOGGER.info("show_courses app=%x", id(self))
        await self._dispose_screen(wait=True)
        self.page.clean()
        self.page.window.width = _COURSES_WIN_W
        self.page.window.height = _COURSES_WIN_H
        controller = CoursesController(
            self.container.courses,
            self.container.settings,
            self.container.download_lessons,
            self.container.authorization,
        )
        with trace_operation("courses_screen_build", app=id(self)):
            self._screen = CoursesScreen(self.page, controller, self.show_start)
            self.page.add(self._screen.view)
        await self.page.window.center()
        self.page.update()

    async def show_start(self) -> None:
        _LOGGER.info("show_start app=%x", id(self))
        await self._dispose_screen(wait=True)
        self.page.clean()
        self.page.window.width = _START_WIN_W
        self.page.window.height = _START_WIN_H
        controller = StartController(self.container.discover_courses)
        with trace_operation("start_screen_build", app=id(self)):
            self._screen = StartScreen(self.page, controller, self.show_courses)
            self.page.add(self._screen.view)
        await self.page.window.center()
        self.page.update()


async def main(page: ft.Page) -> None:
    await App(page).show_initial_screen()


def run() -> None:
    ft.run(main)


if __name__ == "__main__":
    run()
