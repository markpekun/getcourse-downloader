from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from typing import Any

from playwright.async_api import Frame, async_playwright

from login import ensure_login_active
from utils_console import configure_console_output

USER_DATA_DIR = "session_data"
_PROJECT_ROOT = Path(__file__).resolve().parent
_COURSES_PATH = _PROJECT_ROOT / "app" / "data" / "courses.json"
PREF = {"cloudflare": 3, "integrosproxy": 2}

configure_console_output()


def _extract_video_id(url: str) -> str:
    match = re.search(r"/api/playlist/media/([^/?#]+)/", url)
    return match.group(1) if match else url


def _extract_provider(url: str) -> str:
    match = re.search(r"[?&]user-cdn=([^&]+)", url)
    return match.group(1) if match else ""


def _provider_score(provider: str) -> int:
    return PREF.get(provider, 1)


def _extract_quality(url: str) -> int:
    path = url.split("?", 1)[0]
    numeric_parts = [part for part in path.split("/") if part.isdigit()]
    if not numeric_parts:
        return 0
    return int(numeric_parts[-1])


def _is_media_playlist_request(url: str) -> bool:
    return "/api/playlist/media/" in url and "user-cdn=" in url


def sanitize_filename(name: str) -> str:
    clean = re.sub(
        r"\b(Просмотрено|Пройдено|Завершено)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\s+", " ", clean).strip()
    return re.sub(r"[\\/*?:\"<>|]", "_", clean)


async def _click_modal_if_present(frame: Frame) -> None:
    modal = frame.locator(".mst-root .cnf-root, .cnf-root")
    try:
        await modal.wait_for(state="attached", timeout=2500)
    except Exception:
        return

    for selector in [".cnf-button--decline", ".cnf-button--confirm"]:
        button = frame.locator(selector)
        if await button.count():
            await frame.evaluate(
                "(el)=>el.click()",
                await button.first.element_handle(),
            )
            break

    try:
        await modal.wait_for(state="detached", timeout=4000)
    except Exception:
        pass


async def _click_play(frame: Frame) -> None:
    button = frame.locator(".fsn-main-btn.fsn-main-btn--play, .fsn-main-btn")
    await button.first.wait_for(state="attached", timeout=8000)
    await frame.evaluate("(el)=>el.click()", await button.first.element_handle())


async def _handle_player_frame(frame: Frame) -> bool:
    if not await frame.query_selector(".vpl-root"):
        return False
    if not await frame.query_selector(".mst-root"):
        return False

    try:
        await frame.evaluate(
            """
            (() => {
                const els = document.querySelectorAll('video, audio');
                for (const el of els) {
                    el.muted = true;
                    el.volume = 0;
                    el.pause = () => {};
                    try { el.play(); } catch {}
                }
                const ctxs = (window.AudioContext || window.webkitAudioContext);
                if (ctxs) {
                    try {
                        const ctx = new ctxs();
                        ctx.suspend();
                    } catch(e) {}
                }
            })();
            """
        )
    except Exception as exc:
        print(f"[WARN] Не удалось заглушить звук: {exc}")

    await _click_modal_if_present(frame)
    await _click_play(frame)
    return True


async def _run_gcpd(url: str, output_path: str) -> None:
    script_path = os.path.join(os.path.dirname(__file__), "gcpd.py")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        script_path,
        "--url",
        url,
        "--output",
        output_path,
    )
    exit_code = await process.wait()
    if exit_code != 0:
        print(f"[WARN] gcpd завершился с ошибкой (код {exit_code}): {output_path}")


