"""Linux file system watcher using inotifywait.

Install: sudo apt install inotify-tools

Uses the close_write event (fires when a write file handle is closed) so no
size-polling stabilisation is needed — the file is always complete when we
see it. Also listens for moved_to for files dropped in via mv/rename.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path
from queue import Empty, Queue
from typing import Literal

from ..config import WatcherConfig
from ..exceptions import WatcherError
from .base import FileEvent, Watcher

log = logging.getLogger(__name__)


class InotifyWatcher(Watcher):
    """Linux watcher backed by inotifywait subprocess."""

    def __init__(self, config: WatcherConfig) -> None:
        self._incoming_dir = config.incoming_dir
        self._queue: Queue[FileEvent] = Queue()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def events(self) -> Iterator[FileEvent]:
        self._start_process()
        log.info("watcher.inotify.started", extra={"dir": str(self._incoming_dir)})

        while not self._stop_event.is_set():
            try:
                event = self._queue.get(timeout=1.0)
                yield event
            except Empty:
                continue

    def stop(self) -> None:
        self._stop_event.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        log.info("watcher.inotify.stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_process(self) -> None:
        if not self._incoming_dir.exists():
            self._incoming_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "inotifywait",
            "--monitor",
            "--recursive",
            "--event", "close_write",
            "--event", "moved_to",
            "--format", "%e|%w%f",
            str(self._incoming_dir),
        ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise WatcherError(
                "inotifywait not found. Install with: sudo apt install inotify-tools"
            ) from exc

        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            if self._stop_event.is_set():
                break
            line = line.strip()
            if "|" not in line:
                continue

            event_str, fullpath_str = line.split("|", 1)
            path = Path(fullpath_str)
            event_type = _parse_event_type(event_str)

            if not self._should_process(path, event_str):
                continue

            self._queue.put(FileEvent(path=path, event_type=event_type))

        if self._proc.returncode not in (None, 0, -15):
            log.error("watcher.inotify.process_exited", extra={"rc": self._proc.returncode})

    def _should_process(self, path: Path, event_str: str = "") -> bool:
        """Filter out paths that should not be processed.

        Only direct children of incoming_dir are processed.  inotify fires
        ISDIR in the event flags (not the path) for directory events — we
        allow those for direct children so folders dropped into incoming are
        handled the same as individual files.
        """
        if path.name.startswith("."):
            return False
        # Only process direct children of incoming_dir.
        # Files inside a subdirectory are skipped here — they are handled
        # when the pipeline processes the parent directory as a whole.
        return path.parent == self._incoming_dir


def _parse_event_type(event_str: str) -> Literal["created", "moved_to"]:
    if "MOVED_TO" in event_str.upper():
        return "moved_to"
    return "created"
