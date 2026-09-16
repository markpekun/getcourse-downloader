from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

from getcourse_downloader.infrastructure.platform.paths import AppPaths


class FfmpegMuxer:
    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths

    def executable(self) -> str:
        bundled = self._paths.resources / "ffmpeg.exe"
        if bundled.is_file():
            return str(bundled.resolve())
        system = shutil.which("ffmpeg")
        if system:
            return system
        raise FileNotFoundError(
            "ffmpeg не найден. Установите его в PATH или поместите ffmpeg.exe в resources/."
        )

    def probe_executable(self) -> str | None:
        bundled = self._paths.resources / "ffprobe.exe"
        if bundled.is_file():
            return str(bundled.resolve())
        return shutil.which("ffprobe")

    async def mux(
        self,
        source: Path,
        destination: Path,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[bool, str]:
        return await self._run_mux(
            ("-y", "-i", str(source), "-c", "copy", "-bsf:a", "aac_adtstoasc", str(destination)),
            is_cancelled=is_cancelled,
        )

    async def mux_concat(
        self,
        source: Path,
        destination: Path,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[bool, str]:
        return await self._run_mux(
            (
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(source),
                "-c",
                "copy",
                "-bsf:a",
                "aac_adtstoasc",
                str(destination),
            ),
            is_cancelled=is_cancelled,
        )

    async def _run_mux(
        self,
        arguments: tuple[str, ...],
        *,
        is_cancelled: Callable[[], bool] | None,
    ) -> tuple[bool, str]:
        flags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            self.executable(),
            *arguments,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=flags,
        )
        communicate = asyncio.create_task(process.communicate())
        started_at = asyncio.get_running_loop().time()
        try:
            while not communicate.done():
                if is_cancelled and is_cancelled():
                    return False, "cancelled"
                if asyncio.get_running_loop().time() - started_at >= 300:
                    return False, "ffmpeg завис (таймаут 5 минут)"
                await asyncio.sleep(0.1)
            _, stderr = await communicate
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                with contextlib.suppress(asyncio.CancelledError):
                    await communicate
                await process.wait()
        if process.returncode != 0:
            return False, stderr.decode("utf-8", errors="replace")[-300:]
        return True, ""

    async def probe_height(self, media: Path) -> int | None:
        executable = self.probe_executable()
        if not executable:
            return None
        flags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            executable,
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=height",
            "-of",
            "json",
            str(media),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=flags,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        except TimeoutError:
            return None
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if process.returncode != 0:
            return None
        try:
            payload = json.loads(stdout.decode("utf-8", errors="replace"))
            streams = payload.get("streams", [])
            height = streams[0].get("height") if streams else None
            return height if isinstance(height, int) and height > 0 else None
        except (AttributeError, IndexError, json.JSONDecodeError):
            return None
