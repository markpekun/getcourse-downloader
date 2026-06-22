import flet as ft


class Color:
    BG_DARK = "#0B0B1A"
    BG_MID = "#12122A"
    BG_CARD = "#1A1A38"
    BG_CARD_HOVER = "#222248"

    ACCENT = "#7C3AED"
    ACCENT_LIGHT = "#8B5CF6"
    ACCENT_DARK = "#5B21B6"
    ACCENT_GLOW = "rgba(124, 58, 237, 0.3)"

    GREEN = "#10B981"
    GREEN_GLOW = "rgba(16, 185, 129, 0.3)"
    YELLOW = "#F59E0B"
    RED = "#EF4444"

    TEXT = "#FFFFFF"
    TEXT_SECONDARY = "#9494B8"
    TEXT_MUTED = "#5C5C80"

    BORDER = "rgba(255,255,255,0.06)"
    BORDER_STRONG = "rgba(255,255,255,0.12)"


class Gradient:
    BG_PRIMARY = ft.LinearGradient(
        begin=ft.Alignment.TOP_LEFT,
        end=ft.Alignment.BOTTOM_RIGHT,
        colors=[Color.BG_DARK, Color.BG_MID],
    )
    ACCENT = ft.LinearGradient(
        begin=ft.Alignment.CENTER_LEFT,
        end=ft.Alignment.CENTER_RIGHT,
        colors=[Color.ACCENT, Color.ACCENT_DARK],
    )
    ACCENT_LIGHT = ft.LinearGradient(
        begin=ft.Alignment.CENTER_LEFT,
        end=ft.Alignment.CENTER_RIGHT,
        colors=[Color.ACCENT_LIGHT, Color.ACCENT],
    )
    GREEN = ft.LinearGradient(
        begin=ft.Alignment.CENTER_LEFT,
        end=ft.Alignment.CENTER_RIGHT,
        colors=[Color.GREEN, "#059669"],
    )
    CARD = ft.LinearGradient(
        begin=ft.Alignment.TOP_LEFT,
        end=ft.Alignment.BOTTOM_RIGHT,
        colors=[Color.BG_CARD, "#161632"],
    )
    CARD_LIGHT = ft.LinearGradient(
        begin=ft.Alignment.TOP_LEFT,
        end=ft.Alignment.BOTTOM_RIGHT,
        colors=["#1E1E42", Color.BG_CARD],
    )
    SUNSET = ft.LinearGradient(
        begin=ft.Alignment.TOP_LEFT,
        end=ft.Alignment.BOTTOM_RIGHT,
        colors=["#7C3AED", "#EC4899"],
    )


class Shadow:
    CARD = ft.BoxShadow(
        blur_radius=20,
        color="rgba(0,0,0,0.4)",
        offset=ft.Offset(0, 4),
    )
    CARD_ELEVATED = ft.BoxShadow(
        blur_radius=32,
        color="rgba(0,0,0,0.5)",
        offset=ft.Offset(0, 8),
    )
    GLOW_PRIMARY = ft.BoxShadow(
        blur_radius=30,
        color=Color.ACCENT_GLOW,
        offset=ft.Offset(0, 8),
    )
    GLOW_GREEN = ft.BoxShadow(
        blur_radius=24,
        color=Color.GREEN_GLOW,
        offset=ft.Offset(0, 6),
    )


def glass_container(
    content: ft.Control,
    width: int | None = None,
    height: int | None = None,
    expand: bool | int | None = None,
    padding: int = 20,
    border_radius: int = 16,
) -> ft.Container:
    return ft.Container(
        content=content,
        width=width,
        height=height,
        expand=expand,
        padding=padding,
        border_radius=border_radius,
        bgcolor=Color.BG_CARD,
        border=ft.Border.all(1, Color.BORDER),
        shadow=Shadow.CARD,
        gradient=Gradient.CARD,
    )


