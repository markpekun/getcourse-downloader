from __future__ import annotations

import hashlib
import re
from pathlib import Path

_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _shorten_component(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    if max_length < 12:
        raise ValueError("max_length must be at least 12")
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    prefix = value[: max_length - len(digest) - 1].rstrip(". ")
    return f"{prefix}~{digest}"


def sanitize_filename(
    name: str,
    *,
    fallback: str = "video",
    max_length: int = 180,
) -> str:
    clean = re.sub(
        r"\b(Просмотрено|Пройдено|Завершено)\b",
        "",
        name,
        flags=re.IGNORECASE,
    )
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"[\\/*?:\"<>|]", "_", clean).rstrip(". ")
    if not clean:
        clean = fallback
    if clean.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED:
        clean = f"_{clean}"
    return _shorten_component(clean, max_length)


def safe_lesson_output_stem(
    save_root: Path,
    course_path: tuple[str, ...],
    lesson_title: str,
    *,
    max_path_length: int = 200,
) -> Path:
    """Build a nested, Windows-safe output stem while retaining every tree level."""

    raw_parts = (*course_path, lesson_title)
    if not raw_parts:
        raise ValueError("course path and lesson title cannot both be empty")

    root_length = len(str(save_root.resolve()))
    separators = len(raw_parts)
    available = max_path_length - root_length - separators
    if available < len(raw_parts) * 12:
        raise ValueError("Путь сохранения слишком длинный для Windows")
    component_limit = min(80, available // len(raw_parts))

    safe_parts = [
        sanitize_filename(
            part,
            fallback="course" if index < len(raw_parts) - 1 else "video",
            max_length=component_limit,
        )
        for index, part in enumerate(raw_parts)
    ]
    return save_root.joinpath(*safe_parts)


def collision_safe_stem(stem: Path, lesson_url: str) -> Path:
    """Add a stable suffix when two selected lessons sanitize to the same path."""

    return stem.with_name(collision_safe_component(stem.name, lesson_url))


def collision_safe_component(value: str, stable_identity: str) -> str:
    """Disambiguate a path component without exceeding its allocated length."""

    digest = hashlib.sha256(stable_identity.encode("utf-8")).hexdigest()[:8]
    target_length = max(12, len(value))
    prefix = value[: target_length - len(digest) - 1].rstrip(". ") or "item"
    return f"{prefix}~{digest}"


def quality_suffixed_path(stem: Path, quality: str) -> Path:
    """Return a readable MP4 path using a known actual/selected video height."""

    match = re.fullmatch(r"(\d{3,4})p?", quality.strip(), flags=re.IGNORECASE)
    suffix = f"_{match.group(1)}" if match else ""
    return stem.with_name(f"{stem.name}{suffix}.mp4")


def existing_output_path(stem: Path) -> Path | None:
    """Find a legacy or quality-suffixed completed MP4 for one output stem."""

    legacy = stem.with_name(f"{stem.name}.mp4")
    try:
        if legacy.is_file() and legacy.stat().st_size > 0:
            return legacy
        pattern = re.compile(rf"^{re.escape(stem.name)}_(\d{{3,4}})\.mp4$", re.IGNORECASE)
        candidates = sorted(
            path
            for path in stem.parent.iterdir()
            if path.is_file() and pattern.fullmatch(path.name) and path.stat().st_size > 0
        )
    except OSError:
        return None
    return candidates[0] if candidates else None
