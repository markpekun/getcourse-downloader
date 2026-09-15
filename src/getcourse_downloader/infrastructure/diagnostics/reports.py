from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType

_URL_PATTERN = re.compile(r"https?://[^\s\]\[\"']+", re.IGNORECASE)


def _without_query(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _sanitize_text(value: str) -> str:
    return _URL_PATTERN.sub(lambda match: _without_query(match.group(0)), value)


class DownloadDiagnostics:
    """Writes one sanitized JSON report per failed lesson and download run."""

    def __init__(self, data_directory: Path, *, app_version: str = "") -> None:
        self._root = data_directory / "diagnostics"
        self._lesson_root = self._root / "lessons"
        self._app_version = app_version
        self._failed_lessons: dict[str, dict[str, object]] = {}
        self._no_video_lessons: dict[str, dict[str, object]] = {}
        self._global_errors: list[dict[str, str]] = []

    def record(self, event: DownloadEvent) -> Path | None:
        reportable = {
            DownloadEventType.ERROR,
            DownloadEventType.LESSON_FAILED,
            DownloadEventType.LESSON_NO_VIDEO,
        }
        if event.type not in reportable:
            return None
        if not event.lesson:
            if event.type is not DownloadEventType.ERROR:
                return None
            self._global_errors.append(
                {
                    "error_code": event.error_code or "DOWNLOAD_FAILED",
                    "stage": event.stage,
                    "summary": _sanitize_text(event.message),
                }
            )
            return None
        key = event.lesson_url or event.lesson
        reports = (
            self._no_video_lessons
            if event.type is DownloadEventType.LESSON_NO_VIDEO
            else self._failed_lessons
        )
        if key in reports:
            return self._lesson_root / f"{self._file_stem(key)}.json"
        report = self._lesson_report(event)
        reports[key] = report
        self._lesson_root.mkdir(parents=True, exist_ok=True)
        target = self._lesson_root / f"{self._file_stem(key)}.json"
        self._write_json(target, report)
        return target

    def finish(
        self,
        *,
        total: int,
        downloaded: int,
        already_present: int,
        no_video: int,
    ) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        report = {
            "counts": {
                "total": total,
                "downloaded": downloaded,
                "already_present": already_present,
                "no_video": no_video,
                "failed": len(self._failed_lessons),
            },
            "failed_lessons": list(self._failed_lessons.values()),
            "no_video_lessons": list(self._no_video_lessons.values()),
            "global_errors": self._global_errors,
        }
        target = self._root / "last-run.json"
        self._write_json(target, report)
        return target

    def _lesson_report(self, event: DownloadEvent) -> dict[str, object]:
        source_host = event.source_host or urlsplit(event.lesson_url).netloc
        return {
            "app_version": self._app_version,
            "error_code": event.error_code or self._default_error_code(event),
            "lesson": event.lesson,
            "lesson_url": _without_query(event.lesson_url),
            "progress": {"current": event.current, "total": event.total},
            "quality": event.quality,
            "source_host": source_host,
            "stage": event.stage,
            "summary": _sanitize_text(event.message),
        }

    @staticmethod
    def _default_error_code(event: DownloadEvent) -> str:
        if event.type is DownloadEventType.LESSON_NO_VIDEO:
            return "VIDEO_NOT_FOUND"
        return "DOWNLOAD_FAILED"

    @staticmethod
    def _file_stem(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]

    @staticmethod
    def _write_json(path: Path, data: Mapping[str, object]) -> None:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
