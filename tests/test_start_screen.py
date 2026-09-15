import asyncio
import concurrent.futures
from types import SimpleNamespace

from getcourse_downloader.application.ports.discovery import CourseDiscoveryUpdate
from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.presentation.flet.screens.start.state import StartViewState
from getcourse_downloader.presentation.flet.screens.start.view import StartScreen


def test_start_state_only_tracks_fast_discovery():
    state = StartViewState()

    assert state.parse_running is False
    assert state.discovery_visible is False
    assert state.total_parsed == 0
    assert not hasattr(state, "video_check_visible")


def test_successful_discovery_does_not_cancel_its_own_navigation_task():
    class Controller:
        async def discover(self, *_args, **_kwargs):
            return [object()]

    async def scenario() -> tuple[bool, bool, object]:
        screen = StartScreen.__new__(StartScreen)
        screen._controller = Controller()
        screen._loading_task = None
        screen._dot_task = None
        screen._auth_event = asyncio.Event()
        screen.loader = SimpleNamespace(visible=True)
        task_handle: concurrent.futures.Future[None] = concurrent.futures.Future()
        screen._parse_task = task_handle
        screen._stop_all_animations = lambda: None
        transition_completed = False

        async def navigate() -> None:
            nonlocal transition_completed
            screen.dispose()
            await asyncio.sleep(0)
            transition_completed = True

        screen._on_courses_ready = navigate
        await screen._parse_async("https://school.example/course")
        return transition_completed, task_handle.cancelled(), screen._parse_task

    transition_completed, task_cancelled, parse_task = asyncio.run(scenario())

    assert transition_completed is True
    assert task_cancelled is False
    assert parse_task is None


def test_discovery_failure_stays_visible_with_code_and_sanitized_details():
    error = ExternalServiceError(
        "Сайт не ответил за отведённое время.",
        code="SITE_TIMEOUT",
        technical_details=(
            "Timeout at C:\\Users\\Alice\\browser-profile "
            "https://school.example/course?token=private-value"
        ),
    )

    class Controller:
        async def discover(self, *_args, **_kwargs):
            raise error

    class Page:
        def __init__(self) -> None:
            self.updates = 0

        def update(self) -> None:
            self.updates += 1

    async def scenario():
        screen = StartScreen.__new__(StartScreen)
        screen._controller = Controller()
        screen._loading_task = None
        screen._dot_task = None
        screen._discovery_scroll_task = None
        screen._pending_discovery_scroll_url = None
        screen._auth_event = asyncio.Event()
        screen.page = Page()
        screen.state = SimpleNamespace(parse_running=True, discovery_visible=True)
        screen.loader = SimpleNamespace(visible=True, content=None)
        screen.url_input = SimpleNamespace(value="https://school.example/course")
        screen._parse_task = concurrent.futures.Future()
        screen._stop_all_animations = lambda: None
        screen._on_courses_ready = lambda: None

        await screen._parse_async(screen.url_input.value)
        return screen

    screen = asyncio.run(scenario())

    assert screen.loader.visible is True
    assert screen.url_input.value == "https://school.example/course"
    assert screen._error_code.value == "Код ошибки: SITE_TIMEOUT"
    assert screen._error_message.value == "Сайт не ответил за отведённое время."
    assert "Alice" not in screen._error_details.value
    assert "private-value" not in screen._error_details.value
    assert screen._error_details_box.visible is False
    assert callable(screen._error_details_button.on_click)
    assert callable(screen._error_retry_button.on_click)
    assert screen._parse_task is None


def test_error_details_toggle_and_retry_reuse_the_entered_url():
    screen = StartScreen.__new__(StartScreen)
    screen.page = SimpleNamespace(update=lambda: None)
    screen.loader = SimpleNamespace(visible=True)
    screen._error_details_box = SimpleNamespace(visible=False)
    screen._error_details_button = SimpleNamespace(text="Показать подробности")
    retries = []
    screen._start_parse = lambda: retries.append("retry")

    screen._toggle_error_details(None)
    assert screen._error_details_box.visible is True
    assert screen._error_details_button.text == "Скрыть подробности"

    screen._retry_after_error(None)
    assert retries == ["retry"]


