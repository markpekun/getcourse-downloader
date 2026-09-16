import concurrent.futures
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from getcourse_downloader.domain.models import (
    DownloadRequest,
    Lesson,
    SelectedLesson,
    VideoQuality,
)
from getcourse_downloader.infrastructure.worker import subprocess_gateway as gateway_module
from getcourse_downloader.infrastructure.worker.subprocess_gateway import SubprocessDownloadGateway
from getcourse_downloader.presentation.cli.worker import WorkerCommandListener

_WORKER_ARGUMENTS = """
import argparse, json, time
p = argparse.ArgumentParser()
for name in ('request-file', 'events-file', 'commands-file'):
    p.add_argument('--' + name)
a = p.parse_args()
"""


def _request(path: Path) -> DownloadRequest:
    return DownloadRequest(
        lessons=(SelectedLesson(("Course",), Lesson("Lesson", "https://example.com/lesson/1")),),
        quality=VideoQuality.AUTO,
        save_path=path,
    )


@pytest.mark.integration
@pytest.mark.parametrize("before_run", [False, True])
def test_cancellation_during_startup_is_delivered_and_does_not_cancel_next_run(
    tmp_path, monkeypatch, before_run
):
    worker = (
        _WORKER_ARGUMENTS
        + """
time.sleep(0.2)
cancelled = 'cancel' in open(a.commands_file, encoding='utf-8').read()
event = dict(protocol_version=2, type='summary', total=1,
             downloaded=int(not cancelled), cancelled=int(cancelled))
with open(a.events_file, 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(event) + '\\n')
"""
    )
    gateway = SubprocessDownloadGateway([sys.executable, "-c", worker])
    starting = threading.Event()
    release = threading.Event()
    real_popen = subprocess.Popen

    def delayed_popen(*args, **kwargs):
        starting.set()
        assert release.wait(5)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(gateway_module.subprocess, "Popen", delayed_popen)
    if before_run:
        gateway.cancel()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(gateway.run, _request(tmp_path), lambda _: None)
        try:
            assert starting.wait(5)
            if not before_run:
                gateway.cancel()
        finally:
            release.set()
        summary = future.result(timeout=5)

    assert summary.cancelled == 1
    assert summary.downloaded == 0
    next_summary = gateway.run(_request(tmp_path), lambda _: None)
    assert next_summary.cancelled == 0
    assert next_summary.downloaded == 1


@pytest.mark.integration
def test_shutting_down_an_idle_gateway_does_not_cancel_the_next_download(tmp_path):
    worker = (
        _WORKER_ARGUMENTS
        + """
time.sleep(0.2)
cancelled = 'cancel' in open(a.commands_file, encoding='utf-8').read()
event = dict(protocol_version=2, type='summary', total=1,
             downloaded=int(not cancelled), cancelled=int(cancelled))
with open(a.events_file, 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(event) + '\\n')
"""
    )
    gateway = SubprocessDownloadGateway([sys.executable, "-c", worker])
    gateway.shutdown(timeout=0)

    summary = gateway.run(_request(tmp_path), lambda _: None)

    assert summary.downloaded == 1
    assert summary.cancelled == 0


@pytest.mark.integration
@pytest.mark.parametrize("split_utf8", [False, True])
def test_event_reader_preserves_records_split_between_writes(tmp_path, split_utf8):
    worker = (
        _WORKER_ARGUMENTS
        + f"""
event = dict(protocol_version=2, type='summary', total=1, downloaded=1, message='Урок')
line = (json.dumps(event, ensure_ascii=False) + '\\n').encode('utf-8')
split = line.index('У'.encode('utf-8')) + 1 if {split_utf8!r} else 35
with open(a.events_file, 'ab') as stream:
    stream.write(line[:split])
    stream.flush()
    time.sleep(0.2)
    stream.write(line[split:])
    stream.flush()
"""
    )
    gateway = SubprocessDownloadGateway([sys.executable, "-c", worker])
    events = []

    summary = gateway.run(_request(tmp_path), events.append)

    assert summary.downloaded == 1
    assert len(events) == 1
    assert events[0].message == "Урок"


@pytest.mark.integration
def test_callback_failure_terminates_worker_without_windows_job(tmp_path, monkeypatch):
    worker = (
        _WORKER_ARGUMENTS
        + """
with open(a.events_file, 'a', encoding='utf-8') as stream:
    stream.write(json.dumps(dict(protocol_version=2, type='log')) + '\\n')
time.sleep(30)
"""
    )
    gateway = SubprocessDownloadGateway([sys.executable, "-c", worker])
    processes = []
    real_popen = subprocess.Popen

    def capture_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail_callback(_event):
        raise RuntimeError("UI callback failed")

    monkeypatch.setattr(gateway_module.subprocess, "Popen", capture_popen)
    monkeypatch.setattr(gateway_module._WindowsProcessJob, "create_for", lambda _: None)
    try:
        with pytest.raises(RuntimeError, match="UI callback failed"):
            gateway.run(_request(tmp_path), fail_callback)
        assert processes[0].poll() is not None
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def test_command_listener_preserves_cancel_split_between_writes(tmp_path):
    command_file = tmp_path / "commands.jsonl"
    command_file.write_text('{"command": "can', encoding="utf-8")
    partial_read = threading.Event()
    cancelled = threading.Event()

    class Gateway:
        def cancel(self):
            cancelled.set()

    class ObservedStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def readline(self):
            line = self.stream.readline()
            if line:
                partial_read.set()
            return line

    class ObservedPath:
        def open(self, *args, **kwargs):
            return ObservedStream(command_file.open(*args, **kwargs))

    listener = WorkerCommandListener(ObservedPath(), Gateway())
    listener.start()
    try:
        assert partial_read.wait(2)
        with command_file.open("a", encoding="utf-8") as stream:
            stream.write('cel"}\n')
        assert cancelled.wait(2)
    finally:
        listener.stop()
