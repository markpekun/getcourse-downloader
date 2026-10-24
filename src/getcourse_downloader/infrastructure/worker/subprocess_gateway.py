from __future__ import annotations

import contextlib
import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from ctypes import wintypes
from dataclasses import replace
from pathlib import Path

from getcourse_downloader import __version__
from getcourse_downloader.application.ports.download import EventHandler
from getcourse_downloader.domain.errors import DownloaderError, InvalidDataError
from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import DownloadRequest, DownloadSummary
from getcourse_downloader.infrastructure.diagnostics.reports import DownloadDiagnostics
from getcourse_downloader.infrastructure.platform.paths import AppPaths

_LOGGER = logging.getLogger("getcourse_downloader.ui.worker")
_SAFE_STAGES = frozenset(
    {
        "access",
        "authentication",
        "ffmpeg",
        "lesson",
        "network",
        "player",
        "playlist",
        "quality",
        "segments",
        "summary",
        "worker",
    }
)
_SAFE_ERROR_CODES = frozenset(
    [
        "BROWSER_PROFILE_BUSY",
        "BROWSER_PROFILE_RESET_FAILED",
        "BROWSER_PROFILE_UNAVAILABLE",
        "BROWSER_RESOURCES_MISSING",
        "BROWSER_START_FAILED",
        "CONNECTION_FAILED",
        "DASH_STREAM_UNSUPPORTED",
        "DNS_FAILED",
        "DOWNLOAD_FAILED",
        "DOWNLOAD_STALLED",
        "DOWNLOADER_ERROR",
        "EMPTY_PLAYLIST",
        "EMPTY_SEGMENT",
        "ENCRYPTED_PLAYLIST_UNSUPPORTED",
        "EXTERNAL_SERVICE_ERROR",
        "FFMPEG_FAILED",
        "FFMPEG_VERSION_UNSUPPORTED",
        "HLS_KEY_INVALID",
        "HLS_STRUCTURE_INVALID",
        "INVALID_CONFIGURATION",
        "LESSON_ACCESS_DENIED",
        "LESSON_PROCESSING_FAILED",
        "NETWORK_TIMEOUT",
        "PLAYER_ACCESS_DENIED",
        "PLAYER_CONFIG_UNSUPPORTED",
        "PLAYER_HTTP_ERROR",
        "PLAYER_INVALID_RESPONSE",
        "PLAYER_MASTER_INVALID",
        "PLAYER_PREPARATION_TIMEOUT",
        "PLAYER_REQUEST_FAILED",
        "PLAYER_STREAMS_INCOMPLETE",
        "PLAYER_VIDEO_NOT_READY",
        "PLAYLIST_NOT_OBSERVED",
        "PLAYLIST_REQUEST_FAILED",
        "SEGMENT_REQUEST_FAILED",
        "TLS_FAILED",
        "UNKNOWN",
        "VIDEO_CHECK_TIMEOUT",
        "VIDEO_NOT_FOUND",
    ]
)
_SAFE_HTTP_ERROR_CODE = re.compile(r"(?:HLS_KEY_)?HTTP_[1-5][0-9]{2}\Z")


def _safe_error_code(code: str) -> str:
    if not code:
        return "none"
    if code in _SAFE_ERROR_CODES or _SAFE_HTTP_ERROR_CODE.fullmatch(code):
        return code
    if code.startswith("HLS_KEY_") and code.removeprefix("HLS_KEY_") in _SAFE_ERROR_CODES:
        return code
    return "unknown"


class _WindowsJobBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("per_process_user_time_limit", ctypes.c_longlong),
        ("per_job_user_time_limit", ctypes.c_longlong),
        ("limit_flags", wintypes.DWORD),
        ("minimum_working_set_size", ctypes.c_size_t),
        ("maximum_working_set_size", ctypes.c_size_t),
        ("active_process_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority_class", wintypes.DWORD),
        ("scheduling_class", wintypes.DWORD),
    ]


class _WindowsJobIoCounters(ctypes.Structure):
    _fields_ = [
        (field, ctypes.c_ulonglong)
        for field in (
            "read_operation_count",
            "write_operation_count",
            "other_operation_count",
            "read_transfer_count",
            "write_transfer_count",
            "other_transfer_count",
        )
    ]


