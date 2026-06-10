"""Local calibredb backend — runs calibredb and ebook-convert directly."""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from ..classifier import AUDIO_EXTENSIONS as _AUDIO_EXT
from ..classifier import EBOOK_EXTENSIONS as _EBOOK_EXT
from ..config import CalibreConfig
from ..exceptions import CalibreImportError, ConversionError
from ..metadata.base import MetadataResult
from .base import CalibreBackend, format_authors

log = logging.getLogger(__name__)

# Derived from the classifier's extension sets so export detection stays in sync
# when new formats are added to either set.  Dot-prefixed for Path.suffix comparison.
_BOOK_EXTENSIONS: frozenset[str] = frozenset(
    f".{ext}" for ext in (*_EBOOK_EXT, *_AUDIO_EXT)
)


class LocalCalibre(CalibreBackend):
    """Calls calibredb and ebook-convert as local subprocesses."""

    def __init__(self, config: CalibreConfig) -> None:
        self._config = config
        if config.library_db_path is None:
            raise ValueError(
                "LocalCalibre requires calibre.library_db_path to be set "
                "(or the legacy calibre.library_path key)"
            )
        # _library — where metadata.db lives; used with --with-library for all calibredb calls
        self._library: Path = config.library_db_path
        # _book_files — where physical book files (EPUB, M4B, …) are stored.
        # Equals _library unless the user has configured calibre-web's
        # "Separate Book Files from Library" feature (Issue #18).
        self._book_files: Path = config.effective_book_path or config.library_db_path

    def add_book(
        self,
        file_path: Path,
        title: str | None = None,
        authors: str | None = None,
    ) -> int:
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
        # --title/--authors determine the directory calibredb creates.
        # Without them calibredb parses the filename as "{title} - {author}"
        # (it never reads embedded M4B audio tags) and the later set_metadata
        # rename is a no-op in split-library mode because the files have
        # already been relocated.
        if title:
            cmd += ["--title", title]
        if authors:
            cmd += ["--authors", authors]
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

        # Split-path mode: move physical book files from library_db_path to book_file_path.
        # calibredb add always places files under _library (metadata.db location).
        # If book_file_path differs, relocate the files so calibre-web can find them.
        if self._book_files != self._library:
            self._relocate_to_book_files(book_id)

        return book_id

    # ------------------------------------------------------------------
    # Split-path helpers (Issue #18)
    # ------------------------------------------------------------------

    def _get_format_paths(self, book_id: int) -> list[Path]:
        """Return the absolute paths of all formats calibredb has stored for *book_id*.

        calibredb stores files under _library after 'add'.  This helper is used
        by _relocate_to_book_files to find what was just added.
        """
        import json as _json

        cmd = [
            "calibredb", "list",
            "--for-machine",
            "--search", f"id:{book_id}",
            "--fields", "formats",
            "--with-library", str(self._library),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("calibre.local.get_format_paths_failed", extra={"stderr": result.stderr})
            return []
        try:
            rows = _json.loads(result.stdout)
            if not rows:
                return []
            raw_formats = rows[0].get("formats", [])
            if isinstance(raw_formats, str):
                raw_formats = [raw_formats] if raw_formats else []
            return [Path(p) for p in raw_formats if p]
        except Exception as exc:
            log.warning("calibre.local.get_format_paths_parse_error: %s", exc)
            return []

    def _relocate_to_book_files(self, book_id: int) -> None:
        """Move all book files from library_db_path into book_file_path after import.

        calibredb always writes files into _library (the metadata.db location).
        When book_file_path is configured separately (calibre-web split-library
        mode), all files in the book's directory must be moved so calibre-web
        can find them — this includes format files (.epub, .m4b), cover.jpg,
        and metadata.opf.

        The relative path (Author/Title (id)/) is preserved so calibre-web's
        path resolution continues to work correctly.
        """
        format_paths = self._get_format_paths(book_id)
        if not format_paths:
            log.warning("calibre.local.relocate_no_formats: book_id=%s", book_id)
            return

        # All format files for a book live in the same directory.
        # Use the first valid one to locate the book's directory under _library.
        src_dir: Path | None = None
        for fmt_path in format_paths:
            if fmt_path.exists():
                try:
                    fmt_path.relative_to(self._library)
                    src_dir = fmt_path.parent
                    break
                except ValueError:
                    continue

        if src_dir is None:
            log.warning("calibre.local.relocate_src_dir_missing: book_id=%s", book_id)
            return

        try:
            rel_dir = src_dir.relative_to(self._library)
        except ValueError:
            log.warning(
                "calibre.local.relocate_not_under_library: %s (library=%s)",
                src_dir, self._library,
            )
            return

        dest_dir = self._book_files / rel_dir
        dest_dir.mkdir(parents=True, exist_ok=True)

        # Move every file in the book directory: formats + cover.jpg + metadata.opf
        for src in src_dir.iterdir():
            if not src.is_file():
                continue
            dest = dest_dir / src.name
            shutil.move(str(src), str(dest))
            log.info(
                "calibre.local.relocated",
                extra={"src": str(src), "dest": str(dest), "book_id": book_id},
            )

        # Remove the now-empty book directory under library_db_path
        try:
            src_dir.rmdir()
        except OSError:
            pass  # Non-empty or already gone — safe to ignore

    def _relocate_cover(self, book_id: int) -> None:
        """Move cover.jpg from library_db_path to book_file_path.

        Called after set_cover() because calibredb always writes cover.jpg into
        _library regardless of whether split-path mode is active.  Without this,
        calibre-web cannot find the cover when book_file_path != library_db_path.

        _get_format_paths() returns paths as calibredb knows them (under _library),
        so the parent directory gives us the exact location of the newly-written
        cover.jpg.
        """
        for fmt_path in self._get_format_paths(book_id):
            try:
                rel_dir = fmt_path.relative_to(self._library).parent
            except ValueError:
                continue
            src_cover = self._library / rel_dir / "cover.jpg"
            if src_cover.exists():
                dest_cover = self._book_files / rel_dir / "cover.jpg"
                dest_cover.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_cover), str(dest_cover))
                log.info(
                    "calibre.local.cover_relocated",
                    extra={"book_id": book_id, "dest": str(dest_cover)},
                )
                return
        log.debug("calibre.local.cover_relocate_not_found: book_id=%s", book_id)

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
            # In split-path mode calibredb writes cover.jpg back into _library.
            # Move it to _book_files so calibre-web can find it.
            if self._book_files != self._library:
                self._relocate_cover(book_id)

    def export_book(self, book_id: int, dest_dir: Path) -> list[Path]:
        dest_dir.mkdir(parents=True, exist_ok=True)

        # In split-library mode calibredb always constructs the physical file path
        # as `_library / books.path / filename`.  But _relocate_to_book_files has
        # already moved those files to `_book_files / books.path / filename`.
        # `calibredb export` therefore finds rc=0 but exports nothing because the
        # files aren't where it expects them.  Bypass calibredb and copy directly
        # from _book_files using the paths calibredb reports.
        if self._book_files != self._library:
            return self._export_from_book_files(book_id, dest_dir)

        cmd = [
            "calibredb", "export",
            "--to-dir", str(dest_dir),
            "--dont-save-cover",
            "--dont-write-opf",
            "--single-dir",          # force flat output — without this calibredb creates
                                     # per-book subdirectories and may write 0 files to dest_dir
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
        exported = [f for f in dest_dir.rglob("*") if f.is_file() and f.suffix.lower() in _BOOK_EXTENSIONS]
        log.info("calibre.local.exported", extra={"book_id": book_id, "files": [str(f) for f in exported]})
        return exported

    def _export_from_book_files(self, book_id: int, dest_dir: Path) -> list[Path]:
        """Copy book files from _book_files when split-library mode is active.

        calibredb reports format paths as absolute paths under _library.  After
        _relocate_to_book_files those files live at the same relative path under
        _book_files instead.  This helper:
          1. Asks calibredb where it thinks the files are (under _library).
          2. Remaps each path from _library → _book_files.
          3. Copies any that exist at the remapped location into dest_dir.

        If the remapped path doesn't exist (e.g. an older book whose path was
        changed by set_metadata before this fix was applied), the helper falls
        back to scanning _book_files for any file whose relative path suffix
        matches what calibredb reports — a best-effort recovery for pre-fix
        imports.
        """
        import json as _json  # noqa: PLC0415

        cmd = [
            "calibredb", "list",
            "--for-machine",
            "--search", f"id:{book_id}",
            "--fields", "formats",
            "--with-library", str(self._library),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise CalibreImportError(
                f"calibredb list failed while exporting book {book_id} "
                f"(rc={result.returncode}): {result.stderr.strip()}"
            )

        try:
            rows = _json.loads(result.stdout)
        except Exception:
            rows = []

        if not rows:
            log.info("calibre.local.exported",
                     extra={"book_id": book_id, "files": []})
            return []

        raw_formats = rows[0].get("formats", [])
        if isinstance(raw_formats, str):
            raw_formats = [raw_formats] if raw_formats else []

        exported: list[Path] = []
        for fmt_str in raw_formats:
            fmt_path = Path(fmt_str)

            # Primary: remap _library → _book_files preserving relative path
            try:
                rel = fmt_path.relative_to(self._library)
                candidate = self._book_files / rel
            except ValueError:
                # Path not under _library — unexpected; try as-is
                candidate = fmt_path

            if candidate.exists():
                dest = dest_dir / candidate.name
                shutil.copy2(str(candidate), str(dest))
                exported.append(dest)
                continue

            # Fallback: scan _book_files for a file with the same name in any
            # sub-directory.  Handles books whose directory was renamed by an
            # earlier set_metadata call before this fix was applied.
            fname = fmt_path.name
            matches = [
                p for p in self._book_files.rglob(fname)
                if p.is_file() and p.suffix.lower() in _BOOK_EXTENSIONS
            ]
            if matches:
                dest = dest_dir / matches[0].name
                shutil.copy2(str(matches[0]), str(dest))
                exported.append(dest)
                log.warning(
                    "calibre.local.export_fallback_match",
                    extra={"book_id": book_id, "found": str(matches[0]),
                           "expected": str(candidate)},
                )

        log.info("calibre.local.exported",
                 extra={"book_id": book_id, "files": [str(f) for f in exported]})
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
        flags.append(["--field", f"authors:{format_authors(result.best.candidate.authors)}"])
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
