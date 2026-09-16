import asyncio
import shutil

import pytest

from getcourse_downloader.infrastructure.media.ffmpeg import FfmpegMuxer
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
