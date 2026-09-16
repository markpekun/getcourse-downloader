"""Offline distribution checks using a disposable browser profile and synthetic media."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path

from playwright.async_api import async_playwright

from getcourse_downloader import __version__
from getcourse_downloader.infrastructure.browser.playwright import PlaywrightBrowserFactory
from getcourse_downloader.infrastructure.media.ffmpeg import FfmpegMuxer
from getcourse_downloader.infrastructure.platform.paths import AppPaths, is_frozen, project_root


async def check_runtime(paths: AppPaths) -> dict[str, str | int]:
    paths.ensure_runtime_directories()
    factory = PlaywrightBrowserFactory(paths)
    async with async_playwright() as playwright:
        browser = await factory.launch(playwright, headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("<title>GetCourse smoke test</title><p>Offline check</p>")
            if await page.title() != "GetCourse smoke test":
                raise RuntimeError("Firefox failed the offline page check")
        finally:
            await browser.close()

    muxer = FfmpegMuxer(paths)
    source = paths.data / "sample.ts"
    generated = subprocess.run(
        [
            muxer.executable(),
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:r=10",
            "-t",
            "1",
            "-c:v",
            "mpeg2video",
            "-f",
            "mpegts",
            str(source),
        ],
        capture_output=True,
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )
    if generated.returncode:
        raise RuntimeError(generated.stderr.decode("utf-8", errors="replace")[-500:])
    concat = paths.data / "segments.ffconcat"
    concat.write_text("ffconcat version 1.0\nfile 'sample.ts'\n", encoding="utf-8")
    output = paths.data / "sample.mp4"
    success, error = await muxer.mux_concat(concat, output)
    if not success or not output.is_file() or not output.stat().st_size:
        raise RuntimeError(f"FFmpeg failed the MP4 check: {error}")
    height = await muxer.probe_height(output)
    if height != 90:
        raise RuntimeError(f"FFprobe returned unexpected video height: {height}")
    if is_frozen():
        import flet_desktop

        archive = Path(flet_desktop.get_package_bin_dir()) / "flet-windows.zip"
        with zipfile.ZipFile(archive) as bundle:
            if "flet/flet.exe" not in bundle.namelist() or bundle.testzip() is not None:
                raise RuntimeError("Flet desktop runtime is missing or damaged")
    return {"firefox": "ok", "ffmpeg_concat": "ok", "ffprobe_height": height}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline check of bundled runtime components")
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args(argv)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {"version": __version__, "frozen": is_frozen()}
    try:
        with tempfile.TemporaryDirectory(prefix="gcd-self-test-", dir=args.report.parent) as folder:
            root = Path(folder)
            paths = AppPaths(root / "data", root / "profile", project_root() / "resources")
            report.update(asyncio.run(check_runtime(paths)))
        if __version__ == "0.0.0+local":
            raise RuntimeError("Application version metadata is missing")
        report["status"] = "ok"
    except Exception as error:
        report.update(status="failed", error=str(error))
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
