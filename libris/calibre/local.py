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


class LocalCalibre(CalibreBackend):
    """Calls calibredb and ebook-convert as local subprocesses."""

    def __init__(self, config: CalibreConfig) -> None:
        self._config = config
        if config.library_path is None:
            raise ValueError("LocalCalibre requires calibre.library_path to be set")
        self._library = config.library_path

    def add_book(self, file_path: Path) -> int:
        cmd = [
            "calibredb", "add",
            str(file_path),
            "--with-library", str(self._library),
            "--automerge", "ignore",
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

        book_id = _parse_book_id(result.stdout)
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
        cmd = [
            "calibredb", "set_cover",
            "--with-library", str(self._library),
            str(book_id), str(cover_path),
        ]
        log.debug("calibre.local.set_cover", extra={"cmd": cmd})
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            log.warning("calibre.local.set_cover_failed", extra={"stderr": result.stderr})
        else:
            log.info("calibre.local.cover_set", extra={"book_id": book_id})

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
    """Build --field flags for calibredb set_metadata."""
    flags = []
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


def _parse_book_id(stdout: str) -> int:
    """Extract the book ID from calibredb add output.

    calibredb prints: "Added book ids: 42" or "Empty search result"
    """
    m = re.search(r"Added book ids?:\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Some versions print "book id: 42"
    m = re.search(r"book id:\s*(\d+)", stdout, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return -1   # unknown ID; import still succeeded
