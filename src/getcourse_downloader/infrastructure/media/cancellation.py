from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable


class MediaDownloadCancelled(Exception):
    """Raised when an in-flight media I/O operation is cancelled by the user."""


async def await_or_cancel[Result](
    operation: Awaitable[Result],
    cancellation_event: asyncio.Event | None,
) -> Result:
    """Await I/O or one cancellation signal without repeatedly polling either."""
    if cancellation_event is None:
        return await operation

    operation_task = asyncio.ensure_future(operation)
    cancellation_task = asyncio.create_task(cancellation_event.wait())
    try:
        completed, _ = await asyncio.wait(
            {operation_task, cancellation_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancellation_task in completed:
            operation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operation_task
            raise MediaDownloadCancelled
        return await operation_task
    finally:
        cancellation_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cancellation_task
        if not operation_task.done():
            operation_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await operation_task