class _WindowsJobExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("basic_limit_information", _WindowsJobBasicLimitInformation),
        ("io_info", _WindowsJobIoCounters),
        ("process_memory_limit", ctypes.c_size_t),
        ("job_memory_limit", ctypes.c_size_t),
        ("peak_process_memory_used", ctypes.c_size_t),
        ("peak_job_memory_used", ctypes.c_size_t),
    ]


class _WindowsProcessJob:
    """Own a Windows process tree and terminate it when the job is closed."""

    _KILL_ON_JOB_CLOSE = 0x00002000
    _EXTENDED_LIMIT_INFORMATION = 9

    def __init__(self, kernel32, handle: int | None) -> None:
        self._kernel32 = kernel32
        self._handle = handle
        self._lock = threading.Lock()

    @classmethod
    def create_for(cls, process: subprocess.Popen[str]) -> _WindowsProcessJob | None:
        if os.name != "nt":
            return None
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
            kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            limits = _WindowsJobExtendedLimitInformation()
            limits.basic_limit_information.limit_flags = cls._KILL_ON_JOB_CLOSE
            if not kernel32.SetInformationJobObject(
                handle,
                cls._EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ) or not kernel32.AssignProcessToJobObject(handle, process._handle):  # type: ignore[attr-defined]
                kernel32.CloseHandle(handle)
                return None
            return cls(kernel32, handle)
        except (AttributeError, OSError):
            return None

    def close(self) -> None:
        with self._lock:
            handle = self._handle
            self._handle = None
        if handle:
            self._kernel32.CloseHandle(handle)


