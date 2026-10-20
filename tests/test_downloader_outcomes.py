import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from getcourse_downloader.domain.events import DownloadEventType
from getcourse_downloader.domain.models import DownloadRequest, Lesson, SelectedLesson, VideoQuality
from getcourse_downloader.infrastructure.getcourse import downloader as downloader_module
from getcourse_downloader.infrastructure.getcourse import player_config as player_config_module
from getcourse_downloader.infrastructure.getcourse.downloader import (
    PlaywrightDownloadGateway,
    _Playlist,
)
from getcourse_downloader.infrastructure.media.hls import (
    HlsDownloader,
    HlsDownloadResult,
    HlsDownloadStatus,
    parse_master_variants,
)
from getcourse_downloader.infrastructure.storage.download_catalog import (
    DownloadedMedia,
    JsonDownloadCatalog,
)


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

    async def query_selector_all(self, _):
        return []

    async def wait_for_load_state(self, *_, **__):
        return None

    async def wait_for_timeout(self, *_):
        return None

    async def close(self):
        return None

    def get_by_role(self, *_, **__):
        return _Visibility(False)

    def get_by_text(self, *_, **__):
        return _Visibility(False)


class _Visibility:
    def __init__(self, visible, text=""):
        self.visible = visible
        self.text = text

    async def is_visible(self):
        return self.visible

    def locator(self, _):
        return self

    async def inner_text(self):
        return self.text


class _Browser:
    def __init__(self, page):
        self.page = page

    async def new_page(self):
        return self.page

    async def close(self):
        return None


class _Hls:
    async def probe_quality(self, _media, fallback=""):
        return fallback

    async def find_existing_output(self, stem, height):
        return await HlsDownloader.find_existing_output(self, stem, height)

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


class _AudioRecordingHls(_RecordingHls):
    def __init__(self) -> None:
        super().__init__()
        self.merges = []

    async def merge_audio_video(self, video, audio, destination, **_kwargs):
        self.merges.append((video, audio, destination))
        destination.write_bytes(b"merged")
        return True, ""


class _KinescopeFrame:
    url = "https://kinescope.io/embed/public-id"

    async def content(self):
        return (
            r'<script>src="https:\/\/kinescope.io\/video-id\/master.m3u8'
            r'?expires=123\u0026sign=master"</script>'
        )

    async def evaluate(self, _script, request):
        playlist_url = request["url"]
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


def test_embedded_playlist_request_has_a_deadline(monkeypatch):
    monkeypatch.setattr(downloader_module, "EMBEDDED_PLAYLIST_REQUEST_SECONDS", 0.01)

    class UnresponsiveFrame(_KinescopeFrame):
        async def evaluate(self, *_):
            await asyncio.Event().wait()

    page = _KinescopePage()
    page.frames = [UnresponsiveFrame()]

    async def scenario():
        return await asyncio.wait_for(
            PlaywrightDownloadGateway._read_embedded_playlists(page), timeout=1.5
        )

    assert asyncio.run(scenario()) == []


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


def test_closed_lesson_reports_access_denied_instead_of_missing_video(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)

    class ClosedPage(_Page):
        def get_by_role(self, role, **kwargs):
            return _Visibility(
                role == "heading" and kwargs.get("name") == "Нет доступа",
                "Нет доступа\nУ вас нет доступа к этому уроку",
            )

        def get_by_text(self, text, **_):
            return _Visibility(text == "У вас нет доступа к этому уроку")

    events = []
    result = asyncio.run(
        PlaywrightDownloadGateway(None, _Hls())._download_lesson(
            _Browser(ClosedPage(player=False)), _item(), tmp_path / "Lesson", "auto", events.append
        )
    )

    assert result.status.value == "failed"
    assert events[-1].error_code == "LESSON_ACCESS_DENIED"
    assert events[-1].stage == "access"


class _PlayerFrame:
    def __init__(self, number):
        self.url = f"https://video.example/sign-player/?video=test-{number}"

    async def content(self):
        return ""


