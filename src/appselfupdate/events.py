"""Minimal multicast callback used for all Updater events."""

from __future__ import annotations

from typing import Callable


class EventHook:
    """Minimal multicast callback, C#-event style. Bind anything — plain
    functions, lambdas, or (via a small adapter) Qt signals::

        updater.checked += lambda info: self.checked.emit(info)

    Handlers run synchronously on whatever thread fires the event, so Qt
    adapters should emit a signal rather than touching widgets directly.
    """

    def __init__(self) -> None:
        self._handlers: list[Callable] = []

    def __iadd__(self, handler: Callable) -> "EventHook":
        self._handlers.append(handler)
        return self

    def __isub__(self, handler: Callable) -> "EventHook":
        try:
            self._handlers.remove(handler)
        except ValueError:
            pass
        return self

    def fire(self, *args) -> None:
        for handler in list(self._handlers):
            try:
                handler(*args)
            except Exception:
                pass  # a broken UI callback must never break the update
