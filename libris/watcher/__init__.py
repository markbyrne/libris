"""File system watcher factory."""

from __future__ import annotations

import platform

from ..config import WatcherConfig
from .base import FileEvent, Watcher


def get_watcher(config: WatcherConfig) -> Watcher:
    """Return the appropriate Watcher for the current platform.

    Darwin → FswatchWatcher (brew install fswatch)
    Linux  → InotifyWatcher (apt install inotify-tools)
    """
    system = platform.system()
    if system == "Darwin":
        from .fswatch_watcher import FswatchWatcher
        return FswatchWatcher(config)
    elif system == "Linux":
        from .inotify_watcher import InotifyWatcher
        return InotifyWatcher(config)
    else:
        raise RuntimeError(
            f"Unsupported platform: {system}. "
            "Only Darwin (macOS) and Linux are supported."
        )


__all__ = ["Watcher", "FileEvent", "get_watcher"]
