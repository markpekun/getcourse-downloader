import asyncio

import flet as ft

from app.screens.start_screen import StartScreen
from app.screens.courses_screen import CoursesScreen
from app.theme import Color, build_theme


_START_WIN_W, _START_WIN_H = 680, 460
_COURSES_WIN_W, _COURSES_WIN_H = 1400, 850


class App:
    def __init__(self, page: ft.Page):
        self.page = page
        page.title = "GetCourse Video Downloader"
        page.dark_theme = build_theme()
        page.theme_mode = ft.ThemeMode.DARK
        page.padding = 0
        page.spacing = 0
        page.bgcolor = Color.BG_DARK
        page.window.min_width = 520
        page.window.min_height = 420

    async def _show_screen(self):
        self.page.clean()
        if CoursesScreen.has_courses():
            page = self.page
            page.window.width = _COURSES_WIN_W
            page.window.height = _COURSES_WIN_H
            screen = CoursesScreen(page)
            page.add(screen.view)
        else:
            page = self.page
            page.window.width = _START_WIN_W
            page.window.height = _START_WIN_H
            screen = StartScreen(page)
            page.add(screen.view)
        await page.window.center()
        self.page.update()

    async def navigate_to_courses(self):
        self.page.clean()
        self.page.window.width = _COURSES_WIN_W
        self.page.window.height = _COURSES_WIN_H
        screen = CoursesScreen(self.page)
        self.page.add(screen.view)
        await self.page.window.center()
        self.page.update()

    def navigate_to_start(self):
        asyncio.create_task(self._show_screen())


async def main(page: ft.Page):
    app = App(page)
    await app._show_screen()


if __name__ == "__main__":
    ft.run(main)
