from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from getcourse_downloader.domain.errors import InvalidDataError
from getcourse_downloader.domain.models import Course, Settings


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidDataError(f"Не удалось прочитать {path.name}") from error


def _write_json_atomic(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


class JsonCourseRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[Course]:
        if not self._path.is_file():
            return []
        payload = _read_json(self._path)
        if not isinstance(payload, dict) or payload.get("schema_version") != 2:
            return []
        raw_courses = payload.get("courses")
        if not isinstance(raw_courses, list):
            raise InvalidDataError("Файл courses.json должен содержать список курсов")
        courses = [Course.from_dict(item) for item in raw_courses if isinstance(item, dict)]
        return courses

    def save(self, courses: Sequence[Course]) -> None:
        _write_json_atomic(
            self._path,
            {
                "schema_version": 2,
                "courses": [course.to_dict() for course in courses],
            },
        )

    def has_courses(self) -> bool:
        try:
            return bool(self.load())
        except InvalidDataError:
            return False

    def delete(self) -> None:
        self._path.unlink(missing_ok=True)


class JsonSettingsRepository:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> Settings:
        if not self._path.is_file():
            return Settings()
        payload = _read_json(self._path)
        if not isinstance(payload, dict):
            return Settings()
        return Settings.from_dict(payload)

    def save(self, settings: Settings) -> None:
        _write_json_atomic(self._path, settings.to_dict())
