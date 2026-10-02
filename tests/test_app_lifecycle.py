import asyncio
from types import SimpleNamespace

import flet as ft

from getcourse_downloader.presentation.flet.app import App


class _Window:
    def __init__(self) -> None:
        self.prevent_close = False
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _Page:
    def __init__(self) -> None:
        self.window = _Window()


class _Screen:
    def __init__(self) -> None:
        self.disposed = False
        self.shutdown_timeout = None

    def dispose(self) -> None:
        self.disposed = True

    def shutdown(self, timeout: float) -> None:
        self.shutdown_timeout = timeout


def test_initial_screen_opens_saved_courses_instead_of_requesting_link_again():
    page = _Page()
    container = SimpleNamespace(courses=SimpleNamespace(has_courses=lambda: True))
    app = App(page, container=container)  # type: ignore[arg-type]
    opened: list[str] = []

    async def show_courses() -> None:
        opened.append("courses")

    async def show_start() -> None:
        opened.append("start")

    app.show_courses = show_courses  # type: ignore[method-assign]
    app.show_start = show_start  # type: ignore[method-assign]

    asyncio.run(app.show_initial_screen())

    assert opened == ["courses"]


def test_window_close_cancels_and_waits_for_active_screen():
    page = _Page()
    app = App(page, container=object())  # type: ignore[arg-type]
    screen = _Screen()
    app._screen = screen

    asyncio.run(app._on_window_event(SimpleNamespace(type=ft.WindowEventType.CLOSE)))

    assert screen.disposed
    assert screen.shutdown_timeout == 6.0
    assert page.window.prevent_close is False
    assert page.window.closed


def test_page_disconnect_only_cancels_active_screen_without_scheduling_shutdown_work():
    page = _Page()
    app = App(page, container=object())  # type: ignore[arg-type]
    screen = _Screen()
    app._screen = screen

    asyncio.run(app._on_page_closed(None))

    assert screen.disposed
    assert screen.shutdown_timeout is None
