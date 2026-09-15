import asyncio
import json
from pathlib import Path

import aiohttp

from getcourse_downloader.domain.events import DownloadEventType
from getcourse_downloader.infrastructure.media import hls as hls_module
from getcourse_downloader.infrastructure.media.hls import (
    HlsDownloader,
    HlsDownloadStatus,
    _checkpoint_path,
    _prepare_checkpoint,
    canonical_media_url,
)


class _Response:
    def __init__(self, *, text: str = "", content: bytes = b"", error: Exception | None = None):
        self._text = text
        self._content = content
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def raise_for_status(self) -> None:
        if self._error:
            raise self._error

    async def text(self) -> str:
        return self._text

    async def read(self) -> bytes:
        return self._content


class _Session:
    def __init__(self, responses: dict[str, _Response], requests: list[str], **_):
        self._responses = responses
        self._requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def get(self, url: str) -> _Response:
        self._requests.append(url)
        return self._responses[url]


class _Muxer:
    def __init__(self, *, succeeds: bool = True):
        self.succeeds = succeeds

    async def mux(
        self,
        source: Path,
        destination: Path,
        *,
        is_cancelled=None,
    ) -> tuple[bool, str]:
        del is_cancelled
        if not self.succeeds:
            return False, "boom"
        destination.write_bytes(source.read_bytes())
        return True, ""

    async def probe_height(self, _media: Path) -> int | None:
        return 720


def _downloader(responses, requests, *, muxer=None) -> HlsDownloader:
    return HlsDownloader(
        muxer or _Muxer(),  # type: ignore[arg-type]
        concurrency=1,
        session_factory=lambda **kwargs: _Session(responses, requests, **kwargs),
    )


def _run(downloader: HlsDownloader, playlist_url: str, stem: Path):
    events = []
    result = asyncio.run(
        downloader.download(
            playlist_url,
            stem,
            "Урок",
            events.append,
            lesson_url="https://school/lesson/1",
            course_path=("Курс",),
        )
    )
    return result, events


def test_existing_nonempty_mp4_is_skipped_but_zero_file_is_incomplete(tmp_path):
    stem = tmp_path / "Lesson"
    output = tmp_path / "Lesson.mp4"
    output.write_bytes(b"done")
    requests: list[str] = []
    result, _ = _run(_downloader({}, requests), "https://cdn/master.m3u8", stem)
    assert result.status is HlsDownloadStatus.ALREADY_PRESENT
    assert requests == []

    output.write_bytes(b"")
    playlist = "#EXTM3U\n#EXTINF:1,\nseg.ts?token=new\n"
    responses = {
        "https://cdn/master.m3u8": _Response(text=playlist),
        "https://cdn/seg.ts?token=new": _Response(content=b"segment"),
    }
    result, _ = _run(_downloader(responses, requests), "https://cdn/master.m3u8", stem)
    assert result.status is HlsDownloadStatus.DOWNLOADED
    assert output.read_bytes() == b"segment"


def test_resume_reuses_segments_when_only_query_tokens_change(tmp_path):
    stem = tmp_path / "Lesson"
    output = tmp_path / "Lesson.mp4"
    old_segments = ["https://cdn/a.ts?token=old", "https://cdn/b.ts?token=old"]
    checkpoint, paths, _ = _prepare_checkpoint(
        output,
        lesson_url="https://school/lesson/1",
        requested_quality="auto",
        playlist_url="https://cdn/master.m3u8?token=old",
        segment_urls=old_segments,
    )
    paths[0].write_bytes(b"A")
    (paths[1].with_suffix(".tmp")).write_bytes(b"partial")

    new_playlist = "#EXTM3U\n#EXTINF:1,\na.ts?token=new\n#EXTINF:1,\nb.ts?token=new\n"
    requests: list[str] = []
    responses = {
        "https://cdn/master.m3u8?token=new": _Response(text=new_playlist),
        "https://cdn/b.ts?token=new": _Response(content=b"B"),
    }
    result, events = _run(
        _downloader(responses, requests),
        "https://cdn/master.m3u8?token=new",
        stem,
    )
    assert result.status is HlsDownloadStatus.DOWNLOADED
    assert result.resumed_segments == 1
    assert requests == ["https://cdn/master.m3u8?token=new", "https://cdn/b.ts?token=new"]
    assert events[0].current == 1
    assert output.read_bytes() == b"AB"
    assert not checkpoint.exists()


