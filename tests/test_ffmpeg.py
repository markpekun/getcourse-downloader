import asyncio
import contextlib
import shutil
import sys
from pathlib import Path

import pytest

from getcourse_downloader.infrastructure.media.ffmpeg import FfmpegMuxer, _supports_sample_aes
from getcourse_downloader.infrastructure.platform.paths import AppPaths


def _paths(tmp_path) -> AppPaths:
    return AppPaths(
        data=tmp_path / "data",
        session=tmp_path / "session",
        resources=tmp_path / "resources",
    )


def test_get_ffmpeg_path_bundled(tmp_path):
    paths = _paths(tmp_path)
    paths.resources.mkdir()
    executable = paths.resources / "ffmpeg.exe"
    executable.write_bytes(b"")
    assert FfmpegMuxer(paths).executable() == str(executable.resolve())


def test_get_ffmpeg_path_system(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: "C:/ffmpeg/bin/ffmpeg.exe")
    assert FfmpegMuxer(_paths(tmp_path)).executable() == "C:/ffmpeg/bin/ffmpeg.exe"


def test_get_ffmpeg_path_not_found(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(FileNotFoundError):
        FfmpegMuxer(_paths(tmp_path)).executable()


def test_mux_concat_uses_ffmpeg_concat_demuxer(monkeypatch, tmp_path):
    captured: list[str] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def create_subprocess_exec(*args, **_kwargs):
        captured.extend(args)
        return Process()

    paths = _paths(tmp_path)
    paths.resources.mkdir()
    (paths.resources / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    success, message = asyncio.run(
        FfmpegMuxer(paths).mux_concat(tmp_path / "segments.ffconcat", tmp_path / "output.mp4")
    )

    assert (success, message) == (True, "")
    assert captured[1:] == [
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(tmp_path / "segments.ffconcat"),
        "-c",
        "copy",
        "-bsf:a",
        "aac_adtstoasc",
        str(tmp_path / "output.mp4"),
    ]


def test_decrypt_fragments_streams_sources_and_places_key_before_input(monkeypatch, tmp_path):
    captured: list[str] = []
    written = bytearray()

    class Stdin:
        def write(self, data):
            written.extend(data)

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    class Process:
        returncode = 0
        stdin = Stdin()

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def create_subprocess_exec(*args, **_kwargs):
        captured.extend(args)
        return Process()

    paths = _paths(tmp_path)
    paths.resources.mkdir()
    (paths.resources / "ffmpeg.exe").write_bytes(b"")
    init = tmp_path / "init.bin"
    fragment = tmp_path / "fragment.bin"
    init.write_bytes(b"init")
    fragment.write_bytes(b"fragment")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)

    success, message = asyncio.run(
        FfmpegMuxer(paths).decrypt_fragments(
            [init, fragment],
            tmp_path / "output.mp4",
            decryption_key_hex="00" * 16,
        )
    )

    assert (success, message) == (True, "")
    assert captured.index("-decryption_key") < captured.index("-i")
    assert captured[captured.index("-i") + 1] == "pipe:0"
    assert bytes(written) == b"initfragment"


@pytest.mark.parametrize(
    ("version_output", "supported"),
    [
        ("ffmpeg version 6.1.2\nlibavformat 60. 16.100", True),
        ("ffmpeg version N-125847\nlibavformat 62. 3.100", True),
        ("ffmpeg version 5.1.6\nlibavformat 59. 27.100", False),
        ("unexpected output", False),
    ],
)
def test_sample_aes_version_gate(version_output, supported):
    assert _supports_sample_aes(version_output) is supported


def test_decrypt_file_uses_local_input_and_copy(monkeypatch, tmp_path):
    captured: list[str] = []

    class Process:
        returncode = 0

        async def communicate(self):
            return b"", b""

        async def wait(self):
            return 0

    async def create_subprocess_exec(*args, **_kwargs):
        captured.extend(args)
        return Process()

    paths = _paths(tmp_path)
    paths.resources.mkdir()
    (paths.resources / "ffmpeg.exe").write_bytes(b"")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create_subprocess_exec)
    source = Path(tmp_path / "video.enc.mp4")

    success, message = asyncio.run(
        FfmpegMuxer(paths).decrypt_file(
            source,
            tmp_path / "output.mp4",
            decryption_key_hex="11" * 16,
        )
    )

    assert (success, message) == (True, "")
    assert captured[1:] == [
        "-y",
        "-v",
        "error",
        "-decryption_key",
        "11" * 16,
        "-i",
        str(source),
        "-map",
        "0",
        "-c",
        "copy",
        str(tmp_path / "output.mp4"),
    ]


@pytest.mark.parametrize("operation", ["mux", "probe"])
def test_task_cancellation_reaps_the_running_media_process(monkeypatch, tmp_path, operation):
    async def scenario():
        original_spawn = asyncio.create_subprocess_exec
        started = asyncio.Event()
        processes = []

        async def spawn(*_args, **kwargs):
            process = await original_spawn(
                sys.executable, "-c", "import time; time.sleep(30)", **kwargs
            )
            processes.append(process)
            started.set()
            return process

        muxer = FfmpegMuxer(_paths(tmp_path))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(muxer, "executable", lambda: sys.executable)
        monkeypatch.setattr(muxer, "probe_executable", lambda: sys.executable)
        coroutine = (
            muxer.mux(tmp_path / "source.ts", tmp_path / "output.mp4")
            if operation == "mux"
            else muxer.probe_height(tmp_path / "source.mp4")
        )
        task = asyncio.create_task(coroutine)
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            returncode = processes[0].returncode
        finally:
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await process.wait()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert returncode is not None

    asyncio.run(scenario())
