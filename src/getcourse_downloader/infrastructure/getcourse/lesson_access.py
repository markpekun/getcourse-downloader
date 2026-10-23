from __future__ import annotations

from contextlib import suppress
from typing import Any

from playwright.async_api import Error as PlaywrightError


async def lesson_access_denied(page: Any) -> bool:
    """Recognize GetCourse's dedicated access error page, not lesson content."""
    with suppress(PlaywrightError):
        heading = page.get_by_role("heading", name="Нет доступа", exact=True)
        if await heading.is_visible():
            message = await heading.locator("..").inner_text()
            return "У вас нет доступа к этому уроку" in message
        error = page.get_by_text("Возникла ошибка #403", exact=True)
        message = page.get_by_text("У вас нет доступа к этому уроку", exact=True)
        return bool(await error.is_visible() and await message.is_visible())
    return False
