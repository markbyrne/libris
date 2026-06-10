"""Integration tests for embed_metadata against REAL ffmpeg.

The v0.3.7b0 regression (``-map_metadata 0:c`` hard-failing ffmpeg on any
chapterless input with "Invalid chapter index 0") shipped because all unit
tests only asserted the shape of the generated command with subprocess
mocked out — invalid ffmpeg syntax was never executed.  These tests close
that gap: they generate real M4B files with ffmpeg, run embed_metadata
unmocked, and verify the output with ffprobe.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="requires ffmpeg and ffprobe in PATH",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    title: str = "Pendragon: The Merchant Of Death",
    author: str = "D.J. MacHale",
) -> MagicMock:
    result = MagicMock()
    result.title = title
    result.author = author
    result.year = "2002"
    result.publisher = None
    result.description = None
    result.language = "en"
    result.series = None
    result.series_index = None
    result.cover_path = None
    return result


def _make_chapterless_m4b(dest: Path) -> None:
    """Generate a 1-second silent M4B with no chapters and no tags."""
    subprocess.run(
        [
            "ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-t", "1", "-c:a", "aac", str(dest), "-y",
        ],
        capture_output=True, text=True, check=True,
    )


def _make_chaptered_m4b(dest: Path, tmp_dir: Path) -> None:
    """Generate a 1-second silent M4B with one chapter marker."""
    meta = tmp_dir / "chapters.ffmeta"
    meta.write_text(
        ";FFMETADATA1\n"
        "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1000\ntitle=Chapter One\n"
    )
    subprocess.run(
        [
            "ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-i", str(meta),
            "-map_metadata", "1", "-map_chapters", "1",
            "-t", "1", "-c:a", "aac", str(dest), "-y",
        ],
        capture_output=True, text=True, check=True,
    )


def _probe_tags(path: Path) -> dict[str, str]:
    out = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format_tags",
            "-of", "json", str(path),
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout).get("format", {}).get("tags", {})


def _probe_chapters(path: Path) -> list[dict]:
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_chapters", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(out.stdout).get("chapters", [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEmbedRealFfmpeg:

    def test_embed_succeeds_on_chapterless_m4b(self, tmp_path):
        """embed_metadata must not raise on an input with no chapters.

        This is the exact v0.3.7b0 regression: -map_metadata 0:c made ffmpeg
        exit with "Invalid chapter index 0" for every chapterless M4B.
        """
        from libris.audio.tagger import embed_metadata

        m4b = tmp_path / "Book01-Merchant of Death.m4b"
        _make_chapterless_m4b(m4b)

        embed_metadata(m4b, _make_result(), overwrite=True)  # must not raise

    def test_embed_succeeds_on_chaptered_m4b(self, tmp_path):
        from libris.audio.tagger import embed_metadata

        m4b = tmp_path / "book.m4b"
        _make_chaptered_m4b(m4b, tmp_path)

        embed_metadata(m4b, _make_result(), overwrite=True)  # must not raise

    def test_tags_written_chapterless(self, tmp_path):
        """The new title/artist/album_artist appear in the output file."""
        from libris.audio.tagger import embed_metadata

        m4b = tmp_path / "book.m4b"
        _make_chapterless_m4b(m4b)

        embed_metadata(m4b, _make_result(), overwrite=True)

        tags = _probe_tags(m4b)
        assert tags.get("title") == "Pendragon: The Merchant Of Death"
        assert tags.get("artist") == "D.J. MacHale"
        assert tags.get("album_artist") == "D.J. MacHale"

    def test_chapters_preserved(self, tmp_path):
        """Chapter markers survive the embed (-map_chapters 0)."""
        from libris.audio.tagger import embed_metadata

        m4b = tmp_path / "book.m4b"
        _make_chaptered_m4b(m4b, tmp_path)
        assert _probe_chapters(m4b), "fixture should have a chapter"

        embed_metadata(m4b, _make_result(), overwrite=True)

        chapters = _probe_chapters(m4b)
        assert chapters, "chapter markers were stripped by embed_metadata"
        assert chapters[0]["tags"]["title"] == "Chapter One"

    def test_stale_tags_cleared(self, tmp_path):
        """Pre-existing wrong tags are fully replaced, not merely appended.

        Simulates the Brisingr scenario: the source file carries the book
        title in the artist field.  After embed, ffprobe must show ONLY the
        corrected artist.
        """
        from libris.audio.tagger import embed_metadata

        m4b = tmp_path / "book.m4b"
        raw = tmp_path / "raw.m4b"
        _make_chapterless_m4b(raw)
        # Pre-tag with deliberately wrong values
        subprocess.run(
            [
                "ffmpeg", "-i", str(raw),
                "-metadata", "artist=Brisingr",
                "-metadata", "title=Inheritance Cycle 3",
                "-c", "copy", str(m4b), "-y",
            ],
            capture_output=True, text=True, check=True,
        )
        assert _probe_tags(m4b).get("artist") == "Brisingr"

        embed_metadata(m4b, _make_result(title="Brisingr", author="Christopher Paolini"),
                       overwrite=True)

        tags = _probe_tags(m4b)
        assert tags.get("artist") == "Christopher Paolini"
        assert tags.get("title") == "Brisingr"
