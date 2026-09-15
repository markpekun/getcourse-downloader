import asyncio
import concurrent.futures
import json
from pathlib import Path
from types import SimpleNamespace

import flet as ft

from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import Course, DownloadSummary, Lesson, SelectedLesson
from getcourse_downloader.presentation.flet.screens.courses.components import (
    build_course_tree,
    build_download_lesson_row,
    folder_badge_text,
    iter_course_lessons,
    selected_course_lessons,
)
from getcourse_downloader.presentation.flet.screens.courses.view import CoursesScreen
from getcourse_downloader.presentation.flet.theme import Color


def test_log_color_segments():
    assert CoursesScreen._log_color("Сегменты: 5/10") == "#F59E0B"


def test_log_color_success():
    assert CoursesScreen._log_color("✅ Готово") == Color.GREEN
    assert CoursesScreen._log_color("✓ ok") == Color.GREEN


def test_log_color_error():
    assert CoursesScreen._log_color("❌ Ошибка") == Color.RED
    assert CoursesScreen._log_color("Ошибка загрузки") == Color.RED


def test_log_color_default():
    assert CoursesScreen._log_color("обычная строка") == Color.TEXT_SECONDARY


def test_parse_summary():
    lines = ["Загружено: 2 из 3", "Не удалось: 1", "✗ Урок 5. Катионные ПАВ"]
    header, failed = CoursesScreen._parse_summary(lines)
    assert header == ["Загружено: 2 из 3", "Не удалось: 1"]
    assert failed == ["✗ Урок 5. Катионные ПАВ"]


def test_parse_summary_empty():
    assert CoursesScreen._parse_summary([]) == ([], [])


def test_parse_summary_all_failed():
    lines = [f"✗ Урок {i}" for i in range(1, 15)]
    header, failed = CoursesScreen._parse_summary(lines)
    assert header == []
    assert len(failed) == 14


def test_update_download_title_stages():
    cs = CoursesScreen.__new__(CoursesScreen)
    cs._download_title = ft.Text("Подготовка")
    cs._update_download_title("  ⏳ Получение запроса...")
    assert cs._download_title.value == "Получение запроса"
    cs._update_download_title("  📡 Получение запроса")
    assert cs._download_title.value == "Получение запроса"
    cs._update_download_title("  ▶ Скачивание сегментов...")
    assert cs._download_title.value == "Загрузка видео"


def test_should_log_lessons_and_segments():
    cs = CoursesScreen.__new__(CoursesScreen)
    assert cs._should_log("Старт скачивания: 2 уроков") is True
    assert cs._should_log("  ▶ Урок 3. Что такое INCI?") is True
    assert cs._should_log("  Сегменты: 1/239 (0%)") is True
    assert cs._should_log("  Сегментов: 239/239 (100%)") is True


def test_should_log_filters_stage_lines():
    cs = CoursesScreen.__new__(CoursesScreen)
    assert cs._should_log("  ✓ Авторизация активна") is True
    assert cs._should_log("  ⏳ Получение запроса...") is False
    assert cs._should_log("  ⏳ Конвертация видео...") is False


def test_should_log_logs_stage_markers():
    cs = CoursesScreen.__new__(CoursesScreen)
    assert cs._should_log("  ▶ Урок 3. Что такое INCI?") is True
    assert cs._should_log("  ▶ Скачивание сегментов...") is True
    assert cs._should_log("  ▶ ZOOM от 03.02.2026: Разбор") is True


def test_is_progress_line():
    assert CoursesScreen._is_progress_line("Сегменты: 3/10 (30%)") is True
    assert CoursesScreen._is_progress_line("Сегментов: 10/10 (100%)") is True
    assert CoursesScreen._is_progress_line("▶ Урок 3. Что такое INCI?") is False


def test_update_download_title_auth_not_confused_with_request():
    cs = CoursesScreen.__new__(CoursesScreen)
    cs._download_title = ft.Text("Подготовка")
    cs._update_download_title("  ⚠ Страница запросила авторизацию")
    assert cs._download_title.value == "Проверка авторизации"


