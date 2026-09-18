from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import aiohttp

from getcourse_downloader.application.ports.download import EventHandler
from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.infrastructure.media.ffmpeg import FfmpegMuxer
from getcourse_downloader.infrastructure.storage.filenames import (
    existing_output_path,
    quality_suffixed_path,
)

_PROGRESS_UPDATE_SECONDS = 0.25
_SPEED_UPDATE_SECONDS = 3.0
_monotonic = time.monotonic
_VOLATILE_QUERY_PARAMETERS = {
    "access_token",
    "auth",
    "authorization",
    "exp",
    "expire",
    "expires",
    "expiration",
    "hdntl",
    "hdnts",
    "jwt",
    "kcd",
    "key-pair-id",
    "policy",
    "sig",
    "sign",
    "signature",
    "token",
}


def _download_error_details(error: BaseException, *, playlist: bool = False) -> tuple[str, str]:
    if isinstance(error, aiohttp.ClientResponseError) and error.status:
        return f"HTTP_{error.status}", f"Сервер вернул HTTP {error.status}"
    if isinstance(error, aiohttp.ClientConnectorDNSError):
        return "DNS_FAILED", "Не удалось найти сервер видео"
    if isinstance(error, aiohttp.ClientConnectorCertificateError):
        return "TLS_FAILED", "Не удалось установить защищённое соединение с сервером видео"
    if isinstance(error, (asyncio.TimeoutError, TimeoutError, aiohttp.ServerTimeoutError)):
        return "NETWORK_TIMEOUT", "Превышено время ожидания ответа сервера видео"
    if isinstance(error, aiohttp.ClientConnectorError):
        return "CONNECTION_FAILED", "Не удалось подключиться к серверу видео"
    if isinstance(error, OSError) and str(error) == "empty HLS segment":
        return "EMPTY_SEGMENT", "Сервер вернул пустой сегмент видео"
    if playlist:
        return "PLAYLIST_REQUEST_FAILED", "Не удалось получить плейлист видео"
    return "SEGMENT_REQUEST_FAILED", "Не удалось получить сегмент видео"


def extract_quality(url: str) -> int:
    path = url.split("?", 1)[0]
    numeric_parts = [part for part in path.split("/") if part.isdigit()]
    return int(numeric_parts[-1]) if numeric_parts else 0


@dataclass(frozen=True, slots=True)
class HlsVariant:
    url: str
    height: int = 0
    bandwidth: int = 0


class HlsKeyError(ValueError):
    """A media playlist has no usable clear-key SAMPLE-AES declaration."""


@dataclass(frozen=True, slots=True)
class HlsKeyDeclaration:
    method: str
    uri: str
    key_format: str = "identity"


@dataclass(frozen=True, slots=True)
class HlsByteRange:
    offset: int
    length: int


@dataclass(frozen=True, slots=True)
class HlsMediaResource:
    url: str
    byte_range: HlsByteRange | None
    role: Literal["init", "media"]


def _hls_attributes(attribute_list: str) -> dict[str, str]:
    attributes: dict[str, str] = {}
    position = 0
    while position < len(attribute_list):
        match = re.match(r"([A-Za-z0-9-]+)=", attribute_list[position:])
        if match is None:
            raise HlsKeyError("invalid HLS key attribute")
        name = match.group(1).upper()
        position += match.end()
        if position < len(attribute_list) and attribute_list[position] == '"':
            end = attribute_list.find('"', position + 1)
            if end == -1:
                raise HlsKeyError("unterminated HLS key string")
            value = attribute_list[position + 1 : end]
            position = end + 1
        else:
            end = attribute_list.find(",", position)
            if end == -1:
                end = len(attribute_list)
            value = attribute_list[position:end]
            position = end
        if not value or name in attributes:
            raise HlsKeyError("invalid HLS key attribute")
        attributes[name] = value
        if position < len(attribute_list):
            if attribute_list[position] != ",":
                raise HlsKeyError("invalid HLS key separator")
            position += 1
    return attributes


