from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

import pytest

from getcourse_downloader.infrastructure.getcourse.discovery import (
    GetCourseDiscoverer,
    clean_title,
    extract_stream_urls,
    normalize_lesson_url,
    normalize_stream_url,
    parse_course_row,
    parse_lesson_item,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_clean_title_removes_status_words():
    assert clean_title("Введение Просмотрено") == "Введение"
    assert clean_title("Модуль 1 Пройдено") == "Модуль 1"
    assert clean_title("Заключение Завершено") == "Заключение"


def test_clean_title_collapses_whitespace():
    assert clean_title("Лекция   1") == "Лекция 1"
    assert clean_title("  Введение  ") == "Введение"


def test_parse_course_row_from_fixture():
    html = (FIXTURES / "course_row.html").read_text(encoding="utf-8")
    title, href = parse_course_row(html)
    assert title == "Курс по Python"
    assert href == "/teach/control/stream/view/id/123"


def test_parse_lesson_item_from_fixture():
    html = (FIXTURES / "lesson_item.html").read_text(encoding="utf-8")
    title, href = parse_lesson_item(html)
    assert title == "Урок 1 — Введение"
    assert href == "/teach/control/lesson/view/id/456"


def test_parse_course_row_with_status_word():
    html = (
        '<tr class="training-row">'
        '<span class="stream-title">Курс Просмотрено</span>'
        '<a href="/course/1">x</a>'
        "</tr>"
    )
    title, href = parse_course_row(html)
    assert title == "Курс"
    assert href == "/course/1"


def test_parse_lesson_item_cleans_title():
    html = '<li><div class="link title">Урок 2 Завершено</div><a href="/lesson/2">x</a></li>'
    title, href = parse_lesson_item(html)
    assert title == "Урок 2"
    assert href == "/lesson/2"


def test_parse_course_row_missing_elements():
    html = '<tr class="training-row"><td>просто текст</td></tr>'
    title, href = parse_course_row(html)
    assert title == "Без названия"
    assert href == "#"


def test_parse_lesson_item_missing_elements():
    html = "<li><span>текст</span></li>"
    title, href = parse_lesson_item(html)
    assert title == "Без названия"
    assert href == "#"


def test_parse_course_row_extra_whitespace():
    html = (
        '<tr class="training-row">'
        '<span class="stream-title">  Курс   с  пробелами  </span>'
        '<a href="/course/1">x</a>'
        "</tr>"
    )
    title, _ = parse_course_row(html)
    assert title == "Курс с пробелами"


def test_extract_stream_urls_from_widget_javascript():
    html = r"""
        <script>
            params = {"link":"https:\/\/school.example\/teach\/control\/stream\/view\/id\/100"};
            duplicate = "/teach/control/stream/view/id/100";
            nested = "/pl/teach/control/stream/view?id=200&editMode=0";
            foreign = "https:\/\/another.example\/teach\/control\/stream\/view\/id\/300";
        </script>
    """

    assert extract_stream_urls(html, "https://school.example/teach/control") == [
        "https://school.example/teach/control/stream/view/id/100",
        "https://school.example/teach/control/stream/view/id/200",
    ]


def test_normalize_getcourse_content_urls():
    base_url = "https://school.example/teach/control"

    assert (
        normalize_stream_url(
            base_url,
            "/pl/teach/control/stream/view?id=123&editMode=0",
        )
        == "https://school.example/teach/control/stream/view/id/123"
    )
    assert (
        normalize_lesson_url(
            base_url,
            "/pl/teach/control/lesson/view?id=456",
        )
        == "https://school.example/teach/control/lesson/view/id/456"
    )
    assert (
        normalize_stream_url(
            base_url,
            "https://another.example/teach/control/stream/view/id/999",
        )
        is None
    )


class _FakeElement:
    def __init__(self, *, html: str = "", text: str = "") -> None:
        self._html = html
        self._text = text

    async def inner_html(self) -> str:
        return self._html

    async def inner_text(self) -> str:
        return self._text


class _FakePage:
    def __init__(
        self,
        browser: _FakeBrowser,
        pages: dict[str, dict[str, Any]],
        start_url: str,
    ) -> None:
        self._browser = browser
        self._pages = pages
        self.url = start_url
        self.closed = False

    async def goto(self, url: str, **_: object) -> None:
        self.url = url
        self._browser.visited_urls.append(url)
        await asyncio.sleep(float(self._pages[url].get("delay", 0)))

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._browser.active_pages -= 1

    async def wait_for_load_state(self, *_: object, **__: object) -> None:
        return None

    async def wait_for_selector(self, *_: object, **__: object) -> None:
        return None

    async def content(self) -> str:
        return str(self._pages[self.url].get("content", ""))

    async def query_selector_all(self, selector: str) -> list[_FakeElement]:
        current = self._pages[self.url]
        if selector == "tr.training-row":
            return [_FakeElement(html=str(html)) for html in current.get("stream_rows", [])]
        if selector == "ul.lesson-list li":
            return [_FakeElement(html=str(html)) for html in current.get("lessons", [])]
        return []

    async def query_selector(self, selector: str) -> _FakeElement | None:
        if selector == "h1" and (title := self._pages[self.url].get("title")):
            return _FakeElement(text=str(title))
        return None


class _FakeBrowser:
    def __init__(self, pages: dict[str, dict[str, Any]]) -> None:
        self._pages = pages
        self.active_pages = 0
        self.max_active_pages = 0
        self.visited_urls: list[str] = []

    async def new_page(self) -> _FakePage:
        if self.active_pages == 0:
            raise RuntimeError("Firefox persistent context has no live window")
        self.active_pages += 1
        self.max_active_pages = max(self.max_active_pages, self.active_pages)
        return _FakePage(self, self._pages, "about:blank")

    def landing_page(self, url: str) -> _FakePage:
        self.active_pages += 1
        self.max_active_pages = max(self.max_active_pages, self.active_pages)
        return _FakePage(self, self._pages, url)


def _lesson_html(identifier: int, title: str) -> str:
    return (
        '<li><a href="/teach/control/lesson/view/id/'
        f'{identifier}"><div class="link title">{title}</div></a></li>'
    )


def _stream_row_html(identifier: int, title: str) -> str:
    return (
        '<tr class="training-row"><a href="/teach/control/stream/view/id/'
        f'{identifier}"><span class="stream-title">{title}</span></a></tr>'
    )


def _nested_stream_rows_from_fixture() -> list[str]:
    html = (FIXTURES / "nested_stream_rows.html").read_text(encoding="utf-8")
    return re.findall(r"<tr\b.*?</tr>", html, flags=re.DOTALL)


def _page_from_fixture(name: str) -> dict[str, Any]:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    title_match = re.search(r"<h1>(.*?)</h1>", html, flags=re.DOTALL)
    return {
        "title": clean_title(title_match.group(1)) if title_match else "",
        "content": html,
        "stream_rows": re.findall(r"<tr\b.*?</tr>", html, flags=re.DOTALL),
        "lessons": re.findall(r"<li\b.*?</li>", html, flags=re.DOTALL),
    }


def test_recursive_discovery_keeps_tree_order_and_ignores_stream_page_fallback():
    base = "https://school.example"
    main_url = f"{base}/teach/control"
    course_a = f"{base}/teach/control/stream/view/id/100"
    course_b = f"{base}/teach/control/stream/view/id/200"
    module_b = f"{base}/teach/control/stream/view/id/201"
    module_c = f"{base}/teach/control/stream/view/id/202"
    pages: dict[str, dict[str, Any]] = {
        main_url: {
            "content": (
                r'params={"link":"https:\/\/school.example\/teach\/control\/stream\/view\/id\/100"};'
                r'params={"link":"https:\/\/school.example\/teach\/control\/stream\/view\/id\/200"};'
            )
        },
        course_a: {
            "title": "Курс A",
            "content": "",
            "lessons": [
                _lesson_html(1001, "Урок A"),
                "<li><span>Служебный пункт без ссылки</span></li>",
            ],
        },
        course_b: {
            "title": "Курс B",
            "content": "",
            "lessons": [_lesson_html(2001, "Введение")],
            "stream_rows": _nested_stream_rows_from_fixture(),
        },
        module_b: {
            "title": "Модуль 1",
            "content": r'parent="\/teach\/control\/stream\/view\/id\/200"',
            "lessons": [_lesson_html(2011, "Практика 1")],
        },
        module_c: {
            "title": "Модуль 2",
            "content": "",
            "lessons": [_lesson_html(2021, "Практика 2")],
        },
    }
    discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
    browser = _FakeBrowser(pages)
    page: Any = browser.landing_page(main_url)
    progress: list[tuple[str, str, int | None]] = []

    async def on_course(update) -> None:
        progress.append((update.url, update.title, update.lesson_count))

    courses = asyncio.run(
        discoverer._parse_page(
            browser,  # type: ignore[arg-type]
            page,
            main_url,
            on_course_discovered=on_course,
        )
    )

    assert [course.title for course in courses] == ["Курс A", "Курс B"]
    assert [child.title for child in courses[1].children] == ["Модуль 2", "Модуль 1"]
    assert [lesson.title for lesson in courses[1].children[0].lessons] == ["Практика 2"]
    assert courses[1].lesson_count == 3
    latest_counts = {url: lesson_count for url, _, lesson_count in progress}
    assert latest_counts == {
        course_a: 1,
        course_b: 3,
        module_b: 1,
        module_c: 1,
    }
    assert (course_b, "Курс B", None) in progress
    assert (course_b, "Курс B", 3) in progress
    assert (course_b, "Курс B", 0) not in progress
    assert progress[0][2] is None
    assert set(browser.visited_urls) == {course_a, course_b, module_b, module_c}
    assert browser.max_active_pages <= 4
    assert browser.active_pages == 0


def test_discovery_concurrency_is_capped_at_four():
    with pytest.raises(ValueError, match="between 1 and 4"):
        GetCourseDiscoverer(browser_factory=None, concurrency=5)  # type: ignore[arg-type]


def test_landing_page_stays_alive_while_firefox_creates_stream_tabs():
    base = "https://school.example"
    main_url = f"{base}/teach/control"
    stream_url = f"{base}/teach/control/stream/view/id/100"
    pages = {
        main_url: {"content": r'course="\/teach\/control\/stream\/view\/id\/100";'},
        stream_url: {"title": "Курс", "content": ""},
    }
    discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
    browser = _FakeBrowser(pages)

    courses = asyncio.run(
        discoverer._parse_page(  # type: ignore[arg-type]
            browser,
            browser.landing_page(main_url),
            main_url,
        )
    )

    assert [course.title for course in courses] == ["Курс"]
    assert browser.active_pages == 0


def test_direct_custom_course_shell_adds_hidden_modules_to_standard_discovery():
    base = "https://school.example"
    root_url = f"{base}/teach/control/stream/view/id/1000"
    module_urls = [
        f"{base}/teach/control/stream/view/id/{identifier}" for identifier in range(1101, 1111)
    ]
    pages: dict[str, dict[str, Any]] = {
        root_url: _page_from_fixture("custom_course_shell.html"),
    }
    for index, module_url in enumerate(module_urls, start=1):
        pages[module_url] = {
            "title": f"Модуль {index}",
            "content": (f'<div class="breadcrumbs"><a href="{root_url}">Большой курс</a></div>'),
            "lessons": [_lesson_html(2000 + index, f"Урок {index}")],
        }

    discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
    browser = _FakeBrowser(pages)

    courses = asyncio.run(
        discoverer._parse_page(  # type: ignore[arg-type]
            browser,
            browser.landing_page(root_url),
            root_url,
        )
    )

    assert len(courses) == 1
    assert courses[0].title == "Большой курс с собственной навигацией"
    assert [child.title for child in courses[0].children] == [
        f"Модуль {index}" for index in range(1, 11)
    ]
    assert courses[0].lesson_count == 10
    assert browser.visited_urls == module_urls


def test_standard_stream_rows_take_priority_over_fallback_markup():
    base = "https://school.example"
    root_url = f"{base}/teach/control/stream/view/id/1000"
    standard_url = f"{base}/teach/control/stream/view/id/1101"
    hidden_url = f"{base}/teach/control/stream/view/id/1199"
    pages = {
        root_url: {
            "title": "Обычный курс",
            "stream_rows": [_stream_row_html(1101, "Основной модуль")],
            "content": r'hidden="\/teach\/control\/stream\/view\/id\/1199"',
        },
        standard_url: {"title": "Основной модуль", "content": ""},
        hidden_url: {"title": "Служебный модуль", "content": ""},
    }
    discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
    browser = _FakeBrowser(pages)

    courses = asyncio.run(
        discoverer._parse_page(  # type: ignore[arg-type]
            browser,
            browser.landing_page(root_url),
            root_url,
        )
    )

    assert [child.title for child in courses[0].children] == ["Основной модуль"]
    assert browser.visited_urls == [standard_url]


def test_sample_nested_tree_keeps_folder_and_fourteen_lessons():
    base = "https://school.example"
    club_url = f"{base}/teach/control/stream/view/id/100"
    dividend_url = f"{base}/teach/control/stream/view/id/200"
    pages = {
        club_url: _page_from_fixture("sample_course.html"),
        dividend_url: _page_from_fixture("sample_module.html"),
    }
    discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
    browser = _FakeBrowser(pages)

    courses = asyncio.run(
        discoverer._parse_page(  # type: ignore[arg-type]
            browser,
            browser.landing_page(club_url),
            club_url,
        )
    )

    assert len(courses) == 1
    assert courses[0].title == "Демо-курс"
    assert [child.title for child in courses[0].children] == ["Учебный модуль"]
    assert courses[0].lesson_count == 14
    assert len(courses[0].children[0].lessons) == 14
    assert not any("/lesson/" in url for url in browser.visited_urls)


def test_direct_child_start_does_not_follow_breadcrumb_parent():
    base = "https://school.example"
    club_url = f"{base}/teach/control/stream/view/id/100"
    dividend_url = f"{base}/teach/control/stream/view/id/200"
    pages = {
        club_url: _page_from_fixture("sample_course.html"),
        dividend_url: _page_from_fixture("sample_module.html"),
    }
    discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
    browser = _FakeBrowser(pages)

    courses = asyncio.run(
        discoverer._parse_page(  # type: ignore[arg-type]
            browser,
            browser.landing_page(dividend_url),
            dividend_url,
        )
    )

    assert [course.title for course in courses] == ["Учебный модуль"]
    assert courses[0].children == ()
    assert browser.visited_urls == []
    assert club_url not in browser.visited_urls


def test_shared_child_uses_first_parent_order_not_fetch_completion_order():
    base = "https://school.example"
    main_url = f"{base}/teach/control"
    root_a = f"{base}/teach/control/stream/view/id/100"
    root_b = f"{base}/teach/control/stream/view/id/200"
    shared = f"{base}/teach/control/stream/view/id/300"
    pages = {
        main_url: {
            "content": (
                r'first="\/teach\/control\/stream\/view\/id\/100";'
                r'second="\/teach\/control\/stream\/view\/id\/200";'
            )
        },
        root_a: {
            "title": "Первый",
            "content": "",
            "delay": 0.02,
            "stream_rows": [_stream_row_html(300, "Общий модуль")],
        },
        root_b: {
            "title": "Второй",
            "content": "",
            "stream_rows": [_stream_row_html(300, "Общий модуль")],
        },
        shared: {
            "title": "Общий модуль",
            "content": "",
            "lessons": [_lesson_html(3001, "Общий урок")],
        },
    }
    discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
    browser = _FakeBrowser(pages)

    courses = asyncio.run(
        discoverer._parse_page(  # type: ignore[arg-type]
            browser,
            browser.landing_page(main_url),
            main_url,
        )
    )

    assert [course.title for course in courses] == ["Первый", "Второй"]
    assert [child.title for child in courses[0].children] == ["Общий модуль"]
    assert courses[1].children == ()
    assert browser.visited_urls.count(shared) == 1


def test_regex_fallback_is_not_used_on_non_landing_page():
    base = "https://school.example"
    arbitrary_url = f"{base}/some/custom/page"
    stream_url = f"{base}/teach/control/stream/view/id/100"
    pages = {
        arbitrary_url: {"content": r'embedded="\/teach\/control\/stream\/view\/id\/100";'},
        stream_url: {
            "title": "Не должен загружаться",
            "content": "",
        },
    }
    discoverer = GetCourseDiscoverer(browser_factory=None)  # type: ignore[arg-type]
    browser = _FakeBrowser(pages)

    courses = asyncio.run(
        discoverer._parse_page(  # type: ignore[arg-type]
            browser,
            browser.landing_page(arbitrary_url),
            arbitrary_url,
        )
    )

    assert courses == []
    assert browser.visited_urls == []
