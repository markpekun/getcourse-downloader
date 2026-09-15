import re
from html import unescape
from urllib.parse import urljoin, urlsplit

MASTER_PLAYLIST_PATH = "/api/playlist/master/"
VIDEO_PLAYER_SELECTOR = ", ".join(
    (
        "iframe.vhi-iframe",
        "iframe.js--vhi-iframe",
        "iframe[src*='rutube.ru/play/embed/']",
        "iframe[src*='kinescope.io/embed/']",
    )
)

_HLS_REFERENCE_RE = re.compile(
    r"(?:(?:https?:)?//|/)[^\"'<>\s]+?\.m3u8(?:\?[^\"'<>\s]*)?",
    flags=re.IGNORECASE,
)


def is_hls_playlist_url(url: str) -> bool:
    normalized = url.casefold()
    return MASTER_PLAYLIST_PATH in normalized or urlsplit(normalized).path.endswith(".m3u8")


def is_master_playlist_url(url: str) -> bool:
    return is_hls_playlist_url(url)


def extract_hls_urls(content: str, base_url: str) -> list[str]:
    normalized = unescape(content).replace("\\u0026", "&").replace("\\/", "/")
    urls: list[str] = []
    seen: set[str] = set()
    for match in _HLS_REFERENCE_RE.finditer(normalized):
        url = urljoin(base_url, match.group(0))
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls
