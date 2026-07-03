import asyncio
import json
from pathlib import Path

import flet as ft
from playwright.async_api import async_playwright

from app.screens.courses_screen import CoursesScreen
from app.scripts.parse_courses import parse_courses
from app.theme import Color, Gradient, Shadow, accent_button, body_text, divider

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_PATH = _PROJECT_ROOT / "app" / "data" / "courses.json"


class StartScreen:

    _DECO_CIRCLES = [
        dict(left=-80, top=-60, size=300, color="rgba(124,58,237,0.10)"),
        dict(right=-120, bottom=-80, size=380, color="rgba(236,72,153,0.06)"),
        dict(left=140, bottom=30, size=150, color="rgba(16,185,129,0.05)"),
    ]

    def __init__(self, page: ft.Page):
        self.page = page
        self._parse_running = False
        self._auth_event = asyncio.Event()

        self.url_input = ft.TextField(
            hint_text="https://school.beilbei.ru/teach/control/stream/view/id/...",
            hint_style=ft.TextStyle(color=Color.TEXT_MUTED, size=13),
            color=Color.TEXT,
            bgcolor="rgba(255,255,255,0.04)",
            border_color=Color.BORDER,
            border_width=1.5,
            border_radius=10,
            focused_border_color=Color.ACCENT,
            focused_bgcolor="rgba(124,58,237,0.06)",
            cursor_color=Color.ACCENT,
            text_style=ft.TextStyle(size=14),
            prefix_icon=ft.Icons.LINK_ROUNDED,
            prefix_style=ft.TextStyle(color=Color.TEXT_MUTED),
            height=46,
            expand=True,
            on_submit=lambda e: self._start_parse(),
        )

        self._build_auth_widgets()

        self._loading_title = ft.Text(
            "",
            size=15,
            weight=ft.FontWeight.W_600,
            color=Color.TEXT,
            text_align=ft.TextAlign.CENTER,
        )
        self._loading_subtitle = ft.Text(
            "",
            size=12,
            color=Color.TEXT_SECONDARY,
            text_align=ft.TextAlign.CENTER,
        )
        self._dot_animation = ft.Text(
            "",
            size=22,
            color=Color.ACCENT_LIGHT,
            text_align=ft.TextAlign.CENTER,
            height=28,
        )

        self._discovery_list = ft.Column(spacing=4)
        self._discovery_counter = ft.Text(
            "",
            size=13,
            color=Color.ACCENT_LIGHT,
            weight=ft.FontWeight.W_500,
            text_align=ft.TextAlign.CENTER,
        )
        self._total_parsed = 0

        self._loading_task: asyncio.Task | None = None
        self._dot_task: asyncio.Task | None = None

        self.loader = ft.Container(
            visible=False,
            expand=True,
            bgcolor="rgba(0,0,0,0.45)",
            content=ft.Container(expand=True, alignment=ft.Alignment(0, 0)),
        )

        self.view = ft.Container(
            expand=True,
            gradient=Gradient.BG_PRIMARY,
            content=ft.Stack(
                expand=True,
                controls=[
                    *self._build_decoration(),
                    ft.Container(
                        expand=True,
                        content=ft.Column(
                            [
                                ft.Container(expand=1),
                                self._build_hero(),
                                ft.Container(expand=1),
                            ],
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    self.loader,
                ],
            ),
        )

    def _build_auth_widgets(self):
        steps = [
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
                                f"{i+1}.", size=12,
                                color=Color.ACCENT_LIGHT,
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.Text(
                                step, size=12, color=Color.TEXT_SECONDARY,
                            ),
                        ],
                    )
                    for i, step in enumerate(steps)
                ],
            ),
        )

        self._auth_status = ft.Text(
            "Браузер откроется через 10 сек.",
            size=13,
            color=Color.ACCENT_LIGHT,
            weight=ft.FontWeight.W_500,
            text_align=ft.TextAlign.CENTER,
            height=20,
        )

        self._auth_button = ft.ElevatedButton(
            "Продолжить",
            on_click=self._on_auth_ready,
            disabled=True,
            style=ft.ButtonStyle(
                color=ft.Colors.WHITE,
                bgcolor=Color.ACCENT,
                padding=ft.Padding.symmetric(horizontal=28, vertical=10),
                text_style=ft.TextStyle(size=14, weight=ft.FontWeight.W_600),
                shape=ft.RoundedRectangleBorder(radius=10),
            ),
        )

        self._auth_error = ft.Text(
            "",
            color="#FF6B6B",
            size=12,
            text_align=ft.TextAlign.CENTER,
            visible=False,
            height=16,
        )

    def _build_decoration(self):
        circles = []
        for c in self._DECO_CIRCLES:
            size = int(c["size"])
            circles.append(
                ft.Container(
                    width=size,
                    height=size,
                    border_radius=size // 2,
                    gradient=ft.RadialGradient(
                        colors=[c["color"], "rgba(0,0,0,0)"],
                    ),
                    left=c.get("left"),
                    right=c.get("right"),
                    top=c.get("top"),
                    bottom=c.get("bottom"),
                )
            )
        return circles

    def _build_hero(self):
        return ft.Container(
            width=560,
            padding=ft.Padding.symmetric(horizontal=32, vertical=32),
            border_radius=20,
            bgcolor=Color.BG_CARD,
            border=ft.Border.all(1, Color.BORDER),
            shadow=Shadow.CARD,
            gradient=Gradient.CARD,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=16,
                controls=[
                    self._build_logo(),
                    ft.Text(
                        "GetCourse Downloader",
                        size=26,
                        weight=ft.FontWeight.W_800,
                        color=Color.TEXT,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    body_text(
                        "Вставьте ссылку на страницу курса из GetCourse,\n"
                        "и приложение найдёт все уроки для скачивания.",
                        size=14,
                    ),
                    divider(),
                    ft.Row(controls=[self.url_input], spacing=0),
                    ft.Container(
                        content=self._build_parse_button(),
                        width=360,
                    ),
                    body_text(
                        "Нажмите ↵ Enter в поле или кнопку",
                        size=11,
                        color=Color.TEXT_MUTED,
                    ),
                ],
            ),
        )

    def _build_logo(self):
        return ft.Container(
            width=56,
            height=56,
            border_radius=14,
            gradient=Gradient.ACCENT,
            shadow=Shadow.GLOW_PRIMARY,
            content=ft.Icon(
                ft.Icons.DOWNLOAD_ROUNDED,
                size=28,
                color=Color.TEXT,
            ),
        )

    def _build_parse_button(self):
        return accent_button(
            "Загрузить курсы",
            on_click=lambda _: self._start_parse(),
            icon=ft.Icons.DOWNLOAD_ROUNDED,
            height=46,
        )

    def _show_simple_overlay(self, title: str, subtitle: str = "") -> None:
        self._stop_all_animations()
        self._loading_title.value = title
        self._loading_subtitle.value = subtitle
        self._dot_animation.value = ""

        self.loader.content = ft.Container(
            expand=True,
            alignment=ft.Alignment(0, 0),
            content=ft.Container(
                width=420,
                padding=ft.Padding.symmetric(horizontal=24, vertical=18),
                border_radius=16,
                bgcolor=Color.BG_CARD,
                border=ft.Border.all(1, Color.BORDER),
                gradient=Gradient.CARD,
                shadow=Shadow.CARD_ELEVATED,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                    controls=[
                        ft.Container(
                            width=34,
                            height=34,
                            border_radius=10,
                            gradient=Gradient.ACCENT,
                            content=ft.Icon(
                                ft.Icons.HOURGLASS_TOP_ROUNDED,
                                size=18,
                                color=Color.TEXT,
                            ),
                        ),
                        self._loading_title,
                        self._loading_subtitle,
                        self._dot_animation,
                    ],
                ),
            ),
        )

    def _show_auth_card(self) -> None:
        self._stop_all_animations()
        self._auth_error.visible = False
        self._auth_button.disabled = True
        self._auth_status.value = "Браузер откроется через 10 сек."

        self.loader.content = ft.Container(
            expand=True,
            alignment=ft.Alignment(0, 0),
            content=ft.Container(
                width=420,
                padding=ft.Padding.symmetric(horizontal=28, vertical=22),
                border_radius=18,
                bgcolor=Color.BG_CARD,
                border=ft.Border.all(1, Color.BORDER),
                gradient=Gradient.CARD,
                shadow=Shadow.CARD_ELEVATED,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=8,
                    controls=[
                        ft.Container(
                            width=38,
                            height=38,
                            border_radius=11,
                            gradient=Gradient.ACCENT,
                            shadow=Shadow.GLOW_PRIMARY,
                            content=ft.Icon(
                                ft.Icons.LOCK_ROUNDED,
                                size=19,
                                color=Color.TEXT,
                            ),
                        ),
                        ft.Text(
                            "Требуется вход",
                            size=17,
                            weight=ft.FontWeight.W_700,
                            color=Color.TEXT,
                        ),
                        ft.Text(
                            "Для доступа к материалам необходимо\nвыполнить вход в аккаунт.",
                            size=12,
                            color=Color.TEXT_SECONDARY,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        self._auth_instructions,
                        self._auth_status,
                        ft.Container(
                            content=self._auth_button,
                            padding=ft.Padding.symmetric(vertical=2),
                        ),
                        self._auth_error,
                    ],
                ),
            ),
        )

    def _show_course_discovery(self) -> None:
        self._stop_all_animations()
        self._discovery_list.controls.clear()
        self._discovery_counter.value = ""
        self._total_parsed = 0

        self.loader.content = ft.Container(
            expand=True,
            alignment=ft.Alignment(0, 0),
            content=ft.Container(
                width=460,
                padding=ft.Padding.symmetric(horizontal=28, vertical=24),
                border_radius=18,
                bgcolor=Color.BG_CARD,
                border=ft.Border.all(1, Color.BORDER),
                gradient=Gradient.CARD,
                shadow=Shadow.CARD_ELEVATED,
                content=ft.Column(
                    horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                    spacing=12,
                    controls=[
                        ft.Container(
                            width=38,
                            height=38,
                            border_radius=11,
                            gradient=Gradient.ACCENT,
                            content=ft.Icon(
                                ft.Icons.DOWNLOADING_ROUNDED,
                                size=20,
                                color=Color.TEXT,
                            ),
                        ),
                        ft.Text(
                            "Получаем список курсов",
                            size=17,
                            weight=ft.FontWeight.W_700,
                            color=Color.TEXT,
                        ),
                        ft.Text(
                            "Курсы...",
                            size=12,
                            color=Color.TEXT_SECONDARY,
                            text_align=ft.TextAlign.CENTER,
                        ),
                        divider(),
                        ft.Container(
                            content=self._discovery_list,
                            height=180,
                            border_radius=10,
                            bgcolor="rgba(0,0,0,0.15)",
                            padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                        ),
                        self._discovery_counter,
                    ],
                ),
            ),
        )

    async def _on_course_parsed(self, title: str, lesson_count: int) -> None:
        self._total_parsed += 1
        _MAX_VISIBLE = 4

        card = ft.Container(
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            border_radius=8,
            bgcolor="rgba(124,58,237,0.08)",
            animate_opacity=ft.Animation(200, ft.AnimationCurve.EASE_IN),
            opacity=0,
            content=ft.Row(
                controls=[
                    ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, size=14, color=Color.GREEN),
                    ft.Text(title, size=12, color=Color.TEXT, expand=True),
                    ft.Container(
                        content=ft.Text(f"{lesson_count} ур.", size=11, color=Color.TEXT_MUTED),
                        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                        border_radius=4,
                        bgcolor="rgba(255,255,255,0.05)",
                    ),
                ],
                spacing=8,
            ),
        )

        self._discovery_list.controls.append(card)

        if len(self._discovery_list.controls) > _MAX_VISIBLE:
            self._discovery_list.controls.pop(0)

        self._discovery_counter.value = f"Найдено {self._total_parsed} курсов"
        self.page.update()
        await asyncio.sleep(0.05)
        card.opacity = 1
        self.page.update()

    def _show_auth_error(self, message: str) -> None:
        self._auth_error.value = message
        self._auth_error.visible = True
        self.page.update()

    def _stop_all_animations(self) -> None:
        if self._loading_task is not None:
            self._loading_task.cancel()
            self._loading_task = None
        if self._dot_task is not None:
            self._dot_task.cancel()
            self._dot_task = None

    def _start_text_animation(self, target: ft.Text, base_text: str) -> None:
        if self._loading_task is not None:
            self._loading_task.cancel()
        self._loading_task = asyncio.create_task(
            self._animate_text(target, base_text)
        )

    def _start_dot_animation(self) -> None:
        if self._dot_task is not None:
            self._dot_task.cancel()
        self._dot_task = asyncio.create_task(self._animate_dots())

    async def _animate_dots(self) -> None:
        states = ["●", "●●", "●●●", "●●"]
        idx = 0
        try:
            while True:
                self._dot_animation.value = states[idx % 4]
                self.page.update()
                idx += 1
                await asyncio.sleep(0.45)
        except asyncio.CancelledError:
            self._dot_animation.value = ""
            try:
                self.page.update()
            except Exception:
                pass
            raise

    async def _animate_text(self, target: ft.Text, base_text: str) -> None:
        dots = ["", ".", "..", "..."]
        idx = 0
        try:
            while True:
                target.value = base_text + dots[idx % 4]
                self.page.update()
                idx += 1
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            target.value = base_text
            try:
                self.page.update()
            except Exception:
                pass
            raise

    def _start_parse(self):
        url = self.url_input.value.strip()
        if not url or self._parse_running:
            return

        self._parse_running = True
        self.loader.visible = True
        self._show_simple_overlay("Проверяем доступ")
        self._start_dot_animation()
        self.page.update()
        self.page.run_task(self._parse_async, url)

    def _on_auth_ready(self, e) -> None:
        self._auth_event.set()

    async def _parse_async(self, url: str) -> None:
        try:
            async with async_playwright() as p:
                browser = await p.firefox.launch_persistent_context(
                    "session_data",
                    headless=True,
                )
                page_obj = browser.pages[0] if browser.pages else await browser.new_page()
                await page_obj.goto(url, wait_until="domcontentloaded")
                needs_auth: bool = "login" in page_obj.url.lower()
                await browser.close()

                if needs_auth:
                    self.page.window.width = 520
                    self.page.window.height = 480
                    await self.page.window.center()
                    self._show_auth_card()
                    self.page.update()

                    for i in range(10, 0, -1):
                        self._auth_status.value = f"Браузер откроется через {i} сек."
                        self.page.update()
                        await asyncio.sleep(1)

                    self._auth_button.disabled = False
                    self._start_text_animation(self._auth_status, "Ожидаем вход")
                    self.page.update()

                    login_browser = await p.firefox.launch_persistent_context(
                        "session_data",
                        headless=False,
                    )
                    login_page = (
                        login_browser.pages[0]
                        if login_browser.pages
                        else await login_browser.new_page()
                    )
                    await login_page.goto(url)

                    self._auth_event.clear()
                    while True:
                        await self._auth_event.wait()
                        self._auth_event.clear()

                        if self._loading_task:
                            self._loading_task.cancel()
                            self._loading_task = None
                        self._auth_status.value = "Проверяем..."
                        self.page.update()

                        check_page = await login_browser.new_page()
                        try:
                            await check_page.goto(
                                url, wait_until="domcontentloaded", timeout=15_000
                            )
                            if "login" not in check_page.url.lower():
                                await check_page.close()
                                break
                            self._show_auth_error(
                                "Вход ещё не выполнен.\nПожалуйста, войдите в браузере."
                            )
                        except Exception:
                            self._show_auth_error(
                                "Ошибка проверки.\nПопробуйте ещё раз."
                            )
                        finally:
                            try:
                                await check_page.close()
                            except Exception:
                                pass

                        self._start_text_animation(self._auth_status, "Ожидаем вход")
                        self.page.update()

                    await login_browser.close()

                self._show_course_discovery()
                self.page.window.width = 680
                self.page.window.height = 460
                await self.page.window.center()
                self.page.update()

                courses = await parse_courses(
                    p, url,
                    on_course_parsed=self._on_course_parsed,
                )
                self._stop_all_animations()

            output_path = _DATA_PATH
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(courses, ensure_ascii=False, indent=4),
                encoding="utf-8",
            )

            self.loader.visible = False
            self.page.clean()
            self.page.window.width = 1400
            self.page.window.height = 850
            screen = CoursesScreen(self.page)
            self.page.add(screen.view)
            await self.page.window.center()
            self.page.update()

        except Exception as ex:
            self._stop_all_animations()
            self._parse_running = False
            self.loader.visible = False
            self.page.update()
            self._show_error(str(ex))

    def _show_error(self, message: str):
        self.page.snack_bar = ft.SnackBar(
            content=ft.Row(
                [
                    ft.Icon(ft.Icons.ERROR_OUTLINE, color=Color.RED, size=20),
                    ft.Text(message, color=Color.TEXT, size=14, expand=True),
                ],
                spacing=8,
            ),
            bgcolor="#2A1A1A",
            shape=ft.RoundedRectangleBorder(radius=12),
            duration=5000,
            margin=ft.Margin.only(bottom=20, left=20, right=20),
            behavior=ft.SnackBarBehavior.FLOATING,
            elevation=10,
        )
        self.page.snack_bar.open = True
        self.page.update()
