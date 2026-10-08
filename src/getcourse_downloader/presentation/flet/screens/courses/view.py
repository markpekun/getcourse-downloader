import asyncio
import concurrent.futures
import contextlib
import json
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path

import flet as ft

from getcourse_downloader.domain.errors import DownloaderError
from getcourse_downloader.domain.events import DownloadEvent, DownloadEventType
from getcourse_downloader.domain.models import Course, DownloadSummary, Lesson
from getcourse_downloader.presentation.flet.screens.courses.completion import CompletionFlow
from getcourse_downloader.presentation.flet.screens.courses.components import (
    DownloadLessonRow,
    build_course_tree,
    build_download_lesson_row,
    iter_course_lessons,
    selected_course_lessons,
)
from getcourse_downloader.presentation.flet.screens.courses.confirmations import (
    ClearTarget,
    build_clear_confirmation,
)
from getcourse_downloader.presentation.flet.screens.courses.controller import CoursesController
from getcourse_downloader.presentation.flet.screens.courses.state import (
    CompletionResult,
    CompletionStep,
    CoursesViewState,
)
from getcourse_downloader.presentation.flet.theme import (
    Color,
    Gradient,
    Shadow,
    accent_button,
    body_text,
    divider,
)

_GITHUB_URL = "https://github.com/markpekun/getcourse-downloader"
_CONTACT_URL = "https://t.me/No_Resp_404"
_DOWNLOAD_FOLLOW_PAUSE_SECONDS = 10.0


_COURSE_COLORS = [
    "#7C3AED",
    "#EC4899",
    "#10B981",
    "#F59E0B",
    "#3B82F6",
    "#EF4444",
    "#14B8A6",
    "#F97316",
]