async def process_lesson(
    browser,
    course_title: str,
    lesson: dict[str, Any],
    save_root: str,
    quality_filter: str = "auto",
) -> None:
    page = await browser.new_page()
    await page.goto(lesson["url"])

    login_required_url = "https://school.beilbei.ru/cms/system/login?required=true"
    was_login_page = page.url.startswith(login_required_url)
    login_restored = await ensure_login_active(page)

    if not login_restored:
        await page.close()
        return

    if was_login_page:
        print("[INFO] Повторная загрузка урока после авторизации...")
        await page.goto(lesson["url"])
        await asyncio.sleep(2)

    best: dict[str, tuple[int, int, str]] = {}
    intercepted_urls: list[str] = []
    seen_interesting_urls: set[str] = set()
    pending_tasks: set[asyncio.Task] = set()

    async def on_request(request):
        url = request.url

        if _is_media_playlist_request(url) and url not in seen_interesting_urls:
            print(f"[REQ] {request.method} {url}")
            seen_interesting_urls.add(url)
            if len(intercepted_urls) < 200:
                intercepted_urls.append(url)

        if _is_media_playlist_request(url):
            video_id = _extract_video_id(url)
            provider = _extract_provider(url)
            score = _provider_score(provider)
            quality_val = _extract_quality(url)
            current = best.get(video_id)
            if current is None or (score, quality_val) > (current[0], current[1]):
                best[video_id] = (score, quality_val, url)

    def on_request_handler(req):
        task = asyncio.create_task(on_request(req))
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)

    page.on("request", on_request_handler)

    for frame in [fr for fr in page.frames if "vhcdn.com" in (fr.url or "")]:
        try:
            await _handle_player_frame(frame)
        except Exception as exc:
            print(f"[WARN] Ошибка фрейма: {exc}")

    await asyncio.sleep(5)
    await page.close()

    if pending_tasks:
        await asyncio.wait(pending_tasks, timeout=2)

    if quality_filter and quality_filter != "auto":
        max_quality = int(quality_filter)
        before = len(best)
        best = {k: v for k, v in best.items() if v[1] <= max_quality}
        filtered = before - len(best)
        if filtered:
            print(f"[QUALITY] Отфильтровано {filtered} потоков выше {quality_filter}p")

    videos = [item[2] for item in sorted(best.values(), key=lambda item: (-item[0], -item[1]))]
    if not videos:
        print(f"[WARN] Видео не найдено: {lesson['title']}")
        if intercepted_urls:
            print("[DEBUG] Последние перехваченные URL (до 20):")
            for captured_url in intercepted_urls[-20:]:
                print(f"[DEBUG] {captured_url}")
        else:
            print("[DEBUG] Перехват запросов /api/playlist/media/ не сработал.")
        return

    course_path = os.path.join(save_root, course_title)
    os.makedirs(course_path, exist_ok=True)
    safe_title = sanitize_filename(lesson["title"])

    if len(videos) == 1:
        await _run_gcpd(videos[0], os.path.join(course_path, safe_title))
        return

    lesson_path = os.path.join(course_path, safe_title)
    os.makedirs(lesson_path, exist_ok=True)
    for index, video_url in enumerate(videos, start=1):
        await _run_gcpd(video_url, os.path.join(lesson_path, f"video_{index}"))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Скачивание уроков из courses.json")
    parser.add_argument("--quality", default="auto", choices=["auto", "1080", "720", "480", "360"])
    parser.add_argument("--save-path", default="downloads", dest="save_path")
    args = parser.parse_args()

    save_root = args.save_path
    quality_setting = args.quality

    if not _COURSES_PATH.exists() or _COURSES_PATH.stat().st_size == 0:
        print("[WARN] Файл courses.json пустой или отсутствует.")
        print("[TIP] Запустите parse_courses.py с URL плейлиста для создания списка курсов.")
        return

    with open(_COURSES_PATH, "r", encoding="utf-8") as courses_file:
        courses = json.load(courses_file)

    async with async_playwright() as playwright:
        browser = await playwright.firefox.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
        )

        for course in courses:
            print(f"\n[COURSE] Курс: {course['course_title']}")
            for lesson in course["lessons"]:
                await process_lesson(
                    browser,
                    course["course_title"],
                    lesson,
                    save_root,
                    quality_setting,
                )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
