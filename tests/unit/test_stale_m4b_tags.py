"""Tests for stale M4B tag clearing in _build_ffmpeg_cmd.

What these flags do
-------------------
``-map_metadata:g -1`` discards inherited GLOBAL metadata from the source
file so stale tags (e.g. a book title left in the artist field by the
file's original tagger) cannot persist alongside the values we explicitly
pass via ``-metadata key=value``.  The ``:g`` out-spec matters: a bare
``-map_metadata -1`` also wipes per-chapter metadata, stripping chapter
titles.  ``-map_chapters 0`` copies chapter markers so combined M4Bs keep
their timing data.

Note: cleaning these tags matters for audiobook players (Audiobookshelf,
Apple Books, Prologue) — NOT for Calibre's directory structure.
``calibredb add`` never reads M4B audio tags; it parses the FILENAME as
``{title} - {author}``.  The directory fix lives in add_book's
``--title``/``--authors`` flags, not here.

Regression note: an earlier version used ``-map_metadata 0:c`` to restore
chapters.  That syntax makes ffmpeg hard-fail with "Invalid chapter index
0" on any input WITHOUT chapters (single-file M4Bs), so every embed on
such files raised ConversionError.  ``-map_chapters 0`` is the correct
flag and is a no-op when the input has no chapters.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    title: str = "Brisingr",
    author: str = "Christopher Paolini",
    series: str | None = "Inheritance Cycle",
    series_index: float | None = 3.0,
) -> MagicMock:
    """Build a minimal MetadataResult mock."""
    result = MagicMock()
    result.title = title
    result.author = author
    result.year = "2008"
    result.publisher = "Knopf"
    result.description = None
    result.language = "en"
    result.series = series
    result.series_index = series_index
    result.cover_path = None
    return result


def _build_cmd(
    input_path: Path,
    output_path: Path,
    result: MagicMock,
    cover_path: Path | None = None,
) -> list[str]:
    from libris.audio.tagger import _build_ffmpeg_cmd  # noqa: PLC0415
    return _build_ffmpeg_cmd(input_path, output_path, result, cover_path)


# ---------------------------------------------------------------------------
# Tests: -map_metadata -1 presence
# ---------------------------------------------------------------------------

class TestMapMetadataClear:
    """The generated command must clear inherited metadata before writing ours."""

    def test_map_metadata_minus1_present(self, tmp_path):
        """-map_metadata -1 must appear in the command (clears stale atoms)."""
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())

        pairs = list(zip(cmd, cmd[1:], strict=False))
        assert ("-map_metadata:g", "-1") in pairs, (
            "-map_metadata -1 is missing; stale ©ART / ©alb atoms will persist "
            "and calibredb will read the wrong author/title from the source file."
        )

    def test_map_metadata_minus1_before_metadata_flags(self, tmp_path):
        """-map_metadata -1 must come before any -metadata flag."""
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())

        first_meta_idx = next(
            (i for i, tok in enumerate(cmd) if tok == "-metadata"),
            len(cmd),
        )
        clear_idx = next(
            (i for i in range(len(cmd) - 1) if cmd[i] == "-map_metadata:g" and cmd[i + 1] == "-1"),
            None,
        )
        assert clear_idx is not None, "-map_metadata -1 not found"
        assert clear_idx < first_meta_idx, (
            "-map_metadata -1 must precede any -metadata flag so that it does not "
            "accidentally suppress the new tag values."
        )

    def test_map_metadata_minus1_present_with_cover(self, tmp_path):
        """-map_metadata -1 must be present even when a cover image is added."""
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)  # minimal JPEG magic

        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result(), cover)

        pairs = list(zip(cmd, cmd[1:], strict=False))
        assert ("-map_metadata:g", "-1") in pairs, (
            "-map_metadata -1 missing in cover-art variant of the command."
        )


# ---------------------------------------------------------------------------
# Tests: chapter preservation
# ---------------------------------------------------------------------------

class TestChapterPreservation:
    """-map_chapters 0 must appear to copy chapter markers after the clear."""

    def test_chapter_copy_present(self, tmp_path):
        """-map_chapters 0 appears to copy chapters from input."""
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())

        pairs = list(zip(cmd, cmd[1:], strict=False))
        assert ("-map_chapters", "0") in pairs, (
            "-map_chapters 0 is missing; combined M4Bs will lose embedded "
            "chapter markers after embed_metadata is called."
        )

    def test_chapter_copy_after_clear(self, tmp_path):
        """-map_chapters 0 must come AFTER -map_metadata -1."""
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())

        clear_idx = next(
            (i for i in range(len(cmd) - 1) if cmd[i] == "-map_metadata:g" and cmd[i + 1] == "-1"),
            None,
        )
        chapter_idx = next(
            (i for i in range(len(cmd) - 1) if cmd[i] == "-map_chapters" and cmd[i + 1] == "0"),
            None,
        )
        assert clear_idx is not None, "-map_metadata -1 not found"
        assert chapter_idx is not None, "-map_chapters 0 not found"
        assert clear_idx < chapter_idx, (
            "-map_chapters 0 must come after -map_metadata -1 in the command "
            "for readability; both must be present."
        )

    def test_no_map_metadata_0c_present(self, tmp_path):
        """The broken -map_metadata 0:c pair must NOT be in the command.

        ffmpeg hard-fails with "Invalid chapter index 0" on chapterless
        inputs when given -map_metadata 0:c, which made embed_metadata raise
        ConversionError for every single-file M4B without chapters.
        """
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())

        pairs = list(zip(cmd, cmd[1:], strict=False))
        assert ("-map_metadata", "0:c") not in pairs, (
            "-map_metadata 0:c found — this breaks ffmpeg on chapterless "
            "inputs; use -map_chapters 0 instead."
        )


# ---------------------------------------------------------------------------
# Tests: correct metadata values still written
# ---------------------------------------------------------------------------

class TestMetadataValuesWritten:
    """After clearing, the explicit tags we care about must still be written."""

    def _flag_value(self, cmd: list[str], key: str) -> str | None:
        """Extract the value for a -metadata key=value flag."""
        for tok in cmd:
            if tok.startswith(f"{key}="):
                return tok[len(key) + 1:]
        return None

    def test_title_written(self, tmp_path):
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())
        assert self._flag_value(cmd, "title") == "Brisingr"

    def test_artist_written(self, tmp_path):
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())
        assert self._flag_value(cmd, "artist") == "Christopher Paolini"

    def test_album_artist_written(self, tmp_path):
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())
        assert self._flag_value(cmd, "album_artist") == "Christopher Paolini"

    def test_series_written_to_grouping(self, tmp_path):
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())
        grouping = self._flag_value(cmd, "grouping")
        assert grouping is not None and "Inheritance Cycle" in grouping

    def test_no_series_no_grouping(self, tmp_path):
        result = _make_result(series=None, series_index=None)
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", result)
        assert self._flag_value(cmd, "grouping") is None

    def test_stream_copy_flag_present(self, tmp_path):
        """'-c copy' (or '-c:a copy') must be present to avoid re-encoding."""
        cmd = _build_cmd(tmp_path / "in.m4b", tmp_path / "out.m4b", _make_result())
        assert "-c" in cmd or "-c:a" in cmd, "Audio copy flag missing"


# ---------------------------------------------------------------------------
# Tests: regression for exact Brisingr scenario
# ---------------------------------------------------------------------------

class TestBrisingrScenario:
    """Regression test for the exact production failure.

    File: 'Inheritance Cycle 3 - Brisingr.m4b'
    Original tags: ©ART='Brisingr', ©alb='Inheritance Cycle 3'
    Expected Calibre path: Books/Paolini, Christopher/Brisingr (100)/...
    Actual (broken) path:  Books/Brisingr/Inheritance Cycle 3 (100)/...

    Without -map_metadata -1, calibredb reads the first occurrence of ©ART
    and ©alb from the stale original file, producing the wrong directory.
    """

    def test_map_metadata_minus1_prevents_stale_artist(self, tmp_path):
        """With -map_metadata -1 in the command, stale ©ART cannot survive."""
        result = _make_result(title="Brisingr", author="Christopher Paolini")
        cmd = _build_cmd(tmp_path / "Inheritance Cycle 3 - Brisingr.m4b",
                         tmp_path / "out.m4b", result)

        pairs = list(zip(cmd, cmd[1:], strict=False))
        # The presence of -map_metadata -1 guarantees no stale atoms from the
        # source file reach the output — this is the specific mechanism that
        # prevented calibredb from picking up ©ART='Brisingr' as the author.
        assert ("-map_metadata:g", "-1") in pairs

    def test_correct_artist_written_in_brisingr_scenario(self, tmp_path):
        """artist= tag must be 'Christopher Paolini', not 'Brisingr'."""
        result = _make_result(title="Brisingr", author="Christopher Paolini")
        cmd = _build_cmd(tmp_path / "Inheritance Cycle 3 - Brisingr.m4b",
                         tmp_path / "out.m4b", result)

        artist_val = next(
            (tok[len("artist="):] for tok in cmd if tok.startswith("artist=")),
            None,
        )
        assert artist_val == "Christopher Paolini", (
            f"artist tag should be 'Christopher Paolini', got {artist_val!r}"
        )
