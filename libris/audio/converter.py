"""Audio pipeline: convert audio files and combine multi-part audiobooks into M4B.

Handles:
  - Single audio files (any format → M4B)
  - Multi-part audiobooks in a folder (combine → single M4B with chapter markers)
  - Preserves embedded chapter metadata within each part
  - Offsets chapter timestamps correctly when combining
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

from ..exceptions import ConversionError

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = frozenset({"mp3", "m4a", "m4b", "flac", "ogg", "aac", "opus", "wav"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def convert_to_m4b(input_path: Path, output_path: Path) -> None:
    """Convert a single audio file to M4B format.

    Args:
        input_path: Source audio file.
        output_path: Destination .m4b file.

    Raises:
        ConversionError: If ffmpeg fails.
    """
    cmd = [
        "ffmpeg", "-i", str(input_path),
        "-c:a", "aac", "-b:a", "128k",
        "-f", "mp4",
        str(output_path),
        "-y",
    ]
    _run(cmd, f"convert {input_path.name} → M4B")
    log.info("audio.converted", extra={"output": str(output_path)})


def combine_parts(
    part_files: list[Path],
    output_path: Path,
) -> None:
    """Combine multiple audio parts into a single M4B with chapter markers.

    Each part's embedded chapters are preserved. If a part has no embedded
    chapters, a single chapter spanning the full duration is created.

    Args:
        part_files: Ordered list of audio files (will be sorted by name).
        output_path: Destination .m4b file.

    Raises:
        ConversionError: If any ffmpeg pass fails.
        ValueError: If part_files is empty.
    """
    if not part_files:
        raise ValueError("part_files must not be empty")

    parts = sorted(part_files, key=lambda p: p.name.lower())

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        filelist = tmp_path / "filelist.txt"
        metadata_file = tmp_path / "chapters.ffmeta"
        combined = tmp_path / "combined.m4b"
        with_chapters = tmp_path / "with_chapters.m4b"

        # ── Build filelist and chapter metadata ──────────────────────────
        metadata_file.write_text(";FFMETADATA1\n")
        offset_ms = 0
        with filelist.open("w") as f:
            for part in parts:
                f.write(f"file '{part}'\n")
                dur_ms = _extract_chapters(part, offset_ms, metadata_file)
                offset_ms += dur_ms

        # ── Pass 1: concatenate all parts (stream-copy, no re-encode) ───────
        # Using -c copy avoids encoder initialisation errors (e.g.
        # "Could not open encoder before EOF") that occur when ffmpeg tries
        # to transcode already-AAC M4B parts.  Parts are guaranteed to be
        # M4B/AAC by the time they reach this function — see
        # _handle_pending_part in pipeline.py which converts non-M4B files
        # before staging.
        _run([
            "ffmpeg", "-f", "concat", "-safe", "0",
            "-i", str(filelist),
            "-c", "copy",
            str(combined), "-y",
        ], f"concatenate {len(parts)} parts")

        # ── Pass 2: embed chapter markers ─────────────────────────────────
        _run([
            "ffmpeg",
            "-i", str(combined),
            "-i", str(metadata_file),
            "-map", "0",
            "-map_chapters", "1",
            "-c", "copy",
            str(with_chapters), "-y",
        ], "embed chapters")

        # ── Copy to final destination ─────────────────────────────────────
        output_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.copy2(str(with_chapters), str(output_path))

    log.info(
        "audio.combined",
        extra={"parts": len(parts), "output": str(output_path), "duration_ms": offset_ms},
    )


def find_audio_files(directory: Path, recursive: bool = True) -> list[Path]:
    """Find all audio files in a directory, optionally recursive."""
    pattern = "**/*" if recursive else "*"
    return sorted(
        p for p in directory.glob(pattern)
        if p.is_file() and p.suffix.lstrip(".").lower() in AUDIO_EXTENSIONS
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_chapters(audio_file: Path, offset_ms: int, metadata_file: Path) -> int:
    """Extract chapter info from audio_file, write to metadata_file, return duration_ms."""
    dur_ms = _get_duration_ms(audio_file)
    chapters = _probe_chapters(audio_file)

    with metadata_file.open("a") as f:
        if chapters:
            for ch in chapters:
                start = int(ch["start_ms"]) + offset_ms
                end = int(ch["end_ms"]) + offset_ms
                title = ch.get("title") or "Chapter"
                f.write(
                    f"\n[CHAPTER]\nTIMEBASE=1/1000\n"
                    f"START={start}\nEND={end}\ntitle={title}\n"
                )
        else:
            # No embedded chapters — treat the whole part as one chapter
            title = audio_file.stem
            f.write(
                f"\n[CHAPTER]\nTIMEBASE=1/1000\n"
                f"START={offset_ms}\nEND={offset_ms + dur_ms}\ntitle={title}\n"
            )

    return dur_ms


def _get_duration_ms(audio_file: Path) -> int:
    """Return the duration of an audio file in milliseconds."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(audio_file)],
        capture_output=True, text=True,
    )
    raw = result.stdout.strip()
    try:
        return int(float(raw) * 1000)
    except (ValueError, TypeError):
        log.warning("audio.duration_parse_failed", extra={"file": str(audio_file), "raw": raw})
        return 0


def _probe_chapters(audio_file: Path) -> list[dict]:
    """Return list of chapter dicts with start_ms, end_ms, title."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json",
         "-show_chapters", str(audio_file)],
        capture_output=True, text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []

    import json
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    chapters = []
    for ch in data.get("chapters", []):
        tb_num, tb_den = 1, 1000
        tb = ch.get("time_base", "1/1000")
        if "/" in tb:
            parts = tb.split("/")
            try:
                tb_num, tb_den = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                pass

        start = ch.get("start", 0)
        end = ch.get("end", 0)
        title = ch.get("tags", {}).get("title", "")

        chapters.append({
            "start_ms": int(int(start) * tb_num / tb_den * 1000),
            "end_ms":   int(int(end)   * tb_num / tb_den * 1000),
            "title": title,
        })

    return chapters


def _run(cmd: list[str], description: str) -> None:
    """Run a subprocess command, raise ConversionError on failure."""
    log.debug("audio.ffmpeg", extra={"cmd": cmd, "description": description})
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise ConversionError(
            f"ffmpeg failed ({description}, rc={result.returncode}): "
            f"{result.stderr[-500:].strip()}"
        )
