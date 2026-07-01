from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import time
from typing import Any

from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright

from utils_console import configure_console_output

USER_DATA_DIR = "session_data"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_COURSES_PATH = _PROJECT_ROOT / "app" / "data" / "courses.json"

configure_console_output()


def _extract_quality(url: str) -> int:
    path = url.split("?", 1)[0]
    numeric_parts = [part for part in path.split("/") if part.isdigit()]
    if not numeric_parts:
        return 0
    return int(numeric_parts[-1])


def sanitize_filename(name: str) -> str:
    clean = re.sub(
        r"\b(Просмотрено|Пройдено|Завершено)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\s+", " ", clean).strip()
    return re.sub(r"[\\/*?:\"<>|]", "_", clean)


def _parse_master_playlist(text: str, master_url: str) -> dict[int, str]:
    qualities = {}
    lines = text.strip().splitlines()
    last_resolution = None

    for line in lines:
        line = line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            match = re.search(r"RESOLUTION=\d+x(\d+)", line)
            last_resolution = int(match.group(1)) if match else None
        elif line and not line.startswith("#"):
            if not line.startswith("http"):
                line = urljoin(master_url, line)
            quality = _extract_quality(line)
            if quality == 0 and last_resolution:
                quality = last_resolution
            if quality > 0:
                qualities[quality] = line
            last_resolution = None

    return qualities


def _select_quality_url(qualities: dict[int, str], quality_filter: str) -> str | None:
    if not qualities:
        return None

    available = sorted(qualities)

    if not quality_filter or quality_filter == "auto":
        return qualities[available[-1]]

    target = int(quality_filter)

    if target in qualities:
        return qualities[target]

    below = [q for q in available if q < target]
    if below:
        return qualities[below[-1]]

    return qualities[available[0]]


async def _download_video(playlist_url: str, output_path: str) -> None:
    import aiohttp
    import shutil
    import tempfile

    output_mp4 = output_path + ".mp4"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://school.beilbei.ru/",
    }

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(playlist_url) as resp:
            playlist = await resp.text()

        segment_urls = [
            line.strip() for line in playlist.splitlines()
            if line.strip() and not line.startswith("#") and (".bin" in line or ".ts" in line)
        ]
        total = len(segment_urls)
        if not total:
            print("  Нет сегментов")
            return

        tmpdir = tempfile.mkdtemp()
        sem = asyncio.Semaphore(10)

        downloaded_count = 0
        last_report = 0

        async def download_seg(idx: int, seg_url: str) -> str | None:
            nonlocal downloaded_count, last_report
            async with sem:
                for attempt in range(3):
                    try:
                        async with session.get(seg_url) as resp:
                            data = await resp.read()
                        path = os.path.join(tmpdir, f"{idx:05d}.bin")
                        with open(path, "wb") as f:
                            f.write(data)
                        downloaded_count += 1
                        pct = downloaded_count * 100 // total
                        if pct >= last_report + 5 or downloaded_count - last_report >= 10:
                            last_report = downloaded_count
                            print(f"\r  Сегменты: {downloaded_count}/{total} ({pct}%)", end="", flush=True)
                        return path
                    except Exception:
                        if attempt == 2:
                            return None
                        await asyncio.sleep(1)

        tasks = [download_seg(i, u) for i, u in enumerate(segment_urls)]
        results = await asyncio.gather(*tasks)
        segments = sorted(r for r in results if r)

        if not segments:
            print("  Не скачано ни одного сегмента")
            return

        ts_file = output_mp4.replace(".mp4", ".ts")
        with open(ts_file, "wb") as out:
            for seg in segments:
                with open(seg, "rb") as f:
                    out.write(f.read())

        shutil.rmtree(tmpdir, ignore_errors=True)

        print(f"\r  Сегментов: {len(segments)}/{total} ({len(segments)*100//total}%)")

        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i", ts_file,
            "-c", "copy",
            "-bsf:a", "aac_adtstoasc",
            output_mp4,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(process.communicate(), timeout=300)
        except asyncio.TimeoutError:
            process.kill()
            print(f"  Ошибка: ffmpeg завис (таймаут 5 мин)")
            os.remove(ts_file)
            return
        os.remove(ts_file)
        if process.returncode == 0:
            print(f"  Сохранено: {output_mp4}")
        else:
            err = stderr.decode("utf-8", errors="replace")[-300:]
            print(f"  Ошибка конвертации")