def test_unexpected_failure_does_not_expose_raw_details_in_the_summary():
    class Page:
        def update(self) -> None:
            return None

    screen = StartScreen.__new__(StartScreen)
    screen.page = Page()
    screen.loader = SimpleNamespace(visible=False, content=None)
    error = RuntimeError(
        "failed at C:\\Users\\Alice\\profile https://school.example/course?token=private-value"
    )

    screen._show_error(error)

    assert screen._error_code.value == "Код ошибки: INTERNAL_ERROR"
    assert screen._error_message.value == "Произошла непредвиденная ошибка."
    assert "Alice" not in screen._error_details.value
    assert "private-value" not in screen._error_details.value


def test_discovery_scroll_targets_processed_row_and_ignores_detached_control():
    class Scrollable:
        def __init__(self, *, detached: bool = False):
            self.detached = detached
            self.calls: list[dict[str, object]] = []

        async def scroll_to(self, **kwargs):
            if self.detached:
                raise RuntimeError("Control must be added to the page first")
            self.calls.append(kwargs)

    async def scenario() -> list[dict[str, object]]:
        screen = StartScreen.__new__(StartScreen)
        visible = Scrollable()
        screen._discovery_list = visible
        await screen._scroll_discovery_to("https://school.example/course/1")
        screen._discovery_list = Scrollable(detached=True)
        await screen._scroll_discovery_to("https://school.example/course/2")
        return visible.calls

    calls = asyncio.run(scenario())

    assert calls[0]["scroll_key"] == "https://school.example/course/1"
    assert calls[0]["duration"] == 650


def test_discovery_scroll_coalesces_burst_before_smooth_movement(monkeypatch):
    from getcourse_downloader.presentation.flet.screens.start import view as view_module

    async def scenario() -> list[str]:
        screen = StartScreen.__new__(StartScreen)
        screen._discovery_scroll_task = None
        screen._pending_discovery_scroll_url = None
        calls: list[str] = []

        async def scroll(course_url: str) -> None:
            calls.append(course_url)

        screen._scroll_discovery_to = scroll
        screen._schedule_discovery_scroll("https://school.example/course/1")
        screen._schedule_discovery_scroll("https://school.example/course/2")
        task = screen._discovery_scroll_task
        assert task is not None
        await task
        return calls

    monkeypatch.setattr(view_module, "_DISCOVERY_SCROLL_DELAY_SECONDS", 0)
    calls = asyncio.run(scenario())

    assert calls == ["https://school.example/course/2"]


def test_discovery_card_uses_scroll_key_and_scrolls_only_after_checkmark():
    class Page:
        def update(self):
            return None

    async def scenario() -> tuple[list[str], object]:
        screen = StartScreen.__new__(StartScreen)
        screen.state = SimpleNamespace(discovery_visible=True, total_parsed=0)
        screen.page = Page()
        screen._discovery_updates = {}
        screen._discovery_list = SimpleNamespace(controls=[])
        screen._discovery_counter = SimpleNamespace(value="")
        scheduled: list[str] = []
        screen._schedule_discovery_scroll = scheduled.append
        queued = CourseDiscoveryUpdate("https://school.example/course/1", "Курс")
        loaded = CourseDiscoveryUpdate("https://school.example/course/1", "Курс", 12)
        await screen._on_course_parsed(queued)
        await screen._on_course_parsed(loaded)
        return scheduled, screen._discovery_list.controls[0].key

    scheduled, key = asyncio.run(scenario())

    assert scheduled == ["https://school.example/course/1"]
    assert getattr(key, "value", None) == "https://school.example/course/1"