def test_update_download_title_playlist_not_found():
    cs = CoursesScreen.__new__(CoursesScreen)
    cs._download_title = ft.Text("Подготовка")
    cs._update_download_title("  ⚠ Master playlist не получен")
    assert cs._download_title.value == "Плейлист не найден"


def test_update_download_title_page_then_waiting():
    cs = CoursesScreen.__new__(CoursesScreen)
    cs._download_title = ft.Text("Подготовка")
    cs._update_download_title("  ▶ Урок 5. Катионные ПАВ")
    assert cs._download_title.value == "Загрузка страницы урока"
    cs._update_download_title("  ⏳ Получение запроса...")
    assert cs._download_title.value == "Получение запроса"


class _FakePage:
    def __init__(self):
        self.updates = 0

    def update(self):
        self.updates += 1


def test_add_log_updates_page_for_filtered_stage_line():
    cs = CoursesScreen.__new__(CoursesScreen)
    cs.page = _FakePage()
    cs._download_title = ft.Text("Подготовка")
    cs._log_column = ft.Column(scroll=ft.ScrollMode.AUTO, auto_scroll=True, spacing=1)
    cs.log_lines = []
    cs._add_log("  ⏳ Получение запроса...")
    assert cs._download_title.value == "Получение запроса"
    assert cs.page.updates >= 1
    assert len(cs._log_column.controls) == 0


def test_add_log_filtered_no_title_change_no_update():
    cs = CoursesScreen.__new__(CoursesScreen)
    cs.page = _FakePage()
    cs._download_title = ft.Text("Загрузка видео")
    cs._log_column = ft.Column(scroll=ft.ScrollMode.AUTO, auto_scroll=True, spacing=1)
    cs.log_lines = []
    cs._add_log("  ⚠ Нет сегментов")
    assert cs.page.updates == 0
    assert len(cs._log_column.controls) == 0


def test_update_download_title_ignores_unrelated():
    cs = CoursesScreen.__new__(CoursesScreen)
    cs._download_title = ft.Text("Подготовка")
    cs._update_download_title("  ⚠ Нет сегментов")
    assert cs._download_title.value == "Подготовка"


def test_build_support_block_github_star_clickable():
    cs = CoursesScreen.__new__(CoursesScreen)
    block = cs._build_support_block()
    assert len(block) == 1
    column = block[0]
    assert isinstance(column, ft.Column)
    text = column.controls[0]
    assert isinstance(text, ft.Text)
    spans = text.spans
    assert len(spans) == 2
    assert spans[0].text == "⭐ "
    link = spans[1]
    assert link.text == "Star on GitHub"
    assert link.style.decoration != ft.TextDecoration.UNDERLINE
    assert link.style.color == Color.ACCENT_LIGHT
    assert all(span.on_click is not None for span in spans)


def test_build_failed_lessons_scrollable():
    cs = CoursesScreen.__new__(CoursesScreen)
    container = cs._build_failed_lessons([f"✗ Урок {i}" for i in range(1, 12)])
    assert isinstance(container, ft.Container)
    inner = container.content
    assert isinstance(inner, ft.Column)
    assert inner.scroll == ft.ScrollMode.AUTO
    assert len(inner.controls) == 11


def test_no_video_summary_is_presented_as_warning():
    screen = CoursesScreen.__new__(CoursesScreen)
    captured: dict[str, object] = {}

    def finish(message, is_error=False, is_warning=False, failed=None):
        captured.update(
            message=message,
            is_error=is_error,
            is_warning=is_warning,
            failed=failed,
        )

    screen._finish_download = finish
    screen._finish_summary(DownloadSummary(total=1, downloaded=0, no_video=1))

    assert captured["is_warning"] is True
    assert "Без видео: 1" in str(captured["message"])


def _nested_course() -> Course:
    lessons = tuple(
        Lesson(f"Урок {index}", f"https://school.example/lesson/{index}") for index in range(1, 15)
    )
    return Course(
        title="Демо-курс",
        url="https://school.example/course/root",
        children=(
            Course(
                title="Учебный модуль",
                url="https://school.example/course/child",
                lessons=lessons,
            ),
        ),
    )