class _DelayedPlaylistPage(_Page):
    def __init__(self, *, missing=False, unloaded=False):
        super().__init__(player=True)
        self.frames = [_PlayerFrame(number) for number in range(1, 4)]
        if unloaded:
            self.frames[2].url = "about:blank"
        self.missing = missing
        self.delayed = None

    def emit_playlist(self, index):
        self.frames[index - 1].url = f"https://video.example/sign-player/?video=test-{index}"
        response = SimpleNamespace(
            url=f"https://cdn.example/video-{index}/master.m3u8",
            request=SimpleNamespace(frame=self.frames[index - 1]),
        )

        async def text():
            return "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=640x360\nvideo.m3u8\n"

        response.text = text
        for handler in self._handlers:
            handler(response)

    async def query_selector_all(self, _):
        class Element:
            def __init__(self, frame):
                self.frame = frame

            async def content_frame(self):
                return self.frame

            async def dispose(self):
                pass

        return [Element(frame) for frame in self.frames]

    async def goto(self, url, **_):
        self.url = url
        self.emit_playlist(1)
        self.emit_playlist(2)

        async def delayed_response():
            await asyncio.sleep(0.4)
            self.emit_playlist(3)

        if not self.missing:
            self.delayed = asyncio.create_task(delayed_response())

    async def close(self):
        if self.delayed:
            self.delayed.cancel()
            await asyncio.gather(self.delayed, return_exceptions=True)


class _ConfiguredPlayerFrame(_PlayerFrame):
    def __init__(self, number, *, ready=False, protected=True):
        super().__init__(number)
        self.ready = ready
        self.created = 0
        self.requests = []
        self.config = {
            "isVideoReady": True,
            "isProtectReady": ready and (number == 1 or not protected),
            "isVideoProtect": protected,
            "masterPlaylistUrl": f"https://video.example/api/playlist/master/video-{number}/view",
            "masterPlaylistDomains": [],
            "createPersonalVideoUrl": f"https://video.example/player/video-{number}/view/create-personal-video",
            "checkPersonalVideoUrl": f"https://video.example/player/video-{number}/view/ready",
        }

    async def evaluate(self, _script, request=None):
        if request is None:
            return self.config
        url, method = request["url"], request["method"]
        self.requests.append((method, url))
        if url.endswith("/create-personal-video"):
            self.created += 1
            self.ready = True
            return {"status": 200, "url": url, "text": ""}
        if url.endswith("/ready"):
            return {"status": 200, "url": url, "text": json.dumps({"ready": self.ready})}
        return {
            "status": 200,
            "url": url,
            "text": (
                "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=640x360\n360.m3u8\n"
                "#EXT-X-STREAM-INF:RESOLUTION=1280x720\n720.m3u8\n"
            ),
        }