def test_incompatible_segment_list_resets_only_checkpoint(tmp_path):
    output = tmp_path / "Lesson.mp4"
    checkpoint, paths, _ = _prepare_checkpoint(
        output,
        lesson_url="https://school/lesson/1",
        requested_quality="auto",
        playlist_url="https://cdn/master.m3u8",
        segment_urls=["https://cdn/old.ts"],
    )
    paths[0].write_bytes(b"old")
    unrelated = tmp_path / "keep.txt"
    unrelated.write_text("keep", encoding="utf-8")

    _, new_paths, resumed = _prepare_checkpoint(
        output,
        lesson_url="https://school/lesson/1",
        requested_quality="auto",
        playlist_url="https://cdn/master.m3u8",
        segment_urls=["https://cdn/new.ts"],
    )
    assert resumed == 0
    assert not new_paths[0].exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    manifest = json.loads((checkpoint / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["segments"] == ["https://cdn/new.ts"]


def test_failed_mux_preserves_checkpoint_and_success_cleans_it(tmp_path):
    stem = tmp_path / "Lesson"
    output = tmp_path / "Lesson.mp4"
    playlist = "#EXTM3U\n#EXTINF:1,\nseg.ts\n"
    responses = {
        "https://cdn/master.m3u8": _Response(text=playlist),
        "https://cdn/seg.ts": _Response(content=b"segment"),
    }
    failed, _ = _run(
        _downloader(responses, [], muxer=_Muxer(succeeds=False)),
        "https://cdn/master.m3u8",
        stem,
    )
    checkpoint = _checkpoint_path(output)
    assert failed.status is HlsDownloadStatus.FAILED
    assert checkpoint.is_dir()
    assert not output.exists()

    succeeded, _ = _run(_downloader(responses, []), "https://cdn/master.m3u8", stem)
    assert succeeded.status is HlsDownloadStatus.DOWNLOADED
    assert not checkpoint.exists()
    assert output.is_file()


def test_segment_network_failure_preserves_downloaded_checkpoint(tmp_path):
    stem = tmp_path / "Lesson"
    output = tmp_path / "Lesson.mp4"
    playlist = "#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n"
    requests: list[str] = []
    responses = {
        "https://cdn/master.m3u8": _Response(text=playlist),
        "https://cdn/a.ts": _Response(content=b"A"),
        "https://cdn/b.ts": _Response(error=OSError("network down")),
    }

    result, _ = _run(_downloader(responses, requests), "https://cdn/master.m3u8", stem)

    checkpoint = _checkpoint_path(output)
    assert result.status is HlsDownloadStatus.FAILED
    assert (checkpoint / "segments" / "000000.bin").read_bytes() == b"A"
    assert not (checkpoint / "segments" / "000001.bin").exists()
    assert not output.exists()


def test_segment_http_failure_has_safe_code_host_and_progress_for_diagnostics(tmp_path):
    stem = tmp_path / "Lesson"
    playlist = "#EXTM3U\n#EXTINF:1,\nseg.ts?token=secret\n"
    requests: list[str] = []
    responses = {
        "https://cdn.example/master.m3u8?sign=secret": _Response(text=playlist),
        "https://cdn.example/seg.ts?token=secret": _Response(
            error=aiohttp.ClientResponseError(
                request_info=None,
                history=(),
                status=403,
                message="Forbidden",
                headers=None,
            )
        ),
    }

    result, events = _run(
        _downloader(responses, requests),
        "https://cdn.example/master.m3u8?sign=secret",
        stem,
    )

    assert result.status is HlsDownloadStatus.FAILED
    failure = events[-1]
    assert failure.error_code == "HTTP_403"
    assert failure.source_host == "cdn.example"
    assert (failure.current, failure.total) == (0, 1)
    assert "secret" not in failure.message


def test_unknown_playlist_failure_is_not_misreported_as_segment_failure(tmp_path):
    playlist_url = "https://cdn.example/master.m3u8?sign=secret"
    responses = {playlist_url: _Response(error=OSError("connection reset"))}

    result, events = _run(_downloader(responses, []), playlist_url, tmp_path / "Lesson")

    assert result.status is HlsDownloadStatus.FAILED
    assert events[-1].stage == "playlist"
    assert events[-1].error_code == "PLAYLIST_REQUEST_FAILED"
    assert events[-1].message == "Не удалось получить плейлист видео"


def test_encrypted_hls_is_rejected_before_segments_are_downloaded(tmp_path):
    stem = tmp_path / "Lesson"
    playlist_url = "https://cdn.example/encrypted.m3u8?sign=abc"
    segment_url = "https://cdn.example/encrypted.ts"
    playlist = (
        "#EXTM3U\n"
        '#EXT-X-KEY:METHOD=SAMPLE-AES,KEYFORMAT="org.w3.clearkey",URI="license"\n'
        "#EXTINF:1,\n"
        "encrypted.ts\n"
    )
    requests: list[str] = []
    responses = {
        playlist_url: _Response(text=playlist),
        segment_url: _Response(content=b"encrypted"),
    }

    result, events = _run(_downloader(responses, requests), playlist_url, stem)

    assert result.status is HlsDownloadStatus.FAILED
    assert requests == [playlist_url]
    assert events[-1].type is DownloadEventType.ERROR
    assert "SAMPLE-AES" in events[-1].message


def test_kinescope_referer_and_origin_are_used_for_signed_hls_requests(tmp_path):
    playlist_url = "https://kinescope.io/video-id/video.m3u8?expires=123&sign=abc"
    segment_url = "https://kinescope.io/video-id/segment.ts?expires=123&sign=abc"
    responses = {
        playlist_url: _Response(text="#EXTM3U\n#EXTINF:1,\nsegment.ts?expires=123&sign=abc\n"),
        segment_url: _Response(content=b"segment"),
    }
    requests: list[str] = []
    session_options = {}

    def session_factory(**options):
        session_options.update(options)
        return _Session(responses, requests, **options)

    downloader = HlsDownloader(
        _Muxer(),  # type: ignore[arg-type]
        concurrency=1,
        session_factory=session_factory,
    )

    result = asyncio.run(
        downloader.download(
            playlist_url,
            tmp_path / "Lesson",
            "Урок",
            lambda _: None,
            lesson_url="https://school.example/lesson/1",
            referer_url="https://kinescope.io/embed/public-id",
        )
    )

    assert result.status is HlsDownloadStatus.DOWNLOADED
    assert session_options["headers"]["Referer"] == "https://kinescope.io/embed/public-id"
    assert session_options["headers"]["Origin"] == "https://kinescope.io"


def test_cancel_preserves_segments_and_next_run_resumes(tmp_path):
    stem = tmp_path / "Lesson"
    playlist = "#EXTM3U\n#EXTINF:1,\na.ts?token=old\n#EXTINF:1,\nb.ts?token=old\n"
    first_requests: list[str] = []
    first_events = []
    first = _downloader(
        {
            "https://cdn/master.m3u8?token=old": _Response(text=playlist),
            "https://cdn/a.ts?token=old": _Response(content=b"A"),
            "https://cdn/b.ts?token=old": _Response(content=b"B"),
        },
        first_requests,
    )

    result = asyncio.run(
        first.download(
            "https://cdn/master.m3u8?token=old",
            stem,
            "Урок",
            first_events.append,
            lesson_url="https://school/lesson/1",
            course_path=("Курс",),
            is_cancelled=lambda: any(
                event.type is DownloadEventType.PROGRESS and event.current == 1
                for event in first_events
            ),
        )
    )

    assert result.status is HlsDownloadStatus.CANCELLED
    checkpoint = _checkpoint_path(tmp_path / "Lesson.mp4")
    assert checkpoint.is_dir()
    assert (checkpoint / "segments" / "000000.bin").read_bytes() == b"A"

    new_playlist = "#EXTM3U\n#EXTINF:1,\na.ts?token=new\n#EXTINF:1,\nb.ts?token=new\n"
    second_requests: list[str] = []
    resumed, _ = _run(
        _downloader(
            {
                "https://cdn/master.m3u8?token=new": _Response(text=new_playlist),
                "https://cdn/b.ts?token=new": _Response(content=b"B"),
            },
            second_requests,
        ),
        "https://cdn/master.m3u8?token=new",
        stem,
    )

    assert resumed.status is HlsDownloadStatus.DOWNLOADED
    assert "https://cdn/a.ts?token=new" not in second_requests
    assert "https://cdn/b.ts?token=new" in second_requests
    assert not checkpoint.exists()


def test_canonical_media_url_ignores_query_and_fragment():
    assert canonical_media_url("HTTPS://CDN.Example/a.ts?token=1#x") == "https://cdn.example/a.ts"


def test_average_speed_is_reported_every_three_seconds_and_on_completion(
    tmp_path,
    monkeypatch,
):
    playlist = "#EXTM3U\n" + "".join(f"#EXTINF:1,\n{index}.ts\n" for index in range(1, 5))
    responses = {"https://cdn/master.m3u8": _Response(text=playlist)}
    responses.update(
        {f"https://cdn/{index}.ts": _Response(content=b"x" * 100) for index in range(1, 5)}
    )
    monotonic_values = iter((0.0, 1.0, 2.0, 3.0, 4.0))
    monkeypatch.setattr(hls_module, "_monotonic", lambda: next(monotonic_values))

    result, events = _run(
        _downloader(responses, []),
        "https://cdn/master.m3u8",
        tmp_path / "Lesson",
    )

    assert result.status is HlsDownloadStatus.DOWNLOADED
    speeds = [event.speed_bps for event in events if event.type is DownloadEventType.PROGRESS]
    assert speeds == [None, None, 100.0, 100.0]
