from __future__ import annotations

import asyncio
from pathlib import Path

from getcourse_downloader.domain.errors import DownloadConfigurationError, ExternalServiceError
from getcourse_downloader.infrastructure.browser.profile_lease import _ProfileLease
from getcourse_downloader.infrastructure.platform.paths import AppPaths


class BrowserProfileResetter:
    """Reset the dedicated Firefox profile while holding its existing ownership lock."""

    def __init__(self, paths: AppPaths) -> None:
        self._paths = paths

    async def clear(self) -> None:
        await asyncio.to_thread(self._clear)

    def _clear(self) -> None:
        profile = self._paths.session
        root = profile.resolve()
        data = self._paths.data.resolve()
        if (
            profile.is_symlink()
            or profile.is_junction()
            or root == data
            or root == self._paths.resources.resolve()
            or root.parent != data.parent
        ):
            raise DownloadConfigurationError("Небезопасный путь к профилю встроенного браузера")

        lease = _ProfileLease(root)
        try:
            root.mkdir(parents=True, exist_ok=True)
            lease.acquire()
            for entry in root.iterdir():
                if entry.name not in {".gcd-profile-owner", ".gcd-profile-lock"}:
                    self._remove_entry(entry, root)
        except OSError as error:
            raise ExternalServiceError(
                "Не удалось очистить авторизацию. Закройте встроенный Firefox и повторите попытку. "
                "Если ошибка остаётся, проверьте права доступа к папке приложения.",
                code="BROWSER_PROFILE_RESET_FAILED",
            ) from error
        finally:
            lease.release()

    @classmethod
    def _remove_entry(cls, entry: Path, root: Path) -> None:
        # Delete links themselves; never traverse a symlink or a Windows junction.
        if entry.is_symlink():
            entry.unlink()
            return
        if entry.is_junction():
            entry.rmdir()
            return
        if not entry.resolve().is_relative_to(root):
            raise DownloadConfigurationError("Файл профиля находится за пределами папки браузера")
        if entry.is_dir():
            for child in entry.iterdir():
                cls._remove_entry(child, root)
            entry.rmdir()
        else:
            entry.unlink()