@pytest.mark.parametrize("phase", ["navigation", "embedded", "personal", "cleanup"])
def test_video_check_deadline_fails_lesson_and_releases_pending_work(monkeypatch, tmp_path, phase):
    monkeypatch.setattr(downloader_module, "VIDEO_CHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(downloader_module, "PAGE_CLEANUP_TIMEOUT_SECONDS", 0.05)
    entered = []
    cancelled = []
    reported_before_cleanup = []

    async def hang():
        entered.append(phase)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(phase)

    class HangingFrame(_ConfiguredPlayerFrame):
        async def content(self):
            if phase == "embedded":
                await hang()
            return ""

        async def evaluate(self, script, request=None):
            if phase == "personal" and request:
                await hang()
            return await super().evaluate(script, request)

    class HangingPage(_Page):
        closed = False

        async def goto(self, url, **kwargs):
            if phase in {"navigation", "cleanup"}:
                await hang()
            await super().goto(url, **kwargs)

        async def close(self):
            try:
                if phase == "cleanup":
                    reported_before_cleanup.append(
                        any(event.error_code == "VIDEO_CHECK_TIMEOUT" for event in events)
                    )
                    await asyncio.Event().wait()
            finally:
                self.closed = True

    page = HangingPage(player=True)
    page.frames = [HangingFrame(1)] if phase != "navigation" else []
    gateway = PlaywrightDownloadGateway(None, _Hls())
    events = []

    async def scenario():
        result = await asyncio.wait_for(
            gateway._download_lesson(
                _Browser(page), _item(), tmp_path / "Lesson", "auto", events.append
            ),
            timeout=1.0,
        )
        monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
        following = await gateway._download_lesson(
            _Browser(_Page(player=False)), _item(), tmp_path / "Next", "auto", events.append
        )
        return result, following

    result, following = asyncio.run(scenario())

    assert result.status.value == "failed"
    assert following.status.value == "no_video"
    assert entered == cancelled == [phase]
    assert page.closed
    errors = [event for event in events if event.type is DownloadEventType.ERROR]
    assert len(errors) == 1
    assert errors[0].error_code == "VIDEO_CHECK_TIMEOUT"
    assert errors[0].lesson_url == _item().lesson.url
    assert errors[0].stage == "playlist"
    if phase == "cleanup":
        assert reported_before_cleanup == [True]


def test_video_check_deadline_does_not_limit_media_download(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "VIDEO_CHECK_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)

    class SlowHls(_SuccessfulHls):
        async def download(self, *args, **kwargs):
            await asyncio.sleep(0.3)
            return await super().download(*args, **kwargs)

    hls = SlowHls()
    page = _Page(
        player=True,
        playlist=("https://cdn.example/video.m3u8", "#EXTM3U\n#EXTINF:5,\nsegment.ts\n"),
    )
    events = []
    result = asyncio.run(
        PlaywrightDownloadGateway(None, hls)._download_lesson(
            _Browser(page), _item(), tmp_path / "Lesson", "auto", events.append
        )
    )

    assert result.status.value == "downloaded"
    assert hls.calls == 1
    assert not any(event.type is DownloadEventType.ERROR for event in events)


def test_configured_players_prepare_once_and_use_existing_master_quality_selection(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(downloader_module, "PLAYLIST_QUIET_SECONDS", 0.0)
    page = _DelayedPlaylistPage(missing=True)
    page.frames = [
        _ConfiguredPlayerFrame(1, ready=True),
        _ConfiguredPlayerFrame(2, ready=True),  # Initial config is stale after prior preparation.
        _ConfiguredPlayerFrame(3),
        _ConfiguredPlayerFrame(4, ready=True, protected=False),
    ]

    async def goto(url, **_):
        page.url = url
        # A browser may have already requested another CDN for the same player.
        response = SimpleNamespace(
            url="https://alternate.example/api/playlist/master/video-1/view",
            request=SimpleNamespace(frame=page.frames[0]),
        )

        async def text():
            return "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=1280x720\n720.m3u8\n"

        response.text = text
        for handler in page._handlers:
            handler(response)

    page.goto = goto
    hls = _RecordingHls()
    events = []
    result = asyncio.run(
        PlaywrightDownloadGateway(None, hls)._download_lesson(
            _Browser(page), _item(), tmp_path / "Lesson", "720", events.append
        )
    )

    assert result.status.value == "downloaded"
    assert len(result.media) == len(hls.calls) == 4
    assert [frame.created for frame in page.frames] == [0, 0, 1, 0]
    assert [len(frame.requests) for frame in page.frames] == [1, 2, 4, 1]
    assert all(url.endswith("/720.m3u8") for url, _ in hls.calls)
    assert all(kwargs["selected_height"] == 720 for _, kwargs in hls.calls)
    assert {kwargs["referer_url"] for _, kwargs in hls.calls} == {
        frame.url for frame in page.frames
    }
    assert not any(event.error_code for event in events)


@pytest.mark.parametrize(
    "failure,code",
    [
        ("http", "PLAYER_ACCESS_DENIED"),
        ("json", "PLAYER_INVALID_RESPONSE"),
        ("timeout", "PLAYER_PREPARATION_TIMEOUT"),
        ("cancel", ""),
    ],
)
def test_personal_video_failure_or_cancel_does_not_download_partial_lesson(
    monkeypatch, tmp_path, failure, code
):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(player_config_module, "PLAYER_PREPARATION_SECONDS", 0.05)
    monkeypatch.setattr(player_config_module, "PLAYER_POLL_SECONDS", 0.01)
    gateway = PlaywrightDownloadGateway(None, _Hls())

    class FailingFrame(_ConfiguredPlayerFrame):
        async def evaluate(self, script, request=None):
            if request and request["url"].endswith("/ready"):
                if failure == "cancel":
                    gateway.cancel()
                return {
                    "status": 403 if failure == "http" else 200,
                    "url": request["url"],
                    "text": "private-response" if failure == "json" else '{"ready":false}',
                }
            return await super().evaluate(script, request)

    page = _DelayedPlaylistPage(missing=True)
    page.frames = [FailingFrame(3)]

    async def goto(url, **_):
        page.url = url

    page.goto = goto
    events = []
    result = asyncio.run(
        gateway._download_lesson(
            _Browser(page), _item(), tmp_path / "Lesson", "auto", events.append
        )
    )
    assert result.status.value == ("cancelled" if failure == "cancel" else "failed")
    assert all("private-response" not in event.message for event in events)
    if code:
        assert events[-1].error_code == code


def test_replaced_player_does_not_keep_failure_from_detached_frame(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 1.0)
    monkeypatch.setattr(downloader_module, "PLAYLIST_QUIET_SECONDS", 0.0)
    page = _DelayedPlaylistPage(missing=True)

    class ReplacedFrame(_ConfiguredPlayerFrame):
        async def evaluate(self, script, request=None):
            if request and request["url"].endswith("/ready"):
                page.frames = [_ConfiguredPlayerFrame(1, ready=True)]
                return {"status": 403}
            return await super().evaluate(script, request)

    page.frames = [ReplacedFrame(1)]

    async def goto(url, **_):
        page.url = url

    page.goto = goto
    hls = _RecordingHls()
    events = []
    result = asyncio.run(
        PlaywrightDownloadGateway(None, hls)._download_lesson(
            _Browser(page), _item(), tmp_path / "Lesson", "auto", events.append
        )
    )
    assert result.status.value == "downloaded"
    assert len(hls.calls) == 1
    assert not any(event.error_code for event in events)


@pytest.mark.parametrize("unloaded", [False, True])
def test_all_player_streams_are_waited_for_after_network_quiet(monkeypatch, tmp_path, unloaded):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 2.0)
    monkeypatch.setattr(downloader_module, "PLAYLIST_QUIET_SECONDS", 0.01)
    hls = _SuccessfulHls()
    result = asyncio.run(
        PlaywrightDownloadGateway(None, hls)._download_lesson(
            _Browser(_DelayedPlaylistPage(unloaded=unloaded)),
            _item(),
            tmp_path / "Lesson",
            "auto",
            lambda _: None,
        )
    )

    assert result.status.value == "downloaded"
    assert len(result.media) == 3
    assert hls.calls == 3


@pytest.mark.parametrize("unloaded", [False, True])
def test_missing_third_player_stream_is_not_reported_as_complete_lesson(
    monkeypatch, tmp_path, unloaded
):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(downloader_module, "PLAYLIST_QUIET_SECONDS", 0.01)
    events = []
    hls = _SuccessfulHls()
    result = asyncio.run(
        PlaywrightDownloadGateway(None, hls)._download_lesson(
            _Browser(_DelayedPlaylistPage(missing=True, unloaded=unloaded)),
            _item(),
            tmp_path / "Lesson",
            "auto",
            events.append,
        )
    )

    assert result.status.value == "failed"
    assert hls.calls == 0
    assert events[-1].error_code == "PLAYER_STREAMS_INCOMPLETE"


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
            "https://school.example/api/playlist/media/1001/360?token=secret",
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


def test_master_audio_playlist_is_downloaded_and_merged_with_selected_video(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    hls = _AudioRecordingHls()
    gateway = PlaywrightDownloadGateway(None, hls)  # type: ignore[arg-type]
    page = _Page(
        player=True,
        playlist=(
            "https://cdn.example/master.m3u8?token=master",
            "#EXTM3U\n"
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="voice",URI="audio.m3u8?token=audio"\n'
            '#EXT-X-STREAM-INF:RESOLUTION=640x360,AUDIO="voice"\n'
            "video.m3u8?token=video\n",
        ),
    )

    result = asyncio.run(
        gateway._download_lesson(
            _Browser(page),
            _item(),
            tmp_path / "Lesson",
            "360",
            lambda _: None,
        )
    )

    assert result.status.value == "downloaded"
    assert [call[0] for call in hls.calls] == [
        "https://cdn.example/video.m3u8?token=video",
        "https://cdn.example/audio.m3u8?token=audio",
    ]
    assert hls.merges == [
        (
            tmp_path / "Lesson.mp4",
            tmp_path / "Lesson.audio.mp4",
            tmp_path / "Lesson.merged.mp4",
        )
    ]


def test_audio_failure_preserves_video_as_partial_not_completed_output(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)

    class AudioFailureHls:
        def __init__(self):
            self.calls = []

        async def download(self, playlist_url, output_stem, *_args, **_kwargs):
            self.calls.append(playlist_url)
            if "audio.m3u8" in playlist_url:
                return HlsDownloadResult(HlsDownloadStatus.FAILED)
            video = Path(f"{output_stem}_360.mp4")
            video.write_bytes(b"video without audio")
            return HlsDownloadResult(HlsDownloadStatus.DOWNLOADED, output_path=video)

        async def probe_quality(self, _media):
            return "360p"

    hls = AudioFailureHls()
    gateway = PlaywrightDownloadGateway(None, hls)  # type: ignore[arg-type]
    page = _Page(
        player=True,
        playlist=(
            "https://cdn.example/master.m3u8",
            "#EXTM3U\n"
            '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="voice",URI="audio.m3u8"\n'
            '#EXT-X-STREAM-INF:RESOLUTION=640x360,AUDIO="voice"\n'
            "video.m3u8\n",
        ),
    )

    result = asyncio.run(
        gateway._download_lesson(
            _Browser(page), _item(), tmp_path / "Lesson", "360", lambda _: None
        )
    )

    assert result.status.value == "failed"
    assert (tmp_path / "Lesson_360.video-only.mp4").read_bytes() == b"video without audio"
    assert not (tmp_path / "Lesson_360.mp4").exists()
    assert not PlaywrightDownloadGateway._output_exists(tmp_path / "Lesson")


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


def test_master_audio_rendition_is_not_selected_as_an_independent_video():
    master = _Playlist(
        "https://cdn.example/master.m3u8",
        "#EXTM3U\n"
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="voice",URI="audio.m3u8?token=master"\n'
        '#EXT-X-STREAM-INF:RESOLUTION=640x360,AUDIO="voice"\nvideo.m3u8\n',
    )
    audio = _Playlist(
        "https://cdn.example/audio.m3u8?token=captured",
        "#EXTM3U\n#EXTINF:1,\na.m4s\n",
    )
    video = _Playlist("https://cdn.example/video.m3u8", "#EXTM3U\n#EXTINF:1,\nv.m4s\n")
    unrelated = _Playlist("https://cdn.example/other.m3u8", "#EXTM3U\n#EXTINF:1,\nother.ts\n")

    selected = PlaywrightDownloadGateway._select_playlists(
        (audio, master, video, unrelated), "auto"
    )
    assert [playlist.url for playlist in selected] == [
        "https://cdn.example/other.m3u8",
        "https://cdn.example/video.m3u8",
    ]
    assert selected[1].audio_url == "https://cdn.example/audio.m3u8?token=master"


def _three_videos_with_renewed_signatures() -> list[_Playlist]:
    playlists = []
    for video_id in ("first", "second", "third"):
        for signature in ("initial", "renewed"):
            prefix = f"https://cdn.example/media.m3u8?r={video_id}&pathsig={signature}"
            playlists.append(
                _Playlist(
                    f"https://cdn.example/playlist.m3u8?r={video_id}&pathsig={signature}",
                    "#EXTM3U\n"
                    f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="voice",URI="{prefix}&st=audio"\n'
                    '#EXT-X-STREAM-INF:RESOLUTION=640x360,AUDIO="voice"\n'
                    f"{prefix}&st=video&quality=360\n"
                    '#EXT-X-STREAM-INF:RESOLUTION=1280x720,AUDIO="voice"\n'
                    f"{prefix}&st=video&quality=720\n",
                )
            )
        for track in ("st=audio", "st=video&quality=360", "st=video&quality=720"):
            playlists.append(
                _Playlist(
                    f"https://cdn.example/media.m3u8?r={video_id}&pathsig=captured&{track}",
                    "#EXTM3U\n#EXTINF:1,\nsegment.m4s\n",
                )
            )
    return playlists


def _vimeo_alternate_cdns() -> list[_Playlist]:
    # Reproduces observed Vimeo structure using fixed synthetic identifiers and credentials.
    path = Path(__file__).parent / "fixtures" / "vimeo_alternate_cdns.json"
    return [_Playlist(**item) for item in json.loads(path.read_text(encoding="utf-8"))]


@pytest.mark.parametrize("quality,height", [("360", 360), ("720", 720), ("auto", 720)])
def test_vimeo_alternate_cdns_and_delivery_routes_select_three_videos(quality, height):
    selected = PlaywrightDownloadGateway._select_playlists(_vimeo_alternate_cdns(), quality)

    assert len(selected) == 3
    assert all(playlist.height == height and playlist.audio_url for playlist in selected)
    assert len({playlist.url.split("/psid=")[0].rsplit("/", 1)[-1] for playlist in selected}) == 3


def test_vimeo_captured_audio_and_other_quality_do_not_add_extra_videos():
    masters = _vimeo_alternate_cdns()
    variants = parse_master_variants(masters[0].text, masters[0].url)
    captured = [
        _Playlist(
            url.replace("vod-adaptive-ak.vimeocdn.com", "other.vimeocdn.com"),
            "#EXTM3U\n#EXTINF:1,\nsegment.m4s\n",
        )
        for variant in variants
        for url in (variant.url, variant.audio_url)
        if url
    ]

    selected = PlaywrightDownloadGateway._select_playlists([*captured, *masters], "360")

    assert len(selected) == 3
    assert all(playlist.height == 360 and playlist.audio_url for playlist in selected)


def test_vimeo_deduplication_preserves_a_fourth_distinct_video():
    masters = _vimeo_alternate_cdns()
    first = masters[0]
    asset = first.url.split("/psid=")[0].rsplit("/", 1)[-1]
    another_asset = "44444444-4444-4444-8444-444444444444"
    fourth = _Playlist(
        first.url.replace(asset, another_asset), first.text.replace(asset, another_asset)
    )

    assert len(PlaywrightDownloadGateway._select_playlists([*masters, fourth], "360")) == 4


def test_three_players_with_renewed_pathsig_select_exactly_three_videos():
    selected = PlaywrightDownloadGateway._select_playlists(
        _three_videos_with_renewed_signatures(), "360"
    )
    assert len(selected) == 3
    assert all(playlist.height == 360 and playlist.audio_url for playlist in selected)
    assert {playlist.url.split("r=", 1)[1].split("&", 1)[0] for playlist in selected} == {
        "first",
        "second",
        "third",
    }


@pytest.mark.parametrize(
    "playlists_factory", [_three_videos_with_renewed_signatures, _vimeo_alternate_cdns]
)
def test_lesson_download_does_not_repeat_three_videos_after_signature_renewal(
    monkeypatch, tmp_path, playlists_factory
):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)

    class RepeatedPlaylistPage(_Page):
        async def goto(self, url, **_):
            self.url = url

            class Response:
                def __init__(self, playlist):
                    self.url = playlist.url
                    self._text = playlist.text

                async def text(self):
                    return self._text

            for playlist in playlists_factory():
                for handler in self._handlers:
                    handler(Response(playlist))

    class FileHls(_AudioRecordingHls):
        async def download(self, playlist_url, output_stem, *_args, **kwargs):
            result = await super().download(playlist_url, output_stem, **kwargs)
            result.output_path.parent.mkdir(parents=True, exist_ok=True)
            result.output_path.write_bytes(b"local media")
            return result

    hls = FileHls()
    gateway = PlaywrightDownloadGateway(None, hls)  # type: ignore[arg-type]
    result = asyncio.run(
        gateway._download_lesson(
            _Browser(RepeatedPlaylistPage(player=True)),
            _item(),
            tmp_path / "Lesson",
            "360",
            lambda _: None,
        )
    )

    assert result.status.value == "downloaded"
    assert len(result.media) == 3
    assert len(hls.merges) == 3
    assert len(hls.calls) == 6  # One video and one companion audio per player.
    assert len(list((tmp_path / "Lesson").glob("video_*.mp4"))) == 3


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


def test_output_stems_preserve_hierarchy_and_number_only_real_collisions(tmp_path):
    lessons = (
        SelectedLesson(("Course", "Module"), Lesson("A:B", "https://school/lesson/1")),
        SelectedLesson(("Course", "Module"), Lesson("A?B", "https://school/lesson/2")),
        SelectedLesson(("Course", "Other"), Lesson("A:B", "https://school/lesson/3")),
    )
    request = DownloadRequest(lessons, VideoQuality.AUTO, tmp_path)
    stems = PlaywrightDownloadGateway._output_stems(request)
    assert stems[0].parent == tmp_path / "Course" / "Module"
    assert stems[1].parent == tmp_path / "Course" / "Module"
    assert stems[0].name == "A_B"
    assert stems[1].name == "A_B (2)"
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
    assert stems[0].parent.name == "A_B"
    assert stems[1].parent.name == "A_B (2)"


def test_truncated_folder_names_do_not_collide_with_literal_numbered_folder(tmp_path):
    prefix = "Демонстрационный модуль " * 8
    lessons = (
        SelectedLesson((prefix + "А",), Lesson("Lesson", "https://school.example/lesson/1")),
        SelectedLesson((prefix + "Б",), Lesson("Lesson", "https://school.example/lesson/2")),
        SelectedLesson(("Module:X",), Lesson("Lesson", "https://school.example/lesson/3")),
        SelectedLesson(("Module?X",), Lesson("Lesson", "https://school.example/lesson/4")),
        SelectedLesson(("Module_X (2)",), Lesson("Lesson", "https://school.example/lesson/5")),
    )
    stems = PlaywrightDownloadGateway._output_stems(
        DownloadRequest(lessons, VideoQuality.AUTO, tmp_path)
    )
    reversed_stems = PlaywrightDownloadGateway._output_stems(
        DownloadRequest(tuple(reversed(lessons)), VideoQuality.AUTO, tmp_path)
    )
    assert stems == list(reversed(reversed_stems))
    assert len({stem.parent for stem in stems}) == 5
    assert all("~" not in str(stem.relative_to(tmp_path)) for stem in stems)
    assert all(len(str(stem.resolve())) <= 200 for stem in stems)
    assert stems[1].parent.name.endswith(" (2)")
    assert {stem.parent.name for stem in stems[2:]} == {"Module_X", "Module_X (2)", "Module_X (3)"}
    for stem in stems:
        stem.parent.mkdir(parents=True, exist_ok=True)
        stem.with_name(f"{stem.name}_720.mp4").write_bytes(b"synthetic")
    assert len(list(tmp_path.rglob("*.mp4"))) == 5


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


@pytest.mark.parametrize("requested", ["480", "720", "1080", "auto"])
def test_other_quality_in_catalog_does_not_skip_requested_lesson(tmp_path, requested):
    stem = tmp_path / "Lesson"
    video = tmp_path / "Lesson_360.mp4"
    video.write_bytes(b"completed 360p")
    catalog = JsonDownloadCatalog(tmp_path / "downloads.json")
    catalog.save(_item().lesson.url, stem, (DownloadedMedia(video, "360p"),))
    gateway = PlaywrightDownloadGateway(None, _Hls(), catalog)

    assert asyncio.run(gateway._existing_result(_item(), stem, requested)) is None


def test_auto_does_not_trust_even_highest_previously_saved_quality(tmp_path):
    stem = tmp_path / "Lesson"
    video = tmp_path / "Lesson_1080.mp4"
    video.write_bytes(b"completed 1080p")
    catalog = JsonDownloadCatalog(tmp_path / "downloads.json")
    catalog.save(_item().lesson.url, stem, (DownloadedMedia(video, "1080p"),))
    gateway = PlaywrightDownloadGateway(None, _Hls(), catalog)

    assert asyncio.run(gateway._existing_result(_item(), stem, "auto")) is None


def test_same_explicit_quality_is_skipped_from_catalog(tmp_path):
    stem = tmp_path / "Lesson"
    video = tmp_path / "Lesson_480.mp4"
    video.write_bytes(b"completed 480p")
    catalog = JsonDownloadCatalog(tmp_path / "downloads.json")
    catalog.save(_item().lesson.url, stem, (DownloadedMedia(video, "480p"),))
    gateway = PlaywrightDownloadGateway(None, _Hls(), catalog)

    result = asyncio.run(gateway._existing_result(_item(), stem, "480"))

    assert result.status.value == "skipped"
    assert result.media == (DownloadedMedia(video, "480p"),)


def test_numbered_name_reuses_its_catalog_record_after_separate_downloads(monkeypatch, tmp_path):
    class PlaywrightContext:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *_):
            return None

    monkeypatch.setattr(downloader_module, "async_playwright", PlaywrightContext)
    catalog = JsonDownloadCatalog(tmp_path / "downloads.json")
    recorded_names = {2: "Lesson", 1: "Lesson (2)", 3: "Lesson (3)"}
    for number, name in recorded_names.items():
        stem = tmp_path / "Course" / name
        stem.parent.mkdir(exist_ok=True)
        video = stem.with_name(f"{name}_720.mp4")
        video.write_bytes(b"synthetic")
        catalog.save(
            f"https://school.example/lesson/{number}", stem, (DownloadedMedia(video, "720p"),)
        )
    gateway = PlaywrightDownloadGateway(None, _Hls(), catalog)
    original = gateway._existing_result
    checked = []

    async def existing(item, stem, quality):
        checked.append(stem)
        return await original(item, stem, quality)

    monkeypatch.setattr(gateway, "_existing_result", existing)
    items = {
        number: SelectedLesson(
            ("Course",), Lesson("Lesson", f"https://school.example/lesson/{number}")
        )
        for number in recorded_names
    }
    for numbers in ((3,), (3,), (1, 2), (2, 1)):
        checked.clear()
        summary = asyncio.run(
            gateway._run_async(
                DownloadRequest(tuple(items[n] for n in numbers), VideoQuality.P720, tmp_path),
                lambda _: None,
            )
        )
        assert summary.already_present == len(numbers)
        assert checked == [tmp_path / "Course" / recorded_names[number] for number in numbers]


def test_requested_version_on_disk_is_found_when_catalog_describes_another_quality(tmp_path):
    stem = tmp_path / "Lesson"
    old = tmp_path / "Lesson_360.mp4"
    wanted = tmp_path / "Lesson_480.mp4"
    old.write_bytes(b"old version")
    wanted.write_bytes(b"wanted version")
    catalog = JsonDownloadCatalog(tmp_path / "downloads.json")
    catalog.save(_item().lesson.url, stem, (DownloadedMedia(old, "360p"),))
    gateway = PlaywrightDownloadGateway(None, _Hls(), catalog)

    result = asyncio.run(gateway._existing_result(_item(), stem, "480"))

    assert result.status.value == "skipped"
    assert result.media == (DownloadedMedia(wanted, "480p"),)
    assert old.read_bytes() == b"old version"


def test_one_matching_video_does_not_mark_entire_multi_video_lesson_complete(tmp_path):
    stem = tmp_path / "Lesson"
    stem.mkdir()
    first = stem / "video_1_480.mp4"
    second = stem / "video_2_360.mp4"
    first.write_bytes(b"first 480p")
    second.write_bytes(b"second 360p")
    catalog = JsonDownloadCatalog(tmp_path / "downloads.json")
    catalog.save(
        _item().lesson.url,
        stem,
        (DownloadedMedia(first, "480p"), DownloadedMedia(second, "360p")),
    )
    gateway = PlaywrightDownloadGateway(None, _Hls(), catalog)

    assert asyncio.run(gateway._existing_result(_item(), stem, "480")) is None


def test_auto_opens_lesson_and_passes_discovered_height_for_opaque_variant_url(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(downloader_module, "PLAYLIST_WAIT_SECONDS", 0.0)
    hls = _RecordingHls()
    gateway = PlaywrightDownloadGateway(None, hls)
    page = _Page(
        player=True,
        playlist=(
            "https://cdn.example/master.m3u8",
            "#EXTM3U\n#EXT-X-STREAM-INF:RESOLUTION=640x360\nlow.m3u8\n"
            "#EXT-X-STREAM-INF:RESOLUTION=1920x1080\nhigh.m3u8\n",
        ),
    )
    (tmp_path / "Lesson_360.mp4").write_bytes(b"lower quality")

    result = asyncio.run(
        gateway._download_lesson(
            _Browser(page), _item(), tmp_path / "Lesson", "auto", lambda _: None
        )
    )

    assert page.url == _item().lesson.url
    assert result.status.value == "downloaded"
    assert hls.calls[0][0] == "https://cdn.example/high.m3u8"
    assert hls.calls[0][1]["selected_height"] == 1080


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
