from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

from getcourse_downloader.infrastructure.platform.paths import AppPaths


def _mp4_boxes(data: bytes, start: int, end: int) -> Iterator[tuple[bytes, int, int, int]]:
    position = start
    while position + 8 <= end:
        size = int.from_bytes(data[position : position + 4], "big")
        header_size = 8
        if size == 1:
            if position + 16 > end:
                return
            size = int.from_bytes(data[position + 8 : position + 16], "big")
            header_size = 16
        elif size == 0:
            size = end - position
        if size < header_size or position + size > end:
            return
        yield data[position + 4 : position + 8], position, position + header_size, position + size
        position += size


def normalize_empty_saiz(data: bytes) -> bytes:
    """Hide empty auxiliary-size boxes that FFmpeg rejects before reading cbcs samples."""

    changed: bytearray | None = None
    for kind, _, moof_content, moof_end in _mp4_boxes(data, 0, len(data)):
        if kind != b"moof":
            continue
        for child_kind, _, traf_content, traf_end in _mp4_boxes(data, moof_content, moof_end):
            if child_kind != b"traf":
                continue
            children = list(_mp4_boxes(data, traf_content, traf_end))
            has_saio = any(box_kind == b"saio" for box_kind, *_ in children)
            has_samples = any(
                box_kind == b"senc"
                and box_end - content >= 8
                and int.from_bytes(data[content + 4 : content + 8], "big") > 0
                for box_kind, _, content, box_end in children
            )
            if not (has_saio and has_samples):
                continue
            for box_kind, box_start, content, box_end in children:
                if box_kind == b"saiz" and data[content:box_end] == b"\x00" * 9:
                    if changed is None:
                        changed = bytearray(data)
                    changed[box_start + 4 : box_start + 8] = b"free"
    return bytes(changed) if changed is not None else data


def _supports_sample_aes(version_output: str) -> bool:
    release = re.search(r"^ffmpeg version (\d+)\.(\d+)", version_output)
    if release is not None:
        return (int(release.group(1)), int(release.group(2))) >= (6, 1)
    libavformat = re.search(r"^libavformat\s+(\d+)\.", version_output, flags=re.MULTILINE)
    return libavformat is not None and int(libavformat.group(1)) >= 60


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

    async def sample_aes_support(self) -> tuple[bool, str]:
        flags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            self.executable(),
            "-version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=flags,
        )
        try:
            stdout, _ = await asyncio.wait_for(process.communicate(), timeout=15)
        except TimeoutError:
            return False, "Не удалось проверить версию FFmpeg"
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        output = stdout.decode("utf-8", errors="replace")
        if process.returncode == 0 and _supports_sample_aes(output):
            return True, ""
        return False, "Для расшифровки SAMPLE-AES требуется FFmpeg 6.1 или новее"

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

    async def decrypt_file(
        self,
        source: Path,
        destination: Path,
        *,
        decryption_key_hex: str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[bool, str]:
        self._validate_decryption_key(decryption_key_hex)
        return await self._run_mux(
            (
                "-y",
                "-v",
                "error",
                "-decryption_key",
                decryption_key_hex,
                "-i",
                str(source),
                "-map",
                "0",
                "-c",
                "copy",
                str(destination),
            ),
            is_cancelled=is_cancelled,
            redactions=(decryption_key_hex,),
        )

    async def merge_audio_video(
        self,
        video: Path,
        audio: Path,
        destination: Path,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[bool, str]:
        return await self._run_mux(
            (
                "-y",
                "-v",
                "error",
                "-i",
                str(video),
                "-i",
                str(audio),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c",
                "copy",
                str(destination),
            ),
            is_cancelled=is_cancelled,
        )

    async def decrypt_fragments(
        self,
        sources: Sequence[Path],
        destination: Path,
        *,
        decryption_key_hex: str,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> tuple[bool, str]:
        self._validate_decryption_key(decryption_key_hex)
        flags = getattr(__import__("subprocess"), "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        process = await asyncio.create_subprocess_exec(
            self.executable(),
            "-y",
            "-v",
            "error",
            "-decryption_key",
            decryption_key_hex,
            "-i",
            "pipe:0",
            "-map",
            "0",
            "-c",
            "copy",
            str(destination),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            creationflags=flags,
        )
        communicate: asyncio.Task[tuple[bytes, bytes]] | None = None
        try:
            if process.stdin is None:
                return False, "ffmpeg stdin недоступен"
            for source_path in sources:
                data = normalize_empty_saiz(source_path.read_bytes())
                for offset in range(0, len(data), 1024 * 1024):
                    if is_cancelled and is_cancelled():
                        return False, "cancelled"
                    process.stdin.write(data[offset : offset + 1024 * 1024])
                    await process.stdin.drain()
            process.stdin.close()
            wait_closed = getattr(process.stdin, "wait_closed", None)
            if wait_closed is not None:
                await wait_closed()
            communicate = asyncio.create_task(process.communicate())
            started_at = asyncio.get_running_loop().time()
            while not communicate.done():
                if is_cancelled and is_cancelled():
                    return False, "cancelled"
                if asyncio.get_running_loop().time() - started_at >= 300:
                    return False, "ffmpeg завис (таймаут 5 минут)"
                await asyncio.sleep(0.1)
            _, stderr = await communicate
        except (BrokenPipeError, ConnectionResetError, OSError) as error:
            return False, str(error).replace(decryption_key_hex, "[redacted]")
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                if communicate is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await communicate
                await process.wait()
        if process.returncode != 0:
            return False, stderr.decode("utf-8", errors="replace")[-300:].replace(
                decryption_key_hex, "[redacted]"
            )
        return True, ""

    @staticmethod
    def _validate_decryption_key(value: str) -> None:
        if len(value) != 32 or re.fullmatch(r"[0-9a-fA-F]{32}", value) is None:
            raise ValueError("FFmpeg decryption key must contain 32 hexadecimal characters")

    async def _run_mux(
        self,
        arguments: tuple[str, ...],
        *,
        is_cancelled: Callable[[], bool] | None,
        redactions: tuple[str, ...] = (),
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
            message = stderr.decode("utf-8", errors="replace")[-300:]
            for secret in redactions:
                message = message.replace(secret, "[redacted]")
            return False, message
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
