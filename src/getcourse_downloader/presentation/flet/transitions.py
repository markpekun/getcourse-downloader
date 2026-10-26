from __future__ import annotations

import asyncio
import concurrent.futures

import flet as ft


class RowTransitions:
    """Publish motion targets once per batch; the client renders every animation frame."""

    def __init__(self, page: ft.Page) -> None:
        self._page = page
        self._pending: dict[str, tuple[ft.Container, ...]] = {}
        self._task: concurrent.futures.Future[None] | None = None
        self._generation = 0
        self._disposed = False

    def reveal(self, key: str, controls: tuple[ft.Container, ...]) -> None:
        if self._disposed or not controls:
            return
        # A newer status replaces an unpublished target for the same row.
        previous = self._pending.get(key, ())
        details = tuple(control for control in previous if control.opacity == 0)
        self._pending[key] = (*details, *controls)
        if self._task is None or self._task.done():
            self._task = self._page.run_task(self._reveal, self._generation)

    async def _reveal(self, generation: int) -> None:
        await asyncio.sleep(0.016)
        if self._disposed or generation != self._generation:
            return
        pending, self._pending = self._pending, {}
        self._task = None
        controls = [control for row in pending.values() for control in row]
        for control in controls:
            control.opacity = 1
            control.scale = 1
            control.offset = ft.Offset(0, 0)
        if controls:
            self._page.update(*controls)

    def reset(self) -> None:
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._pending.clear()

    def dispose(self) -> None:
        self._disposed = True
        self.reset()


class OverlayTransition:
    """Animate an already mounted overlay without blocking the event loop."""

    def __init__(self, page: ft.Page, overlay: ft.Container, card: ft.Container) -> None:
        self._page, self._overlay, self._card = page, overlay, card
        self._task: concurrent.futures.Future[None] | None = None
        self._generation = 0
        self._open = False
        self._closing = False
        self._disposed = False
        overlay.opacity = 0
        card.scale = 0.985
        card.offset = ft.Offset(0, 0.008)
        card.animate_opacity = None

    def _cancel(self) -> int:
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
            self._task = None
        return self._generation

    def _duration(self, milliseconds: int) -> None:
        animation = ft.Animation(milliseconds, ft.AnimationCurve.EASE_OUT_CUBIC)
        self._overlay.animate_opacity = animation
        self._card.animate_scale = animation
        self._card.animate_offset = animation

    def show(self) -> None:
        if self._disposed:
            return
        if self._open:
            self._page.update(self._overlay)
            return
        generation = self._cancel()
        self._open = True
        self._closing = False
        self._duration(240)
        self._overlay.visible = True
        self._overlay.opacity = 0
        self._card.opacity = 1
        self._card.scale = 0.985
        self._card.offset = ft.Offset(0, 0.008)
        self._page.update(self._overlay)
        self._task = self._page.run_task(self._reveal, generation)

    async def _reveal(self, generation: int) -> None:
        # Publish the starting state before sending the animated target on the next frame.
        await asyncio.sleep(0.016)
        if self._disposed or generation != self._generation:
            return
        self._overlay.opacity = 1
        self._card.scale = 1
        self._card.offset = ft.Offset(0, 0)
        self._page.update(self._overlay)

    def close(self, *, update: bool = True) -> None:
        if update and self._closing:
            return
        generation = self._cancel()
        self._open = False
        self._overlay.opacity = 0
        self._card.scale = 0.99
        self._card.offset = ft.Offset(0, 0.005)
        if not update:
            self._overlay.visible = False
            self._closing = False
            return
        if not self._overlay.visible or self._disposed:
            return
        self._duration(180)
        self._closing = True
        self._page.update(self._overlay)
        self._task = self._page.run_task(self._hide, generation)

    async def _hide(self, generation: int) -> None:
        await asyncio.sleep(0.18)
        if self._disposed or generation != self._generation:
            return
        self._overlay.visible = False
        self._closing = False
        self._page.update(self._overlay)

    def dispose(self) -> None:
        self._disposed = True
        self._cancel()


class PreparationReveal:
    """Fade the preparation cover away after the already-mounted rows are ready."""

    def __init__(self, page: ft.Page, cover: ft.Container) -> None:
        self._page = page
        self._cover = cover
        self._task: concurrent.futures.Future[None] | None = None
        self._generation = 0
        self._disposed = False
        cover.animate_opacity = ft.Animation(200, ft.AnimationCurve.EASE_OUT_CUBIC)

    def show(self) -> None:
        if self._disposed:
            return
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._cover.visible = True
        self._cover.opacity = 1

    def reveal(self) -> None:
        if self._disposed or not self._cover.visible:
            return
        self._generation += 1
        generation = self._generation
        self._cover.opacity = 0
        self._page.update(self._cover)
        self._task = self._page.run_task(self._hide, generation)

    async def _hide(self, generation: int) -> None:
        await asyncio.sleep(0.21)
        if self._disposed or generation != self._generation:
            return
        self._cover.visible = False
        self._cover.opacity = 1
        self._page.update(self._cover)

    def dispose(self) -> None:
        self._disposed = True
        self._generation += 1
        if self._task is not None:
            self._task.cancel()
            self._task = None