def _build_tree(
    course: Course,
    selected: set[str],
    expanded: set[str],
    query: str = "",
):
    return build_course_tree(
        course,
        accent="#7C3AED",
        selected_urls=selected,
        expanded_urls=expanded,
        query=query,
        on_folder_toggle=lambda _: None,
        on_folder_selection=lambda _course, _selected: None,
        on_lesson_selection=lambda _lesson, _selected: None,
    )


def test_recursive_lessons_keep_course_path():
    items = list(iter_course_lessons(_nested_course()))

    assert len(items) == 14
    assert items[0].course_path == ("Демо-курс", "Учебный модуль")


def test_selected_lessons_deduplicate_same_url_and_keep_first_tree_path():
    duplicate = Lesson("Один урок", "https://school.example/lesson/shared")
    courses = (
        Course("Первый курс", lessons=(duplicate,), url="https://school.example/course/1"),
        Course("Второй курс", lessons=(duplicate,), url="https://school.example/course/2"),
    )

    selected = selected_course_lessons(courses, {duplicate.url})

    assert len(selected) == 1
    assert selected[0].course_path == ("Первый курс",)


def test_partially_selected_folder_stays_visually_unchecked():
    course = _nested_course()
    selected = {f"https://school.example/lesson/{index}" for index in range(1, 4)}
    tree = _build_tree(
        course,
        selected,
        {"https://school.example/course/root"},
    )

    root_url = "https://school.example/course/root"
    child_url = "https://school.example/course/child"
    assert tree.folder_checkboxes[root_url].value is False
    assert tree.folder_checkboxes[root_url].tristate is False
    assert tree.folder_checkboxes[child_url].value is False
    assert tree.folder_checkboxes[child_url].tristate is False
    assert tree.folder_badges[root_url].value == "3 из 14"
    assert folder_badge_text(0, 14) == "14 уроков"
    assert folder_badge_text(14, 14) == "14 из 14"


def test_selection_survives_collapse_and_expand():
    course = _nested_course()
    selected = {"https://school.example/lesson/2"}
    root_url = "https://school.example/course/root"
    child_url = "https://school.example/course/child"

    collapsed = _build_tree(course, selected, set())
    assert collapsed.lesson_checkboxes == {}
    assert selected == {"https://school.example/lesson/2"}

    expanded = _build_tree(course, selected, {root_url, child_url})
    assert expanded.lesson_checkboxes["https://school.example/lesson/2"].value is True
    assert expanded.lesson_checkboxes["https://school.example/lesson/1"].value is False


def test_recursive_search_keeps_ancestors_and_only_matching_lessons():
    course = _nested_course()
    tree = _build_tree(course, set(), set(), "Урок 12")

    assert set(tree.folder_checkboxes) == {
        "https://school.example/course/root",
        "https://school.example/course/child",
    }
    assert set(tree.lesson_checkboxes) == {"https://school.example/lesson/12"}


def test_search_hides_unmatched_root_without_losing_selection_state():
    course = _nested_course()
    selected = {"https://school.example/lesson/2"}
    tree = _build_tree(course, selected, set(), "такого урока нет")

    assert tree.control.visible is False
    assert tree.lesson_checkboxes == {}
    assert selected == {"https://school.example/lesson/2"}


