"""Abstract CalibreBackend protocol."""

from __future__ import annotations

import logging
import re
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


def _normalize_author_key(name: str) -> str:
    """Case/whitespace-insensitive key for comparing author name spellings.

    "D. J. MacHale" and "D.J. MacHale" collapse to the same key so the
    author-merge endpoint can treat them as the same person even though
    Calibre stores them as distinct literal strings.
    """
    return re.sub(r"\s+", "", name).strip().lower()


def _replace_author_tokens(
    authors: list[str], from_names: list[str], to_name: str
) -> list[str] | None:
    """Replace any author token matching (case/space-insensitively) a
    from_name with to_name, preserving co-authors and de-duplicating.

    Returns the new author list, or None if nothing would change (the book
    has no from_name token — makes the merge idempotent: re-running it is a
    no-op for books already renamed).
    """
    from_keys = {_normalize_author_key(n) for n in from_names}
    replaced = [
        to_name if (_normalize_author_key(a) in from_keys and a != to_name) else a
        for a in authors
    ]

    # De-dupe while preserving order — e.g. "A & D. J. MacHale & D.J. MacHale"
    # (already partially merged) collapses to "A & D.J. MacHale".
    deduped: list[str] = []
    seen: set[str] = set()
    for a in replaced:
        key = _normalize_author_key(a)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(a)

    if deduped == authors:
        return None
    return deduped


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

    @abstractmethod
    def set_authors(self, book_id: int, authors: list[str]) -> bool:
        """Overwrite the authors field of an existing Calibre record.

        Unlike set_metadata (which pushes a full MetadataResult), this only
        touches the authors field — used by merge_authors() to rewrite a
        book's author list in place without disturbing title/series/etc.

        Args:
            book_id: Calibre book ID.
            authors: Full replacement author list, in order.

        Returns:
            True if calibredb accepted the change, False on any failure.
            Failures are logged, never raised.
        """
        ...

    def merge_authors(self, from_names: list[str], to_name: str) -> int:
        """Rename every book authored by any name in from_names to to_name.

        Concrete (non-abstract) so LocalCalibre and DockerCalibre share one
        implementation built on the abstract search/list_books/set_authors
        primitives — no per-backend duplication needed.

        For each from_name, uses search() (authors:"<name>" — a contains
        match, the same style _find_calibre_duplicates uses) to collect
        candidate book ids, then list_books() to read each candidate's
        current author list and _replace_author_tokens() to compute the
        rewritten list, preserving co-authors and de-duplicating.

        Best-effort per book: a search or set_authors failure is logged and
        that book is skipped, never raised. Idempotent — a book already
        bearing only to_name has no from_name token left, so a second call
        is a no-op for it (and, once every match is renamed, for the whole
        library).

        Returns the number of books actually renamed.
        """
        from_names = [n.strip() for n in from_names if n and n.strip()]
        to_name = (to_name or "").strip()
        if not to_name or not from_names:
            return 0

        candidate_ids: set[int] = set()
        for name in from_names:
            safe = name.replace('"', '\\"')
            try:
                candidate_ids.update(self.search(f'authors:"{safe}"'))
            except Exception as exc:
                log.warning(
                    "calibre.merge_authors_search_failed",
                    extra={"author_name": name, "error": str(exc)},
                )
        if not candidate_ids:
            return 0

        try:
            authors_by_id = {b["id"]: b["authors"] for b in self.list_books()}
        except Exception as exc:
            log.warning("calibre.merge_authors_list_failed: %s", exc)
            return 0

        renamed = 0
        for book_id in candidate_ids:
            authors = authors_by_id.get(book_id)
            if not authors:
                continue
            new_authors = _replace_author_tokens(authors, from_names, to_name)
            if new_authors is None:
                continue  # already correct — idempotent no-op
            try:
                ok = self.set_authors(book_id, new_authors)
            except Exception as exc:
                log.warning(
                    "calibre.merge_authors_set_failed",
                    extra={"book_id": book_id, "error": str(exc)},
                )
                continue
            if ok:
                renamed += 1
            else:
                log.warning(
                    "calibre.merge_authors_set_not_applied",
                    extra={"book_id": book_id},
                )
        return renamed
