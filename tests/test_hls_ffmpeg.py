from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
from aiohttp import web

from getcourse_downloader.infrastructure.media.ffmpeg import FfmpegMuxer
from getcourse_downloader.infrastructure.media.hls import (
    HlsDownloader,
    HlsDownloadStatus,
    _write_concat_list,
)
from getcourse_downloader.infrastructure.platform.paths import AppPaths

pytestmark = pytest.mark.integration


@pytest.fixture
def media_tools(tmp_path):
    muxer = FfmpegMuxer(
        AppPaths(tmp_path / "data", tmp_path / "session", Path(__file__).parents[1] / "resources")
    )
    try:
        muxer.executable()
    except FileNotFoundError:
        pytest.skip("FFmpeg is required for the local media regression")
    if muxer.probe_executable() is None:
        pytest.skip("FFprobe is required to verify the local media regression")
    return muxer


def _generate_fmp4_hls(muxer: FfmpegMuxer, folder: Path, kind: str) -> None:
    folder.mkdir()
    source = "testsrc2=size=160x90:rate=10" if kind == "video" else "sine=frequency=440"
    codec = ["-c:v", "libx264", "-g", "10", "-an"] if kind == "video" else ["-c:a", "aac", "-vn"]
    process = subprocess.run(
        [
            muxer.executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            source,
            "-t",
            "2",
            *codec,
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_segment_type",
            "fmp4",
            "-hls_segment_filename",
            "segment%d.m4s",
            "media.m3u8",
        ],
        cwd=folder,
        capture_output=True,
        timeout=30,
    )
    assert process.returncode == 0, process.stderr.decode(errors="replace")


def _probe(muxer: FfmpegMuxer, path: Path) -> dict:
    process = subprocess.run(
        [
            muxer.probe_executable(),
            "-v",
            "error",
            "-count_packets",
            "-show_entries",
            "stream=codec_type,nb_read_packets:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        timeout=15,
    )
    assert process.returncode == 0, process.stderr.decode(errors="replace")
    return json.loads(process.stdout)


def test_fmp4_hls_downloads_and_merges_real_video_and_audio(media_tools, tmp_path):
    for kind in ("video", "audio"):
        _generate_fmp4_hls(media_tools, tmp_path / kind, kind)
    payloads = {
        f"/{kind}/{path.name}": path.read_bytes()
        for kind in ("video", "audio")
        for path in (tmp_path / kind).iterdir()
    }

    async def scenario():
        async def serve(request):
            body = payloads.get(request.path)
            if body is None:
                raise web.HTTPNotFound()
            return web.Response(body=body)

        app = web.Application()
        app.router.add_get("/{kind}/{name}", serve)
        runner = web.AppRunner(app)
        await runner.setup()
        server = await asyncio.get_running_loop().create_server(runner.server, "127.0.0.1", 0)
        origin = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        try:
            downloader = HlsDownloader(media_tools, concurrency=1)
            video = await downloader.download(
                f"{origin}/video/media.m3u8", tmp_path / "Video", "Local", lambda _: None
            )
            audio = await downloader.download(
                f"{origin}/audio/media.m3u8", tmp_path / "Audio", "Local", lambda _: None
            )
            assert video.status is audio.status is HlsDownloadStatus.DOWNLOADED
            assert video.output_path is not None and audio.output_path is not None
            for kind, result in (("video", video), ("audio", audio)):
                streams = _probe(media_tools, result.output_path)["streams"]
                assert [stream["codec_type"] for stream in streams] == [kind]
                assert int(streams[0]["nb_read_packets"]) > 0
            final = tmp_path / "merged.mp4"
            success, message = await downloader.merge_audio_video(
                video.output_path, audio.output_path, final
            )
            assert success, message
            payload = _probe(media_tools, final)
            assert {stream["codec_type"] for stream in payload["streams"]} == {"video", "audio"}
            assert all(int(stream["nb_read_packets"]) > 0 for stream in payload["streams"])
            assert float(payload["format"]["duration"]) == pytest.approx(2, abs=0.1)
            decoded = subprocess.run(
                [media_tools.executable(), "-v", "error", "-i", str(final), "-f", "null", "-"],
                capture_output=True,
                timeout=15,
            )
            assert decoded.returncode == 0, decoded.stderr.decode(errors="replace")
        finally:
            server.close()
            await server.wait_closed()
            await runner.cleanup()

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["mux", "mux_concat", "decrypt_file", "decrypt_fragments"])
def test_ffmpeg_rejects_init_without_media_packets(media_tools, tmp_path, operation):
    _generate_fmp4_hls(media_tools, tmp_path / "video", "video")
    init = tmp_path / "video" / "init.mp4"
    destination = tmp_path / "empty.mp4"

    async def scenario():
        if operation == "mux_concat":
            listing = tmp_path / "init.ffconcat"
            _write_concat_list(listing, [init])
            return await media_tools.mux_concat(listing, destination)
        if operation == "decrypt_file":
            return await media_tools.decrypt_file(init, destination, decryption_key_hex="00" * 16)
        if operation == "decrypt_fragments":
            return await media_tools.decrypt_fragments(
                [init], destination, decryption_key_hex="00" * 16
            )
        return await media_tools.mux(init, destination)

    success, message = asyncio.run(scenario())
    assert not success
    assert message
