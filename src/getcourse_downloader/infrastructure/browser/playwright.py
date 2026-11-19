from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
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

_FIREFOX_INSTALL_TIMEOUT = 300
_FIREFOX_INSTALL_FAILED = "Не удалось установить Firefox. Проверьте интернет и повторите попытку."


async def _stop_installer(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    # Playwright's Python CLI starts Node and the download can have its own child process.
    if sys.platform == "win32":
        with contextlib.suppress(OSError):
            taskkill = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            await taskkill.wait()
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
    await process.wait()


async def _install_firefox(executable: Path) -> None:
    try:
        installer = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "playwright",
            "install",
            "firefox",
            env={**os.environ, "PLAYWRIGHT_SKIP_BROWSER_GC": "1"},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            start_new_session=sys.platform != "win32",
        )
    except OSError as error:
        raise ExternalServiceError(
            _FIREFOX_INSTALL_FAILED,
            code="BROWSER_INSTALL_FAILED",
            technical_details=f"Could not start Playwright installer: {type(error).__name__}",
        ) from error

    try:
        exit_code = await asyncio.wait_for(installer.wait(), timeout=_FIREFOX_INSTALL_TIMEOUT)
    except asyncio.CancelledError:
        await _stop_installer(installer)
        raise
    except TimeoutError as error:
        await _stop_installer(installer)
        raise ExternalServiceError(
            _FIREFOX_INSTALL_FAILED,
            code="BROWSER_INSTALL_FAILED",
            technical_details="Playwright Firefox installation timed out",
        ) from error

    if exit_code != 0 or not executable.is_file():
        raise ExternalServiceError(
            _FIREFOX_INSTALL_FAILED,
            code="BROWSER_INSTALL_FAILED",
            technical_details=f"Playwright installer exit code: {exit_code}. "
            f"Expected Firefox executable present: {executable.is_file()}.",
        )


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
        executable = Path(playwright.firefox.executable_path)
        if not executable.is_file():
            if is_frozen():
                raise ExternalServiceError(
                    "Встроенный Firefox отсутствует или повреждён. "
                    "Полностью распакуйте архив приложения и повторите загрузку.",
                    code="BROWSER_RESOURCES_MISSING",
                )
            await _install_firefox(executable)
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
