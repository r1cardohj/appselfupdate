"""The Updater: check / download / apply / restart for one single-file app.

Update strategy ("rename swap"): a running Windows exe cannot be overwritten,
but it CAN be renamed; POSIX systems allow both. Using rename everywhere keeps
one protocol for all platforms. So we:
  1. download the new file next to the current one as ``<name>.new``
  2. rename the running app to ``<name>.old``
  3. rename ``<name>.new`` to the original path
  4. start the new app and exit; the new process cleans up ``.old`` on startup

Any half-finished state (leftover ``.new`` / ``.old``) is repaired by
:func:`Updater.startup_cleanup`, which the app should call as early as possible.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

from . import _platform
from .errors import UpdateError
from .events import EventHook
from .manifest import UpdateInfo
from .version import is_newer, version_tuple

_CHUNK = 1 << 16  # 64 KiB

ProgressCallback = Callable[[int, int], None]  # (downloaded_bytes, total_bytes)


def _open_url(url: str, timeout: float) -> urllib.response.addinfourl:
    """Open an http(s) or file:// URL."""
    if urlparse(url).scheme == "file":
        return urllib.request.urlopen(url)  # file URLs ignore timeout
    req = urllib.request.Request(url, headers={"User-Agent": "appselfupdate/0.1"})
    return urllib.request.urlopen(req, timeout=timeout)


