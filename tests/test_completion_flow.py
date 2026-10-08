import asyncio
from dataclasses import fields
from types import SimpleNamespace

import flet as ft
import pytest

from getcourse_downloader.domain.models import DownloadSummary
from getcourse_downloader.presentation.flet.screens.courses.state import CoursesViewState
from getcourse_downloader.presentation.flet.screens.courses.view import CoursesScreen


def _walk(control):
    if not isinstance(control, ft.Control):
        return
    yield control
    for field in fields(control):
        value = getattr(control, field.name)
        if isinstance(value, ft.Control):
            yield from _walk(value)
        elif isinstance(value, (list, tuple)):
            for child in value:
                yield from _walk(child)


def _texts(screen):
    return [c.value for c in _walk(screen._overlay_card.content) if isinstance(c, ft.Text)]


def _content(screen):
    content = screen._overlay_card.content
    return content.content if isinstance(content, ft.AnimatedSwitcher) else content


def _action(screen, key):
    return next(c for c in _walk(screen._overlay_card.content) if c.key == key)


def _screen():
    screen = CoursesScreen.__new__(CoursesScreen)
    opened = []

    async def launch_url(url):
        opened.append(url)

    screen.page = SimpleNamespace(update=lambda: None, launch_url=launch_url)
    screen.state = CoursesViewState(save_path="D:/demo")
    screen.overlay = ft.Container(visible=False, content=ft.Container())
    screen._overlay_card = ft.Container(width=600, padding=24)
    screen._diagnostic_reports_by_title = {}
    return screen, opened


def test_result_has_next_without_close_or_support_links():
    screen, _ = _screen()
    screen._show_completion_overlay("Скачано: 2\nВсего выбрано: 2")

    assert screen.overlay.visible
    assert any(c.key == "completion-next" for c in _walk(screen._overlay_card.content))
    assert not any(c.key == "completion-close" for c in _walk(screen._overlay_card.content))
    assert not any(c.key == "completion-github" for c in _walk(screen._overlay_card.content))
    screen._close_completion_overlay()
    assert screen.overlay.visible


def test_auto_height_download_enters_result_without_size_tween_then_animates_next_steps():
    screen, _ = _screen()
    screen._overlay_card.height = None
    screen._overlay_card.animate = ft.Animation(300)

    screen._show_completion_overlay("Скачано: 1\nВсего выбрано: 1")

    # Flet cannot interpolate an intrinsic height into a fixed height: the
    # expanding result would occupy the viewport until the tween finishes.
    assert screen._overlay_card.height is not None
    assert screen._overlay_card.animate.duration == 0
    _action(screen, "completion-next").on_click(None)
    assert screen._overlay_card.animate.duration > 0
    _action(screen, "completion-help").on_click(None)
    assert screen._overlay_card.animate.duration > 0


def test_help_is_optional_and_back_preserves_the_result():
    screen, _ = _screen()
    screen._show_completion_overlay("Скачано: 1\nОшибки: 1", is_warning=True, failed=["✗ Урок"])
    result_content = _content(screen)
    _action(screen, "completion-next").on_click(None)
    support_content = _content(screen)
    assert any(c.key == "completion-close" for c in _walk(support_content))
    assert not any("Continue with Google" in str(t) for t in _texts(screen))

    _action(screen, "completion-help").on_click(None)
    assert any("Continue with Google" in str(t) for t in _texts(screen))
    _action(screen, "completion-back").on_click(None)
    assert _content(screen) is support_content
    assert screen._completion_flow.result_content is result_content


@pytest.mark.parametrize("instruction", [False, True])
def test_support_and_instruction_can_be_closed(instruction):
    screen, _ = _screen()
    screen._show_completion_overlay("Скачано: 1")
    _action(screen, "completion-next").on_click(None)
    if instruction:
        _action(screen, "completion-help").on_click(None)
    _action(screen, "completion-close").on_click(None)
    assert not screen.overlay.visible


