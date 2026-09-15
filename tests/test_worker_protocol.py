import concurrent.futures
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from getcourse_downloader.domain.errors import DownloaderError
from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import (
    DownloadRequest,
    Lesson,
    SelectedLesson,
    VideoQuality,
)
from getcourse_downloader.infrastructure.browser.playwright import _process_exists
from getcourse_downloader.infrastructure.worker.subprocess_gateway import (
    SubprocessDownloadGateway,
)

pytestmark = pytest.mark.integration


def test_subprocess_gateway_reads_typed_events(tmp_path):
    worker = Path(__file__).parent / "fixtures" / "fake_worker.py"
    gateway = SubprocessDownloadGateway([sys.executable, str(worker)])
    request = DownloadRequest(
        lessons=(SelectedLesson(("Курс",), Lesson("Урок", "https://example.com")),),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )
    events = []
    summary = gateway.run(request, events.append)
    assert summary.total == 1
    assert summary.downloaded == 0
    assert summary.failed == ("Урок",)
    assert [event.type for event in events] == [
        DownloadEventType.LESSON_FAILED,
        DownloadEventType.SUMMARY,
    ]


def test_subprocess_gateway_rejects_worker_without_summary(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import argparse\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--request-file')\n"
        "p.add_argument('--events-file')\n"
        "p.add_argument('--commands-file')\n"
        "p.parse_args()\n",
        encoding="utf-8",
    )
    gateway = SubprocessDownloadGateway([sys.executable, str(worker)])
    request = DownloadRequest(
        lessons=(SelectedLesson(("Курс",), Lesson("Урок", "https://example.com")),),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )

    with pytest.raises(DownloaderError, match="без итогового события"):
        gateway.run(request, lambda _: None)


def test_subprocess_gateway_stops_worker_that_no_longer_reports_progress(tmp_path):
    worker = tmp_path / "stalled_worker.py"
    worker.write_text(
        "import argparse,json,time\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--request-file')\n"
        "p.add_argument('--events-file')\n"
        "p.add_argument('--commands-file')\n"
        "a=p.parse_args()\n"
        "event={'protocol_version':2,'type':'video_found','message':'Загрузка',"
        "'lesson':'Урок','lesson_url':'https://example.com/lesson/1'}\n"
        "open(a.events_file,'a',encoding='utf-8').write(json.dumps(event)+'\\n')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    gateway = SubprocessDownloadGateway(
        [sys.executable, str(worker)],
        inactivity_timeout_seconds=1.0,
        diagnostics_directory=tmp_path,
    )
    request = DownloadRequest(
        lessons=(
            SelectedLesson(
                ("Курс",),
                Lesson("Урок", "https://example.com/lesson/1"),
            ),
        ),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )
    events = []

    summary = gateway.run(request, events.append)

    assert summary.failed == ("Урок",)
    assert [event.type for event in events[-3:]] == [
        DownloadEventType.ERROR,
        DownloadEventType.LESSON_FAILED,
        DownloadEventType.SUMMARY,
    ]
    assert events[-3].error_code == "DOWNLOAD_STALLED"
    assert Path(events[-3].diagnostic_report).is_file()
    assert Path(events[-1].diagnostic_report).name == "last-run.json"


def test_stalled_run_report_keeps_earlier_failed_and_no_video_lessons(tmp_path):
    worker = tmp_path / "partially_stalled_worker.py"
    worker.write_text(
        "import argparse,json,time\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--request-file')\n"
        "p.add_argument('--events-file')\n"
        "p.add_argument('--commands-file')\n"
        "a=p.parse_args()\n"
        "events=[\n"
        " {'protocol_version':2,'type':'lesson_failed','message':'HTTP 403',"
        "  'stage':'playlist','error_code':'HTTP_403','lesson':'Урок 1',"
        "  'lesson_url':'https://example.com/lesson/1'},\n"
        " {'protocol_version':2,'type':'lesson_no_video','message':'Плеер не найден',"
        "  'stage':'player','lesson':'Урок 2',"
        "  'lesson_url':'https://example.com/lesson/2'},\n"
        " {'protocol_version':2,'type':'video_found','message':'Загрузка',"
        "  'lesson':'Урок 3','lesson_url':'https://example.com/lesson/3'},\n"
        "]\n"
        "with open(a.events_file,'a',encoding='utf-8') as f:\n"
        "  for event in events: f.write(json.dumps(event)+'\\n')\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    gateway = SubprocessDownloadGateway(
        [sys.executable, str(worker)],
        inactivity_timeout_seconds=1.0,
        diagnostics_directory=tmp_path,
    )
    request = DownloadRequest(
        lessons=tuple(
            SelectedLesson(
                ("Курс",), Lesson(f"Урок {number}", f"https://example.com/lesson/{number}")
            )
            for number in range(1, 4)
        ),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )
    events = []

    summary = gateway.run(request, events.append)

    report = json.loads(Path(events[-1].diagnostic_report).read_text(encoding="utf-8"))
    assert summary.failed == ("Урок 1", "Урок 3")
    assert summary.no_video == 1
    assert report["counts"] == {
        "already_present": 0,
        "downloaded": 0,
        "failed": 2,
        "no_video": 1,
        "total": 3,
    }
    assert [lesson["error_code"] for lesson in report["failed_lessons"]] == [
        "HTTP_403",
        "DOWNLOAD_STALLED",
    ]
    assert [lesson["error_code"] for lesson in report["no_video_lessons"]] == ["VIDEO_NOT_FOUND"]


