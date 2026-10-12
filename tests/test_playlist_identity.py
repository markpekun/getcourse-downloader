import pytest

from getcourse_downloader.infrastructure.media.hls import canonical_media_url
from getcourse_downloader.infrastructure.media.playlist_identity import playlist_identity

_ASSET = "11111111-1111-4111-8111-111111111111"
_RENDITION = f"/{_ASSET}/psid=session/v2/playlist/av/asset/avf/360/media.m3u8"


def test_vimeo_identity_ignores_cdn_authentication_session_and_delivery_route():
    first = (
        f"https://vod-adaptive-ak.vimeocdn.com/exp=1~acl=fake~hmac=fake{_RENDITION}"
        "?pathsig=initial&qsr=1&r=region-one&rh=route-one&st=video"
    )
    renewed = (
        "https://skyfire.vimeocdn.com/1234-0xfake"
        + _RENDITION.replace("psid=session", "psid=renewed")
        + "?st=video&r=region-two&qsr=2&rh=route-two&PathSig=renewed"
    )

    assert canonical_media_url(first) != canonical_media_url(renewed)
    assert playlist_identity(first) == playlist_identity(renewed)
    assert "hmac" not in playlist_identity(first)
    assert "psid" not in playlist_identity(first)


@pytest.mark.parametrize(
    "other",
    [
        _RENDITION.replace(_ASSET, "22222222-2222-4222-8222-222222222222") + "?st=video",
        _RENDITION.replace("avf/360", "avf/720") + "?st=video",
        _RENDITION + "?st=audio",
        _RENDITION + "?st=video&id=another-identity",
    ],
)
def test_vimeo_identity_preserves_asset_rendition_track_and_unknown_parameters(other):
    base = "https://vod-adaptive-ak.vimeocdn.com/authentication"

    assert playlist_identity(base + _RENDITION + "?st=video") != playlist_identity(base + other)


@pytest.mark.parametrize(
    "base,path",
    [
        ("https://cdn.example", _RENDITION),
        ("https://vimeocdn.com.example", _RENDITION),
        ("https://fakevimeocdn.com", _RENDITION),
        ("https://vod-adaptive-ak.vimeocdn.com", "/unknown-layout/media.m3u8"),
    ],
)
def test_unrecognized_provider_or_layout_keeps_conservative_url_identity(base, path):
    first = base + "/authentication" + path + "?r=video-one&token=initial"
    other = base + "/authentication" + path + "?r=video-two&token=renewed"

    assert playlist_identity(first) == canonical_media_url(first)
    assert playlist_identity(first) != playlist_identity(other)
