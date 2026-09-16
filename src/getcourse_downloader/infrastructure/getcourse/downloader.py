from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from getcourse_downloader.application.ports.download import EventHandler
from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import DownloadRequest, DownloadSummary, SelectedLesson
from getcourse_downloader.infrastructure.browser.playwright import PlaywrightBrowserFactory
from getcourse_downloader.infrastructure.getcourse.authentication import is_authentication_url
from getcourse_downloader.infrastructure.getcourse.video_signals import (
    VIDEO_PLAYER_SELECTOR,
    extract_hls_urls,
    stream_manifest_kind,
)
from getcourse_downloader.infrastructure.media.hls import (
    HlsDownloader,
    HlsDownloadStatus,
    canonical_media_url,
    has_unsupported_hls_encryption,
    is_hls_master_playlist,
    is_hls_playlist,
    parse_master_variants,
    select_stream_playlist_url,
)
from getcourse_downloader.infrastructure.storage.download_catalog import (
    DownloadedMedia,
    JsonDownloadCatalog,
)
from getcourse_downloader.infrastructure.storage.filenames import (
    collision_safe_component,
    collision_safe_stem,
    safe_lesson_output_stem,
    sanitize_filename,
)

PLAYLIST_WAIT_SECONDS = 30.0
PLAYLIST_QUIET_SECONDS = 1.0


class _AuthenticationExpired(RuntimeError):
    pass


class _LessonStatus(StrEnum):
    DOWNLOADED = "downloaded"
    SKIPPED = "skipped"
    NO_VIDEO = "no_video"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class _LessonResult:
    status: _LessonStatus
    media: tuple[DownloadedMedia, ...] = ()


@dataclass(frozen=True, slots=True)
class _Playlist:
    url: str
    text: str
    referer_url: str = ""


@dataclass(frozen=True, slots=True)
class _ManifestObservation:
    kind: str
    host: str
    status: int | None


