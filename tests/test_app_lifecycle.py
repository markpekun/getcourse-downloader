import asyncio
from types import SimpleNamespace

import flet as ft
import pytest

from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import Course, DownloadSummary, Lesson
from getcourse_downloader.presentation.flet.app import App
from getcourse_downloader.presentation.flet.screens.courses.controller import CoursesController
from getcourse_downloader.presentation.flet.screens.courses.view import CoursesScreen


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


def _downloading_screen(tmp_path):
    queued = []
    page = SimpleNamespace(update=lambda: None, run_thread=queued.append)

    class Controller:
        make_request = staticmethod(CoursesController.make_request)
        cancelled = False

        def load_save_path(self):
            return str(tmp_path)

        def load_courses(self):
            return [Course("Course", lessons=(Lesson("Lesson", "https://school.example/1"),))]

        def start_download(self, request, **callbacks):
            self.callbacks = callbacks

        def cancel(self):
            self.cancelled = True

    controller = Controller()
    screen = CoursesScreen(page, controller, lambda: None)
    screen.state.selected_lesson_urls.add("https://school.example/1")
    screen._start_download(None)
    return screen, controller, queued


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("on_event", DownloadEvent(DownloadEventType.LESSON_STARTED)),
        ("on_finished", DownloadSummary(total=1, downloaded=0, no_video=1)),
        ("on_failed", RuntimeError("example download failure")),
    ],
)
def test_disposed_courses_screen_drops_late_download_callbacks(tmp_path, kind, value):
    screen, controller, queued = _downloading_screen(tmp_path)
    screen.dispose()
    controller.callbacks[kind](value)
    assert queued == []
    assert controller.cancelled


def test_callback_queued_before_disposal_does_not_replace_the_closed_screen(tmp_path):
    screen, controller, queued = _downloading_screen(tmp_path)
    controller.callbacks["on_finished"](DownloadSummary(total=1, downloaded=1))
    screen.dispose()
    original = screen._overlay_card.content
    queued.pop()()
    assert screen._overlay_card.content is original


def test_destroyed_flet_session_during_dispatch_cancels_without_callback_error(tmp_path):
    screen, controller, _ = _downloading_screen(tmp_path)

    class Session:
        pass

    session = Session()
    dead_page = ft.Page(session)
    del session
    screen.page.run_thread = dead_page.run_thread
    controller.callbacks["on_finished"](DownloadSummary(total=1, downloaded=1))
    assert controller.cancelled


def test_unrelated_dispatch_error_is_not_silenced(tmp_path):
    screen, controller, _ = _downloading_screen(tmp_path)

    def broken_dispatch(_):
        raise RuntimeError("unrelated scheduling failure")

    screen.page.run_thread = broken_dispatch
    with pytest.raises(RuntimeError, match="unrelated scheduling failure"):
        controller.callbacks["on_finished"](DownloadSummary(total=1, downloaded=1))


def test_session_destroyed_after_queueing_is_handled_when_the_callback_runs(tmp_path):
    screen, controller, queued = _downloading_screen(tmp_path)
    controller.callbacks["on_finished"](DownloadSummary(total=1, downloaded=1))

    class Session:
        pass

    session = Session()
    dead_page = ft.Page(session)
    del session
    screen.page.update = dead_page.update
    queued.pop()()
    controller.callbacks["on_event"](DownloadEvent(DownloadEventType.LESSON_STARTED))
    assert queued == []


def test_ui_lifecycle_log_is_not_duplicated_and_excludes_report_contents(tmp_path):
    import logging

    from getcourse_downloader.infrastructure.diagnostics.ui_lifecycle import (
        configure_ui_lifecycle_logging,
    )
    from getcourse_downloader.infrastructure.platform.paths import AppPaths

    logger = logging.getLogger("getcourse_downloader.ui")
    original_handlers = tuple(logger.handlers)
    original_level, original_propagate = logger.level, logger.propagate
    paths = AppPaths(tmp_path, tmp_path / "unused-profile", tmp_path / "resources")
    try:
        configure_ui_lifecycle_logging(paths)
        configure_ui_lifecycle_logging(paths)
        screen, controller, queued = _downloading_screen(tmp_path)
        controller.callbacks["on_failed"](
            RuntimeError("https://cdn.example/?token=fictional-token")
        )
        queued.pop()()
        report = tmp_path / "lesson.json"
        report.write_text('{"lesson":"Fictional private lesson"}', encoding="utf-8")
        screen._show_diagnostic_report(report, "Fictional private lesson")
        screen.dispose()
        content = paths.ui_lifecycle_log_file.read_text(encoding="utf-8")
        assert content.count("courses_screen_created") == 1
        assert "lesson_report_opened" in content
        assert "courses_screen_disposed" in content
        assert "fictional-token" not in content
        assert "Fictional private lesson" not in content
        assert str(report) not in content
    finally:
        for handler in tuple(logger.handlers):
            if handler not in original_handlers:
                logger.removeHandler(handler)
                handler.close()
        logger.setLevel(original_level)
        logger.propagate = original_propagate
