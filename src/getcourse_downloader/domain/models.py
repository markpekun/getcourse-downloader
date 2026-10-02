from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from getcourse_downloader.domain.errors import InvalidDataError


def _required_text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InvalidDataError(f"Поле {key!r} должно быть непустой строкой")
    return value.strip()


class VideoQuality(StrEnum):
    AUTO = "auto"
    P1080 = "1080"
    P720 = "720"
    P480 = "480"
    P360 = "360"

    @classmethod
    def parse(cls, value: object) -> VideoQuality:
        try:
            return cls(str(value or cls.AUTO))
        except ValueError as error:
            raise InvalidDataError(f"Неизвестное качество видео: {value!r}") from error


class MediaKind(StrEnum):
    VIDEO = "video"
    AUDIO = "audio"

    @classmethod
    def parse(cls, value: object) -> MediaKind:
        try:
            return cls(str(value))
        except ValueError as error:
            raise InvalidDataError(f"Неизвестный тип медиа: {value!r}") from error


@dataclass(frozen=True, slots=True)
class MediaSelection:
    """The media kinds a single download request is allowed to process."""

    kinds: tuple[MediaKind, ...]

    def __post_init__(self) -> None:
        parsed = tuple(MediaKind.parse(kind) for kind in self.kinds)
        if not parsed:
            raise InvalidDataError("Хотя бы один тип медиа должен быть выбран")
        if len(set(parsed)) != len(parsed):
            raise InvalidDataError("Типы медиа не должны повторяться")
        object.__setattr__(
            self,
            "kinds",
            tuple(kind for kind in MediaKind if kind in parsed),
        )

    @classmethod
    def video_only(cls) -> MediaSelection:
        return cls((MediaKind.VIDEO,))

    @classmethod
    def audio_only(cls) -> MediaSelection:
        return cls((MediaKind.AUDIO,))

    @classmethod
    def video_and_audio(cls) -> MediaSelection:
        return cls((MediaKind.VIDEO, MediaKind.AUDIO))

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> MediaSelection:
        raw_kinds = data.get("kinds")
        if not isinstance(raw_kinds, list):
            raise InvalidDataError("Поле 'kinds' должно быть списком")
        return cls(tuple(MediaKind.parse(value) for value in raw_kinds))

    def includes(self, kind: MediaKind) -> bool:
        return kind in self.kinds

    def to_dict(self) -> dict[str, list[str]]:
        return {"kinds": [kind.value for kind in self.kinds]}


@dataclass(frozen=True, slots=True)
class Lesson:
    title: str
    url: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Lesson:
        return cls(title=_required_text(data, "title"), url=_required_text(data, "url"))

    def to_dict(self) -> dict[str, str]:
        return {"title": self.title, "url": self.url}


@dataclass(frozen=True, slots=True)
class Course:
    title: str
    lessons: tuple[Lesson, ...] = ()
    url: str = ""
    children: tuple[Course, ...] = ()

    @property
    def lesson_count(self) -> int:
        return len(self.lessons) + sum(child.lesson_count for child in self.children)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Course:
        raw_lessons = data.get("lessons", [])
        raw_children = data.get("children", [])
        if not isinstance(raw_lessons, list):
            raise InvalidDataError("Поле 'lessons' должно быть списком")
        if not isinstance(raw_children, list):
            raise InvalidDataError("Поле 'children' должно быть списком")
        return cls(
            title=_required_text(data, "title"),
            lessons=tuple(Lesson.from_dict(item) for item in raw_lessons if isinstance(item, dict)),
            url=_required_text(data, "url"),
            children=tuple(
                Course.from_dict(item) for item in raw_children if isinstance(item, dict)
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "url": self.url,
            "lessons": [lesson.to_dict() for lesson in self.lessons],
            "children": [child.to_dict() for child in self.children],
        }


@dataclass(frozen=True, slots=True)
class SelectedLesson:
    course_path: tuple[str, ...]
    lesson: Lesson

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SelectedLesson:
        raw_lesson = data.get("lesson")
        if not isinstance(raw_lesson, dict):
            raise InvalidDataError("Поле 'lesson' должно быть объектом")
        raw_path = data.get("course_path")
        if not isinstance(raw_path, list) or not raw_path:
            raise InvalidDataError("Поле 'course_path' должно быть непустым списком")
        path = tuple(part.strip() for part in raw_path if isinstance(part, str) and part.strip())
        if len(path) != len(raw_path):
            raise InvalidDataError("Все элементы 'course_path' должны быть непустыми строками")
        return cls(course_path=path, lesson=Lesson.from_dict(raw_lesson))

    def to_dict(self) -> dict[str, object]:
        return {"course_path": list(self.course_path), "lesson": self.lesson.to_dict()}


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    lessons: tuple[SelectedLesson, ...]
    quality: VideoQuality
    save_path: Path
    media_selection: MediaSelection = field(default_factory=MediaSelection.video_only)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> DownloadRequest:
        if data.get("schema_version") != 2:
            raise InvalidDataError("Поддерживается только schema_version=2")
        raw_lessons = data.get("lessons")
        if not isinstance(raw_lessons, list):
            raise InvalidDataError("Поле 'lessons' должно быть списком")
        raw_media_selection = data.get("media_selection")
        if raw_media_selection is None:
            media_selection = MediaSelection.video_only()
        elif isinstance(raw_media_selection, dict):
            media_selection = MediaSelection.from_dict(raw_media_selection)
        else:
            raise InvalidDataError("Поле 'media_selection' должно быть объектом")
        return cls(
            lessons=tuple(
                SelectedLesson.from_dict(item) for item in raw_lessons if isinstance(item, dict)
            ),
            quality=VideoQuality.parse(data.get("quality")),
            save_path=Path(_required_text(data, "save_path")),
            media_selection=media_selection,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "lessons": [item.to_dict() for item in self.lessons],
            "quality": self.quality.value,
            "save_path": str(self.save_path),
            "media_selection": self.media_selection.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class DownloadSummary:
    total: int
    downloaded: int
    already_present: int = 0
    no_video: int = 0
    failed: tuple[str, ...] = ()
    cancelled: int = 0

    @property
    def processed(self) -> int:
        return self.downloaded + self.already_present + self.no_video + len(self.failed)

    @property
    def successful(self) -> bool:
        return self.processed == self.total and not self.failed and not self.cancelled


@dataclass(frozen=True, slots=True)
class Settings:
    save_path: str = "downloads"

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Settings:
        save_path = data.get("save_path", "downloads")
        return cls(save_path=save_path if isinstance(save_path, str) else "downloads")

    def to_dict(self) -> dict[str, str]:
        return {"save_path": self.save_path}
