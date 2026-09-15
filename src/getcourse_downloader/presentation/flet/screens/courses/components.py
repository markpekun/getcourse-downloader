from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass

import flet as ft

from getcourse_downloader.domain.models import Course, Lesson, SelectedLesson
from getcourse_downloader.presentation.flet.theme import Color, Gradient, Shadow


@dataclass(frozen=True, slots=True)
class CourseTreeView:
    control: ft.Container
    lesson_checkboxes: dict[str, ft.Checkbox]
    folder_checkboxes: dict[str, ft.Checkbox]
    folder_badges: dict[str, ft.Text]


@dataclass(frozen=True, slots=True)
class DownloadLessonRow:
    control: ft.Container
    status_holder: ft.Container
    status_text: ft.Text
    details_button: ft.Container
    progress: ft.ProgressBar
    progress_text: ft.Text


def iter_course_lessons(
    course: Course,
    parent_path: tuple[str, ...] = (),
) -> Iterable[SelectedLesson]:
    course_path = (*parent_path, course.title)
    for lesson in course.lessons:
        yield SelectedLesson(course_path=course_path, lesson=lesson)
    for child in course.children:
        yield from iter_course_lessons(child, course_path)


def selected_course_lessons(
    courses: Iterable[Course],
    selected_urls: set[str],
) -> list[SelectedLesson]:
    """Return selected lessons once, preserving the first visible tree path."""

    selected: list[SelectedLesson] = []
    seen_urls: set[str] = set()
    for course in courses:
        for item in iter_course_lessons(course):
            lesson_url = item.lesson.url
            if lesson_url in selected_urls and lesson_url not in seen_urls:
                seen_urls.add(lesson_url)
                selected.append(item)
    return selected


def _lesson_label(count: int) -> str:
    last_two = count % 100
    last = count % 10
    if 11 <= last_two <= 14:
        word = "уроков"
    elif last == 1:
        word = "урок"
    elif 2 <= last <= 4:
        word = "урока"
    else:
        word = "уроков"
    return f"{count} {word}"


def folder_badge_text(selected: int, total: int) -> str:
    return f"{selected} из {total}" if selected else _lesson_label(total)


def _course_matches(course: Course, query: str) -> bool:
    if not query:
        return True
    if query in course.title.casefold():
        return True
    if any(query in lesson.title.casefold() for lesson in course.lessons):
        return True
    return any(_course_matches(child, query) for child in course.children)


def build_course_tree(
    course: Course,
    *,
    accent: str,
    selected_urls: set[str],
    expanded_urls: set[str],
    query: str,
    on_folder_toggle: Callable[[str], None],
    on_folder_selection: Callable[[Course, bool], None],
    on_lesson_selection: Callable[[Lesson, bool], None],
) -> CourseTreeView:
    lesson_checkboxes: dict[str, ft.Checkbox] = {}
    folder_checkboxes: dict[str, ft.Checkbox] = {}
    folder_badges: dict[str, ft.Text] = {}
    normalized_query = query.strip().casefold()

    def build_node(node: Course, depth: int, ancestor_match: bool = False) -> ft.Control | None:
        title_match = bool(normalized_query and normalized_query in node.title.casefold())
        visible_by_parent = ancestor_match or title_match
        if (
            normalized_query
            and not visible_by_parent
            and not _course_matches(node, normalized_query)
        ):
            return None

        descendants = list(iter_course_lessons(node))
        lesson_urls = {item.lesson.url for item in descendants}
        selected_count = len(lesson_urls & selected_urls)
        total = len(lesson_urls)
        folder_value = bool(total and selected_count == total)

        folder_checkbox = ft.Checkbox(
            value=folder_value,
            tristate=False,
            disabled=not total,
            active_color=accent,
            check_color=Color.TEXT,
            on_change=lambda _event, current=node, choose=selected_count < total: (
                on_folder_selection(current, choose)
            ),
            tooltip="Выбрать все уроки в папке",
        )
        folder_checkboxes[node.url] = folder_checkbox
        badge = ft.Text(
            folder_badge_text(selected_count, total),
            size=12,
            weight=ft.FontWeight.W_600,
            color=accent,
        )
        folder_badges[node.url] = badge

        is_expanded = node.url in expanded_urls
        show_children = is_expanded or bool(normalized_query)
        has_content = bool(node.lessons or node.children)
        chevron = ft.Icon(
            ft.Icons.EXPAND_MORE_ROUNDED if show_children else ft.Icons.CHEVRON_RIGHT_ROUNDED,
            size=21 if depth == 0 else 18,
            color=Color.TEXT_SECONDARY,
        )

        header = ft.Container(
            padding=ft.Padding.symmetric(
                horizontal=18 if depth == 0 else 8,
                vertical=12 if depth == 0 else 7,
            ),
            border_radius=12 if depth else 0,
            ink=has_content,
            on_click=(lambda _, url=node.url: on_folder_toggle(url)) if has_content else None,
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(
                        spacing=9,
                        expand=True,
                        controls=[
                            folder_checkbox,
                            ft.Icon(
                                ft.Icons.FOLDER_OPEN_ROUNDED
                                if show_children
                                else ft.Icons.FOLDER_ROUNDED,
                                size=21 if depth == 0 else 18,
                                color=accent if depth == 0 else Color.TEXT_SECONDARY,
                            ),
                            ft.Text(
                                node.title,
                                size=16 if depth == 0 else 14,
                                weight=ft.FontWeight.W_600,
                                color=Color.TEXT,
                                expand=True,
                                max_lines=1,
                                overflow=ft.TextOverflow.ELLIPSIS,
                            ),
                        ],
                    ),
                    ft.Row(
                        spacing=9,
                        controls=[
                            ft.Container(
                                content=badge,
                                padding=ft.Padding.symmetric(horizontal=9, vertical=4),
                                border_radius=8,
                                bgcolor=ft.Colors.with_opacity(0.12, accent),
                            ),
                            chevron,
                        ],
                    ),
                ],
            ),
        )

        body_controls: list[ft.Control] = []
        if show_children:
            for lesson in node.lessons:
                if (
                    normalized_query
                    and not visible_by_parent
                    and normalized_query not in lesson.title.casefold()
                ):
                    continue
                checkbox = ft.Checkbox(
                    label=lesson.title,
                    value=lesson.url in selected_urls,
                    tristate=False,
                    data=lesson.url,
                    on_change=lambda event, current=lesson: on_lesson_selection(
                        current,
                        event.control.value is True,
                    ),
                    active_color=accent,
                    check_color=Color.TEXT,
                    fill_color={
                        ft.ControlState.DEFAULT: ft.Colors.with_opacity(0.08, accent),
                        ft.ControlState.SELECTED: accent,
                    },
                    label_style=ft.TextStyle(size=13, color=Color.TEXT),
                    semantics_label=lesson.title,
                )
                lesson_checkboxes[lesson.url] = checkbox
                body_controls.append(
                    ft.Container(
                        padding=ft.Padding.only(left=6, right=6),
                        content=checkbox,
                    )
                )
            for child in node.children:
                child_control = build_node(child, depth + 1, visible_by_parent)
                if child_control is not None:
                    body_controls.append(child_control)

        body: ft.Control = ft.Container(height=0)
        if body_controls:
            body = ft.Container(
                margin=ft.Margin.only(left=25 if depth == 0 else 18),
                padding=ft.Padding.only(left=10, bottom=8),
                border=ft.Border.only(left=ft.BorderSide(1, ft.Colors.with_opacity(0.3, accent))),
                content=ft.Column(spacing=1, controls=body_controls),
            )

        if depth == 0:
            return ft.Container(
                border_radius=16,
                bgcolor=Color.BG_CARD,
                border=ft.Border.all(1, Color.BORDER),
                shadow=Shadow.CARD,
                gradient=Gradient.CARD,
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                content=ft.Column(spacing=0, controls=[header, body]),
            )
        return ft.Container(
            border_radius=10,
            bgcolor="rgba(255,255,255,0.02)",
            content=ft.Column(spacing=0, controls=[header, body]),
        )

    root = build_node(course, 0)
    if root is None:
        root = ft.Container(visible=False)
    assert isinstance(root, ft.Container)
    return CourseTreeView(root, lesson_checkboxes, folder_checkboxes, folder_badges)


