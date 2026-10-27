"""Offline tests for appselfupdate — everything runs against file:// manifests
and fake exe payloads in a temp dir. No network, no Qt."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from appselfupdate import (
    EventHook,
    UpdateError,
    UpdateInfo,
    Updater,
    is_newer,
    version_tuple,
)
from appselfupdate._platform import spawn_env


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class VersionTests(unittest.TestCase):
    def test_version_tuple(self):
        self.assertEqual(version_tuple("1.2.3"), (1, 2, 3))
        self.assertEqual(version_tuple("v2.0"), (2, 0))
        self.assertEqual(version_tuple("1.10.0"), (1, 10, 0))

    def test_is_newer(self):
        self.assertTrue(is_newer("1.2.4", "1.2.3"))
        self.assertTrue(is_newer("2.0.0", "1.9.9"))
        self.assertFalse(is_newer("1.2.3", "1.2.3"))
        self.assertFalse(is_newer("1.2.2", "1.2.3"))
        self.assertFalse(is_newer("1.9.9", "1.10.0"))  # numeric, not lexical


class EventHookTests(unittest.TestCase):
    def test_subscribe_fire_unsubscribe(self):
        hook = EventHook()
        seen = []
        fn = lambda *a: seen.append(a)
        hook += fn
        hook.fire(1, 2)
        hook -= fn
        hook.fire(3, 4)
        self.assertEqual(seen, [(1, 2)])

    def test_broken_handler_does_not_break_others(self):
        hook = EventHook()
        seen = []
        hook += lambda *a: 1 / 0
        hook += lambda *a: seen.append(a)
        hook.fire("x")
        self.assertEqual(seen, [("x",)])


class SpawnEnvTests(unittest.TestCase):
    def test_strips_pyinstaller_onefile_vars(self):
        import os
        saved = dict(os.environ)
        try:
            os.environ["_MEIPASS2"] = r"C:\Temp\_MEI123"
            os.environ["_MEIanything"] = "x"
            os.environ["_PYI_APPLICATION_HOME_DIR"] = r"C:\Temp\_MEI123"
            os.environ["_PYI_ARCHIVE_FILE"] = r"D:\app\app.exe"
            os.environ["PATH"] = saved.get("PATH", "")
            env = spawn_env()
            self.assertNotIn("_MEIPASS2", env)
            self.assertNotIn("_MEIanything", env)
            self.assertNotIn("_PYI_APPLICATION_HOME_DIR", env)
            self.assertNotIn("_PYI_ARCHIVE_FILE", env)
            self.assertIn("PATH", env)
        finally:
            os.environ.clear()
            os.environ.update(saved)


class UpdaterFlowTests(unittest.TestCase):
    """Full check -> download -> apply -> cleanup cycle, offline."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        # the "running" app exe
        self.app = self.dir / "MyApp.exe"
        self.app.write_bytes(b"old-version-bytes")
        # the "server": a manifest + a new exe payload
        self.payload = b"new-version-bytes" * 1000
        self.new_exe = self.dir / "server" / "MyApp-2.0.0.exe"
        self.new_exe.parent.mkdir()
        self.new_exe.write_bytes(self.payload)
        self.manifest = self.dir / "server" / "latest.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _write_manifest(self, **overrides):
        data = {
            "version": "2.0.0",
            "url": self.new_exe.as_uri(),
            "sha256": _sha256(self.payload),
            "notes": "bug fixes",
        }
        data.update(overrides)
        self.manifest.write_text(json.dumps(data), encoding="utf-8")

    def _updater(self, version="1.0.0"):
        return Updater(version, self.manifest.as_uri(), app_path=self.app)

    def test_check_finds_update(self):
        self._write_manifest()
        u = self._updater()
        info = u.check()
        self.assertIsNotNone(info)
        self.assertEqual(info.version, "2.0.0")
        self.assertEqual(info.notes, "bug fixes")

    def test_check_accepts_utf8_bom_manifest(self):
        self._write_manifest()
        body = self.manifest.read_bytes()
        self.manifest.write_bytes(b"\xef\xbb\xbf" + body)
        info = self._updater().check()
        self.assertIsNotNone(info)
        self.assertEqual(info.version, "2.0.0")

    def test_check_no_update_when_current(self):
        self._write_manifest()
        u = self._updater(version="2.0.0")
        self.assertIsNone(u.check())

    def test_check_rejects_bad_sha256_field(self):
        self._write_manifest(sha256="not-hex")
        with self.assertRaises(UpdateError):
            self._updater().check()

    def test_check_min_supported_blocks(self):
        self._write_manifest(min_supported="1.5.0")
        with self.assertRaises(UpdateError):
            self._updater(version="1.0.0").check()

    def test_download_and_apply(self):
        self._write_manifest()
        u = self._updater()

        events = []
        u.checked += lambda i: events.append(("checked", i.version if i else None))
        u.downloaded += lambda p: events.append(("downloaded", p.name))
        u.applied += lambda: events.append(("applied",))

        info = u.check()

        path = u.download(info)
        self.assertTrue(path.exists())
        self.assertEqual(path, self.dir / "MyApp.exe.new")

        u.apply()
        self.assertEqual(self.app.read_bytes(), self.payload)
        self.assertTrue((self.dir / "MyApp.exe.old").exists())
        self.assertFalse((self.dir / "MyApp.exe.new").exists())
        self.assertEqual(events, [
            ("checked", "2.0.0"),
            ("downloaded", "MyApp.exe.new"),
            ("applied",),
        ])

    def test_download_progress_hook(self):
        self._write_manifest()
        u = self._updater()
        progress = []
        u.download_progress += lambda done, total: progress.append((done, total))
        u.download(u.check())
        self.assertTrue(progress)
        self.assertEqual(progress[-1][0], len(self.payload))
        self.assertEqual(progress[-1][1], len(self.payload))

    def test_download_bad_payload_checksum_cleans_up(self):
        self._write_manifest()
        self.new_exe.write_bytes(b"corrupted")  # payload no longer matches
        u = self._updater()
        errors = []
        u.error += lambda stage, exc: errors.append(stage)
        with self.assertRaises(UpdateError):
            u.download(u.check())
        self.assertEqual(errors, ["download"])
        self.assertFalse((self.dir / "MyApp.exe.new").exists())
        self.assertFalse((self.dir / "MyApp.exe.new.part").exists())
        self.assertEqual(self.app.read_bytes(), b"old-version-bytes")

    def test_apply_without_download_fails(self):
        self._write_manifest()
        u = self._updater()
        with self.assertRaises(UpdateError):
            u.apply()
        self.assertEqual(self.app.read_bytes(), b"old-version-bytes")

    def test_startup_cleanup_removes_old(self):
        (self.dir / "MyApp.exe.old").write_bytes(b"stale")
        self._updater().startup_cleanup()
        self.assertFalse((self.dir / "MyApp.exe.old").exists())

    def test_startup_cleanup_discards_stale_new(self):
        (self.dir / "MyApp.exe.new").write_bytes(b"stale-download")
        self._updater().startup_cleanup()
        self.assertFalse((self.dir / "MyApp.exe.new").exists())
        self.assertEqual(self.app.read_bytes(), b"old-version-bytes")

    def test_startup_cleanup_completes_interrupted_swap(self):
        # crash after "app -> old" but before "new -> app"
        self.app.unlink()
        (self.dir / "MyApp.exe.new").write_bytes(self.payload)
        self._updater().startup_cleanup()
        self.assertEqual(self.app.read_bytes(), self.payload)
        self.assertFalse((self.dir / "MyApp.exe.new").exists())

    def test_after_update_pid(self):
        self.assertEqual(Updater.after_update_pid(["--after-update=1234"]), 1234)
        self.assertIsNone(Updater.after_update_pid(["--other"]))
        self.assertIsNone(Updater.after_update_pid(["--after-update=abc"]))

    def test_requires_app_path_when_not_frozen(self):
        with self.assertRaises(UpdateError):
            Updater("1.0.0", "file:///x/latest.json")


class WaitForProcessTests(unittest.TestCase):
    """Cross-platform: spawn a real child and wait for it."""

    def test_waits_for_child_exit(self):
        import subprocess
        import sys
        import time

        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(0.5)"])
        start = time.monotonic()
        Updater.wait_for_process(proc.pid, timeout=10)
        elapsed = time.monotonic() - start
        self.assertEqual(proc.wait(), 0)
        self.assertLess(elapsed, 10)

    def test_dead_pid_returns_fast(self):
        import subprocess
        import sys
        import time

        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        start = time.monotonic()
        Updater.wait_for_process(proc.pid, timeout=5)
        self.assertLess(time.monotonic() - start, 5)


if __name__ == "__main__":
    unittest.main()
