from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

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
        if is_frozen():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(self._bundled_browsers)

    @property
    def profile_path(self) -> str:
        return str(self._paths.session)

    async def launch(self, playwright: Playwright, *, headless: bool) -> BrowserContext:
        if is_frozen() and not Path(playwright.firefox.executable_path).is_file():
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
            raise ExternalServiceError(
                "Не удалось запустить встроенный Firefox.",
                code="BROWSER_START_FAILED",
                technical_details=str(error),
            ) from error
        except BaseException:
            lease.release()
            raise
