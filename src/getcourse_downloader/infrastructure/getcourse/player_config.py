from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from playwright.async_api import Error as PlaywrightError

from getcourse_downloader.infrastructure.getcourse.video_signals import frame_lineage_ids
from getcourse_downloader.infrastructure.media.hls import is_hls_master_playlist

PLAYER_PREPARATION_SECONDS = 120.0
PLAYER_POLL_SECONDS = 2.0
PLAYER_REQUEST_SECONDS = 15.0

_READ_CONFIG = """() => {
    const c = window.configs;
    if (!c || typeof c !== 'object') return null;
    return {
        isVideoReady: c.isVideoReady,
        isProtectReady: c.isProtectReady,
        masterPlaylistUrl: c.masterPlaylistUrl,
        masterPlaylistDomains: c.masterPlaylistDomains,
        createPersonalVideoUrl: c.createPersonalVideoUrl,
        checkPersonalVideoUrl: c.checkPersonalVideoUrl
    };
}"""

_FETCH = """async ({url, method, timeoutMs}) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
        const response = await fetch(url, {
            method, credentials: 'include', signal: controller.signal
        });
        return {status: response.status, url: response.url, text: await response.text()};
    } finally {
        clearTimeout(timer);
    }
}"""


@dataclass(frozen=True, slots=True)
class PlayerMaster:
    url: str
    text: str
    referer_url: str
    frame_ids: tuple[int, ...]


