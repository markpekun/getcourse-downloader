import asyncio
import inspect
from types import SimpleNamespace

import flet as ft
import pytest

from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.presentation.flet.screens.courses.state import CoursesViewState
from getcourse_downloader.presentation.flet.screens.courses.view import CoursesScreen


def _click(handler):
    result = handler(None)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _screen():
    calls = []
    dialogs = []

    def delete_courses():
        calls.append("delete-courses")

    async def clear_authorization():
        calls.append("clear-authorization")

    async def navigate():
        calls.append("navigate")

    screen = CoursesScreen.__new__(CoursesScreen)
    screen.state = CoursesViewState(save_path="D:/demo")
    screen._confirmation_dialog = None
    screen._controller = SimpleNamespace(
        delete_courses=delete_courses, clear_authorization=clear_authorization
    )
    screen._on_navigate_start = navigate
    screen.page = SimpleNamespace(
        show_dialog=dialogs.append,
        pop_dialog=lambda: calls.append("close-dialog"),
        update=lambda: None,
    )
    screen._show_snack = lambda text, is_error=False: calls.append((text, is_error))
    return screen, calls, dialogs


def test_delete_courses_first_shows_warning_without_deleting_or_navigating():
    screen, calls, dialogs = _screen()

    _click(screen._delete_courses)

    assert calls == []
    assert len(dialogs) == 1
    assert "заново" in dialogs[0].content.content.value
    _click(dialogs[0].actions[0].on_click)
    assert calls == ["close-dialog"]


def test_confirming_course_clear_deletes_only_list_then_navigates():
    screen, calls, dialogs = _screen()
    _click(screen._delete_courses)

    _click(dialogs[0].actions[1].on_click)

    assert calls == ["close-dialog", "delete-courses", "navigate"]
    assert not screen.state.clearing


def test_cookie_button_requires_confirmation_and_keeps_course_list():
    screen, calls, dialogs = _screen()
    _click(screen._clear_authorization)
    assert calls == []
    assert len(dialogs) == 1
    _click(dialogs[0].actions[1].on_click)

    assert "clear-authorization" in calls
    assert "delete-courses" not in calls
    assert "navigate" not in calls
    assert not screen.state.clearing
    assert calls[-1][1] is False


@pytest.mark.parametrize("action", ["_delete_courses", "_clear_authorization"])
def test_clear_actions_are_blocked_during_download(action):
    screen, calls, dialogs = _screen()
    screen.state.downloading = True

    _click(getattr(screen, action))

    assert dialogs == []
    assert "delete-courses" not in calls
    assert "clear-authorization" not in calls


def test_repeated_clear_clicks_do_not_stack_dialogs():
    screen, calls, dialogs = _screen()
    _click(screen._delete_courses)
    _click(screen._clear_authorization)

    assert len(dialogs) == 1
    assert calls == []


def test_profile_busy_error_is_visible_without_false_success():
    screen, calls, dialogs = _screen()

    async def fail():
        raise ExternalServiceError("Профиль браузера занят", code="BROWSER_PROFILE_BUSY")

    screen._controller.clear_authorization = fail
    _click(screen._clear_authorization)
    _click(dialogs[0].actions[1].on_click)

    assert calls[-1] == ("Профиль браузера занят", True)
    assert not screen.state.clearing
    assert "navigate" not in calls


def test_confirmation_does_not_clear_after_download_begins():
    screen, calls, dialogs = _screen()
    _click(screen._delete_courses)
    screen.state.downloading = True
    _click(dialogs[0].actions[1].on_click)

    assert "delete-courses" not in calls
    assert "navigate" not in calls


def test_course_delete_failure_keeps_screen_and_reports_error():
    screen, calls, dialogs = _screen()

    def fail():
        raise OSError("synthetic filesystem failure")

    screen._controller.delete_courses = fail
    _click(screen._delete_courses)
    _click(dialogs[0].actions[1].on_click)

    assert "navigate" not in calls
    assert calls[-1][1] is True
    assert not screen.state.clearing


def test_dismissing_dialog_never_clears_and_allows_reopening():
    screen, calls, dialogs = _screen()
    _click(screen._delete_courses)
    _click(dialogs[0].on_dismiss)
    _click(screen._clear_authorization)

    assert calls == []
    assert len(dialogs) == 2


def test_delayed_dismiss_of_old_dialog_keeps_new_confirmation():
    screen, calls, dialogs = _screen()
    _click(screen._delete_courses)
    _click(dialogs[0].actions[0].on_click)
    _click(screen._clear_authorization)
    dialogs[0].on_dismiss(SimpleNamespace(control=dialogs[0]))

    assert screen._confirmation_dialog is dialogs[1]
    assert calls == ["close-dialog"]


def test_double_confirm_does_not_delete_twice():
    screen, calls, dialogs = _screen()
    _click(screen._delete_courses)
    _click(dialogs[0].actions[1].on_click)
    _click(dialogs[0].actions[1].on_click)
    assert calls.count("delete-courses") == 1


def test_header_has_separate_course_and_cookie_controls():
    screen, calls, dialogs = _screen()
    screen.data = []
    screen._speed_text = ft.Text("Средняя: —")
    screen._build_header()

    _click(screen._clear_courses_button.on_click)
    assert dialogs[0].key == "confirm-clear-courses"
    _click(dialogs[0].actions[0].on_click)
    _click(screen._clear_authorization_button.on_click)
    assert dialogs[1].key == "confirm-clear-authorization"
    assert "delete-courses" not in calls


def test_download_cannot_start_while_authorization_is_being_cleared():
    screen, calls, dialogs = _screen()
    screen.state.clearing = True
    # No course/controller attributes are needed: the busy guard must run first.
    _click(screen._start_download)
    assert calls == []
    assert dialogs == []


def test_clear_notification_is_registered_with_current_flet_dialog_api():
    screen, _, dialogs = _screen()
    CoursesScreen._show_snack(screen, "Авторизация очищена")

    assert len(dialogs) == 1
    assert isinstance(dialogs[0], ft.SnackBar)
    assert dialogs[0].content.controls[1].value == "Авторизация очищена"
