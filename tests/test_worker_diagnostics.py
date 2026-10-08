from __future__ import annotations

from pathlib import Path

from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import DownloadRequest, Lesson, SelectedLesson, VideoQuality
from getcourse_downloader.infrastructure.diagnostics.reports import DownloadDiagnostics
from getcourse_downloader.infrastructure.platform.paths import AppPaths
from getcourse_downloader.presentation.cli import worker
from getcourse_downloader.presentation.cli.worker import DiagnosticEventSink, JsonLineEventSink


def test_worker_event_sink_adds_per_lesson_and_run_report_paths(tmp_path):
    events_path = tmp_path / "events.jsonl"
    sink = DiagnosticEventSink(
        JsonLineEventSink(events_path),
        DownloadDiagnostics(tmp_path),
    )

    sink(
        DownloadEvent(
            DownloadEventType.ERROR,
            message="Сервер вернул HTTP 403",
            stage="segments",
            lesson="Урок",
            lesson_url="https://school.example/lesson?token=secret",
            error_code="HTTP_403",
            source_host="cdn.example",
            level="error",
        )
    )
    sink(
        DownloadEvent(
            DownloadEventType.SUMMARY,
            total=1,
            downloaded=0,
            already_present=0,
            no_video=0,
        )
    )

    events = [DownloadEvent.from_json(line) for line in events_path.read_text().splitlines()]
    assert Path(events[0].diagnostic_report).is_file()
    assert Path(events[1].diagnostic_report).name == "last-run.json"
    assert Path(events[1].diagnostic_report).is_file()
    assert "secret" not in Path(events[0].diagnostic_report).read_text(encoding="utf-8")


def test_worker_preserves_profile_busy_error_code_in_events_and_report(tmp_path, monkeypatch):
    import json

    paths = AppPaths(tmp_path / "data", tmp_path / "profile", tmp_path / "resources")
    monkeypatch.setattr(worker.AppPaths, "discover", lambda: paths)

    def fail_factory(_paths):
        raise ExternalServiceError("Профиль уже используется", code="BROWSER_PROFILE_BUSY")

    monkeypatch.setattr(worker, "PlaywrightBrowserFactory", fail_factory)
    request = DownloadRequest(
        lessons=(SelectedLesson(("Course",), Lesson("Lesson", "https://example.com/lesson/1")),),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(request.to_dict()), encoding="utf-8")
    events_path = tmp_path / "events.jsonl"

    assert (
        worker.main(
            [
                "--request-file",
                str(request_path),
                "--events-file",
                str(events_path),
                "--commands-file",
                str(tmp_path / "commands.jsonl"),
            ]
        )
        == 1
    )

    events = [DownloadEvent.from_json(line) for line in events_path.read_text().splitlines()]
    errors = [
        event
        for event in events
        if event.type
        in {
            DownloadEventType.ERROR,
            DownloadEventType.LESSON_FAILED,
        }
    ]
    assert len(errors) == 2
    assert all(event.error_code == "BROWSER_PROFILE_BUSY" for event in errors)
    report = json.loads(Path(errors[1].diagnostic_report).read_text(encoding="utf-8"))
    assert report["error_code"] == "BROWSER_PROFILE_BUSY"
