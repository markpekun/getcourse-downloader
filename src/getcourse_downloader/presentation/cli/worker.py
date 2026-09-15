from __future__ import annotations

import argparse
import json
import threading
import time
import traceback
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from getcourse_downloader import __version__
from getcourse_downloader.application.use_cases.download_lessons import DownloadLessons
from getcourse_downloader.domain.errors import InvalidDataError
from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import DownloadRequest
from getcourse_downloader.infrastructure.browser.playwright import PlaywrightBrowserFactory
from getcourse_downloader.infrastructure.diagnostics.reports import DownloadDiagnostics
from getcourse_downloader.infrastructure.getcourse.downloader import PlaywrightDownloadGateway
from getcourse_downloader.infrastructure.media.ffmpeg import FfmpegMuxer
from getcourse_downloader.infrastructure.media.hls import HlsDownloader
from getcourse_downloader.infrastructure.platform.paths import AppPaths
from getcourse_downloader.infrastructure.storage.download_catalog import JsonDownloadCatalog


class JsonLineEventSink:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    def __call__(self, event: DownloadEvent) -> None:
        try:
            with self._lock, self._path.open("a", encoding="utf-8") as stream:
                stream.write(event.to_json() + "\n")
                stream.flush()
        except OSError:
            return


class DiagnosticEventSink:
    """Adds local diagnostic report paths to worker events before forwarding them."""

    def __init__(self, sink: JsonLineEventSink, diagnostics: DownloadDiagnostics) -> None:
        self._sink = sink
        self._diagnostics = diagnostics

    def __call__(self, event: DownloadEvent) -> None:
        report = self._diagnostics.record(event)
        if event.type is DownloadEventType.SUMMARY:
            report = self._diagnostics.finish(
                total=event.total or 0,
                downloaded=event.downloaded or 0,
                already_present=event.already_present or 0,
                no_video=event.no_video or 0,
            )
        if report is not None:
            event = replace(event, diagnostic_report=str(report))
        self._sink(event)


class WorkerCommandListener:
    def __init__(self, path: Path, gateway: PlaywrightDownloadGateway) -> None:
        self._path = path
        self._gateway = gateway
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._run, name="worker-commands", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        self._thread.join(timeout=1)

    def _run(self) -> None:
        try:
            with self._path.open("r", encoding="utf-8") as stream:
                while not self._stopped.is_set():
                    line = stream.readline()
                    if not line:
                        time.sleep(0.05)
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    command = payload.get("command") if isinstance(payload, dict) else None
                    if command == "cancel":
                        self._gateway.cancel()
                    elif command == "continue_authentication":
                        self._gateway.continue_authentication()
        except OSError:
            return


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Внутренний worker загрузки видео")
    parser.add_argument("--request-file", required=True, help="JSON-файл DownloadRequest")
    parser.add_argument("--events-file", required=True, help="JSONL-файл событий")
    parser.add_argument("--commands-file", required=True, help="JSONL-файл команд")
    return parser


def _load_request(path: Path) -> DownloadRequest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise InvalidDataError("Не удалось прочитать запрос worker") from error
    if not isinstance(payload, dict):
        raise InvalidDataError("Запрос worker должен быть JSON-объектом")
    return DownloadRequest.from_dict(payload)


def main(argv: Sequence[str] | None = None) -> int:
    request: DownloadRequest | None = None
    listener: WorkerCommandListener | None = None
    sink: DiagnosticEventSink | None = None
    paths = AppPaths.discover()
    paths.ensure_runtime_directories()
    try:
        args = build_parser().parse_args(argv)
        sink = DiagnosticEventSink(
            JsonLineEventSink(Path(args.events_file)),
            DownloadDiagnostics(paths.data, app_version=__version__),
        )
        request = _load_request(Path(args.request_file))
        gateway = PlaywrightDownloadGateway(
            PlaywrightBrowserFactory(paths),
            HlsDownloader(FfmpegMuxer(paths)),
            JsonDownloadCatalog(paths.downloads_file),
        )
        listener = WorkerCommandListener(Path(args.commands_file), gateway)
        listener.start()
        summary = DownloadLessons(gateway).execute(request, sink)
        return 0 if summary.successful else 2
    except Exception as error:
        try:
            with paths.worker_log_file.open("a", encoding="utf-8") as stream:
                stream.write(traceback.format_exc())
                stream.write("\n")
        except OSError:
            pass
        if sink is not None:
            sink(
                DownloadEvent(
                    DownloadEventType.ERROR,
                    message=str(error),
                    stage="worker",
                    level="error",
                )
            )
            if request is not None:
                for item in request.lessons:
                    sink(
                        DownloadEvent(
                            DownloadEventType.LESSON_FAILED,
                            message=str(error),
                            stage="worker",
                            lesson=item.lesson.title,
                            lesson_url=item.lesson.url,
                            course_path=item.course_path,
                            level="error",
                        )
                    )
                sink(
                    DownloadEvent(
                        DownloadEventType.SUMMARY,
                        message=f"Загрузка завершилась с ошибкой: {error}",
                        stage="summary",
                        current=len(request.lessons),
                        total=len(request.lessons),
                        downloaded=0,
                        already_present=0,
                        no_video=0,
                        failed_count=len(request.lessons),
                        cancelled=0,
                        level="error",
                    )
                )
        return 1
    finally:
        if listener is not None:
            listener.stop()


if __name__ == "__main__":
    raise SystemExit(main())
