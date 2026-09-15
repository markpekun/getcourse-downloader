from __future__ import annotations

from dataclasses import dataclass

from getcourse_downloader.application.use_cases.discover_courses import DiscoverCourses
from getcourse_downloader.application.use_cases.download_lessons import DownloadLessons
from getcourse_downloader.infrastructure.browser.playwright import PlaywrightBrowserFactory
from getcourse_downloader.infrastructure.getcourse.discovery import GetCourseDiscoverer
from getcourse_downloader.infrastructure.platform.paths import AppPaths
from getcourse_downloader.infrastructure.storage.json_repositories import (
    JsonCourseRepository,
    JsonSettingsRepository,
)
from getcourse_downloader.infrastructure.worker.subprocess_gateway import (
    SubprocessDownloadGateway,
)


@dataclass(frozen=True, slots=True)
class AppContainer:
    paths: AppPaths
    courses: JsonCourseRepository
    settings: JsonSettingsRepository
    discover_courses: DiscoverCourses
    download_lessons: DownloadLessons


def build_container() -> AppContainer:
    paths = AppPaths.discover()
    paths.ensure_runtime_directories()
    courses = JsonCourseRepository(paths.courses_file)
    settings = JsonSettingsRepository(paths.settings_file)
    browsers = PlaywrightBrowserFactory(paths)
    return AppContainer(
        paths=paths,
        courses=courses,
        settings=settings,
        discover_courses=DiscoverCourses(GetCourseDiscoverer(browsers), courses),
        download_lessons=DownloadLessons(
            SubprocessDownloadGateway(diagnostics_directory=paths.data)
        ),
    )
