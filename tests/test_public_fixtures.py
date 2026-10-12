import json
import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

_FIXTURES = Path(__file__).parent / "fixtures"


def test_vimeo_fixture_contains_only_fixed_synthetic_identifiers_and_credentials():
    playlists = json.loads((_FIXTURES / "vimeo_alternate_cdns.json").read_text(encoding="utf-8"))
    allowed_query_values = {
        "ext-subs": r"1",
        "omit": r"(?:av1-hevc-opus|opus)",
        "pathsig": r"signature-\d+",
        "qsr": r"1",
        "r": r"r-\d+",
        "rh": r"rh-\d+",
        "sf": r"fmp4",
        "st": r"(?:audio|video)",
        "d": r"d-\d+",
    }
    asset_ids = set()
    for playlist in playlists:
        assert set(playlist) == {"url", "text"}
        urls = [playlist["url"], *re.findall(r'https://[^\s"]+', playlist["text"])]
        for url in urls:
            parts = urlsplit(url)
            assert parts.hostname in {"vod-adaptive-ak.vimeocdn.com", "skyfire.vimeocdn.com"}
            assert parts.username is parts.password is None
            assert not parts.fragment
            assert re.fullmatch(
                r"/authentication(?:-\d+)?/(?P<asset>00000000-0000-4000-8000-00000000000[123])"
                r"/psid=session-[123]/v2/playlist/"
                r"(?:av/(?:primary/sub/subtitles-[123]-en-US/prot/cXNyPTE"
                r"|video-[123]/avf/rendition-\d+)|sub/subtitles-[123])/"
                r"(?:playlist|media)\.m3u8",
                parts.path,
            )
            asset_ids.add(parts.path.split("/psid=")[0].rsplit("/", 1)[-1])
            for key, value in parse_qsl(parts.query, keep_blank_values=True):
                assert key in allowed_query_values
                assert re.fullmatch(allowed_query_values[key], value)
    assert len(playlists) == 12
    assert len(asset_ids) == 3


def test_scheduled_lesson_fixture_uses_neutral_names_and_example_lesson_ids():
    class FixtureParser(HTMLParser):
        def handle_starttag(self, tag, attrs):
            for key, value in attrs:
                if key == "href":
                    assert value in {
                        "/teach/control/lesson/view/id/3101",
                        "/teach/control/lesson/view/id/3102",
                        "/pl/teach/control/lesson/view?id=3103&mode=preview",
                    }

        def handle_data(self, data):
            if data.strip():
                assert data.strip() in {
                    "Модуль с отложенным доступом",
                    "Уже открыт",
                    "Открытый урок",
                    "Описание вне заголовка",
                    "Откроется позже",
                    "Следующий",
                    "урок",
                    "Будущий урок",
                }

    FixtureParser().feed((_FIXTURES / "scheduled_lesson_list.html").read_text(encoding="utf-8"))
