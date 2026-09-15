from __future__ import annotations

import ctypes
import json
import os
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
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
        self._done.clear()
        request_file: Path | None = None
        event_file: Path | None = None
        command_file: Path | None = None
        job: _WindowsProcessJob | None = None
        summary: DownloadSummary | None = None
        failed_titles: list[str] = []
        outcomes: dict[str, DownloadEventType] = {}
        diagnostic_events: list[DownloadEvent] = []
        last_activity_at = time.monotonic()
        waiting_for_authentication = False
        active_lesson_url = ""
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
            job = _WindowsProcessJob.create_for(process)
            with self._lock:
                self._process = process
                self._job = job
                self._command_file = command_file

            def consume(line: str) -> None:
                nonlocal active_lesson_url, last_activity_at, summary
                nonlocal waiting_for_authentication
                try:
                    event = DownloadEvent.from_json(line)
                except InvalidDataError:
                    return
                last_activity_at = time.monotonic()
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

            with event_file.open("r", encoding="utf-8") as stream:
                while True:
                    raw_line = stream.readline()
                    if raw_line:
                        line = raw_line.strip()
                        if line:
                            consume(line)
                        continue
                    if process.poll() is not None:
                        for remaining in stream:
                            line = remaining.strip()
                            if line:
                                consume(line)
                        break
                    if (
                        not waiting_for_authentication
                        and self._inactivity_timeout_seconds > 0
                        and time.monotonic() - last_activity_at >= self._inactivity_timeout_seconds
                    ):
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
        finally:
            with self._lock:
                self._process = None
                self._job = None
                self._command_file = None
                self._done.set()
            if job is not None:
                job.close()
            if request_file:
                request_file.unlink(missing_ok=True)
            if event_file:
                event_file.unlink(missing_ok=True)
            if command_file:
                command_file.unlink(missing_ok=True)

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
        with self._lock:
            path = self._command_file
            process = self._process
            if path is None or process is None or process.poll() is not None:
                return
            try:
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"command": command}) + "\n")
                    stream.flush()
            except OSError:
                return

    def continue_authentication(self) -> None:
        self._send_command("continue_authentication")

    def cancel(self) -> None:
        self._send_command("cancel")

    def shutdown(self, timeout: float = 6.0) -> None:
        self.cancel()
        if self._done.wait(timeout):
            return
        with self._lock:
            process = self._process
            job = self._job
        if process is None or process.poll() is not None:
            return
        self._terminate_process(process, job)
        self._done.wait(2)

    @staticmethod
    def _terminate_process(
        process: subprocess.Popen[str], job: _WindowsProcessJob | None = None
    ) -> None:
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
            process.kill()