def test_download_row_states_are_keyed_by_lesson_url():
    item = SelectedLesson(
        course_path=("Курс", "Модуль"),
        lesson=Lesson("Урок", "https://school.example/lesson/1"),
    )
    lesson_row = build_download_lesson_row(item)
    assert isinstance(lesson_row.control.key, ft.ScrollKey)
    assert lesson_row.control.key.value == item.lesson.url
    screen = CoursesScreen.__new__(CoursesScreen)
    screen.page = _FakePage()
    screen._download_rows = {item.lesson.url: lesson_row}

    screen._update_download_row(
        DownloadEvent(
            DownloadEventType.LESSON_STARTED,
            lesson="Урок",
            lesson_url=item.lesson.url,
        )
    )
    assert isinstance(lesson_row.status_holder.content, ft.Icon)
    assert lesson_row.status_text.value == "Проверяем видео…"

    screen._update_download_row(
        DownloadEvent(
            DownloadEventType.VIDEO_FOUND,
            lesson="Урок",
            lesson_url=item.lesson.url,
        )
    )
    assert lesson_row.progress.visible is True

    screen._update_download_row(
        DownloadEvent(
            DownloadEventType.PROGRESS,
            lesson="Урок",
            lesson_url=item.lesson.url,
            current=3,
            total=4,
        )
    )
    assert lesson_row.progress.value == 0.75
    assert lesson_row.progress_text.value == "75%"

    screen._update_download_row(
        DownloadEvent(
            DownloadEventType.LESSON_COMPLETED,
            lesson="Урок",
            lesson_url=item.lesson.url,
            quality="1080p",
        )
    )
    assert lesson_row.status_text.value == "Готово · 1080p"
    assert lesson_row.progress.visible is False
    assert lesson_row.progress_text.visible is False

    screen._update_download_row(
        DownloadEvent(
            DownloadEventType.LESSON_NO_VIDEO,
            lesson="Урок",
            lesson_url=item.lesson.url,
        )
    )
    assert lesson_row.status_text.value == "Видео не найдено"
    assert lesson_row.status_text.color == Color.RED
    assert lesson_row.progress.visible is False


def test_download_speed_is_formatted_for_header():
    assert CoursesScreen._format_speed(512) == "512 Б/с"
    assert CoursesScreen._format_speed(128 * 1024) == "128 КБ/с"
    assert CoursesScreen._format_speed(1.5 * 1024 * 1024) == "1.5 МБ/с"


def test_failed_download_row_exposes_the_individual_diagnostic_report():
    item = SelectedLesson(("Курс",), Lesson("Урок", "https://school.example/lesson/1"))
    opened: list[str] = []
    lesson_row = build_download_lesson_row(item, on_details=lambda: opened.append("opened"))
    screen = CoursesScreen.__new__(CoursesScreen)
    screen.page = SimpleNamespace(update=lambda: None)
    screen._download_rows = {item.lesson.url: lesson_row}

    screen._update_download_row(
        DownloadEvent(
            DownloadEventType.ERROR,
            lesson="Урок",
            lesson_url=item.lesson.url,
            error_code="HTTP_403",
            diagnostic_report="C:/reports/lesson.json",
        )
    )

    assert lesson_row.status_text.value == "Ошибка"
    assert lesson_row.details_button.visible is True
    lesson_row.details_button.on_click(None)
    assert opened == ["opened"]


def test_screen_tracks_individual_and_last_run_diagnostic_reports():
    screen = CoursesScreen.__new__(CoursesScreen)
    screen._diagnostic_reports = {}
    screen._diagnostic_reports_by_title = {}
    screen._last_run_report = None
    screen._diagnostics_button = SimpleNamespace(visible=False)

    screen._remember_diagnostic(
        DownloadEvent(
            DownloadEventType.ERROR,
            lesson="Урок",
            lesson_url="https://school.example/lesson/1",
            diagnostic_report="C:/reports/lesson.json",
        )
    )
    screen._remember_diagnostic(
        DownloadEvent(
            DownloadEventType.SUMMARY,
            diagnostic_report="C:/reports/last-run.json",
        )
    )

    assert screen._diagnostic_reports["https://school.example/lesson/1"] == Path(
        "C:/reports/lesson.json"
    )
    assert screen._diagnostic_reports_by_title["Урок"] == Path("C:/reports/lesson.json")
    assert screen._last_run_report == Path("C:/reports/last-run.json")
    assert screen._diagnostics_button.visible is True


