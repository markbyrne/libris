"""Ebook format conversion: delegates to the configured CalibreBackend."""

from __future__ import annotations

import logging
from pathlib import Path

from ..calibre.base import CalibreBackend
from ..exceptions import ConversionError

log = logging.getLogger(__name__)

# Formats that calibredb can import natively without conversion
NATIVE_FORMATS = frozenset({"epub", "mobi", "pdf", "azw", "azw3", "lit"})


def to_format(
    input_path: Path,
    target_format: str,
    dest_dir: Path,
    calibre: CalibreBackend,
) -> Path:
    """Convert an ebook to *target_format* using the configured Calibre backend.

    If the file is already in the target format the input path is returned
    unchanged (no conversion, no copy).  Otherwise the converted file is
    written to *dest_dir* and that path is returned.

    Args:
        input_path:    Source ebook file.
        target_format: Target extension without leading dot, e.g. ``"epub"``.
        dest_dir:      Directory where the converted file should be written.
                       Created if it does not exist.
        calibre:       CalibreBackend to use for conversion.

    Returns:
        Path to the (possibly converted) ebook file.

    Raises:
        ConversionError: If ebook-convert fails or produces no output.
    """
    ext = input_path.suffix.lstrip(".").lower()
    target = target_format.lower()

    if ext == target:
        log.debug(
            "ebook.already_target_format",
            extra={"file": str(input_path), "format": target},
        )
        return input_path

    dest_dir.mkdir(parents=True, exist_ok=True)
    output_path = dest_dir / (input_path.stem + f".{target}")

    log.info(
        "ebook.converting",
        extra={"from": ext, "to": target, "file": str(input_path), "output": str(output_path)},
    )

    calibre.convert_ebook(input_path, output_path)

    if not output_path.exists():
        raise ConversionError(
            f"ebook-convert succeeded but output not found: {output_path}"
        )

    return output_path


# ---------------------------------------------------------------------------
# Backwards-compatible alias
# ---------------------------------------------------------------------------

def to_epub(input_path: Path, calibre: CalibreBackend, dest_dir: Path | None = None) -> Path:
    """Convert to EPUB.  Wraps ``to_format`` for callers that predate the
    generalised converter; *dest_dir* defaults to the same directory as the
    source file when not provided.
    """
    return to_format(
        input_path,
        "epub",
        dest_dir if dest_dir is not None else input_path.parent,
        calibre,
    )
