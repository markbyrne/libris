"""Abstract CalibreBackend protocol."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..metadata.base import MetadataResult


class CalibreBackend(ABC):
    """Interface for interacting with a Calibre library."""

    @abstractmethod
    def add_book(self, file_path: Path) -> int:
        """Import a file into the Calibre library.

        Returns:
            The Calibre book ID assigned to the new entry.

        Raises:
            CalibreImportError: If calibredb exits with a non-zero code.
        """
        ...

    @abstractmethod
    def set_metadata(self, book_id: int, result: MetadataResult) -> None:
        """Update all available metadata fields on an existing Calibre record.

        Called after add_book to push full metadata (description, publisher,
        series, language, ISBN) that calibredb add does not set automatically.

        Args:
            book_id: Calibre book ID returned by add_book.
            result: Resolved metadata to apply.
        """
        ...

    @abstractmethod
    def set_cover(self, book_id: int, cover_path: Path) -> None:
        """Set the cover image for a Calibre book record.

        Args:
            book_id: Calibre book ID.
            cover_path: Local path to the cover image file.
        """
        ...

    @abstractmethod
    def convert_ebook(self, input_path: Path, output_path: Path) -> None:
        """Convert an ebook from one format to another using ebook-convert.

        Raises:
            ConversionError: If ebook-convert fails.
        """
        ...

    @abstractmethod
    def export_book(self, book_id: int, dest_dir: Path) -> list[Path]:
        """Export a book's file(s) from the Calibre library to *dest_dir*.

        Returns a list of the exported file paths (may be more than one if
        the book has multiple formats stored).

        Raises:
            CalibreImportError: If calibredb export fails.
        """
        ...

    @abstractmethod
    def remove_book(self, book_id: int) -> None:
        """Permanently remove a book and its files from the Calibre library.

        Raises:
            CalibreImportError: If calibredb remove fails.
        """
        ...

    @abstractmethod
    def search(self, query: str) -> list[int]:
        """Search the Calibre library and return matching book IDs.

        Uses calibredb's search syntax:
          title:"=Exact Title"   — exact title match (case-insensitive)
          authors:"Surname"      — author name contains
          title:"Foo" and authors:"Bar"  — combined

        Returns an empty list on no match or any error.
        """
        ...
