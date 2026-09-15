from __future__ import annotations

import ctypes
import os
from pathlib import Path

from playwright.async_api import BrowserContext, Playwright
from playwright.async_api import Error as PlaywrightError

from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.infrastructure.platform.paths import AppPaths


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    kernel32 = windll.kernel32
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return False
    kernel32.CloseHandle(process)
    return True


class _ProfileLease:
    def __init__(self, profile: Path) -> None:
        self._path = profile / ".gcd-profile-owner"
        self._released = False

    def acquire(self) -> None:
        for _ in range(2):
            try:
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    owner = int(self._path.read_text(encoding="ascii").strip())
                except (OSError, ValueError):
                    owner = 0
                if _process_exists(owner):
                    raise ExternalServiceError(
                        "Профиль браузера уже используется другой копией приложения. "
                        "Закройте её и повторите загрузку.",
                        code="BROWSER_PROFILE_BUSY",
                    ) from None
                self._path.unlink(missing_ok=True)
                continue
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(str(os.getpid()))
            return
        raise ExternalServiceError("Не удалось получить доступ к профилю браузера")

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            if self._path.read_text(encoding="ascii").strip() == str(os.getpid()):
                self._path.unlink(missing_ok=True)
        except OSError:
            return


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
        if self._bundled_firefox is None or not self._bundled_firefox.is_file():
            raise ExternalServiceError(
                "Встроенный Firefox отсутствует или повреждён. "
                "Полностью распакуйте архив приложения и повторите загрузку.",
                code="BROWSER_RESOURCES_MISSING",
            )
        lease = _ProfileLease(self._paths.session)
        lease.acquire()
        try:
            context = await playwright.firefox.launch_persistent_context(
                self.profile_path,
                headless=headless,
            )
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
        context.on("close", lambda *_: lease.release())
        return context
