from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from math import pi

import flet as ft

from getcourse_downloader.domain.models import Course, Lesson, SelectedLesson
from getcourse_downloader.presentation.flet.theme import Color, Glass


def _clickable_row(
    control: ft.Container,
    tint: str,
    callback: Callable[..., None],
    *,
    hover_surface: str | None = None,
) -> ft.Container:
    if control.padding is not None:
        control.content = ft.Container(padding=control.padding, content=control.content)
        control.padding = None
    control.ink = True
    control.ink_color = ft.Colors.with_opacity(0.1, tint)
    control.on_click = callback
    control.clip_behavior = ft.ClipBehavior.ANTI_ALIAS
    if hover_surface is not None:
        surface = control.bgcolor

        def on_hover(event) -> None:
            ft.context.disable_auto_update()
            event.control.bgcolor = (
                hover_surface if event.data is True or event.data == "true" else surface
            )
            event.control.update()

        control.on_hover = on_hover
        control.animate = ft.Animation(180, ft.AnimationCurve.EASE_OUT_CUBIC)
    return control


@dataclass(frozen=True, slots=True)
class CourseFolderRow:
    tile: ft.ExpansionTile
    icon: ft.Icon
    chevron: ft.Icon
    checkbox: ft.Checkbox
    badge: ft.Text
    lesson_urls: frozenset[str]
    lesson_checkboxes: tuple[ft.Checkbox, ...]
    lesson_badges: tuple[ft.Text, ...]

    def set_expanded(self, expanded: bool) -> None:
        self.tile.expanded = expanded
        self.chevron.rotate = pi / 2 if expanded else 0


@dataclass(frozen=True, slots=True)
class CourseTreeView:
    control: ft.Container
    lesson_checkboxes: dict[str, ft.Checkbox]
    folder_checkboxes: dict[str, ft.Checkbox]
    folder_badges: dict[str, ft.Text]
    folder_rows: dict[str, CourseFolderRow]


@dataclass(slots=True)
class DownloadLessonRow:
    control: ft.Container
    status_holder: ft.Container
    status_text: ft.Text
    details_button: ft.Container
    progress: ft.ProgressBar
    progress_text: ft.Text
    icon_switcher: ft.AnimatedSwitcher
    status_switcher: ft.AnimatedSwitcher
    details_motion: ft.Container
    active: bool = False
    _revision: int = 0

    def set_active(self, active: bool) -> bool:
        if active == self.active:
            return False
        self.active = active
        self.control.bgcolor = ft.Colors.with_opacity(
            0.12 if active else 0.03, Color.ACCENT if active else ft.Colors.WHITE
        )
        self.control.border = ft.Border.all(
            1,
            ft.Colors.with_opacity(0.24, Color.ACCENT_LIGHT) if active else ft.Colors.TRANSPARENT,
        )
        return True

    def set_status(
        self, icon: ft.IconData, text: str, color: str, *, animate: bool = True
    ) -> tuple[ft.Container, ...]:
        previous_icon = self.status_holder.content
        if (
            isinstance(previous_icon, ft.Icon)
            and previous_icon.icon == icon
            and self.status_text.value == text
            and self.status_text.color == color
        ):
            return ()
        if not animate:
            self.status_holder.content = ft.Icon(icon, size=18, color=color)
            self.status_text.value, self.status_text.color = text, color
            return ()
        self._revision += 1
        self.status_holder = _row_motion(
            ft.Icon(icon, size=18, color=color),
            key=f"icon-{self._revision}",
            width=24,
            scale=0.97,
        )
        self.status_text = _download_status_text(text, color)
        status_motion = _row_motion(
            self.status_text, key=f"status-{self._revision}", width=112, scale=0.98
        )
        self.icon_switcher.content = self.status_holder
        self.status_switcher.content = status_motion
        return self.status_holder, status_motion

    def show_details(self) -> tuple[ft.Container, ...]:
        if self.details_button.visible:
            return ()
        self.details_button.visible = True
        self.details_motion.ignore_interactions = False
        return (self.details_motion,)


def _download_status_text(text: str, color: str) -> ft.Text:
    return ft.Text(
        text,
        size=11,
        color=color,
        width=112,
        max_lines=1,
        overflow=ft.TextOverflow.ELLIPSIS,
        text_align=ft.TextAlign.RIGHT,
    )


def _row_motion(
    content: ft.Control, *, key: str | None = None, width: int, scale: float
) -> ft.Container:
    return ft.Container(
        key=key,
        content=content,
        width=width,
        height=24,
        alignment=ft.Alignment.CENTER,
        scale=scale,
        offset=ft.Offset(0, 0.06),
        animate_scale=ft.Animation(260, ft.AnimationCurve.EASE_OUT_CUBIC),
        animate_offset=ft.Animation(260, ft.AnimationCurve.EASE_OUT_CUBIC),
    )


