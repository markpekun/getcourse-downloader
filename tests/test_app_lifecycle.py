import asyncio
from types import SimpleNamespace

import flet as ft
import msgpack
import pytest
from flet.messaging.connection import Connection
from flet.messaging.protocol import (
    ClientAction,
    configure_encode_object_for_msgpack,
)
from flet.messaging.session import Session as FletSession
from flet.pubsub.pubsub_hub import PubSubHub

from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import Course, DownloadSummary, Lesson
from getcourse_downloader.presentation.flet.app import App
from getcourse_downloader.presentation.flet.lifecycle import UiCallbacks
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


def test_system_page_observations_and_ignored_window_events_do_not_patch_the_whole_page(
    monkeypatch,
):
    async def scenario():
        connection = Connection()
        connection.pubsubhub = PubSubHub()
        connection.loop = asyncio.get_running_loop()
        connection.send_message = lambda message: msgpack.packb(
            [message.action, message.body],
            default=configure_encode_object_for_msgpack(ft.BaseControl),
        )
        session = FletSession(connection)
        App(session.page, container=object())  # type: ignore[arg-type]
        msgpack.packb(
            session.get_page_patch(), default=configure_encode_object_for_msgpack(ft.BaseControl)
        )
        patched = []
        original = session.patch_control

        def record(control, *args, **kwargs):
            patched.append(control)
            return original(control, *args, **kwargs)

        monkeypatch.setattr(session, "patch_control", record)
        events = (
            (session.page._i, "app_lifecycle_state_change", {"state": "inactive"}),
            (session.page._i, "resize", {"width": 1400, "height": 850}),
            (session.page.window._i, "event", {"type": "focus"}),
        )
        for control_id, event_name, data in events:
            ft.context.enable_auto_update()
            await session.dispatch_event(control_id, event_name, data)

        assert patched == []

    asyncio.run(scenario())


def _downloading_screen(tmp_path, *, page=None):
    queued = []
    if page is None:
        page = SimpleNamespace(
            update=lambda *_: None,
            run_task=lambda handler, *args: queued.append(lambda: asyncio.run(handler(*args))),
        )

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
    if isinstance(page, ft.Page):
        page.add(screen.view)
    screen.state.selected_lesson_urls.add("https://school.example/1")
    screen._start_download(None)
    if not isinstance(page, ft.Page):
        while queued and screen._preparing_download:
            queued.pop(0)()
        # The preparation cover owns a delayed visual hide.  Callback tests below
        # start from the settled screen and exercise worker callbacks only.
        queued.clear()
    return screen, controller, queued


def test_worker_callbacks_share_one_ui_task_and_coalesce_pending_progress():
    queued = []
    seen = []
    page = SimpleNamespace(run_task=queued.append)
    callbacks = UiCallbacks(page, lambda: None, screen_id=1)
    callbacks.post(lambda: seen.append("started"), kind="event")
    for current in range(20):
        callbacks.post(
            lambda value=current: seen.append(value), kind="event", replace_key="progress"
        )
    callbacks.post(lambda: seen.append("finished"), kind="finished")

    assert len(queued) == 1
    asyncio.run(queued.pop()())

    assert seen == ["started", 19, "finished"]


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


def test_repeated_report_back_then_cancel_keeps_download_and_completion_visible(tmp_path):
    screen, controller, queued = _downloading_screen(tmp_path)
    controller.callbacks["on_event"](
        DownloadEvent(
            DownloadEventType.ERROR,
            lesson="Lesson",
            lesson_url="https://school.example/1",
            diagnostic_report=str(tmp_path / "lesson.json"),
            error_code="LESSON_ACCESS_DENIED",
        )
    )
    queued.pop()()
    download_content = screen._overlay_card.content
    for _ in range(3):
        screen._show_lesson_diagnostics("https://school.example/1")
        assert screen._overlay_card.content is download_content
        screen._close_diagnostic_report()
        assert screen.overlay.visible
    screen._cancel_download()
    assert controller.cancelled and screen.state.cancelling
    controller.callbacks["on_finished"](DownloadSummary(total=1, downloaded=0, cancelled=1))
    queued.pop()()
    assert screen.overlay.visible
    assert screen._completion_flow.result.cancelled


