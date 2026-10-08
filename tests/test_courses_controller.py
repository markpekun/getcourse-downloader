from __future__ import annotations

import asyncio

import pytest

from getcourse_downloader.application.use_cases.download_lessons import DownloadLessons
from getcourse_downloader.domain.errors import ExternalServiceError, InvalidDataError
from getcourse_downloader.domain.models import Settings
from getcourse_downloader.presentation.flet.screens.courses.controller import CoursesController


class _Courses:
    def load(self):
        return []

    def has_courses(self):
        return False

    def delete(self):
        return None


class _CorruptSettings:
    def __init__(self) -> None:
        self.load_calls = 0

    def load(self) -> Settings:
        self.load_calls += 1
        raise InvalidDataError("Не удалось прочитать settings.json")

    def save(self, settings: Settings) -> None:
        raise AssertionError(f"Corrupt settings must not be overwritten: {settings}")


class _Gateway:
    def run(self, request, on_event):
        raise AssertionError("Download is not part of this test")

    def continue_authentication(self):
        return None

    def cancel(self):
        return None

    def shutdown(self, timeout: float = 6.0):
        return None


def test_corrupt_settings_do_not_prevent_courses_screen_from_starting():
    settings = _CorruptSettings()
    controller = CoursesController(_Courses(), settings, DownloadLessons(_Gateway()))

    assert controller.load_save_path() == Settings().save_path
    assert settings.load_calls == 1


def test_clear_authorization_waits_for_worker_shutdown_before_profile_reset():
    calls = []

    class Gateway(_Gateway):
        def shutdown(self, timeout=6.0):
            calls.append("worker-stopped")

    class Authorization:
        async def clear(self):
            calls.append("profile-cleared")

    controller = CoursesController(
        _Courses(), _CorruptSettings(), DownloadLessons(Gateway()), Authorization()
    )
    asyncio.run(controller.clear_authorization())

    assert calls == ["worker-stopped", "profile-cleared"]


def test_clear_authorization_preserves_profile_busy_error():
    class Authorization:
        async def clear(self):
            raise ExternalServiceError("Профиль занят", code="BROWSER_PROFILE_BUSY")

    controller = CoursesController(
        _Courses(), _CorruptSettings(), DownloadLessons(_Gateway()), Authorization()
    )
    with pytest.raises(ExternalServiceError) as caught:
        asyncio.run(controller.clear_authorization())
    assert caught.value.code == "BROWSER_PROFILE_BUSY"
