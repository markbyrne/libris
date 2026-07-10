"""In-process ring-buffer log capture for the web dashboard's Logs view.

The web process may not have permission to read systemd's journal, and
libris normally just logs to stdout — so instead of depending on journald
or a log file path, we capture records in-process via a small
`logging.Handler` backed by a bounded deque.

ponytail: the buffer is in-memory and per-process — records are lost on
restart, and only the most recent `maxlen` records are kept. If that
ceiling becomes a problem, back it with a persisted store (e.g. a SQLite
table) instead of a deque.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone

_MAXLEN = 1000

_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


@dataclass
class LogEntry:
    timestamp: str
    level: str
    logger: str
    message: str


class RingBufferHandler(logging.Handler):
    """A logging.Handler that keeps the last `maxlen` formatted records."""

    def __init__(self, maxlen: int = _MAXLEN) -> None:
        super().__init__()
        self._records: deque[LogEntry] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = LogEntry(
                timestamp=datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                level=record.levelname,
                logger=record.name,
                message=self.format(record),
            )
        except Exception:
            self.handleError(record)
            return
        with self._lock:
            self._records.append(entry)

    def clear(self) -> None:
        with self._lock:
            self._records.clear()

    def get_records(
        self,
        level: str | None = None,
        limit: int | None = None,
        search: str | None = None,
    ) -> list[LogEntry]:
        """Return records newest-first, optionally filtered.

        `level` filters to records at-or-above that severity (e.g. "WARNING"
        also returns ERROR/CRITICAL). `search` matches (case-insensitively)
        against the logger name or message.
        """
        with self._lock:
            records = list(self._records)
        records.reverse()
        if level:
            threshold = _LEVELS.get(level.upper(), 0)
            records = [r for r in records if _LEVELS.get(r.level, 0) >= threshold]
        if search:
            needle = search.lower()
            records = [
                r for r in records
                if needle in r.message.lower() or needle in r.logger.lower()
            ]
        if limit is not None:
            records = records[:limit]
        return records


# Module-level singleton shared by the web app and its routes.
_handler = RingBufferHandler()
_handler.setFormatter(logging.Formatter("%(message)s"))
_installed = False
_install_lock = threading.Lock()


def install(log_level_name: str = "INFO") -> None:
    """Attach the shared ring-buffer handler to the "libris" logger tree.

    Safe to call more than once (e.g. if the app factory runs again under
    --reload) — only installs the handler the first time per process.
    """
    global _installed
    with _install_lock:
        if _installed:
            return
        logger = logging.getLogger("libris")
        configured = _LEVELS.get((log_level_name or "INFO").upper(), logging.INFO)
        # Always capture INFO+ even if the app is configured quieter
        # (e.g. WARNING), but go more verbose if configured that way (DEBUG).
        logger.setLevel(min(configured, logging.INFO))
        _handler.setLevel(logging.INFO)
        logger.addHandler(_handler)
        _installed = True


def get_records(
    level: str | None = None,
    limit: int | None = None,
    search: str | None = None,
) -> list[LogEntry]:
    return _handler.get_records(level=level, limit=limit, search=search)


def clear() -> None:
    """Drop all buffered records. Mainly useful for test isolation."""
    _handler.clear()