def test_links_open_existing_addresses_without_changing_step():
    screen, opened = _screen()
    screen._show_completion_overlay("Скачано: 1")
    _action(screen, "completion-next").on_click(None)
    content = _content(screen)
    asyncio.run(_action(screen, "completion-contact").on_click(None))
    asyncio.run(_action(screen, "completion-github").on_click(None))
    assert opened == [
        "https://t.me/No_Resp_404",
        "https://github.com/markpekun/getcourse-downloader",
    ]
    assert _content(screen) is content
    _action(screen, "completion-help").on_click(None)
    asyncio.run(_action(screen, "completion-github").on_click(None))
    assert opened[-1] == "https://github.com/markpekun/getcourse-downloader"


def test_new_completion_starts_with_new_result():
    screen, _ = _screen()
    screen._show_completion_overlay("Скачано: 1")
    _action(screen, "completion-next").on_click(None)
    _action(screen, "completion-help").on_click(None)
    screen._show_completion_overlay("Скачано: 3")
    assert "Скачано: 3" in _texts(screen)
    assert "Скачано: 1" not in _texts(screen)
    assert any(c.key == "completion-next" for c in _walk(screen._overlay_card.content))


def test_instructions_cannot_skip_the_result():
    screen, _ = _screen()
    screen._show_completion_overlay("Скачано: 1")
    result_content = _content(screen)
    screen._completion_flow.show_instructions()
    assert _content(screen) is result_content


def test_next_download_replaces_the_completion_and_resets_its_height():
    screen, _ = _screen()
    screen._auth_overlay_task = None
    screen._download_title = ft.Text()
    screen._download_rows_container = ft.Container()
    screen._continue_btn = ft.TextButton("Продолжить")
    screen._cancel_btn = ft.OutlinedButton("Отмена")
    screen._show_completion_overlay("Скачано: 1")
    _action(screen, "completion-next").on_click(None)

    screen._switch_overlay_to_download()

    assert screen._completion_flow is None
    assert screen._overlay_card.height is None
    assert screen._download_title.value == "Подготовка"
    assert screen._cancel_btn in list(_walk(screen._overlay_card.content))
    assert not any(c.key == "completion-github" for c in _walk(screen._overlay_card.content))


@pytest.mark.parametrize(
    ("summary", "title"),
    [
        (DownloadSummary(total=2, downloaded=2), "Готово! Видео скачаны"),
        (DownloadSummary(total=2, downloaded=1, failed=("Урок",)), "Завершено с предупреждениями"),
        (DownloadSummary(total=1, downloaded=0, failed=("Урок",)), "Ошибка загрузки"),
        (DownloadSummary(total=1, downloaded=0, no_video=1), "Завершено с предупреждениями"),
        (DownloadSummary(total=1, downloaded=0, cancelled=1), "Загрузка отменена"),
    ],
)
def test_summary_outcomes_keep_accurate_titles_and_statistics(summary, title):
    screen, _ = _screen()
    screen._cancel_btn = ft.OutlinedButton("Отмена")
    screen._speed_text = ft.Text()
    screen._download_rows = {}
    screen._reset_download_follow = lambda: None
    screen._finish_summary(summary)
    assert title in _texts(screen)
    assert any(f"Скачано: {summary.downloaded}" in str(t) for t in _texts(screen))
    _action(screen, "completion-next").on_click(None)
    assert screen._completion_flow.result.message.startswith(f"Скачано: {summary.downloaded}")


def test_failed_lesson_diagnostic_returns_to_same_result(tmp_path):
    screen, _ = _screen()
    report = tmp_path / "report.json"
    report.write_text('{"error_code":"HTTP_403"}', encoding="utf-8")
    screen._diagnostic_reports_by_title["Урок"] = report
    screen._show_completion_overlay("Ошибки: 1", is_warning=True, failed=["✗ Урок"])
    original = screen._overlay_card.content
    row = next(c for c in _walk(original) if isinstance(c, ft.Container) and c.on_click)
    row.on_click(None)
    screen._close_diagnostic_report()
    assert screen._overlay_card.content is original
    assert screen.overlay.visible
