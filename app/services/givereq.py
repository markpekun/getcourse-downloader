from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
from typing import Any

from pathlib import Path

from playwright.async_api import Frame, async_playwright

from utils_console import configure_console_output

USER_DATA_DIR = "session_data"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_COURSES_PATH = _PROJECT_ROOT / "app" / "data" / "courses.json"

configure_console_output()


def _extract_video_id(url: str) -> str:
    match = re.search(r"/api/playlist/media/([^/?#]+)/", url)
    return match.group(1) if match else url


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
    try:
        await button.first.wait_for(state="attached", timeout=8000)
        await frame.evaluate("(el)=>el.click()", await button.first.element_handle())
    except Exception:
        pass


async def _handle_player_frame(frame: Frame) -> bool:
    if not await frame.query_selector(".vpl-root"):
        return False
    if not await frame.query_selector(".mst-root"):
        return False
    await _click_modal_if_present(frame)
    await _click_play(frame)
    return True


async def _switch_to_highest_quality(page) -> bool:
    player_frames = [
        fr for fr in page.frames
        if "vhcdn.com" in (fr.url or "") or "gceuproxy.com" in (fr.url or "")
    ]
    target_frames = player_frames or [page]

    for fi, frame in enumerate(target_frames):
        frame_url = frame.url

        for quality in ["2160", "1440", "1080", "720"]:
            try:
                els = frame.get_by_text(quality, exact=False)
                count = await els.count()
                if count > 0:
                    for i in range(count):
                        try:
                            await els.nth(i).click()
                            await asyncio.sleep(0.5)
                            return True
                        except Exception as e:
                            continue
            except Exception as e:
                continue

        js_find = """
        (() => {
            const ALL_TAGS = ['button', 'span', 'div', 'a', 'li', 'label', 'i'];
            const result = [];
            function walk(node, depth) {
                if (depth > 10) return;
                if (!node || !node.tagName) return;
                const text = (node.textContent || '').trim();
                if (ALL_TAGS.includes(node.tagName.toLowerCase())) {
                    const match = text.match(/\\b(2160|1440|1080|720|480|360|240)\\b/);
                    if (match && text.length < 20) {
                        result.push({
                            tag: node.tagName,
                            text: text,
                            classes: (node.className || '').slice(0, 60)
                        });
                    }
                }
                for (let c of node.children) walk(c, depth + 1);
            }
            walk(document.body, 0);
            return result;
        })()
        """
        try:
            elements = await frame.evaluate(js_find)
        except Exception:
            pass

        settings_selectors = [
            ".vpl-settings-button",
            ".vpl-quality-btn",
            ".vpl-quality-button",
            ".vpl-settings",
            "button.settings-button",
            "button.vjs-quality-button",
            "button.vjs-settings-button",
            "[class*='quality']",
            "[class*='Quality']",
            "[class*='settings']",
            "[class*='Settings']",
            "button:has(.vjs-icon-cog)",
            ".vjs-control-bar button:has-text('720'), .vjs-control-bar button:has-text('1080'), .vjs-control-bar button:has-text('480')",
        ]
        for sel in settings_selectors:
            try:
                btn = frame.locator(sel).first
                if await btn.count():
                    await btn.click()
                    await asyncio.sleep(0.5)
                    for quality in ["1080", "720"]:
                        try:
                            opt = frame.get_by_text(quality, exact=False).first
                            if await opt.count():
                                await opt.click()
                                await asyncio.sleep(0.5)
                                return True
                        except Exception:
                            continue
            except Exception:
                continue

    js_code = """
    (() => {
        let changed = false;
        const candidates = [
            window.player,
            window.videojs && document.querySelector('video') && videojs(document.querySelector('video').id),
            window.vjs_player,
            document.querySelector('.vpl-root') && document.querySelector('.vpl-root').__vue__,
        ];
        for (const p of candidates) {
            if (!p) continue;
            if (typeof p.setQuality === 'function') { p.setQuality(1080); changed = true; }
            if (p.qualityLevels && typeof p.qualityLevels === 'function') {
                try {
                    const ql = p.qualityLevels();
                    if (ql && ql.length) { ql.selectedIndex = ql.length - 1; changed = true; }
                } catch(e) {}
            }
            if (typeof p.quality === 'function') { p.quality(1080); changed = true; }
            if (p.currentLevel !== undefined) { p.currentLevel = -1; changed = true; }
        }
        const v = document.querySelector('video');
        if (v) {
            v.preload = 'auto';
            v.autoplay = true;
            const src = v.src || (v.querySelector('source') && v.querySelector('source').src);
            if (src && src.includes('/480')) {
                const newSrc = src.replace('/480', '/1080');
                v.src = newSrc;
                v.load();
                changed = true;
            }
        }
        return changed;
    })()
    """
    for frame in target_frames:
        try:
            result = await frame.evaluate(js_code)
            if result:
                return True
        except Exception:
            pass

    return False


