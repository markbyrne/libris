"""Tests for import-dir functionality.

Covers:
  1. extract_part recognises compact Disc/CD notation (DiscNN, CDnn, Disc01-NNN).
  2. strip_part_marker strips compact Disc/CD notation.
  3. Pipeline.import_directory_combined stages N parts with correct part_num /
     total_parts and triggers auto-combine on the last file.
  4. import_directory_combined raises ValueError when the directory has no
     audio files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, call, patch

import pytest

from libris.cleaner import extract_part, strip_part_marker
from libris.state import FileRecord, FileState


# ---------------------------------------------------------------------------
# 1. extract_part — compact Disc/CD patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    # No space between keyword and number
    ("Book01-Merchant of Death-Disc01-001",  (1, None)),
    ("Book01-Merchant of Death-Disc03-005",  (3, None)),
    ("Book01-Merchant of Death-CD02-001",    (2, None)),
    # Capital / mixed case
    ("SomeBook DISC10",                      (10, None)),
    ("SomeBook CD01",                        (1, None)),
    # Compact disc only, no track suffix
    ("Audiobook Disc05",                     (5, None)),
    # Existing patterns still work
    ("Brisingr (part 1 of 3)",               (1, 3)),
    ("Name of the Wind Disc 1 of 2",         (1, 2)),  # spaced form
    # Plain filename — no part
    ("D.J. MacHale-Book01-The Merchant of Death",  (None, None)),
])
def test_extract_part_compact_disc(raw, expected):
    assert extract_part(raw) == expected


# ---------------------------------------------------------------------------
# 2. strip_part_marker — compact Disc/CD patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("Book01-Merchant of Death-Disc01-001",  "Book01-Merchant of Death"),
    ("Book01-Merchant of Death-Disc10-010",  "Book01-Merchant of Death"),
    ("SomeBook CD02",                        "SomeBook"),
    # Existing patterns still work
    ("Brisingr (part 1 of 3)",               "Brisingr"),
    ("Eragon",                               "Eragon"),
])
def test_strip_part_marker_compact_disc(raw, expected):
    assert strip_part_marker(raw) == expected


# ---------------------------------------------------------------------------
# Helper — build a minimal FileRecord for mocking
# ---------------------------------------------------------------------------

def _make_record(path: Path, idx: int, state: FileState = FileState.INCOMING) -> FileRecord:
    raw = f"{path.resolve()}\x00{idx}"
    rec_id = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return FileRecord(
        id=rec_id,
        original_path=str(path),
        current_path=str(path),
        media_type="audiobook",
        state=state,
    )


# ---------------------------------------------------------------------------
# 3. import_directory_combined — stages N parts correctly
# ---------------------------------------------------------------------------

def test_import_directory_combined_calls_handle_pending_part_correctly(tmp_path):
    """Each audio file must be staged as part N of total_parts=N."""
    # Create a directory of 3 fake MP3 files
    audio_dir = tmp_path / "Book01-The Merchant of Death"
    audio_dir.mkdir()
    files = [
        audio_dir / "Book01-Disc01-001.mp3",
        audio_dir / "Book01-Disc01-002.mp3",
        audio_dir / "Book01-Disc02-001.mp3",
    ]
    for f in files:
        f.write_bytes(b"fake-audio")

    # Build a list of fake records to return from _get_or_create_record
    fake_records = [_make_record(f, i) for i, f in enumerate(files)]

    # The combined result returned from the last _handle_pending_part call
    combined_record = _make_record(files[-1], 99, FileState.IMPORTED)
    combined_record.matched_title = "The Merchant of Death"
    combined_record.matched_author = "D.J. MacHale"
    combined_record.confidence = 0.92

    from libris.config import (
        CalibreConfig, Config, MetadataConfig, NtfyConfig, OutputConfig,
        PathsConfig, WatcherConfig,
    )

    config = Config(
        watcher=WatcherConfig(incoming_dir=tmp_path / "incoming"),
        paths=PathsConfig(
            staging_dir=tmp_path / "staging",
            review_dir=tmp_path / "review",
            failed_dir=tmp_path / "failed",
            state_db=tmp_path / "state.db",
        ),
        calibre=CalibreConfig(mode="local", library_db_path=tmp_path / "calibre"),
        metadata=MetadataConfig(confidence_threshold=0.75, mock_mode=True),
        output=OutputConfig(preferred_ebook_format="epub", preferred_audio_format="m4b"),
        ntfy=NtfyConfig(topic="test"),
    )

    from libris.pipeline import Pipeline

    pipeline = Pipeline(config)

    # Track calls to _handle_pending_part
    staged_calls: List[tuple] = []

    def fake_handle_pending_part(path, record, part_num, total_parts):
        staged_calls.append((path.name, part_num, total_parts))
        record.part_num = part_num
        record.total_parts = total_parts
        record.state = FileState.PENDING_PARTS
        # Simulate combine firing on the last part
        if part_num == total_parts:
            combined_record.part_num = None
            combined_record.total_parts = None
            return combined_record
        return record

    with (
        patch.object(pipeline, "_get_or_create_record", side_effect=fake_records),
        patch.object(pipeline, "_handle_pending_part", side_effect=fake_handle_pending_part),
        patch("libris.audio.converter.find_audio_files", return_value=files),
    ):
        result = pipeline.import_directory_combined(audio_dir)

    # All three files must have been staged
    assert len(staged_calls) == 3

    # Part numbers must be 1, 2, 3 and total must be 3 for each
    assert staged_calls[0] == (files[0].name, 1, 3)
    assert staged_calls[1] == (files[1].name, 2, 3)
    assert staged_calls[2] == (files[2].name, 3, 3)

    # The returned record should be the combined one
    assert result is combined_record
    assert result.state == FileState.IMPORTED


# ---------------------------------------------------------------------------
# 4. import_directory_combined — raises ValueError on empty directory
# ---------------------------------------------------------------------------

def test_import_directory_combined_raises_on_empty_dir(tmp_path):
    """Should raise ValueError when no audio files exist in the directory."""
    empty_dir = tmp_path / "empty-audiobook"
    empty_dir.mkdir()

    from libris.config import (
        CalibreConfig, Config, MetadataConfig, NtfyConfig, OutputConfig,
        PathsConfig, WatcherConfig,
    )

    config = Config(
        watcher=WatcherConfig(incoming_dir=tmp_path / "incoming"),
        paths=PathsConfig(
            staging_dir=tmp_path / "staging",
            review_dir=tmp_path / "review",
            failed_dir=tmp_path / "failed",
            state_db=tmp_path / "state.db",
        ),
        calibre=CalibreConfig(mode="local", library_db_path=tmp_path / "calibre"),
        metadata=MetadataConfig(confidence_threshold=0.75, mock_mode=True),
        output=OutputConfig(preferred_ebook_format="epub", preferred_audio_format="m4b"),
        ntfy=NtfyConfig(topic="test"),
    )

    from libris.pipeline import Pipeline

    pipeline = Pipeline(config)

    with patch("libris.audio.converter.find_audio_files", return_value=[]):
        with pytest.raises(ValueError, match="No audio files found"):
            pipeline.import_directory_combined(empty_dir)