class CoursesScreen:
    def __init__(
        self,
        page: ft.Page,
        controller: CoursesController,
        on_navigate_start: Callable[[], Awaitable[None]],
    ):
        self.page = page
        self._controller = controller
        self._on_navigate_start = on_navigate_start
        self.state = CoursesViewState(save_path=controller.load_save_path())
        self._confirmation_dialog: ft.AlertDialog | None = None
        self._download_scroll_task: concurrent.futures.Future[None] | None = None
        self._download_follow_resume_task: concurrent.futures.Future[None] | None = None
        self._download_follow_paused = False
        self._active_lesson_url: str | None = None
        self._completion_flow: CompletionFlow | None = None
        self._diagnostic_reports: dict[str, Path] = {}
        self._diagnostic_reports_by_title: dict[str, Path] = {}
        self._diagnostic_open = False
        self._diagnostic_previous_content: ft.Control | None = None
        self._diagnostic_previous_visible = False
        self._active_download_id = 0
        self._download_title = ft.Text(
            "Подготовка",
            size=18,
            weight=ft.FontWeight.W_600,
            color=Color.TEXT,
        )

        self.data = controller.load_courses()
        self.state.expanded_course_urls.update(course.url for course in self.data)
        self.lesson_refs: dict[str, ft.Checkbox] = {}
        self.folder_refs: dict[str, ft.Checkbox] = {}
        self.folder_badges: dict[str, ft.Text] = {}

        self.file_picker = ft.FilePicker()

        self._search_query = ft.TextField(
            hint_text="Поиск уроков...",
            hint_style=ft.TextStyle(color=Color.TEXT_MUTED, size=14),
            color=Color.TEXT,
            bgcolor="rgba(255,255,255,0.04)",
            border_color=Color.BORDER,
            border_width=1,
            border_radius=10,
            focused_border_color=Color.ACCENT,
            focused_bgcolor="rgba(124,58,237,0.06)",
            cursor_color=Color.ACCENT,
            text_style=ft.TextStyle(size=14),
            prefix_icon=ft.Icons.SEARCH_ROUNDED,
            prefix_style=ft.TextStyle(color=Color.TEXT_MUTED),
            height=44,
            expand=True,
            on_change=self._on_search,
        )

        self.selected_label = ft.Text(
            "0",
            size=24,
            weight=ft.FontWeight.W_800,
            color=Color.ACCENT_LIGHT,
        )

        self.selected_hint = body_text("уроков выбрано", size=13)
        self._speed_text = ft.Text(
            "Средняя: —",
            size=12,
            color=Color.TEXT_SECONDARY,
            weight=ft.FontWeight.W_500,
        )
        self.course_list = ft.Column(spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)
        self._build_course_list()

        self.side_content = ft.Column(spacing=16)
        self._build_side_panel()

        self.log_lines: list[str] = []
        self._log_column = ft.Column(scroll=ft.ScrollMode.AUTO, auto_scroll=True, spacing=1)
        self._log_container = ft.Container(
            width=440,
            height=180,
            border_radius=10,
            bgcolor="rgba(0,0,0,0.3)",
            border=ft.Border.all(1, "rgba(255,255,255,0.06)"),
            padding=ft.Padding.all(12),
            content=self._log_column,
        )
        self._download_rows: dict[str, DownloadLessonRow] = {}
        self._download_rows_column = ft.Column(
            spacing=5,
            scroll=ft.ScrollMode.AUTO,
            scroll_interval=100,
            on_scroll=self._on_download_rows_scroll,
        )
        self._download_rows_container = ft.Container(
            width=540,
            height=290,
            border_radius=10,
            bgcolor="rgba(0,0,0,0.25)",
            border=ft.Border.all(1, "rgba(255,255,255,0.06)"),
            padding=ft.Padding.all(10),
            content=self._download_rows_column,
        )
        self._continue_btn = ft.Container(
            visible=False,
            padding=ft.Padding.symmetric(horizontal=20, vertical=8),
            border_radius=8,
            gradient=Gradient.ACCENT,
            ink=True,
            on_click=self._send_continue,
            content=ft.Text("Продолжить", size=14, weight=ft.FontWeight.W_600, color=Color.TEXT),
        )
        self._cancel_btn = ft.OutlinedButton(
            "Отмена",
            icon=ft.Icons.STOP_CIRCLE_OUTLINED,
            on_click=self._cancel_download,
            style=ft.ButtonStyle(
                color=Color.RED,
                side=ft.BorderSide(1, "rgba(239,68,68,0.55)"),
                padding=ft.Padding.symmetric(horizontal=20, vertical=9),
            ),
        )
        self._auth_icon = ft.Container(
            width=38,
            height=38,
            border_radius=11,
            gradient=Gradient.ACCENT,
            shadow=Shadow.GLOW_PRIMARY,
            content=ft.Icon(ft.Icons.LOCK_ROUNDED, size=19, color=Color.TEXT),
        )
        self._auth_title = ft.Text(
            "Требуется вход",
            size=17,
            weight=ft.FontWeight.W_700,
            color=Color.TEXT,
        )
        self._auth_desc = ft.Text(
            "Для доступа к материалам необходимо\nвыполнить вход в аккаунт.",
            size=12,
            color=Color.TEXT_SECONDARY,
            text_align=ft.TextAlign.CENTER,
        )
        _auth_steps = [
            "Дождитесь открытия браузера.",
            "Выполните вход в аккаунт.",
            "Вернитесь в приложение.",
            "Нажмите «Продолжить».",
        ]
        self._auth_instructions = ft.Container(
            padding=ft.Padding.symmetric(vertical=2),
            content=ft.Column(
                spacing=2,
                controls=[
                    ft.Row(
                        spacing=6,
                        controls=[
                            ft.Text(
                                f"{i + 1}.",
                                size=12,
                                color=Color.ACCENT_LIGHT,
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.Text(step, size=12, color=Color.TEXT_SECONDARY),
                        ],
                    )
                    for i, step in enumerate(_auth_steps)
                ],
            ),
        )
        self._auth_status = ft.Text(
            "Ожидаем вход в браузере...",
            size=13,
            color=Color.ACCENT_LIGHT,
            weight=ft.FontWeight.W_500,
            text_align=ft.TextAlign.CENTER,
            height=20,
        )

        self._auth_overlay_task: asyncio.Task | None = None

        self._overlay_card = ft.Container(
            width=600,
            padding=ft.Padding.all(24),
            border_radius=20,
            bgcolor=Color.BG_CARD,
            border=ft.Border.all(1, Color.ACCENT_GLOW),
            shadow=ft.BoxShadow(
                blur_radius=40,
                color=Color.ACCENT_GLOW,
                offset=ft.Offset(0, 8),
            ),
            gradient=Gradient.CARD,
            opacity=0,
            offset=ft.Offset(0, 0.15),
            animate_opacity=ft.Animation(350, ft.AnimationCurve.EASE_OUT),
            animate_offset=ft.Animation(350, ft.AnimationCurve.EASE_OUT),
            animate=ft.Animation(300, ft.AnimationCurve.EASE_OUT),
            content=ft.Column(
                tight=True,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                controls=[
                    ft.Icon(ft.Icons.VIDEO_LIBRARY_ROUNDED, size=38, color=Color.ACCENT_LIGHT),
                    ft.Text(
                        "Загрузка видео",
                        size=18,
                        weight=ft.FontWeight.W_600,
                        color=Color.TEXT,
                    ),
                    self._download_rows_container,
                    self._continue_btn,
                    self._cancel_btn,
                ],
            ),
        )

        self.overlay = ft.Container(
            expand=True,
            left=0,
            right=0,
            top=0,
            bottom=0,
            bgcolor=ft.Colors.with_opacity(0.7, ft.Colors.BLACK),
            visible=False,
            content=ft.Container(
                expand=True,
                padding=16,
                alignment=ft.Alignment.CENTER,
                content=self._overlay_card,
            ),
        )

        self.error_overlay = ft.Container(
            expand=True,
            bgcolor="rgba(0,0,0,0.7)",
            visible=False,
            content=ft.Row(
                expand=True,
                alignment=ft.MainAxisAlignment.CENTER,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Column(
                        alignment=ft.MainAxisAlignment.CENTER,
                        controls=[
                            ft.Container(
                                width=420,
                                padding=ft.Padding.all(24),
                                border_radius=20,
                                bgcolor=Color.BG_CARD,
                                border=ft.Border.all(1, Color.BORDER),
                                shadow=Shadow.CARD,
                                gradient=Gradient.CARD,
                                content=ft.Column(
                                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                    spacing=20,
                                    controls=[
                                        ft.Row(
                                            alignment=ft.MainAxisAlignment.END,
                                            controls=[
                                                ft.Container(
                                                    content=ft.Icon(
                                                        ft.Icons.CLOSE,
                                                        size=20,
                                                        color=Color.TEXT_SECONDARY,
                                                    ),
                                                    padding=ft.Padding.all(4),
                                                    border_radius=6,
                                                    ink=True,
                                                    on_click=self._dismiss_error,
                                                ),
                                            ],
                                        ),
                                        ft.Icon(ft.Icons.ERROR_OUTLINE, size=56, color=Color.RED),
                                        ft.Text(
                                            "Папка сохранения не найдена!",
                                            size=20,
                                            weight=ft.FontWeight.W_600,
                                            color=Color.TEXT,
                                            text_align=ft.TextAlign.CENTER,
                                        ),
                                        ft.Text(
                                            f"Путь: {self.state.save_path}",
                                            size=13,
                                            color=Color.TEXT_MUTED,
                                            text_align=ft.TextAlign.CENTER,
                                        ),
                                        ft.Container(
                                            content=ft.Text(
                                                "Выбрать другую папку",
                                                size=15,
                                                weight=ft.FontWeight.W_600,
                                                color=Color.TEXT,
                                            ),
                                            padding=ft.Padding.symmetric(
                                                horizontal=24, vertical=12
                                            ),
                                            border_radius=10,
                                            gradient=Gradient.ACCENT,
                                            ink=True,
                                            on_click=self._dismiss_error_and_pick,
                                        ),
                                    ],
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        )

        self.view = ft.Container(
            expand=True,
            gradient=Gradient.BG_PRIMARY,
            content=ft.Stack(
                expand=True,
                controls=[
                    ft.Column(
                        spacing=0,
                        controls=[
                            self._build_header(),
                            divider(),
                            ft.Container(
                                expand=True,
                                padding=ft.Padding.only(left=32, right=32, top=20, bottom=20),
                                content=ft.Row(
                                    spacing=24,
                                    controls=[
                                        ft.Container(
                                            width=320,
                                            content=self.side_content,
                                        ),
                                        ft.Container(
                                            expand=True,
                                            content=ft.Column(
                                                spacing=16,
                                                controls=[
                                                    self._build_toolbar(),
                                                    self.course_list,
                                                ],
                                            ),
                                        ),
                                    ],
                                ),
                            ),
                        ],
                    ),
                    self.overlay,
                    self.error_overlay,
                ],
            ),
        )
        self._update_selected_count()

    def _build_header(self) -> ft.Container:
        total_lessons = sum(course.lesson_count for course in self.data)
        self._clear_courses_button = ft.IconButton(
            icon=ft.Icons.PLAYLIST_REMOVE_ROUNDED,
            icon_color=Color.RED,
            icon_size=20,
            bgcolor="rgba(239,68,68,0.10)",
            tooltip="Очистить список курсов и уроков",
            on_click=self._delete_courses,
        )
        self._clear_authorization_button = ft.IconButton(
            icon=ft.Icons.COOKIE_OUTLINED,
            icon_color=Color.YELLOW,
            icon_size=20,
            bgcolor="rgba(245,158,11,0.10)",
            tooltip="Очистить авторизацию (cookies)",
            on_click=self._clear_authorization,
        )
        return ft.Container(
            padding=ft.Padding.symmetric(horizontal=32, vertical=16),
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(
                        spacing=16,
                        controls=[
                            ft.Container(
                                width=40,
                                height=40,
                                border_radius=12,
                                gradient=Gradient.ACCENT,
                                content=ft.Icon(
                                    ft.Icons.DOWNLOAD_ROUNDED,
                                    size=20,
                                    color=Color.TEXT,
                                ),
                            ),
                            ft.Column(
                                spacing=0,
                                controls=[
                                    ft.Text(
                                        "Мои курсы",
                                        size=22,
                                        weight=ft.FontWeight.W_700,
                                        color=Color.TEXT,
                                    ),
                                    body_text(
                                        f"{len(self.data)} курсов · {total_lessons} уроков",
                                        size=12,
                                    ),
                                ],
                            ),
                        ],
                    ),
                    ft.Container(
                        content=ft.Row(
                            spacing=8,
                            controls=[
                                ft.Container(
                                    content=ft.Row(
                                        [
                                            ft.Icon(
                                                ft.Icons.SPEED_ROUNDED,
                                                size=15,
                                                color=Color.GREEN,
                                            ),
                                            self._speed_text,
                                        ],
                                        spacing=5,
                                    ),
                                    padding=ft.Padding.symmetric(horizontal=10, vertical=5),
                                    border_radius=8,
                                    bgcolor="rgba(16,185,129,0.10)",
                                    tooltip=(
                                        "Средняя скорость загрузки видео "
                                        "(обновляется раз в 3 секунды)"
                                    ),
                                ),
                                self._clear_courses_button,
                                self._clear_authorization_button,
                            ],
                        ),
                    ),
                ],
            ),
        )

    def _build_toolbar(self) -> ft.Container:
        return ft.Container(
            padding=ft.Padding.symmetric(vertical=2),
            content=ft.Row(
                spacing=10,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[self._search_query],
            ),
        )

    def _build_course_list(self):
        self.course_list.controls.clear()
        self.lesson_refs.clear()
        self.folder_refs.clear()
        self.folder_badges.clear()
        for idx, course in enumerate(self.data):
            accent = _COURSE_COLORS[idx % len(_COURSE_COLORS)]
            tree = build_course_tree(
                course,
                accent=accent,
                selected_urls=self.state.selected_lesson_urls,
                expanded_urls=self.state.expanded_course_urls,
                query=self.state.search_query,
                on_folder_toggle=self._toggle_course,
                on_folder_selection=self._set_course_selection,
                on_lesson_selection=self._set_lesson_selection,
            )
            self.lesson_refs.update(tree.lesson_checkboxes)
            self.folder_refs.update(tree.folder_checkboxes)
            self.folder_badges.update(tree.folder_badges)
            self.course_list.controls.append(tree.control)

    def _toggle_course(self, course_url: str):
        if course_url in self.state.expanded_course_urls:
            self.state.expanded_course_urls.discard(course_url)
        else:
            self.state.expanded_course_urls.add(course_url)
        self._build_course_list()
        self.page.update()

    def _on_search(self, e):
        self.state.search_query = e.control.value.strip()
        self._build_course_list()
        self.page.update()

    def _set_lesson_selection(self, lesson: Lesson, selected: bool) -> None:
        if selected:
            self.state.selected_lesson_urls.add(lesson.url)
        else:
            self.state.selected_lesson_urls.discard(lesson.url)
        self._build_course_list()
        self._update_selected_count()

    def _set_course_selection(self, course: Course, selected: bool) -> None:
        urls = {item.lesson.url for item in iter_course_lessons(course)}
        if selected:
            self.state.selected_lesson_urls.update(urls)
        else:
            self.state.selected_lesson_urls.difference_update(urls)
        self._build_course_list()
        self._update_selected_count()

    def _build_side_panel(self):
        total_lessons = sum(course.lesson_count for course in self.data)

        def _stat_card() -> ft.Container:
            return ft.Container(
                padding=20,
                border_radius=16,
                bgcolor=Color.BG_CARD,
                border=ft.Border.all(1, Color.BORDER),
                shadow=Shadow.CARD,
                gradient=Gradient.CARD,
                content=ft.Column(
                    spacing=16,
                    controls=[
                        ft.Row(
                            spacing=10,
                            controls=[
                                ft.Container(
                                    width=40,
                                    height=40,
                                    border_radius=12,
                                    gradient=Gradient.ACCENT,
                                    content=ft.Icon(
                                        ft.Icons.DOWNLOAD_DONE_ROUNDED,
                                        size=20,
                                        color=Color.TEXT,
                                    ),
                                ),
                                ft.Text(
                                    "Загрузка",
                                    size=18,
                                    weight=ft.FontWeight.W_700,
                                    color=Color.TEXT,
                                ),
                            ],
                        ),
                        divider(),
                        ft.Container(
                            padding=16,
                            border_radius=12,
                            bgcolor="rgba(124,58,237,0.06)",
                            border=ft.Border.all(1, "rgba(124,58,237,0.12)"),
                            content=ft.Column(
                                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                                spacing=4,
                                controls=[
                                    ft.Row(
                                        alignment=ft.MainAxisAlignment.CENTER,
                                        spacing=6,
                                        controls=[
                                            self.selected_label,
                                            ft.Text(
                                                f"/ {total_lessons}",
                                                size=18,
                                                color=Color.TEXT_MUTED,
                                                weight=ft.FontWeight.W_500,
                                            ),
                                        ],
                                    ),
                                    body_text("уроков выбрано", size=13),
                                ],
                            ),
                        ),
                    ],
                ),
            )

        def _quality_card() -> ft.Container:
            return ft.Container(
                padding=20,
                border_radius=16,
                bgcolor=Color.BG_CARD,
                border=ft.Border.all(1, Color.BORDER),
                shadow=Shadow.CARD,
                gradient=Gradient.CARD,
                content=ft.Column(
                    spacing=12,
                    controls=[
                        ft.Text(
                            "Качество видео",
                            size=16,
                            weight=ft.FontWeight.W_600,
                            color=Color.TEXT,
                        ),
                        divider(),
                        ft.Dropdown(
                            value=self.state.quality,
                            options=[
                                ft.DropdownOption(key="auto", text="Авто (максимальное)"),
                                ft.DropdownOption(key="1080", text="1080p"),
                                ft.DropdownOption(key="720", text="720p"),
                                ft.DropdownOption(key="480", text="480p"),
                                ft.DropdownOption(key="360", text="360p"),
                            ],
                            on_select=self._on_quality_change,
                            text_style=ft.TextStyle(size=14, color=Color.TEXT),
                            bgcolor="rgba(255,255,255,0.04)",
                            border_color=Color.BORDER,
                            border_width=1,
                            border_radius=10,
                            focused_border_color=Color.ACCENT,
                            color=Color.TEXT,
                            height=48,
                        ),
                    ],
                ),
            )

        def _save_path_card() -> ft.Container:
            return ft.Container(
                padding=20,
                border_radius=16,
                bgcolor=Color.BG_CARD,
                border=ft.Border.all(1, Color.BORDER),
                shadow=Shadow.CARD,
                gradient=Gradient.CARD,
                content=ft.Column(
                    spacing=12,
                    controls=[
                        ft.Text(
                            "Папка сохранения",
                            size=16,
                            weight=ft.FontWeight.W_600,
                            color=Color.TEXT,
                        ),
                        divider(),
                        ft.Row(
                            spacing=8,
                            controls=[
                                ft.TextField(
                                    value=self.state.save_path,
                                    read_only=True,
                                    expand=True,
                                    hint_text="Выберите папку...",
                                    hint_style=ft.TextStyle(color=Color.TEXT_MUTED, size=13),
                                    color=Color.TEXT,
                                    bgcolor="rgba(255,255,255,0.04)",
                                    border_color=Color.BORDER,
                                    border_width=1,
                                    border_radius=10,
                                    text_style=ft.TextStyle(size=13),
                                    height=44,
                                ),
                                ft.Container(
                                    content=ft.Icon(
                                        ft.Icons.FOLDER_OPEN_ROUNDED,
                                        size=20,
                                        color=Color.TEXT,
                                    ),
                                    width=44,
                                    height=44,
                                    border_radius=10,
                                    gradient=Gradient.ACCENT,
                                    ink=True,
                                    on_click=self._pick_directory,
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ],
                ),
            )

        def _actions_card() -> ft.Container:
            return ft.Container(
                padding=20,
                border_radius=16,
                bgcolor=Color.BG_CARD,
                border=ft.Border.all(1, Color.BORDER),
                shadow=Shadow.CARD,
                gradient=Gradient.CARD,
                content=ft.Column(
                    spacing=10,
                    controls=[
                        ft.Text(
                            "Действия",
                            size=16,
                            weight=ft.FontWeight.W_600,
                            color=Color.TEXT,
                        ),
                        divider(),
                        ft.Container(
                            content=ft.Row(
                                spacing=10,
                                controls=[
                                    ft.Container(
                                        content=ft.Row(
                                            [
                                                ft.Icon(
                                                    ft.Icons.SELECT_ALL_ROUNDED,
                                                    size=18,
                                                    color=Color.TEXT_SECONDARY,
                                                ),
                                                ft.Text(
                                                    "Выбрать все",
                                                    size=14,
                                                    weight=ft.FontWeight.W_500,
                                                    color=Color.TEXT,
                                                ),
                                            ],
                                            spacing=6,
                                            alignment=ft.MainAxisAlignment.CENTER,
                                        ),
                                        padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                                        border_radius=10,
                                        border=ft.Border.all(1, Color.BORDER_STRONG),
                                        bgcolor="rgba(255,255,255,0.03)",
                                        ink=True,
                                        on_click=self._select_all,
                                        expand=True,
                                    ),
                                    ft.Container(
                                        content=ft.Row(
                                            [
                                                ft.Icon(
                                                    ft.Icons.DESELECT_ROUNDED,
                                                    size=18,
                                                    color=Color.TEXT_SECONDARY,
                                                ),
                                                ft.Text(
                                                    "Убрать все",
                                                    size=14,
                                                    weight=ft.FontWeight.W_500,
                                                    color=Color.TEXT,
                                                ),
                                            ],
                                            spacing=6,
                                            alignment=ft.MainAxisAlignment.CENTER,
                                        ),
                                        padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                                        border_radius=10,
                                        border=ft.Border.all(1, Color.BORDER_STRONG),
                                        bgcolor="rgba(255,255,255,0.03)",
                                        ink=True,
                                        on_click=self._unselect_all,
                                        expand=True,
                                    ),
                                ],
                            ),
                        ),
                        accent_button(
                            "Скачать выбранное",
                            on_click=self._start_download,
                            icon=ft.Icons.DOWNLOAD_ROUNDED,
                            height=48,
                        ),
                    ],
                ),
            )

        self.side_content.controls = [
            _stat_card(),
            _quality_card(),
            _save_path_card(),
            _actions_card(),
        ]

    def _on_quality_change(self, e):
        self.state.quality = e.control.value

    async def _pick_directory(self, e):
        kwargs = {"dialog_title": "Выберите папку для сохранения видео"}
        if Path(self.state.save_path).is_absolute():
            kwargs["initial_directory"] = self.state.save_path
        try:
            path = await self.file_picker.get_directory_path(**kwargs)
        except Exception:
            kwargs.pop("initial_directory", None)
            path = await self.file_picker.get_directory_path(**kwargs)
        if path:
            self.state.save_path = path
            self._controller.save_save_path(path)
            self._build_side_panel()
            self.page.update()

    def _delete_courses(self, e=None) -> None:
        self._request_clear("courses")

    def _clear_authorization(self, e=None) -> None:
        self._request_clear("authorization")

    def _clear_is_blocked(self) -> bool:
        return self.state.downloading or self.state.cancelling or self.state.clearing

    def _refresh_clear_actions(self) -> None:
        for name in ("_clear_courses_button", "_clear_authorization_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.disabled = self._clear_is_blocked()

    def _request_clear(self, target: ClearTarget) -> None:
        if self._confirmation_dialog is not None:
            return
        if self._clear_is_blocked():
            self._show_snack("Дождитесь завершения текущей операции", is_error=True)
            return

        async def confirm(_event):
            await self._confirm_clear(target)

        self._confirmation_dialog = build_clear_confirmation(
            target,
            on_confirm=confirm,
            on_cancel=self._close_clear_confirmation,
            on_dismiss=self._on_clear_confirmation_dismissed,
        )
        self.page.show_dialog(self._confirmation_dialog)

    def _close_clear_confirmation(self, _event=None) -> None:
        if self._confirmation_dialog is not None:
            self.page.pop_dialog()
            self._confirmation_dialog = None

    def _on_clear_confirmation_dismissed(self, _event=None) -> None:
        if _event is None or _event.control is self._confirmation_dialog:
            self._confirmation_dialog = None

    async def _confirm_clear(self, target: ClearTarget) -> None:
        if self._confirmation_dialog is None:
            return
        self._close_clear_confirmation()
        if self._clear_is_blocked():
            self._show_snack("Дождитесь завершения текущей операции", is_error=True)
            return
        self.state.clearing = True
        self._refresh_clear_actions()
        self.page.update()
        succeeded = False
        try:
            if target == "courses":
                await asyncio.to_thread(self._controller.delete_courses)
            else:
                await self._controller.clear_authorization()
            succeeded = True
        except DownloaderError as error:
            self._show_snack(str(error), is_error=True)
        except OSError:
            self._show_snack(
                "Не удалось выполнить очистку. Проверьте права доступа и повторите попытку.",
                is_error=True,
            )
        finally:
            self.state.clearing = False
            self._refresh_clear_actions()
            self.page.update()
        if succeeded:
            if target == "courses":
                await self._on_navigate_start()
            else:
                self._show_snack(
                    "Авторизация очищена. При следующем открытии курса войдите заново."
                )

    def _update_selected_count(self, e=None):
        self.selected_label.value = str(len(self.state.selected_lesson_urls))
        self.page.update()

    def _select_all(self, e):
        self.state.selected_lesson_urls = {
            item.lesson.url for course in self.data for item in iter_course_lessons(course)
        }
        self._build_course_list()
        self._update_selected_count()

    def _unselect_all(self, e):
        self.state.selected_lesson_urls.clear()
        self._build_course_list()
        self._update_selected_count()

    def _dismiss_error(self, e):
        self.error_overlay.visible = False
        self.page.update()

    async def _dismiss_error_and_pick(self, e):
        self.error_overlay.visible = False
        await self._pick_directory(None)
        self.page.update()

    @staticmethod
    def _log_color(line: str) -> str:
        if "Сегменты" in line or "сегмент" in line.lower():
            return "#F59E0B"
        if line.startswith(("✅", "✓")):
            return Color.GREEN
        if line.startswith("❌") or "Ошибка" in line:
            return Color.RED
        return Color.TEXT_SECONDARY

    @staticmethod
    def _parse_summary(summary_lines: list[str]) -> tuple[list[str], list[str]]:
        header: list[str] = []
        failed: list[str] = []
        for line in summary_lines:
            if line.startswith("✗"):
                failed.append(line)
            else:
                header.append(line)
        return header, failed

    async def _scroll_download_to(self, lesson_url: str) -> None:
        try:
            await self._download_rows_column.scroll_to(
                scroll_key=lesson_url,
                duration=650,
                curve=ft.AnimationCurve.EASE_IN_OUT_CUBIC,
            )
        except (asyncio.CancelledError, IndexError, RuntimeError):
            return

    def _schedule_download_scroll(self, lesson_url: str) -> None:
        previous = self._download_scroll_task
        if previous is not None and not previous.done():
            previous.cancel()
        try:
            self._download_scroll_task = self.page.run_task(
                self._scroll_download_to,
                lesson_url,
            )
        except (AttributeError, RuntimeError):
            self._download_scroll_task = None

    def _follow_download_lesson(self, lesson_url: str) -> None:
        self._active_lesson_url = lesson_url
        if not self._download_follow_paused:
            self._schedule_download_scroll(self._download_scroll_target(lesson_url))

    def _download_scroll_target(self, lesson_url: str) -> str:
        previous_url = lesson_url
        for candidate_url in self._download_rows:
            if candidate_url == lesson_url:
                return previous_url
            previous_url = candidate_url
        return lesson_url

    def _on_download_rows_scroll(self, event: ft.OnScrollEvent) -> None:
        if not self.state.downloading or event.event_type is not ft.ScrollType.USER:
            return
        if event.direction is ft.ScrollDirection.IDLE and not self._download_follow_paused:
            return
        self._download_follow_paused = True
        scroll_task = self._download_scroll_task
        if scroll_task is not None and not scroll_task.done():
            scroll_task.cancel()
        resume_task = self._download_follow_resume_task
        if resume_task is not None and not resume_task.done():
            resume_task.cancel()
        try:
            self._download_follow_resume_task = self.page.run_task(
                self._resume_download_follow,
            )
        except (AttributeError, RuntimeError):
            self._download_follow_resume_task = None

    async def _resume_download_follow(self) -> None:
        try:
            await asyncio.sleep(_DOWNLOAD_FOLLOW_PAUSE_SECONDS)
        except asyncio.CancelledError:
            return
        self._download_follow_resume_task = None
        self._download_follow_paused = False
        lesson_url = self._active_lesson_url
        if self.state.downloading and lesson_url:
            await self._scroll_download_to(self._download_scroll_target(lesson_url))

    def _reset_download_follow(self) -> None:
        for task in (self._download_scroll_task, self._download_follow_resume_task):
            if task is not None and not task.done():
                task.cancel()
        self._download_scroll_task = None
        self._download_follow_resume_task = None
        self._download_follow_paused = False
        self._active_lesson_url = None

    @staticmethod
    def _is_progress_line(text: str) -> bool:
        return "Сегменты:" in text or "Сегментов:" in text

    def _should_log(self, line: str) -> bool:
        stripped = line.lstrip()
        if stripped.startswith(("Старт скачивания", "✓", "❌")):
            return True
        if "▶" in line:
            return True
        return self._is_progress_line(line) or "Ошибка" in line

    def _update_download_title(self, line: str) -> bool:
        check = line.lower()
        if "сегмент" in check and "нет" not in check:
            title = "Загрузка видео"
        elif "не получен" in check:
            title = "Плейлист не найден"
        elif "получение запроса" in check:
            title = "Получение запроса"
        elif "▶" in line:
            title = "Загрузка страницы урока"
        elif "авторизац" in check:
            title = "Проверка авторизации"
        else:
            return False
        if self._download_title.value != title:
            self._download_title.value = title
            return True
        return False

    def _refresh_title(self, changed: bool):
        if changed:
            with contextlib.suppress(IndexError, RuntimeError):
                self.page.update()

    def _add_log(self, line: str):
        title_changed = self._update_download_title(line)
        if not self._should_log(line):
            self._refresh_title(title_changed)
            return
        self.log_lines.append(line)
        color = self._log_color(line)
        self._log_column.controls.append(ft.Text(line, size=13, color=color, selectable=False))
        with contextlib.suppress(IndexError, RuntimeError):
            self.page.update()

    def _update_last_log(self, line: str):
        title_changed = self._update_download_title(line)
        if not self._should_log(line):
            self._refresh_title(title_changed)
            return
        if self._log_column.controls:
            last = self._log_column.controls[-1]
            if (
                isinstance(last, ft.Text)
                and self._is_progress_line(line)
                and self._is_progress_line(last.value)
            ):
                last.value = line
                last.color = self._log_color(line)
                if self.log_lines:
                    self.log_lines[-1] = line
                with contextlib.suppress(IndexError, RuntimeError):
                    self.page.update()
                return
        self._add_log(line)

    def _show_continue_btn(self):
        self._continue_btn.visible = True
        self.page.update()

    def _switch_overlay_to_download(self):
        self._completion_flow = None
        self._overlay_card.width = 600
        self._overlay_card.height = None
        self._diagnostic_open = False
        self._diagnostic_previous_content = None
        if self._auth_overlay_task is not None:
            self._auth_overlay_task.cancel()
            self._auth_overlay_task = None

        self._download_title.value = "Подготовка"
        self._overlay_card.content = ft.Column(
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12,
            controls=[
                ft.Icon(ft.Icons.VIDEO_LIBRARY_ROUNDED, size=38, color=Color.ACCENT_LIGHT),
                self._download_title,
                self._download_rows_container,
                self._continue_btn,
                self._cancel_btn,
            ],
        )
        self.page.update()

    def _switch_overlay_to_auth(self):
        self._diagnostic_open = False
        self._diagnostic_previous_content = None
        if self._auth_overlay_task is not None:
            self._auth_overlay_task.cancel()
            self._auth_overlay_task = None

        self._continue_btn.visible = False
        self._auth_status.value = "Требуется авторизация"
        self._overlay_card.content = ft.Column(
            tight=True,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
            controls=[
                self._auth_icon,
                self._auth_title,
                self._auth_desc,
                self._auth_instructions,
                self._auth_status,
                self._continue_btn,
                self._cancel_btn,
            ],
        )
        self.page.update()

        try:
            loop = asyncio.get_running_loop()
            self._auth_overlay_task = loop.create_task(self._auth_overlay_countdown())
        except RuntimeError:
            self._continue_btn.visible = True
            self.page.update()

    async def _auth_overlay_countdown(self):
        try:
            for i in range(5, 0, -1):
                self._auth_status.value = f"Браузер откроется через {i} сек."
                try:
                    self.page.update()
                except Exception:
                    return
                await asyncio.sleep(1)

            self._auth_status.value = "Войдите в аккаунт в браузере"
            self._continue_btn.visible = True
            with contextlib.suppress(Exception):
                self.page.update()
        except asyncio.CancelledError:
            pass

    def _send_continue(self, e):
        self._continue_btn.visible = False
        self._switch_overlay_to_download()
        self.page.update()
        self._controller.continue_authentication()

    def _start_download(self, e):
        if self._clear_is_blocked():
            return
        lessons_to_download = selected_course_lessons(
            self.data,
            self.state.selected_lesson_urls,
        )
        if not lessons_to_download:
            self._show_snack("Нет выбранных уроков", is_error=True)
            return

        if not Path(self.state.save_path).is_dir():
            self.error_overlay.visible = True
            self.page.update()
            return

        self.state.downloading = True
        self.state.cancelling = False
        self._refresh_clear_actions()
        self._active_download_id += 1
        run_id = self._active_download_id
        self._reset_download_follow()
        self._cancel_btn.disabled = False
        self._cancel_btn.content = "Отмена"
        self._speed_text.value = "Средняя: —"
        self.log_lines.clear()
        self._log_column.controls.clear()
        self._diagnostic_reports.clear()
        self._diagnostic_reports_by_title.clear()
        self._download_rows.clear()
        self._download_rows_column.controls.clear()
        for item in lessons_to_download:
            row = build_download_lesson_row(
                item,
                on_details=partial(self._show_lesson_diagnostics, item.lesson.url),
            )
            self._download_rows[item.lesson.url] = row
            self._download_rows_column.controls.append(row.control)
        self._continue_btn.visible = False
        self._switch_overlay_to_download()
        self._overlay_card.opacity = 0
        self._overlay_card.offset = ft.Offset(0, 0.15)
        self.overlay.visible = True
        self.page.update()
        self._overlay_card.opacity = 1
        self._overlay_card.offset = ft.Offset(0, 0)
        self.page.update()

        self._add_log(f"Старт скачивания: {len(lessons_to_download)} уроков")

        request = self._controller.make_request(
            lessons_to_download,
            self.state.quality,
            self.state.save_path,
        )
        self._controller.start_download(
            request,
            on_event=lambda event: self.page.run_thread(
                partial(self._handle_download_event, event, run_id=run_id)
            ),
            on_finished=lambda summary: self.page.run_thread(
                partial(self._finish_summary, summary, run_id=run_id)
            ),
            on_failed=lambda error: self.page.run_thread(
                partial(self._finish_download, f"Ошибка: {error}", True, run_id=run_id)
            ),
        )

    def _handle_download_event(self, event: DownloadEvent, *, run_id: int | None = None) -> None:
        if run_id is not None and run_id != self._active_download_id:
            return
        self._remember_diagnostic(event)
        if event.type is DownloadEventType.AUTH_REQUIRED:
            self._switch_overlay_to_auth()
            return
        if event.type is DownloadEventType.AUTHENTICATED:
            self._switch_overlay_to_download()
        elif event.type is DownloadEventType.PROGRESS:
            self._download_title.value = "Загрузка видео"
            if event.speed_bps is not None:
                self._speed_text.value = f"Средняя: {self._format_speed(event.speed_bps)}"
            self._update_last_log(event.message)
            self._update_download_row(event)
            return
        elif event.type is DownloadEventType.LESSON_STARTED:
            self._download_title.value = "Проверяем видео"
            self._speed_text.value = "Средняя: —"
            self._add_log(f"▶ {event.lesson}")
            self._update_download_row(event)
            if event.lesson_url:
                self._follow_download_lesson(event.lesson_url)
            return
        elif event.type is DownloadEventType.VIDEO_FOUND:
            self._speed_text.value = "Средняя: —"
            self._update_download_row(event)
        elif event.type in {
            DownloadEventType.LESSON_COMPLETED,
            DownloadEventType.LESSON_SKIPPED,
            DownloadEventType.LESSON_NO_VIDEO,
        }:
            self._update_download_row(event)
        elif event.type is DownloadEventType.LESSON_FAILED or event.type is DownloadEventType.ERROR:
            self._update_download_row(event)
            self._add_log(f"❌ {event.message}")
            return
        elif event.message:
            prefix = "✓ " if event.type is DownloadEventType.LESSON_COMPLETED else ""
            self._add_log(f"{prefix}{event.message}")

    def _remember_diagnostic(self, event: DownloadEvent) -> None:
        if not event.diagnostic_report:
            return
        report = Path(event.diagnostic_report)
        if event.type is DownloadEventType.SUMMARY:
            return
        if event.lesson_url:
            self._diagnostic_reports[event.lesson_url] = report
        if event.lesson:
            self._diagnostic_reports_by_title[event.lesson] = report

    def _show_lesson_diagnostics(self, lesson_url: str) -> None:
        report = self._diagnostic_reports.get(lesson_url)
        if report is None:
            self._show_snack("Для этого урока нет отчёта", is_error=True)
            return
        self._show_diagnostic_report(report, "Отчёт по уроку")

    def _show_diagnostic_report(self, path: Path, title: str) -> None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            formatted = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        except (OSError, json.JSONDecodeError):
            self._show_snack("Не удалось открыть отчёт диагностики", is_error=True)
            return
        if not getattr(self, "_diagnostic_open", False):
            self._diagnostic_previous_content = self._overlay_card.content
            self._diagnostic_previous_visible = self.overlay.visible
            self._diagnostic_previous_height = self._overlay_card.height
            self._diagnostic_open = True
        in_completion = getattr(self, "_completion_flow", None) is not None
        if in_completion:
            self._overlay_card.height = 620
        self._overlay_card.content = ft.Column(
            expand=in_completion,
            tight=not in_completion,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12,
            controls=[
                ft.Icon(ft.Icons.BUG_REPORT_OUTLINED, size=38, color=Color.YELLOW),
                ft.Text(title, size=19, weight=ft.FontWeight.W_700, color=Color.TEXT),
                ft.Text(
                    str(path),
                    size=11,
                    color=Color.TEXT_MUTED,
                    selectable=True,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Container(
                    width=None if in_completion else 520,
                    height=None if in_completion else 360,
                    expand=in_completion,
                    padding=ft.Padding.all(12),
                    border_radius=10,
                    bgcolor="rgba(0,0,0,0.3)",
                    border=ft.Border.all(1, "rgba(255,255,255,0.08)"),
                    content=ft.Column(
                        scroll=ft.ScrollMode.AUTO,
                        controls=[
                            ft.Text(
                                formatted,
                                size=11,
                                color=Color.TEXT_SECONDARY,
                                selectable=True,
                                font_family="Consolas",
                            )
                        ],
                    ),
                ),
                ft.OutlinedButton("Назад", on_click=self._close_diagnostic_report),
            ],
        )
        self.overlay.visible = True
        self.page.update()

    def _close_diagnostic_report(self, _event=None) -> None:
        if not self._diagnostic_open:
            return
        self._overlay_card.content = self._diagnostic_previous_content
        self._overlay_card.height = getattr(self, "_diagnostic_previous_height", None)
        self.overlay.visible = self._diagnostic_previous_visible
        self._diagnostic_open = False
        self._diagnostic_previous_content = None
        self.page.update()

    @staticmethod
    def _format_speed(speed_bps: float) -> str:
        if speed_bps >= 1024 * 1024:
            return f"{speed_bps / (1024 * 1024):.1f} МБ/с"
        if speed_bps >= 1024:
            return f"{speed_bps / 1024:.0f} КБ/с"
        return f"{speed_bps:.0f} Б/с"

    def _update_download_row(self, event: DownloadEvent) -> None:
        if not event.lesson_url:
            return
        row = self._download_rows.get(event.lesson_url)
        if row is None:
            return

        if event.type is DownloadEventType.LESSON_STARTED:
            row.status_holder.content = ft.Icon(
                ft.Icons.VIDEO_FILE_ROUNDED,
                size=18,
                color=Color.ACCENT_LIGHT,
            )
            row.status_text.value = "Проверяем видео…"
            row.status_text.color = Color.ACCENT_LIGHT
        elif event.type is DownloadEventType.VIDEO_FOUND:
            row.status_holder.content = ft.Icon(
                ft.Icons.DOWNLOADING_ROUNDED,
                size=18,
                color=Color.ACCENT_LIGHT,
            )
            row.status_text.value = "Загрузка"
            row.status_text.color = Color.ACCENT_LIGHT
            row.progress.visible = True
            row.progress_text.visible = True
        elif event.type is DownloadEventType.PROGRESS:
            total = event.total or 0
            current = event.current or 0
            value = min(1.0, current / total) if total else 0
            row.progress.value = value
            row.progress.visible = True
            row.progress_text.visible = True
            row.progress_text.value = f"{round(value * 100)}%"
            row.status_text.value = "Загрузка"
            row.status_text.color = Color.ACCENT_LIGHT
        elif event.type is DownloadEventType.LESSON_COMPLETED:
            row.status_holder.content = ft.Icon(
                ft.Icons.CHECK_CIRCLE_ROUNDED,
                size=18,
                color=Color.GREEN,
            )
            row.status_text.value = f"Готово · {event.quality}" if event.quality else "Готово"
            row.status_text.color = Color.GREEN
            row.progress.visible = False
            row.progress_text.visible = False
        elif event.type is DownloadEventType.LESSON_SKIPPED:
            row.status_holder.content = ft.Icon(
                ft.Icons.CHECK_CIRCLE_OUTLINE_ROUNDED,
                size=18,
                color=Color.GREEN,
            )
            row.status_text.value = (
                f"Уже скачано · {event.quality}" if event.quality else "Уже скачано"
            )
            row.status_text.color = Color.GREEN
            row.progress.visible = False
            row.progress_text.visible = False
        elif event.type is DownloadEventType.LESSON_NO_VIDEO:
            row.status_holder.content = ft.Icon(
                ft.Icons.CANCEL_ROUNDED,
                size=18,
                color=Color.RED,
            )
            row.status_text.value = "Видео не найдено"
            row.status_text.color = Color.RED
            if event.diagnostic_report:
                row.details_button.visible = True
            row.progress.visible = False
            row.progress_text.visible = False
        elif event.type in {DownloadEventType.LESSON_FAILED, DownloadEventType.ERROR}:
            row.status_holder.content = ft.Icon(
                ft.Icons.ERROR_ROUNDED,
                size=18,
                color=Color.YELLOW,
            )
            row.status_text.value = "Ошибка"
            row.status_text.color = Color.YELLOW
            if event.diagnostic_report:
                row.details_button.visible = True
            row.progress.visible = False
            row.progress_text.visible = False
        self.page.update()

    def _finish_summary(self, summary: DownloadSummary, *, run_id: int | None = None) -> None:
        if run_id is not None and run_id != self._active_download_id:
            return
        if summary.cancelled:
            self._mark_unfinished_rows(
                "Остановлено",
                Color.TEXT_MUTED,
                ft.Icons.STOP_CIRCLE_OUTLINED,
            )
        lines = [f"Скачано: {summary.downloaded}"]
        if summary.already_present:
            lines.append(f"Уже было на диске: {summary.already_present}")
        if summary.no_video:
            lines.append(f"Без видео: {summary.no_video}")
        if summary.failed:
            lines.append(f"Ошибки: {len(summary.failed)}")
        if summary.cancelled:
            lines.append(f"Отменено: {summary.cancelled}")
        lines.append(f"Всего выбрано: {summary.total}")
        message = "\n".join(lines)
        failed = [f"✗ {title}" for title in summary.failed]
        self._finish_download(
            message,
            is_error=bool(summary.failed)
            and not (summary.downloaded or summary.already_present or summary.cancelled),
            is_warning=bool(summary.no_video or summary.failed or summary.cancelled),
            failed=failed,
            cancelled=bool(summary.cancelled),
        )

    def _finish_download(
        self,
        message: str,
        is_error: bool = False,
        is_warning: bool = False,
        failed: list[str] | None = None,
        *,
        run_id: int | None = None,
        cancelled: bool = False,
    ):
        if run_id is not None and run_id != self._active_download_id:
            return
        self.state.downloading = False
        self.state.cancelling = False
        self._refresh_clear_actions()
        self._reset_download_follow()
        self._cancel_btn.disabled = False
        self._cancel_btn.content = "Отмена"
        self._speed_text.value = "Средняя: —"
        if is_error:
            self._mark_unfinished_rows("Ошибка", Color.YELLOW, ft.Icons.ERROR_ROUNDED)
        self._show_completion_overlay(message, is_error, is_warning, failed, cancelled=cancelled)

    def _cancel_download(self, _event=None) -> None:
        if not self.state.downloading or self.state.cancelling:
            return
        self.state.cancelling = True
        self._cancel_btn.disabled = True
        self._cancel_btn.content = "Останавливаем…"
        self._download_title.value = "Останавливаем загрузку"
        self._controller.cancel()
        self.page.update()

    def _mark_unfinished_rows(self, text: str, color: str, icon: ft.IconData) -> None:
        terminal_prefixes = ("Готово", "Уже скачано", "Видео не найдено", "Ошибка")
        for row in self._download_rows.values():
            if str(row.status_text.value).startswith(terminal_prefixes):
                continue
            row.status_holder.content = ft.Icon(icon, size=18, color=color)
            row.status_text.value = text
            row.status_text.color = color
            row.progress.visible = False
            row.progress_text.visible = False

    def shutdown(self, timeout: float = 6.0) -> None:
        self._controller.shutdown(timeout)

    def dispose(self) -> None:
        self._reset_download_follow()
        if self.state.downloading:
            self._controller.cancel()

    def _show_completion_overlay(
        self,
        message: str,
        is_error: bool = False,
        is_warning: bool = False,
        failed: list[str] | None = None,
        *,
        cancelled: bool = False,
    ) -> None:
        self._diagnostic_open = False
        self._diagnostic_previous_content = None
        self._overlay_card.opacity = 1
        self._overlay_card.offset = ft.Offset(0, 0)
        self._completion_flow = CompletionFlow(
            CompletionResult(message, is_error, is_warning, cancelled),
            self._overlay_card,
            on_change=self.page.update,
            on_close=self._close_completion_overlay,
            on_github=self._open_github,
            on_contact=self._open_contact,
            failed_details=self._build_failed_lessons(failed) if failed else None,
        )
        self.overlay.visible = True
        self.page.update()

    def _build_failed_lessons(self, failed: list[str]) -> ft.Container:
        rows: list[ft.Control] = []
        reports_by_title = getattr(self, "_diagnostic_reports_by_title", {})
        for line in failed:
            title = line[1:].strip() if line.startswith("✗") else line
            report = reports_by_title.get(title)
            rows.append(
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=4, vertical=3),
                    border_radius=6,
                    ink=report is not None,
                    on_click=(
                        lambda _event, current=report, name=title: (
                            self._show_diagnostic_report(current, f"Отчёт: {name}")
                            if current is not None
                            else None
                        )
                    ),
                    content=ft.Row(
                        spacing=8,
                        controls=[
                            ft.Icon(ft.Icons.CLOSE_ROUNDED, size=14, color=Color.RED),
                            ft.Text(
                                title,
                                size=13,
                                color=Color.TEXT_SECONDARY,
                                selectable=False,
                                expand=True,
                            ),
                            ft.Text(
                                "Подробнее" if report is not None else "",
                                size=11,
                                color=Color.ACCENT_LIGHT,
                            ),
                        ],
                    ),
                )
            )
        return ft.Container(
            height=min(180, 48 * len(failed)),
            border_radius=10,
            bgcolor="rgba(0,0,0,0.3)",
            border=ft.Border.all(1, "rgba(255,255,255,0.06)"),
            padding=ft.Padding.all(12),
            content=ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO, controls=rows),
        )

    async def _open_github(self, _event=None) -> None:
        await self.page.launch_url(_GITHUB_URL)

    async def _open_contact(self, _event=None) -> None:
        await self.page.launch_url(_CONTACT_URL)

    def _close_completion_overlay(self, _event=None) -> None:
        flow = getattr(self, "_completion_flow", None)
        if flow is not None and flow.state.step is CompletionStep.RESULT:
            return
        self.overlay.visible = False
        self.page.update()

    def _show_snack(self, message: str, is_error: bool = False):
        bg = "#1A0A0A" if is_error else "#0A1A15"
        icon = ft.Icons.ERROR_OUTLINE if is_error else ft.Icons.CHECK_CIRCLE_OUTLINE
        icon_color = Color.RED if is_error else Color.GREEN

        snack_bar = ft.SnackBar(
            content=ft.Row(
                [
                    ft.Icon(icon, color=icon_color, size=18),
                    ft.Text(message, color=Color.TEXT, size=13, expand=True),
                ],
                spacing=6,
            ),
            bgcolor=bg,
            shape=ft.RoundedRectangleBorder(radius=10),
            duration=4000,
            margin=ft.Margin.only(bottom=16, left=16, right=16),
            behavior=ft.SnackBarBehavior.FLOATING,
            elevation=8,
        )
        self.page.show_dialog(snack_bar)