async def _probe_best_quality(
    playlist_url: str,
    browser,
    max_quality: int | None = None,
) -> str:
    match = re.search(r"/(\d{3,4})\?", playlist_url)
    if not match:
        return playlist_url

    base_url = playlist_url.replace(f"/{match.group(1)}?", "/{}?")
    current_q = int(match.group(1))
    candidates = [q for q in [480, 720, 1080] if q > current_q]
    if max_quality is not None:
        candidates = [q for q in candidates if q <= max_quality]

    best_url = playlist_url

    async def _fetch(url: str, label: str) -> str | None:
        p = await browser.new_page()
        try:
            resp = await p.goto(url, timeout=15000)
            if resp and resp.ok:
                body = await p.evaluate("document.body.innerText")
                return body
            else:
                return None
        except Exception as e:
            return None
        finally:
            await p.close()

    ref_body = await _fetch(playlist_url, f"{current_q}p")
    if ref_body is None:
        return playlist_url

    for q in candidates:
        test_url = base_url.format(q)
        try:
            body = await _fetch(test_url, f"{q}p")
            if body is None:
                break
            if not body.strip().startswith("#EXTM3U"):
                break
            if body == ref_body:
                break
            best_url = test_url
            current_q = q
            ref_body = body
        except Exception:
            break

    return best_url


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
    await page.goto(lesson_url)

    for attempt in range(3):
        if "login" not in page.url.lower() and "required=true" not in page.url:
            break
        print("\n  Требуется авторизация. Войдите в аккаунт в браузере.")
        print("  Через 30 секунд страница перезагрузится...")
        await asyncio.sleep(30)
        await page.goto(lesson_url)

    best: dict[str, tuple[int, str]] = {}
    intercepted_urls: list[str] = []
    seen_interesting_urls: set[str] = set()
    pending_tasks: set[asyncio.Task] = set()

    async def on_request(request):
        url = request.url
        if _is_media_playlist_request(url) and url not in seen_interesting_urls:
            seen_interesting_urls.add(url)
            if len(intercepted_urls) < 200:
                intercepted_urls.append(url)

        if _is_media_playlist_request(url):
            video_id = _extract_video_id(url)
            quality_val = _extract_quality(url)
            current = best.get(video_id)
            if current is None or quality_val > current[0]:
                best[video_id] = (quality_val, url)

    def on_request_handler(req):
        task = asyncio.create_task(on_request(req))
        pending_tasks.add(task)
        task.add_done_callback(pending_tasks.discard)

    page.on("request", on_request_handler)

    for frame in [fr for fr in page.frames if "vhcdn.com" in (fr.url or "") or "gceuproxy.com" in (fr.url or "")]:
        try:
            await _handle_player_frame(frame)
        except Exception:
            pass

    await asyncio.sleep(3)

    await _switch_to_highest_quality(page)
    await asyncio.sleep(5)
    await page.close()

    if pending_tasks:
        await asyncio.wait(pending_tasks, timeout=2)

    videos = [url for quality, url in sorted(best.values(), key=lambda x: -x[0])]
    if not videos:
        print("  Видео не найдено")
        return

    highest_q = _extract_quality(videos[0])
    all_found = sorted(set(_extract_quality(url) for _, url in best.values()))
    target_q = int(quality_filter) if (quality_filter and quality_filter != "auto") else 1080
    if highest_q < target_q:
        videos = [await _probe_best_quality(url, browser, max_quality=target_q if quality_filter != "auto" else None) for url in videos]

    if quality_filter and quality_filter != "auto":
        max_q = int(quality_filter)
        videos = [url for url in videos if _extract_quality(url) <= max_q]
        if not videos:
            print(f"  Видео не найдено (фильтр качества: {quality_filter})")
            return

    course_path = os.path.join(save_root, course_title)
    os.makedirs(course_path, exist_ok=True)
    safe_title = sanitize_filename(lesson_title)

    if len(videos) == 1:
        q = _extract_quality(videos[0])
        print(f"  Качество: {q}p")
        await _download_video(videos[0], os.path.join(course_path, safe_title))
        return

    lesson_path = os.path.join(course_path, safe_title)
    os.makedirs(lesson_path, exist_ok=True)
    for index, video_url in enumerate(videos, start=1):
        q = _extract_quality(video_url)
        print(f"  video_{index}: {q}p")
        await _download_video(video_url, os.path.join(lesson_path, f"video_{index}"))


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
