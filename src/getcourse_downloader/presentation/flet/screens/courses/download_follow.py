from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import time
from collections.abc import Callable

import flet as ft

_FOLLOW_DURATION_SECONDS = 0.35


class DownloadListFollow:
    """Keep one smooth client-side follow motion behind the newest lesson.

    Flet starts ``scroll_to`` on the client and returns before its animation is
    complete.  This coordinator therefore waits for that animation window and
    coalesces newer lessons instead of creating a task for every worker event.
    """

    def __init__(
        self,
        page: ft.Page,
        scrollable: ft.Column,
        *,
        can_follow: Callable[[], bool],
        target_for: Callable[[str], str],
        target_index: Callable[[str], int],
        screen_id: int,
        logger: logging.Logger,
        pause_seconds: float,
    ) -> None:
        self._page = page
        self._scrollable = scrollable
        self._can_follow = can_follow
        self._target_for = target_for
        self._target_index = target_index
        self._screen_id = screen_id
        self._logger = logger
        self._pause_seconds = pause_seconds
        self._target: str | None = None
        self._last_scrolled_target: str | None = None
        self._paused_until = 0.0
        self._report_open = False
        self._motion_stopped = False
        self._changed = asyncio.Event()
        self._task: concurrent.futures.Future[None] | None = None
        self._disposed = False

    @property
    def paused(self) -> bool:
        return time.monotonic() < self._paused_until

    def follow(self, lesson_url: str) -> None:
        if self._disposed:
            return
        self._target = self._target_for(lesson_url)
        self._ensure_task()
        self._changed.set()

    def pause_for_interaction(self, reason: str) -> None:
        if self._disposed:
            return
        self._paused_until = time.monotonic() + self._pause_seconds
        self._last_scrolled_target = None
        self._logger.info(
            "download_follow_paused screen=%x reason=%s seconds=%s",
            self._screen_id,
            reason,
            self._pause_seconds,
        )
        self._ensure_task()
        self._changed.set()

    def set_report_open(self, opened: bool) -> None:
        if self._disposed or self._report_open == opened:
            return
        self._report_open = opened
        self._ensure_task()
        self._changed.set()

    def refresh(self) -> None:
        """Re-evaluate visibility/state changes without manufacturing a user pause."""
        if self._disposed:
            return
        self._last_scrolled_target = None
        self._ensure_task()
        self._changed.set()

    def reset(self) -> None:
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._task = None
        self._target = None
        self._last_scrolled_target = None
        self._paused_until = 0.0
        self._report_open = False
        self._motion_stopped = False
        self._changed.clear()

    def dispose(self) -> None:
        self._disposed = True
        self.reset()

    def _ensure_task(self) -> None:
        if self._task is not None and not self._task.done():
            return
        try:
            self._task = self._page.run_task(self._run)
        except (AttributeError, RuntimeError):
            self._task = None

    async def _wait_for_change(self, timeout: float | None = None) -> None:
        try:
            if timeout is None:
                await self._changed.wait()
            else:
                await asyncio.wait_for(self._changed.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            self._changed.clear()

    async def _stop_motion(self) -> None:
        if self._motion_stopped:
            return
        self._motion_stopped = True
        # A user drag cancels Flutter's in-flight ScrollController animation.
        # Do not issue another scroll command here: it races the gesture and,
        # unlike a target scroll, can wait for a client response while a report
        # is opening.

    async def _settle_motion(self, started_at: float) -> None:
        """Wait for a client animation but wake immediately for a real pause."""
        while True:
            remaining = _FOLLOW_DURATION_SECONDS - (time.monotonic() - started_at)
            if remaining <= 0:
                return
            await self._wait_for_change(remaining)
            if self._report_open or self.paused or self._disposed or not self._can_follow():
                return

    async def _run(self) -> None:
        try:
            while not self._disposed:
                if self._report_open or self.paused:
                    await self._stop_motion()
                    delay = max(0.0, self._paused_until - time.monotonic())
                    await self._wait_for_change(delay if self.paused else None)
                    continue
                if not self._can_follow() or self._target is None:
                    await self._wait_for_change()
                    continue

                target = self._target
                if target == self._last_scrolled_target:
                    await self._wait_for_change()
                    continue
                self._motion_stopped = False
                try:
                    await self._scrollable.scroll_to(
                        scroll_key=target,
                        duration=350,
                        curve=ft.AnimationCurve.EASE_OUT_CUBIC,
                    )
                except (asyncio.CancelledError, IndexError, RuntimeError):
                    return
                self._logger.debug(
                    "download_follow_started screen=%x target_index=%s",
                    self._screen_id,
                    self._target_index(target),
                )
                self._last_scrolled_target = target
                await self._settle_motion(time.monotonic())
                if not self._report_open and not self.paused:
                    self._logger.debug(
                        "download_follow_settled screen=%x target_index=%s",
                        self._screen_id,
                        self._target_index(target),
                    )
        except asyncio.CancelledError:
            return
        finally:
            self._task = None
