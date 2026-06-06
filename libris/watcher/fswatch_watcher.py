"""macOS file system watcher using fswatch.

Install: brew install fswatch

fswatch is spawned as a subprocess. It outputs one absolute path per line
for each file system event. We filter for files only, skip hidden files and
the 'incoming' sentinel directory, then poll for size stability before yielding.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from queue import Empty, Queue

from ..config import WatcherConfig
from ..exceptions import WatcherError
from .base import FileEvent, Watcher

log = logging.getLogger(__name__)

# How long to wait for size stability (two consecutive equal reads)
_STABILIZE_INTERVAL = 0.5
_STABILIZE_MAX_WAIT = 60.0   # give up after 60s and yield anyway


class FswatchWatcher(Watcher):
    """macOS watcher backed by fswatch subprocess."""

    def __init__(self, config: WatcherConfig) -> None:
        self._incoming_dir = config.incoming_dir
        self._latency = config.poll_interval_seconds
        self._queue: Queue[FileEvent] = Queue()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def events(self) -> Iterator[FileEvent]:
        """Start fswatch and yield FileEvent objects as files arrive."""
        self._start_process()
        log.info("watcher.fswatch.started", extra={"dir": str(self._incoming_dir)})

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
        log.info("watcher.fswatch.stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _start_process(self) -> None:
        if not self._incoming_dir.exists():
            self._incoming_dir.mkdir(parents=True, exist_ok=True)

        cmd = [
            "fswatch",
            "--recursive",
            "--event", "Created",
            "--event", "MovedTo",
            "--latency", str(self._latency),
            "--format", "%p",
            str(self._incoming_dir),
        ]

        try:
            self._proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError:
            raise WatcherError(
                "fswatch not found. Install with: brew install fswatch"
            )

        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        """Read fswatch output lines and enqueue FileEvents."""
        assert self._proc and self._proc.stdout
        for line in self._proc.stdout:
            if self._stop_event.is_set():
                break
            path = Path(line.strip())
            if not self._should_process(path):
                continue
            if path.is_dir():
                _wait_stable_dir(path)
            else:
                _wait_stable(path)
            self._queue.put(FileEvent(path=path, event_type="created"))

        if self._proc.returncode not in (None, 0, -15):  # -15 = SIGTERM
            log.error("watcher.fswatch.process_exited", extra={"rc": self._proc.returncode})

    def _should_process(self, path: Path) -> bool:
        """Filter out paths that should not be processed.

        Only direct children of incoming_dir are processed:
        - A file dropped directly into incoming_dir → process as a single file
        - A directory dropped into incoming_dir → process as a folder of parts
        - Files inside a subdirectory → skip; the parent directory handles them
        """
        if not path.exists():
            return False
        if path.name.startswith("."):
            return False
        # Only process direct children of incoming_dir.
        # This also filters out events for individual files inside a
        # subdirectory that is itself being processed as an audiobook folder.
        return path.parent == self._incoming_dir


def _wait_stable(path: Path, interval: float = _STABILIZE_INTERVAL) -> None:
    """Block until the file size stops changing (two consecutive equal reads)."""
    prev_size = -1
    waited = 0.0
    while waited < _STABILIZE_MAX_WAIT:
        try:
            curr_size = path.stat().st_size
        except OSError:
            return   # file disappeared
        if curr_size == prev_size:
            return   # stable
        prev_size = curr_size
        time.sleep(interval)
        waited += interval
    log.warning("watcher.stabilize_timeout", extra={"file": str(path)})


def _wait_stable_dir(path: Path, interval: float = 1.0) -> None:
    """Block until the total recursive byte count of a directory stops changing.

    Two consecutive equal measurements with no OSError are required before
    returning.  This handles the case where a directory is copied into
    incoming_dir file-by-file (e.g. a slow download manager writing files
    as they complete).  Directories moved in atomically (mv/rename) will
    typically be stable on the first check.
    """
    prev_size = -1
    waited = 0.0
    while waited < _STABILIZE_MAX_WAIT:
        try:
            curr_size = sum(
                f.stat().st_size
                for f in path.rglob("*")
                if f.is_file()
            )
        except OSError:
            return   # directory disappeared or permission error
        if curr_size == prev_size:
            return   # stable
        prev_size = curr_size
        time.sleep(interval)
        waited += interval
    log.warning("watcher.dir_stabilize_timeout", extra={"dir": str(path)})