class SubprocessDownloadGateway:
    """Runs the downloader in an isolated process and consumes JSONL events."""

    def __init__(
        self,
        entrypoint: list[str] | None = None,
        *,
        inactivity_timeout_seconds: float = 360.0,
        diagnostics_directory: Path | None = None,
    ) -> None:
        self._entrypoint = entrypoint
        self._inactivity_timeout_seconds = inactivity_timeout_seconds
        self._diagnostics_directory = diagnostics_directory
        self._process: subprocess.Popen[str] | None = None
        self._job: _WindowsProcessJob | None = None
        self._command_file: Path | None = None
        self._cancel_requested = False
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._done.set()

    def _command(self) -> list[str]:
        if self._entrypoint:
            return list(self._entrypoint)
        if getattr(sys, "frozen", False):
            return [sys.executable, "--download-worker"]
        return [
            sys.executable,
            "-m",
            "getcourse_downloader.presentation.cli.worker",
        ]

    def run(self, request: DownloadRequest, on_event: EventHandler) -> DownloadSummary:
        started_at = time.monotonic()
        run_id = time.monotonic_ns()
        _LOGGER.info(
            "worker_run_started gateway=%x run=%x lesson_total=%s",
            id(self),
            run_id,
            len(request.lessons),
        )
        self._done.clear()
        request_file: Path | None = None
        event_file: Path | None = None
        command_file: Path | None = None
        job: _WindowsProcessJob | None = None
        process: subprocess.Popen[str] | None = None
        summary: DownloadSummary | None = None
        failed_titles: list[str] = []
        outcomes: dict[str, DownloadEventType] = {}
        diagnostic_events: list[DownloadEvent] = []
        last_activity_at = time.monotonic()
        waiting_for_authentication = False
        active_lesson_url = ""
        lesson_ordinals = {item.lesson.url: index for index, item in enumerate(request.lessons, 1)}
        event_count, invalid_count, progress_sample_count = 0, 0, 0
        last_progress_at = float("-inf")
        request_descriptor, request_name = tempfile.mkstemp(prefix="gcd-request-", suffix=".json")
        event_descriptor, event_name = tempfile.mkstemp(prefix="gcd-events-", suffix=".jsonl")
        command_descriptor, command_name = tempfile.mkstemp(prefix="gcd-commands-", suffix=".jsonl")
        request_file = Path(request_name)
        event_file = Path(event_name)
        command_file = Path(command_name)
        try:
            with os.fdopen(request_descriptor, "w", encoding="utf-8") as stream:
                json.dump(request.to_dict(), stream, ensure_ascii=False)
            os.close(event_descriptor)
            os.close(command_descriptor)

            flags = 0
            if os.name == "nt":
                flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                )
            environment = os.environ.copy()
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUTF8"] = "1"
            process = subprocess.Popen(
                [
                    *self._command(),
                    "--request-file",
                    str(request_file),
                    "--events-file",
                    str(event_file),
                    "--commands-file",
                    str(command_file),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                text=True,
                creationflags=flags,
                env=environment,
            )
            _LOGGER.info(
                "worker_process_started gateway=%x run=%x pid=%s startup_ms=%s",
                id(self),
                run_id,
                process.pid,
                round((time.monotonic() - started_at) * 1000),
            )
            job = _WindowsProcessJob.create_for(process)
            with self._lock:
                self._process = process
                self._job = job
                self._command_file = command_file
                if self._cancel_requested:
                    self._write_command("cancel")

            def consume(line: bytes) -> None:
                nonlocal active_lesson_url, last_activity_at, summary
                nonlocal waiting_for_authentication
                nonlocal event_count, invalid_count, last_progress_at, progress_sample_count
                try:
                    event = DownloadEvent.from_json(line.decode("utf-8"))
                except (InvalidDataError, UnicodeDecodeError) as error:
                    invalid_count += 1
                    _LOGGER.warning(
                        "worker_record_invalid gateway=%x run=%x error_type=%s count=%s",
                        id(self),
                        run_id,
                        type(error).__name__,
                        invalid_count,
                    )
                    return
                last_activity_at = time.monotonic()
                event_count += 1
                log_event = True
                if event.type is DownloadEventType.PROGRESS:
                    progress_sample_count += 1
                    log_event = last_activity_at - last_progress_at >= 1.0
                    if log_event:
                        last_progress_at = last_activity_at
                if log_event:
                    level = (
                        logging.DEBUG
                        if event.type in {DownloadEventType.LOG, DownloadEventType.PROGRESS}
                        else logging.INFO
                    )
                    _LOGGER.log(
                        level,
                        "worker_event gateway=%x run=%x type=%s stage=%s error_code=%s "
                        "lesson_ordinal=%s event_count=%s progress_count=%s current=%s total=%s "
                        "downloaded=%s already_present=%s no_video=%s failed=%s cancelled=%s",
                        id(self),
                        run_id,
                        event.type.value,
                        event.stage if event.stage in _SAFE_STAGES else "unknown",
                        _safe_error_code(event.error_code),
                        lesson_ordinals.get(event.lesson_url, 0),
                        event_count,
                        progress_sample_count,
                        event.current,
                        event.total,
                        event.downloaded,
                        event.already_present,
                        event.no_video,
                        event.failed_count,
                        event.cancelled,
                    )
                if event.type is DownloadEventType.AUTH_REQUIRED:
                    waiting_for_authentication = True
                elif event.type is DownloadEventType.AUTHENTICATED:
                    waiting_for_authentication = False
                elif event.type is DownloadEventType.LESSON_STARTED:
                    active_lesson_url = event.lesson_url
                on_event(event)
                if event.type in {
                    DownloadEventType.ERROR,
                    DownloadEventType.LESSON_FAILED,
                    DownloadEventType.LESSON_NO_VIDEO,
                }:
                    diagnostic_events.append(event)
                if event.type is DownloadEventType.LESSON_FAILED and event.lesson:
                    failed_titles.append(event.lesson)
                if event.type in {
                    DownloadEventType.LESSON_COMPLETED,
                    DownloadEventType.LESSON_SKIPPED,
                    DownloadEventType.LESSON_NO_VIDEO,
                    DownloadEventType.LESSON_FAILED,
                }:
                    outcomes[event.lesson_url or event.lesson] = event.type
                if event.type is DownloadEventType.SUMMARY:
                    summary = DownloadSummary(
                        total=event.total or 0,
                        downloaded=event.downloaded or 0,
                        already_present=event.already_present or 0,
                        no_video=event.no_video or 0,
                        failed=tuple(failed_titles),
                        cancelled=event.cancelled or 0,
                    )

            with event_file.open("rb") as events_stream:
                pending = b""
                while True:
                    raw_line = events_stream.readline()
                    if raw_line:
                        pending += raw_line
                        if pending.endswith(b"\n"):
                            if pending.strip():
                                consume(pending)
                            pending = b""
                        continue
                    if process.poll() is not None:
                        for line in (pending + events_stream.read()).splitlines():
                            if line.strip():
                                consume(line)
                        break
                    if (
                        not waiting_for_authentication
                        and self._inactivity_timeout_seconds > 0
                        and time.monotonic() - last_activity_at >= self._inactivity_timeout_seconds
                    ):
                        _LOGGER.warning(
                            "worker_inactivity_timeout gateway=%x run=%x pid=%s idle_ms=%s",
                            id(self),
                            run_id,
                            process.pid,
                            round((time.monotonic() - last_activity_at) * 1000),
                        )
                        self._terminate_process(process, job)
                        summary = self._stalled_summary(
                            request,
                            on_event,
                            outcomes,
                            failed_titles,
                            active_lesson_url,
                            diagnostic_events,
                        )
                        break
                    time.sleep(0.05)

            return_code = process.wait()
            if summary is None:
                raise DownloaderError(
                    f"Worker завершился без итогового события (код {return_code})"
                )
            return summary
        except BaseException as error:
            _LOGGER.warning(
                "worker_run_failed gateway=%x run=%x error_type=%s",
                id(self),
                run_id,
                type(error).__name__,
            )
            raise
        finally:
            if process is not None and process.poll() is None:
                self._terminate_process(process, job)
            if job is not None:
                job.close()
            for path in (request_file, event_file, command_file):
                if path is not None:
                    with contextlib.suppress(OSError):
                        path.unlink(missing_ok=True)
            with self._lock:
                self._process = None
                self._job = None
                self._command_file = None
                self._cancel_requested = False
                self._done.set()
            _LOGGER.info(
                "worker_run_finished gateway=%x run=%x pid=%s exit_code=%s duration_ms=%s "
                "summary_received=%s event_count=%s invalid_count=%s progress_count=%s",
                id(self),
                run_id,
                process.pid if process is not None else 0,
                process.returncode if process is not None else None,
                round((time.monotonic() - started_at) * 1000),
                summary is not None,
                event_count,
                invalid_count,
                progress_sample_count,
            )

    def _stalled_summary(
        self,
        request: DownloadRequest,
        on_event: EventHandler,
        outcomes: dict[str, DownloadEventType],
        failed_titles: list[str],
        active_lesson_url: str,
        diagnostic_events: list[DownloadEvent],
    ) -> DownloadSummary:
        pending = [item for item in request.lessons if item.lesson.url not in outcomes]
        active = next(
            (item for item in pending if item.lesson.url == active_lesson_url),
            pending[0] if pending else None,
        )
        data_directory = self._diagnostics_directory or AppPaths.discover().data
        diagnostics = DownloadDiagnostics(data_directory, app_version=__version__)
        for event in diagnostic_events:
            diagnostics.record(event)
        if active is not None:
            message = "Загрузка не менялась более 6 минут и была остановлена"
            error = DownloadEvent(
                DownloadEventType.ERROR,
                message=message,
                stage="worker",
                lesson=active.lesson.title,
                lesson_url=active.lesson.url,
                course_path=active.course_path,
                level="error",
                error_code="DOWNLOAD_STALLED",
            )
            report = diagnostics.record(error)
            if report is not None:
                error = replace(error, diagnostic_report=str(report))
            on_event(error)
            failed = DownloadEvent(
                DownloadEventType.LESSON_FAILED,
                message=f"Не удалось скачать: {active.lesson.title}",
                stage="worker",
                lesson=active.lesson.title,
                lesson_url=active.lesson.url,
                course_path=active.course_path,
                level="error",
                error_code="DOWNLOAD_STALLED",
            )
            report = diagnostics.record(failed)
            if report is not None:
                failed = replace(failed, diagnostic_report=str(report))
            on_event(failed)
            failed_titles.append(active.lesson.title)

        downloaded = sum(value is DownloadEventType.LESSON_COMPLETED for value in outcomes.values())
        already_present = sum(
            value is DownloadEventType.LESSON_SKIPPED for value in outcomes.values()
        )
        no_video = sum(value is DownloadEventType.LESSON_NO_VIDEO for value in outcomes.values())
        processed = downloaded + already_present + no_video + len(failed_titles)
        cancelled = max(0, len(request.lessons) - processed)
        report = diagnostics.finish(
            total=len(request.lessons),
            downloaded=downloaded,
            already_present=already_present,
            no_video=no_video,
        )
        on_event(
            DownloadEvent(
                DownloadEventType.SUMMARY,
                message="Загрузка остановлена: процесс перестал отвечать",
                stage="summary",
                current=processed,
                total=len(request.lessons),
                downloaded=downloaded,
                already_present=already_present,
                no_video=no_video,
                failed_count=len(failed_titles),
                cancelled=cancelled,
                level="error",
                diagnostic_report=str(report),
            )
        )
        return DownloadSummary(
            total=len(request.lessons),
            downloaded=downloaded,
            already_present=already_present,
            no_video=no_video,
            failed=tuple(failed_titles),
            cancelled=cancelled,
        )

    def _send_command(self, command: str) -> None:
        _LOGGER.info("worker_command_queued gateway=%x command=%s", id(self), command)
        with self._lock:
            if command == "cancel":
                self._cancel_requested = True
            self._write_command(command)

    def _write_command(self, command: str) -> None:
        """Write a command while the caller holds the lifecycle lock."""
        path = self._command_file
        process = self._process
        if path is None or process is None or process.poll() is not None:
            _LOGGER.debug(
                "worker_command_deferred gateway=%x command=%s process_available=%s",
                id(self),
                command,
                process is not None,
            )
            return
        try:
            with path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"command": command}) + "\n")
                stream.flush()
            _LOGGER.info(
                "worker_command_written gateway=%x pid=%s command=%s",
                id(self),
                process.pid,
                command,
            )
        except OSError as error:
            _LOGGER.warning(
                "worker_command_write_failed gateway=%x pid=%s command=%s error_type=%s",
                id(self),
                process.pid,
                command,
                type(error).__name__,
            )
            return

    def continue_authentication(self) -> None:
        self._send_command("continue_authentication")

    def cancel(self) -> None:
        self._send_command("cancel")

    def shutdown(self, timeout: float = 6.0) -> None:
        started_at = time.monotonic()
        _LOGGER.info(
            "worker_shutdown_requested gateway=%x active=%s timeout_ms=%s",
            id(self),
            not self._done.is_set(),
            round(timeout * 1000),
        )
        if self._done.is_set():
            return
        self.cancel()
        if self._done.wait(timeout):
            _LOGGER.info(
                "worker_shutdown_finished gateway=%x forced=%s duration_ms=%s",
                id(self),
                False,
                round((time.monotonic() - started_at) * 1000),
            )
            return
        with self._lock:
            process = self._process
            job = self._job
        if process is None or process.poll() is not None:
            return
        self._terminate_process(process, job)
        completed = self._done.wait(2)
        _LOGGER.info(
            "worker_shutdown_finished gateway=%x forced=%s completed=%s duration_ms=%s",
            id(self),
            True,
            completed,
            round((time.monotonic() - started_at) * 1000),
        )

    @staticmethod
    def _terminate_process(
        process: subprocess.Popen[str], job: _WindowsProcessJob | None = None
    ) -> None:
        started_at = time.monotonic()
        _LOGGER.warning(
            "worker_force_termination pid=%s job_owned=%s", process.pid, job is not None
        )
        if job is not None:
            job.close()
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        else:
            process.kill()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            _LOGGER.warning("worker_force_termination_retry pid=%s", process.pid)
            process.kill()
            process.wait(timeout=2)
        _LOGGER.info(
            "worker_process_terminated pid=%s exit_code=%s duration_ms=%s",
            process.pid,
            process.returncode,
            round((time.monotonic() - started_at) * 1000),
        )
