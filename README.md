# appselfupdate

Self-update for single-file desktop apps (including PyInstaller onefile) on
Windows, Linux, and macOS. Requires Python 3.10 or newer.

The core is dependency-free, standard-library-only, and UI-agnostic. Optional
PySide6 integration runs blocking operations on a worker thread and reports
results through Qt signals.

> appselfupdate is not currently published on PyPI. Install it from GitHub.

## Installation

Core library:

```bash
python -m pip install "appselfupdate @ git+https://github.com/r1cardohj/appselfupdate.git"
```

With Qt support:

```bash
python -m pip install "appselfupdate[qt] @ git+https://github.com/r1cardohj/appselfupdate.git"
```

For reproducible applications, use a release tag or commit instead of the
default branch. Qt support requires PySide6 6.6 or newer.

## How it works

A running Windows executable cannot be overwritten, but it **can be renamed**.
POSIX systems allow both; rename is used everywhere to keep one protocol:

1. `check()` fetches a JSON manifest and compares versions.
2. `download()` streams the new file to `<YourApp>.new` **next to the running
   app** (the same volume, so the later rename is atomic) and verifies SHA-256.
3. `apply()` renames the running app to `.old`, then `.new` into place. If the
   second rename fails, it rolls back so the original app is restored.
4. `restart_and_exit()` starts the new app with `--after-update=<pid>` and
   exits. The new process waits for the old one to die, then
   `startup_cleanup()` deletes the leftover `.old`.

`startup_cleanup()` also repairs half-finished states such as an interrupted
swap or stale download on the next launch.

### Platform support and limitations

- **Windows and Linux:** supports bare single-file application binaries.
- **macOS:** supports bare single-file binaries, not `.app` bundles. Downloaded
  unsigned binaries remain subject to Gatekeeper and quarantine rules.
- The application needs write permission to its own directory.
- Code-signature verification, delta updates, and UI-driven rollback to `.old`
  are not implemented.

## Manifest

Host a JSON manifest on your HTTPS server, object storage, or CDN:

```json
{
  "version": "2.0.0",
  "url": "https://oss.example.com/app/YourApp-2.0.0.exe",
  "sha256": "<64 lowercase hex characters>",
  "notes": "what's new",
  "min_supported": "1.0.0",
  "force": false
}
```

`version`, `url`, and `sha256` are required. `notes`, `min_supported`, and
`force` are optional. `file://` URLs are supported for local testing.

Use HTTPS in production. SHA-256 verifies the downloaded bytes against the
manifest, but it does not replace transport security or code signing: anyone
who can replace both the manifest and payload can also replace the checksum.

## Usage

Plain Python, with no UI framework:

```python
from appselfupdate import Updater

updater = Updater(
    current_version="1.0.0",
    manifest_url="https://oss.example.com/app/latest.json",
    # app_path defaults to sys.executable when frozen with PyInstaller.
)

updater.download_progress += lambda done, total: print(f"\r{done}/{total}", end="")

info = updater.check()          # None when already up to date
if info:
    updater.download(info)      # -> Path of the .new file
    updater.apply()             # rename swap, safe while running
    updater.restart_and_exit()  # start new app, exit this process
```

`check()` and `download()` block. Run them on a worker thread in GUI apps, or
use the bundled Qt integration.

### Qt integration

`QtUpdater` reports through Qt signals delivered on the GUI thread, making it
safe to touch widgets in slots:

```python
from appselfupdate import Updater
from appselfupdate.qt import QtUpdater

updater = Updater(
    current_version="1.0.0",
    manifest_url="https://oss.example.com/app/latest.json",
)
qt = QtUpdater(updater, parent=self)

qt.checked.connect(on_update_found)          # UpdateInfo | None
qt.progress.connect(on_progress)             # (done_bytes, total_bytes)
qt.failed.connect(show_error_message)        # (stage, message)

qt.check()                                   # non-blocking
# Later, if the user accepts:
qt.download(info)                            # -> qt.downloaded(Path)
# Then on the GUI thread:
updater.apply()
qt.restart_and_quit()
```

Dialog text, buttons, and release-note presentation belong to your app; the
library provides update machinery rather than a fixed user experience.

### Startup cleanup

In the **new** process, as early as possible in `main()`:

```python
from appselfupdate import Updater

pid = Updater.after_update_pid()
if pid:
    Updater.wait_for_process(pid)   # let the old process release files/mutexes

Updater(current_version=..., manifest_url=...).startup_cleanup()
```

## Events and threading

- `checking`, `checked`, `download_progress`, `downloaded`, `applying`,
  `applied`, and `error` are available as `EventHook` callbacks.
- Handlers run synchronously on whichever thread performs the operation. Do not
  touch GUI widgets directly from a raw `EventHook` handler on a worker thread.
- Handler exceptions are swallowed so a broken UI callback cannot interrupt an
  update.
- `QtUpdater` performs one operation at a time; check `busy` before starting
  another operation.

## Development

Clone the repository and install the project with its optional Qt dependency
using [uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/r1cardohj/appselfupdate.git
cd appselfupdate
uv sync --extra qt
uv run python -m unittest discover -s tests -v
```

The core tests use local `file://` manifests and need no network. Qt tests run
with `QT_QPA_PLATFORM=offscreen` and skip automatically if PySide6 is absent.

Build and validate the source and wheel distributions without uploading them:

```bash
uv build
uvx twine check dist/*
```

## License

[MIT](LICENSE)
