from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from playwright.async_api import BrowserContext, Route, async_playwright

from getcourse_downloader.domain.errors import ExternalServiceError
from getcourse_downloader.infrastructure.getcourse import discovery
from getcourse_downloader.infrastructure.getcourse.discovery import GetCourseDiscoverer
from getcourse_downloader.infrastructure.getcourse.video_signals import VIDEO_PLAYER_SELECTOR

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).parent / "fixtures"
ORIGIN = "https://school.example"
ROOT_URL = f"{ORIGIN}/teach/control/stream/view/id/1000"
MODULE_URL = f"{ORIGIN}/teach/control/stream/view/id/1102"
MODULE_TITLES = [
    "Введение",
    "Модуль 0. Подготовка",
    "Модуль 1. Основы учебного предмета",
    "Модуль 2. Практические задания",
    "Модуль 3. Итоговая работа",
    "Дополнительные материалы",
]
LESSON_TITLES = [
    "1. Первое занятие",
    "2. Подготовка материалов",
    "3. Пример учебной задачи",
    "4. Практическое упражнение",
    "4а. Дополнительное упражнение",
    "5. Подведение итогов",
    "Домашнее задание",
]


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _run_in_firefox(check: Callable[[BrowserContext], Awaitable[None]]) -> None:
    """Use an isolated browser and block every request outside the supplied pages."""

    async def run() -> None:
        async with async_playwright() as playwright:
            if not Path(playwright.firefox.executable_path).is_file():
                pytest.skip("Local Firefox is required for the offline DOM regression tests")
            browser = await playwright.firefox.launch(headless=True)
            try:
                context = await browser.new_context()

                async def block_request(route: Route) -> None:
                    await route.abort()

                await context.route("**/*", block_request)
                await check(context)
            finally:
                await browser.close()

    asyncio.run(run())


async def _serve_pages(context: BrowserContext, pages: dict[str, str]) -> list[str]:
    visited: list[str] = []

    async def serve(route: Route) -> None:
        url = route.request.url
        if route.request.resource_type == "document" and url in pages:
            visited.append(url)
            await route.fulfill(content_type="text/html; charset=utf-8", body=pages[url])
        else:
            await route.abort()

    await context.route("**/*", serve)
    return visited


def test_redesigned_course_tree_collects_seven_lessons_and_keeps_module_order():
    async def check(context: BrowserContext) -> None:
        pages = {ROOT_URL: _fixture("redesigned_course.html")}
        for identifier, title in enumerate(MODULE_TITLES, start=1101):
            pages[f"{ORIGIN}/teach/control/stream/view/id/{identifier}"] = (
                f'<div class="training-title">{title}</div><div class="LessonList-container"></div>'
            )
        pages[MODULE_URL] = _fixture("redesigned_module.html")
        visited = await _serve_pages(context, pages)
        page = await context.new_page()
        await page.goto(ROOT_URL, wait_until="domcontentloaded")
        discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
        updates = []

        async def on_update(update) -> None:
            updates.append(update)

        courses = await discoverer._parse_page(
            context, page, ROOT_URL, on_course_discovered=on_update
        )

        assert len(courses) == 1
        assert courses[0].title == "Демонстрационный курс"
        assert [child.title for child in courses[0].children] == MODULE_TITLES
        module = courses[0].children[1]
        assert [lesson.title for lesson in module.lessons] == LESSON_TITLES
        assert [lesson.url for lesson in module.lessons] == [
            f"{ORIGIN}/teach/control/lesson/view/id/{identifier}"
            for identifier in range(2101, 2108)
        ]
        # Other modules have no supplied lesson HTML: footer counts are not lessons.
        assert module.lesson_count == courses[0].lesson_count == 7
        assert {update.url: update.lesson_count for update in updates}[ROOT_URL] == 7
        assert len(visited) == len(set(visited)) == 7

    _run_in_firefox(check)


def test_redesigned_module_cards_provide_titles_without_url_fallback():
    async def check(context: BrowserContext) -> None:
        page = await context.new_page()
        await page.set_content(_fixture("redesigned_course.html"))
        links = await GetCourseDiscoverer._extract_stream_links(
            page, ROOT_URL, allow_fallback=False
        )
        assert [link.title for link in links] == MODULE_TITLES
        assert [link.url for link in links] == [
            f"{ORIGIN}/teach/control/stream/view/id/{identifier}"
            for identifier in range(1101, 1107)
        ]

    _run_in_firefox(check)


