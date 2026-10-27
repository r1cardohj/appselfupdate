"""Platform-specific helpers: file swap retries, child-process spawning,
and waiting for the old process to exit."""

from __future__ import annotations

import ctypes
import os
import sys
import time
from pathlib import Path
from typing import Optional

from .errors import UpdateError

_RENAME_RETRIES = 10
_RENAME_DELAY = 0.5


def rename_with_retry(src: Path, dst: Path) -> None:
    """Rename with retries — antivirus/Defender may briefly lock fresh files."""
    last: Optional[OSError] = None
    for _ in range(_RENAME_RETRIES):
        try:
            os.replace(src, dst)
            return
        except OSError as e:
            last = e
            time.sleep(_RENAME_DELAY)
    raise UpdateError(f"could not rename {src} -> {dst}: {last}")


def spawn_env() -> dict:
    """Environment for the replacement process.

    Strips PyInstaller onefile variables (``_PYI_APPLICATION_HOME_DIR``,
    ``_PYI_ARCHIVE_FILE``, …; ``_MEIPASS2`` on older PyInstaller): inherited,
    they would make the child reuse THIS process's extraction dir — which the
    exiting process is busy deleting, so the child crashes on import.
    """
    return {k: v for k, v in os.environ.items()
            if not k.startswith(("_MEI", "_PYI_"))}


def wait_for_process(pid: int, timeout: float = 15.0) -> None:
    """Block until the given process exits (or timeout). Used at startup so
    the old process has fully let go of files/mutexes before we continue."""
    if sys.platform == "win32":
        _wait_for_process_win32(pid, timeout)
    else:
        _wait_for_process_posix(pid, timeout)


def _wait_for_process_win32(pid: int, timeout: float) -> None:
    SYNCHRONIZE = 0x00100000
    handle = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return  # already gone
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if ctypes.windll.kernel32.WaitForSingleObject(handle, 100) == 0:
                return
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _wait_for_process_posix(pid: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            waited_pid, _ = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return  # already gone
            except PermissionError:
                pass  # alive but owned by another user — keep waiting
        else:
            if waited_pid == pid:
                return  # child exited and has now been reaped
        time.sleep(0.1)
