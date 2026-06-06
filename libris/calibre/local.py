"""Local calibredb backend — runs calibredb and ebook-convert directly."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from ..config import CalibreConfig
from ..exceptions import CalibreImportError, ConversionError
from ..metadata.base import MetadataResult
from .base import CalibreBackend

log = logging.getLogger(__name__)

_BOOK_EXTENSIONS = {".epub", ".m4b", ".mp3", ".mobi", ".azw3", ".pdf", ".cbz", ".cbr", ".djvu"}


class LocalCalibre(CalibreBackend):
    """Calls calibredb and ebook-convert as local subprocesses."""

    def __init__(self, config: CalibreConfig) -> None:
        self._config = config
        if config.library_path is None:
            raise ValueError("LocalCalibre requires calibre.library_path to be set")
        self._library = config.library_path

    def add_book(self, file_path: Path) -> int:
        # Do NOT pass --automerge ignore.  When calibredb's automerge detects
        # a "similar" book and ignores the add, it returns rc=0 with no
        # "Added book ids: N" in stdout.  The fallback integer-extraction in
        # _parse_book_id then picks up stray numbers from DeDRM plugin output
        # (e.g. "0.2 seconds" → 2) and returns the wrong book ID, causing
        # set_metadata/set_cover to corrupt an unrelated existing book.
        # Libris handles duplicate detection itself via _handle_duplicate before
        # calling add_book, so calibredb should always add a fresh record here.
        cmd = [
            "calibredb", "add",
            str(file_path),
            "--with-library", str(self._library),
        ]
        log.debug("calibre.local.add", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log.error(
                "calibre.local.add_failed",
                extra={"stderr": result.stderr, "stdout": result.stdout},
            )
            raise CalibreImportError(
                f"calibredb add failed (rc={result.returncode}): {result.stderr.strip()}"
            )

        # Defensive: if calibredb still declined to add (e.g. plugin or library
        # issue), the stderr will say "were not added".  Treat as a hard error.
        if "were not added" in result.stderr or "was not added" in result.stderr:
            raise CalibreImportError(
                f"calibredb refused to add {file_path.name}: {result.stderr.strip()}"
            )

        log.debug("calibre.local.add_stdout", extra={"stdout": result.stdout.strip()})
        book_id = _parse_book_id(result.stdout)
        if book_id < 0:
            raise CalibreImportError(
                f"calibredb add returned no book ID for {file_path.name}. "
                f"stdout: {result.stdout.strip()!r}"
            )
        log.info("calibre.local.added", extra={"file": str(file_path), "book_id": book_id})
        return book_id

    def set_metadata(self, book_id: int, result: MetadataResult) -> None:
        if book_id < 0:
            log.warning("calibre.local.set_metadata_skipped", extra={"reason": "unknown book_id"})
            return
        cmd = ["calibredb", "set_metadata", str(book_id), "--with-library", str(self._library)]
        for flag in _metadata_flags(result):
            cmd += flag
        log.debug("calibre.local.set_metadata", extra={"cmd": cmd})
        result_proc = subprocess.run(cmd, capture_output=True, text=True)
        if result_proc.returncode != 0:
            log.warning("calibre.local.set_metadata_failed", extra={"stderr": result_proc.stderr})

    def set_cover(self, book_id: int, cover_path: Path) -> None:
        if book_id < 0 or not cover_path.exists():
            return
        # --field cover:/abs/path is the correct way to set a cover via
        # calibredb.  The OPF approach (calibredb set_metadata BOOK_ID opf)
        # reads only the OPF metadata fields and stores the OPF XML as text,
        # not the referenced image bytes.
        cmd = [
            "calibredb", "set_metadata", str(book_id),
            "--field", f"cover:{cover_path.resolve()}",
            "--with-library", str(self._library),
        ]
        log.debug("calibre.local.set_cover", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("calibre.local.set_cover_failed: %s", result.stderr.strip())
        else:
            log.info("calibre.local.cover_set", extra={"book_id": book_id})

    def export_book(self, book_id: int, dest_dir: Path) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)
        cmd = [
            "calibredb", "export",
            "--to-dir", str(dest_dir),
            "--dont-save-cover",
            "--dont-write-opf",
            "--template", "{title}",
            str(book_id),
            "--with-library", str(self._library),
        ]
        log.debug("calibre.local.export", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CalibreImportError(
                f"calibredb export failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        exported = [f for f in dest_dir.rglob("*") if f.suffix.lower() in _BOOK_EXTENSIONS]
        log.info("calibre.local.exported", extra={"book_id": book_id, "files": [str(f) for f in exported]})
        return exported

    def remove_book(self, book_id: int) -> None:
        cmd = [
            "calibredb", "remove",
            str(book_id),
            "--with-library", str(self._library),
        ]
        log.debug("calibre.local.remove", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CalibreImportError(
                f"calibredb remove failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        log.info("calibre.local.removed", extra={"book_id": book_id})

    def search(self, query: str) -> list[int]:
        cmd = [
            "calibredb", "search", query,
            "--with-library", str(self._library),
        ]
        log.debug("calibre.local.search", extra={"query": query})
        result = subprocess.run(cmd, capture_output=True, text=True)
        # rc=1 with empty stdout = no matches (not an error)
        raw = result.stdout.strip()
        if not raw:
            return []
        try:
            return [int(x.strip()) for x in raw.split(",") if x.strip().isdigit()]
        except ValueError:
            log.warning("calibre.local.search_parse_failed", extra={"raw": raw})
            return []

    def add_format(self, book_id: int, file_path: Path) -> None:
        cmd = [
            "calibredb", "add_format",
            str(book_id),
            str(file_path),
            "--with-library", str(self._library),
        ]
        log.debug("calibre.local.add_format", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CalibreImportError(
                f"calibredb add_format failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        log.info("calibre.local.format_added", extra={"book_id": book_id, "file": str(file_path)})

    def get_formats(self, book_id: int) -> set[str]:
        cmd = [
            "calibredb", "list",
            "--search", f"id:{book_id}",
            "--fields", "formats",
            "--with-library", str(self._library),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return _parse_formats(result.stdout)

    def list_books(self) -> list[dict]:
        import json as _json
        cmd = [
            "calibredb", "list",
            "--for-machine",
            "--fields", "id,title,authors,formats",
            "--with-library", str(self._library),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("calibre.local.list_books_failed", extra={"stderr": result.stderr})
            return []
        try:
            raw = _json.loads(result.stdout)
        except _json.JSONDecodeError:
            log.warning("calibre.local.list_books_parse_error", extra={"stdout": result.stdout[:200]})
            return []
        return [_normalise_book_entry(b) for b in raw]

    def convert_ebook(self, input_path: Path, output_path: Path) -> None:
        cmd = ["ebook-convert", str(input_path), str(output_path)]
        log.debug("calibre.local.convert", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            log.error("calibre.local.convert_failed", extra={"stderr": result.stderr})
            raise ConversionError(
                f"ebook-convert failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        log.info("calibre.local.converted", extra={"output": str(output_path)})


def _metadata_flags(result: MetadataResult) -> list[list[str]]:
    """Build --field flags for calibredb set_metadata.

    Sets title and authors explicitly so that calibredb's initial add (which
    reads embedded EPUB/M4B tags, which may be wrong or missing) is corrected
    with the API-resolved values.
    """
    flags = []
    # Always overwrite title and authors — the file's embedded tags are often wrong
    if result.title:
        flags.append(["--field", f"title:{result.title}"])
    if result.best and result.best.candidate.authors:
        # Calibre uses " & " to separate multiple authors
        authors_str = " & ".join(result.best.candidate.authors)
        flags.append(["--field", f"authors:{authors_str}"])
    if result.publisher:
        flags.append(["--field", f"publisher:{result.publisher}"])
    if result.description:
        flags.append(["--field", f"comments:{result.description}"])
    if result.language:
        flags.append(["--field", f"languages:{result.language}"])
    if result.isbn:
        flags.append(["--field", f"identifiers:isbn:{result.isbn}"])
    if result.series:
        flags.append(["--field", f"series:{result.series}"])
    if result.series_index is not None:
        flags.append(["--field", f"series_index:{result.series_index}"])
    return flags


def _normalise_book_entry(raw: dict) -> dict:
    """Normalise a single calibredb --for-machine list entry.

    calibredb varies across versions in how it represents authors and formats:
      - authors: str "Andy Weir" | list ["Andy Weir"]
      - formats: str "/path/book.epub" | list ["/path/book.epub"]
    Returns a consistent dict with id (int), title (str), authors (list[str]),
    formats (list[str] of lowercase extensions, no leading dot).
    """
    # authors: comma-separated string or list
    raw_authors = raw.get("authors", "")
    if isinstance(raw_authors, list):
        authors = raw_authors
    else:
        authors = [a.strip() for a in str(raw_authors).split("&") if a.strip()]

    # formats: single path string or list of path strings
    raw_formats = raw.get("formats", "")
    if isinstance(raw_formats, list):
        fmt_paths = raw_formats
    elif raw_formats:
        fmt_paths = [raw_formats]
    else:
        fmt_paths = []

    exts = [Path(p).suffix.lstrip(".").lower() for p in fmt_paths if p]

    return {
        "id": int(raw.get("id", -1)),
        "title": str(raw.get("title", "")),
        "authors": authors,
        "formats": exts,
    }


def _parse_formats(stdout: str) -> set[str]:
    """Extract lowercase format extensions from calibredb list --fields formats output.

    calibredb output varies: extensions appear as bare filenames or in a
    Python-list repr.  A simple regex over all dot-extensions is robust to both.
    """
    import re
    return {ext.lower() for ext in re.findall(r"\.([a-z0-9]{2,5})\b", stdout, re.IGNORECASE)}


def _parse_book_id(stdout: str) -> int:
    """Extract the book ID from calibredb add output.

    calibredb output varies across versions:
      - "Added book ids: 42"
      - "Added book ids:42"
      - "book id: 42"
      - Plain integer on its own line

    Returns -1 if no ID can be found.

    WARNING: do NOT fall back to "last integer in stdout" — third-party
    plugins (e.g. DeDRM) write timing info like "0.2 seconds" to stdout,
    and the last integer found would be 2, which is a valid Calibre book ID.
    """
    # Canonical: "Added book ids: 42" or "Added book ids:42"
    m = re.search(r"Added book ids?:\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Some versions: "book id: 42"
    m = re.search(r"\bbook\s+id:\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Last resort: a standalone integer on its own line
    # (some calibredb versions print just the ID with nothing else on that line)
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.isdigit():
            return int(stripped)
    return -1
