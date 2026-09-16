import json

import pytest

from getcourse_downloader.domain.errors import InvalidDataError
from getcourse_downloader.domain.models import Course, Lesson, Settings
from getcourse_downloader.infrastructure.storage.json_repositories import (
    JsonCourseRepository,
    JsonSettingsRepository,
)


def test_course_repository_round_trip(tmp_path):
    repository = JsonCourseRepository(tmp_path / "courses.json")
    expected = [
        Course(
            title="Python",
            lessons=(Lesson("Введение", "https://example.com/lesson/1"),),
            url="https://example.com/stream/1",
            children=(
                Course(
                    title="Модуль",
                    lessons=(Lesson("Практика", "https://example.com/lesson/2"),),
                    url="https://example.com/stream/2",
                ),
            ),
        )
    ]
    repository.save(expected)
    assert repository.load() == expected
    assert repository.has_courses() is True
    payload = json.loads(repository.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2


def test_course_repository_ignores_legacy_flat_cache(tmp_path):
    path = tmp_path / "courses.json"
    path.write_text(
        '[{"course_title":"Старый курс","lessons":[]}]',
        encoding="utf-8",
    )
    repository = JsonCourseRepository(path)

    assert repository.load() == []
    assert repository.has_courses() is False


def test_course_repository_invalid_json_is_not_a_course(tmp_path):
    path = tmp_path / "courses.json"
    path.write_text("{invalid", encoding="utf-8")
    repository = JsonCourseRepository(path)
    assert repository.has_courses() is False
    with pytest.raises(InvalidDataError):
        repository.load()


def test_settings_repository_round_trip(tmp_path):
    repository = JsonSettingsRepository(tmp_path / "settings.json")
    assert repository.load() == Settings()
    repository.save(Settings("D:/videos"))
    assert repository.load() == Settings("D:/videos")


def test_settings_repository_invalid_value_uses_default(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"save_path": 123}', encoding="utf-8")
    assert JsonSettingsRepository(path).load() == Settings()


def test_course_repository_invalid_utf8_is_reported_as_corrupt_data(tmp_path):
    path = tmp_path / "courses.json"
    path.write_bytes(b"\xff\xfe\x00")
    repository = JsonCourseRepository(path)

    assert repository.has_courses() is False
    with pytest.raises(InvalidDataError):
        repository.load()


def test_settings_repository_invalid_utf8_is_reported_as_corrupt_data(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(InvalidDataError):
        JsonSettingsRepository(path).load()
