"""Embed book metadata (title, author, year) into an M4B file via ffmpeg."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from ..exceptions import ConversionError
from ..metadata.base import MetadataResult

log = logging.getLogger(__name__)


def embed_metadata(audio_path: Path, result: MetadataResult) -> None:
    """Embed title, author, and year into an M4B in-place.

    Uses a temp file to avoid corrupting the original on failure.

    Args:
        audio_path: M4B file to tag (modified in-place on success).
        result: Resolved metadata to embed.

    Raises:
        ConversionError: If ffmpeg fails.
    """
    if _already_tagged(audio_path):
        log.info("audio.tagger.skip_already_tagged", extra={"file": str(audio_path)})
        return

    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".m4b", dir=audio_path.parent)
    tmp_path = Path(tmp_path_str)

    try:
        import os
        os.close(tmp_fd)

        cmd = [
            "ffmpeg", "-i", str(audio_path),
            "-metadata", f"title={result.title}",
            "-metadata", f"artist={result.author}",
            "-metadata", f"album_artist={result.author}",
            "-metadata", f"album={result.title}",
            "-metadata", f"date={result.year}",
            "-c", "copy",
            str(tmp_path), "-y",
        ]
        log.debug("audio.tagger.embed", extra={"cmd": cmd})
        run_result = subprocess.run(cmd, capture_output=True, text=True)

        if run_result.returncode != 0:
            raise ConversionError(
                f"ffmpeg metadata embed failed (rc={run_result.returncode}): "
                f"{run_result.stderr[-500:].strip()}"
            )

        # Atomically replace original
        tmp_path.replace(audio_path)
        log.info(
            "audio.tagger.embedded",
            extra={
                "file": str(audio_path),
                "title": result.title,
                "author": result.author,
                "year": result.year,
            },
        )

    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _already_tagged(audio_path: Path) -> bool:
    """Return True if the file already has both title and artist tags."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format_tags=title,artist",
            "-of", "default=noprint_wrappers=1",
            str(audio_path),
        ],
        capture_output=True, text=True,
    )
    lines = [line for line in result.stdout.splitlines() if "=." not in line and "=" in line]
    tagged_count = sum(1 for line in result.stdout.splitlines() if line.strip().endswith("=") is False and "=" in line)
    # Count non-empty tag values
    non_empty = [
        line for line in result.stdout.splitlines()
        if "=" in line and line.split("=", 1)[1].strip()
    ]
    return len(non_empty) >= 2