class Updater:
    """Check / download / apply updates for one app binary.

    Parameters
    ----------
    current_version:
        Version of the running app, e.g. ``"1.2.3"``.
    manifest_url:
        URL of a JSON manifest ``{version, url, sha256, notes?, min_supported?, force?}``.
        ``http(s)://`` and ``file://`` are supported (file is handy for tests).
    app_path:
        The binary to replace. Defaults to ``sys.executable`` when frozen
        (PyInstaller). Must be given explicitly otherwise.
    """

    def __init__(self, current_version: str, manifest_url: str,
                 app_path: Optional[os.PathLike | str] = None):
        self.current_version = current_version
        self.manifest_url = manifest_url
        if app_path is None:
            if getattr(sys, "frozen", False):
                app_path = sys.executable
            else:
                raise UpdateError(
                    "app_path is required when not running from a frozen exe")
        self.app_path = Path(app_path)

        # -- event hooks (bind Qt signals etc. here; see EventHook) --------
        #: fired before the manifest request starts
        self.checking = EventHook()
        #: fired with ``UpdateInfo | None`` after a successful check
        self.checked = EventHook()
        #: fired with ``(downloaded_bytes, total_bytes)`` while downloading
        self.download_progress = EventHook()
        #: fired with the downloaded ``Path`` after checksum verification
        self.downloaded = EventHook()
        #: fired before the file swap begins
        self.applying = EventHook()
        #: fired after the swap succeeded (app should restart now)
        self.applied = EventHook()
        #: fired with ``(stage, exception)`` on any failure;
        #: stage is one of "check" | "download" | "apply"
        self.error = EventHook()

    # -- derived paths ---------------------------------------------------

    @property
    def new_path(self) -> Path:
        return self.app_path.with_name(self.app_path.name + ".new")

    @property
    def old_path(self) -> Path:
        return self.app_path.with_name(self.app_path.name + ".old")

    # -- steps ------------------------------------------------------------

    def check(self, timeout: float = 10.0) -> Optional[UpdateInfo]:
        """Fetch the manifest; return UpdateInfo if an update applies, else None.

        Fires ``checking``, then ``checked`` on success or ``error`` on failure.
        """
        self.checking.fire()
        try:
            info = self._check(timeout)
        except Exception as e:
            self.error.fire("check", e)
            raise
        self.checked.fire(info)
        return info

    def _check(self, timeout: float) -> Optional[UpdateInfo]:
        try:
            with _open_url(self.manifest_url, timeout) as resp:
                # utf-8-sig tolerates a BOM (e.g. PowerShell Set-Content -Encoding utf8).
                data = json.loads(resp.read().decode("utf-8-sig"))
        except (OSError, ValueError) as e:
            raise UpdateError(f"could not read manifest: {e}") from e
        info = UpdateInfo.from_manifest(data)
        if not is_newer(info.version, self.current_version):
            return None
        if info.min_supported and \
                version_tuple(self.current_version) < version_tuple(info.min_supported):
            raise UpdateError(
                f"this version ({self.current_version}) is below min_supported "
                f"({info.min_supported}); a full reinstall is required")
        return info

    def download(self, info: UpdateInfo,
                 on_progress: Optional[ProgressCallback] = None) -> Path:
        """Download the new binary to ``<app>.new`` (same dir = same volume)
        and verify its SHA256. The partial file is removed on any failure.

        Progress goes to the ``download_progress`` hook; ``on_progress`` is an
        optional extra callback. Fires ``downloaded`` on success, ``error``
        on failure.
        """
        target = self.new_path
        tmp = target.with_name(target.name + ".part")
        try:
            hasher = hashlib.sha256()
            with _open_url(info.url, timeout=60) as resp, open(tmp, "wb") as f:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    f.write(chunk)
                    hasher.update(chunk)
                    done += len(chunk)
                    self.download_progress.fire(done, total)
                    if on_progress:
                        on_progress(done, total)
            digest = hasher.hexdigest()
            if digest != info.sha256:
                raise UpdateError(
                    f"sha256 mismatch: expected {info.sha256}, got {digest}")
            _platform.rename_with_retry(tmp, target)
        except Exception as e:
            for p in (tmp, target):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            self.error.fire("download", e)
            raise
        self.downloaded.fire(target)
        return target

    def apply(self) -> None:
        """Swap the running app with the downloaded ``.new`` file.

        Safe to call while the app is running (rename, not overwrite).
        Rolls back if the second rename fails, so ``app_path`` always exists.
        Fires ``applying``, then ``applied`` on success or ``error``.
        """
        new, old, app = self.new_path, self.old_path, self.app_path
        self.applying.fire()
        try:
            if not new.exists():
                raise UpdateError(f"nothing downloaded: {new} does not exist")
            old.unlink(missing_ok=True)
            _platform.rename_with_retry(app, old)
            try:
                _platform.rename_with_retry(new, app)
            except UpdateError:
                _platform.rename_with_retry(old, app)  # roll back
                raise
        except Exception as e:
            self.error.fire("apply", e)
            raise
        self.applied.fire()

    def restart_and_exit(self, extra_args: Optional[list[str]] = None) -> None:
        """Start the (new) app with ``--after-update=<pid>`` and exit immediately."""
        args = [str(self.app_path), f"--after-update={os.getpid()}"]
        if extra_args:
            args.extend(extra_args)
        subprocess.Popen(args, close_fds=True, env=_platform.spawn_env())
        os._exit(0)

    # -- startup helpers (for the NEW process) -----------------------------

    @staticmethod
    def after_update_pid(argv: Optional[list[str]] = None) -> Optional[int]:
        """Parse ``--after-update=<pid>`` from argv; None if absent."""
        for arg in (argv if argv is not None else sys.argv[1:]):
            if arg.startswith("--after-update="):
                try:
                    return int(arg.split("=", 1)[1])
                except ValueError:
                    return None
        return None

    @staticmethod
    def wait_for_process(pid: int, timeout: float = 15.0) -> None:
        """Block until the given process exits (or timeout). Used at startup so
        the old process has fully let go of files/mutexes before we continue."""
        _platform.wait_for_process(pid, timeout)

    def startup_cleanup(self) -> None:
        """Repair any half-finished update state. Call as early as possible
        in the NEW process.

        - leftover ``.old`` -> delete (update succeeded previously)
        - ``.new`` present but app binary missing -> complete the swap
        - ``.new`` present and app binary present -> stale download, delete
        """
        old, new, app = self.old_path, self.new_path, self.app_path
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            if new.exists():
                if app.exists():
                    new.unlink()
                else:
                    _platform.rename_with_retry(new, app)
        except OSError:
            pass