def test_stalled_lessons_with_the_same_title_are_counted_separately(tmp_path):
    first = SelectedLesson(("Первый модуль",), Lesson("Урок 1", "https://example.com/lesson/1"))
    second = SelectedLesson(("Второй модуль",), Lesson("Урок 1", "https://example.com/lesson/2"))
    request = DownloadRequest(
        lessons=(first, second),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )
    gateway = SubprocessDownloadGateway(diagnostics_directory=tmp_path)
    earlier_failure = DownloadEvent(
        DownloadEventType.LESSON_FAILED,
        message="Не удалось скачать первый урок",
        lesson=first.lesson.title,
        lesson_url=first.lesson.url,
    )
    events = []

    summary = gateway._stalled_summary(
        request,
        events.append,
        {first.lesson.url: DownloadEventType.LESSON_FAILED},
        [first.lesson.title],
        second.lesson.url,
        [earlier_failure],
    )

    report = json.loads(Path(events[-1].diagnostic_report).read_text(encoding="utf-8"))
    assert summary.failed == ("Урок 1", "Урок 1")
    assert report["counts"]["failed"] == 2


def test_subprocess_gateway_sends_cancel_and_receives_cancelled_summary(tmp_path):
    worker = tmp_path / "cancel_worker.py"
    worker.write_text(
        "import argparse,json,time\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--request-file')\n"
        "p.add_argument('--events-file')\n"
        "p.add_argument('--commands-file')\n"
        "a=p.parse_args()\n"
        "def emit(data):\n"
        "  with open(a.events_file,'a',encoding='utf-8') as f: f.write(json.dumps(data)+'\\n')\n"
        "emit({'protocol_version':2,'type':'lesson_started','lesson':'Урок',"
        "'lesson_url':'https://example.com'})\n"
        "while True:\n"
        "  text=open(a.commands_file,encoding='utf-8').read()\n"
        '  if \'"command": "cancel"\' in text: break\n'
        "  time.sleep(0.02)\n"
        "emit({'protocol_version':2,'type':'summary','current':0,'total':1,'downloaded':0,"
        "'already_present':0,'no_video':0,'failed_count':0,'cancelled':1})\n",
        encoding="utf-8",
    )
    gateway = SubprocessDownloadGateway([sys.executable, str(worker)])
    request = DownloadRequest(
        lessons=(SelectedLesson(("Курс",), Lesson("Урок", "https://example.com")),),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )
    started = threading.Event()

    def on_event(event):
        if event.type is DownloadEventType.LESSON_STARTED:
            started.set()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(gateway.run, request, on_event)
        assert started.wait(5)
        gateway.cancel()
        summary = future.result(timeout=5)

    assert summary.cancelled == 1
    assert summary.processed == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows process-tree shutdown")
def test_shutdown_force_closes_worker_process_tree(tmp_path):
    worker = tmp_path / "stuck_worker.py"
    worker.write_text(
        "import argparse,json,os,subprocess,sys,time\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--request-file')\n"
        "p.add_argument('--events-file')\n"
        "p.add_argument('--commands-file')\n"
        "a=p.parse_args()\n"
        "request=json.load(open(a.request_file,encoding='utf-8'))\n"
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        "path=os.path.join(request['save_path'],'pids.json')\n"
        "open(path,'w').write(json.dumps([os.getpid(),child.pid]))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    gateway = SubprocessDownloadGateway([sys.executable, str(worker)])
    request = DownloadRequest(
        lessons=(SelectedLesson(("Курс",), Lesson("Урок", "https://example.com")),),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(gateway.run, request, lambda _: None)
        pid_file = tmp_path / "pids.json"
        for _ in range(100):
            if pid_file.is_file():
                break
            time.sleep(0.05)
        assert pid_file.is_file()
        pids = json.loads(pid_file.read_text(encoding="utf-8"))
        gateway.shutdown(0.1)
        with pytest.raises(DownloaderError):
            future.result(timeout=5)

    for pid in pids:
        assert not _process_exists(pid)
