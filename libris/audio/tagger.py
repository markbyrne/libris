"""Embed full book metadata and cover art into an M4B file via ffmpeg."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ..exceptions import ConversionError
from ..metadata.base import MetadataResult

log = logging.getLogger(__name__)


def embed_metadata(
    audio_path: Path,
    result: MetadataResult,
    overwrite: bool = True,
    cover_path: Optional[Path] = None,
) -> None:
    """Embed title, author, year, and all available metadata into an M4B in-place.

    Optionally embeds a cover image as album art.
    Uses a temp file to avoid corrupting the original on failure.

    Args:
        audio_path: M4B file to tag (modified in-place on success).
        result: Resolved metadata to embed.
        overwrite: If False, skip files that already have title + artist tags.
        cover_path: Optional path to a cover image (overrides result.cover_path).

    Raises:
        ConversionError: If ffmpeg fails.
    """
    if not overwrite and _already_tagged(audio_path):
        log.info("audio.tagger.skip_already_tagged", extra={"file": str(audio_path)})
        return

    effective_cover = cover_path or result.cover_path
    tmp_fd, tmp_path_str = tempfile.mkstemp(suffix=".m4b", dir=audio_path.parent)
    tmp_path = Path(tmp_path_str)

    try:
        import os
        os.close(tmp_fd)

        cmd = _build_ffmpeg_cmd(audio_path, tmp_path, result, effective_cover)
        log.debug("audio.tagger.embed", extra={"cmd": cmd})
        run_result = subprocess.run(cmd, capture_output=True, text=True)

        if run_result.returncode != 0:
            raise ConversionError(
                f"ffmpeg metadata embed failed (rc={run_result.returncode}): "
                f"{run_result.stderr[-500:].strip()}"
            )

        tmp_path.replace(audio_path)
        log.info(
            "audio.tagger.embedded",
            extra={
                "file": str(audio_path),
                "title": result.title,
                "author": result.author,
                "year": result.year,
                "has_cover": effective_cover is not None,
            },
        )

    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _build_ffmpeg_cmd(
    input_path: Path,
    output_path: Path,
    result: MetadataResult,
    cover_path: Optional[Path],
) -> list[str]:
    """Build the ffmpeg command to embed metadata and optional cover art."""
    has_cover = cover_path is not None and cover_path.exists()

    cmd = ["ffmpeg", "-i", str(input_path)]
    if has_cover:
        cmd += ["-i", str(cover_path)]

    cmd += ["-map", "0:a"]
    if has_cover:
        cmd += ["-map", "1:v"]

    # Core tags
    tags: dict[str, str] = {
        "title": result.title,
        "artist": result.author,
        "album_artist": result.author,
        "album": result.title,
    }
    if result.year:
        tags["date"] = result.year
    if result.publisher:
        tags["publisher"] = result.publisher
    if result.description:
        tags["comment"] = result.description[:500]
    if result.language:
        tags["language"] = result.language
    if result.series:
        # grouping  — read by Apple Books, Prologue, Overcast, and most players
        index_suffix = f" #{int(result.series_index)}" if result.series_index is not None else ""
        tags["grouping"] = f"{result.series}{index_suffix}"
        # series / series-part — AudioBookshelf custom tags
        tags["series"] = result.series
        if result.series_index is not None:
            tags["series-part"] = str(int(result.series_index))

    for key, value in tags.items():
        if value:
            cmd += ["-metadata", f"{key}={value}"]

    if has_cover:
        cmd += [
            "-metadata:s:v", "title=Album cover",
            "-metadata:s:v", "comment=Cover (front)",
            "-c:a", "copy",
            "-c:v", "mjpeg",
            "-disposition:v", "attached_pic",
        ]
    else:
        cmd += ["-c", "copy"]

    cmd += [str(output_path), "-y"]
    return cmd


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
    non_empty = [
        line for line in result.stdout.splitlines()
        if "=" in line and line.split("=", 1)[1].strip()
    ]
    return len(non_empty) >= 2