async def process_lesson(
    browser,
    course_title: str,
    lesson: dict[str, Any],
    save_root: str,
    quality_filter: str = "auto",
) -> None:
    lesson_title = lesson["title"]
    lesson_url = lesson["url"]
    print(f"\n  >>> {lesson_title}")

    page = await browser.new_page()

    master_urls_seen: set[str] = set()
    master_playlists: list[tuple[str, str]] = []
    last_arrival = 0.0

    async def _on_response(response):
        nonlocal last_arrival
        url = response.url
        if "/api/playlist/master/" not in url or url in master_urls_seen:
            return
        master_urls_seen.add(url)
        try:
            text = await response.text()
            master_playlists.append((url, text))
            last_arrival = time.monotonic()
        except Exception:
            pass

    page.on("response", lambda resp: asyncio.create_task(_on_response(resp)))

    await page.goto(lesson_url)

    for attempt in range(3):
        if "login" not in page.url.lower() and "required=true" not in page.url:
            break
        print("\n  Требуется авторизация. Войдите в аккаунт в браузере.")
        print("  Через 30 секунд страница перезагрузится...")
        master_urls_seen.clear()
        master_playlists.clear()
        last_arrival = 0.0
        await asyncio.sleep(30)
        await page.goto(lesson_url)

    start_time = time.monotonic()
    while True:
        if time.monotonic() - start_time >= 30:
            break
        if master_playlists and time.monotonic() - last_arrival >= 5:
            break
        await asyncio.sleep(0.5)

    await page.close()

    if not master_playlists:
        print("  Master playlist не получен")
        return

    for idx, (master_url, master_text) in enumerate(master_playlists, start=1):
        qualities = _parse_master_playlist(master_text, master_url)
        selected_url = _select_quality_url(qualities, quality_filter)

        if not selected_url:
            print("  Не удалось подобрать качество")
            continue

        q = _extract_quality(selected_url)
        print(f"  Качество: {q}p")

        course_path = os.path.join(save_root, course_title)
        os.makedirs(course_path, exist_ok=True)
        safe_title = sanitize_filename(lesson_title)

        if len(master_playlists) > 1:
            video_dir = os.path.join(course_path, safe_title)
            os.makedirs(video_dir, exist_ok=True)
            await _download_video(selected_url, os.path.join(video_dir, f"video_{idx}"))
        else:
            await _download_video(selected_url, os.path.join(course_path, safe_title))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Скачивание уроков из courses.json")
    parser.add_argument("--quality", default="auto", choices=["auto", "1080", "720", "480", "360"])
    parser.add_argument("--save-path", default="downloads", dest="save_path")
    parser.add_argument("--lessons-file", help="JSON-файл с выбранными уроками")
    args = parser.parse_args()

    save_root = args.save_path
    quality_setting = args.quality

    if args.lessons_file:
        with open(args.lessons_file, "r", encoding="utf-8") as f:
            entries = json.load(f)
    else:
        if not _COURSES_PATH.exists() or _COURSES_PATH.stat().st_size == 0:
            print("Файл courses.json пустой или отсутствует.")
            return

        with open(_COURSES_PATH, "r", encoding="utf-8") as courses_file:
            courses = json.load(courses_file)

        entries = []
        for course in courses:
            for lesson in course["lessons"]:
                entries.append({
                    "course_title": course["course_title"],
                    "lesson": lesson,
                })

    async with async_playwright() as playwright:
        browser = await playwright.firefox.launch_persistent_context(
            USER_DATA_DIR,
            headless=False,
        )

        for entry in entries:
            course_title = entry["course_title"]
            lesson = entry["lesson"]
            await process_lesson(
                browser,
                course_title,
                lesson,
                save_root,
                quality_setting,
            )

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
