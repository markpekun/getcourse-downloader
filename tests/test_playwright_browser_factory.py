import asyncio
import os
import sys
from collections.abc import Iterator

import pytest

from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.infrastructure.browser.playwright import (
    PlaywrightBrowserFactory,
    _ProfileLease,
)
from getcourse_downloader.infrastructure.platform.paths import AppPaths

_TEST_FIREFOX_REVISION = "firefox-9001"
_OTHER_TEST_FIREFOX_REVISION = "firefox-9002"


@pytest.fixture(autouse=True)
def _restore_browser_path() -> Iterator[None]:
    original = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)
        else:
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = original


def _paths(tmp_path) -> AppPaths:
    return AppPaths(
        data=tmp_path / "data",
        session=tmp_path / "browser-profile",
        resources=tmp_path / "resources",
    )


@pytest.fixture
def installed_firefox(tmp_path):
    executable = tmp_path / "test-browser-cache" / _TEST_FIREFOX_REVISION / "firefox.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake executable")
    return str(executable)


@pytest.mark.parametrize("frozen", [False, True])
def test_browser_path_uses_bundle_only_in_frozen_app(monkeypatch, tmp_path, frozen):
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    paths = _paths(tmp_path)
    bundled = paths.resources / "ms-playwright"
    executable = bundled / _TEST_FIREFOX_REVISION / "firefox" / "firefox.exe"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"fake executable")
    test_cache = str(tmp_path / "test-browser-cache")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", test_cache)

    PlaywrightBrowserFactory(paths)

    expected = str(bundled.resolve()) if frozen else test_cache
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == expected


@pytest.mark.parametrize("other_revision", [False, True])
def test_launch_fails_clearly_when_bundled_firefox_is_missing(
    monkeypatch, tmp_path, other_revision
):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    paths = _paths(tmp_path)
    if other_revision:
        stale = paths.resources / "ms-playwright" / _OTHER_TEST_FIREFOX_REVISION / "firefox"
        stale.mkdir(parents=True)
        (stale / "firefox.exe").write_bytes(b"unrelated revision")
    factory = PlaywrightBrowserFactory(paths)

    class Firefox:
        executable_path = str(
            paths.resources / "ms-playwright" / _TEST_FIREFOX_REVISION / "firefox" / "firefox.exe"
        )

        async def launch_persistent_context(self, *_args, **_kwargs):
            raise AssertionError("An installed browser must not be used as a fallback")

    class Playwright:
        firefox = Firefox()

    with pytest.raises(ExternalServiceError, match="Встроенный Firefox отсутствует") as captured:
        asyncio.run(factory.launch(Playwright(), headless=True))  # type: ignore[arg-type]

    assert getattr(captured.value, "code", "") == "BROWSER_RESOURCES_MISSING"


def test_browser_launch_error_has_stable_diagnostic_code(tmp_path, installed_firefox):
    paths = _paths(tmp_path)
    factory = PlaywrightBrowserFactory(paths)

    from playwright.async_api import Error as PlaywrightError

    class Firefox:
        executable_path = installed_firefox

        async def launch_persistent_context(self, *_args, **_kwargs):
            raise PlaywrightError("Failed to launch browser process")

    class Playwright:
        firefox = Firefox()

    with pytest.raises(ExternalServiceError) as captured:
        asyncio.run(factory.launch(Playwright(), headless=True))  # type: ignore[arg-type]

    assert getattr(captured.value, "code", "") == "BROWSER_START_FAILED"


@pytest.mark.parametrize("state", ["cached", "missing", "install-failed"])
def test_source_launch_prepares_the_required_playwright_firefox(tmp_path, monkeypatch, state):
    paths = _paths(tmp_path)
    factory = PlaywrightBrowserFactory(paths)
    executable = tmp_path / "test-browser-cache" / _TEST_FIREFOX_REVISION / "firefox.exe"
    if state == "cached":
        executable.parent.mkdir(parents=True)
        executable.write_bytes(b"fake executable")
    install_calls = []

    class Installer:
        async def wait(self):
            if state == "install-failed":
                return 1
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"fake executable")
            return 0

    async def start_installer(*args, **_kwargs):
        install_calls.append(args)
        return Installer()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", start_installer)

    class Context:
        def on(self, event, callback):
            assert event == "close"
            self.close_callback = callback

    context = Context()

    class Firefox:
        executable_path = str(executable)

        async def launch_persistent_context(self, profile, *, headless):
            assert executable.is_file()
            assert profile == str(paths.session)
            assert headless is True
            return context

    class Playwright:
        firefox = Firefox()

    if state == "install-failed":
        with pytest.raises(ExternalServiceError) as captured:
            asyncio.run(factory.launch(Playwright(), headless=True))
        assert captured.value.code == "BROWSER_INSTALL_FAILED"
    else:
        # The second launch has a cached Firefox even after the first installs it.
        for _ in range(2):
            assert asyncio.run(factory.launch(Playwright(), headless=True)) is context
            context.close_callback()
    expected_command = (sys.executable, "-m", "playwright", "install", "firefox")
    assert install_calls == ([] if state == "cached" else [expected_command])
    assert not (paths.session / ".gcd-profile-owner").exists()


def test_cancelled_browser_launch_releases_profile(tmp_path, installed_firefox):
    paths = _paths(tmp_path)

    class Firefox:
        executable_path = installed_firefox

        async def launch_persistent_context(self, *_args, **_kwargs):
            raise asyncio.CancelledError

    class Playwright:
        firefox = Firefox()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(PlaywrightBrowserFactory(paths).launch(Playwright(), headless=True))
    assert not (paths.session / ".gcd-profile-owner").exists()


def test_launch_waits_for_previous_profile_owner_to_close(tmp_path, installed_firefox):
    paths = _paths(tmp_path)
    factory = PlaywrightBrowserFactory(paths)
    previous = _ProfileLease(paths.session)
    previous.acquire()

    class Context:
        def on(self, event, callback):
            self.close_callback = callback

    context = Context()

    class Firefox:
        executable_path = installed_firefox

        async def launch_persistent_context(self, *_args, **_kwargs):
            return context

    class Playwright:
        firefox = Firefox()

    async def run():
        async def close_previous():
            await asyncio.sleep(0.05)
            previous.release()

        closing = asyncio.create_task(close_previous())
        try:
            assert await factory.launch(Playwright(), headless=True) is context
            context.close_callback()
        finally:
            await closing
            previous.release()

    asyncio.run(run())


def test_launch_retries_a_profile_that_firefox_is_still_closing(tmp_path, installed_firefox):
    paths = _paths(tmp_path)
    factory = PlaywrightBrowserFactory(paths)

    from playwright.async_api import Error as PlaywrightError

    class Context:
        def on(self, event, callback):
            self.close_callback = callback

    context = Context()

    class Firefox:
        executable_path = installed_firefox
        attempts = 0

        async def launch_persistent_context(self, *_args, **_kwargs):
            self.attempts += 1
            if self.attempts == 1:
                raise PlaywrightError("profile is already in use")
            return context

    class Playwright:
        firefox = Firefox()

    assert asyncio.run(factory.launch(Playwright(), headless=True)) is context
    context.close_callback()
    lease = _ProfileLease(paths.session)
    lease.acquire()
    lease.release()
