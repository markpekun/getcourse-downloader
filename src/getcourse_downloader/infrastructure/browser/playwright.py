from __future__ import annotations

import asyncio
import os
import time

from playwright.async_api import BrowserContext, Playwright
from playwright.async_api import Error as PlaywrightError

from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.infrastructure.browser.profile_lease import (
    _process_exists as _process_exists,
)
from getcourse_downloader.infrastructure.browser.profile_lease import _ProfileLease
from getcourse_downloader.infrastructure.platform.paths import AppPaths, is_frozen


class PlaywrightBrowserFactory:
    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths
        self._paths.ensure_runtime_directories()
        self._bundled_browsers = (paths.resources / "ms-playwright").resolve()
        self._bundled_firefox = next(
            self._bundled_browsers.glob("firefox-*/firefox/firefox.exe"),
            None,
        )
        if self._bundled_browsers.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self._bundled_browsers)

    @property
    def profile_path(self) -> str:
        return str(self._paths.session)

    async def launch(self, playwright: Playwright, *, headless: bool) -> BrowserContext:
        if is_frozen() and (self._bundled_firefox is None or not self._bundled_firefox.is_file()):
            raise ExternalServiceError(
                "Встроенный Firefox отсутствует или повреждён. "
                "Полностью распакуйте архив приложения и повторите загрузку.",
                code="BROWSER_RESOURCES_MISSING",
            )
        lease = _ProfileLease(self._paths.session)
        try:
            deadline = time.monotonic() + 6
            while True:
                try:
                    lease.acquire()
                    break
                except ExternalServiceError as error:
                    if error.code != "BROWSER_PROFILE_BUSY" or time.monotonic() >= deadline:
                        raise
                    await asyncio.sleep(0.1)
            deadline = time.monotonic() + 6
            while True:
                try:
                    context = await playwright.firefox.launch_persistent_context(
                        self.profile_path,
                        headless=headless,
                    )
                    break
                except PlaywrightError as error:
                    message = str(error).casefold()
                    profile_busy = any(
                        text in message
                        for text in (
                            "already running",
                            "profile is already in use",
                            "profile is in use",
                            "parent.lock",
                            "failed to lock",
                        )
                    )
                    if not profile_busy:
                        raise
                    if time.monotonic() >= deadline:
                        raise ExternalServiceError(
                            "Встроенный Firefox ещё использует профиль. "
                            "Закройте другое окно приложения и повторите загрузку.",
                            code="BROWSER_PROFILE_BUSY",
                            technical_details=str(error),
                        ) from error
                    await asyncio.sleep(0.1)
            context.on("close", lambda *_: lease.release())
            return context
        except PlaywrightError as error:
            lease.release()
            message = str(error).casefold()
            if "already running" in message or "failed to launch" in message:
                raise ExternalServiceError(
                    "Встроенный Firefox не запустился. Возможно, предыдущая загрузка "
                    "завершилась некорректно и браузер ещё закрывается. "
                    "Подождите несколько секунд и повторите.",
                    code="BROWSER_START_FAILED",
                    technical_details=str(error),
                ) from error
            raise ExternalServiceError(
                "Не удалось запустить встроенный Firefox.",
                code="BROWSER_START_FAILED",
                technical_details=str(error),
            ) from error
        except BaseException:
            lease.release()
            raise
