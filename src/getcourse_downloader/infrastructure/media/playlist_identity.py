from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from getcourse_downloader.infrastructure.media.hls import canonical_media_url

_VIMEO_PLAYLIST_PATH = re.compile(
    r"/(?P<asset>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    r"/psid=[^/]+/v2/(?P<representation>playlist/av/.+\.m3u8)$",
    re.IGNORECASE,
)
_VIMEO_DELIVERY_PARAMETERS = {"qsr", "r", "rh"}


def playlist_identity(url: str) -> str:
    """Identify duplicate renditions without changing the URL used to download them.

    Vimeo's v2 playlists name the asset and rendition in the path. CDN hosts,
    signed prefixes, player sessions and delivery routes can differ for the
    same rendition. Preserve its path and track selector; use the existing
    conservative URL identity for other providers and unrecognized layouts.
    This identity is for playlist selection, not persisted HLS checkpoints.
    """
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    if host.endswith(".vimeocdn.com"):
        match = _VIMEO_PLAYLIST_PATH.search(parts.path)
        if match:
            query = urlencode(
                [
                    (key, value)
                    for key, value in parse_qsl(parts.query, keep_blank_values=True)
                    if key.casefold() not in _VIMEO_DELIVERY_PARAMETERS
                ]
            )
            url = urlunsplit(
                (
                    "https",
                    "vimeocdn.com",
                    f"/{match['asset'].lower()}/v2/{match['representation']}",
                    query,
                    "",
                )
            )
    return canonical_media_url(url)