def test_direct_redesigned_module_does_not_follow_parent_breadcrumbs():
    async def check(context: BrowserContext) -> None:
        visited = await _serve_pages(context, {MODULE_URL: _fixture("redesigned_module.html")})
        page = await context.new_page()
        await page.goto(MODULE_URL, wait_until="domcontentloaded")
        discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
        courses = await discoverer._parse_page(context, page, MODULE_URL)
        assert len(courses) == 1
        assert courses[0].lesson_count == 7
        assert courses[0].children == ()
        assert visited == [MODULE_URL]

    _run_in_firefox(check)


def test_lesson_cards_filter_invalid_links_and_deduplicate_with_legacy_lessons():
    async def check(context: BrowserContext) -> None:
        page = await context.new_page()
        await page.set_content(
            '<ul class="lesson-list"><li><a href="/teach/control/lesson/view/id/1">'
            '<div class="link title">Обычный урок</div></a></li></ul>'
            '<div class="LessonList-container">'
            '<a class="card-link" href="/pl/teach/control/lesson/view?id=1">'
            '<div class="LessonCard"><div class="lesson-card_heading">Дубликат</div>'
            "</div></a>"
            '<a class="card-link" href="/pl/teach/control/lesson/view?id=2&amp;mode=preview">'
            '<div class="LessonCard"><div class="lesson-card_heading">'
            "Новый <strong>урок</strong></div><div>Все видео просмотрены</div></div></a>"
            '<a class="card-link" href="/teach/control/lesson/view/id/2">'
            '<div class="lesson-card_heading">Второй дубликат</div></a>'
            '<a class="card-link" href="https://another.example/teach/control/lesson/view/id/3">'
            '<div class="lesson-card_heading">Другая школа</div></a>'
            '<a class="card-link" href="/teach/control/stream/view/id/4">'
            '<div class="lesson-card_heading">Модуль</div></a>'
            '<a class="card-link" href="/teach/control/lesson/view/id/5">'
            '<div class="lesson-card_heading"> </div></a>'
            '<a class="card-link" href="/teach/control/lesson/view/id/6">Без заголовка</a>'
            '<a class="card-link"><div class="lesson-card_heading">Без ссылки</div></a>'
            "</div>"
            '<nav><a href="/teach/control/lesson/view/id/99">Следующий урок</a></nav>'
        )
        lessons = await GetCourseDiscoverer._read_lessons(page, MODULE_URL)
        assert [lesson.title for lesson in lessons] == ["Обычный урок", "Новый урок"]
        assert [lesson.url for lesson in lessons] == [
            f"{ORIGIN}/teach/control/lesson/view/id/1",
            f"{ORIGIN}/teach/control/lesson/view/id/2",
        ]

    _run_in_firefox(check)


def test_lesson_page_navigation_is_not_a_lesson_list_and_player_is_recognized():
    async def check(context: BrowserContext) -> None:
        page = await context.new_page()
        await page.set_content(_fixture("redesigned_lesson.html"))
        assert await GetCourseDiscoverer._read_lessons(page, MODULE_URL) == []
        assert await page.query_selector(VIDEO_PLAYER_SELECTOR) is not None

    _run_in_firefox(check)


def test_redesigned_module_waits_for_skeleton_to_hide_before_reading_cards():
    async def check(context: BrowserContext) -> None:
        shell = '<div class="gc-redesigned"><div class="loader-skeleton">Загрузка</div></div>'
        await _serve_pages(context, {MODULE_URL: shell})
        page = await context.new_page()
        await page.goto(MODULE_URL, wait_until="domcontentloaded")
        await page.evaluate(
            "html => { setTimeout(() => { document.body.innerHTML = html; }, 150); }",
            _fixture("redesigned_module.html"),
        )
        discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
        courses = await discoverer._parse_page(context, page, MODULE_URL)
        assert courses[0].title == MODULE_TITLES[1]
        assert [lesson.title for lesson in courses[0].lessons] == LESSON_TITLES

    _run_in_firefox(check)


def test_unfinished_redesigned_loading_reports_an_error_instead_of_zero_lessons(monkeypatch):
    monkeypatch.setattr(discovery, "REDESIGNED_CONTENT_TIMEOUT_MS", 50, raising=False)

    async def check(context: BrowserContext) -> None:
        shell = '<div class="gc-redesigned"><div class="loader-skeleton">Загрузка</div></div>'
        await _serve_pages(context, {MODULE_URL: shell})
        page = await context.new_page()
        await page.goto(MODULE_URL, wait_until="domcontentloaded")
        discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
        with pytest.raises(ExternalServiceError) as error:
            await discoverer._parse_page(context, page, MODULE_URL)
        assert error.value.code == "CONTENT_LOAD_TIMEOUT"

    _run_in_firefox(check)
