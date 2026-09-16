from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from getcourse_downloader.infrastructure.media.hls import parse_sample_aes_segments
from getcourse_downloader.infrastructure.media.sample_aes import decrypt_sample_aes_ts

_FIXTURES = Path(__file__).resolve().parents[1] / "debug-sample-aes"
_REQUIRED = ("playlist.m3u8", "key.bin", "seg0.ts", "seg1.ts")


def _fixture_path(name: str) -> Path:
    path = _FIXTURES / name
    if not path.is_file():
        pytest.skip(f"local SAMPLE-AES fixture is absent: {path}")
    return path


def _probe(path: Path) -> dict[str, object]:
    executable = shutil.which("ffprobe")
    if executable is None:
        pytest.skip("ffprobe is unavailable")
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,codec_name",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    payload = json.loads(result.stdout)
    assert isinstance(payload, dict)
    return cast(dict[str, object], payload)


@pytest.mark.integration
def test_authorized_sample_aes_fixture_decrypts_and_muxes_to_playable_mp4(tmp_path):
    playlist = _fixture_path("playlist.m3u8").read_text(encoding="utf-8")
    key = _fixture_path("key.bin").read_bytes()
    segments = parse_sample_aes_segments(playlist, "https://fixtures.invalid/playlist.m3u8")
    assert len(segments) >= 2
    assert len(key) == 16

    clear_segments: list[Path] = []
    for index, name in enumerate(("seg0.ts", "seg1.ts")):
        segment = segments[index]
        assert segment.sample_aes_key is not None
        iv = segment.iv or segment.sequence_number.to_bytes(16, "big")
        clear = tmp_path / name
        clear.write_bytes(decrypt_sample_aes_ts(_fixture_path(name).read_bytes(), key, iv))
        clear_segments.append(clear)

    first_segment = _probe(clear_segments[0])
    streams = first_segment["streams"]
    assert isinstance(streams, list)
    codec_types = {
        stream["codec_type"]
        for stream in streams
        if isinstance(stream, dict) and isinstance(stream.get("codec_type"), str)
    }
    assert {"video", "audio"} <= codec_types

    transport_stream = tmp_path / "lesson.ts"
    transport_stream.write_bytes(b"".join(path.read_bytes() for path in clear_segments))
    mp4 = tmp_path / "lesson.mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        pytest.skip("ffmpeg is unavailable")
    mux = subprocess.run(
        [ffmpeg, "-v", "error", "-y", "-i", str(transport_stream), "-c", "copy", str(mp4)],
        capture_output=True,
        check=False,
        text=True,
    )
    assert mux.returncode == 0, mux.stderr
    assert mux.stderr == ""

    probe = _probe(mp4)
    expected_duration = sum(
        float(line.split(":", 1)[1].rstrip(","))
        for line in playlist.splitlines()
        if line.startswith("#EXTINF:")
    )
    format_info = probe["format"]
    assert isinstance(format_info, dict)
    actual_duration = float(format_info["duration"])
    assert actual_duration == pytest.approx(expected_duration, abs=0.2)
