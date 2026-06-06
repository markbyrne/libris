"""Abstract Watcher protocol and FileEvent datatype."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal


@dataclass
class FileEvent:
    """A file system event indicating a new or moved-in file."""

    path: Path
    event_type: Literal["created", "moved_to"]
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def __str__(self) -> str:
        return f"FileEvent({self.event_type}, {self.path.name})"


class Watcher(ABC):
    """Abstract file system watcher.

    Implementations must yield FileEvent for each new file detected in the
    configured incoming directory. The generator blocks indefinitely until
    stop() is called or the underlying subprocess exits.

    Both implementations yield events only for direct children of incoming_dir:
    - Hidden entries (starting with '.') are always skipped
    - A file dropped directly into incoming_dir → yielded as-is
    - A directory dropped into incoming_dir → yielded; classifier inspects contents
    - Files nested inside a subdirectory of incoming_dir → skipped; the parent
      directory event handles them as an audiobook folder
    """

    @abstractmethod
    def events(self) -> Iterator[FileEvent]:
        """Blocking generator yielding FileEvent objects."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Signal the watcher to stop. Should be idempotent."""
        ...
