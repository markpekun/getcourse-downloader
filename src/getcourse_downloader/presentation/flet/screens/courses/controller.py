from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from getcourse_downloader import __version__
from getcourse_downloader.application.ports.browser import BrowserAuthorizationResetter
from getcourse_downloader.application.ports.repositories import (
    CourseRepository,
    SettingsRepository,
)
from getcourse_downloader.application.use_cases.download_lessons import DownloadLessons
from getcourse_downloader.domain.errors import DownloadConfigurationError, InvalidDataError
from getcourse_downloader.domain.events import DownloadEvent
from getcourse_downloader.domain.models import (
    Course,
    DownloadRequest,
    DownloadSummary,
    SelectedLesson,
    Settings,
    VideoQuality,
)
from getcourse_downloader.infrastructure.diagnostics.reports import DownloadDiagnostics
from getcourse_downloader.infrastructure.diagnostics.ui_lifecycle import trace_operation

DownloadFinished = Callable[[DownloadSummary], None]
DownloadFailed = Callable[[Exception], None]


class CoursesController:
    @staticmethod
    async def load_diagnostic(path: Path) -> str:
        return await asyncio.to_thread(DownloadDiagnostics.read, path)

    @staticmethod
    def describe_diagnostic(event: DownloadEvent) -> str:
        payload = DownloadDiagnostics.describe(event, app_version=__version__)
        return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)

    def __init__(
        self,
        courses: CourseRepository,
        settings: SettingsRepository,
        download_lessons: DownloadLessons,
        authorization: BrowserAuthorizationResetter | None = None,
    ) -> None:
        self._courses = courses
        self._settings = settings
        self._download_lessons = download_lessons
        self._authorization = authorization

    def load_courses(self) -> list[Course]:
        return self._courses.load()

    def has_courses(self) -> bool:
        return self._courses.has_courses()

    def delete_courses(self) -> None:
        self._courses.delete()

    async def clear_authorization(self) -> None:
        if self._authorization is None:
            raise DownloadConfigurationError("Очистка авторизации недоступна в этом запуске")
        await asyncio.to_thread(self._download_lessons.shutdown)
        await self._authorization.clear()

    def load_save_path(self) -> str:
        try:
            return self._settings.load().save_path
        except InvalidDataError:
            return Settings().save_path

    def save_save_path(self, path: str) -> None:
        self._settings.save(Settings(save_path=path))

    @staticmethod
    def make_request(
        lessons: Sequence[SelectedLesson], quality: str, save_path: str
    ) -> DownloadRequest:
        return DownloadRequest(
            lessons=tuple(lessons),
            quality=VideoQuality.parse(quality),
            save_path=Path(save_path),
        )

    def start_download(
        self,
        request: DownloadRequest,
        *,
        on_event: Callable[[DownloadEvent], None],
        on_finished: DownloadFinished,
        on_failed: DownloadFailed,
    ) -> None:
        def run() -> None:
            try:
                with trace_operation("download_execute", lessons=len(request.lessons)):
                    summary = self._download_lessons.execute(request, on_event)
            except Exception as error:
                on_failed(error)
            else:
                on_finished(summary)

        threading.Thread(target=run, name="download-worker-client", daemon=True).start()

    @trace_operation("authentication_continue")
    def continue_authentication(self) -> None:
        self._download_lessons.continue_authentication()

    @trace_operation("download_cancel_command")
    def cancel(self) -> None:
        self._download_lessons.cancel()

    @trace_operation("download_controller_shutdown")
    def shutdown(self, timeout: float = 6.0) -> None:
        self._download_lessons.shutdown(timeout)
