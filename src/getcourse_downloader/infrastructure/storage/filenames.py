from __future__ import annotations

import re
from collections.abc import Iterable
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
    return value[:max_length].rstrip(". ")


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
    clean = _shorten_component(clean, max_length) or _shorten_component(fallback, max_length)
    if clean.split(".", maxsplit=1)[0].upper() in _WINDOWS_RESERVED:
        clean = f"_{clean}"
    return clean


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


def collision_safe_stem(stem: Path, number: int) -> Path:
    """Add a readable number without exceeding the component's length budget."""

    return stem.with_name(collision_safe_component(stem.name, number))


def collision_safe_component(value: str, number: int) -> str:
    """Disambiguate a path component without exceeding its allocated length."""

    if number < 2:
        raise ValueError("collision number must be at least 2")
    suffix = f" ({number})"
    target_length = max(12, len(value))
    prefix = value[: target_length - len(suffix)].rstrip(". ") or "item"
    return f"{prefix}{suffix}"


def allocate_component_names(components: Iterable[tuple[str, str]]) -> dict[str, str]:
    """Assign sibling names by stable identity, reserving literal names like 'Module (2)'."""

    proposed: dict[str, str] = {}
    for identity, name in components:
        previous = proposed.get(identity)
        if previous is None or (len(name), name) < (len(previous), previous):
            proposed[identity] = name
    reserved = {name.casefold() for name in proposed.values()}
    used: set[str] = set()
    allocated: dict[str, str] = {}
    for identity in sorted(proposed):
        base = proposed[identity]
        name = base
        if name.casefold() in used:
            number = 2
            while True:
                name = collision_safe_component(base, number)
                if name.casefold() not in used and name.casefold() not in reserved:
                    break
                number += 1
        used.add(name.casefold())
        allocated[identity] = name
    return allocated


def quality_suffixed_path(stem: Path, quality: str) -> Path:
    """Return a readable MP4 path using a known actual/selected video height."""

    match = re.fullmatch(r"(\d{3,4})p?", quality.strip(), flags=re.IGNORECASE)
    suffix = f"_{match.group(1)}" if match else ""
    return stem.with_name(f"{stem.name}{suffix}.mp4")


def existing_output_paths(stem: Path) -> tuple[Path, ...]:
    """List nonempty completed MP4s, excluding partial and audio-only outputs."""

    legacy = stem.with_name(f"{stem.name}.mp4")
    try:
        candidates = [legacy] if legacy.is_file() and legacy.stat().st_size > 0 else []
        pattern = re.compile(rf"^{re.escape(stem.name)}_(\d{{3,4}})\.mp4$", re.IGNORECASE)
        candidates.extend(
            sorted(
                path
                for path in stem.parent.iterdir()
                if path.is_file() and pattern.fullmatch(path.name) and path.stat().st_size > 0
            )
        )
    except OSError:
        return ()
    return tuple(candidates)


def existing_output_path(stem: Path) -> Path | None:
    """Find any completed version when its quality does not matter."""

    return next(iter(existing_output_paths(stem)), None)