def parse_key_tag(line: str, playlist_url: str) -> HlsKeyDeclaration | None:
    """Parse one identity SAMPLE-AES key tag without retaining its signed query."""

    if not line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
        raise HlsKeyError("not an HLS key tag")
    attributes = _hls_attributes(line.split(":", 1)[1])
    method = attributes.get("METHOD", "").upper()
    if method == "NONE":
        return None
    if method != "SAMPLE-AES":
        raise HlsKeyError(f"unsupported HLS encryption method {method or 'missing'}")
    key_format = attributes.get("KEYFORMAT", "identity").casefold()
    if key_format != "identity":
        raise HlsKeyError("SAMPLE-AES requires KEYFORMAT=identity")
    uri = attributes.get("URI")
    if uri is None:
        raise HlsKeyError("SAMPLE-AES key URI is missing")
    absolute_uri = urljoin(playlist_url, uri)
    if urlsplit(absolute_uri).scheme.casefold() != "https":
        raise HlsKeyError("SAMPLE-AES key URI must use HTTPS")
    return HlsKeyDeclaration(method, absolute_uri, key_format)


def parse_session_key(playlist: str, playlist_url: str) -> HlsKeyDeclaration | None:
    for raw_line in playlist.splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-SESSION-KEY:"):
            return parse_key_tag(line, playlist_url)
    return None


def _parse_byte_range(value: str, *, implicit_offset: int | None) -> HlsByteRange:
    length_text, separator, offset_text = value.partition("@")
    try:
        length = int(length_text)
        offset = int(offset_text) if separator else implicit_offset
    except ValueError as error:
        raise HlsKeyError("invalid HLS byte range") from error
    if length <= 0:
        raise HlsKeyError("HLS byte range length must be positive")
    if offset is None:
        raise HlsKeyError("implicit HLS byte range has no preceding offset")
    if offset < 0:
        raise HlsKeyError("HLS byte range offset must not be negative")
    return HlsByteRange(offset, length)


def parse_media_resources(playlist: str, playlist_url: str) -> tuple[HlsMediaResource, ...]:
    """Return the exact init/media byte sequence represented by an HLS playlist."""

    media: list[HlsMediaResource] = []
    init: HlsMediaResource | None = None
    pending_range: str | None = None
    next_offset_by_url: dict[str, int] = {}
    for raw_line in playlist.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-MAP:"):
            attributes = _hls_attributes(line.split(":", 1)[1])
            uri = attributes.get("URI")
            if uri is None:
                raise HlsKeyError("EXT-X-MAP URI is missing")
            url = urljoin(playlist_url, uri)
            value = attributes.get("BYTERANGE")
            byte_range = (
                _parse_byte_range(value, implicit_offset=None) if value is not None else None
            )
            init = HlsMediaResource(url, byte_range, "init")
            continue
        if line.startswith("#EXT-X-BYTERANGE:"):
            pending_range = line.split(":", 1)[1]
            continue
        if line.startswith("#"):
            continue
        url = urljoin(playlist_url, line)
        if urlsplit(url).path.casefold().endswith(".m3u8"):
            continue
        byte_range = None
        if pending_range is not None:
            byte_range = _parse_byte_range(
                pending_range,
                implicit_offset=next_offset_by_url.get(url),
            )
            next_offset_by_url[url] = byte_range.offset + byte_range.length
            pending_range = None
        media.append(HlsMediaResource(url, byte_range, "media"))

    if pending_range is not None:
        raise HlsKeyError("HLS byte range has no media URI")
    if init is None:
        return tuple(media)
    if media:
        init_parts = urlsplit(init.url)
        first_parts = urlsplit(media[0].url)
        if not init_parts.query and (init_parts.scheme, init_parts.netloc, init_parts.path) == (
            first_parts.scheme,
            first_parts.netloc,
            first_parts.path,
        ):
            init = HlsMediaResource(media[0].url, init.byte_range, "init")
    return (init, *media)


