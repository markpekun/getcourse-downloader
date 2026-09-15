from __future__ import annotations

from pathlib import Path

from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.infrastructure.diagnostics.reports import DownloadDiagnostics
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
