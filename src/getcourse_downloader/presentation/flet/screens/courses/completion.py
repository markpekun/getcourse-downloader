from collections.abc import Awaitable, Callable

import flet as ft

from getcourse_downloader.presentation.flet.screens.courses.state import (
    CompletionResult,
    CompletionStep,
    CompletionViewState,
)
from getcourse_downloader.presentation.flet.theme import Color


class CompletionFlow:
    """Three views inside the existing overlay, independent of download execution."""

    def __init__(
        self,
        result: CompletionResult,
        card: ft.Container,
        *,
        on_change: Callable[[], None],
        on_close: Callable,
        on_github: Callable[..., Awaitable[None]],
        on_contact: Callable[..., Awaitable[None]],
        failed_details: ft.Control | None = None,
    ) -> None:
        self.state = CompletionViewState(result)
        self.result = result
        self._card = card
        self._on_change = on_change
        self._on_close = on_close
        self._on_github = on_github
        self._on_contact = on_contact
        self._result_height = 440 if failed_details is not None else 360
        self.result_content = self._build_result(failed_details)
        self._switcher = ft.AnimatedSwitcher(
            content=self.result_content,
            expand=True,
            duration=220,
            reverse_duration=90,
            transition=ft.AnimatedSwitcherTransition.FADE,
            switch_in_curve=ft.AnimationCurve.EASE_IN_OUT_CUBIC,
            switch_out_curve=ft.AnimationCurve.EASE_IN_CUBIC,
        )
        self._support_content: ft.Column | None = None
        self._instructions_content: ft.Column | None = None
        self._show(self.result_content, CompletionStep.RESULT)

    @staticmethod
    def _text(text: str, *, heading: bool = False, centered: bool = False) -> ft.Text:
        return ft.Text(
            text,
            size=20 if heading else 14,
            weight=ft.FontWeight.W_600 if heading else ft.FontWeight.W_400,
            color=Color.TEXT if heading else Color.TEXT_SECONDARY,
            text_align=ft.TextAlign.CENTER if centered else ft.TextAlign.LEFT,
        )

    def _layout(
        self, body: list[ft.Control], actions: list[ft.Control], *, label: str
    ) -> ft.Column:
        controls: list[ft.Control] = [
            ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                controls=[
                    ft.Text(label, size=12, color=Color.TEXT_SECONDARY),
                    ft.IconButton(
                        key="completion-close",
                        icon=ft.Icons.CLOSE,
                        icon_color=Color.TEXT_SECONDARY,
                        tooltip="Закрыть",
                        on_click=self._on_close,
                    ),
                ],
            ),
            ft.Column(
                key="completion-body",
                controls=body,
                expand=True,
                scroll=ft.Scrollbar(thumb_visibility=True, thickness=4, interactive=True),
                spacing=16,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        ]
        if actions:
            controls.append(
                ft.Column(
                    controls=[
                        ft.Divider(color=ft.Colors.with_opacity(0.08, ft.Colors.WHITE), height=8),
                        *actions,
                    ],
                    spacing=4,
                )
            )
        return ft.Column(
            controls=controls,
            expand=True,
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )

    def _build_result(self, failed_details: ft.Control | None) -> ft.Stack:
        if self.result.is_error:
            title, icon, color = "Ошибка загрузки", ft.Icons.ERROR_ROUNDED, Color.RED
        elif self.result.cancelled:
            title, icon, color = "Загрузка отменена", ft.Icons.STOP_CIRCLE_OUTLINED, Color.YELLOW
        elif self.result.is_warning:
            title, icon, color = (
                "Завершено с предупреждениями",
                ft.Icons.WARNING_AMBER_ROUNDED,
                Color.YELLOW,
            )
        else:
            title, icon, color = (
                "Готово! Видео скачаны",
                ft.Icons.CHECK_CIRCLE_ROUNDED,
                Color.GREEN,
            )
        body: list[ft.Control] = [
            ft.Container(
                width=64,
                height=64,
                border_radius=32,
                alignment=ft.Alignment.CENTER,
                bgcolor=ft.Colors.with_opacity(0.15, color),
                content=ft.Icon(icon, size=36, color=color),
            ),
            self._text(title, heading=True, centered=True),
        ]
        if not (self.result.is_error or self.result.is_warning or self.result.cancelled):
            body.append(self._text("Файлы сохранены в выбранную папку.", centered=True))
        body.append(self._text(self.result.message, centered=True))
        if failed_details is not None:
            body.append(failed_details)
        return ft.Stack(
            expand=True,
            controls=[
                ft.Container(
                    expand=True,
                    padding=ft.Padding.symmetric(vertical=40),
                    alignment=ft.Alignment.CENTER,
                    content=ft.Column(
                        key="completion-body",
                        controls=body,
                        tight=True,
                        spacing=14,
                        alignment=ft.MainAxisAlignment.CENTER,
                        scroll=ft.Scrollbar(thumb_visibility=True, thickness=4, interactive=True),
                        horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ),
                ft.Container(
                    right=0,
                    bottom=0,
                    content=ft.TextButton(
                        key="completion-next",
                        content=ft.Row(
                            tight=True,
                            spacing=8,
                            controls=[
                                ft.Text("Далее"),
                                ft.Icon(ft.Icons.ARROW_FORWARD, size=18),
                            ],
                        ),
                        style=ft.ButtonStyle(color=Color.ACCENT_LIGHT),
                        on_click=self.show_support,
                    ),
                ),
            ],
        )

    def _github_button(self) -> ft.TextButton:
        return ft.TextButton(
            "Перейти на GitHub",
            key="completion-github",
            icon=ft.Icons.OPEN_IN_NEW_ROUNDED,
            on_click=self._on_github,
            style=ft.ButtonStyle(color=Color.ACCENT_LIGHT),
        )

    @staticmethod
    def _emblem(icon: ft.IconData, color: str, size: int = 56) -> ft.Container:
        return ft.Container(
            width=size,
            height=size,
            border_radius=18,
            alignment=ft.Alignment.CENTER,
            bgcolor=ft.Colors.with_opacity(0.09, color),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.12, color)),
            content=ft.Icon(icon, color=color, size=size * 0.5),
        )

    def _help_panel(self) -> ft.Container:
        return ft.Container(
            padding=14,
            border_radius=16,
            bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.07, ft.Colors.WHITE)),
            content=ft.Column(
                tight=True,
                spacing=8,
                controls=[
                    ft.Row(
                        spacing=12,
                        controls=[
                            self._emblem(
                                ft.Icons.CHAT_BUBBLE_OUTLINE_ROUNDED, Color.ACCENT_LIGHT, 36
                            ),
                            ft.Text(
                                "Возникли сложности? Давайте разберёмся.",
                                size=16,
                                weight=ft.FontWeight.W_600,
                                color=Color.TEXT,
                                expand=True,
                            ),
                        ],
                    ),
                    ft.Text(
                        "Курсы устроены по-разному, и предусмотреть каждый случай непросто. "
                        "Если что-то не скачалось, появилась ошибка или приложение работает "
                        "не так, как вы ожидали, — напишите мне.",
                        size=13,
                        color=Color.TEXT_SECONDARY,
                    ),
                    ft.Text(
                        "Помогу разобраться. Вместе найдём причину, а ваш отзыв поможет "
                        "сделать приложение лучше.",
                        size=13,
                        color=Color.TEXT_SECONDARY,
                    ),
                    ft.TextButton(
                        "Написать автору",
                        key="completion-contact",
                        icon=ft.Icons.SEND_ROUNDED,
                        style=ft.ButtonStyle(color=Color.ACCENT_LIGHT),
                        on_click=self._on_contact,
                    ),
                ],
            ),
        )

    def _support_panel(self) -> ft.Container:
        return ft.Container(
            padding=14,
            border_radius=16,
            bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.07, ft.Colors.WHITE)),
            content=ft.Column(
                tight=True,
                spacing=8,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    ft.Row(
                        spacing=12,
                        controls=[
                            self._emblem(ft.Icons.STAR_ROUNDED, Color.YELLOW, 36),
                            ft.Text(
                                "Много часов моей работы. Меньше минуты для вашей поддержки.",
                                size=16,
                                weight=ft.FontWeight.W_600,
                                color=Color.TEXT,
                                expand=True,
                            ),
                        ],
                    ),
                    ft.Text(
                        "Я вложил много времени в это приложение, чтобы вы экономили своё — "
                        "сохраняли видео курсов бесплатно и без лишней ручной работы.",
                        size=13,
                        color=Color.TEXT_SECONDARY,
                    ),
                    ft.Text(
                        "Ваша звезда показывает мне, что эти часы работы принесли пользу, "
                        "и мотивирует улучшать приложение дальше. А ещё она помогает проекту "
                        "стать заметнее, чтобы другие люди тоже могли им воспользоваться.",
                        size=13,
                        color=Color.TEXT_SECONDARY,
                    ),
                    ft.Row(
                        wrap=True,
                        alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                        spacing=12,
                        run_spacing=4,
                        controls=[
                            self._github_button(),
                            ft.TextButton(
                                "Как поставить звезду?",
                                key="completion-help",
                                icon=ft.Icons.HELP_OUTLINE_ROUNDED,
                                style=ft.ButtonStyle(
                                    color=Color.TEXT_SECONDARY,
                                    padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                                    shape=ft.RoundedRectangleBorder(radius=10),
                                ),
                                on_click=self.show_instructions,
                            ),
                        ],
                    ),
                ],
            ),
        )

    def _build_support(self) -> ft.Column:
        return self._layout(
            [self._help_panel(), self._support_panel()],
            [],
            label="Помощь и поддержка",
        )

    def _build_instructions(self) -> ft.Column:
        body: list[ft.Control] = [
            ft.Row(
                spacing=14,
                controls=[
                    self._emblem(ft.Icons.STAR_OUTLINE_ROUNDED, Color.YELLOW, 48),
                    ft.Text(
                        "Как поставить звезду?",
                        size=22,
                        weight=ft.FontWeight.W_700,
                        color=Color.TEXT,
                        expand=True,
                    ),
                ],
            )
        ]
        for number, (heading, text, icon) in enumerate(
            (
                (
                    "Откройте проект",
                    "Нажмите кнопку «Перейти на GitHub» внизу.",
                    ft.Icons.OPEN_IN_NEW_ROUNDED,
                ),
                (
                    "Войдите в GitHub",
                    "Используйте Google или другой удобный способ входа. Если аккаунта ещё нет, "
                    "его можно бесплатно создать через Continue with Google.",
                    ft.Icons.ACCOUNT_CIRCLE_OUTLINED,
                ),
                (
                    "Нажмите Star",
                    "На странице проекта найдите кнопку Star в верхней части страницы "
                    "и нажмите её. Готово — вы поддержали проект!",
                    ft.Icons.STAR_ROUNDED,
                ),
            ),
            start=1,
        ):
            body.append(
                ft.Container(
                    padding=16,
                    border_radius=16,
                    bgcolor=ft.Colors.with_opacity(0.035, ft.Colors.WHITE),
                    border=ft.Border.all(1, ft.Colors.with_opacity(0.08, ft.Colors.WHITE)),
                    content=ft.Row(
                        spacing=14,
                        vertical_alignment=ft.CrossAxisAlignment.START,
                        controls=[
                            ft.Container(
                                width=32,
                                height=32,
                                border_radius=10,
                                alignment=ft.Alignment.CENTER,
                                bgcolor=ft.Colors.with_opacity(0.18, Color.ACCENT),
                                content=ft.Text(
                                    str(number),
                                    size=15,
                                    weight=ft.FontWeight.W_700,
                                    color="#C4B5FD",
                                ),
                            ),
                            ft.Column(
                                expand=True,
                                tight=True,
                                spacing=6,
                                controls=[
                                    ft.Row(
                                        controls=[
                                            ft.Text(
                                                heading,
                                                size=15,
                                                weight=ft.FontWeight.W_600,
                                                color=Color.TEXT,
                                                expand=True,
                                            ),
                                            ft.Icon(icon, size=18, color=Color.ACCENT_LIGHT),
                                        ],
                                    ),
                                    self._text(text),
                                ],
                            ),
                        ],
                    ),
                )
            )
        return self._layout(
            body,
            [
                ft.Row(alignment=ft.MainAxisAlignment.CENTER, controls=[self._github_button()]),
                ft.Row(
                    alignment=ft.MainAxisAlignment.START,
                    controls=[
                        ft.TextButton(
                            "Назад",
                            key="completion-back",
                            icon=ft.Icons.ARROW_BACK,
                            style=ft.ButtonStyle(color=Color.TEXT_SECONDARY),
                            on_click=self.show_support,
                        )
                    ],
                ),
            ],
            label="Звезда на GitHub",
        )

    def _show(self, content: ft.Control, step: CompletionStep) -> None:
        self.state.step = step
        content.key = f"completion-{step.value}"
        # An intrinsic starting height has no finite size to interpolate from.
        # Apply the first fixed height directly; later steps can resize smoothly.
        # Zero duration keeps the animated container mounted for those steps.
        self._card.animate = ft.Animation(
            0 if self._card.height is None else 320, ft.AnimationCurve.EASE_OUT_CUBIC
        )
        self._card.width = {
            CompletionStep.RESULT: 600,
            CompletionStep.SUPPORT: 660,
            CompletionStep.INSTRUCTIONS: 640,
        }[step]
        self._card.height = {
            CompletionStep.RESULT: self._result_height,
            CompletionStep.SUPPORT: 560,
            CompletionStep.INSTRUCTIONS: 620,
        }[step]
        self._switcher.content = content
        self._card.content = self._switcher

    def show_support(self, _event=None) -> None:
        if self._support_content is None:
            self._support_content = self._build_support()
        self._show(self._support_content, CompletionStep.SUPPORT)
        self._on_change()

    def show_instructions(self, _event=None) -> None:
        if self.state.step is not CompletionStep.SUPPORT:
            return
        if self._instructions_content is None:
            self._instructions_content = self._build_instructions()
        self._show(self._instructions_content, CompletionStep.INSTRUCTIONS)
        self._on_change()
