from __future__ import annotations

import errno
import hashlib
import os
import stat
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Event

_VIDEO_EXTENSIONS = frozenset(
    {".avi", ".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm", ".wmv"}
)
_CHUNK_SIZE = 1024 * 1024
_PROGRESS_INTERVAL = 0.15


@dataclass(frozen=True, slots=True)
class VideoCopy:
    path: Path
    stamp: tuple[int, int, int, int]

    @property
    def size(self) -> int:
        return self.stamp[2]


@dataclass(frozen=True, slots=True)
class DuplicateProgress:
    video_count: int
    candidate_count: int | None = None
    completed: int = 0
    bytes_read: int = 0
    bytes_total: int = 0


@dataclass(frozen=True, slots=True)
class DuplicateScan:
    folder: Path
    groups: tuple[tuple[VideoCopy, ...], ...]
    errors: tuple[str, ...]
    video_count: int


@dataclass(frozen=True, slots=True)
class DuplicateRemoval:
    deleted: int
    removed_folders: int
    errors: tuple[str, ...]


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _stamp(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _check_unchanged(copy: VideoCopy) -> None:
    info = copy.path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or _is_link(info)
        or _stamp(info) != copy.stamp
        or copy.path.resolve(strict=True) != copy.path
    ):
        raise OSError("файл или его расположение изменились")


def _hash(copy: VideoCopy, cancel: Event, on_read: Callable[[int], None]) -> bytes | None:
    _check_unchanged(copy)
    digest = hashlib.sha256()
    with copy.path.open("rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != copy.stamp:
            raise OSError("файл изменился перед чтением")
        remaining = copy.size
        while remaining and not cancel.is_set():
            chunk = stream.read(min(_CHUNK_SIZE, remaining))
            if not chunk:
                raise OSError("файл изменился во время чтения")
            digest.update(chunk)
            remaining -= len(chunk)
            on_read(len(chunk))
        if cancel.is_set():
            return None
        _check_unchanged(copy)
        return digest.digest()


def _describe_error(folder: Path, path: Path, error: OSError) -> str:
    return f"{path.relative_to(folder)} — {error.strerror or error}"


def scan_duplicate_videos(
    folder: Path,
    cancel: Event,
    on_progress: Callable[[DuplicateProgress], None] | None = None,
) -> DuplicateScan:
    folder = folder.resolve(strict=True)
    if not folder.is_dir():
        raise NotADirectoryError("Выберите папку с видео.")

    errors: list[str] = []
    by_size: dict[int, list[VideoCopy]] = defaultdict(list)
    video_count = completed = bytes_read = bytes_total = 0
    candidate_count: int | None = None
    last_update = 0.0

    def publish(*, force: bool = False) -> None:
        nonlocal last_update
        if on_progress is None or cancel.is_set():
            return
        now = time.monotonic()
        if force or now - last_update >= _PROGRESS_INTERVAL:
            last_update = now
            on_progress(
                DuplicateProgress(video_count, candidate_count, completed, bytes_read, bytes_total)
            )

    def record_read(size: int) -> None:
        nonlocal bytes_read
        bytes_read += size
        publish()

    def on_error(error: OSError) -> None:
        path = Path(error.filename) if error.filename else folder
        errors.append(_describe_error(folder, path, error))

    publish(force=True)
    for directory, subdirectories, filenames in folder.walk(on_error=on_error):
        if cancel.is_set():
            break
        # Prune directory links and junctions before Path.walk descends into them.
        regular_directories: list[str] = []
        for name in subdirectories:
            try:
                if not _is_link((directory / name).lstat()):
                    regular_directories.append(name)
            except OSError as error:
                errors.append(_describe_error(folder, directory / name, error))
        subdirectories[:] = regular_directories
        for name in filenames:
            if cancel.is_set():
                break
            path = directory / name
            if path.suffix.lower() not in _VIDEO_EXTENSIONS:
                continue
            try:
                info = path.lstat()
                if stat.S_ISREG(info.st_mode) and not _is_link(info) and info.st_size > 0:
                    by_size[info.st_size].append(VideoCopy(path, _stamp(info)))
                    video_count += 1
                    publish()
            except OSError as error:
                errors.append(_describe_error(folder, path, error))

    candidates_by_size = {size: copies for size, copies in by_size.items() if len(copies) > 1}
    candidate_count = sum(map(len, candidates_by_size.values()))
    bytes_total = sum(size * len(copies) for size, copies in candidates_by_size.items())
    if candidate_count:
        publish(force=True)
    groups: list[tuple[VideoCopy, ...]] = []
    for candidates in candidates_by_size.values():
        if cancel.is_set():
            break
        by_hash: dict[bytes, list[VideoCopy]] = defaultdict(list)
        for copy in candidates:
            if cancel.is_set():
                break
            try:
                digest = _hash(copy, cancel, record_read)
                if digest is not None:
                    by_hash[digest].append(copy)
            except OSError as error:
                errors.append(_describe_error(folder, copy.path, error))
            completed += 1
            publish()
        groups.extend(
            tuple(sorted(copies, key=lambda copy: copy.path))
            for copies in by_hash.values()
            if len(copies) > 1
        )
    groups.sort(key=lambda group: (-(len(group) - 1) * group[0].size, group[0].path))
    return DuplicateScan(folder, tuple(groups), tuple(errors), video_count)


def delete_duplicate_videos(
    scan: DuplicateScan, selected: frozenset[Path], cancel: Event
) -> DuplicateRemoval:
    deleted = 0
    errors: list[str] = []
    affected_folders: set[Path] = set()
    for group in scan.groups:
        targets = [copy for copy in group if copy.path in selected]
        if not targets:
            continue
        keepers = [copy for copy in group if copy.path not in selected]
        for copy in targets:
            if cancel.is_set():
                return DuplicateRemoval(deleted, 0, tuple(errors))
            try:
                _check_unchanged(copy)
                # Keep at least one still-valid copy, including after a partial deletion.
                while keepers:
                    try:
                        _check_unchanged(keepers[-1])
                        break
                    except OSError:
                        keepers.pop()
                if not keepers:
                    raise OSError("не найдена неизменённая копия, которую можно оставить")
                copy.path.unlink()
                deleted += 1
                for directory in copy.path.parents:
                    if directory == scan.folder:
                        break
                    affected_folders.add(directory)
            except OSError as error:
                errors.append(_describe_error(scan.folder, copy.path, error))

    removed_folders = 0
    for directory in sorted(affected_folders, key=lambda path: len(path.parts), reverse=True):
        if cancel.is_set():
            break
        try:
            if _is_link(directory.lstat()) or directory.resolve(strict=True) != directory:
                raise OSError("расположение папки изменилось")
            directory.rmdir()
        except OSError as error:
            if not isinstance(error, FileNotFoundError) and error.errno != errno.ENOTEMPTY:
                errors.append(_describe_error(scan.folder, directory, error))
        else:
            removed_folders += 1
    return DuplicateRemoval(deleted, removed_folders, tuple(errors))
