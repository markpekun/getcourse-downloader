import json
import threading
from pathlib import Path

import flet as ft

from app.theme import Color, Gradient, Shadow, accent_button, body_text, divider

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_COURSES_PATH = _PROJECT_ROOT / "app" / "data" / "courses.json"

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

    @staticmethod
    def has_courses() -> bool:
        file = _COURSES_PATH
        if not file.exists():
            return False
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
            return isinstance(data, list) and len(data) > 0
        except Exception:
            return False

    def __init__(self, page: ft.Page):
        self.page = page
        self._downloading = False

        with open(_COURSES_PATH, "r", encoding="utf-8") as f:
            self.data: list = json.load(f)

        self.expanded_courses: set[int] = set(range(len(self.data)))
        self.lesson_refs: dict[int, list[ft.Checkbox]] = {}
        self._register: list[ft.Checkbox] = []

        self._quality = "auto"
        self._save_path = "downloads"

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

        self.course_list = ft.Column(spacing=12, scroll=ft.ScrollMode.AUTO, expand=True)
        self._build_course_list()

        self.side_content = ft.Column(spacing=16)
        self._build_side_panel()

        self.view = ft.Container(
            expand=True,
            gradient=Gradient.BG_PRIMARY,
            content=ft.Column(
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
        )
        self._update_selected_count()

    def _build_header(self) -> ft.Container:
        total_lessons = sum(len(c["lessons"]) for c in self.data)
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
                                            ft.Icon(ft.Icons.FOLDER_OPEN_ROUNDED, size=14, color=Color.ACCENT_LIGHT),
                                            ft.Text(
                                                str(len(self.data)),
                                                size=13,
                                                weight=ft.FontWeight.W_600,
                                                color=Color.ACCENT_LIGHT,
                                            ),
                                        ],
                                        spacing=4,
                                    ),
                                    padding=ft.Padding.symmetric(horizontal=8, vertical=4),
                                    border_radius=6,
                                    bgcolor="rgba(124,58,237,0.12)",
                                ),
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
        self._register.clear()
        for idx, course in enumerate(self.data):
            card = self._build_course_card(idx, course)
            self.course_list.controls.append(card)

    def _build_course_card(self, idx: int, course: dict) -> ft.Container:
        accent = _COURSE_COLORS[idx % len(_COURSE_COLORS)]
        lessons = course.get("lessons", [])
        is_expanded = idx in self.expanded_courses

        lesson_checks: list[ft.Checkbox] = []
        for lesson in lessons:
            cb = ft.Checkbox(
                label=lesson.get("title", "Без названия"),
                value=False,
                tristate=False,
                on_change=self._update_selected_count,
                active_color=accent,
                check_color=Color.TEXT,
                fill_color={
                    ft.ControlState.DEFAULT: ft.Colors.with_opacity(0.08, accent),
                    ft.ControlState.SELECTED: accent,
                },
                label_style=ft.TextStyle(
                    size=14,
                    color=Color.TEXT,
                    weight=ft.FontWeight.W_400,
                ),
                semantics_label=lesson.get("title", ""),
            )
            lesson_checks.append(cb)

        self.lesson_refs[idx] = lesson_checks
        self._register.extend(lesson_checks)

        lesson_list = ft.Column(spacing=2, controls=lesson_checks)
        body = lesson_list if is_expanded else ft.Container(height=0)

        selected_count = sum(1 for c in lesson_checks if c.value)

        return ft.Container(
            border_radius=16,
            bgcolor=Color.BG_CARD,
            border=ft.Border.all(1, Color.BORDER),
            shadow=Shadow.CARD,
            gradient=Gradient.CARD,
            animate=ft.Animation(200, ft.AnimationCurve.EASE_OUT),
            content=ft.Column(
                spacing=0,
                controls=[
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=20, vertical=14),
                        ink=True,
                        on_click=lambda _, i=idx: self._toggle_course(i),
                        content=ft.Row(
                            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            controls=[
                                ft.Row(
                                    spacing=14,
                                    controls=[
                                        ft.Container(
                                            width=4,
                                            height=32,
                                            border_radius=2,
                                            bgcolor=accent,
                                        ),
                                        ft.Column(
                                            spacing=2,
                                            controls=[
                                                ft.Text(
                                                    course.get("course_title", "Без названия"),
                                                    size=16,
                                                    weight=ft.FontWeight.W_600,
                                                    color=Color.TEXT,
                                                ),
                                                body_text(
                                                    f"{len(lessons)} уроков",
                                                    size=12,
                                                ),
                                            ],
                                        ),
                                    ],
                                ),
                                ft.Row(
                                    spacing=12,
                                    controls=[
                                        ft.Container(
                                            content=ft.Text(
                                                str(selected_count),
                                                size=13,
                                                weight=ft.FontWeight.W_600,
                                                color=accent,
                                            ),
                                            padding=ft.Padding.symmetric(horizontal=10, vertical=4),
                                            border_radius=8,
                                            bgcolor=ft.Colors.with_opacity(0.12, accent),
                                        ),
                                        ft.Icon(
                                            ft.Icons.EXPAND_MORE_ROUNDED
                                            if is_expanded
                                            else ft.Icons.CHEVRON_RIGHT_ROUNDED,
                                            size=22,
                                            color=Color.TEXT_SECONDARY,
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ),
                    body,
                ],
            ),
        )

    def _toggle_course(self, idx: int):
        if idx in self.expanded_courses:
            self.expanded_courses.discard(idx)
        else:
            self.expanded_courses.add(idx)
        self._build_course_list()
        self.page.update()

    def _on_search(self, e):
        query = e.control.value.strip().lower()
        if not query:
            for card in self.course_list.controls:
                card.visible = True
            for refs in self.lesson_refs.values():
                for cb in refs:
                    cb.visible = True
            self.page.update()
            return

        for idx, course in enumerate(self.data):
            lessons = course.get("lessons", [])
            refs = self.lesson_refs.get(idx, [])
            course_matches = any(
                query in lesson.get("title", "").lower() for lesson in lessons
            )
            card = self.course_list.controls[idx] if idx < len(self.course_list.controls) else None
            if card:
                card.visible = course_matches or any(
                    query in cb.label.lower() for cb in refs
                )
            for cb in refs:
                cb.visible = cb.label and query in cb.label.lower()
        self.page.update()

    def _build_side_panel(self):
        total_lessons = sum(len(c["lessons"]) for c in self.data)

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
                            value=self._quality,
                            options=[
                                ft.DropdownOption(key="auto", text="Авто"),
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
                                    value=self._save_path,
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
                                                ft.Icon(ft.Icons.SELECT_ALL_ROUNDED, size=18, color=Color.TEXT_SECONDARY),
                                                ft.Text("Все", size=14, weight=ft.FontWeight.W_500, color=Color.TEXT),
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
                                                ft.Icon(ft.Icons.DESELECT_ROUNDED, size=18, color=Color.TEXT_SECONDARY),
                                                ft.Text("Нет", size=14, weight=ft.FontWeight.W_500, color=Color.TEXT),
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
        self._quality = e.control.value

    async def _pick_directory(self, e):
        path = await self.file_picker.get_directory_path(
            dialog_title="Выберите папку для сохранения видео",
            initial_directory=self._save_path,
        )
        if path:
            self._save_path = path
            self._build_side_panel()
            self.page.update()

    def _update_selected_count(self, e=None):
        selected = sum(1 for cb in self._register if cb.value)
        self.selected_label.value = str(selected)
        for idx, refs in self.lesson_refs.items():
            count = sum(1 for cb in refs if cb.value)
            card = self.course_list.controls[idx] if idx < len(self.course_list.controls) else None
            if card:
                accent = _COURSE_COLORS[idx % len(_COURSE_COLORS)]
                self._update_card_badge(card, count, accent)
        self.page.update()

    def _update_card_badge(self, card: ft.Container, count: int, accent: str):
        column = card.content
        if not isinstance(column, ft.Column) or not column.controls:
            return
        header = column.controls[0]
        if not isinstance(header, ft.Container):
            return
        row = header.content
        if not isinstance(row, ft.Row):
            return
        right_side = row.controls[-1]
        if not isinstance(right_side, ft.Row):
            return
        badge = right_side.controls[0]
        if isinstance(badge, ft.Container) and isinstance(badge.content, ft.Text):
            badge.content.value = str(count)

    def _select_all(self, e):
        for cb in self._register:
            cb.value = True
        self._update_selected_count()

    def _unselect_all(self, e):
        for cb in self._register:
            cb.value = False
        self._update_selected_count()

    def _start_download(self, e):
        selected = [cb for cb in self._register if cb.value]
        if not selected:
            self._show_snack("Нет выбранных уроков", is_error=True)
            return
        if self._downloading:
            return
        self._downloading = True

        lessons_to_download = []
        for idx, course in enumerate(self.data):
            refs = self.lesson_refs.get(idx, [])
            for i, cb in enumerate(refs):
                if cb.value and i < len(course["lessons"]):
                    lessons_to_download.append({
                        "course_title": course["course_title"],
                        "lesson": course["lessons"][i],
                    })

        self._show_snack(f"Загрузка {len(lessons_to_download)} уроков...")

        threading.Thread(
            target=self._run_download,
            args=(lessons_to_download,),
            daemon=True,
        ).start()

    def _run_download(self, lessons: list):
        import subprocess
        import sys
        page = self.page
        try:
            subprocess.run(
                [
                    sys.executable, "givereq.py",
                    "--quality", self._quality,
                    "--save-path", self._save_path,
                ],
                check=True,
            )
            page.run_thread(lambda: self._show_snack("Загрузка завершена"))
        except Exception as ex:
            page.run_thread(lambda: self._show_snack(f"Ошибка: {ex}", is_error=True))
        finally:
            self._downloading = False

    def _show_snack(self, message: str, is_error: bool = False):
        bg = "#1A0A0A" if is_error else "#0A1A15"
        icon = ft.Icons.ERROR_OUTLINE if is_error else ft.Icons.CHECK_CIRCLE_OUTLINE
        icon_color = Color.RED if is_error else Color.GREEN

        self.page.snack_bar = ft.SnackBar(
            content=ft.Row(
                [ft.Icon(icon, color=icon_color, size=18), ft.Text(message, color=Color.TEXT, size=13, expand=True)],
                spacing=6,
            ),
            bgcolor=bg,
            shape=ft.RoundedRectangleBorder(radius=10),
            duration=4000,
            margin=ft.Margin.only(bottom=16, left=16, right=16),
            behavior=ft.SnackBarBehavior.FLOATING,
            elevation=8,
        )
        self.page.snack_bar.open = True
        self.page.update()
