"""Ebook format conversion: delegates to the configured CalibreBackend."""

from __future__ import annotations

import logging
from pathlib import Path

from ..calibre.base import CalibreBackend
from ..exceptions import ConversionError

log = logging.getLogger(__name__)

# Formats that calibredb can import natively without conversion
NATIVE_FORMATS = frozenset({"epub", "mobi", "pdf", "azw", "azw3", "lit"})


def to_epub(input_path: Path, calibre: CalibreBackend) -> Path:
    """Convert an ebook to EPUB format using the configured Calibre backend.

    If the file is already EPUB, returns the input path unchanged.

    Args:
        input_path: Source ebook file.
        calibre: CalibreBackend to use for conversion.

    Returns:
        Path to the EPUB file (may be the original if already EPUB).

    Raises:
        ConversionError: If ebook-convert fails.
    """
    ext = input_path.suffix.lstrip(".").lower()

    if ext == "epub":
        log.debug("ebook.already_epub", extra={"file": str(input_path)})
        return input_path

    output_path = input_path.with_suffix(".epub")
    log.info(
        "ebook.converting",
        extra={"from": ext, "file": str(input_path), "output": str(output_path)},
    )

    calibre.convert_ebook(input_path, output_path)

    if not output_path.exists():
        raise ConversionError(f"ebook-convert succeeded but output not found: {output_path}")

    return output_path
