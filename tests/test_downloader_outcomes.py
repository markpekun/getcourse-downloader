import asyncio
from pathlib import Path

import pytest

from getcourse_downloader.domain.events import DownloadEventType
from getcourse_downloader.domain.models import DownloadRequest, Lesson, SelectedLesson, VideoQuality
from getcourse_downloader.infrastructure.getcourse import downloader as downloader_module
from getcourse_downloader.infrastructure.getcourse.downloader import (
    PlaywrightDownloadGateway,
    _Playlist,
)
from getcourse_downloader.infrastructure.media.hls import HlsDownloadResult, HlsDownloadStatus


class _Page:
    def __init__(self, *, player: bool, playlist: tuple[str, str] | None = None) -> None:
        self.url = "about:blank"
        self._player = player
        self._playlist = playlist
        self._handlers = []

    def on(self, event, handler):
        if event == "response":
            self._handlers.append(handler)

    async def goto(self, url, **_):
        self.url = url
        if self._playlist:
            playlist_url, text = self._playlist

            class Response:
                url = playlist_url

                async def text(self):
                    return text

            for handler in self._handlers:
                handler(Response())

    async def query_selector(self, _):
        return object() if self._player else None

    async def wait_for_load_state(self, *_, **__):
        return None

    async def wait_for_timeout(self, *_):
        return None

    async def close(self):
        return None


class _Browser:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page

    async def close(self):
        return None


class _Hls:
    async def download(self, *_, **__):
        raise AssertionError("HLS downloader should not be called")


class _SuccessfulHls:
    def __init__(self) -> None:
        self.calls = 0

    async def download(self, _playlist_url, output_stem, *_args, **_kwargs):
        self.calls += 1
        return HlsDownloadResult(
            HlsDownloadStatus.DOWNLOADED,
            output_path=Path(f"{output_stem}.mp4"),
        )


class _RecordingHls:
    def __init__(self) -> None:
        self.calls = []

    async def download(self, playlist_url, output_stem, *_args, **kwargs):
        self.calls.append((playlist_url, kwargs))
        return HlsDownloadResult(
            HlsDownloadStatus.DOWNLOADED,
            output_path=Path(f"{output_stem}.mp4"),
        )


class _KinescopeFrame:
    url = "https://kinescope.io/embed/public-id"

    async def content(self):
        return (
            r'<script>src="https:\/\/kinescope.io\/video-id\/master.m3u8'
            r'?expires=123\u0026sign=master"</script>'
        )

    async def evaluate(self, _script, playlist_url):
        assert playlist_url == ("https://kinescope.io/video-id/master.m3u8?expires=123&sign=master")
        return {
            "url": playlist_url,
            "text": (
                "#EXTM3U\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720\n"
                "720.m3u8?expires=123&sign=low\n"
                "#EXT-X-STREAM-INF:BANDWIDTH=3200000,RESOLUTION=1920x1080\n"
                "1080.m3u8?expires=123&sign=high\n"
            ),
        }


class _KinescopePage(_Page):
    def __init__(self) -> None:
        super().__init__(player=True)
        self.frames = [_KinescopeFrame()]


class _LateKinescopePage(_Page):
    def __init__(self) -> None:
        super().__init__(player=True)
        self._frame_reads = 0

    @property
    def frames(self):
        self._frame_reads += 1
        return [] if self._frame_reads == 1 else [_KinescopeFrame()]


class _DashResponse:
    url = "https://cdn.example/video/manifest.mpd?token=secret"
    status = 200

    async def all_headers(self):
        return {"content-type": "application/dash+xml"}


class _DashPage(_Page):
    async def goto(self, url, **kwargs):
        await super().goto(url, **kwargs)
        for handler in self._handlers:
            handler(_DashResponse())


class _MediaApiResponse:
    url = "https://player.example/api/video/session?token=secret"
    status = 200

    async def all_headers(self):
        return {"content-type": "application/json"}


class _MediaApiPage(_Page):
    async def goto(self, url, **kwargs):
        await super().goto(url, **kwargs)
        for handler in self._handlers:
            handler(_MediaApiResponse())


def _item():
    return SelectedLesson(("Course", "Module"), Lesson("Lesson", "https://school/lesson/1"))


class _DelayedPlayerPage(_Page):
    def __init__(self) -> None:
        super().__init__(player=False)
        self._player_checks = 0

    async def query_selector(self, _):
        self._player_checks += 1
        return object() if self._player_checks > 1 else None


