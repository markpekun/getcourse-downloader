from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

from getcourse_downloader.domain.errors import ExternalServiceError


def _windows_process_started_at(pid: int) -> int | None:
    """Return the running process's creation time in Unix nanoseconds."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        *([ctypes.POINTER(wintypes.FILETIME)] * 4),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    process = kernel32.OpenProcess(0x00101000, False, pid)
    if not process:
        error = ctypes.get_last_error()  # type: ignore[attr-defined]
        if error == 87:  # ERROR_INVALID_PARAMETER: no process with this PID.
            return None
        raise ctypes.WinError(error)  # type: ignore[attr-defined]
    try:
        state = kernel32.WaitForSingleObject(process, 0)
        if state == 0:  # WAIT_OBJECT_0: the process has exited, even if a handle remains open.
            return None
        if state != 258:  # WAIT_TIMEOUT: still running.
            raise ctypes.WinError()  # type: ignore[attr-defined]
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            process,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ctypes.WinError()  # type: ignore[attr-defined]
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return (ticks - 116444736000000000) * 100
    finally:
        kernel32.CloseHandle(process)


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            return _windows_process_started_at(pid) is not None
        os.kill(pid, 0)
    except PermissionError:
        return True  # An inaccessible process must not be mistaken for a dead owner.
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _busy_error() -> ExternalServiceError:
    return ExternalServiceError(
        "Профиль браузера уже используется другой копией приложения. "
        "Закройте её и повторите загрузку.",
        code="BROWSER_PROFILE_BUSY",
    )


def _lock_descriptor(descriptor: int, *, unlock: bool = False) -> None:
    if sys.platform == "win32":
        import msvcrt

        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK if unlock else msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(descriptor, fcntl.LOCK_UN if unlock else fcntl.LOCK_EX | fcntl.LOCK_NB)


class _ProfileLease:
    """Hold an OS lock; keep PID metadata only for compatibility with older releases."""

    def __init__(self, profile: Path) -> None:
        self._path = profile / ".gcd-profile-owner"
        # Never unlink this file: all contenders must lock the same file object.
        self._lock_path = profile / ".gcd-profile-lock"
        self._descriptor: int | None = None
        self._owns_owner = False

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        try:
            descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                _lock_descriptor(descriptor)
            except OSError as error:
                os.close(descriptor)
                if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise _busy_error() from error
                raise
            except BaseException:
                os.close(descriptor)
                raise
            self._descriptor = descriptor
            self._claim_owner()
        except OSError as error:
            self.release()
            raise ExternalServiceError(
                "Не удалось получить доступ к профилю браузера. Проверьте права доступа к папке.",
                code="BROWSER_PROFILE_UNAVAILABLE",
            ) from error
        except BaseException:
            self.release()
            raise

    def _claim_owner(self) -> None:
        for _ in range(3):
            try:
                descriptor = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                try:
                    recorded = self._path.stat()
                    try:
                        owner = int(self._path.read_text(encoding="ascii").strip())
                    except (ValueError, UnicodeError):
                        owner = 0
                    if self._path.stat().st_mtime_ns != recorded.st_mtime_ns:
                        continue
                    initializing = owner == 0 and 0 <= time.time() - recorded.st_mtime < 5
                    if initializing or self._legacy_owner_is_live(owner, recorded.st_mtime_ns):
                        raise _busy_error()
                    self._path.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass
                continue
            self._owns_owner = True
            with os.fdopen(descriptor, "w", encoding="ascii") as stream:
                stream.write(str(os.getpid()))
            return
        raise _busy_error()

    @staticmethod
    def _legacy_owner_is_live(pid: int, recorded_at: int) -> bool:
        if pid <= 0:
            return False
        if os.name != "nt":
            return _process_exists(pid)
        try:
            started_at = _windows_process_started_at(pid)
        except OSError:
            return True
        # A process born after the owner file cannot have created that file.
        return started_at is not None and started_at <= recorded_at

    def release(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            if self._owns_owner:
                with contextlib.suppress(OSError, UnicodeError):
                    if self._path.read_text(encoding="ascii").strip() == str(os.getpid()):
                        self._path.unlink(missing_ok=True)
        finally:
            self._owns_owner = False
            with contextlib.suppress(OSError):
                _lock_descriptor(descriptor, unlock=True)
            os.close(descriptor)
