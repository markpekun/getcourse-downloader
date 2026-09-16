import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.infrastructure.getcourse.discovery import (
    GetCourseDiscoverer,
    _is_authentication_required,
)


@pytest.mark.parametrize(
    ("status", "code", "message"),
    [
        (403, "HTTP_403", "нет доступа"),
        (404, "HTTP_404", "не найдена"),
        (503, "HTTP_5XX", "временно недоступен"),
        (429, "HTTP_ERROR", "HTTP 429"),
    ],
)
def test_http_failures_have_stable_codes(status, code, message):
    with pytest.raises(ExternalServiceError, match=message) as captured:
        GetCourseDiscoverer._raise_for_http_error(SimpleNamespace(status=status))

    assert captured.value.code == code


def test_successful_or_missing_navigation_response_is_accepted():
    GetCourseDiscoverer._raise_for_http_error(SimpleNamespace(status=200))
    GetCourseDiscoverer._raise_for_http_error(None)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (PlaywrightTimeoutError("Timeout 30000ms exceeded"), "SITE_TIMEOUT"),
        (PlaywrightError("net::ERR_NAME_NOT_RESOLVED"), "SITE_CONNECTION_FAILED"),
    ],
)
def test_navigation_failures_have_stable_codes(error, code):
    class Page:
        async def goto(self, *_args, **_kwargs):
            raise error

    discoverer = GetCourseDiscoverer.__new__(GetCourseDiscoverer)

    with pytest.raises(ExternalServiceError) as captured:
        asyncio.run(discoverer._navigate(Page(), "https://school.example/course"))

    assert captured.value.code == code


@pytest.mark.parametrize(
    ("url", "required"),
    [
        ("https://login-school.getcourse.ru/teach/control/stream/view/id/100", False),
        ("https://school.example/teach/control?next=/login", False),
        ("https://school.example/teach/control?notrequired=true", False),
        ("https://school.example/cms/system/login", True),
        ("https://school.example/teach/control?required=true", True),
    ],
)
def test_authentication_detection_uses_login_route_and_exact_query_parameter(url, required):
    class Page:
        async def wait_for_load_state(self, *_args, **_kwargs):
            return None

    page = Page()
    page.url = url

    assert asyncio.run(_is_authentication_required(page)) is required
