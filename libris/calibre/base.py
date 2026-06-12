"""Abstract CalibreBackend protocol."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

from ..metadata.base import MetadataResult

log = logging.getLogger(__name__)


def format_authors(authors: list[str]) -> str:
    """Join an author list with Calibre's multi-author separator (" & ").

    Never join with ", " for calibredb fields — Calibre parses "A, B" as a
    single inverted name ("Surname, Given").
    """
    return " & ".join(authors)


def notify_reconnect(url: str | None) -> None:
    """Ping calibre-web's /reconnect endpoint after a calibredb write.

    calibre-web holds metadata.db open continuously; external calibredb
    writes accumulate in the WAL beneath its (eventually stale) connection,
    which both blocks WAL checkpointing and can desync calibre-web's shm
    view into "database disk image is malformed".  Hitting /reconnect makes
    calibre-web drop and reopen its connection instead.

    Best-effort by design: any failure (endpoint disabled, calibre-web down,
    timeout) is logged and swallowed — the import itself already succeeded.
    Requires calibre-web started with the -r flag; a 404 means it isn't.
    """
    if not url:
        return
    import httpx  # noqa: PLC0415 — keep the hot import path free of httpx

    try:
        response = httpx.get(url, timeout=5.0)
        if response.status_code == 200:
            log.debug("calibre.reconnect_notified", extra={"url": url})
        elif response.status_code == 404:
            log.warning(
                "calibre.reconnect_unavailable: calibre-web returned 404 — "
                "start calibre-web with the -r flag to enable /reconnect"
            )
        else:
            log.warning(
                "calibre.reconnect_failed",
                extra={"url": url, "status": response.status_code},
            )
    except Exception as exc:
        log.warning("calibre.reconnect_error", extra={"url": url, "error": str(exc)})


class CalibreBackend(ABC):
    """Interface for interacting with a Calibre library."""

    @abstractmethod
    def add_book(
        self,
        file_path: Path,
        title: str | None = None,
        authors: str | None = None,
    ) -> int:
        """Import a file into the Calibre library.

        title/authors are passed to calibredb add as --title/--authors and
        determine the directory structure Calibre creates
        ({author_sort}/{title} ({id})/).  Without them calibredb falls back
        to parsing the FILENAME as "{title} - {author}" — it never reads
        embedded M4B audio tags — which produces wrong directories for
        files like "Book01-Merchant of Death.m4b" (author "Unknown").

        Args:
            file_path: Book file to import.
            title: Resolved title, or None to let calibredb guess.
            authors: " & "-joined author string (see format_authors), or
                None to let calibredb guess.

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
    def set_cover(self, book_id: int, cover_path: Path) -> bool:
        """Set the cover image for a Calibre book record.

        Args:
            book_id: Calibre book ID.
            cover_path: Local path to the cover image file.

        Returns:
            True if calibredb accepted the cover, False on any failure
            (e.g. PermissionError writing cover.jpg into the book dir).
            Failures are logged, never raised — but callers that report
            per-book results (get-covers) must check the return value.
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

    @abstractmethod
    def add_format(self, book_id: int, file_path: Path) -> None:
        """Add a new format file to an existing Calibre book record.

        Uses `calibredb add_format`.  Replaces the existing format if one with
        the same extension is already stored (Calibre's default behaviour).

        Raises:
            CalibreImportError: If calibredb exits with a non-zero code.
        """
        ...

    @abstractmethod
    def get_formats(self, book_id: int) -> set[str]:
        """Return the set of format extensions stored for a Calibre book.

        Extensions are lowercase without a leading dot, e.g. {"epub", "m4b"}.
        Returns an empty set on any error so callers can treat it as "unknown".
        """
        ...

    @abstractmethod
    def list_books(self) -> list[dict]:
        """Return all books in the library as a list of dicts.

        Each dict has:
            id      (int)        — Calibre book ID
            title   (str)        — book title
            authors (list[str])  — author name(s)
            formats (list[str])  — lowercase format extensions, e.g. ["epub"]

        Returns an empty list on any error.
        """
        ...
