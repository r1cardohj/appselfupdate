"""Optional PySide6 integration: run Updater operations on worker threads.

Requires PySide6 — install with ``pip install appselfupdate[qt]``.
The core library stays dependency-free; only importing this module needs Qt.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Optional

try:
    from PySide6.QtCore import (
        QCoreApplication,
        QMetaObject,
        QObject,
        Qt,
        QThread,
        Signal,
        Slot,
    )
except ImportError as e:
    raise ImportError(
        "appselfupdate.qt requires PySide6 — "
        "install it with: pip install appselfupdate[qt]"
    ) from e

from .manifest import UpdateInfo
from .updater import Updater
from ._platform import spawn_env

log = logging.getLogger("appselfupdate.qt")


class _Worker(QObject):
    """Lives on the worker thread; calls the blocking Updater methods."""

    checked = Signal(object)       # UpdateInfo | None
    downloaded = Signal(object)    # Path of the .new file
    progress = Signal(int, int)    # done_bytes, total_bytes (total may be 0)
    failed = Signal(str, str)      # stage ("check" | "download"), message

    def __init__(self, updater: Updater):
        super().__init__()
        self._updater = updater
        self._job = None
        updater.download_progress += lambda d, t: self.progress.emit(d, t)

    @Slot()
    def run(self) -> None:
        if self._job is not None:
            self._job()

    def do_check(self, timeout: float) -> None:
        try:
            self.checked.emit(self._updater.check(timeout=timeout))
        except Exception as e:
            log.exception("update check failed")
            self.failed.emit("check", str(e))

    def do_download(self, info: UpdateInfo) -> None:
        try:
            self.downloaded.emit(self._updater.download(info))
        except Exception as e:
            log.exception("update download failed")
            self.failed.emit("download", str(e))


class QtUpdater(QObject):
    """Runs :meth:`Updater.check` / :meth:`Updater.download` on a QThread
    and reports back through Qt signals (delivered on the GUI thread).

    Signals
    -------
    checked(object)
        ``UpdateInfo`` if an update applies, ``None`` when up to date.
    downloaded(object)
        ``pathlib.Path`` of the downloaded ``.new`` file; call
        ``updater.apply()`` (cheap, GUI thread is fine) then
        ``updater.restart_and_exit()``.
    progress(int, int)
        Bytes downloaded / total (total is 0 when the server sends no
        Content-Length).
    failed(str, str)
        ``(stage, message)`` where stage is ``"check"`` or ``"download"``.

    One operation at a time; check :attr:`busy` before starting another.
    """

    checked = Signal(object)
    downloaded = Signal(object)
    progress = Signal(int, int)
    failed = Signal(str, str)

    def __init__(self, updater: Updater, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.updater = updater
        self._thread: Optional[QThread] = None
        self._worker: Optional[_Worker] = None
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.wait_idle)

    def restart_and_quit(self, extra_args: Optional[list[str]] = None) -> None:
        """Spawn the updated exe with ``--after-update=<pid>`` and ask the
        QApplication to quit normally.

        Prefer this over :meth:`Updater.restart_and_exit` in Qt apps:
        ``os._exit`` would kill the process with other threads mid-call into
        Windows APIs (access violation), while ``QCoreApplication.quit()``
        lets ``aboutToQuit`` handlers stop them cleanly.
        """
        args = [str(self.updater.app_path),
                f"--after-update={os.getpid()}"]
        if extra_args:
            args.extend(extra_args)
        subprocess.Popen(args, close_fds=True, env=spawn_env())
        QCoreApplication.quit()

    @property
    def busy(self) -> bool:
        return self._thread is not None

    def check(self, timeout: float = 10.0) -> None:
        """Fetch the manifest on a worker thread; result via ``checked``."""
        self._start("do_check", timeout)

    def download(self, info: UpdateInfo) -> None:
        """Download + verify on a worker thread; result via ``downloaded``."""
        self._start("do_download", info)

    def wait_idle(self, msecs: int = 3000) -> None:
        """Block until the current operation's thread has stopped."""
        thread = self._thread
        if thread is not None:
            thread.quit()
            thread.wait(msecs)
            self._reap(thread)

    # -- internals ----------------------------------------------------------

    def _collect_or_raise(self) -> None:
        """Reap the previous operation's thread before starting a new one.

        The ``finished``-signal cleanup is queued to the GUI thread and can be
        delayed arbitrarily (e.g. while a modal dialog is open), so don't rely
        on it. When a result signal was just emitted the worker thread is
        microseconds from exiting — a brief wait collects it. A thread that
        is still genuinely running means a real busy violation.
        """
        thread = self._thread
        if thread is None:
            return
        if thread.isRunning() and not thread.wait(2000):
            raise RuntimeError(
                "QtUpdater is busy — wait for the current operation to finish")
        self._reap(thread)

    def _reap(self, thread: QThread) -> None:
        """Release the given thread if it is the current one.

        The thread object itself stays parented to this QtUpdater and is
        destroyed with it: deleting it here could race a still-queued
        ``finished`` signal emitted by that same thread object.
        """
        if thread is None or thread is not self._thread:
            return  # stale `finished` from an already-reaped thread
        self._thread = None
        self._worker = None

    def _on_thread_finished(self) -> None:
        self._reap(self.sender())

    def _start(self, method: str, *args) -> None:
        self._collect_or_raise()
        thread = QThread(self)
        worker = _Worker(self.updater)
        worker._job = lambda: getattr(worker, method)(*args)
        worker.moveToThread(thread)
        # Forward worker signals to our own (queued to the GUI thread).
        worker.checked.connect(self.checked)
        worker.downloaded.connect(self.downloaded)
        worker.progress.connect(self.progress)
        worker.failed.connect(self.failed)
        # Any terminal signal idles the thread. DirectConnection: quit() is
        # thread-safe and must run NOW — a queued connection would stall
        # whenever the GUI thread is inside a modal dialog.
        for signal in (worker.checked, worker.downloaded, worker.failed):
            signal.connect(thread.quit, Qt.ConnectionType.DirectConnection)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        self._thread = thread
        self._worker = worker
        thread.start()
        # Queue the job so it runs INSIDE the thread's event loop. (started
        # is emitted before exec() begins, so a job invoked directly from it
        # would swallow the quit() above — quit before exec is a no-op.)
        QMetaObject.invokeMethod(worker, "run", Qt.ConnectionType.QueuedConnection)
