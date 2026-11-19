from collections.abc import Callable
from typing import Literal

import flet as ft

from getcourse_downloader.presentation.flet.theme import Color

ClearTarget = Literal["courses", "authorization"]


def build_clear_confirmation(
    target: ClearTarget,
    *,
    on_confirm: Callable,
    on_cancel: Callable,
    on_dismiss: Callable,
) -> ft.AlertDialog:
    if target == "courses":
        title = "Очистить список курсов?"
        description = (
            "Будет удалён весь загруженный список курсов и уроков. "
            "Чтобы продолжить работу, вам придётся загрузить его заново.\n\n"
            "Скачанные видео останутся на диске."
        )
        label = "Очистить список"
        icon = ft.Icons.PLAYLIST_REMOVE_ROUNDED
    else:
        title = "Очистить авторизацию?"
        description = (
            "Будут удалены cookies и данные входа встроенного браузера. "
            "При следующем обращении к курсу потребуется снова войти в аккаунт.\n\n"
            "Список курсов и скачанные видео сохранятся."
        )
        label = "Очистить авторизацию"
        icon = ft.Icons.COOKIE_OUTLINED
    return _build_confirmation(
        f"confirm-clear-{target}",
        title,
        description,
        label,
        icon,
        on_confirm=on_confirm,
        on_cancel=on_cancel,
        on_dismiss=on_dismiss,
    )


def build_removal_confirmation(
    item_title: str,
    *,
    is_folder: bool,
    on_confirm: Callable,
    on_cancel: Callable,
    on_dismiss: Callable,
) -> ft.AlertDialog:
    description = f"«{item_title}» будет удалён из списка приложения."
    if is_folder:
        description += " Вложенные разделы и уроки тоже будут удалены из списка."
    description += "\n\nСкачанные видео останутся на диске."
    return _build_confirmation(
        "confirm-remove-course-item",
        "Удалить из списка?",
        description,
        "Удалить",
        ft.Icons.DELETE_OUTLINE_ROUNDED,
        on_confirm=on_confirm,
        on_cancel=on_cancel,
        on_dismiss=on_dismiss,
    )


def _build_confirmation(
    key: str,
    title: str,
    description: str,
    label: str,
    icon: ft.IconData,
    *,
    on_confirm: Callable,
    on_cancel: Callable,
    on_dismiss: Callable,
) -> ft.AlertDialog:
    return ft.AlertDialog(
        key=key,
        modal=True,
        scrollable=True,
        bgcolor=Color.BG_CARD,
        barrier_color=ft.Colors.with_opacity(0.7, ft.Colors.BLACK),
        shape=ft.RoundedRectangleBorder(
            radius=20,
            side=ft.BorderSide(1, ft.Colors.with_opacity(0.08, ft.Colors.WHITE)),
        ),
        icon=ft.Icon(icon, color=Color.RED, size=30),
        title=ft.Text(title, size=20, weight=ft.FontWeight.W_600, color=Color.TEXT),
        content=ft.Container(
            width=420,
            padding=16,
            border_radius=16,
            bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE),
            border=ft.Border.all(1, ft.Colors.with_opacity(0.07, ft.Colors.WHITE)),
            content=ft.Text(description, size=14, color=Color.TEXT_SECONDARY),
        ),
        actions=[
            ft.TextButton("Отмена", on_click=on_cancel, autofocus=True),
            ft.TextButton(label, on_click=on_confirm, style=ft.ButtonStyle(color=Color.RED)),
        ],
        on_dismiss=on_dismiss,
    )
