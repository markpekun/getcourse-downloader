import asyncio
import os

import pytest

from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.infrastructure.browser.playwright import PlaywrightBrowserFactory
from getcourse_downloader.infrastructure.platform.paths import AppPaths


def _paths(tmp_path) -> AppPaths:
    return AppPaths(
        data=tmp_path / "data",
        session=tmp_path / "browser-profile",
        resources=tmp_path / "resources",
    )


def test_bundled_firefox_overrides_an_existing_playwright_browser_path(monkeypatch, tmp_path):
    paths = _paths(tmp_path)
    bundled = paths.resources / "ms-playwright"
    (bundled / "firefox-1538" / "firefox").mkdir(parents=True)
    (bundled / "firefox-1538" / "firefox" / "firefox.exe").write_bytes(b"firefox")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "C:/stale-playwright-cache")

    PlaywrightBrowserFactory(paths)

    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(bundled.resolve())


def test_launch_fails_clearly_when_bundled_firefox_is_missing(tmp_path):
    paths = _paths(tmp_path)
    factory = PlaywrightBrowserFactory(paths)

    class Firefox:
        async def launch_persistent_context(self, *_args, **_kwargs):
            raise AssertionError("An installed browser must not be used as a fallback")

    class Playwright:
        firefox = Firefox()

    with pytest.raises(ExternalServiceError, match="Встроенный Firefox отсутствует") as captured:
        asyncio.run(factory.launch(Playwright(), headless=True))  # type: ignore[arg-type]

    assert getattr(captured.value, "code", "") == "BROWSER_RESOURCES_MISSING"


def test_browser_launch_error_has_stable_diagnostic_code(tmp_path):
    paths = _paths(tmp_path)
    bundled = paths.resources / "ms-playwright" / "firefox-1538" / "firefox"
    bundled.mkdir(parents=True)
    (bundled / "firefox.exe").write_bytes(b"firefox")
    factory = PlaywrightBrowserFactory(paths)

    from playwright.async_api import Error as PlaywrightError

    class Firefox:
        async def launch_persistent_context(self, *_args, **_kwargs):
            raise PlaywrightError("Failed to launch browser process")

    class Playwright:
        firefox = Firefox()

    with pytest.raises(ExternalServiceError) as captured:
        asyncio.run(factory.launch(Playwright(), headless=True))  # type: ignore[arg-type]

    assert getattr(captured.value, "code", "") == "BROWSER_START_FAILED"
