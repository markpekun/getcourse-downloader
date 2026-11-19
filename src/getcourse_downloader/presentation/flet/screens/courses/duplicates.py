from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from threading import Event

import flet as ft

from getcourse_downloader.infrastructure.storage.video_duplicates import (
    DuplicateProgress,
    DuplicateScan,
    VideoCopy,
)
from getcourse_downloader.presentation.flet.screens.courses.controller import CoursesController
from getcourse_downloader.presentation.flet.theme import Color, result_emblem


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024:
            digits = 0 if unit == "Б" or value >= 100 else 1
            formatted = f"{value:.{digits}f}".removesuffix(".0").replace(".", ",")
            return f"{formatted} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ".replace(".", ",")


class DuplicateDialog:
    def __init__(self, page: ft.Page, controller: CoursesController) -> None:
        self._page = page
        self._controller = controller
        self._dialog: ft.AlertDialog | None = None
        self._cancel: Event | None = None

    @property
    def active(self) -> bool:
        return self._dialog is not None or self._cancel is not None

    async def open(self, folder: Path) -> None:
        if self.active:
            return
        phase = ft.Text("Ищем видео в папке и подпапках", weight=ft.FontWeight.W_600)
        status = ft.Text("Собираем список видео…", size=13, color=Color.TEXT_SECONDARY)
        progress = ft.ProgressBar(
            color=Color.ACCENT_LIGHT,
            bgcolor=ft.Colors.with_opacity(0.12, Color.ACCENT_LIGHT),
            bar_height=6,
            border_radius=5,
        )
        dialog = ft.AlertDialog(
            modal=True,
            scrollable=True,
            bgcolor=Color.BG_CARD,
            inset_padding=ft.Padding.symmetric(horizontal=24, vertical=20),
            title=ft.Text("Дубликаты видео", size=21, weight=ft.FontWeight.W_600),
            content=ft.Column(
                width=self._content_width(),
                tight=True,
                spacing=16,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[
                    self._folder_note(folder.absolute()),
                    ft.Container(
                        padding=16,
                        border_radius=12,
                        bgcolor=ft.Colors.with_opacity(0.08, Color.ACCENT_LIGHT),
                        border=ft.Border.all(1, ft.Colors.with_opacity(0.20, Color.ACCENT_LIGHT)),
                        content=ft.Column(
                            tight=True,
                            spacing=12,
                            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
                            controls=[phase, progress, status],
                        ),
                    ),
                ],
            ),
            actions=[ft.TextButton("Отмена", on_click=self._close)],
            on_dismiss=self._on_dismiss,
        )
        self._page.show_dialog(dialog)
        self._dialog = dialog
        cancel = Event()
        self._cancel = cancel
        loop = asyncio.get_running_loop()

        def update_progress(snapshot: DuplicateProgress) -> None:
            if self._dialog is not dialog or cancel.is_set():
                return
            if snapshot.candidate_count is None:
                status.value = f"Найдено видео: {snapshot.video_count}"
            else:
                phase.value = "Сравниваем содержимое файлов"
                progress.value = snapshot.bytes_read / snapshot.bytes_total
                status.value = (
                    f"Обработано файлов одинакового размера: "
                    f"{snapshot.completed} из {snapshot.candidate_count}\n"
                    f"Прочитано {_format_size(snapshot.bytes_read)} "
                    f"из {_format_size(snapshot.bytes_total)}"
                )
            self._page.update(phase, progress, status)

        def report_progress(snapshot: DuplicateProgress) -> None:
            loop.call_soon_threadsafe(update_progress, snapshot)

        try:
            scan = await self._controller.scan_duplicates(folder, cancel, report_progress)
        except OSError as error:
            if self._dialog is dialog:
                self._show_outcome(
                    dialog,
                    "Не удалось проверить папку",
                    str(error.strerror or error),
                    icon=ft.Icons.ERROR_ROUNDED,
                    color=Color.RED,
                )
        else:
            if self._dialog is dialog:
                self._show_scan(dialog, scan)
        finally:
            cancel.set()
            self._cancel = None

    def _show_scan(self, dialog: ft.AlertDialog, scan: DuplicateScan) -> None:
        if not scan.groups:
            self._show_empty_scan(dialog, scan)
            return

        duplicate_count = sum(len(group) - 1 for group in scan.groups)
        selected: dict[Path, int] = {}
        selection_info = ft.Text("Выбрано: 0", size=13, weight=ft.FontWeight.W_600)
        select_all_button = ft.TextButton("Выбрать все дубликаты")
        remove = ft.TextButton(
            "Удалить выбранные",
            disabled=True,
            style=ft.ButtonStyle(
                color={
                    ft.ControlState.DISABLED: Color.TEXT_MUTED,
                    ft.ControlState.DEFAULT: Color.RED,
                }
            ),
        )
        rows: list[ft.Control] = []
        groups: list[list[tuple[VideoCopy, ft.Checkbox]]] = []

        def refresh_group(group: list[tuple[VideoCopy, ft.Checkbox]]) -> None:
            all_but_one = sum(copy.path in selected for copy, _ in group) == len(group) - 1
            for copy, checkbox in group:
                checkbox.value = copy.path in selected
                checkbox.disabled = all_but_one and copy.path not in selected

        def refresh_actions() -> None:
            count = len(selected)
            selection_info.value = (
                f"Выбрано: {count} · {_format_size(sum(selected.values()))}"
                if count
                else "Выбрано: 0"
            )
            remove.disabled = not selected
            remove.content = f"Удалить ({count})" if count else "Удалить выбранные"
            select_all_button.content = (
                "Снять выделение" if count == duplicate_count else "Выбрать все дубликаты"
            )

        def change_handler(
            copy: VideoCopy, group: list[tuple[VideoCopy, ft.Checkbox]]
        ) -> Callable[[ft.Event[ft.Checkbox]], Awaitable[None]]:
            async def change(event: ft.Event[ft.Checkbox]) -> None:
                ft.context.disable_auto_update()
                if self._dialog is not dialog or self._cancel is not None:
                    return
                if event.control.value and any(
                    other is not copy and other.path not in selected for other, _ in group
                ):
                    selected[copy.path] = copy.size
                else:
                    selected.pop(copy.path, None)
                refresh_group(group)
                refresh_actions()
                self._page.update(
                    selection_info, select_all_button, remove, *(checkbox for _, checkbox in group)
                )

            return change

        width = self._content_width()
        for copies in scan.groups:
            group: list[tuple[VideoCopy, ft.Checkbox]] = []
            for copy in copies:
                relative_path = copy.path.relative_to(scan.folder)
                parent = (
                    str(relative_path.parent) if len(relative_path.parts) > 1 else "Корень папки"
                )
                checkbox = ft.Checkbox(
                    tooltip=str(relative_path),
                    label=ft.Column(
                        width=width - 88,
                        tight=True,
                        spacing=2,
                        controls=[
                            ft.Text(
                                copy.path.name,
                                size=13,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                            ft.Text(
                                parent,
                                size=12,
                                color=Color.TEXT_SECONDARY,
                                max_lines=2,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                    ),
                    value=False,
                    on_change=change_handler(copy, group),
                )
                group.append((copy, checkbox))
            groups.append(group)
            name = copies[0].path.name
            file_size = _format_size(copies[0].size)
            rows.append(
                ft.Container(
                    padding=12,
                    border_radius=12,
                    bgcolor=ft.Colors.with_opacity(0.035, ft.Colors.WHITE),
                    border=ft.Border.all(1, ft.Colors.with_opacity(0.08, ft.Colors.WHITE)),
                    content=ft.Column(
                        tight=True,
                        spacing=4,
                        controls=[
                            ft.Row(
                                controls=[
                                    ft.Icon(
                                        ft.Icons.MOVIE_OUTLINED, size=17, color=Color.ACCENT_LIGHT
                                    ),
                                    ft.Text(
                                        name,
                                        tooltip=name,
                                        expand=True,
                                        max_lines=1,
                                        overflow=ft.TextOverflow.ELLIPSIS,
                                        weight=ft.FontWeight.W_600,
                                    ),
                                    ft.Text(
                                        f"Копий: {len(copies)} · {file_size} за файл",
                                        size=12,
                                        color=Color.TEXT_SECONDARY,
                                    ),
                                ],
                            ),
                            *(checkbox for _, checkbox in group),
                        ],
                    ),
                )
            )

        async def select_all(_event: ft.Event[ft.TextButton]) -> None:
            ft.context.disable_auto_update()
            active_dialog = self._dialog
            if active_dialog is None or active_dialog is not dialog or self._cancel is not None:
                return
            clear_selection = len(selected) == duplicate_count
            selected.clear()
            for group in groups:
                if not clear_selection:
                    selected.update((copy.path, copy.size) for copy, _ in group[1:])
                refresh_group(group)
            refresh_actions()
            active_dialog.update()

        async def delete(_event: ft.Event[ft.TextButton]) -> None:
            ft.context.disable_auto_update()
            if self._dialog is not dialog or self._cancel is not None or not selected:
                return
            await self._delete(dialog, scan, frozenset(selected))

        if scan.errors:
            rows.append(ft.Text(f"Ошибки чтения: {len(scan.errors)}", color=Color.RED))
            rows.extend(ft.Text(error, selectable=True) for error in scan.errors)
        dialog.content = ft.Column(
            width=width,
            tight=True,
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            controls=[
                self._folder_note(scan.folder),
                ft.Row(
                    wrap=True,
                    spacing=16,
                    run_spacing=4,
                    controls=[
                        ft.Text(f"Найдено видео: {scan.video_count}", size=13),
                        ft.Text(f"Групп: {len(scan.groups)}", size=13),
                        ft.Text(f"Лишних копий: {duplicate_count}", size=13),
                    ],
                ),
                ft.ListView(
                    height=self._list_height(),
                    spacing=8,
                    padding=ft.Padding.only(right=9),
                    scroll=ft.Scrollbar(thumb_visibility=True, thickness=4, interactive=True),
                    controls=rows,
                ),
                selection_info,
                ft.Text(
                    "В каждой группе останется копия. Выбранные файлы и опустевшие подпапки "
                    "удаляются безвозвратно.",
                    size=12,
                    color=Color.TEXT_SECONDARY,
                ),
            ],
        )
        remove.on_click = delete
        select_all_button.on_click = select_all
        dialog.actions = [
            ft.TextButton("Отмена", on_click=self._close),
            select_all_button,
            remove,
        ]
        dialog.update()

    def _show_empty_scan(self, dialog: ft.AlertDialog, scan: DuplicateScan) -> None:
        if scan.errors:
            self._show_outcome(
                dialog,
                "Проверка завершена с предупреждениями",
                f"Среди проверенных файлов дубликатов нет.\nНайдено видео: {scan.video_count}",
                icon=ft.Icons.WARNING_AMBER_ROUNDED,
                color=Color.YELLOW,
                errors=scan.errors,
            )
        elif scan.video_count:
            self._show_outcome(
                dialog,
                "Дубликаты не найдены",
                f"Найдено видео: {scan.video_count}",
            )
        else:
            self._show_outcome(
                dialog,
                "Видео не найдены",
                "В папке загрузки и её подпапках нет видео для проверки.",
                icon=ft.Icons.VIDEO_LIBRARY_OUTLINED,
                color=Color.ACCENT_LIGHT,
            )

    async def _delete(
        self, dialog: ft.AlertDialog, scan: DuplicateScan, selected: frozenset[Path]
    ) -> None:
        cancel = Event()
        self._cancel = cancel
        dialog.content = ft.Row(
            [ft.ProgressRing(width=20, height=20), ft.Text("Удаляем выбранные файлы…")]
        )
        dialog.actions = []
        try:
            dialog.update()
            result = await self._controller.delete_duplicates(scan, selected, cancel)
            if self._dialog is not dialog:
                return
            message = f"Удалено файлов: {result.deleted}"
            all_deleted = result.deleted == len(selected)
            if not all_deleted:
                message += f" из {len(selected)}"
            if result.removed_folders:
                message += f"\nУдалено пустых папок: {result.removed_folders}"
            if all_deleted and not result.errors:
                self._show_outcome(dialog, "Дубликаты удалены", message)
            elif result.deleted:
                self._show_outcome(
                    dialog,
                    (
                        "Дубликаты удалены с предупреждениями"
                        if all_deleted
                        else "Часть дубликатов удалена"
                    ),
                    message,
                    icon=ft.Icons.WARNING_AMBER_ROUNDED,
                    color=Color.YELLOW,
                    errors=result.errors,
                )
            else:
                self._show_outcome(
                    dialog,
                    "Не удалось удалить дубликаты",
                    message,
                    icon=ft.Icons.ERROR_ROUNDED,
                    color=Color.RED,
                    errors=result.errors,
                )
        finally:
            cancel.set()
            self._cancel = None

    def _show_outcome(
        self,
        dialog: ft.AlertDialog,
        title: str,
        message: str,
        *,
        icon: ft.IconData = ft.Icons.CHECK_CIRCLE_ROUNDED,
        color: str = Color.GREEN,
        errors: tuple[str, ...] = (),
    ) -> None:
        controls: list[ft.Control] = [
            result_emblem(icon, color),
            ft.Text(
                title,
                size=20,
                weight=ft.FontWeight.W_600,
                color=Color.TEXT,
                text_align=ft.TextAlign.CENTER,
            ),
            ft.Text(message, size=14, color=Color.TEXT_SECONDARY, text_align=ft.TextAlign.CENTER),
        ]
        if errors:
            controls.append(
                ft.Container(
                    width=min(500, self._content_width()),
                    padding=12,
                    border_radius=10,
                    bgcolor=ft.Colors.with_opacity(0.06, ft.Colors.WHITE),
                    content=ft.Column(
                        tight=True,
                        controls=[
                            ft.Text(f"Ошибки: {len(errors)}", weight=ft.FontWeight.W_600),
                            ft.Column(
                                height=min(self._list_height(), max(56, 36 * len(errors))),
                                scroll=ft.ScrollMode.AUTO,
                                controls=[
                                    ft.Text(error, size=13, selectable=True) for error in errors
                                ],
                            ),
                        ],
                    ),
                )
            )
        dialog.title = None
        dialog.content_padding = ft.Padding.only(left=24, top=30, right=24, bottom=12)
        dialog.content = ft.Column(
            width=min(500 if errors else 390, self._content_width()),
            tight=True,
            spacing=12,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            controls=controls,
        )
        dialog.actions_alignment = ft.MainAxisAlignment.CENTER
        dialog.actions = [
            ft.TextButton(
                "Закрыть",
                style=ft.ButtonStyle(color=Color.ACCENT_LIGHT),
                on_click=self._close,
            )
        ]
        dialog.update()

    @staticmethod
    def _folder_note(folder: Path) -> ft.Container:
        path = str(folder)
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=12, vertical=10),
            border_radius=10,
            bgcolor=ft.Colors.with_opacity(0.045, ft.Colors.WHITE),
            content=ft.Row(
                spacing=10,
                controls=[
                    ft.Icon(ft.Icons.FOLDER_OUTLINED, size=19, color=Color.ACCENT_LIGHT),
                    ft.Column(
                        expand=True,
                        tight=True,
                        spacing=2,
                        controls=[
                            ft.Text("Папка загрузки", size=12, color=Color.TEXT_SECONDARY),
                            ft.Text(path, size=13, tooltip=path, selectable=True),
                        ],
                    ),
                ],
            ),
        )

    def _content_width(self) -> float:
        return min(680, max(280, (self._page.width or 800) - 104))

    def _list_height(self) -> float:
        return min(390, max(110, (self._page.height or 700) - 345))

    async def _close(self, _event: ft.Event[ft.TextButton]) -> None:
        ft.context.disable_auto_update()
        self.dispose()

    async def _on_dismiss(self, event: ft.Event[ft.DialogControl]) -> None:
        if event.control is self._dialog:
            self._dialog = None
            if self._cancel is not None:
                self._cancel.set()

    def dispose(self) -> None:
        if self._cancel is not None:
            self._cancel.set()
        dialog, self._dialog = self._dialog, None
        if dialog is not None and dialog.open:
            dialog.open = False
            dialog.update()
