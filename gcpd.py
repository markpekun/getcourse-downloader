import os
import re
import sys
import tempfile
from urllib.parse import urljoin
import aiohttp
import asyncio
from tqdm import tqdm
import subprocess
import argparse
from utils_console import configure_console_output

MAX_PARALLEL_DOWNLOADS = 4

configure_console_output()


def merge_video_audio(video_file, audio_file, output_file):
    print("Объединение видео и аудио дорожек...")
    try:
        subprocess.run(
            ['ffmpeg', '-i', video_file, '-i', audio_file, '-c', 'copy', '-shortest', output_file],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        print(f"Объединение завершено: {output_file}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"Ошибка при объединении: {e}")
        return False


async def download_kinescope_tracks(video_url, audio_url, result_file):
    video_m3u8 = video_url
    audio_m3u8 = audio_url

    with tempfile.TemporaryDirectory() as tmpdir:
        video_file = os.path.join(tmpdir, 'video.mp4')
        audio_file = os.path.join(tmpdir, 'audio.mp4')

        print(f"Скачивание видео дорожки...")
        print(f"URL: {video_m3u8}")
        subprocess.run([
            'yt-dlp', '--add-header', 'Referer:https://kinescope.io',
            '--add-header', 'Origin:https://kinescope.io',
            '-o', video_file, video_m3u8
        ], check=True)

        print(f"\nСкачивание аудио дорожки...")
        print(f"URL: {audio_m3u8}")
        subprocess.run([
            'yt-dlp', '--add-header', 'Referer:https://kinescope.io',
            '--add-header', 'Origin:https://kinescope.io',
            '-o', audio_file, audio_m3u8
        ], check=True)

        output_file = result_file if result_file.endswith('.mp4') else result_file + '.mp4'
        if merge_video_audio(video_file, audio_file, output_file):
            print(f"Готово: {output_file}")
        else:
            print("Не удалось объединить дорожки")


async def download_file(session, url, destination, progress_bar):
    async with session.get(url) as response:
        response.raise_for_status()
        total_size = int(response.headers.get('content-length', 0))
        with open(destination, 'wb') as file:
            downloaded = 0
            async for chunk in response.content.iter_chunked(64*1024):
                file.write(chunk)
                downloaded += len(chunk)
                progress_bar.update(len(chunk))


async def download_segment(session, ts_url, tmpdir, idx, overall_progress, semaphore, count_segments=False):
    async with semaphore:
        ts_file = os.path.join(tmpdir, f'{idx:05}.ts')
        for attempt in range(3):
            try:
                async with session.get(ts_url) as response:
                    response.raise_for_status()
                    with open(ts_file, 'wb') as file:
                        async for chunk in response.content.iter_chunked(64*1024):
                            file.write(chunk)
                            if not count_segments:
                                overall_progress.update(len(chunk))
                if count_segments:
                    overall_progress.update(1)
                return ts_file
            except aiohttp.ClientError:
                if attempt == 2:
                    raise
                await asyncio.sleep(1)


def _extract_segment_urls(content, base_url):
    urls = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if re.search(r'\.(ts|bin)(\?|$)', line):
            urls.append(urljoin(base_url, line))
    return urls


def _extract_playlist_candidate(content, base_url):
    variant_url = resolve_master_playlist(content, base_url)
    if variant_url:
        return variant_url

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    non_comment = [line for line in lines if not line.startswith('#')]
    if non_comment:
        tail = non_comment[-1]
        return urljoin(base_url, tail)

    return None


async def get_total_size(session, urls):
    if not urls:
        return 0
    total_size = 0
    async with session.head(urls[0]) as response:
        size = int(response.headers.get('content-length', 0))
    if size == 0:
        return None
    for url in tqdm(urls, desc="Получение размеров файлов", unit="file"):
        async with session.head(url) as response:
            total_size += int(response.headers.get('content-length', 0))
    return total_size


def _parse_resolution(resolution_str):
    if not resolution_str:
        return (0, 0)
    m = re.match(r'(\d+)x(\d+)', resolution_str.strip())
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def resolve_master_playlist(content, base_url):
    variants = []
    lines = content.strip().split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith('#EXT-X-STREAM-INF:'):
            attrs = {}
            rest = line[len('#EXT-X-STREAM-INF:'):].strip()
            for part in rest.split(','):
                part = part.strip()
                if '=' in part:
                    k, v = part.split('=', 1)
                    attrs[k.strip()] = v.strip()
            resolution = attrs.get('RESOLUTION', '')
            bandwidth = int(attrs.get('BANDWIDTH', 0) or '0')
            width, height = _parse_resolution(resolution)
            i += 1
            if i < len(lines) and not lines[i].startswith('#'):
                uri = lines[i].strip()
                if uri:
                    resolved = urljoin(base_url, uri)
                    variants.append((resolved, height, bandwidth))
            i += 1
            continue
        i += 1
    if not variants:
        return None
    variants.sort(key=lambda v: (v[1], v[2]), reverse=True)
    return variants[0][0]


async def try_download_with_quality(url, result_file, quality=None, no_pre_download=True):
    await main(url, result_file, no_pre_download)


def convert_to_mp4(result_file, max_retries=3):
    mp4_file = result_file + '.mp4'
    for attempt in range(max_retries):
        print(f"Попытка конвертации в MP4 ({attempt + 1}/{max_retries})...")
        try:
            process = subprocess.Popen(
                ['ffmpeg', '-i', result_file, '-c', 'copy', mp4_file],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                universal_newlines=False
            )

            while True:
                output = process.stderr.readline()
                if output == b'' and process.poll() is not None:
                    break
                if output:
                    try:
                        line = output.decode('utf-8').strip()
                    except UnicodeDecodeError:
                        line = output.decode('utf-8', errors='replace').strip()
                    if "Duration" in line or "time=" in line:
                        print(line)

            if process.returncode == 0:
                print(f"Конвертация завершена. Результат здесь:\n{mp4_file}")
                os.remove(result_file)
                print(f"Файл {result_file} удалён.")
                return True

            error_output = process.stderr.read()
            try:
                error_output = error_output.decode('utf-8')
            except UnicodeDecodeError:
                error_output = error_output.decode('utf-8', errors='replace')
            print(f"Ошибка при конвертации файла: {error_output}")

            if os.path.exists(mp4_file):
                os.remove(mp4_file)
                print(f"Неполный файл {mp4_file} удалён.")

            if attempt < max_retries - 1:
                print(f"Повторная попытка через 5 секунд...")
                import time
                time.sleep(5)

        except Exception as e:
            print(f"Произошла ошибка: {str(e)}")
            if os.path.exists(mp4_file):
                os.remove(mp4_file)
                print(f"Неполный файл {mp4_file} удалён.")
            if attempt < max_retries - 1:
                print(f"Повторная попытка через 5 секунд...")
                import time
                time.sleep(5)

    print("Достигнуто максимальное количество попыток. Конвертация не удалась.")
    return False


async def main(url, result_file, no_pre_download):
    async with aiohttp.ClientSession() as session:
        with tempfile.TemporaryDirectory() as tmpdir:
            main_playlist = os.path.join(tmpdir, 'main_playlist.m3u8')

            print("Загрузка основного плейлиста...")
            with tqdm(total=None, desc="Основной плейлист", unit="B", unit_scale=True) as pbar:
                await download_file(session, url, main_playlist, pbar)

            with open(main_playlist, 'r', encoding='utf-8') as f:
                main_playlist_content = f.read()

            second_playlist = os.path.join(tmpdir, 'second_playlist.m3u8')
            ts_urls = _extract_segment_urls(main_playlist_content, url)

            if ts_urls:
                with open(second_playlist, 'w', encoding='utf-8') as f:
                    f.write("\n".join(ts_urls))
            else:
                playlist_url = _extract_playlist_candidate(main_playlist_content, url)
                if not playlist_url:
                    print("Не удалось найти ссылки на сегменты или плейлист.")
                    print("Проверьте ссылку или опишите проблему:")
                    print("https://github.com/snhplayer/GetCoursePythonDownloader/issues")
                    return

                print("Загрузка вторичного плейлиста...")
                with tqdm(total=None, desc="Вторичный плейлист", unit="B", unit_scale=True) as pbar:
                    await download_file(session, playlist_url, second_playlist, pbar)

                with open(second_playlist, 'r', encoding='utf-8') as f:
                    second_playlist_content = f.read()
                ts_urls = _extract_segment_urls(second_playlist_content, playlist_url)

            print(f"Число сегментов для загрузки: {len(ts_urls)}")
            if not ts_urls:
                print("Не удалось извлечь ссылки на сегменты из плейлиста.")
                return

            total_size = None
            if not no_pre_download:
                total_size = await get_total_size(session, ts_urls)

            semaphore = asyncio.Semaphore(MAX_PARALLEL_DOWNLOADS)

            if no_pre_download:
                overall_pbar = tqdm(total=len(ts_urls), desc="Общий прогресс", unit="сегмент")
            else:
                overall_pbar = tqdm(total=total_size, desc="Общий прогресс", unit="B", unit_scale=True)

            tasks = [download_segment(session, ts_url, tmpdir, idx, overall_pbar, semaphore, count_segments=no_pre_download)
                     for idx, ts_url in enumerate(ts_urls)]
            ts_files = []
            for task in asyncio.as_completed(tasks):
                ts_file = await task
                ts_files.append(ts_file)

            overall_pbar.close()

            print("Объединение сегментов...")
            with open(result_file, 'wb') as result:
                for ts_file in tqdm(sorted(ts_files), desc="Объединение", unit="file"):
                    with open(ts_file, 'rb') as ts:
                        result.write(ts.read())

            print(f"Скачивание завершено. Результат здесь:\n{result_file}")

            if convert_to_mp4(result_file):
                print("Конвертация успешно завершена.")
            else:
                print("Не удалось выполнить конвертацию после нескольких попыток.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Download and process video segments.')
    parser.add_argument('--pd', action='store_false', dest='no_pre_download',
                        help='Включить предварительную загрузку размеров (по умолчанию отключено)')
    parser.add_argument('--url', type=str, dest='url', default=None,
                        help='Одиночный URL плейлиста для скачивания')
    parser.add_argument('--output', type=str, dest='output', default=None,
                        help='Имя выходного файла для --url')
    parser.add_argument('-f', type=str, dest='file',
                        help='Указать файл где находятся ссылки плей-листов и имена выходных файлов', default=False)
    args = parser.parse_args()

    if args.url:
        if not args.output:
            parser.error("Для --url нужно указать --output")
        asyncio.run(main(args.url, args.output, args.no_pre_download))
        sys.exit(0)

    if args.file != False:
        url = ""
        result_file = ""
        if not os.path.exists(args.file):
            print("Файл для скачивания не существует")
            sys.exit(1)

        lines = [line.strip() for line in open(args.file, encoding="utf-8") if line.strip()]
        i = 0

        def is_kinescope_video_url(s):
            return s.startswith("http") and "kinescope.io" in s and "type=video" in s

        def is_kinescope_audio_url(s):
            return s.startswith("http") and "kinescope.io" in s and "type=audio" in s

        def is_getcourse_url(s):
            if not (s and s.startswith("http")):
                return False
            if is_kinescope_video_url(s) or is_kinescope_audio_url(s):
                return False
            return (
                "playlist.servicecdn.ru" in s
                or "/api/playlist/master/" in s
                or ".m3u8" in s
            )

        while i < len(lines):
            line = lines[i]

            if line.startswith("TITLE:"):
                current_page_title = line[6:].strip() or "video"
                i += 1
                urls = []
                while i < len(lines) and is_getcourse_url(lines[i]):
                    urls.append(lines[i])
                    i += 1
                for j, u in enumerate(urls):
                    result_file = f"{current_page_title} - {j + 1}"
                    print("Скачивание плей-листа: ", u)
                    print("В файл: ", result_file)
                    asyncio.run(main(u, result_file, args.no_pre_download))
                continue

            if is_kinescope_video_url(line) and i + 2 < len(lines):
                next_url = lines[i + 1]
                if is_kinescope_audio_url(next_url):
                    video_url = line
                    audio_url = next_url
                    result_file = lines[i + 2] if i + 2 < len(lines) else "output"
                    print(f"Обнаружены Kinescope дорожки")
                    print(f"Видео: {video_url}")
                    print(f"Аудио: {audio_url}")
                    print(f"Выходной файл: {result_file}")
                    asyncio.run(download_kinescope_tracks(video_url, audio_url, result_file))
                    i += 3
                    continue

            if is_getcourse_url(line):
                url = line
                result_file = lines[i + 1] if i + 1 < len(lines) else "output"
                print("Скачивание плей-листа: ", url)
                print("В файл: ", result_file)
                asyncio.run(main(url, result_file, args.no_pre_download))
                i += 2
                continue

            i += 1
    else:
        while True:
            url = input("Введите ссылку (плейлист или видео Kinescope): ")

            if 'kinescope.io' in url and 'type=video' in url:
                audio_url = input("Введите ссылку на аудио дорожку: ")
                result_file = input("Введите имя выходного файла: ")
                asyncio.run(download_kinescope_tracks(url, audio_url, result_file))
            else:
                result_file = input("Введите имя выходного файла: ")
                asyncio.run(main(url, result_file, args.no_pre_download))
