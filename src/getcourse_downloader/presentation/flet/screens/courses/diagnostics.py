from __future__ import annotations

import concurrent.futures
import json
import logging
from collections.abc import Callable
from pathlib import Path

import flet as ft

from getcourse_downloader.infrastructure.diagnostics.ui_lifecycle import trace_operation
from getcourse_downloader.presentation.flet.screens.courses.controller import CoursesController
from getcourse_downloader.presentation.flet.theme import Color, Gradient, Shadow
from getcourse_downloader.presentation.flet.transitions import OverlayTransition

_LOGGER = logging.getLogger("getcourse_downloader.ui")


class DiagnosticOverlay:
    """Keep the download controls mounted while an individual report is open."""

    def __init__(self, page: ft.Page, on_back: Callable) -> None:
        self._page = page
        self._generation = 0
        self._task: concurrent.futures.Future[None] | None = None
        self._disposed = False
        self.title = ft.Text(size=19, weight=ft.FontWeight.W_700, color=Color.TEXT)
        self.path = ft.Text(size=11, color=Color.TEXT_MUTED, selectable=True)
        self.notice = ft.Text(size=11, color=Color.YELLOW, visible=False)
        self.text = ft.Text(
            size=11, color=Color.TEXT_SECONDARY, selectable=True, font_family="Consolas"
        )
        self.card = ft.Container(
            width=600,
            height=620,
            padding=ft.Padding.all(24),
            border_radius=20,
            bgcolor=Color.BG_CARD,
            gradient=Gradient.CARD,
            shadow=Shadow.GLOW_PRIMARY,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    ft.Icon(ft.Icons.BUG_REPORT_OUTLINED, size=38, color=Color.YELLOW),
                    self.title,
                    self.notice,
                    self.path,
                    ft.Container(
                        width=520,
                        expand=True,
                        padding=12,
                        border_radius=10,
                        bgcolor="rgba(0,0,0,0.3)",
                        content=ft.Column(scroll=ft.ScrollMode.AUTO, controls=[self.text]),
                    ),
                    ft.OutlinedButton("Назад", on_click=on_back),
                ],
            ),
        )
        self.control = ft.Container(
            expand=True,
            left=0,
            right=0,
            top=0,
            bottom=0,
            visible=False,
            bgcolor=ft.Colors.with_opacity(0.7, ft.Colors.BLACK),
            content=ft.Container(padding=16, alignment=ft.Alignment.CENTER, content=self.card),
        )
        self._transition = OverlayTransition(page, self.control, self.card)

    def show(self, path: Path, title: str, cached: str = "") -> None:
        self._cancel_read()
        self.title.value = title
        self.path.value = str(path)
        self.notice.visible = False
        self.text.value = cached or "Чтение отчёта…"
        self._transition.show()
        if not cached:
            self._task = self._page.run_task(self._read, path, self._generation)

    async def _read(self, path: Path, generation: int) -> None:
        notice = ""
        try:
            with trace_operation("lesson_report_read", generation=generation):
                formatted = await CoursesController.load_diagnostic(path)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            _LOGGER.warning("lesson_report_file_unavailable reason=%s", type(error).__name__)
            notice = "Не удалось прочитать сохранённый файл отчёта."
            formatted = json.dumps(
                {"error_code": "DIAGNOSTIC_REPORT_UNAVAILABLE", "summary": notice},
                ensure_ascii=False,
                indent=2,
            )
        if self._disposed or generation != self._generation or not self.control.visible:
            return
        self.text.value = formatted
        self.notice.value = notice
        self.notice.visible = bool(notice)
        self._page.update(self.control)

    def _cancel_read(self) -> None:
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def close(self, *, update: bool = True) -> None:
        self._cancel_read()
        self._transition.close(update=update)

    def dispose(self) -> None:
        self._disposed = True
        self._cancel_read()
        self._transition.dispose()
