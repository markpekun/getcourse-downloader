from __future__ import annotations

import json

import pytest

from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.infrastructure.diagnostics.reports import DownloadDiagnostics


def test_failed_lesson_creates_sanitized_individual_and_run_reports(tmp_path):
    """A support report must identify the failing stage without exposing signed URLs."""

    diagnostics = DownloadDiagnostics(tmp_path, app_version="1.2.3")
    failure = DownloadEvent(
        DownloadEventType.ERROR,
        message="Сервер отклонил сегмент: https://cdn.example/video.ts?token=secret",
        stage="segments",
        lesson="Урок 1",
        lesson_url="https://school.example/lesson?id=42&token=secret",
        current=14,
        total=53,
        quality="720p",
        error_code="HTTP_403",
        source_host="cdn.example",
        level="error",
    )

    diagnostics.record(failure)
    run_report = diagnostics.finish(total=1, downloaded=0, already_present=0, no_video=0)

    lesson_reports = list((tmp_path / "diagnostics" / "lessons").glob("*.json"))
    assert len(lesson_reports) == 1
    lesson = json.loads(lesson_reports[0].read_text(encoding="utf-8"))
    assert lesson == {
        "app_version": "1.2.3",
        "error_code": "HTTP_403",
        "lesson": "Урок 1",
        "lesson_url": "https://school.example/lesson",
        "progress": {"current": 14, "total": 53},
        "quality": "720p",
        "source_host": "cdn.example",
        "stage": "segments",
        "summary": "Сервер отклонил сегмент: https://cdn.example/video.ts",
    }
    run = json.loads(run_report.read_text(encoding="utf-8"))
    assert run["counts"] == {
        "already_present": 0,
        "downloaded": 0,
        "failed": 1,
        "no_video": 0,
        "total": 1,
    }
    assert run["failed_lessons"] == [lesson]
    assert "secret" not in run_report.read_text(encoding="utf-8")


def test_non_failure_events_are_omitted_from_diagnostic_reports(tmp_path):
    diagnostics = DownloadDiagnostics(tmp_path)

    diagnostics.record(
        DownloadEvent(
            DownloadEventType.PROGRESS,
            message="Сегменты: 10/10",
            lesson="Урок без ошибки",
            lesson_url="https://school.example/lesson",
        )
    )
    run_report = diagnostics.finish(total=1, downloaded=1, already_present=0, no_video=0)

    assert not (tmp_path / "diagnostics" / "lessons").exists()
    assert json.loads(run_report.read_text(encoding="utf-8"))["failed_lessons"] == []


def test_run_report_keeps_global_errors_without_creating_a_fake_lesson(tmp_path):
    diagnostics = DownloadDiagnostics(tmp_path)

    diagnostics.record(
        DownloadEvent(
            DownloadEventType.ERROR,
            message="Не удалось открыть https://school.example/path?token=secret",
            stage="browser",
            error_code="BROWSER_START_FAILED",
            level="error",
        )
    )
    run_report = diagnostics.finish(total=1, downloaded=0, already_present=0, no_video=0)

    report = json.loads(run_report.read_text(encoding="utf-8"))
    assert report["failed_lessons"] == []
    assert report["global_errors"] == [
        {
            "error_code": "BROWSER_START_FAILED",
            "stage": "browser",
            "summary": "Не удалось открыть https://school.example/path",
        }
    ]
    assert "secret" not in run_report.read_text(encoding="utf-8")


def test_failed_lesson_without_a_prior_error_gets_its_own_report(tmp_path):
    diagnostics = DownloadDiagnostics(tmp_path)

    report_path = diagnostics.record(
        DownloadEvent(
            DownloadEventType.LESSON_FAILED,
            message="Worker остановился до загрузки урока",
            stage="worker",
            lesson="Урок 2",
            lesson_url="https://school.example/lesson/2?token=secret",
            level="error",
        )
    )

    assert report_path is not None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["error_code"] == "DOWNLOAD_FAILED"
    assert report["lesson_url"] == "https://school.example/lesson/2"
    assert "secret" not in report_path.read_text(encoding="utf-8")


def test_no_video_lesson_is_included_in_individual_and_run_reports(tmp_path):
    diagnostics = DownloadDiagnostics(tmp_path, app_version="1.2.3")

    report_path = diagnostics.record(
        DownloadEvent(
            DownloadEventType.LESSON_NO_VIDEO,
            message="Видео не найдено: Урок без плеера",
            stage="playlist",
            lesson="Урок без плеера",
            lesson_url="https://school.example/lesson/3?token=secret",
            level="warning",
        )
    )
    run_path = diagnostics.finish(total=1, downloaded=0, already_present=0, no_video=1)

    assert report_path is not None
    lesson_report = json.loads(report_path.read_text(encoding="utf-8"))
    run_report = json.loads(run_path.read_text(encoding="utf-8"))
    assert lesson_report["error_code"] == "VIDEO_NOT_FOUND"
    assert run_report["no_video_lessons"] == [lesson_report]
    assert run_report["failed_lessons"] == []


def test_report_removes_url_credentials_from_urls_messages_and_source_hosts(tmp_path):
    report_path = DownloadDiagnostics(tmp_path).record(
        DownloadEvent(
            DownloadEventType.ERROR,
            lesson="Lesson",
            lesson_url="https://user:secret@school.example/lesson?token=secret",
            message="Failed https://user:secret@cdn.example/video?token=secret#secret",
            source_host="user:secret@cdn.example",
        )
    )

    assert report_path is not None
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["lesson_url"] == "https://school.example/lesson"
    assert payload["source_host"] == "cdn.example"
    assert payload["summary"] == "Failed https://cdn.example/video"
    assert "secret" not in report_path.read_text(encoding="utf-8")


def test_report_removes_credentials_from_derived_source_host(tmp_path):
    report_path = DownloadDiagnostics(tmp_path).record(
        DownloadEvent(
            DownloadEventType.ERROR,
            lesson="Lesson",
            lesson_url="https://user:secret@school.example/lesson",
        )
    )

    assert report_path is not None
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["source_host"] == "school.example"
    assert "secret" not in report_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "lesson_url", ["https://[broken/lesson?token=secret", "https://[::1]/lesson?token=secret"]
)
def test_report_handles_malformed_and_ipv6_urls_without_leaking_query(tmp_path, lesson_url):
    report_path = DownloadDiagnostics(tmp_path).record(
        DownloadEvent(
            DownloadEventType.ERROR,
            lesson="Lesson",
            lesson_url=lesson_url,
            message=f"Failed {lesson_url}",
        )
    )

    assert report_path is not None
    payload = report_path.read_text(encoding="utf-8")
    assert "Failed" in payload
    assert "secret" not in payload
