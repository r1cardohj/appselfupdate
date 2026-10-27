"""appselfupdate — self-update for single-file apps (Windows/Linux/macOS)."""

from .errors import UpdateError
from .events import EventHook
from .manifest import UpdateInfo
from .updater import Updater
from .version import is_newer, version_tuple

__version__ = "0.1.0"

__all__ = [
    "Updater",
    "UpdateInfo",
    "UpdateError",
    "EventHook",
    "is_newer",
    "version_tuple",
    "__version__",
]
