from __future__ import annotations

import asyncio
from typing import Any

import flet as ft

from getcourse_downloader.bootstrap import AppContainer, build_container
from getcourse_downloader.presentation.flet.screens.courses.controller import CoursesController
from getcourse_downloader.presentation.flet.screens.courses.view import CoursesScreen
from getcourse_downloader.presentation.flet.screens.start.controller import StartController
from getcourse_downloader.presentation.flet.screens.start.view import StartScreen
from getcourse_downloader.presentation.flet.theme import Color, build_theme

_START_WIN_W, _START_WIN_H = 680, 460
_COURSES_WIN_W, _COURSES_WIN_H = 1400, 850


class App:
    def __init__(self, page: ft.Page, container: AppContainer | None = None) -> None:
        self.page = page
        self.container = container or build_container()
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

    async def _dispose_screen(self, *, wait: bool = False) -> None:
        screen = self._screen
        self._screen = None
        if screen is None:
            return
        dispose = getattr(screen, "dispose", None)
        if dispose:
            dispose()
        shutdown = getattr(screen, "shutdown", None)
        if wait and shutdown:
            await asyncio.to_thread(shutdown, 6.0)

    async def _on_window_event(self, event) -> None:
        if event.type != ft.WindowEventType.CLOSE or self._closing:
            return
        self._closing = True
        await self._dispose_screen(wait=True)
        self.page.window.prevent_close = False
        await self.page.window.close()

    async def _on_page_closed(self, _event) -> None:
        if self._closing:
            return
        self._closing = True
        await self._dispose_screen(wait=True)

    async def show_initial_screen(self) -> None:
        if self.container.courses.has_courses():
            await self.show_courses()
        else:
            await self.show_start()

    async def show_courses(self) -> None:
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
        self._screen = CoursesScreen(self.page, controller, self.show_start)
        self.page.add(self._screen.view)
        await self.page.window.center()
        self.page.update()

    async def show_start(self) -> None:
        await self._dispose_screen(wait=True)
        self.page.clean()
        self.page.window.width = _START_WIN_W
        self.page.window.height = _START_WIN_H
        controller = StartController(self.container.discover_courses)
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
