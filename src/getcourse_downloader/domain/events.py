from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from getcourse_downloader.domain.errors import InvalidDataError


class DownloadEventType(StrEnum):
    LOG = "log"
    PROGRESS = "progress"
    AUTH_REQUIRED = "auth_required"
    AUTHENTICATED = "authenticated"
    LESSON_STARTED = "lesson_started"
    VIDEO_FOUND = "video_found"
    LESSON_COMPLETED = "lesson_completed"
    LESSON_SKIPPED = "lesson_skipped"
    LESSON_NO_VIDEO = "lesson_no_video"
    LESSON_FAILED = "lesson_failed"
    SUMMARY = "summary"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DownloadEvent:
    type: DownloadEventType
    message: str = ""
    stage: str = ""
    lesson: str = ""
    lesson_url: str = ""
    course_path: tuple[str, ...] = ()
    video_index: int | None = None
    video_total: int | None = None
    current: int | None = None
    total: int | None = None
    downloaded: int | None = None
    already_present: int | None = None
    no_video: int | None = None
    failed_count: int | None = None
    cancelled: int | None = None
    quality: str = ""
    speed_bps: float | None = None
    level: str = "info"
    error_code: str = ""
    source_host: str = ""
    diagnostic_report: str = ""

    def to_json(self) -> str:
        payload = {"protocol_version": 2, **asdict(self)}
        payload["type"] = self.type.value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DownloadEvent:
        if data.get("protocol_version") != 2:
            raise InvalidDataError("Поддерживается только protocol_version=2")
        try:
            event_type = DownloadEventType(str(data["type"]))
        except (KeyError, ValueError) as error:
            raise InvalidDataError("Неизвестный тип события worker") from error

        def optional_int(key: str) -> int | None:
            value = data.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        raw_course_path = data.get("course_path", [])
        if not isinstance(raw_course_path, list) or not all(
            isinstance(part, str) for part in raw_course_path
        ):
            raise InvalidDataError("Поле события 'course_path' должно быть списком строк")

        return cls(
            type=event_type,
            message=str(data.get("message", "")),
            stage=str(data.get("stage", "")),
            lesson=str(data.get("lesson", "")),
            lesson_url=str(data.get("lesson_url", "")),
            course_path=tuple(raw_course_path),
            video_index=optional_int("video_index"),
            video_total=optional_int("video_total"),
            current=optional_int("current"),
            total=optional_int("total"),
            downloaded=optional_int("downloaded"),
            already_present=optional_int("already_present"),
            no_video=optional_int("no_video"),
            failed_count=optional_int("failed_count"),
            cancelled=optional_int("cancelled"),
            quality=str(data.get("quality", "")),
            speed_bps=(
                float(data["speed_bps"])
                if isinstance(data.get("speed_bps"), (int, float))
                and not isinstance(data.get("speed_bps"), bool)
                else None
            ),
            level=str(data.get("level", "info")),
            error_code=str(data.get("error_code", "")),
            source_host=str(data.get("source_host", "")),
            diagnostic_report=str(data.get("diagnostic_report", "")),
        )

    @classmethod
    def from_json(cls, line: str) -> DownloadEvent:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise InvalidDataError("Worker вернул некорректный JSON") from error
        if not isinstance(payload, dict):
            raise InvalidDataError("Событие worker должно быть JSON-объектом")
        return cls.from_dict(payload)