@pytest.mark.parametrize("session_state", ["destroyed", "disconnected", "loop_closed"])
def test_destroyed_flet_session_during_dispatch_cancels_without_callback_error(
    tmp_path, session_state
):
    screen, controller, _ = _downloading_screen(tmp_path)

    class Session:
        pass

    session = Session()
    session.connection = (
        SimpleNamespace(loop=SimpleNamespace(is_closed=lambda: True))
        if session_state == "loop_closed"
        else None
    )
    dead_page = ft.Page(session)
    if session_state == "destroyed":
        del session
    screen._ui_callbacks._page = dead_page
    controller.callbacks["on_finished"](DownloadSummary(total=1, downloaded=1))
    assert controller.cancelled


@pytest.mark.parametrize("authentication", [False, True])
def test_cancel_and_completion_patch_only_mounted_controls_and_reset_speed(
    tmp_path, authentication
):
    async def scenario():
        connection = Connection()
        connection.pubsubhub = PubSubHub()
        connection.loop = asyncio.get_running_loop()
        targets = []
        session = FletSession(connection)

        def encode(value):
            return msgpack.packb(value, default=configure_encode_object_for_msgpack(ft.BaseControl))

        def send(message):
            if message.action is ClientAction.PATCH_CONTROL:
                assert message.body.id in session.index
                targets.append(message.body.id)
            encode([message.action, message.body])

        connection.send_message = send
        encode(session.get_page_patch())
        screen, controller, _ = _downloading_screen(tmp_path, page=session.page)
        await asyncio.wrap_future(screen._preparation_task)
        screen._handle_download_event(
            DownloadEvent(
                DownloadEventType.PROGRESS,
                lesson_url="https://school.example/1",
                current=1,
                total=10,
                speed_bps=1024,
            )
        )
        if authentication:
            screen._handle_download_event(DownloadEvent(DownloadEventType.AUTH_REQUIRED))
            assert screen._download_title._i not in session.index
        screen._cancel_download()
        assert controller.cancelled
        targets.clear()
        screen._finish_summary(DownloadSummary(total=1, downloaded=0, cancelled=1))
        assert screen._speed_text._i in targets
        assert screen._speed_text.value == "Средняя: —"
        assert screen._auth_overlay_task is None
        screen.dispose()

    asyncio.run(scenario())


def test_flet_scroll_and_repeated_report_back_do_not_refresh_the_whole_page(tmp_path, monkeypatch):
    async def scenario():
        connection = Connection()
        connection.pubsubhub = PubSubHub()
        connection.loop = asyncio.get_running_loop()
        connection.send_message = lambda message: msgpack.packb(
            [message.action, message.body],
            default=configure_encode_object_for_msgpack(ft.BaseControl),
        )
        session = FletSession(connection)
        msgpack.packb(
            session.get_page_patch(), default=configure_encode_object_for_msgpack(ft.BaseControl)
        )
        screen, _, _ = _downloading_screen(tmp_path, page=session.page)
        await asyncio.wrap_future(screen._preparation_task)
        patched = []
        original = session.patch_control

        def record(control, *args, **kwargs):
            patched.append(control)
            return original(control, *args, **kwargs)

        monkeypatch.setattr(session, "patch_control", record)
        for event_type in ("start", "update", "end", "user"):
            ft.context.enable_auto_update()
            await session.dispatch_event(
                screen._download_rows_column._i,
                "scroll",
                {
                    "event_type": event_type,
                    "direction": "idle",
                    "pixels": 10.0,
                    "min_scroll_extent": 0.0,
                    "max_scroll_extent": 100.0,
                    "viewport_dimension": 50.0,
                },
            )
        assert patched == []
        screen._diagnostic_fallbacks[tmp_path / "lesson.json"] = '{"error_code":"EXAMPLE"}'
        screen._show_diagnostic_report(tmp_path / "lesson.json", "Example")
        back = screen._diagnostics.card.content.controls[-1]
        patched.clear()
        ft.context.enable_auto_update()
        await session.dispatch_event(back._i, "click", None)
        await asyncio.sleep(0.16)
        assert not screen._diagnostics.control.visible
        assert patched and all(control is screen._diagnostics.control for control in patched)
        patched.clear()
        ft.context.enable_auto_update()
        await session.dispatch_event(back._i, "click", None)
        assert patched == []
        screen.dispose()

    asyncio.run(scenario())


def test_unrelated_dispatch_error_is_not_silenced(tmp_path):
    screen, controller, _ = _downloading_screen(tmp_path)

    def broken_dispatch(_):
        raise RuntimeError("unrelated scheduling failure")

    screen.page.run_task = broken_dispatch
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
        flush_ui_lifecycle_logging,
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
        flush_ui_lifecycle_logging()
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