class PlayerConfigError(RuntimeError):
    """Only safe diagnostic fields escape the iframe; never include response bodies/URLs."""

    def __init__(self, code: str, message: str, frame_id: int, host: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.frame_id = frame_id
        self.host = host


def _http_url(value: Any, base: str) -> str:
    if not isinstance(value, str) or not value:
        return ""
    try:
        url = urljoin(base, value)
        parts = urlsplit(url)
        if parts.scheme in {"http", "https"} and parts.hostname and not parts.username:
            return url
    except ValueError:
        pass
    return ""


class GetCoursePlayerResolver:
    """Resolve known sign-player configs without clicks, playback or minified JS internals."""

    def __init__(
        self,
        is_cancelled: Callable[[], bool],
        on_preparing: Callable[[], None],
    ) -> None:
        self._is_cancelled = is_cancelled
        self._on_preparing = on_preparing
        self._tasks: dict[int, asyncio.Task[PlayerMaster | None]] = {}
        self._finished: set[int] = set()
        self._requests = asyncio.Semaphore(4)
        self.failures: list[PlayerConfigError] = []

    @property
    def pending(self) -> bool:
        return bool(self._tasks)

    def scan(self, frames: Iterable[Any]) -> None:
        for frame in frames:
            frame_id = id(frame)
            if frame_id in self._tasks or frame_id in self._finished:
                continue
            if "/sign-player/" not in urlsplit(frame.url).path or not hasattr(frame, "evaluate"):
                continue
            self._tasks[frame_id] = asyncio.create_task(self._resolve(frame))

    def collect(self) -> list[PlayerMaster]:
        masters = []
        for frame_id, task in tuple(self._tasks.items()):
            if not task.done():
                continue
            del self._tasks[frame_id]
            if task.cancelled() and self._is_cancelled():
                continue
            try:
                master = task.result()
            except PlayerConfigError as error:
                self._finished.add(frame_id)
                self.failures.append(error)
            else:
                # An absent/unknown config can appear later; don't mark it as finished.
                if master is not None:
                    self._finished.add(frame_id)
                    masters.append(master)
        return masters

    async def close(self) -> None:
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    def _check_cancelled(self) -> None:
        if self._is_cancelled():
            raise asyncio.CancelledError

    async def _resolve(self, frame: Any) -> PlayerMaster | None:
        self._check_cancelled()
        try:
            config = await asyncio.wait_for(frame.evaluate(_READ_CONFIG), PLAYER_REQUEST_SECONDS)
        except (PlaywrightError, TimeoutError):
            # Navigation may still be replacing the document. Keep the passive path and retry.
            return None
        if not isinstance(config, dict):
            return None
        master_url = _http_url(config.get("masterPlaylistUrl"), frame.url)
        if not master_url or not isinstance(config.get("isProtectReady"), bool):
            return None
        try:
            async with asyncio.timeout(PLAYER_PREPARATION_SECONDS):
                if config.get("isVideoReady") is False:
                    raise self._error(
                        frame, "PLAYER_VIDEO_NOT_READY", "Сайт ещё не подготовил исходное видео"
                    )
                if not config["isProtectReady"]:
                    await self._prepare(frame, config)
                urls = [master_url]
                alternatives = config.get("masterPlaylistDomains")
                if isinstance(alternatives, list):
                    for value in alternatives:
                        url = _http_url(value, frame.url)
                        if url and url not in urls:
                            urls.append(url)
                return await self._master(frame, urls[:3])
        except TimeoutError:
            raise self._error(
                frame,
                "PLAYER_PREPARATION_TIMEOUT",
                "Истекло время подготовки персонального видео",
            ) from None

    @staticmethod
    def _error(frame: Any, code: str, message: str, url: str = "") -> PlayerConfigError:
        return PlayerConfigError(
            code, message, id(frame), urlsplit(url or frame.url).hostname or ""
        )

    async def _request(self, frame: Any, url: str, method: str) -> dict[str, Any]:
        self._check_cancelled()
        try:
            async with self._requests:
                self._check_cancelled()
                response = await asyncio.wait_for(
                    frame.evaluate(
                        _FETCH,
                        {
                            "url": url,
                            "method": method,
                            "timeoutMs": int(PLAYER_REQUEST_SECONDS * 1000),
                        },
                    ),
                    PLAYER_REQUEST_SECONDS + 1,
                )
        except (PlaywrightError, TimeoutError):
            raise self._error(
                frame, "PLAYER_REQUEST_FAILED", "Не удалось выполнить запрос плеера", url
            ) from None
        self._check_cancelled()
        if not isinstance(response, dict) or not isinstance(response.get("status"), int):
            raise self._error(
                frame, "PLAYER_INVALID_RESPONSE", "Плеер вернул некорректный ответ", url
            )
        status = response["status"]
        if not 200 <= status < 300:
            code = "PLAYER_ACCESS_DENIED" if status in {401, 403} else "PLAYER_HTTP_ERROR"
            raise self._error(frame, code, f"Запрос плеера завершился с HTTP {status}", url)
        return response

    async def _ready(self, frame: Any, url: str) -> bool:
        response = await self._request(frame, url, "POST")
        try:
            data = json.loads(response.get("text", ""))
        except (TypeError, ValueError):
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("ready"), bool):
            raise self._error(
                frame,
                "PLAYER_INVALID_RESPONSE",
                "Сайт вернул некорректное состояние готовности видео",
                url,
            )
        return bool(data["ready"])

    async def _prepare(self, frame: Any, config: dict[str, Any]) -> None:
        ready_url = _http_url(config.get("checkPersonalVideoUrl"), frame.url)
        create_url = _http_url(config.get("createPersonalVideoUrl"), frame.url)
        origin = urlsplit(frame.url)[:2]
        if not all(url and urlsplit(url)[:2] == origin for url in (ready_url, create_url)):
            raise self._error(
                frame, "PLAYER_CONFIG_UNSUPPORTED", "В конфигурации плеера нет адресов подготовки"
            )
        if await self._ready(frame, ready_url):
            return
        self._on_preparing()
        await self._request(frame, create_url, "POST")
        while True:
            self._check_cancelled()
            await asyncio.sleep(PLAYER_POLL_SECONDS)
            if await self._ready(frame, ready_url):
                return

    async def _master(self, frame: Any, urls: list[str]) -> PlayerMaster:
        last_error: PlayerConfigError | None = None
        for url in urls:
            try:
                response = await self._request(frame, url, "GET")
                text = response.get("text")
                if not isinstance(text, str) or not is_hls_master_playlist(text):
                    raise self._error(
                        frame,
                        "PLAYER_MASTER_INVALID",
                        "Плеер не вернул корректный master-плейлист",
                        url,
                    )
                return PlayerMaster(
                    _http_url(response.get("url"), url) or url,
                    text,
                    frame.url,
                    frame_lineage_ids(frame),
                )
            except PlayerConfigError as error:
                if error.code == "PLAYER_ACCESS_DENIED":
                    raise
                last_error = error
        assert last_error is not None
        raise last_error