def test_no_video_row_exposes_its_diagnostic_report_without_relayout():
    item = SelectedLesson(("Курс",), Lesson("Урок", "https://school.example/lesson/1"))
    lesson_row = build_download_lesson_row(item, on_details=lambda: None)
    screen = CoursesScreen.__new__(CoursesScreen)
    screen.page = SimpleNamespace(update=lambda: None)
    screen._download_rows = {item.lesson.url: lesson_row}

    screen._update_download_row(
        DownloadEvent(
            DownloadEventType.LESSON_NO_VIDEO,
            lesson="Урок",
            lesson_url=item.lesson.url,
            error_code="VIDEO_NOT_FOUND",
            diagnostic_report="C:/reports/no-video.json",
        )
    )

    assert lesson_row.status_text.value == "Видео не найдено"
    assert lesson_row.details_button.visible is True
    assert lesson_row.status_text.max_lines == 1


def test_diagnostic_report_viewport_fits_inside_overlay_card(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"error_code": "HTTP_403"}), encoding="utf-8")
    screen = CoursesScreen.__new__(CoursesScreen)
    screen.page = SimpleNamespace(update=lambda: None)
    screen.overlay = SimpleNamespace(visible=False)
    screen._overlay_card = ft.Container(width=600, padding=ft.Padding.all(24))

    screen._show_diagnostic_report(path, "Отчёт по уроку")

    report_column = screen._overlay_card.content
    viewport = next(
        control for control in report_column.controls if isinstance(control, ft.Container)
    )
    available_width = (
        screen._overlay_card.width
        - screen._overlay_card.padding.left
        - screen._overlay_card.padding.right
    )
    assert viewport.width <= available_width
    assert screen.overlay.visible is True


def test_failed_run_does_not_offer_previous_runs_report_as_current():
    screen = CoursesScreen.__new__(CoursesScreen)
    screen.page = SimpleNamespace(update=lambda: None)
    screen.overlay = SimpleNamespace(visible=False)
    screen._overlay_card = ft.Container()
    screen._last_run_report = Path("C:/reports/previous-run.json")
    screen._current_run_report = None

    screen._show_completion_overlay("Ошибка запуска", is_error=True)

    buttons = [
        control
        for control in screen._overlay_card.content.controls
        if isinstance(control, ft.OutlinedButton)
    ]
    assert not any(button.content == "Открыть общий отчёт" for button in buttons)

    screen._current_run_report = Path("C:/reports/current-run.json")
    screen._show_completion_overlay("Загрузка с ошибкой", is_error=True)
    buttons = [
        control
        for control in screen._overlay_card.content.controls
        if isinstance(control, ft.OutlinedButton)
    ]
    assert any(button.content == "Открыть общий отчёт" for button in buttons)


def test_closing_diagnostics_restores_active_download_controls(tmp_path):
    path = tmp_path / "lesson.json"
    path.write_text(json.dumps({"error_code": "HTTP_403"}), encoding="utf-8")
    screen = CoursesScreen.__new__(CoursesScreen)
    screen.page = SimpleNamespace(update=lambda: None)
    screen.overlay = SimpleNamespace(visible=True)
    original_controls = ft.Column(controls=[ft.Text("Загрузка"), ft.OutlinedButton("Отмена")])
    screen._overlay_card = ft.Container(content=original_controls)

    screen._show_diagnostic_report(path, "Отчёт по уроку")
    report_controls = screen._overlay_card.content.controls
    report_controls[-1].on_click(None)

    assert screen._overlay_card.content is original_controls
    assert screen.overlay.visible is True


def test_closing_diagnostics_from_header_hides_only_the_report(tmp_path):
    path = tmp_path / "run.json"
    path.write_text(json.dumps({"failed_lessons": []}), encoding="utf-8")
    screen = CoursesScreen.__new__(CoursesScreen)
    screen.page = SimpleNamespace(update=lambda: None)
    screen.overlay = SimpleNamespace(visible=False)
    previous_content = ft.Text("Предыдущий экран")
    screen._overlay_card = ft.Container(content=previous_content)

    screen._show_diagnostic_report(path, "Последний отчёт")
    screen._overlay_card.content.controls[-1].on_click(None)

    assert screen._overlay_card.content is previous_content
    assert screen.overlay.visible is False


