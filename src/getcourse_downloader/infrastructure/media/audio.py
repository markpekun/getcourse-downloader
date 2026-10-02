from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import aiohttp

from getcourse_downloader.infrastructure.media.cancellation import (
    MediaDownloadCancelled,
    await_or_cancel,
)

_PROGRESS_UPDATE_SECONDS = 0.3


@dataclass(frozen=True, slots=True)
class AudioSource:
    """A direct audio resource found in an already loaded lesson document."""

    url: str
    title: str = ""


class DirectAudioDownloadStatus(StrEnum):
    DOWNLOADED = "downloaded"
    ALREADY_PRESENT = "already_present"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class DirectAudioDownloadResult:
    status: DirectAudioDownloadStatus
    output_path: Path | None = None
    error_code: str = ""
    error_message: str = ""


class _AudioElementParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self._audio_titles: list[str] = []
        self.sources: list[AudioSource] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key.casefold(): (value or "").strip() for key, value in attrs}
        if tag.casefold() == "audio":
            title = attributes.get("title") or attributes.get("data-title") or ""
            self._audio_titles.append(title)
            self._add_source(attributes.get("src", ""), title)
        elif tag.casefold() == "source" and self._audio_titles:
            self._add_source(attributes.get("src", ""), self._audio_titles[-1])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() == "audio":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "audio" and self._audio_titles:
            self._audio_titles.pop()

    def _add_source(self, raw_url: str, title: str) -> None:
        if not raw_url:
            return
        url = urljoin(self._base_url, raw_url)
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.netloc:
            return
        self.sources.append(AudioSource(url, title))


def extract_audio_sources(document_html: str, base_url: str) -> tuple[AudioSource, ...]:
    """Passively extract direct audio references from a lesson document.

    This function deliberately only reads already available HTML. It does not
    invoke a player, evaluate page JavaScript, or issue a network request.
    """

    parser = _AudioElementParser(base_url)
    parser.feed(document_html)
    parser.close()
    unique: list[AudioSource] = []
    seen_urls: set[str] = set()
    for source in parser.sources:
        if source.url in seen_urls:
            continue
        seen_urls.add(source.url)
        unique.append(source)
    return tuple(unique)


class DirectAudioDownloader:
    """Stream a direct audio resource without turning it into a video workflow."""

    def __init__(self, *, session_factory=None) -> None:
        self._session_factory = session_factory or aiohttp.ClientSession

    async def download(
        self,
        source_url: str,
        output_path: Path,
        *,
        is_cancelled: Callable[[], bool] | None = None,
        cancellation_event: asyncio.Event | None = None,
        on_progress: Callable[[int, int | None], None] | None = None,
    ) -> DirectAudioDownloadResult:
        if is_cancelled and is_cancelled():
            return DirectAudioDownloadResult(DirectAudioDownloadStatus.CANCELLED)
        try:
            if output_path.is_file() and output_path.stat().st_size > 0:
                return DirectAudioDownloadResult(
                    DirectAudioDownloadStatus.ALREADY_PRESENT,
                    output_path=output_path,
                )
        except OSError:
            pass

        partial_path = output_path.with_name(f".{output_path.name}.gcd-part")
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            offset = partial_path.stat().st_size if partial_path.is_file() else 0
        except OSError:
            return DirectAudioDownloadResult(
                DirectAudioDownloadStatus.FAILED,
                error_code="AUDIO_OUTPUT_FAILED",
                error_message="Не удалось подготовить файл аудио",
            )

        headers = {"Range": f"bytes={offset}-"} if offset else {}
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=60)
        try:
            async with (
                self._session_factory(timeout=timeout) as session,
                session.get(
                    source_url,
                    headers=headers,
                    allow_redirects=True,
                ) as response,
            ):
                response.raise_for_status()
                resume = offset > 0 and response.status == 206
                if not resume:
                    offset = 0
                content_length = getattr(response, "content_length", None)
                total = offset + content_length if isinstance(content_length, int) else None
                written = offset
                last_progress_at = float("-inf")
                last_reported = offset
                mode = "ab" if resume else "wb"
                with partial_path.open(mode) as destination:
                    chunks = response.content.iter_chunked(64 * 1024).__aiter__()
                    while True:
                        try:
                            chunk = await await_or_cancel(anext(chunks), cancellation_event)
                        except StopAsyncIteration:
                            break
                        if is_cancelled and is_cancelled():
                            return DirectAudioDownloadResult(DirectAudioDownloadStatus.CANCELLED)
                        if not chunk:
                            continue
                        destination.write(chunk)
                        written += len(chunk)
                        now = time.monotonic()
                        if on_progress and now - last_progress_at >= _PROGRESS_UPDATE_SECONDS:
                            on_progress(written, total)
                            last_progress_at = now
                            last_reported = written
                if on_progress and last_reported != written:
                    on_progress(written, total)
        except MediaDownloadCancelled:
            return DirectAudioDownloadResult(DirectAudioDownloadStatus.CANCELLED)
        except (TimeoutError, aiohttp.ClientError, OSError) as error:
            return DirectAudioDownloadResult(
                DirectAudioDownloadStatus.FAILED,
                error_code=self._error_code(error),
                error_message="Не удалось скачать аудиофайл",
            )

        if is_cancelled and is_cancelled():
            return DirectAudioDownloadResult(DirectAudioDownloadStatus.CANCELLED)
        try:
            partial_path.replace(output_path)
        except OSError:
            return DirectAudioDownloadResult(
                DirectAudioDownloadStatus.FAILED,
                error_code="AUDIO_FINALIZE_FAILED",
                error_message="Не удалось сохранить аудиофайл",
            )
        return DirectAudioDownloadResult(
            DirectAudioDownloadStatus.DOWNLOADED,
            output_path=output_path,
        )

    @staticmethod
    def _error_code(error: BaseException) -> str:
        if isinstance(error, aiohttp.ClientResponseError) and error.status:
            return f"HTTP_{error.status}"
        if isinstance(error, asyncio.TimeoutError):
            return "NETWORK_TIMEOUT"
        if isinstance(error, aiohttp.ClientConnectorError):
            return "CONNECTION_FAILED"
        return "AUDIO_REQUEST_FAILED"
