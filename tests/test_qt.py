"""Qt integration tests — skipped when PySide6 is not installed.

Runs offscreen: no windows appear. Spin a local event loop per operation
with a QTimer failsafe so a broken signal can't hang the suite.
"""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    HAS_QT = True
except ImportError:
    HAS_QT = False

from appselfupdate import Updater

if HAS_QT:
    from PySide6.QtCore import QObject
    from appselfupdate.qt import QtUpdater

    class _Relay(QObject):
        """Gives a plain callable a QObject receiver context so signal
        delivery is queued to the receiver's thread."""

        def __init__(self, fn):
            super().__init__()
            self._fn = fn

        def call(self, *args):
            self._fn(*args)

_TIMEOUT_MS = 15000


@unittest.skipUnless(HAS_QT, "PySide6 not installed")
class QtUpdaterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app_exe = self.dir / "MyApp.exe"
        self.app_exe.write_bytes(b"old-bytes")
        self.payload = b"new-bytes" * 5000
        self.new_exe = self.dir / "server" / "MyApp-2.0.0.exe"
        self.new_exe.parent.mkdir()
        self.new_exe.write_bytes(self.payload)
        (self.dir / "server" / "latest.json").write_text(json.dumps({
            "version": "2.0.0",
            "url": self.new_exe.as_uri(),
            "sha256": hashlib.sha256(self.payload).hexdigest(),
        }))
        self.manifest_url = (self.dir / "server" / "latest.json").as_uri()

    def tearDown(self):
        self.tmp.cleanup()

    def _run_until(self, qt_updater, signal):
        """Run a local event loop until `signal` fires; return its args."""
        loop = QEventLoop()
        result = []
        signal.connect(lambda *args: (result.append(args), loop.quit()))
        QTimer.singleShot(_TIMEOUT_MS, loop.quit)
        loop.exec()
        self.assertTrue(result, "signal did not fire within timeout")
        qt_updater.wait_idle()
        return result[0]

    def test_check_via_signals(self):
        qt = QtUpdater(Updater("1.0.0", self.manifest_url, app_path=self.app_exe))
        qt.check()
        (info,) = self._run_until(qt, qt.checked)
        self.assertEqual(info.version, "2.0.0")
        self.assertFalse(qt.busy)

    def test_check_no_update(self):
        qt = QtUpdater(Updater("2.0.0", self.manifest_url, app_path=self.app_exe))
        qt.check()
        (info,) = self._run_until(qt, qt.checked)
        self.assertIsNone(info)

    def test_download_with_progress(self):
        qt = QtUpdater(Updater("1.0.0", self.manifest_url, app_path=self.app_exe))
        progress = []
        qt.progress.connect(lambda d, t: progress.append((d, t)))
        info = qt.updater.check()
        qt.download(info)
        (path,) = self._run_until(qt, qt.downloaded)
        self.assertEqual(Path(path).read_bytes(), self.payload)
        self.assertTrue(progress)
        self.assertEqual(progress[-1][0], len(self.payload))

    def test_failure_reports_stage(self):
        (self.dir / "server" / "latest.json").write_text("{not json")
        qt = QtUpdater(Updater("1.0.0", self.manifest_url, app_path=self.app_exe))
        qt.check()
        (stage, message) = self._run_until(qt, qt.failed)
        self.assertEqual(stage, "check")
        self.assertTrue(message)

    def test_download_started_inside_checked_slot(self):
        """Regression: starting download from the checked slot used to raise
        'busy' because the check thread's finished cleanup was still queued."""
        qt = QtUpdater(Updater("1.0.0", self.manifest_url, app_path=self.app_exe))
        # A QObject relay gives the slot a receiver context, so it is queued
        # to the GUI thread like a real QObject slot (plain lambdas connected
        # to signals emitted from a worker run on the worker thread).
        relay = _Relay(lambda info: qt.download(info))
        qt.checked.connect(relay.call)
        qt.check()
        (path,) = self._run_until(qt, qt.downloaded)
        self.assertEqual(Path(path).read_bytes(), self.payload)

    def test_busy_guard(self):
        class FakeRunningThread:
            def isRunning(self):
                return True

            def wait(self, msecs):
                return False

        qt = QtUpdater(Updater("1.0.0", self.manifest_url, app_path=self.app_exe))
        qt._thread = FakeRunningThread()  # a genuinely still-running operation
        with self.assertRaises(RuntimeError):
            qt.check()


if __name__ == "__main__":
    unittest.main()
