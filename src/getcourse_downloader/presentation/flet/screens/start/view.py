import asyncio
import concurrent.futures
import contextlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar, TypedDict

import flet as ft

from getcourse_downloader.application.ports.discovery import CourseDiscoveryUpdate
from getcourse_downloader.domain.errors import DownloaderError
from getcourse_downloader.presentation.flet.screens.start.controller import StartController
from getcourse_downloader.presentation.flet.screens.start.state import StartViewState
from getcourse_downloader.presentation.flet.theme import (
    Color,
    Gradient,
    Shadow,
    accent_button,
    body_text,
    divider,
)

_DISCOVERY_SCROLL_DELAY_SECONDS = 0.55


class _DecorationCircle(TypedDict, total=False):
    left: int
    right: int
    top: int
    bottom: int
    size: int
    color: str


@dataclass(frozen=True)
class _ErrorGuidance:
    title: str
    summary: str
    steps: tuple[str, ...]


class StartScreen:
    _DECO_CIRCLES: ClassVar[list[_DecorationCircle]] = [
        {"left": -80, "top": -60, "size": 300, "color": "rgba(124,58,237,0.10)"},
        {"right": -120, "bottom": -80, "size": 380, "color": "rgba(236,72,153,0.06)"},
        {"left": 140, "bottom": 30, "size": 150, "color": "rgba(16,185,129,0.05)"},
    ]

    def __init__(
        self,
        page: ft.Page,
        controller: StartController,
        on_courses_ready: Callable[[], Awaitable[None]],
    ):
        self.page = page
        self._controller = controller
        self._on_courses_ready = on_courses_ready
        self.state = StartViewState()
        self._auth_event = asyncio.Event()

        self.url_input = ft.TextField(
            hint_text="https://school.example/teach/control/stream/view/id/...",
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

        self._discovery_list = ft.Column(spacing=4, scroll=ft.ScrollMode.AUTO)
        self._discovery_updates: dict[str, CourseDiscoveryUpdate] = {}
        self._discovery_counter = ft.Text(
            "",
            size=13,
            color=Color.ACCENT_LIGHT,
            weight=ft.FontWeight.W_500,
            text_align=ft.TextAlign.CENTER,
        )
        self._loading_task: asyncio.Task | None = None
        self._dot_task: asyncio.Task | None = None
        self._discovery_scroll_task: asyncio.Task[None] | None = None
        self._pending_discovery_scroll_url: str | None = None
        self._parse_task: concurrent.futures.Future | None = None

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
                                f"{i + 1}.",
                                size=12,
                                color=Color.ACCENT_LIGHT,
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.Text(
                                step,
                                size=12,
                                color=Color.TEXT_SECONDARY,
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
        self._discovery_list.controls = [
            ft.Container(
                padding=ft.Padding.symmetric(horizontal=10, vertical=8),
                border_radius=8,
                bgcolor="rgba(124,58,237,0.08)",
                content=ft.Row(
                    spacing=8,
                    controls=[
                        ft.Icon(
                            ft.Icons.CLOUD_DOWNLOAD_OUTLINED,
                            size=15,
                            color=Color.ACCENT_LIGHT,
                        ),
                        ft.Text(
                            "Открываем страницу и ищем первые папки…",
                            size=12,
                            color=Color.TEXT_SECONDARY,
                        ),
                    ],
                ),
            )
        ]
        self._discovery_updates.clear()
        self._discovery_counter.value = ""
        self.state.total_parsed = 0

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

    async def _on_course_parsed(self, update: CourseDiscoveryUpdate) -> None:
        if not self.state.discovery_visible:
            self.state.discovery_visible = True
            self._show_course_discovery()
            self.page.update()
        previous = self._discovery_updates.get(update.url)
        self._discovery_updates[update.url] = update
        self.state.total_parsed = len(self._discovery_updates)
        self._discovery_list.controls = [
            self._build_discovery_card(item) for item in self._discovery_updates.values()
        ]
        loaded = sum(item.loaded for item in self._discovery_updates.values())
        self._discovery_counter.value = (
            f"Найдено папок: {self.state.total_parsed} · обработано: {loaded}"
        )
        self.page.update()
        if update.loaded and (previous is None or not previous.loaded):
            self._schedule_discovery_scroll(update.url)

    async def _scroll_discovery_to(self, course_url: str) -> None:
        try:
            await self._discovery_list.scroll_to(
                scroll_key=course_url,
                duration=650,
                curve=ft.AnimationCurve.EASE_IN_OUT_CUBIC,
            )
        except (asyncio.CancelledError, IndexError, RuntimeError):
            return

    def _schedule_discovery_scroll(self, course_url: str) -> None:
        self._pending_discovery_scroll_url = course_url
        if self._discovery_scroll_task is None or self._discovery_scroll_task.done():
            self._discovery_scroll_task = asyncio.create_task(self._drain_discovery_scrolls())

    async def _drain_discovery_scrolls(self) -> None:
        try:
            while self._pending_discovery_scroll_url is not None:
                await asyncio.sleep(_DISCOVERY_SCROLL_DELAY_SECONDS)
                course_url = self._pending_discovery_scroll_url
                self._pending_discovery_scroll_url = None
                await self._scroll_discovery_to(course_url)
        except asyncio.CancelledError:
            return
        finally:
            self._discovery_scroll_task = None

    @staticmethod
    def _build_discovery_card(update: CourseDiscoveryUpdate) -> ft.Container:
        return ft.Container(
            key=ft.ScrollKey(update.url),  # type: ignore[call-arg]
            padding=ft.Padding.symmetric(horizontal=10, vertical=6),
            border_radius=8,
            bgcolor="rgba(124,58,237,0.08)",
            content=ft.Row(
                controls=[
                    ft.Icon(
                        ft.Icons.CHECK_CIRCLE_OUTLINE
                        if update.loaded
                        else ft.Icons.SCHEDULE_ROUNDED,
                        size=14,
                        color=Color.GREEN if update.loaded else Color.ACCENT_LIGHT,
                    ),
                    ft.Text(update.title, size=12, color=Color.TEXT, expand=True),
                    ft.Container(
                        content=ft.Text(
                            f"{update.lesson_count} ур." if update.loaded else "в очереди",
                            size=11,
                            color=Color.TEXT_MUTED,
                        ),
                        padding=ft.Padding.symmetric(horizontal=6, vertical=2),
                        border_radius=4,
                        bgcolor="rgba(255,255,255,0.05)",
                    ),
                ],
                spacing=8,
            ),
        )

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
        if self._discovery_scroll_task is not None:
            self._discovery_scroll_task.cancel()
            self._discovery_scroll_task = None
        self._pending_discovery_scroll_url = None

    def _start_text_animation(self, target: ft.Text, base_text: str) -> None:
        if self._loading_task is not None:
            self._loading_task.cancel()
        self._loading_task = asyncio.create_task(self._animate_text(target, base_text))

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
            with contextlib.suppress(Exception):
                self.page.update()
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
            with contextlib.suppress(Exception):
                self.page.update()
            raise

    def _start_parse(self):
        url = self.url_input.value.strip()
        if not url or self.state.parse_running:
            return

        self.state.parse_running = True
        self.state.discovery_visible = False
        self.state.total_parsed = 0
        self.loader.visible = True
        self.state.discovery_visible = True
        self._show_course_discovery()
        self.page.update()
        self._parse_task = self.page.run_task(self._parse_async, url)

    def _on_auth_ready(self, e) -> None:
        self._auth_event.set()

    async def _wait_for_authentication(self, message: str) -> None:
        self._show_auth_card()
        self._auth_error.visible = False
        self._auth_status.value = message
        self._auth_button.disabled = False
        self.page.update()

        self._auth_event.clear()
        await self._auth_event.wait()
        if self._loading_task:
            self._loading_task.cancel()
            self._loading_task = None
        self._auth_button.disabled = True
        self._auth_status.value = "Проверяем..."
        self.page.update()

    async def _parse_async(self, url: str) -> None:
        try:
            await self._controller.discover(
                url,
                on_auth_required=self._wait_for_authentication,
                on_course_discovered=self._on_course_parsed,
            )
            self._stop_all_animations()
            self.loader.visible = False
            self._parse_task = None
            await self._on_courses_ready()

        except Exception as ex:
            self._stop_all_animations()
            self.state.parse_running = False
            self.state.discovery_visible = False
            self._parse_task = None
            self._show_error(ex)

    @staticmethod
    def _sanitize_error_details(details: str) -> str:
        text = re.sub(
            r"(?i)\b[A-Z]:\\Users\\[^\\\s]+",
            "<папка пользователя>",
            details,
        )
        text = re.sub(r"(https?://[^\s?]+)\?[^\s]+", r"\1?<скрыто>", text)
        text = " ".join(text.split())
        return text[:800] or "Дополнительные сведения отсутствуют"

    @staticmethod
    def _error_title(code: str) -> str:
        return StartScreen._error_guidance(code).title

    @staticmethod
    def _error_guidance(code: str) -> _ErrorGuidance:
        if code in {"BROWSER_RESOURCES_MISSING", "BROWSER_START_FAILED"}:
            return _ErrorGuidance(
                "Встроенный Firefox не запустился",
                "Приложению не удалось открыть встроенный браузер.",
                (
                    "Полностью распакуйте архив приложения в отдельную папку.",
                    "Закройте приложение и откройте его снова.",
                    "Если ошибка повторяется, распакуйте свежую копию архива.",
                ),
            )
        if code == "BROWSER_PROFILE_BUSY":
            return _ErrorGuidance(
                "Firefox уже используется",
                "Предыдущая загрузка ещё использует встроенный браузер.",
                (
                    "Закройте другие открытые окна GetCourse Video Downloader.",
                    "Подождите несколько секунд и нажмите «Повторить».",
                ),
            )
        if code in {"SITE_TIMEOUT", "SITE_CONNECTION_FAILED"}:
            return _ErrorGuidance(
                "Сайт недоступен",
                "Не удалось установить соединение с сайтом курса.",
                (
                    "Проверьте, открывается ли ссылка в обычном браузере.",
                    "Проверьте подключение к интернету, VPN или прокси.",
                    "Подождите немного и нажмите «Повторить».",
                ),
            )
        if code in {"HTTP_401", "HTTP_403", "AUTH_REQUIRED"}:
            return _ErrorGuidance(
                "Нет доступа к курсу",
                "У аккаунта нет доступа к этой странице.",
                (
                    "Войдите в аккаунт, на котором доступен курс.",
                    "Убедитесь, что курс открыт для этого аккаунта.",
                    "Скопируйте ссылку на курс из личного кабинета и повторите попытку.",
                ),
            )
        if code == "HTTP_404":
            return _ErrorGuidance(
                "Страница не найдена",
                "Ссылка ведёт на страницу, которой больше нет, либо содержит ошибку.",
                (
                    "Откройте ссылку в обычном браузере.",
                    "Скопируйте актуальную ссылку из личного кабинета GetCourse.",
                    "Если страница не открывается, запросите новую ссылку у автора курса.",
                ),
            )
        if code == "HTTP_5XX":
            return _ErrorGuidance(
                "Ошибка сервера",
                "Сервер курса временно не смог обработать запрос.",
                (
                    "Подождите несколько минут и нажмите «Повторить».",
                    "Проверьте, открывается ли курс в обычном браузере.",
                    "Если проблема не проходит, обратитесь к владельцу курса.",
                ),
            )
        if code == "INVALID_CONFIGURATION":
            return _ErrorGuidance(
                "Проверьте ссылку",
                "Приложению нужен полный адрес страницы курса.",
                (
                    "Вставьте ссылку целиком, включая https://.",
                    "Убедитесь, что это ссылка на страницу GetCourse, "
                    "а не текст из адресной строки.",
                ),
            )
        return _ErrorGuidance(
            "Не удалось загрузить курсы",
            "Не получилось получить список курсов по этой ссылке.",
            (
                "Проверьте, открывается ли ссылка в обычном браузере.",
                "Убедитесь, что вы вошли в аккаунт с доступом к курсу.",
                "Попробуйте повторить загрузку позже.",
            ),
        )

    @staticmethod
    def _error_card(*, width: int, height: int, content) -> ft.Container:
        return ft.Container(
            expand=True,
            alignment=ft.Alignment(0, 0),
            content=ft.Container(
                width=width,
                height=height,
                alignment=ft.Alignment(0, 0),
                padding=ft.Padding.symmetric(horizontal=28, vertical=24),
                border_radius=18,
                bgcolor=Color.BG_CARD,
                border=ft.Border.all(1, "rgba(239,68,68,0.35)"),
                gradient=Gradient.CARD,
                shadow=Shadow.CARD_ELEVATED,
                content=content,
            ),
        )

    def _show_error(self, error: Exception) -> None:
        code = str(getattr(error, "code", "INTERNAL_ERROR"))
        self._error_guidance_data = self._error_guidance(code)
        self._error_code_value = code
        message = (
            str(error).strip()
            if isinstance(error, DownloaderError)
            else "Произошла непредвиденная ошибка."
        )
        message = message or "Произошла непредвиденная ошибка."
        raw_details = str(getattr(error, "technical_details", "")).strip()
        if not raw_details:
            raw_details = f"{type(error).__name__}: {error}"

        self._error_message = ft.Text(
            message,
            size=13,
            color=Color.TEXT_SECONDARY,
            text_align=ft.TextAlign.CENTER,
        )
        self._error_code = ft.Text(
            f"Код ошибки: {code}",
            size=11,
            color=Color.RED,
            weight=ft.FontWeight.W_600,
        )
        self._error_details = ft.Text(
            self._sanitize_error_details(raw_details),
            size=11,
            color=Color.TEXT_MUTED,
            selectable=True,
        )
        self._error_details_button = ft.TextButton(
            "Что можно сделать",
            icon=ft.Icons.INFO_OUTLINE_ROUNDED,
            on_click=self._show_error_details,
        )
        self._error_retry_button = ft.Button(
            "Повторить",
            icon=ft.Icons.REFRESH_ROUNDED,
            on_click=self._retry_after_error,
            style=ft.ButtonStyle(
                color=ft.Colors.WHITE,
                bgcolor=Color.ACCENT,
                shape=ft.RoundedRectangleBorder(radius=9),
            ),
        )
        close_button = ft.OutlinedButton(
            "Закрыть",
            on_click=self._dismiss_error,
            style=ft.ButtonStyle(
                color=Color.TEXT_SECONDARY,
                side=ft.BorderSide(1, Color.BORDER),
                shape=ft.RoundedRectangleBorder(radius=9),
            ),
        )

        self._error_summary_view = self._error_card(
            width=460,
            height=300,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
                controls=[
                    ft.Container(
                        width=40,
                        height=40,
                        border_radius=12,
                        bgcolor="rgba(239,68,68,0.14)",
                        content=ft.Icon(ft.Icons.ERROR_OUTLINE, color=Color.RED, size=22),
                    ),
                    ft.Text(
                        self._error_guidance_data.title,
                        size=17,
                        weight=ft.FontWeight.W_700,
                        color=Color.TEXT,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    self._error_message,
                    self._error_code,
                    self._error_details_button,
                    ft.Row(
                        [close_button, self._error_retry_button],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=10,
                    ),
                ],
            ),
        )
        self.loader.content = self._error_summary_view
        self.loader.visible = True
        self.page.update()

    def _show_error_details(self, _event) -> None:
        back_button = ft.OutlinedButton(
            "Назад",
            icon=ft.Icons.ARROW_BACK_ROUNDED,
            on_click=self._show_error_summary,
            style=ft.ButtonStyle(
                color=Color.TEXT_SECONDARY,
                side=ft.BorderSide(1, Color.BORDER),
                shape=ft.RoundedRectangleBorder(radius=9),
            ),
        )
        steps = ft.Column(
            width=420,
            spacing=5,
            controls=[
                ft.Row(
                    [
                        ft.Icon(ft.Icons.CHECK_CIRCLE_OUTLINE, color=Color.ACCENT_LIGHT, size=16),
                        ft.Text(step, size=12, color=Color.TEXT_SECONDARY, expand=True),
                    ],
                    vertical_alignment=ft.CrossAxisAlignment.START,
                    spacing=8,
                )
                for step in self._error_guidance_data.steps
            ],
        )
        self.loader.content = self._error_card(
            width=540,
            height=380,
            content=ft.Column(
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=8,
                controls=[
                    ft.Text(
                        self._error_guidance_data.title,
                        size=18,
                        weight=ft.FontWeight.W_700,
                        color=Color.TEXT,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        f"Код ошибки: {self._error_code_value}",
                        size=11,
                        color=Color.RED,
                        weight=ft.FontWeight.W_600,
                    ),
                    ft.Text(
                        "Что это значит", size=13, weight=ft.FontWeight.W_600, color=Color.TEXT
                    ),
                    ft.Text(
                        self._error_guidance_data.summary,
                        size=12,
                        color=Color.TEXT_SECONDARY,
                        text_align=ft.TextAlign.CENTER,
                    ),
                    ft.Text(
                        "Что можно сделать", size=13, weight=ft.FontWeight.W_600, color=Color.TEXT
                    ),
                    steps,
                    ft.Text(
                        "Технические сведения",
                        size=12,
                        weight=ft.FontWeight.W_600,
                        color=Color.TEXT,
                    ),
                    ft.Container(
                        width=420,
                        padding=ft.Padding.symmetric(horizontal=12, vertical=8),
                        border_radius=8,
                        bgcolor="rgba(0,0,0,0.22)",
                        content=self._error_details,
                    ),
                    ft.Row(
                        [back_button, self._error_retry_button],
                        alignment=ft.MainAxisAlignment.CENTER,
                        spacing=10,
                    ),
                ],
            ),
        )
        self.page.update()

    def _show_error_summary(self, _event) -> None:
        self.loader.content = self._error_summary_view
        self.page.update()

    def _dismiss_error(self, _event) -> None:
        self.loader.visible = False
        self.page.update()

    def _retry_after_error(self, _event) -> None:
        self.loader.visible = False
        self.page.update()
        self._start_parse()

    def dispose(self) -> None:
        self._stop_all_animations()
        self._auth_event.set()
        if self._parse_task is not None and not self._parse_task.done():
            self._parse_task.cancel()

    def shutdown(self, _timeout: float = 6.0) -> None:
        self.dispose()
