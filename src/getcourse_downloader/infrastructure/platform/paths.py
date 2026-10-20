from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

APP_NAME = "GetCourseVideoDownloader"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def project_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[4]


@dataclass(frozen=True, slots=True)
class AppPaths:
    data: Path
    session: Path
    resources: Path

    @classmethod
    def discover(cls) -> AppPaths:
        root = project_root()
        user_root = user_data_path(APP_NAME, appauthor=False, roaming=False)
        return cls(
            data=user_root / "data",
            session=user_root / "browser-profile",
            resources=root / "resources",
        )

    @property
    def courses_file(self) -> Path:
        return self.data / "courses.json"

    @property
    def settings_file(self) -> Path:
        return self.data / "settings.json"

    @property
    def downloads_file(self) -> Path:
        return self.data / "downloads.json"

    @property
    def worker_log_file(self) -> Path:
        return self.data / "worker.log"

    @property
    def ui_lifecycle_log_file(self) -> Path:
        return self.data / "ui-lifecycle.log"

    def ensure_runtime_directories(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        self.session.mkdir(parents=True, exist_ok=True)
