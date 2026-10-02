import pytest

from getcourse_downloader.domain.errors import InvalidDataError
from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import (
    Course,
    DownloadRequest,
    Lesson,
    MediaKind,
    MediaSelection,
    SelectedLesson,
    VideoQuality,
)


def test_recursive_course_json_round_trip():
    payload = {
        "title": "Курс",
        "url": "https://example.com/stream/1",
        "lessons": [],
        "children": [
            {
                "title": "Модуль",
                "url": "https://example.com/stream/2",
                "lessons": [{"title": "Урок", "url": "https://example.com/lesson/1"}],
                "children": [],
            }
        ],
    }
    course = Course.from_dict(payload)
    assert course.lesson_count == 1
    assert course.children[0].lessons == (Lesson("Урок", "https://example.com/lesson/1"),)
    assert course.to_dict() == payload


def test_event_json_round_trip():
    event = DownloadEvent(
        DownloadEventType.PROGRESS,
        message="Сегменты: 5/10",
        stage="segments",
        lesson="Урок",
        lesson_url="https://example.com/lesson/1",
        course_path=("Курс", "Модуль"),
        video_index=1,
        video_total=2,
        media_kind="audio",
        media_title="Мантра",
        media_index=1,
        media_total=2,
        current=5,
        total=10,
        downloaded=1,
        already_present=2,
        no_video=3,
        failed_count=4,
        cancelled=5,
        quality="1080p",
        speed_bps=1_500_000.0,
    )
    assert DownloadEvent.from_json(event.to_json()) == event


def test_download_request_schema_v2_round_trip(tmp_path):
    request = DownloadRequest(
        lessons=(
            SelectedLesson(
                course_path=("Курс", "Модуль"),
                lesson=Lesson("Урок", "https://example.com/lesson/1"),
            ),
        ),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )

    payload = request.to_dict()

    assert payload["schema_version"] == 2
    assert DownloadRequest.from_dict(payload) == request


def test_media_selection_serializes_in_stable_kind_order():
    selection = MediaSelection((MediaKind.AUDIO, MediaKind.VIDEO))

    assert selection.kinds == (MediaKind.VIDEO, MediaKind.AUDIO)
    assert selection.to_dict() == {"kinds": ["video", "audio"]}
    assert MediaSelection.from_dict({"kinds": ["audio", "video"]}) == selection


def test_media_selection_rejects_empty_or_unknown_kinds():
    with pytest.raises(InvalidDataError, match="Хотя бы один"):
        MediaSelection.from_dict({"kinds": []})

    with pytest.raises(InvalidDataError, match="Неизвестный тип"):
        MediaSelection.from_dict({"kinds": ["document"]})


def test_download_request_defaults_legacy_payload_to_video_only(tmp_path):
    payload = {
        "schema_version": 2,
        "lessons": [],
        "quality": "auto",
        "save_path": str(tmp_path),
    }

    request = DownloadRequest.from_dict(payload)

    assert request.media_selection == MediaSelection.video_only()
    assert request.to_dict()["media_selection"] == {"kinds": ["video"]}


def test_download_request_round_trip_preserves_audio_selection(tmp_path):
    request = DownloadRequest(
        lessons=(),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
        media_selection=MediaSelection.audio_only(),
    )

    assert DownloadRequest.from_dict(request.to_dict()) == request


def test_unknown_quality_rejected():
    with pytest.raises(InvalidDataError):
        VideoQuality.parse("4k")


def test_worker_request_v1_is_rejected():
    with pytest.raises(InvalidDataError, match="schema_version=2"):
        DownloadRequest.from_dict(
            {"schema_version": 1, "lessons": [], "quality": "auto", "save_path": "."}
        )
