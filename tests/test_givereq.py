from getcourse_downloader.infrastructure.media.hls import (
    extract_quality,
    extract_segment_urls,
    is_hls_master_playlist,
    is_hls_playlist,
    parse_master_playlist,
    select_quality_url,
    select_stream_playlist_url,
)
from getcourse_downloader.infrastructure.storage.filenames import sanitize_filename


def test_sanitize_filename_removes_status_words():
    assert sanitize_filename("Урок 1 Просмотрено") == "Урок 1"
    assert sanitize_filename("Урок 2 Пройдено") == "Урок 2"
    assert sanitize_filename("Урок 3 Завершено") == "Урок 3"


def test_sanitize_filename_collapses_whitespace():
    assert sanitize_filename("Урок   с  пробелами") == "Урок с пробелами"


def test_sanitize_filename_replaces_illegal_chars():
    assert sanitize_filename('a/b\\c:d*e?f"g<h>i|') == "a_b_c_d_e_f_g_h_i_"


def test_sanitize_filename_protects_windows_reserved_names_with_extensions():
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("con.txt") == "_con.txt"
    assert sanitize_filename("LPT1.mp4") == "_LPT1.mp4"


def test_extract_quality_from_url():
    assert extract_quality("https://example.com/video/720/index.m3u8") == 720
    assert extract_quality("https://example.com/video/1080/index.m3u8") == 1080
    assert extract_quality("https://example.com/video/noquality/index.m3u8") == 0


MASTER_PLAYLIST = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080/index.m3u8
"""


def test_parse_master_playlist_resolves_relative_urls():
    qualities = parse_master_playlist(MASTER_PLAYLIST, "https://example.com/master.m3u8")
    assert qualities == {
        360: "https://example.com/360/index.m3u8",
        720: "https://example.com/720/index.m3u8",
        1080: "https://example.com/1080/index.m3u8",
    }


def test_parse_master_playlist_uses_resolution_fallback():
    playlist = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\nstream.m3u8\n"
    qualities = parse_master_playlist(playlist, "https://example.com/master.m3u8")
    assert qualities == {720: "https://example.com/stream.m3u8"}


def test_parse_master_playlist_prefers_resolution_over_rutube_path_ids():
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n"
        "https://river.rutube.example/hls-vod/3324/video.mp4.m3u8\n"
    )

    assert parse_master_playlist(playlist, "https://bl.rutube.example/route/video.m3u8") == {
        720: "https://river.rutube.example/hls-vod/3324/video.mp4.m3u8"
    }


def test_select_quality_auto_picks_highest():
    qualities = {360: "a", 720: "b", 1080: "c"}
    assert select_quality_url(qualities, "auto") == "c"


def test_select_quality_exact_match():
    qualities = {360: "a", 720: "b", 1080: "c"}
    assert select_quality_url(qualities, "720") == "b"


def test_select_quality_falls_back_to_best_below():
    qualities = {360: "a", 720: "b"}
    assert select_quality_url(qualities, "1080") == "b"


def test_select_quality_falls_back_to_lowest():
    qualities = {1080: "c"}
    assert select_quality_url(qualities, "360") == "c"


def test_select_quality_empty():
    assert select_quality_url({}, "720") is None


def test_extract_quality_ignores_query_string():
    assert extract_quality("https://example.com/video/720/index.m3u8?token=abc") == 720


def test_extract_quality_ignores_query_numbers():
    assert extract_quality("https://example.com/index.m3u8?quality=1080") == 0


def test_parse_master_playlist_absolute_urls():
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720\n"
        "https://cdn.example.com/720/index.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n"
        "https://cdn.example.com/1080/index.m3u8\n"
    )
    qualities = parse_master_playlist(playlist, "https://example.com/master.m3u8")
    assert qualities == {
        720: "https://cdn.example.com/720/index.m3u8",
        1080: "https://cdn.example.com/1080/index.m3u8",
    }


def test_parse_master_playlist_uses_url_quality_without_resolution():
    playlist = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000000\n480/index.m3u8\n"
    qualities = parse_master_playlist(playlist, "https://example.com/master.m3u8")
    assert qualities == {480: "https://example.com/480/index.m3u8"}


def test_parse_master_playlist_empty():
    assert parse_master_playlist("", "https://example.com/master.m3u8") == {}


def test_bandwidth_only_variants_are_selectable_without_inventing_a_resolution():
    playlist = (
        "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=5000000\nhigh.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=2000000\nlow.m3u8\n"
    )
    for quality in ("auto", "720"):
        assert select_stream_playlist_url(playlist, "https://cdn/master.m3u8", quality) == (
            "https://cdn/high.m3u8"
        )


def test_hls_playlist_types_and_stream_selection():
    media_playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nsegment/00001\n"

    assert is_hls_playlist(MASTER_PLAYLIST)
    assert is_hls_master_playlist(MASTER_PLAYLIST)
    assert is_hls_playlist(media_playlist)
    assert not is_hls_master_playlist(media_playlist)
    assert (
        select_stream_playlist_url(
            MASTER_PLAYLIST,
            "https://example.com/master.m3u8",
            "720",
        )
        == "https://example.com/720/index.m3u8"
    )
    assert (
        select_stream_playlist_url(
            media_playlist,
            "https://river.rutube.ru/video/index.m3u8",
            "1080",
        )
        == "https://river.rutube.ru/video/index.m3u8"
    )


def test_extract_segment_urls_resolves_relative():
    playlist = "#EXTM3U\nseg1.ts\nseg2.bin\n#EXTINF:5,\n"
    urls = extract_segment_urls(playlist, "https://cdn.example.com/video/720/index.m3u8")
    assert urls == [
        "https://cdn.example.com/video/720/seg1.ts",
        "https://cdn.example.com/video/720/seg2.bin",
    ]


def test_extract_segment_urls_keeps_absolute():
    playlist = "#EXTM3U\nhttps://cdn.example.com/seg1.bin\n"
    urls = extract_segment_urls(playlist, "https://cdn.example.com/video/index.m3u8")
    assert urls == ["https://cdn.example.com/seg1.bin"]


def test_extract_segment_urls_ignores_comments_and_others():
    playlist = "#EXTM3U\n#EXT-X-VERSION:3\n#EXTINF:6,\nseg1.ts\nhttps://other.example.com/a.m3u8\n"
    urls = extract_segment_urls(playlist, "https://cdn.example.com/video/720/index.m3u8")
    assert urls == ["https://cdn.example.com/video/720/seg1.ts"]


def test_extract_segment_urls_accepts_extensionless_rutube_segments():
    playlist = "#EXTM3U\n#EXTINF:6,\nsegment/00001?token=abc\n"

    assert extract_segment_urls(playlist, "https://river.rutube.ru/video/index.m3u8") == [
        "https://river.rutube.ru/video/segment/00001?token=abc"
    ]