def has_unsupported_hls_encryption(text: str) -> str | None:
    """Return a protected method that is outside the clear-key SAMPLE-AES scope."""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
            continue
        try:
            attributes = _hls_attributes(line.split(":", 1)[1])
        except HlsKeyError:
            return "UNKNOWN"
        method = attributes.get("METHOD", "").upper()
        if method == "NONE":
            continue
        key_format = attributes.get("KEYFORMAT", "identity").casefold()
        if method == "SAMPLE-AES" and key_format == "identity":
            continue
        return method or "UNKNOWN"
    return None


def parse_master_variants(text: str, master_url: str) -> tuple[HlsVariant, ...]:
    variants: list[HlsVariant] = []
    attributes: str | None = None
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes = line
        elif line and not line.startswith("#") and attributes is not None:
            absolute_url = urljoin(master_url, line)
            resolution = re.search(r"(?:[:,])RESOLUTION=\d+x(\d+)", attributes)
            bandwidth = re.search(r"(?:[:,])BANDWIDTH=(\d+)", attributes)
            variants.append(
                HlsVariant(
                    absolute_url,
                    int(resolution.group(1)) if resolution else extract_quality(absolute_url),
                    int(bandwidth.group(1)) if bandwidth else 0,
                )
            )
            attributes = None
    return tuple(variants)


def parse_master_playlist(text: str, master_url: str) -> dict[int, str]:
    return {
        variant.height: variant.url
        for variant in parse_master_variants(text, master_url)
        if variant.height > 0
    }


def is_hls_playlist(text: str) -> bool:
    return text.lstrip().startswith("#EXTM3U")


def is_hls_master_playlist(text: str) -> bool:
    return is_hls_playlist(text) and "#EXT-X-STREAM-INF" in text


def encrypted_hls_method(text: str) -> str | None:
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith(("#EXT-X-KEY:", "#EXT-X-SESSION-KEY:")):
            continue
        match = re.search(r"(?::|,)METHOD=([^,]+)", line, flags=re.IGNORECASE)
        if match:
            method = match.group(1).strip().upper()
            if method != "NONE":
                return method
    return None


def select_quality_url(qualities: dict[int, str], quality: str) -> str | None:
    if not qualities:
        return None
    available = sorted(qualities)
    if not quality or quality == "auto":
        return qualities[available[-1]]
    target = int(quality)
    if target in qualities:
        return qualities[target]
    below = [candidate for candidate in available if candidate < target]
    return qualities[below[-1] if below else available[0]]


def select_stream_playlist_url(text: str, playlist_url: str, quality: str) -> str | None:
    if is_hls_master_playlist(text):
        qualities = parse_master_playlist(text, playlist_url)
        if qualities:
            return select_quality_url(qualities, quality)
        variants = parse_master_variants(text, playlist_url)
        return max(variants, key=lambda variant: variant.bandwidth).url if variants else None
    if is_hls_playlist(text):
        return playlist_url
    return None


def extract_segment_urls(playlist: str, playlist_url: str) -> list[str]:
    segments: list[str] = []
    for raw_line in playlist.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        absolute_url = urljoin(playlist_url, line)
        if urlsplit(absolute_url).path.casefold().endswith(".m3u8"):
            continue
        segments.append(absolute_url)
    return segments


def canonical_media_url(url: str) -> str:
    """Retain media identity without persisting query values or rotating credentials."""

    parts = urlsplit(url)
    identity = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.casefold() not in _VOLATILE_QUERY_PARAMETERS
        and not key.casefold().startswith(("x-amz-", "x-goog-"))
    )
    digest = hashlib.sha256(urlencode(identity).encode("utf-8")).hexdigest() if identity else ""
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, "", digest))