def test_download_scroll_targets_lesson_and_ignores_detached_control():
    class Scrollable:
        def __init__(self, *, detached: bool = False):
            self.detached = detached
            self.calls: list[dict[str, object]] = []

        async def scroll_to(self, **kwargs):
            if self.detached:
                raise RuntimeError("Control must be added to the page first")
            self.calls.append(kwargs)

    async def scenario() -> list[dict[str, object]]:
        screen = CoursesScreen.__new__(CoursesScreen)
        visible = Scrollable()
        screen._download_rows_column = visible
        await screen._scroll_download_to("https://school.example/lesson/7")
        screen._download_rows_column = Scrollable(detached=True)
        await screen._scroll_download_to("https://school.example/lesson/8")
        return visible.calls

    calls = asyncio.run(scenario())

    assert calls[0]["scroll_key"] == "https://school.example/lesson/7"
    assert calls[0]["duration"] == 650
    assert calls[0]["curve"] is ft.AnimationCurve.EASE_IN_OUT_CUBIC


def test_download_scroll_keeps_previous_lesson_at_top():
    screen = CoursesScreen.__new__(CoursesScreen)
    screen._download_rows = {
        "https://school.example/lesson/1": object(),
        "https://school.example/lesson/2": object(),
        "https://school.example/lesson/3": object(),
    }

    assert (
        screen._download_scroll_target("https://school.example/lesson/1")
        == "https://school.example/lesson/1"
    )
    assert (
        screen._download_scroll_target("https://school.example/lesson/3")
        == "https://school.example/lesson/2"
    )


def test_download_follow_pauses_on_manual_scroll_and_tracks_latest_lesson():
    class Page:
        def __init__(self):
            self.handlers = []

        def run_task(self, handler, *args):
            self.handlers.append((handler, args))
            return concurrent.futures.Future()

    screen = CoursesScreen.__new__(CoursesScreen)
    screen.page = Page()
    screen.state = SimpleNamespace(downloading=True)
    screen._download_follow_paused = False
    screen._active_lesson_url = "https://school.example/lesson/1"
    screen._download_scroll_task = concurrent.futures.Future()
    screen._download_follow_resume_task = None
    scheduled: list[str] = []
    screen._schedule_download_scroll = scheduled.append

    screen._on_download_rows_scroll(
        SimpleNamespace(
            event_type=ft.ScrollType.USER,
            direction=ft.ScrollDirection.REVERSE,
        )
    )
    first_resume_task = screen._download_follow_resume_task
    screen._on_download_rows_scroll(
        SimpleNamespace(
            event_type=ft.ScrollType.USER,
            direction=ft.ScrollDirection.IDLE,
        )
    )
    screen._follow_download_lesson("https://school.example/lesson/2")

    assert screen._download_follow_paused is True
    assert screen._download_scroll_task.cancelled() is True
    assert screen._active_lesson_url == "https://school.example/lesson/2"
    assert scheduled == []
    assert first_resume_task.cancelled() is True
    assert len(screen.page.handlers) == 2
    assert screen.page.handlers[0][0] == screen._resume_download_follow


def test_download_follow_resumes_at_active_lesson_after_pause(monkeypatch):
    from getcourse_downloader.presentation.flet.screens.courses import view as view_module

    async def scenario() -> tuple[bool, list[str]]:
        screen = CoursesScreen.__new__(CoursesScreen)
        screen.state = SimpleNamespace(downloading=True)
        screen._download_follow_resume_task = concurrent.futures.Future()
        screen._download_follow_paused = True
        screen._active_lesson_url = "https://school.example/lesson/9"
        screen._download_rows = {
            "https://school.example/lesson/8": object(),
            "https://school.example/lesson/9": object(),
        }
        calls: list[str] = []

        async def scroll(lesson_url: str) -> None:
            calls.append(lesson_url)

        screen._scroll_download_to = scroll
        await screen._resume_download_follow()
        return screen._download_follow_paused, calls

    monkeypatch.setattr(view_module, "_DOWNLOAD_FOLLOW_PAUSE_SECONDS", 0)
    paused, calls = asyncio.run(scenario())

    assert paused is False
    assert calls == ["https://school.example/lesson/8"]
