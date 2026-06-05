"""Local calibredb backend — runs calibredb and ebook-convert directly."""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from ..config import CalibreConfig
from ..exceptions import CalibreImportError, ConversionError
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