def _status_switcher(content: ft.Control, width: int) -> ft.AnimatedSwitcher:
    return ft.AnimatedSwitcher(
        content=content,
        width=width,
        height=24,
        duration=240,
        reverse_duration=180,
        switch_in_curve=ft.AnimationCurve.EASE_OUT_CUBIC,
        switch_out_curve=ft.AnimationCurve.EASE_IN_CUBIC,
        transition=ft.AnimatedSwitcherTransition.FADE,
    )


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
    on_folder_toggle: Callable[[str, bool], None],
    on_folder_selection: Callable[[Course, bool], None],
    on_lesson_selection: Callable[[Lesson, bool], None],
) -> CourseTreeView:
    lesson_checkboxes: dict[str, ft.Checkbox] = {}
    folder_checkboxes: dict[str, ft.Checkbox] = {}
    folder_badges: dict[str, ft.Text] = {}
    folder_rows: dict[str, CourseFolderRow] = {}
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
        if depth == 0:
            kind, kind_icon, tint = "Курс", ft.Icons.LIBRARY_BOOKS_ROUNDED, accent
            surface = Glass.COURSE_SURFACE
            hover_surface = Glass.COURSE_HOVER
        elif node.children:
            kind, kind_icon, tint = "Раздел", ft.Icons.FOLDER_COPY_ROUNDED, Glass.SECTION
            surface = Glass.SECTION_SURFACE
            hover_surface = Glass.SECTION_HOVER
        else:
            kind, kind_icon, tint = "Модуль", ft.Icons.FOLDER_ROUNDED, Glass.FOLDER
            surface = Glass.MODULE_SURFACE
            hover_surface = Glass.MODULE_HOVER

        folder_checkbox = ft.Checkbox(
            value=folder_value,
            tristate=False,
            disabled=not total,
            active_color=Glass.CHECKED,
            check_color=Color.TEXT,
            width=30,
            height=30,
            scale=1.1,
            visual_density=ft.VisualDensity.COMPACT,
            shape=ft.RoundedRectangleBorder(radius=3),
            border_side=ft.BorderSide(1.4, "#98A2C1"),
            on_change=lambda event, current=node: on_folder_selection(
                current, event.control.value is True
            ),
            tooltip="Выбрать все уроки в папке",
        )
        folder_checkboxes[node.url] = folder_checkbox
        badge = ft.Text(
            folder_badge_text(selected_count, total),
            size=12,
            weight=ft.FontWeight.W_600,
            color=tint,
        )
        folder_badges[node.url] = badge

        is_expanded = node.url in expanded_urls
        show_children = is_expanded or bool(normalized_query)
        has_content = bool(node.lessons or node.children)
        chevron = ft.Icon(
            ft.Icons.CHEVRON_RIGHT_ROUNDED,
            size=21 if depth == 0 else 18,
            color=tint,
            rotate=pi / 2 if show_children else 0,
            animate_rotation=ft.Animation(280, ft.AnimationCurve.EASE_IN_OUT_CUBIC),
        )
        folder_icon = ft.Icon(
            kind_icon,
            size=23 if depth == 0 else 21,
            color=tint,
            tooltip=kind,
        )

        def on_header_hover(event) -> None:
            ft.context.disable_auto_update()
            event.control.bgcolor = (
                hover_surface if event.data is True or event.data == "true" else surface
            )
            event.control.update()

        def on_header_click(_event) -> None:
            on_folder_toggle(node.url, not bool(tile.expanded))

        header = ft.Container(
            height=56 if depth == 0 else 48,
            padding=ft.Padding.symmetric(
                horizontal=20 if depth == 0 else 12,
                vertical=8 if depth == 0 else 0,
            ),
            border_radius=12 if depth == 0 else 8,
            bgcolor=surface,
            animate=ft.Animation(180, ft.AnimationCurve.EASE_OUT_CUBIC),
            on_hover=on_header_hover,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            shadow=ft.BoxShadow(
                blur_radius=8,
                color="#40000000",
                offset=ft.Offset(0, 2),
            )
            if depth == 0
            else None,
            content=ft.Row(
                alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                controls=[
                    ft.Row(
                        spacing=12,
                        expand=True,
                        controls=[
                            folder_checkbox,
                            folder_icon,
                            ft.Text(
                                node.title,
                                size=18 if depth == 0 else 15 if node.children else 14,
                                weight=ft.FontWeight.W_700
                                if depth == 0
                                else ft.FontWeight.W_600
                                if node.children
                                else ft.FontWeight.W_500,
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
                                padding=ft.Padding.symmetric(horizontal=11, vertical=5),
                                border_radius=11,
                                bgcolor=ft.Colors.with_opacity(0.09, tint),
                            ),
                            chevron,
                        ],
                    ),
                ],
            ),
        )
        if has_content:
            _clickable_row(header, tint, on_header_click)

        body_controls: list[ft.Control] = []
        node_checkboxes: list[ft.Checkbox] = []
        node_badges: list[ft.Text] = []
        if has_content:
            for lesson in node.lessons:
                if (
                    normalized_query
                    and not visible_by_parent
                    and normalized_query not in lesson.title.casefold()
                ):
                    continue
                checkbox = ft.Checkbox(
                    value=lesson.url in selected_urls,
                    tristate=False,
                    data=lesson.url,
                    on_change=lambda event, current=lesson: on_lesson_selection(
                        current,
                        event.control.value is True,
                    ),
                    width=30,
                    height=30,
                    scale=1.1,
                    visual_density=ft.VisualDensity.COMPACT,
                    shape=ft.RoundedRectangleBorder(radius=3),
                    border_side=ft.BorderSide(1.4, "#98A2C1"),
                    active_color=Glass.CHECKED,
                    check_color=Color.TEXT,
                    fill_color={
                        ft.ControlState.DEFAULT: ft.Colors.TRANSPARENT,
                        ft.ControlState.SELECTED: Glass.CHECKED,
                    },
                    label_style=ft.TextStyle(size=13, color=Color.TEXT),
                    semantics_label=lesson.title,
                )
                lesson_checkboxes[lesson.url] = checkbox
                node_checkboxes.append(checkbox)
                lesson_badge = ft.Text(
                    "1 из 1" if lesson.url in selected_urls else "1 урок",
                    size=11,
                    color=accent,
                    weight=ft.FontWeight.W_500,
                )
                node_badges.append(lesson_badge)
                lesson_row = _clickable_row(
                    ft.Container(
                        height=48,
                        padding=ft.Padding.symmetric(horizontal=12, vertical=5),
                        border_radius=11,
                        border=ft.Border.all(1, Glass.LESSON_BORDER),
                        bgcolor=Glass.LESSON_SURFACE,
                        content=ft.Row(
                            spacing=12,
                            controls=[
                                checkbox,
                                ft.Icon(
                                    ft.Icons.SMART_DISPLAY_OUTLINED,
                                    size=20,
                                    color=Glass.FOLDER,
                                ),
                                ft.Text(
                                    lesson.title,
                                    size=14,
                                    color=Color.TEXT,
                                    expand=True,
                                    max_lines=1,
                                    overflow=ft.TextOverflow.ELLIPSIS,
                                ),
                                ft.Container(
                                    content=lesson_badge,
                                    padding=ft.Padding.symmetric(horizontal=11, vertical=5),
                                    border_radius=11,
                                    bgcolor=ft.Colors.with_opacity(0.08, accent),
                                ),
                            ],
                        ),
                    ),
                    Glass.FOLDER,
                    lambda _event, current=lesson, ref=checkbox: on_lesson_selection(
                        current, ref.value is not True
                    ),
                    hover_surface=Glass.LESSON_HOVER,
                )
                body_controls.append(lesson_row)
            for child in node.children:
                child_control = build_node(child, depth + 1, visible_by_parent)
                if child_control is not None:
                    body_controls.append(child_control)

        body: ft.Control = ft.Container(height=0)
        if body_controls:
            connector_color = ft.Colors.with_opacity(0.32, tint)
            body = ft.Container(
                margin=ft.Margin.only(
                    left=18 if depth == 0 else 24,
                    right=12,
                    top=8,
                    bottom=8 if depth == 0 or node.children else 0,
                ),
                content=ft.Column(
                    spacing=8,
                    controls=[
                        ft.Stack(
                            clip_behavior=ft.ClipBehavior.NONE,
                            controls=[
                                ft.Container(
                                    margin=ft.Margin.only(left=12),
                                    content=child,
                                ),
                                ft.Container(
                                    left=0,
                                    top=0,
                                    bottom=-8 if index < len(body_controls) - 1 else None,
                                    width=1,
                                    height=25 if index == len(body_controls) - 1 else None,
                                    bgcolor=connector_color,
                                ),
                                ft.Container(
                                    left=0,
                                    top=24,
                                    width=12,
                                    height=1,
                                    bgcolor=connector_color,
                                ),
                            ],
                        )
                        for index, child in enumerate(body_controls)
                    ],
                ),
            )

        def on_expansion_changed(event) -> None:
            ft.context.disable_auto_update()
            expanded = event.data is True or event.data == "true"
            if expanded != bool(tile.expanded):
                on_folder_toggle(node.url, expanded)

        tile = ft.ExpansionTile(
            key=f"course-folder:{node.url}",
            title=header,
            controls=[body] if body_controls else [],
            expanded=show_children,
            maintain_state=True,
            show_trailing_icon=False,
            tile_padding=0,
            min_tile_height=0,
            visual_density=ft.VisualDensity.COMPACT,
            controls_padding=0,
            expanded_alignment=ft.Alignment.TOP_LEFT,
            expanded_cross_axis_alignment=ft.CrossAxisAlignment.STRETCH,
            bgcolor=ft.Colors.TRANSPARENT,
            collapsed_bgcolor=ft.Colors.TRANSPARENT,
            shape=ft.RoundedRectangleBorder(radius=0, side=ft.BorderSide(0, ft.Colors.TRANSPARENT)),
            collapsed_shape=ft.RoundedRectangleBorder(
                radius=0, side=ft.BorderSide(0, ft.Colors.TRANSPARENT)
            ),
            animation_style=ft.AnimationStyle(
                duration=280,
                reverse_duration=240,
                curve=ft.AnimationCurve.EASE_IN_OUT_CUBIC,
                reverse_curve=ft.AnimationCurve.EASE_IN_OUT_CUBIC,
            ),
            on_change=on_expansion_changed if has_content else None,
        )
        folder_rows[node.url] = CourseFolderRow(
            tile,
            folder_icon,
            chevron,
            folder_checkbox,
            badge,
            frozenset(lesson_urls),
            tuple(node_checkboxes),
            tuple(node_badges),
        )

        if depth == 0:
            return ft.Container(
                border_radius=14,
                border=ft.Border.all(1, ft.Colors.with_opacity(0.3, accent)),
                bgcolor=Glass.COURSE_BODY,
                theme=ft.Theme(
                    hover_color=ft.Colors.TRANSPARENT,
                    highlight_color=ft.Colors.TRANSPARENT,
                    splash_color=ft.Colors.TRANSPARENT,
                    list_tile_theme=ft.ListTileTheme(min_vertical_padding=0),
                ),
                dark_theme=ft.Theme(
                    hover_color=ft.Colors.TRANSPARENT,
                    highlight_color=ft.Colors.TRANSPARENT,
                    splash_color=ft.Colors.TRANSPARENT,
                    list_tile_theme=ft.ListTileTheme(min_vertical_padding=0),
                ),
                clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
                content=tile,
            )
        return ft.Container(
            border_radius=8,
            bgcolor=Glass.SECTION_BODY if node.children else ft.Colors.TRANSPARENT,
            clip_behavior=ft.ClipBehavior.ANTI_ALIAS,
            content=tile,
        )

    root = build_node(course, 0)
    if root is None:
        root = ft.Container(visible=False)
    assert isinstance(root, ft.Container)
    return CourseTreeView(root, lesson_checkboxes, folder_checkboxes, folder_badges, folder_rows)


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
    status_text = _download_status_text("Ожидание", Color.TEXT_MUTED)
    icon_switcher = _status_switcher(status_holder, 24)
    status_switcher = _status_switcher(status_text, 112)
    details_button = ft.Container(
        visible=False,
        padding=ft.Padding.symmetric(horizontal=6, vertical=3),
        border_radius=6,
        bgcolor=ft.Colors.with_opacity(0.18, Color.ACCENT),
        ink=on_details is not None,
        on_click=(lambda _event: on_details()) if on_details is not None else None,
        content=ft.Text(
            "Подробнее",
            size=10,
            color=Color.ACCENT_LIGHT,
            weight=ft.FontWeight.W_600,
        ),
    )
    details_motion = _row_motion(details_button, width=76, scale=0.96)
    details_motion.opacity = 0
    details_motion.ignore_interactions = True
    details_motion.animate_opacity = ft.Animation(200, ft.AnimationCurve.EASE_OUT_CUBIC)
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
        height=66,
        padding=ft.Padding.symmetric(horizontal=10, vertical=8),
        border_radius=9,
        bgcolor=ft.Colors.with_opacity(0.03, ft.Colors.WHITE),
        border=ft.Border.all(1, ft.Colors.TRANSPARENT),
        animate=ft.Animation(280, ft.AnimationCurve.EASE_IN_OUT_CUBIC),
        content=ft.Column(
            spacing=4,
            controls=[
                ft.Row(
                    height=30,
                    spacing=8,
                    controls=[
                        icon_switcher,
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
                        status_switcher,
                        details_motion,
                    ],
                ),
                ft.Container(
                    height=14,
                    content=ft.Row(
                        spacing=8, controls=[ft.Container(width=24), progress, progress_text]
                    ),
                ),
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
        icon_switcher,
        status_switcher,
        details_motion,
    )
