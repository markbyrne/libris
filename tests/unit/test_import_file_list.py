"""Tests for Pipeline.import_file_list — the explicit-file-list import seam.

Phase 1 of the API-driven-import plan: callers (e.g. a future HTTP endpoint,
or Librarr) can hand the pipeline an explicit list of files instead of
relying on watcher/directory discovery. Covers:

  1. Single ebook via import_file_list with a pre-registered directive —
     imported with directive metadata, resolve_metadata never called.
  2. Multi-part audio list with MISMATCHED stems + explicit group_key —
     combines into ONE m4b import; record survives keyed on parts[0].
  3. Default-path regression: _handle_pending_part WITHOUT group_key still
     groups by stem (existing daemon behaviour, untouched).
  4. Hardlink preservation: importing a hardlinked file leaves its twin
     intact (only the supplied link is removed).
  5. Empty list / mixed ebook+audio n>1 both raise ValueError.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from libris.config import (
    CalibreConfig,
    Config,
    MetadataConfig,
    NtfyConfig,
    OutputConfig,
    PathsConfig,
    WatcherConfig,
)
from libris.pipeline import Pipeline
from libris.state import FileState


def _make_config(tmp_path: Path) -> Config:
    return Config(
        watcher=WatcherConfig(incoming_dir=tmp_path / "incoming"),
        paths=PathsConfig(
            staging_dir=tmp_path / "staging",
            review_dir=tmp_path / "review",
            failed_dir=tmp_path / "failed",
            state_db=tmp_path / "state.db",
        ),
        calibre=CalibreConfig(mode="local", library_db_path=tmp_path / "calibre"),
        metadata=MetadataConfig(confidence_threshold=0.75, mock_mode=True),
        output=OutputConfig(
            preferred_ebook_format="epub",
            preferred_audio_format="m4b",
            embed_cover_art=False,
        ),
        ntfy=NtfyConfig(topic="test", enabled=False),
    )


def _directive_metadata(title="Dune", author="Frank Herbert", isbn="9780441013593") -> str:
    return json.dumps({
        "title": title,
        "authors": [author],
        "isbn_13": isbn,
        "isbn_10": None,
        "published_year": 1965,
        "publisher": "Ace",
        "description": None,
        "language": "en",
        "series": None,
        "series_index": None,
        "cover_url": None,
        "categories": [],
        "source": "librarr",
        "confidence": 0.95,
        "score_breakdown": {},
    })


def _make_pipeline(tmp_path: Path) -> Pipeline:
    cfg = _make_config(tmp_path)
    cfg.watcher.incoming_dir.mkdir(parents=True, exist_ok=True)
    pipeline = Pipeline(cfg)
    pipeline._calibre = MagicMock()
    pipeline._calibre.search.return_value = []
    pipeline._calibre.add_book.return_value = 42
    return pipeline


# ---------------------------------------------------------------------------
# 1. Single ebook, pre-registered directive
# ---------------------------------------------------------------------------

class TestSingleFileImport:
    def test_single_ebook_with_directive_skips_resolve_metadata(self, tmp_path):
        pipeline = _make_pipeline(tmp_path)

        # File lives OUTSIDE incoming_dir — e.g. another tool's landing folder.
        landing = tmp_path / "librarr-landing"
        landing.mkdir()
        book = landing / "dune.epub"
        book.write_bytes(b"fake epub")

        pipeline._store.add_directive(
            "dir1", "dune.epub", _directive_metadata(), source="librarr", confidence=0.95,
        )

        with patch("libris.pipeline.resolve_metadata") as mock_resolve:
            record = pipeline.import_file_list([book])

        mock_resolve.assert_not_called()
        assert record.matched_title == "Dune"
        assert record.matched_author == "Frank Herbert"
        assert record.state == FileState.IMPORTED
        pipeline._calibre.add_book.assert_called_once()
        # Input file removed per existing single-file import semantics.
        assert not book.exists()


# ---------------------------------------------------------------------------
# 2. Multi-part audio, mismatched stems, explicit group_key
# ---------------------------------------------------------------------------

class TestMultiPartMismatchedStems:
    def test_mismatched_stems_combine_with_explicit_group_key(self, tmp_path):
        pipeline = _make_pipeline(tmp_path)

        landing = tmp_path / "librarr-landing"
        landing.mkdir()
        # Deliberately mismatched stems — stem-derived grouping would fail.
        part1 = landing / "Book Part 1.m4b"
        part2 = landing / "totally-different-name.m4b"
        part1.write_bytes(b"fake audio part 1")
        part2.write_bytes(b"fake audio part 2")

        pipeline._store.add_directive(
            "dir1", "Book Part 1.m4b", _directive_metadata(title="Combined Book"),
            source="librarr", confidence=0.9,
        )

        def fake_combine_parts(part_files, output_path):
            # Simulate ffmpeg producing the combined file.
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(b"combined audio")

        # Capture the id assigned to parts[0]'s record the moment it's
        # created — this is what a caller polling by basename(paths[0])
        # would need to look up, before the source file disappears and
        # FileRecord.make_id's mtime component becomes unrecoverable.
        from libris.state import FileRecord
        parts0_id = FileRecord.make_id(part1)

        with (
            patch("libris.pipeline.audio_conv.combine_parts", side_effect=fake_combine_parts) as mock_combine,
            patch("libris.pipeline.audio_conv.convert_to_m4b") as mock_convert,
            patch("libris.pipeline.audio_tag.embed_metadata"),
            patch("libris.pipeline.resolve_metadata") as mock_resolve,
        ):
            record = pipeline.import_file_list([part1, part2])

        # Both parts already .m4b — no per-part conversion needed.
        mock_convert.assert_not_called()
        mock_combine.assert_called_once()
        mock_resolve.assert_not_called()

        assert record.state == FileState.IMPORTED
        assert record.matched_title == "Combined Book"
        pipeline._calibre.add_book.assert_called_once()

        # Surviving record is keyed on parts[0] (verified against
        # pipeline.py:_combine_pending_group -> primary = parts[0]): the
        # record returned by import_file_list IS the record created for
        # part1, just mutated in place through combine + import.
        assert record.id == parts0_id

    def test_default_path_still_groups_by_stem(self, tmp_path):
        """Regression: _handle_pending_part called WITHOUT group_key still
        derives the group key from the stem (existing daemon behaviour)."""
        pipeline = _make_pipeline(tmp_path)
        from libris.cleaner import clean_query, strip_part_marker
        from libris.state import FileRecord

        path = tmp_path / "incoming" / "My Book (part 1 of 2).m4b"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake audio")

        record = FileRecord(
            id=FileRecord.make_id(path),
            original_path=str(path),
            current_path=str(path),
            media_type="audiobook",
            state=FileState.INCOMING,
        )
        pipeline._store.upsert(record)

        result = pipeline._handle_pending_part(path, record, part_num=1, total_parts=2)

        stripped_stem = strip_part_marker(path.stem)
        expected_key = (clean_query(stripped_stem) or stripped_stem).lower().strip()
        assert result.part_group_key == expected_key


# ---------------------------------------------------------------------------
# 3. Hardlink preservation
# ---------------------------------------------------------------------------

class TestHardlinkPreservation:
    def test_hardlink_twin_survives_import(self, tmp_path):
        pipeline = _make_pipeline(tmp_path)

        landing = tmp_path / "librarr-landing"
        landing.mkdir()
        original = landing / "dune.epub"
        original.write_bytes(b"fake epub")

        # The "other tool" keeps its own copy via a hardlink; libris is
        # handed the link, not the original.
        twin = landing / "dune-twin.epub"
        os.link(original, twin)

        pipeline._store.add_directive(
            "dir1", "dune.epub", _directive_metadata(), source="librarr", confidence=0.95,
        )

        with patch("libris.pipeline.resolve_metadata") as mock_resolve:
            record = pipeline.import_file_list([original])

        mock_resolve.assert_not_called()
        assert record.state == FileState.IMPORTED
        assert not original.exists()
        assert twin.exists()
        assert twin.read_bytes() == b"fake epub"


# ---------------------------------------------------------------------------
# 4. Error cases
# ---------------------------------------------------------------------------

class TestImportFileListErrors:
    def test_empty_list_raises_value_error(self, tmp_path):
        pipeline = _make_pipeline(tmp_path)
        with pytest.raises(ValueError):
            pipeline.import_file_list([])

    def test_mixed_ebook_and_audio_raises_value_error(self, tmp_path):
        pipeline = _make_pipeline(tmp_path)
        landing = tmp_path / "librarr-landing"
        landing.mkdir()
        ebook = landing / "book.epub"
        audio = landing / "book part 1.m4b"
        ebook.write_bytes(b"fake epub")
        audio.write_bytes(b"fake audio")

        with pytest.raises(ValueError):
            pipeline.import_file_list([ebook, audio])
