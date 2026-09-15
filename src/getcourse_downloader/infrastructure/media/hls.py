from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp

from getcourse_downloader.application.ports.download import EventHandler
from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.infrastructure.media.ffmpeg import FfmpegMuxer

_PROGRESS_UPDATE_SECONDS = 0.25
_SPEED_UPDATE_SECONDS = 3.0
_monotonic = time.monotonic


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


def parse_master_playlist(text: str, master_url: str) -> dict[int, str]:
    qualities: dict[int, str] = {}
    last_resolution: int | None = None
    expects_variant = False
    for raw_line in text.strip().splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r"RESOLUTION=\d+x(\d+)", line)
            last_resolution = int(match.group(1)) if match else None
            expects_variant = True
        elif line and not line.startswith("#") and expects_variant:
            absolute_url = urljoin(master_url, line)
            quality = last_resolution or extract_quality(absolute_url) or 0
            if quality > 0:
                qualities[quality] = absolute_url
            last_resolution = None
            expects_variant = False
    return qualities


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
        return select_quality_url(qualities, quality)
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
    """Strip volatile signatures while retaining the stable media identity."""

    parts = urlsplit(url)
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path, "", ""))


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


def _prepare_checkpoint(
    output_mp4: Path,
    *,
    lesson_url: str,
    requested_quality: str,
    playlist_url: str,
    segment_urls: list[str],
) -> tuple[Path, list[Path], int]:
    checkpoint = _checkpoint_path(output_mp4)
    manifest_path = checkpoint / "manifest.json"
    canonical_segments = [canonical_media_url(url) for url in segment_urls]
    expected_manifest = {
        "schema_version": 1,
        "lesson_url": lesson_url,
        "requested_quality": requested_quality,
        "playlist_url": canonical_media_url(playlist_url),
        "segments": canonical_segments,
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
        is_cancelled: Callable[[], bool] | None = None,
    ) -> HlsDownloadResult:
        output_mp4 = output_without_suffix.parent / f"{output_without_suffix.name}.mp4"
        output_mp4.parent.mkdir(parents=True, exist_ok=True)
        if output_mp4.is_file() and output_mp4.stat().st_size > 0:
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
                output_path=output_mp4,
                quality=await self.probe_quality(output_mp4, fallback),
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

            encryption_method = encrypted_hls_method(playlist)
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

            segment_urls = extract_segment_urls(playlist, playlist_url)
            total = len(segment_urls)
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

            checkpoint, segment_paths, resumed = _prepare_checkpoint(
                output_mp4,
                lesson_url=lesson_url,
                requested_quality=requested_quality,
                playlist_url=playlist_url,
                segment_urls=segment_urls,
            )
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

            async def download_segment(index: int, url: str) -> bool:
                nonlocal completed, last_progress_report, last_speed_report, transferred_bytes
                path = segment_paths[index]
                if path.is_file() and path.stat().st_size > 0:
                    return True
                async with semaphore:
                    for attempt in range(3):
                        if is_cancelled and is_cancelled():
                            return False
                        temporary = path.with_suffix(".tmp")
                        try:
                            async with session.get(url) as response:
                                response.raise_for_status()
                                content = await response.read()
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
                *(download_segment(index, url) for index, url in enumerate(segment_urls))
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

            transport_stream = checkpoint / "video.ts"
            temporary_transport = checkpoint / "video.ts.tmp"
            with temporary_transport.open("wb") as destination:
                for segment in segment_paths:
                    with segment.open("rb") as source:
                        shutil.copyfileobj(source, destination, length=1024 * 1024)
            os.replace(temporary_transport, transport_stream)

            if is_cancelled and is_cancelled():
                return HlsDownloadResult(
                    HlsDownloadStatus.CANCELLED,
                    resumed_segments=resumed,
                    total_segments=total,
                )

            temporary_output = checkpoint / "output.part.mp4"
            emit(event(DownloadEventType.LOG, "Собираю MP4 через FFmpeg", stage="ffmpeg"))
            success, error_message = await self._muxer.mux(
                transport_stream,
                temporary_output,
                is_cancelled=is_cancelled,
            )
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
            os.replace(temporary_output, output_mp4)
            fallback = (
                f"{extract_quality(playlist_url)}p"
                if extract_quality(playlist_url)
                else (f"{requested_quality}p" if requested_quality.isdigit() else "")
            )
            quality = await self.probe_quality(output_mp4, fallback)
            _reset_checkpoint(checkpoint, output_mp4)
            return HlsDownloadResult(
                HlsDownloadStatus.DOWNLOADED,
                resumed_segments=resumed,
                total_segments=total,
                output_path=output_mp4,
                quality=quality,
            )