class HlsDownloadStatus(StrEnum):
    DOWNLOADED = "downloaded"
    ALREADY_PRESENT = "already_present"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class HlsDownloadResult:
    status: HlsDownloadStatus
    resumed_segments: int = 0
    total_segments: int = 0
    output_path: Path | None = None
    quality: str = ""


def _checkpoint_path(output_mp4: Path) -> Path:
    return output_mp4.with_name(f".{output_mp4.name}.gcd-part")


def _reset_checkpoint(checkpoint: Path, output_mp4: Path) -> None:
    expected = _checkpoint_path(output_mp4)
    if checkpoint.resolve() != expected.resolve():
        raise ValueError("Refusing to remove an unexpected checkpoint path")
    if checkpoint.is_symlink() or checkpoint.is_file():
        checkpoint.unlink(missing_ok=True)
    elif checkpoint.is_dir():
        shutil.rmtree(checkpoint)


def _write_json_atomic(path: Path, payload: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_concat_list(path: Path, segment_paths: list[Path]) -> None:
    """Write an FFmpeg concat-demuxer list without exposing it outside the checkpoint."""

    lines = ["ffconcat version 1.0"]
    for segment in segment_paths:
        value = segment.resolve().as_posix().replace("'", r"'\\''")
        lines.append(f"file '{value}'")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _allows_local_decryption_fallback(message: str) -> bool:
    normalized = message.casefold()
    return bool(normalized) and normalized != "cancelled"


def _prepare_checkpoint(
    output_mp4: Path,
    *,
    lesson_url: str,
    requested_quality: str,
    playlist_url: str,
    segment_urls: list[str],
    segment_encryption: list[dict[str, str | int] | None] | None = None,
    resource_ranges: list[HlsByteRange | None] | None = None,
    resource_roles: list[str] | None = None,
) -> tuple[Path, list[Path], int]:
    checkpoint = _checkpoint_path(output_mp4)
    manifest_path = checkpoint / "manifest.json"
    canonical_segments = [canonical_media_url(url) for url in segment_urls]
    ranges = resource_ranges or [None] * len(segment_urls)
    roles = resource_roles or ["media"] * len(segment_urls)
    expected_manifest = {
        "schema_version": 3,
        "lesson_url": lesson_url,
        "requested_quality": requested_quality,
        "playlist_url": canonical_media_url(playlist_url),
        "resources": [
            {
                "role": role,
                "url": url,
                "offset": byte_range.offset if byte_range else None,
                "length": byte_range.length if byte_range else None,
            }
            for role, url, byte_range in zip(roles, canonical_segments, ranges, strict=True)
        ],
        "segment_encryption": segment_encryption or [None] * len(segment_urls),
    }

    current_manifest: object = None
    if manifest_path.is_file():
        try:
            current_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current_manifest = None
    if current_manifest != expected_manifest and checkpoint.exists():
        _reset_checkpoint(checkpoint, output_mp4)

    segment_dir = checkpoint / "segments"
    segment_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(manifest_path, expected_manifest)

    for temporary in segment_dir.glob("*.tmp"):
        temporary.unlink(missing_ok=True)

    segment_paths = [segment_dir / f"{index:06d}.bin" for index in range(len(segment_urls))]
    resumed = 0
    for segment in segment_paths:
        try:
            if segment.is_file() and segment.stat().st_size > 0:
                resumed += 1
            elif segment.exists():
                segment.unlink(missing_ok=True)
        except OSError:
            segment.unlink(missing_ok=True)
    return checkpoint, segment_paths, resumed


class HlsDownloader:
    def __init__(
        self,
        muxer: FfmpegMuxer,
        *,
        concurrency: int = 10,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._muxer = muxer
        self._concurrency = concurrency
        self._session_factory = session_factory or aiohttp.ClientSession

    async def probe_quality(self, media: Path, fallback: str = "") -> str:
        probe = getattr(self._muxer, "probe_height", None)
        if probe is not None:
            try:
                height = await probe(media)
            except (OSError, RuntimeError):
                height = None
            if isinstance(height, int) and height > 0:
                return f"{height}p"
        return fallback

    async def download(
        self,
        playlist_url: str,
        output_without_suffix: Path,
        lesson_title: str,
        emit: EventHandler,
        *,
        lesson_url: str = "",
        referer_url: str = "",
        course_path: tuple[str, ...] = (),
        requested_quality: str = "auto",
        video_index: int = 1,
        video_total: int = 1,
        session_key: HlsKeyDeclaration | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> HlsDownloadResult:
        output_mp4 = output_without_suffix.parent / f"{output_without_suffix.name}.mp4"
        output_mp4.parent.mkdir(parents=True, exist_ok=True)
        existing_output = existing_output_path(output_without_suffix)
        if existing_output is not None:
            checkpoint = _checkpoint_path(output_mp4)
            if checkpoint.exists():
                _reset_checkpoint(checkpoint, output_mp4)
            fallback = (
                f"{extract_quality(playlist_url)}p"
                if extract_quality(playlist_url)
                else (f"{requested_quality}p" if requested_quality.isdigit() else "")
            )
            return HlsDownloadResult(
                HlsDownloadStatus.ALREADY_PRESENT,
                output_path=existing_output,
                quality=await self.probe_quality(existing_output, fallback),
            )

        if is_cancelled and is_cancelled():
            return HlsDownloadResult(HlsDownloadStatus.CANCELLED)

        def event(
            event_type: DownloadEventType,
            message: str,
            *,
            stage: str,
            current: int | None = None,
            total: int | None = None,
            level: str = "info",
            speed_bps: float | None = None,
            error_code: str = "",
            source_host: str = "",
        ) -> DownloadEvent:
            return DownloadEvent(
                event_type,
                message=message,
                stage=stage,
                lesson=lesson_title,
                lesson_url=lesson_url,
                course_path=course_path,
                video_index=video_index,
                video_total=video_total,
                current=current,
                total=total,
                speed_bps=speed_bps,
                level=level,
                error_code=error_code,
                source_host=source_host,
            )

        parts = urlsplit(playlist_url)
        request_referer = referer_url or lesson_url or f"{parts.scheme}://{parts.netloc}/"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": request_referer,
        }
        referer_parts = urlsplit(request_referer)
        if referer_parts.scheme and referer_parts.netloc:
            headers["Origin"] = f"{referer_parts.scheme}://{referer_parts.netloc}"
        timeout = aiohttp.ClientTimeout(
            total=600,
            connect=15,
            sock_connect=15,
            sock_read=15,
        )

        async with self._session_factory(headers=headers, timeout=timeout) as session:
            try:
                async with session.get(playlist_url) as response:
                    response.raise_for_status()
                    playlist = await response.text()
            except Exception as error:
                error_code, summary = _download_error_details(error, playlist=True)
                emit(
                    event(
                        DownloadEventType.ERROR,
                        summary,
                        stage="playlist",
                        level="error",
                        error_code=error_code,
                        source_host=parts.netloc,
                    )
                )
                return HlsDownloadResult(HlsDownloadStatus.FAILED)

            encryption_method = has_unsupported_hls_encryption(playlist)
            if encryption_method:
                emit(
                    event(
                        DownloadEventType.ERROR,
                        f"Плейлист использует защищённое шифрование {encryption_method}",
                        stage="playlist",
                        level="error",
                        error_code="ENCRYPTED_PLAYLIST_UNSUPPORTED",
                        source_host=parts.netloc,
                    )
                )
                return HlsDownloadResult(HlsDownloadStatus.FAILED)

            try:
                resources = parse_media_resources(playlist, playlist_url)
            except HlsKeyError as error:
                emit(
                    event(
                        DownloadEventType.ERROR,
                        f"Некорректная структура HLS: {error}",
                        stage="playlist",
                        level="error",
                        error_code="HLS_STRUCTURE_INVALID",
                        source_host=parts.netloc,
                    )
                )
                return HlsDownloadResult(HlsDownloadStatus.FAILED)

            try:
                key_lines = [
                    raw_line.strip()
                    for raw_line in playlist.splitlines()
                    if raw_line.strip().startswith("#EXT-X-KEY:")
                ]
                parsed_media_keys = [parse_key_tag(line, playlist_url) for line in key_lines]
                media_keys = [key for key in parsed_media_keys if key is not None]
                unique_keys = {key.uri: key for key in media_keys}
                if len(unique_keys) > 1:
                    raise HlsKeyError("multiple SAMPLE-AES keys are unsupported")
                key_declaration = (
                    next(iter(unique_keys.values()), None) if key_lines else session_key
                )
            except HlsKeyError as error:
                emit(
                    event(
                        DownloadEventType.ERROR,
                        f"Некорректный ключ SAMPLE-AES: {error}",
                        stage="playlist",
                        level="error",
                        error_code="HLS_KEY_INVALID",
                        source_host=parts.netloc,
                    )
                )
                return HlsDownloadResult(HlsDownloadStatus.FAILED)

            segment_urls = [resource.url for resource in resources]
            total = len(resources)
            if not total:
                emit(
                    event(
                        DownloadEventType.ERROR,
                        "В плейлисте нет сегментов",
                        stage="segments",
                        level="error",
                        error_code="EMPTY_PLAYLIST",
                        source_host=parts.netloc,
                    )
                )
                return HlsDownloadResult(HlsDownloadStatus.FAILED)

            if key_declaration is not None:
                support_check = getattr(self._muxer, "sample_aes_support", None)
                if support_check is not None:
                    supported, support_message = await support_check()
                    if not supported:
                        emit(
                            event(
                                DownloadEventType.ERROR,
                                support_message,
                                stage="ffmpeg",
                                level="error",
                                error_code="FFMPEG_VERSION_UNSUPPORTED",
                            )
                        )
                        return HlsDownloadResult(HlsDownloadStatus.FAILED)

            checkpoint, segment_paths, resumed = _prepare_checkpoint(
                output_mp4,
                lesson_url=lesson_url,
                requested_quality=requested_quality,
                playlist_url=playlist_url,
                segment_urls=segment_urls,
                segment_encryption=[
                    {"method": "SAMPLE-AES"} if key_declaration is not None else None
                    for _ in resources
                ],
                resource_ranges=[resource.byte_range for resource in resources],
                resource_roles=[resource.role for resource in resources],
            )
            decryption_key_hex: str | None = None
            if key_declaration is not None:
                key_host = urlsplit(key_declaration.uri).netloc
                try:
                    async with session.get(key_declaration.uri) as response:
                        response.raise_for_status()
                        key_data = (await response.read()).strip(b" \t\r\n")
                    if len(key_data) != 16:
                        raise HlsKeyError("сервер ключа вернул не 16 байт")
                    try:
                        key_data.decode("ascii")
                    except UnicodeDecodeError as error:
                        raise HlsKeyError("сервер ключа вернул не ASCII") from error
                    decryption_key_hex = key_data.hex()
                except HlsKeyError as error:
                    emit(
                        event(
                            DownloadEventType.ERROR,
                            f"Некорректный ключ SAMPLE-AES: {error}",
                            stage="playlist",
                            level="error",
                            error_code="HLS_KEY_INVALID",
                            source_host=key_host,
                        )
                    )
                    return HlsDownloadResult(HlsDownloadStatus.FAILED)
                except (TimeoutError, aiohttp.ClientError, OSError) as error:
                    error_code, summary = _download_error_details(error)
                    if error_code == "HTTP_403":
                        summary = (
                            "Ссылка для получения ключа истекла. Перезапустите загрузку урока."
                        )
                    emit(
                        event(
                            DownloadEventType.ERROR,
                            summary,
                            stage="playlist",
                            level="error",
                            error_code=f"HLS_KEY_{error_code}",
                            source_host=key_host,
                        )
                    )
                    return HlsDownloadResult(HlsDownloadStatus.FAILED)
            completed = resumed
            emit(
                event(
                    DownloadEventType.VIDEO_FOUND,
                    f"Видео найдено: {completed}/{total}",
                    stage="segments",
                    current=completed,
                    total=total,
                    level="success",
                )
            )

            semaphore = asyncio.Semaphore(self._concurrency)
            last_progress_report = float("-inf")
            started_at = _monotonic()
            last_speed_report = started_at
            transferred_bytes = 0
            segment_failures: list[tuple[int, str, str, str]] = []

            async def download_segment(index: int, resource: HlsMediaResource) -> bool:
                nonlocal completed, last_progress_report, last_speed_report, transferred_bytes
                url = resource.url
                path = segment_paths[index]
                if path.is_file() and path.stat().st_size > 0:
                    return True
                async with semaphore:
                    for attempt in range(3):
                        if is_cancelled and is_cancelled():
                            return False
                        temporary = path.with_suffix(".tmp")
                        try:
                            request_headers = None
                            if resource.byte_range is not None:
                                start = resource.byte_range.offset
                                end = start + resource.byte_range.length - 1
                                request_headers = {"Range": f"bytes={start}-{end}"}
                            async with session.get(url, headers=request_headers) as response:
                                response.raise_for_status()
                                content = await response.read()
                            if resource.byte_range is not None:
                                expected = resource.byte_range.length
                                if len(content) != expected:
                                    start = resource.byte_range.offset
                                    end = start + expected
                                    if len(content) >= end:
                                        content = content[start:end]
                                    else:
                                        raise OSError("invalid HLS byte range length")
                            if not content:
                                raise OSError("empty HLS segment")
                            temporary.write_bytes(content)
                            os.replace(temporary, path)
                            completed += 1
                            transferred_bytes += len(content)
                            now = _monotonic()
                            if (
                                now - last_progress_report >= _PROGRESS_UPDATE_SECONDS
                                or completed == total
                            ):
                                last_progress_report = now
                                speed_bps = None
                                if (
                                    now - last_speed_report >= _SPEED_UPDATE_SECONDS
                                    or completed == total
                                ):
                                    last_speed_report = now
                                    elapsed = max(now - started_at, 0.001)
                                    speed_bps = transferred_bytes / elapsed
                                emit(
                                    event(
                                        DownloadEventType.PROGRESS,
                                        f"Сегменты: {completed}/{total}",
                                        stage="segments",
                                        current=completed,
                                        total=total,
                                        speed_bps=speed_bps,
                                    )
                                )
                            return True
                        except (TimeoutError, aiohttp.ClientError, OSError) as error:
                            temporary.unlink(missing_ok=True)
                            if attempt == 2:
                                error_code, summary = _download_error_details(error)
                                segment_failures.append(
                                    (index, error_code, summary, urlsplit(url).netloc)
                                )
                                return False
                            await asyncio.sleep(2**attempt)
                return False

            results = await asyncio.gather(
                *(download_segment(index, resource) for index, resource in enumerate(resources))
            )
            if is_cancelled and is_cancelled():
                return HlsDownloadResult(
                    HlsDownloadStatus.CANCELLED,
                    resumed_segments=resumed,
                    total_segments=total,
                )
            if not all(results):
                failed_count = sum(not result for result in results)
                failed_index, error_code, summary, source_host = min(segment_failures)
                emit(
                    event(
                        DownloadEventType.ERROR,
                        f"{summary}: сегмент {failed_index + 1}/{total}; ошибок: {failed_count}",
                        stage="segments",
                        current=completed,
                        total=total,
                        level="error",
                        error_code=error_code,
                        source_host=source_host,
                    )
                )
                return HlsDownloadResult(
                    HlsDownloadStatus.FAILED,
                    resumed_segments=resumed,
                    total_segments=total,
                )

            temporary_output = checkpoint / "output.part.mp4"
            if decryption_key_hex is not None:
                success, error_message = await self._muxer.decrypt_fragments(
                    segment_paths,
                    temporary_output,
                    decryption_key_hex=decryption_key_hex,
                    is_cancelled=is_cancelled,
                )
            else:
                concat_list = checkpoint / "segments.ffconcat"
                _write_concat_list(concat_list, segment_paths)
                success, error_message = await self._muxer.mux_concat(
                    concat_list,
                    temporary_output,
                    is_cancelled=is_cancelled,
                )
            if is_cancelled and is_cancelled():
                return HlsDownloadResult(
                    HlsDownloadStatus.CANCELLED,
                    resumed_segments=resumed,
                    total_segments=total,
                )
            if not success and decryption_key_hex is None:
                transport_stream = checkpoint / "video.ts"
                temporary_transport = checkpoint / "video.ts.tmp"
                with temporary_transport.open("wb") as destination:
                    for segment_path in segment_paths:
                        with segment_path.open("rb") as source:
                            shutil.copyfileobj(source, destination, length=1024 * 1024)
                os.replace(temporary_transport, transport_stream)
                if is_cancelled and is_cancelled():
                    return HlsDownloadResult(
                        HlsDownloadStatus.CANCELLED,
                        resumed_segments=resumed,
                        total_segments=total,
                    )
                success, error_message = await self._muxer.mux(
                    transport_stream,
                    temporary_output,
                    is_cancelled=is_cancelled,
                )
            elif (
                not success
                and decryption_key_hex is not None
                and _allows_local_decryption_fallback(error_message)
            ):
                encrypted_mp4 = checkpoint / "video.enc.mp4"
                temporary_encrypted = checkpoint / "video.enc.mp4.tmp"
                with temporary_encrypted.open("wb") as destination:
                    for segment_path in segment_paths:
                        with segment_path.open("rb") as source:
                            shutil.copyfileobj(source, destination, length=1024 * 1024)
                os.replace(temporary_encrypted, encrypted_mp4)
                success, error_message = await self._muxer.decrypt_file(
                    encrypted_mp4,
                    temporary_output,
                    decryption_key_hex=decryption_key_hex,
                    is_cancelled=is_cancelled,
                )
                if success:
                    encrypted_mp4.unlink(missing_ok=True)
            if is_cancelled and is_cancelled():
                return HlsDownloadResult(
                    HlsDownloadStatus.CANCELLED,
                    resumed_segments=resumed,
                    total_segments=total,
                )
            if not success:
                emit(
                    event(
                        DownloadEventType.ERROR,
                        f"Ошибка FFmpeg: {error_message}",
                        stage="ffmpeg",
                        level="error",
                        error_code="FFMPEG_FAILED",
                    )
                )
                return HlsDownloadResult(
                    HlsDownloadStatus.FAILED,
                    resumed_segments=resumed,
                    total_segments=total,
                )
            fallback = (
                f"{extract_quality(playlist_url)}p"
                if extract_quality(playlist_url)
                else (f"{requested_quality}p" if requested_quality.isdigit() else "")
            )
            quality = await self.probe_quality(temporary_output, fallback)
            final_output = quality_suffixed_path(output_without_suffix, quality)
            os.replace(temporary_output, final_output)
            _reset_checkpoint(checkpoint, output_mp4)
            return HlsDownloadResult(
                HlsDownloadStatus.DOWNLOADED,
                resumed_segments=resumed,
                total_segments=total,
                output_path=final_output,
                quality=quality,
            )