def card_container(
    content: ft.Control,
    width: int | None = None,
    padding: int = 20,
    border_radius: int = 14,
    elevated: bool = False,
) -> ft.Container:
    return ft.Container(
        content=content,
        width=width,
        padding=padding,
        border_radius=border_radius,
        bgcolor=Color.BG_CARD,
        border=ft.Border.all(1, Color.BORDER),
        shadow=Shadow.CARD_ELEVATED if elevated else Shadow.CARD,
        gradient=Gradient.CARD,
    )


def accent_button(
    text: str,
    on_click,
    icon: ft.IconData | None = None,
    expand: bool = False,
    height: int = 48,
    disabled: bool = False,
) -> ft.Container:
    content = ft.Row(
        [
            ft.Icon(icon, size=18) if icon else ft.Container(width=0),
            ft.Text(text, size=15, weight=ft.FontWeight.W_600),
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        spacing=8,
    )
    return ft.Container(
        content=content,
        height=height,
        expand=expand,
        border_radius=10,
        gradient=Gradient.ACCENT,
        shadow=Shadow.GLOW_PRIMARY if not disabled else None,
        ink=True,
        on_click=on_click,
        opacity=0.5 if disabled else 1.0,
    )


def outline_button(
    text: str,
    on_click,
    icon: ft.IconData | None = None,
    expand: bool = False,
    height: int = 48,
) -> ft.Container:
    content = ft.Row(
        [
            ft.Icon(icon, size=16, color=Color.TEXT_SECONDARY) if icon else ft.Container(width=0),
            ft.Text(text, size=13, weight=ft.FontWeight.W_500, color=Color.TEXT_SECONDARY),
        ],
        alignment=ft.MainAxisAlignment.CENTER,
        spacing=6,
    )
    return ft.Container(
        content=content,
        height=height,
        expand=expand,
        border_radius=10,
        border=ft.Border.all(1, Color.BORDER_STRONG),
        bgcolor="rgba(255,255,255,0.03)",
        ink=True,
        on_click=on_click,
    )


def section_title(text: str, size: int = 22) -> ft.Text:
    return ft.Text(
        text,
        size=size,
        weight=ft.FontWeight.W_700,
        color=Color.TEXT,
    )


def body_text(text: str, size: int = 14, color: str | None = None) -> ft.Text:
    return ft.Text(
        text,
        size=size,
        color=color or Color.TEXT_SECONDARY,
        height=1.5,
    )


def status_badge(text: str, color: str = Color.GREEN) -> ft.Container:
    return ft.Container(
        content=ft.Text(text, size=11, weight=ft.FontWeight.W_600, color=color),
        padding=ft.Padding.symmetric(horizontal=8, vertical=3),
        border_radius=6,
        bgcolor=ft.Colors.with_opacity(0.15, color),
    )


def divider() -> ft.Divider:
    return ft.Divider(height=1, color=Color.BORDER)


def page_header(title: str, subtitle: str = "") -> ft.Column:
    return ft.Column(
        spacing=4,
        controls=[
            ft.Text(title, size=32, weight=ft.FontWeight.W_700, color=Color.TEXT),
            ft.Text(subtitle, size=15, color=Color.TEXT_SECONDARY) if subtitle else ft.Container(),
        ],
    )


def build_theme() -> ft.Theme:
    return ft.Theme(
        font_family="Segoe UI",
        color_scheme=ft.ColorScheme(
            primary=Color.ACCENT,
            on_primary=Color.TEXT,
            primary_container=Color.ACCENT_DARK,
            secondary=Color.ACCENT_LIGHT,
            on_secondary=Color.TEXT,
            surface=Color.BG_CARD,
            on_surface=Color.TEXT,
            error=Color.RED,
            on_error=Color.TEXT,
        ),
        page_transitions=ft.PageTransitionsTheme(
            android=ft.PageTransitionTheme.NONE,
            ios=ft.PageTransitionTheme.NONE,
            linux=ft.PageTransitionTheme.NONE,
            macos=ft.PageTransitionTheme.NONE,
            windows=ft.PageTransitionTheme.NONE,
        ),
        scrollbar_theme=ft.ScrollbarTheme(
            thickness=4,
            thumb_visibility=False,
        ),
    )
