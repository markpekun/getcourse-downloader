import asyncio
import json
from pathlib import Path

import aiohttp
import pytest

from getcourse_downloader.domain.events import DownloadEventType
from getcourse_downloader.infrastructure.media import hls as hls_module
from getcourse_downloader.infrastructure.media.hls import (
    HlsDownloader,
    HlsDownloadStatus,
    _checkpoint_path,
    _prepare_checkpoint,
    canonical_media_url,
    parse_key_tag,
    parse_media_resources,
    parse_session_key,
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


def test_parse_key_tag_accepts_kinescope_identity_key():
    tag = (
        "#EXT-X-KEY:METHOD=SAMPLE-AES,"
        'URI="https://license.kinescope.io/v2/vod/id/acquire/sample-aes/key",'
        'KEYFORMAT="identity",IV=0x00112233445566778899aabbccddeeff'
    )

    key = parse_key_tag(tag, "https://cdn.example/media.m3u8")

    assert key is not None
    assert key.method == "SAMPLE-AES"
    assert key.key_format == "identity"
    assert key.uri == "https://license.kinescope.io/v2/vod/id/acquire/sample-aes/key"


def test_parse_session_key_uses_master_url_for_relative_uri():
    key = parse_session_key(
        '#EXTM3U\n#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="../key"\n',
        "https://cdn.example/master/index.m3u8",
    )

    assert key is not None
    assert key.uri == "https://cdn.example/key"


def test_parse_media_resources_orders_map_and_explicit_and_implicit_ranges():
    playlist = (
        "#EXTM3U\n"
        '#EXT-X-MAP:URI="https://cdn.example/1080p.mp4",BYTERANGE="901@0"\n'
        "#EXT-X-BYTERANGE:100@901\nhttps://cdn.example/1080p.mp4?kcd=token\n"
        "#EXT-X-BYTERANGE:200\nhttps://cdn.example/1080p.mp4?kcd=token\n"
    )

    resources = parse_media_resources(playlist, "https://cdn.example/media.m3u8")

    assert [
        (resource.role, resource.byte_range.offset, resource.byte_range.length)
        for resource in resources
        if resource.byte_range is not None
    ] == [
        ("init", 0, 901),
        ("media", 901, 100),
        ("media", 1001, 200),
    ]
    assert resources[0].url == "https://cdn.example/1080p.mp4?kcd=token"


def test_parse_media_resources_rejects_implicit_first_range():
    playlist = "#EXTM3U\n#EXT-X-BYTERANGE:200\nhttps://cdn.example/1080p.mp4?kcd=token\n"

    with pytest.raises(ValueError, match="offset"):
        parse_media_resources(playlist, "https://cdn.example/media.m3u8")


class _Session:
    def __init__(self, responses: dict[str, _Response], requests: list[str], **_):
        self._responses = responses
        self._requests = requests

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    def get(self, url: str, **_) -> _Response:
        self._requests.append(url)
        return self._responses[url]


class _Muxer:
    def __init__(
        self,
        *,
        succeeds: bool = True,
        concat_succeeds: bool | None = None,
        decrypt_succeeds: bool = True,
        decrypt_file_succeeds: bool = True,
    ):
        self.succeeds = succeeds
        self.concat_succeeds = succeeds if concat_succeeds is None else concat_succeeds
        self.decrypt_succeeds = decrypt_succeeds
        self.decrypt_file_succeeds = decrypt_file_succeeds
        self.concat_sources: list[Path] = []
        self.mux_sources: list[Path] = []
        self.decrypt_sources: list[Path] = []
        self.decrypt_source_bytes: list[bytes] = []
        self.decryption_keys: list[str] = []
        self.decrypt_file_sources: list[bytes] = []

    async def mux_concat(
        self,
        source: Path,
        destination: Path,
        *,
        is_cancelled=None,
    ) -> tuple[bool, str]:
        del is_cancelled
        self.concat_sources.append(source)
        if not self.concat_succeeds:
            return False, "concat boom"
        segments = [
            Path(line.removeprefix("file '").removesuffix("'"))
            for line in source.read_text(encoding="utf-8").splitlines()
            if line.startswith("file '")
        ]
        destination.write_bytes(b"".join(segment.read_bytes() for segment in segments))
        return True, ""

    async def mux(
        self,
        source: Path,
        destination: Path,
        *,
        is_cancelled=None,
    ) -> tuple[bool, str]:
        del is_cancelled
        self.mux_sources.append(source)
        if not self.succeeds:
            return False, "boom"
        destination.write_bytes(source.read_bytes())
        return True, ""

    async def decrypt_fragments(
        self,
        sources,
        destination: Path,
        *,
        decryption_key_hex: str,
        is_cancelled=None,
    ) -> tuple[bool, str]:
        del is_cancelled
        self.decrypt_sources = list(sources)
        self.decrypt_source_bytes = [path.read_bytes() for path in sources]
        self.decryption_keys.append(decryption_key_hex)
        if not self.decrypt_succeeds:
            return False, "Invalid data found when processing input"
        destination.write_bytes(b"".join(path.read_bytes() for path in sources))
        return True, ""

    async def decrypt_file(
        self,
        source: Path,
        destination: Path,
        *,
        decryption_key_hex: str,
        is_cancelled=None,
    ) -> tuple[bool, str]:
        del decryption_key_hex, is_cancelled
        self.decrypt_file_sources.append(source.read_bytes())
        if not self.decrypt_file_succeeds:
            return False, "decrypt failed"
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


def test_kinescope_identity_key_is_requested_once_and_passed_to_ffmpeg(tmp_path):
    playlist_url = "https://cdn.example/media.m3u8"
    key_url = "https://license.example/key"
    signed_mp4 = "https://cdn.example/1080p.mp4?kcd=token"
    playlist = (
        "#EXTM3U\n"
        '#EXT-X-MAP:URI="https://cdn.example/1080p.mp4",BYTERANGE="4@0"\n'
        f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{key_url}",KEYFORMAT="identity"\n'
        f"#EXT-X-BYTERANGE:5@4\n{signed_mp4}\n"
    )
    requests: list[str] = []
    muxer = _Muxer()
    responses = {
        playlist_url: _Response(text=playlist),
        key_url: _Response(content=b"ml1C_JjWlcjeDEo="),
        signed_mp4: _Response(content=b"initmedia"),
    }

    result, _ = _run(
        _downloader(responses, requests, muxer=muxer),
        playlist_url,
        tmp_path / "Lesson",
    )

    assert result.status is HlsDownloadStatus.DOWNLOADED
    assert result.output_path == tmp_path / "Lesson_720.mp4"
    assert requests.count(key_url) == 1
    assert muxer.decryption_keys == ["6d6c31435f4a6a576c636a6544456f3d"]
    assert muxer.decrypt_source_bytes == [b"init", b"media"]


def test_kinescope_key_http_403_reports_expired_access(tmp_path):
    playlist_url = "https://cdn.example/media.m3u8"
    key_url = "https://license.example/key"
    playlist = f'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{key_url}"\n#EXTINF:1,\nsegment.mp4\n'
    forbidden = aiohttp.ClientResponseError(
        request_info=None,
        history=(),
        status=403,
        message="Forbidden",
        headers=None,
    )
    responses = {
        playlist_url: _Response(text=playlist),
        key_url: _Response(error=forbidden),
    }

    result, events = _run(_downloader(responses, []), playlist_url, tmp_path / "Lesson")

    assert result.status is HlsDownloadStatus.FAILED
    assert events[-1].error_code == "HLS_KEY_HTTP_403"
    assert events[-1].message == (
        "Ссылка для получения ключа истекла. Перезапустите загрузку урока."
    )


def test_kinescope_stream_failure_uses_local_encrypted_file_without_redownload(tmp_path):
    playlist_url = "https://cdn.example/media.m3u8"
    key_url = "https://license.example/key"
    signed_mp4 = "https://cdn.example/1080p.mp4?kcd=token"
    playlist = (
        "#EXTM3U\n"
        '#EXT-X-MAP:URI="https://cdn.example/1080p.mp4",BYTERANGE="4@0"\n'
        f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{key_url}"\n'
        f"#EXT-X-BYTERANGE:5@4\n{signed_mp4}\n"
    )
    requests: list[str] = []
    muxer = _Muxer(decrypt_succeeds=False)
    responses = {
        playlist_url: _Response(text=playlist),
        key_url: _Response(content=b"ml1C_JjWlcjeDEo="),
        signed_mp4: _Response(content=b"initmedia"),
    }

    result, _ = _run(
        _downloader(responses, requests, muxer=muxer),
        playlist_url,
        tmp_path / "Lesson",
    )

    assert result.status is HlsDownloadStatus.DOWNLOADED
    assert muxer.decrypt_file_sources == [b"initmedia"]
    assert requests.count(signed_mp4) == 2
    assert not _checkpoint_path(tmp_path / "Lesson.mp4").exists()


def test_kinescope_ranges_send_exact_inclusive_http_headers(tmp_path):
    playlist_url = "https://cdn.example/media.m3u8"
    key_url = "https://license.example/key"
    signed_mp4 = "https://cdn.example/1080p.mp4?kcd=token"
    playlist = (
        "#EXTM3U\n"
        '#EXT-X-MAP:URI="https://cdn.example/1080p.mp4",BYTERANGE="4@0"\n'
        f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{key_url}"\n'
        f"#EXT-X-BYTERANGE:5@4\n{signed_mp4}\n"
    )
    requested_ranges: list[str | None] = []

    class RangeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

        def get(self, url, *, headers=None):
            requested_ranges.append(headers.get("Range") if headers else None)
            if url == playlist_url:
                return _Response(text=playlist)
            if url == key_url:
                return _Response(content=b"ml1C_JjWlcjeDEo=")
            ranges = {"bytes=0-3": b"init", "bytes=4-8": b"media"}
            return _Response(content=ranges[headers["Range"]])

    muxer = _Muxer()
    downloader = HlsDownloader(
        muxer,  # type: ignore[arg-type]
        concurrency=1,
        session_factory=lambda **_: RangeSession(),
    )

    result, _ = _run(downloader, playlist_url, tmp_path / "Lesson")

    assert result.status is HlsDownloadStatus.DOWNLOADED
    assert requested_ranges == [None, None, "bytes=0-3", "bytes=4-8"]
    assert muxer.decrypt_source_bytes == [b"init", b"media"]


def test_failed_kinescope_checkpoint_contains_no_key_or_signed_query(tmp_path):
    playlist_url = "https://cdn.example/media.m3u8?expires=123"
    key_url = "https://license.example/key?sign=secret"
    signed_mp4 = "https://cdn.example/1080p.mp4?kcd=secret-token"
    playlist = (
        "#EXTM3U\n"
        '#EXT-X-MAP:URI="https://cdn.example/1080p.mp4",BYTERANGE="4@0"\n'
        f'#EXT-X-KEY:METHOD=SAMPLE-AES,URI="{key_url}"\n'
        f"#EXT-X-BYTERANGE:5@4\n{signed_mp4}\n"
    )
    muxer = _Muxer(decrypt_succeeds=False, decrypt_file_succeeds=False)
    responses = {
        playlist_url: _Response(text=playlist),
        key_url: _Response(content=b"ml1C_JjWlcjeDEo="),
        signed_mp4: _Response(content=b"initmedia"),
    }

    result, _ = _run(_downloader(responses, [], muxer=muxer), playlist_url, tmp_path / "Lesson")

    assert result.status is HlsDownloadStatus.FAILED
    manifest = (_checkpoint_path(tmp_path / "Lesson.mp4") / "manifest.json").read_text(
        encoding="utf-8"
    )
    assert "ml1C_JjWlcjeDEo=" not in manifest
    assert "6d6c31435f4a6a576c636a6544456f3d" not in manifest
    assert "secret" not in manifest


@pytest.mark.parametrize(
    "key",
    [b"", b"x" * 15, b"x" * 17, b"\xff" * 16, b"<html>key unavailable</html>"],
)
def test_invalid_sample_aes_key_never_commits_segment(tmp_path, key):
    url = "https://cdn/list.m3u8"
    requests = []
    responses = {
        url: _Response(text='#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\nseg.ts\n'),
        "https://cdn/key": _Response(content=key),
        "https://cdn/seg.ts": _Response(content=b"encrypted"),
    }
    result, events = _run(_downloader(responses, requests), url, tmp_path / "Lesson")
    assert result.status is HlsDownloadStatus.FAILED
    assert events[-1].error_code == "HLS_KEY_INVALID"
    assert not list(_checkpoint_path(tmp_path / "Lesson.mp4").rglob("*.bin"))


@pytest.mark.parametrize(
    ("tag", "expected_code"),
    [
        ("METHOD=SAMPLE-AES", "HLS_KEY_INVALID"),
        ('METHOD=SAMPLE-AES,URI="skd://key"', "HLS_KEY_INVALID"),
        (
            'METHOD=SAMPLE-AES,URI="key",KEYFORMAT="com.apple.streamingkeydelivery"',
            "ENCRYPTED_PLAYLIST_UNSUPPORTED",
        ),
    ],
)
def test_invalid_sample_aes_tag_fails_before_segment_download(tmp_path, tag, expected_code):
    url = "https://cdn/list.m3u8"
    requests = []
    result, events = _run(
        _downloader({url: _Response(text=f"#EXTM3U\n#EXT-X-KEY:{tag}\na.ts\n")}, requests),
        url,
        tmp_path / "Lesson",
    )
    assert result.status is HlsDownloadStatus.FAILED
    assert events[-1].error_code == expected_code
    assert requests == [url]


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
    assert output.read_bytes() == b""
    assert (tmp_path / "Lesson_720.mp4").read_bytes() == b"segment"


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
    assert (tmp_path / "Lesson_720.mp4").read_bytes() == b"AB"
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
    assert manifest["resources"] == [
        {"role": "media", "url": "https://cdn/new.ts", "offset": None, "length": None}
    ]


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
    assert (tmp_path / "Lesson_720.mp4").is_file()


def test_fast_concat_assembles_mp4_without_intermediate_transport_stream(tmp_path):
    stem = tmp_path / "Lesson"
    playlist = "#EXTM3U\n#EXTINF:1,\na.ts\n#EXTINF:1,\nb.ts\n"
    responses = {
        "https://cdn/master.m3u8": _Response(text=playlist),
        "https://cdn/a.ts": _Response(content=b"A"),
        "https://cdn/b.ts": _Response(content=b"B"),
    }
    muxer = _Muxer()

    result, events = _run(_downloader(responses, [], muxer=muxer), "https://cdn/master.m3u8", stem)

    assert result.status is HlsDownloadStatus.DOWNLOADED
    assert (tmp_path / "Lesson_720.mp4").read_bytes() == b"AB"
    assert len(muxer.concat_sources) == 1
    assert muxer.mux_sources == []
    assert [event for event in events if event.type is DownloadEventType.LOG] == []


def test_failed_fast_concat_falls_back_to_transport_stream_and_cleans_checkpoint(tmp_path):
    stem = tmp_path / "Lesson"
    playlist = "#EXTM3U\n#EXTINF:1,\nseg.ts\n"
    responses = {
        "https://cdn/master.m3u8": _Response(text=playlist),
        "https://cdn/seg.ts": _Response(content=b"segment"),
    }
    muxer = _Muxer(concat_succeeds=False)

    result, events = _run(_downloader(responses, [], muxer=muxer), "https://cdn/master.m3u8", stem)

    assert result.status is HlsDownloadStatus.DOWNLOADED
    assert (tmp_path / "Lesson_720.mp4").read_bytes() == b"segment"
    assert len(muxer.concat_sources) == 1
    assert len(muxer.mux_sources) == 1
    assert [event for event in events if event.type is DownloadEventType.LOG] == []
    assert not _checkpoint_path(tmp_path / "Lesson.mp4").exists()


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
    assert canonical_media_url("https://cdn.example/a.mp4?kcd=rotating") == (
        "https://cdn.example/a.mp4"
    )


def test_resume_does_not_reuse_segments_for_a_different_query_media_identity(tmp_path):
    output = tmp_path / "Lesson.mp4"
    arguments = {
        "lesson_url": "https://school/lesson/1",
        "requested_quality": "auto",
        "playlist_url": "https://cdn/master.m3u8?token=old",
    }
    _, paths, _ = _prepare_checkpoint(
        output,
        **arguments,
        segment_urls=["https://cdn/segment.ts?video=first&token=old"],
    )
    paths[0].write_bytes(b"old video")

    checkpoint, paths, resumed = _prepare_checkpoint(
        output,
        **arguments,
        segment_urls=["https://cdn/segment.ts?video=second&token=new"],
    )

    assert resumed == 0
    assert not paths[0].exists()
    manifest = (checkpoint / "manifest.json").read_text(encoding="utf-8")
    assert "second" not in manifest
    assert "token=" not in manifest


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
