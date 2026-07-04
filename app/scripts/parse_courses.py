from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import Playwright, async_playwright

_PROJECT_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from utils_console import configure_console_output


USER_DATA_DIR: str = "session_data"
_PRJ = Path(__file__).resolve().parent.parent.parent
_OUTPUT_DIR = _PRJ / "app" / "data"
_OUTPUT_FILE = str(_OUTPUT_DIR / "courses.json")
LESSON_LIST_TIMEOUT: float = 5_000.0

configure_console_output()


def clean_title(title: str) -> str:
    cleaned = re.sub(
        r"\b(Просмотрено|Пройдено|Завершено)\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", cleaned).strip()


async def ensure_authenticated(
    playwright: Playwright,
    playlist_url: str,
    wait_for_login: Callable[[], Awaitable[None]] | None = None,
) -> None:
    browser = await playwright.firefox.launch_persistent_context(
        USER_DATA_DIR,
        headless=True,
    )
    page = browser.pages[0] if browser.pages else await browser.new_page()
    await page.goto(playlist_url)
    needs_auth: bool = "login" in page.url.lower()
    await browser.close()

    if not needs_auth:
        print("[OK] Авторизация активна.")
        return

    print("[INFO] Требуется авторизация. Выполните вход в браузере.")

    browser = await playwright.firefox.launch_persistent_context(
        USER_DATA_DIR,
        headless=False,
    )
    login_page = browser.pages[0] if browser.pages else await browser.new_page()
    await login_page.goto(playlist_url)

    if wait_for_login:
        await wait_for_login()
    else:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            input,
            "После успешного входа нажмите Enter...",
        )

    await browser.close()
    print("[OK] Авторизация выполнена, продолжаем работу.")


async def parse_courses(
    playwright: Playwright,
    playlist_url: str,
    on_course_parsed: Callable[[str, int], Awaitable[None]] | None = None,
) -> list[dict]:
    browser = await playwright.firefox.launch_persistent_context(
        USER_DATA_DIR,
        headless=True,
    )
    page = browser.pages[0] if browser.pages else await browser.new_page()
    await page.goto(playlist_url)

    print("[INFO] Загружаю список курсов...")
    print("   → Ищу список курсов на странице...")
    try:
        await page.wait_for_selector("tr.training-row", timeout=5_000)
        rows = await page.query_selector_all("tr.training-row")
    except Exception:
        rows = []

    if rows:
        courses: list[dict[str, str]] = []
        for row in rows:
            title_el = await row.query_selector("span.stream-title")
            course_title: str = await title_el.inner_text() if title_el else "Без названия"

            link_el = await row.query_selector("a")
            href: str = await link_el.get_attribute("href") if link_el else "#"
            href = urljoin(playlist_url, href)

            courses.append({"title": clean_title(course_title), "href": href})

        all_courses = []
        for course in courses:
            print(f"\n[COURSE] {course['title']}")
            await page.goto(course["href"])

            try:
                await page.wait_for_selector("ul.lesson-list li", timeout=LESSON_LIST_TIMEOUT)
                lesson_elements = await page.query_selector_all("ul.lesson-list li")
            except Exception:
                lesson_elements = []

            lessons_data = []
            for lesson in lesson_elements:
                title_el = await lesson.query_selector("div.link.title")
                lesson_title: str = await title_el.inner_text() if title_el else "Без названия"
                lesson_title = clean_title(lesson_title)

                link_el = await lesson.query_selector("a")
                lesson_href: str = await link_el.get_attribute("href") if link_el else "#"
                lesson_href = urljoin(playlist_url, lesson_href)

                print(f"   [LESSON] {lesson_title}")
                lessons_data.append({"title": lesson_title, "url": lesson_href})

            all_courses.append({"course_title": course["title"], "lessons": lessons_data})

            if on_course_parsed:
                await on_course_parsed(course["title"], len(lessons_data))

        await browser.close()
        return all_courses

    lessons = await page.query_selector_all("ul.lesson-list li")

    if lessons:
        print("   → Найдены уроки на текущей странице (один курс)")
        course_title_el = await page.query_selector("h1")
        course_title: str = await course_title_el.inner_text() if course_title_el else "Курс"
        course_title = clean_title(course_title)

        all_courses: list[dict] = [{
            "course_title": course_title,
            "lessons": [],
        }]

        for lesson in lessons:
            title_el = await lesson.query_selector("div.link.title")
            lesson_title: str = await title_el.inner_text() if title_el else "Без названия"
            lesson_title = clean_title(lesson_title)

            link_el = await lesson.query_selector("a")
            lesson_href: str = await link_el.get_attribute("href") if link_el else "#"
            lesson_href = urljoin(playlist_url, lesson_href)

            print(f"   [LESSON] {lesson_title}")
            all_courses[0]["lessons"].append({"title": lesson_title, "url": lesson_href})

        if on_course_parsed:
            await on_course_parsed(course_title, len(all_courses[0]["lessons"]))

        await browser.close()
        return all_courses

    print("   ⚠ Не удалось найти курсы или уроки на странице.")
    await browser.close()
    return []


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Парсинг курсов GetCourse — извлечение списка уроков в JSON.",
    )
    parser.add_argument(
        "playlist_url",
        help="URL плейлиста курсов на GetCourse (https://…)",
    )
    args = parser.parse_args()

    playlist_url: str = args.playlist_url
    if not playlist_url.startswith("http"):
        print("[ERROR] URL плейлиста должен начинаться с http:// или https://")
        sys.exit(1)

    print(f"[INFO] Парсинг плейлиста: {playlist_url}")

    async with async_playwright() as playwright:
        await ensure_authenticated(playwright, playlist_url)
        courses = await parse_courses(playwright, playlist_url)

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = Path(_OUTPUT_FILE)
    output_path.write_text(
        json.dumps(courses, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )

    print(f"\n[OK] Курсы сохранены в {_OUTPUT_FILE}.")


if __name__ == "__main__":
    asyncio.run(main())
