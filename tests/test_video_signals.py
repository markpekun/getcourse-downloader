import subprocess
import sys

from getcourse_downloader.infrastructure.getcourse import video_signals
from getcourse_downloader.infrastructure.getcourse.video_signals import (
    VIDEO_PLAYER_SELECTOR,
    is_hls_playlist_url,
    is_master_playlist_url,
)


def test_video_selector_keeps_getcourse_and_adds_old_rutube_embed():
    assert "iframe.vhi-iframe" in VIDEO_PLAYER_SELECTOR
    assert "iframe.js--vhi-iframe" in VIDEO_PLAYER_SELECTOR
    assert "rutube.ru/play/embed/" in VIDEO_PLAYER_SELECTOR
    assert "kinescope.io/embed/" in VIDEO_PLAYER_SELECTOR
    assert "player.vimeo.com/video/" in VIDEO_PLAYER_SELECTOR
    assert "/sign-player/" in VIDEO_PLAYER_SELECTOR


def test_hls_url_detection_supports_getcourse_and_rutube_formats():
    assert is_hls_playlist_url("https://vh.example/api/playlist/master/123")
    assert is_hls_playlist_url("https://vh.example/api/playlist/media/123/360?token=secret")
    assert is_hls_playlist_url("https://bl.rutube.ru/route/video/master.m3u8?token=abc")
    assert is_master_playlist_url("https://river.rutube.ru/video/index.m3u8")
    assert not is_hls_playlist_url("https://rutube.ru/play/embed/video/")
    assert not is_hls_playlist_url("https://example.com/poster.jpg")


def test_extract_signed_hls_urls_from_kinescope_configuration():
    html = r"""
        <script>
          window.player = {
            "src": "https:\/\/kinescope.io\/video-id\/master.m3u8?expires=123\u0026sign=abc",
            "duplicate": "https://kinescope.io/video-id/master.m3u8?expires=123&amp;sign=abc"
          };
        </script>
    """

    assert video_signals.extract_hls_urls(html, "https://kinescope.io/embed/public-id") == [
        "https://kinescope.io/video-id/master.m3u8?expires=123&sign=abc"
    ]


def test_extract_hls_urls_does_not_stall_on_long_non_playlist_values():
    script = (
        "from getcourse_downloader.infrastructure.getcourse.video_signals import extract_hls_urls\n"
        "html = '<img src=\"data:image/png;base64,' + '/' * 80_000 + '\">'\n"
        "html += '<script>src=\"https://cdn.example/master.m3u8?sign=example\"</script>'\n"
        "assert extract_hls_urls(html, 'https://school.example/lesson') == "
        "['https://cdn.example/master.m3u8?sign=example']\n"
    )
    subprocess.run([sys.executable, "-c", script], check=True, timeout=5)
