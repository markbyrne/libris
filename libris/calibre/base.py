"""Abstract CalibreBackend protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class CalibreBackend(ABC):
    """Interface for interacting with a Calibre library."""

    @abstractmethod
    def add_book(self, file_path: Path) -> int:
        """Import a file into the Calibre library.

        Args:
            file_path: Absolute path to the file to import.

        Returns:
            The Calibre book ID assigned to the new entry.

        Raises:
            CalibreImportError: If calibredb exits with a non-zero code.
        """
        ...

    @abstractmethod
    def convert_ebook(self, input_path: Path, output_path: Path) -> None:
        """Convert an ebook from one format to another using ebook-convert.

        Args:
            input_path: Source file path.
            output_path: Destination file path (format inferred from extension).

        Raises:
            ConversionError: If ebook-convert fails.
        """
        ...
