from typing import Protocol


class BrowserAuthorizationResetter(Protocol):
    """Clear authorization from the application's browser without touching app data."""

    async def clear(self) -> None: ...
