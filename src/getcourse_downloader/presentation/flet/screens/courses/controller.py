from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from pathlib import Path

from getcourse_downloader.application.ports.repositories import (
    CourseRepository,
    SettingsRepository,
)
from getcourse_downloader.application.use_cases.download_lessons import DownloadLessons
from getcourse_downloader.domain.errors import InvalidDataError
from getcourse_downloader.domain.events import DownloadEvent
from getcourse_downloader.domain.models import (
    Course,
    DownloadRequest,
    DownloadSummary,
    SelectedLesson,
    Settings,
    VideoQuality,
)

DownloadFinished = Callable[[DownloadSummary], None]
DownloadFailed = Callable[[Exception], None]


class CoursesController:
    def __init__(
        self,
        courses: CourseRepository,
        settings: SettingsRepository,
        download_lessons: DownloadLessons,
    ) -> None:
        self._courses = courses
        self._settings = settings
        self._download_lessons = download_lessons

    def load_courses(self) -> list[Course]:
        return self._courses.load()

    def has_courses(self) -> bool:
        return self._courses.has_courses()

    def delete_courses(self) -> None:
        self._courses.delete()

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
                summary = self._download_lessons.execute(request, on_event)
            except Exception as error:
                on_failed(error)
            else:
                on_finished(summary)

        threading.Thread(target=run, name="download-worker-client", daemon=True).start()

    def continue_authentication(self) -> None:
        self._download_lessons.continue_authentication()

    def cancel(self) -> None:
        self._download_lessons.cancel()

    def shutdown(self, timeout: float = 6.0) -> None:
        self._download_lessons.shutdown(timeout)