def build_download_lesson_row(
    item: SelectedLesson,
    *,
    on_details: Callable[[], None] | None = None,
) -> DownloadLessonRow:
    status_holder = ft.Container(
        width=24,
        height=24,
        alignment=ft.Alignment(0, 0),
        content=ft.Icon(ft.Icons.SCHEDULE_ROUNDED, size=17, color=Color.TEXT_MUTED),
    )
    status_text = ft.Text(
        "Ожидание",
        size=11,
        color=Color.TEXT_MUTED,
        width=112,
        max_lines=1,
        overflow=ft.TextOverflow.ELLIPSIS,
        text_align=ft.TextAlign.RIGHT,
    )
    details_button = ft.Container(
        visible=False,
        padding=ft.Padding.symmetric(horizontal=6, vertical=3),
        border_radius=6,
        bgcolor="rgba(124,58,237,0.18)",
        ink=on_details is not None,
        on_click=(lambda _event: on_details()) if on_details is not None else None,
        content=ft.Text(
            "Подробнее",
            size=10,
            color=Color.ACCENT_LIGHT,
            weight=ft.FontWeight.W_600,
        ),
    )
    progress = ft.ProgressBar(
        value=0,
        bar_height=5,
        color=Color.ACCENT,
        bgcolor="rgba(255,255,255,0.08)",
        border_radius=3,
        visible=False,
        expand=True,
    )
    progress_text = ft.Text(
        "0%",
        size=10,
        color=Color.TEXT_MUTED,
        width=34,
        visible=False,
        text_align=ft.TextAlign.RIGHT,
    )
    control = ft.Container(
        key=ft.ScrollKey(item.lesson.url),  # type: ignore[call-arg]
        padding=ft.Padding.symmetric(horizontal=10, vertical=8),
        border_radius=9,
        bgcolor="rgba(255,255,255,0.03)",
        content=ft.Column(
            spacing=5,
            controls=[
                ft.Row(
                    spacing=8,
                    controls=[
                        status_holder,
                        ft.Column(
                            spacing=1,
                            expand=True,
                            controls=[
                                ft.Text(
                                    item.lesson.title,
                                    size=12,
                                    color=Color.TEXT,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Text(
                                    " / ".join(item.course_path),
                                    size=10,
                                    color=Color.TEXT_MUTED,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                            ],
                        ),
                        status_text,
                        details_button,
                    ],
                ),
                ft.Row(spacing=8, controls=[ft.Container(width=24), progress, progress_text]),
            ],
        ),
    )
    return DownloadLessonRow(
        control,
        status_holder,
        status_text,
        details_button,
        progress,
        progress_text,
    )