class PlaywrightDownloadGateway:
    """Open only selected lessons, detect their streams, and download their media."""

    def __init__(
        self,
        browsers: PlaywrightBrowserFactory,
        hls: HlsDownloader,
        catalog: JsonDownloadCatalog | None = None,
    ) -> None:
        self._browsers = browsers
        self._hls = hls
        self._catalog = catalog
        self._cancelled = threading.Event()
        self._authentication_continued = threading.Event()

    def run(self, request: DownloadRequest, on_event: EventHandler) -> DownloadSummary:
        try:
            return asyncio.run(self._run_async(request, on_event))
        finally:
            self._cancelled.clear()
            self._authentication_continued.clear()

    def continue_authentication(self) -> None:
        self._authentication_continued.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._authentication_continued.set()

    def shutdown(self, timeout: float = 6.0) -> None:
        del timeout
        self.cancel()

    @staticmethod
    def _event(
        item: SelectedLesson,
        event_type: DownloadEventType,
        message: str,
        *,
        stage: str = "lesson",
        level: str = "info",
        quality: str = "",
        error_code: str = "",
        source_host: str = "",
    ) -> DownloadEvent:
        return DownloadEvent(
            event_type,
            message=message,
            stage=stage,
            lesson=item.lesson.title,
            lesson_url=item.lesson.url,
            course_path=item.course_path,
            quality=quality,
            level=level,
            error_code=error_code,
            source_host=source_host,
        )

    @staticmethod
    def _quality_label(media: tuple[DownloadedMedia, ...]) -> str:
        qualities = list(dict.fromkeys(item.quality for item in media if item.quality))
        return ", ".join(qualities)

    async def _existing_result(
        self,
        item: SelectedLesson,
        output_stem: Path,
    ) -> _LessonResult | None:
        if self._catalog:
            catalogued = self._catalog.find(item.lesson.url, output_stem)
            if catalogued:
                return _LessonResult(_LessonStatus.SKIPPED, catalogued)

        direct = output_stem.parent / f"{output_stem.name}.mp4"
        try:
            exists = direct.is_file() and direct.stat().st_size > 0
        except OSError:
            exists = False
        if not exists:
            return None
        quality = await self._hls.probe_quality(direct)
        media = (DownloadedMedia(direct, quality),)
        if self._catalog:
            self._catalog.save(item.lesson.url, output_stem, media)
        return _LessonResult(_LessonStatus.SKIPPED, media)

    @staticmethod
    def _output_exists(stem: Path) -> bool:
        direct = stem.parent / f"{stem.name}.mp4"
        return direct.is_file() and direct.stat().st_size > 0

    @staticmethod
    def _output_stems(request: DownloadRequest) -> list[Path]:
        stems: list[Path] = []
        for item in request.lessons:
            stem = safe_lesson_output_stem(
                request.save_path,
                item.course_path,
                item.lesson.title,
            )
            parts = list(stem.relative_to(request.save_path).parts)
            for depth, raw_component in enumerate(item.course_path):
                if sanitize_filename(raw_component, fallback="course") != raw_component:
                    parts[depth] = collision_safe_component(
                        parts[depth],
                        "\x1f".join(item.course_path[: depth + 1]),
                    )
            stems.append(
                collision_safe_stem(
                    request.save_path.joinpath(*parts),
                    item.lesson.url,
                )
            )
        return stems

    async def _run_async(self, request: DownloadRequest, emit: EventHandler) -> DownloadSummary:
        downloaded = 0
        already_present = 0
        no_video = 0
        failed: list[str] = []
        cancelled = 0
        output_stems = self._output_stems(request)

        async with async_playwright() as playwright:
            pending: list[int] = []
            for index, item in enumerate(request.lessons):
                if self._cancelled.is_set():
                    cancelled = len(request.lessons) - index
                    break
                existing = await self._existing_result(item, output_stems[index])
                if existing is None:
                    pending.append(index)
                    continue
                already_present += 1
                emit(
                    self._event(
                        item,
                        DownloadEventType.LESSON_SKIPPED,
                        f"Уже скачано: {item.lesson.title}",
                        level="success",
                        quality=self._quality_label(existing.media),
                    )
                )

            browser = None
            try:
                if pending and not self._cancelled.is_set():
                    browser = await self._launch_authenticated_context(
                        playwright,
                        request.lessons[pending[0]].lesson.url,
                        emit,
                    )
                for pending_position, index in enumerate(pending):
                    item = request.lessons[index]
                    if self._cancelled.is_set() or browser is None:
                        cancelled += len(pending) - pending_position
                        break

                    emit(self._event(item, DownloadEventType.LESSON_STARTED, "Проверяю урок"))
                    result = _LessonResult(_LessonStatus.FAILED)
                    for authentication_attempt in range(2):
                        try:
                            result = await self._download_lesson(
                                browser,
                                item,
                                output_stems[index],
                                request.quality.value,
                                emit,
                            )
                            break
                        except _AuthenticationExpired:
                            await browser.close()
                            if authentication_attempt:
                                break
                            browser = await self._launch_authenticated_context(
                                playwright,
                                item.lesson.url,
                                emit,
                            )
                            if browser is None:
                                result = _LessonResult(_LessonStatus.CANCELLED)
                                break
                        except Exception as error:
                            if self._cancelled.is_set():
                                result = _LessonResult(_LessonStatus.CANCELLED)
                                break
                            emit(
                                self._event(
                                    item,
                                    DownloadEventType.ERROR,
                                    "Ошибка обработки урока: "
                                    + (
                                        "не удалось прочитать или сохранить файлы"
                                        if isinstance(error, OSError)
                                        else "не удалось открыть страницу или обработать видео"
                                    ),
                                    level="error",
                                    error_code="LESSON_PROCESSING_FAILED",
                                )
                            )
                            break

                    if result.status is _LessonStatus.DOWNLOADED:
                        downloaded += 1
                        emit(
                            self._event(
                                item,
                                DownloadEventType.LESSON_COMPLETED,
                                f"Готово: {item.lesson.title}",
                                level="success",
                                quality=self._quality_label(result.media),
                            )
                        )
                        if self._catalog:
                            self._catalog.save(item.lesson.url, output_stems[index], result.media)
                    elif result.status is _LessonStatus.SKIPPED:
                        already_present += 1
                        emit(
                            self._event(
                                item,
                                DownloadEventType.LESSON_SKIPPED,
                                f"Уже скачано: {item.lesson.title}",
                                level="success",
                                quality=self._quality_label(result.media),
                            )
                        )
                        if self._catalog:
                            self._catalog.save(item.lesson.url, output_stems[index], result.media)
                    elif result.status is _LessonStatus.NO_VIDEO:
                        no_video += 1
                        emit(
                            self._event(
                                item,
                                DownloadEventType.LESSON_NO_VIDEO,
                                "На странице урока не найден поддерживаемый видеоплеер: "
                                f"{item.lesson.title}",
                                stage="player",
                                level="warning",
                                error_code="VIDEO_NOT_FOUND",
                            )
                        )
                    elif result.status is _LessonStatus.CANCELLED:
                        cancelled += len(pending) - pending_position
                        break
                    else:
                        failed.append(item.lesson.title)
                        emit(
                            self._event(
                                item,
                                DownloadEventType.LESSON_FAILED,
                                f"Не удалось скачать: {item.lesson.title}",
                                level="error",
                            )
                        )
            finally:
                if browser is not None:
                    with contextlib.suppress(PlaywrightError):
                        await browser.close()

        summary = DownloadSummary(
            total=len(request.lessons),
            downloaded=downloaded,
            already_present=already_present,
            no_video=no_video,
            failed=tuple(failed),
            cancelled=cancelled,
        )
        emit(
            DownloadEvent(
                DownloadEventType.SUMMARY,
                message=(
                    f"Загружено: {summary.downloaded}; уже было: {summary.already_present}; "
                    f"без видео: {summary.no_video}; ошибок: {len(summary.failed)}"
                ),
                stage="summary",
                current=summary.processed,
                total=summary.total,
                downloaded=summary.downloaded,
                already_present=summary.already_present,
                no_video=summary.no_video,
                failed_count=len(summary.failed),
                cancelled=summary.cancelled,
                level="success" if summary.successful else "warning",
            )
        )
        return summary

    async def _launch_authenticated_context(
        self,
        playwright,
        url: str,
        emit: EventHandler,
    ):
        browser = await self._browsers.launch(playwright, headless=True)
        try:
            page = browser.pages[0] if browser.pages else await browser.new_page()
            opened = await self._open_page(
                page,
                url,
                "страницу для проверки авторизации",
                emit,
            )
            if not opened:
                await browser.close()
                return None
            needs_auth = await self._authentication_required(page)
        except Exception:
            with contextlib.suppress(PlaywrightError):
                await browser.close()
            raise

        if not needs_auth:
            emit(
                DownloadEvent(
                    DownloadEventType.AUTHENTICATED,
                    message="Авторизация активна",
                    stage="authentication",
                )
            )
            return browser

        await browser.close()

        browser = await self._browsers.launch(playwright, headless=False)
        try:
            page = browser.pages[0] if browser.pages else await browser.new_page()
            if not await self._open_page(page, url, "страницу входа", emit):
                return None
            while True:
                self._authentication_continued.clear()
                emit(
                    DownloadEvent(
                        DownloadEventType.AUTH_REQUIRED,
                        message="Войдите в GetCourse и нажмите «Продолжить»",
                        stage="authentication",
                    )
                )
                await asyncio.to_thread(self._authentication_continued.wait)
                if self._cancelled.is_set():
                    return None
                if not await self._open_page(
                    page,
                    url,
                    "страницу для проверки авторизации",
                    emit,
                ):
                    return None
                if not await self._authentication_required(page):
                    break
        finally:
            await browser.close()
        if self._cancelled.is_set():
            return None
        emit(
            DownloadEvent(
                DownloadEventType.AUTHENTICATED,
                message="Авторизация выполнена",
                stage="authentication",
                level="success",
            )
        )
        return await self._browsers.launch(playwright, headless=True)

    async def _download_lesson(
        self,
        browser,
        item: SelectedLesson,
        output_stem: Path,
        quality: str,
        emit: EventHandler,
    ) -> _LessonResult:
        existing = await self._existing_result(item, output_stem)
        if existing is not None:
            return existing

        page = await browser.new_page()
        playlists: dict[str, _Playlist] = {}
        observed_manifests: list[_ManifestObservation] = []
        observed_manifest_keys: set[tuple[str, str, int | None]] = set()
        response_tasks: set[asyncio.Task[None]] = set()
        last_playlist_at = 0.0

        async def on_response(response) -> None:
            nonlocal last_playlist_at
            url = response.url
            response_headers: dict[str, str] = {}
            with contextlib.suppress(Exception):
                response_headers = await response.all_headers()
            kind = stream_manifest_kind(url, response_headers.get("content-type", ""))
            host = urlsplit(url).hostname or ""
            raw_status = getattr(response, "status", None)
            status = raw_status if isinstance(raw_status, int) else None
            if kind:
                key = (kind, host, status)
                if key not in observed_manifest_keys:
                    observed_manifest_keys.add(key)
                    observed_manifests.append(_ManifestObservation(kind, host, status))
            if kind != "hls" or url in playlists:
                return
            try:
                text = await asyncio.wait_for(response.text(), timeout=15)
            except Exception:
                return
            if is_hls_master_playlist(text) or is_hls_playlist(text):
                referer_url = ""
                with contextlib.suppress(Exception):
                    headers = await response.request.all_headers()
                    referer_url = headers.get("referer", "")
                playlists[url] = _Playlist(url, text, referer_url)
                last_playlist_at = time.monotonic()

        def schedule_response(response) -> None:
            task = asyncio.create_task(on_response(response))
            response_tasks.add(task)
            task.add_done_callback(response_tasks.discard)

        page.on("response", schedule_response)

        try:
            if not await self._open_page(
                page,
                item.lesson.url,
                "страницу урока",
                emit,
                item=item,
            ):
                return _LessonResult(_LessonStatus.CANCELLED)
            if await self._authentication_required(page):
                raise _AuthenticationExpired

            player_present = await self._has_supported_player(page)
            embedded = await self._read_embedded_playlists(page)
            for playlist in embedded:
                playlists.setdefault(playlist.url, playlist)
            if embedded:
                last_playlist_at = time.monotonic()
            started_at = time.monotonic()
            while time.monotonic() - started_at < PLAYLIST_WAIT_SECONDS:
                if self._cancelled.is_set():
                    return _LessonResult(_LessonStatus.CANCELLED)
                if playlists and time.monotonic() - last_playlist_at >= PLAYLIST_QUIET_SECONDS:
                    break
                await asyncio.sleep(0.25)

            if response_tasks:
                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)

            if not playlists:
                for playlist in await self._read_embedded_playlists(page):
                    playlists.setdefault(playlist.url, playlist)

            if not playlists:
                player_present = player_present or await self._has_supported_player(page)
                if player_present:
                    dash = next(
                        (
                            observation
                            for observation in observed_manifests
                            if observation.kind == "dash"
                        ),
                        None,
                    )
                    if dash is not None:
                        status_suffix = f" (HTTP {dash.status})" if dash.status else ""
                        emit(
                            self._event(
                                item,
                                DownloadEventType.ERROR,
                                "Обнаружен DASH manifest на "
                                f"{dash.host or 'сервере видео'}{status_suffix}; "
                                "поддерживается только HLS",
                                stage="playlist",
                                level="error",
                                error_code="DASH_STREAM_UNSUPPORTED",
                                source_host=dash.host,
                            )
                        )
                        return _LessonResult(_LessonStatus.FAILED)
                    media_api = next(
                        (
                            observation
                            for observation in observed_manifests
                            if observation.kind == "media_api"
                        ),
                        None,
                    )
                    if media_api is not None:
                        status_suffix = f" (HTTP {media_api.status})" if media_api.status else ""
                        emit(
                            self._event(
                                item,
                                DownloadEventType.ERROR,
                                "Плеер найден, но поддерживаемый HLS поток не получен. "
                                "Обнаружен Media API на "
                                f"{media_api.host or 'сервере видео'}{status_suffix}",
                                stage="playlist",
                                level="error",
                                error_code="PLAYLIST_NOT_OBSERVED",
                                source_host=media_api.host,
                            )
                        )
                        return _LessonResult(_LessonStatus.FAILED)
                    emit(
                        self._event(
                            item,
                            DownloadEventType.ERROR,
                            "Плеер найден, но видеопоток не получен",
                            stage="playlist",
                            level="error",
                            error_code="PLAYLIST_NOT_OBSERVED",
                        )
                    )
                    return _LessonResult(_LessonStatus.FAILED)
                return _LessonResult(_LessonStatus.NO_VIDEO)

            encrypted_master = next(
                (
                    playlist
                    for playlist in playlists.values()
                    if is_hls_master_playlist(playlist.text)
                    and has_unsupported_hls_encryption(playlist.text)
                ),
                None,
            )
            if encrypted_master is not None:
                encryption_method = has_unsupported_hls_encryption(encrypted_master.text)
                emit(
                    self._event(
                        item,
                        DownloadEventType.ERROR,
                        f"Плейлист использует защищённое шифрование {encryption_method}",
                        stage="playlist",
                        level="error",
                        error_code="ENCRYPTED_PLAYLIST_UNSUPPORTED",
                    )
                )
                return _LessonResult(_LessonStatus.FAILED)

            selected = self._select_playlists(playlists.values(), quality)

            if not selected:
                emit(
                    self._event(
                        item,
                        DownloadEventType.ERROR,
                        "Не удалось подобрать качество видео",
                        stage="quality",
                        level="error",
                    )
                )
                return _LessonResult(_LessonStatus.FAILED)

            download_results = []
            for video_index, playlist in enumerate(selected, start=1):
                output = output_stem if len(selected) == 1 else output_stem / f"video_{video_index}"
                result = await self._hls.download(
                    playlist.url,
                    output,
                    item.lesson.title,
                    emit,
                    lesson_url=item.lesson.url,
                    referer_url=playlist.referer_url,
                    course_path=item.course_path,
                    requested_quality=quality,
                    video_index=video_index,
                    video_total=len(selected),
                    is_cancelled=self._cancelled.is_set,
                )
                download_results.append(result)
                if result.status is HlsDownloadStatus.CANCELLED or self._cancelled.is_set():
                    return _LessonResult(_LessonStatus.CANCELLED)

            statuses = [result.status for result in download_results]
            if any(status is HlsDownloadStatus.FAILED for status in statuses):
                return _LessonResult(_LessonStatus.FAILED)
            media = tuple(
                DownloadedMedia(result.output_path, result.quality)
                for result in download_results
                if result.output_path is not None
            )
            if all(status is HlsDownloadStatus.ALREADY_PRESENT for status in statuses):
                return _LessonResult(_LessonStatus.SKIPPED, media)
            return _LessonResult(_LessonStatus.DOWNLOADED, media)
        finally:
            for task in response_tasks:
                task.cancel()
            if response_tasks:
                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
            with contextlib.suppress(PlaywrightError):
                await page.close()

    @staticmethod
    def _select_playlist_urls(playlists: Iterable[_Playlist], quality: str) -> list[str]:
        return [
            playlist.url
            for playlist in PlaywrightDownloadGateway._select_playlists(playlists, quality)
        ]

    @staticmethod
    def _select_playlists(playlists: Iterable[_Playlist], quality: str) -> list[_Playlist]:
        candidates = tuple(playlists)
        selected: dict[str, _Playlist] = {}
        master_variant_keys: set[str] = set()

        for playlist in candidates:
            if not is_hls_master_playlist(playlist.text):
                continue
            master_variant_keys.update(
                canonical_media_url(variant.url)
                for variant in parse_master_variants(playlist.text, playlist.url)
            )
            selected_url = select_stream_playlist_url(playlist.text, playlist.url, quality)
            if selected_url:
                key = canonical_media_url(selected_url)
                selected.setdefault(key, _Playlist(selected_url, "", playlist.referer_url))

        for playlist in candidates:
            if is_hls_master_playlist(playlist.text):
                continue
            key = canonical_media_url(playlist.url)
            if key in master_variant_keys:
                continue
            selected_url = select_stream_playlist_url(playlist.text, playlist.url, quality)
            if selected_url:
                selected.setdefault(
                    canonical_media_url(selected_url),
                    _Playlist(selected_url, "", playlist.referer_url),
                )
        return [selected[key] for key in sorted(selected)]

    @staticmethod
    async def _read_embedded_playlists(page: Any) -> list[_Playlist]:
        frames: list[Any] = []
        with contextlib.suppress(Exception):
            frames.extend(page.frames)
        if not frames:
            frames.append(page)

        playlists: list[_Playlist] = []
        seen: set[str] = set()
        for frame in frames:
            try:
                frame_url = frame.url or page.url
                content = await frame.content()
            except Exception:
                continue
            for candidate in extract_hls_urls(content, frame_url):
                if candidate in seen:
                    continue
                seen.add(candidate)
                try:
                    result = await frame.evaluate(
                        """async (url) => {
                            const response = await fetch(url, {credentials: "include"});
                            return {url: response.url || url, text: await response.text()};
                        }""",
                        candidate,
                    )
                except Exception:
                    continue
                if not isinstance(result, dict):
                    continue
                url = result.get("url")
                text = result.get("text")
                if not isinstance(url, str) or not isinstance(text, str):
                    continue
                if is_hls_master_playlist(text) or is_hls_playlist(text):
                    playlists.append(_Playlist(url, text, frame_url))
        return playlists

    @staticmethod
    async def _has_supported_player(page: Any) -> bool:
        with contextlib.suppress(PlaywrightError):
            return await page.query_selector(VIDEO_PLAYER_SELECTOR) is not None
        return False

    @staticmethod
    async def _authentication_required(page: Any) -> bool:
        with contextlib.suppress(PlaywrightError):
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        await page.wait_for_timeout(500)
        return is_authentication_url(page.url)

    async def _goto_or_cancel(self, page: Any, url: str) -> bool:
        navigation = asyncio.create_task(page.goto(url, wait_until="commit", timeout=60_000))
        try:
            while not navigation.done():
                if self._cancelled.is_set():
                    navigation.cancel()
                    with contextlib.suppress(asyncio.CancelledError, PlaywrightError):
                        await navigation
                    return False
                await asyncio.sleep(0.1)
            await navigation
            return True
        except asyncio.CancelledError:
            navigation.cancel()
            with contextlib.suppress(asyncio.CancelledError, PlaywrightError):
                await navigation
            raise

    async def _open_page(
        self,
        page: Any,
        url: str,
        purpose: str,
        emit: EventHandler,
        attempts: int = 3,
        *,
        item: SelectedLesson | None = None,
    ) -> bool:
        last_error: PlaywrightError | None = None
        for attempt in range(1, attempts + 1):
            try:
                return await self._goto_or_cancel(page, url)
            except (PlaywrightTimeoutError, PlaywrightError) as error:
                last_error = error
                if self._cancelled.is_set():
                    return False
                if attempt < attempts:
                    event = DownloadEvent(
                        DownloadEventType.LOG,
                        message=f"Не удалось открыть {purpose}. Повтор {attempt}/{attempts - 1}",
                        stage="network",
                        level="warning",
                    )
                    if item:
                        event = self._event(
                            item,
                            DownloadEventType.LOG,
                            event.message,
                            stage="network",
                            level="warning",
                        )
                    emit(event)
                    for _ in range(attempt * 30):
                        if self._cancelled.is_set():
                            return False
                        await asyncio.sleep(0.1)
        raise RuntimeError(
            "Не удалось открыть страницу: сайт не отвечает. Проверьте интернет и повторите."
        ) from last_error
