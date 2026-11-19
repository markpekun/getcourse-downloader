from collections.abc import Callable

import flet as ft

from getcourse_downloader.domain.models import Course, Lesson
from getcourse_downloader.presentation.flet.theme import Color, Glass
from getcourse_downloader.presentation.flet.transitions import OverlayTransition

_WIDTH = 100
_HEIGHT = 34
_DISMISS_MARGIN = 24


class RemoveMenu:
    def __init__(self, page: ft.Page, on_remove: Callable[[Course, Lesson | None], None]) -> None:
        self._page = page
        self._on_remove = on_remove
        self._target: tuple[Course, Lesson | None] | None = None
        card = ft.Container(
            width=_WIDTH,
            height=_HEIGHT,
            border_radius=8,
            border=ft.Border.all(1, Glass.BORDER_LIGHT),
            bgcolor=Glass.MENU_SURFACE,
            shadow=ft.BoxShadow(blur_radius=14, color="#70000000", offset=ft.Offset(0, 4)),
            content=ft.TextButton(
                "Удалить",
                icon=ft.Icons.DELETE_OUTLINE_ROUNDED,
                on_click=self._select,
                style=ft.ButtonStyle(
                    color=Color.TEXT,
                    icon_color=Color.RED,
                    icon_size=16,
                    text_style=ft.TextStyle(size=13, weight=ft.FontWeight.W_500),
                    padding=ft.Padding.symmetric(horizontal=8),
                    shape=ft.RoundedRectangleBorder(radius=7),
                    bgcolor={ft.ControlState.HOVERED: Glass.MENU_HOVER},
                    overlay_color=ft.Colors.TRANSPARENT,
                    visual_density=ft.VisualDensity.COMPACT,
                ),
            ),
        )
        self._region = ft.GestureDetector(
            left=0,
            top=0,
            on_exit=self._dismiss,
            on_tap=self._dismiss,
            content=ft.Container(
                bgcolor=ft.Colors.TRANSPARENT,
                padding=ft.Padding.all(_DISMISS_MARGIN),
                content=card,
            ),
        )
        self._keyboard = ft.KeyboardListener(
            on_key_down=self._on_key_down,
            content=ft.Stack(
                fit=ft.StackFit.EXPAND,
                controls=[
                    ft.GestureDetector(
                        left=0,
                        right=0,
                        top=0,
                        bottom=0,
                        on_hover=self._dismiss,
                        on_tap=self._dismiss,
                        content=ft.Container(bgcolor=ft.Colors.TRANSPARENT),
                    ),
                    self._region,
                ],
            ),
        )
        self.control = ft.Container(
            left=0, right=0, top=0, bottom=0, visible=False, content=self._keyboard
        )
        self._transition = OverlayTransition(page, self.control, card)

    async def show(self, course: Course, lesson: Lesson | None, position: ft.Offset) -> None:
        self._target = (course, lesson)
        right = (self._page.width or position.x + _WIDTH) - _WIDTH
        bottom = (self._page.height or position.y + _HEIGHT) - _HEIGHT
        self._region.left = max(0, min(position.x, right)) - _DISMISS_MARGIN
        self._region.top = max(0, min(position.y, bottom)) - _DISMISS_MARGIN
        self._transition.show()
        await self._keyboard.focus()

    async def _dismiss(self, _event) -> None:
        self.close()

    async def _on_key_down(self, event: ft.KeyDownEvent) -> None:
        if event.key == "Escape":
            self.close()

    async def _select(self, _event) -> None:
        target = self._target
        self.close()
        if target is not None:
            self._on_remove(*target)

    def close(self) -> None:
        self._target = None
        if self.control.visible:
            self._transition.close(update=False)
            self._page.update(self.control)

    def dispose(self) -> None:
        self._target = None
        self._transition.dispose()