def test_no_player_and_no_hls_is_no_video(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    gateway = PlaywrightDownloadGateway(None, _Hls())  # type: ignore[arg-type]
    result = asyncio.run(
        gateway._download_lesson(
            _Browser(_Page(player=False)),
            _item(),
            tmp_path / "Lesson",
            "auto",
            lambda _: None,
        )
    )
    assert result.status.value == "no_video"


def test_no_video_event_explains_that_supported_player_is_missing(monkeypatch, tmp_path):
    class PlaywrightContext:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(downloader_module, "async_playwright", PlaywrightContext)
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    gateway = PlaywrightDownloadGateway(None, _Hls())  # type: ignore[arg-type]

    async def launch(*_):
        return _Browser(_Page(player=False))

    monkeypatch.setattr(gateway, "_launch_authenticated_context", launch)
    events = []
    request = DownloadRequest(
        lessons=(_item(),),
        quality=VideoQuality.AUTO,
        save_path=tmp_path,
    )

    summary = asyncio.run(gateway._run_async(request, events.append))

    assert summary.no_video == 1
    no_video = next(event for event in events if event.type is DownloadEventType.LESSON_NO_VIDEO)
    assert no_video.stage == "player"
    assert no_video.error_code == "VIDEO_NOT_FOUND"
    assert "поддерживаемый видеоплеер" in no_video.message


def test_player_without_hls_is_technical_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    gateway = PlaywrightDownloadGateway(None, _Hls())  # type: ignore[arg-type]
    events = []
    result = asyncio.run(
        gateway._download_lesson(
            _Browser(_Page(player=True)),
            _item(),
            tmp_path / "Lesson",
            "auto",
            events.append,
        )
    )
    assert result.status.value == "failed"
    assert events[-1].type is DownloadEventType.ERROR
    assert events[-1].lesson_url == "https://school/lesson/1"


def test_player_with_dash_manifest_reports_sanitized_unsupported_stream(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    gateway = PlaywrightDownloadGateway(None, _Hls())  # type: ignore[arg-type]
    events = []

    result = asyncio.run(
        gateway._download_lesson(
            _Browser(_DashPage(player=True)),
            _item(),
            tmp_path / "Lesson",
            "auto",
            events.append,
        )
    )

    assert result.status.value == "failed"
    assert events[-1].error_code == "DASH_STREAM_UNSUPPORTED"
    assert events[-1].source_host == "cdn.example"
    assert "DASH" in events[-1].message
    assert "secret" not in events[-1].message


def test_player_with_media_api_reports_sanitized_discovery_trace(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    gateway = PlaywrightDownloadGateway(None, _Hls())  # type: ignore[arg-type]
    events = []

    result = asyncio.run(
        gateway._download_lesson(
            _Browser(_MediaApiPage(player=True)),
            _item(),
            tmp_path / "Lesson",
            "auto",
            events.append,
        )
    )

    assert result.status.value == "failed"
    assert events[-1].error_code == "PLAYLIST_NOT_OBSERVED"
    assert events[-1].source_host == "player.example"
    assert "Media API" in events[-1].message
    assert "HTTP 200" in events[-1].message
    assert "secret" not in events[-1].message


def test_getcourse_media_playlist_response_is_downloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    hls = _SuccessfulHls()
    page = _Page(
        player=True,
        playlist=(
            "https://school.example/api/playlist/media/342118993/360?token=secret",
            "#EXTM3U\n#EXTINF:5,\nsegment.ts\n",
        ),
    )

    result = asyncio.run(
        PlaywrightDownloadGateway(None, hls)._download_lesson(
            _Browser(page),
            _item(),
            tmp_path / "Lesson",
            "360",
            lambda _: None,
        )
    )

    assert result.status.value == "downloaded"
    assert hls.calls == 1


def test_player_inserted_during_settle_is_not_reported_as_no_video(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    gateway = PlaywrightDownloadGateway(None, _Hls())  # type: ignore[arg-type]
    events = []

    result = asyncio.run(
        gateway._download_lesson(
            _Browser(_DelayedPlayerPage()),
            _item(),
            tmp_path / "Lesson",
            "auto",
            events.append,
        )
    )

    assert result.status.value == "failed"
    assert events[-1].type is DownloadEventType.ERROR


def test_lesson_finishes_without_post_download_five_second_idle(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(downloader_module, "PLAYLIST_QUIET_SECONDS", 0.0)
    hls = _SuccessfulHls()
    gateway = PlaywrightDownloadGateway(None, hls)  # type: ignore[arg-type]
    page = _Page(
        player=True,
        playlist=(
            "https://cdn.example/video.m3u8",
            "#EXTM3U\n#EXTINF:5,\nsegment.ts\n",
        ),
    )

    async def download():
        return await asyncio.wait_for(
            gateway._download_lesson(
                _Browser(page),
                _item(),
                tmp_path / "Lesson",
                "auto",
                lambda _: None,
            ),
            timeout=0.75,
        )

    result = asyncio.run(download())

    assert result.status.value == "downloaded"
    assert hls.calls == 1


def test_kinescope_signed_master_is_found_passively_and_best_quality_is_downloaded(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    hls = _RecordingHls()
    gateway = PlaywrightDownloadGateway(None, hls)  # type: ignore[arg-type]

    result = asyncio.run(
        gateway._download_lesson(
            _Browser(_KinescopePage()),
            _item(),
            tmp_path / "Lesson",
            "auto",
            lambda _: None,
        )
    )

    assert result.status.value == "downloaded"
    assert len(hls.calls) == 1
    playlist_url, kwargs = hls.calls[0]
    assert playlist_url == "https://kinescope.io/video-id/1080.m3u8?expires=123&sign=high"
    assert kwargs["referer_url"] == "https://kinescope.io/embed/public-id"


def test_identity_sample_aes_master_passes_to_hls_downloader(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    hls = _RecordingHls()
    gateway = PlaywrightDownloadGateway(None, hls)  # type: ignore[arg-type]
    events = []
    page = _Page(
        player=True,
        playlist=(
            "https://cdn.example/master.m3u8?sign=abc",
            "#EXTM3U\n"
            '#EXT-X-SESSION-KEY:METHOD=SAMPLE-AES,URI="https://license.example/key"\n'
            "#EXT-X-STREAM-INF:RESOLUTION=1920x1080\n"
            "1080.m3u8?sign=abc\n",
        ),
    )

    result = asyncio.run(
        gateway._download_lesson(
            _Browser(page),
            _item(),
            tmp_path / "Lesson",
            "auto",
            events.append,
        )
    )

    assert result.status.value == "downloaded"
    assert hls.calls[0][0] == "https://cdn.example/1080.m3u8?sign=abc"
    assert hls.calls[0][1]["session_key"].uri == "https://license.example/key"
    assert not any(event.error_code == "ENCRYPTED_PLAYLIST_UNSUPPORTED" for event in events)


def test_kinescope_master_available_after_initial_page_read_is_downloaded(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    hls = _RecordingHls()
    gateway = PlaywrightDownloadGateway(None, hls)  # type: ignore[arg-type]

    result = asyncio.run(
        gateway._download_lesson(
            _Browser(_LateKinescopePage()),
            _item(),
            tmp_path / "Lesson",
            "auto",
            lambda _: None,
        )
    )

    assert result.status.value == "downloaded"
    assert [call[0] for call in hls.calls] == [
        "https://kinescope.io/video-id/1080.m3u8?expires=123&sign=high"
    ]


def test_master_and_captured_variant_are_deduplicated_by_canonical_url():
    master = _Playlist(
        "https://cdn.example/master.m3u8?token=one",
        "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1920x1080\nvideo.m3u8?token=one\n",
    )
    variant = _Playlist(
        "https://cdn.example/video.m3u8?token=two",
        "#EXTM3U\n#EXTINF:5,\nsegment.ts\n",
    )

    selected = PlaywrightDownloadGateway._select_playlist_urls((master, variant), "auto")

    assert len(selected) == 1
    assert selected[0] == "https://cdn.example/video.m3u8?token=one"


def test_master_suppresses_all_captured_lower_quality_variants():
    master = _Playlist(
        "https://cdn.example/master.m3u8?token=master",
        (
            "#EXTM3U\n"
            "#EXT-X-STREAM-INF:RESOLUTION=1280x720\n"
            "720/video.m3u8?token=low\n"
            "#EXT-X-STREAM-INF:RESOLUTION=1920x1080\n"
            "1080/video.m3u8?token=high\n"
        ),
    )
    low = _Playlist(
        "https://cdn.example/720/video.m3u8?token=captured-low",
        "#EXTM3U\n#EXTINF:5,\nsegment.ts\n",
    )
    high = _Playlist(
        "https://cdn.example/1080/video.m3u8?token=captured-high",
        "#EXTM3U\n#EXTINF:5,\nsegment.ts\n",
    )

    selected = PlaywrightDownloadGateway._select_playlist_urls((low, master, high), "auto")

    assert selected == ["https://cdn.example/1080/video.m3u8?token=high"]


def test_multiple_video_order_is_stable_despite_response_order():
    first = _Playlist("https://cdn.example/z.m3u8", "#EXTM3U\n#EXTINF:5,\nz.ts\n")
    second = _Playlist("https://cdn.example/a.m3u8", "#EXTM3U\n#EXTINF:5,\na.ts\n")

    assert PlaywrightDownloadGateway._select_playlist_urls((first, second), "auto") == [
        "https://cdn.example/a.m3u8",
        "https://cdn.example/z.m3u8",
    ]
    assert PlaywrightDownloadGateway._select_playlist_urls((second, first), "auto") == [
        "https://cdn.example/a.m3u8",
        "https://cdn.example/z.m3u8",
    ]


def test_query_identified_videos_are_not_collapsed_into_one_download():
    master = _Playlist(
        "https://cdn.example/master.m3u8?video=one",
        "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\n"
        "play.m3u8?video=one&quality=720&token=secret\n",
    )
    other = _Playlist(
        "https://cdn.example/play.m3u8?video=two&quality=720&token=other",
        "#EXTM3U\n#EXTINF:5,\nsegment.ts\n",
    )

    selected = PlaywrightDownloadGateway._select_playlist_urls((master, other), "auto")

    assert set(selected) == {
        "https://cdn.example/play.m3u8?video=one&quality=720&token=secret",
        "https://cdn.example/play.m3u8?video=two&quality=720&token=other",
    }


def test_bandwidth_only_master_selects_best_and_suppresses_captured_variants():
    master = _Playlist(
        "https://cdn.example/master.m3u8",
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\nlow.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=5000000\nhigh.m3u8\n",
    )
    captured = _Playlist(
        "https://cdn.example/low.m3u8",
        "#EXTM3U\n#EXTINF:5,\nsegment.ts\n",
    )

    assert PlaywrightDownloadGateway._select_playlist_urls((captured, master), "auto") == [
        "https://cdn.example/high.m3u8"
    ]


def test_output_stems_preserve_hierarchy_and_hash_only_real_collisions(tmp_path):
    lessons = (
        SelectedLesson(("Course", "Module"), Lesson("A:B", "https://school/lesson/1")),
        SelectedLesson(("Course", "Module"), Lesson("A?B", "https://school/lesson/2")),
        SelectedLesson(("Course", "Other"), Lesson("A:B", "https://school/lesson/3")),
    )
    request = DownloadRequest(lessons, VideoQuality.AUTO, tmp_path)
    stems = PlaywrightDownloadGateway._output_stems(request)
    assert stems[0].parent == tmp_path / "Course" / "Module"
    assert stems[1].parent == tmp_path / "Course" / "Module"
    assert stems[0].name.startswith("A_B~")
    assert stems[1].name.startswith("A_B~")
    assert stems[0] != stems[1]
    assert stems[2].parent == tmp_path / "Course" / "Other"
    assert stems[2].name == "A_B"


def test_unique_output_stem_keeps_readable_lesson_title(tmp_path):
    lesson = SelectedLesson(
        ("Course", "Module"),
        Lesson("Лекция №1 Жена Мечты", "https://school/lesson/1"),
    )

    stem = PlaywrightDownloadGateway._output_stems(
        DownloadRequest((lesson,), VideoQuality.AUTO, tmp_path)
    )[0]

    assert stem.name == "Лекция №1 Жена Мечты"
    assert "~" not in stem.name


def test_output_stems_disambiguate_sanitized_folder_collisions_stably(tmp_path):
    lessons = (
        SelectedLesson(("A:B",), Lesson("First", "https://school/lesson/1")),
        SelectedLesson(("A?B",), Lesson("Second", "https://school/lesson/2")),
    )
    request = DownloadRequest(lessons, VideoQuality.AUTO, tmp_path)

    stems = PlaywrightDownloadGateway._output_stems(request)
    reversed_stems = PlaywrightDownloadGateway._output_stems(
        DownloadRequest(tuple(reversed(lessons)), VideoQuality.AUTO, tmp_path)
    )

    by_url = {item.lesson.url: stem for item, stem in zip(lessons, stems, strict=True)}
    reversed_by_url = {
        item.lesson.url: stem for item, stem in zip(reversed(lessons), reversed_stems, strict=True)
    }
    assert by_url == reversed_by_url
    assert stems[0].parent != stems[1].parent
    assert stems[0].parent.name.startswith("A_B~")
    assert stems[1].parent.name.startswith("A_B~")


def test_existing_output_is_detected_before_opening_lesson(tmp_path):
    stem = tmp_path / "Course" / "Lesson"
    stem.parent.mkdir()
    (stem.parent / "Lesson.mp4").write_bytes(b"done")
    assert PlaywrightDownloadGateway._output_exists(stem)

    (stem.parent / "Lesson.mp4").write_bytes(b"")
    assert not PlaywrightDownloadGateway._output_exists(stem)

    (stem.parent / "Lesson_720.mp4").write_bytes(b"done")
    assert PlaywrightDownloadGateway._output_exists(stem)
    (stem.parent / "Lesson_720.mp4").unlink()

    stem.mkdir()
    (stem / "video_1.mp4").write_bytes(b"one")
    (stem / "video_2.mp4").write_bytes(b"two")
    assert not PlaywrightDownloadGateway._output_exists(stem)


def test_path_too_long_is_rejected(tmp_path):
    from getcourse_downloader.infrastructure.storage.filenames import safe_lesson_output_stem

    with pytest.raises(ValueError, match="слишком длинный"):
        safe_lesson_output_stem(Path("C:/") / ("x" * 190), ("Course",), "Lesson")


def test_output_paths_disambiguate_only_when_selected_paths_actually_alias(tmp_path):
    first = SelectedLesson(("A:B",), Lesson("Same title", "https://school/lesson/1"))
    second = SelectedLesson(("A?B",), Lesson("Same title", "https://school/lesson/2"))
    together = PlaywrightDownloadGateway._output_stems(
        DownloadRequest((first, second), VideoQuality.AUTO, tmp_path)
    )
    individually = [
        PlaywrightDownloadGateway._output_stems(
            DownloadRequest((item,), VideoQuality.AUTO, tmp_path)
        )[0]
        for item in (first, second)
    ]

    assert together != individually
    assert together[0].parent != together[1].parent
    assert individually[0] == individually[1]


def test_same_title_lessons_need_catalog_to_detect_separate_run_collision(tmp_path):
    outputs = [
        PlaywrightDownloadGateway._output_stems(
            DownloadRequest(
                (SelectedLesson(("Course",), Lesson("Lesson", f"https://school/lesson/{number}")),),
                VideoQuality.AUTO,
                tmp_path,
            )
        )[0]
        for number in (1, 2)
    ]

    assert outputs[0] == outputs[1] == tmp_path / "Course" / "Lesson"


def test_lesson_navigation_failure_is_reported_and_next_lesson_still_runs(monkeypatch, tmp_path):
    class PlaywrightContext:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_):
            return None

    items = (
        SelectedLesson(("Course",), Lesson("First", "https://school/lesson/1")),
        SelectedLesson(("Course",), Lesson("Second", "https://school/lesson/2")),
    )
    gateway = PlaywrightDownloadGateway(None, _Hls())

    async def launch(*_):
        return _Browser(_Page(player=False))

    async def download_lesson(_browser, item, *_):
        if item == items[0]:
            raise RuntimeError("Failed to open https://school/lesson/1?token=secret")
        return downloader_module._LessonResult(downloader_module._LessonStatus.NO_VIDEO)

    monkeypatch.setattr(downloader_module, "async_playwright", PlaywrightContext)
    monkeypatch.setattr(gateway, "_launch_authenticated_context", launch)
    monkeypatch.setattr(gateway, "_download_lesson", download_lesson)
    events = []

    summary = asyncio.run(
        gateway._run_async(DownloadRequest(items, VideoQuality.AUTO, tmp_path), events.append)
    )

    assert summary.failed == ("First",)
    assert summary.no_video == 1
    assert summary.processed == 2
    assert events[-1].type is DownloadEventType.SUMMARY
    assert "secret" not in "\n".join(event.message for event in events)


def test_download_gateway_preserves_cancel_received_before_run(monkeypatch, tmp_path):
    gateway = PlaywrightDownloadGateway(None, _Hls())

    async def run_async(*_):
        return gateway._cancelled.is_set()

    monkeypatch.setattr(gateway, "_run_async", run_async)
    gateway.cancel()

    assert gateway.run(DownloadRequest((), VideoQuality.AUTO, tmp_path), lambda _: None) is True


@pytest.mark.parametrize(
    "url",
    ["https://login-school.getcourse.ru/lesson/1", "https://school/lesson/1?next=/login"],
)
def test_lesson_url_with_login_outside_path_is_not_authentication(url):
    page = _Page(player=False)
    page.url = url

    assert asyncio.run(PlaywrightDownloadGateway._authentication_required(page)) is False
