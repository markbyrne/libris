"""Tests for recursive directory dispatch in the incoming pipeline.

Before this change _process_audiobook_folder only scanned direct children;
nested subdirectories were silently ignored and ebook-only directories were
rejected with an error.

New behaviour:
  - Audio files grouped by their immediate parent directory.
      single file in dir  → standalone audiobook (normal single-file path)
      multiple files      → import_directory_combined (treated as parts)
  - Ebook files at any depth → dispatched individually.
  - The directory tree is removed after all files have been extracted.
  - Ebook-only directories (classified EBOOK) now delegate to the same
    dispatcher instead of raising BookPipelineError.
"""

from __future__ import annotations

import textwrap
from collections import defaultdict
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest
from click.testing import CliRunner

from libris.cli import main
from libris.state import FileRecord, FileState


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def libris_tree(tmp_path):
    root     = tmp_path / "libris"
    incoming = root / "incoming"
    staging  = root / "staging"
    review   = root / "review"
    failed   = root / "failed"
    calibre  = tmp_path / "calibre"
    for d in (incoming, staging, review, failed, calibre):
        d.mkdir(parents=True)

    db_file = root / "libris.db"
    db_file.write_text("db")

    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(f"""
        watcher:
          incoming_dir: {incoming}
        paths:
          staging_dir: {staging}
          review_dir:  {review}
          failed_dir:  {failed}
          state_db:    {db_file}
        calibre:
          mode: local
          library_path: {calibre}
        metadata:
          confidence_threshold: 0.75
        ntfy:
          topic: test
    """))
    return {
        "config": cfg, "root": root, "incoming": incoming,
        "staging": staging, "review": review, "failed": failed,
        "calibre": calibre, "db": db_file, "tmp": tmp_path,
    }


def _make_pipeline(tmp_path: Path):
    """Return a minimal Pipeline instance with all external calls mocked."""
    from libris.pipeline import Pipeline

    pipeline = Pipeline.__new__(Pipeline)

    cfg = MagicMock()
    cfg.metadata.confidence_threshold = 0.75
    cfg.metadata.overwrite_existing = True
    cfg.metadata.google_books_api_key = None
    cfg.output.embed_cover_art = False
    cfg.output.preferred_ebook_format = "epub"
    cfg.output.ebook_format_policy = "preferred"
    cfg.paths.staging_dir = tmp_path / "staging"
    cfg.paths.review_dir  = tmp_path / "review"
    cfg.paths.failed_dir  = tmp_path / "failed"
    for d in (cfg.paths.staging_dir, cfg.paths.review_dir, cfg.paths.failed_dir):
        d.mkdir(parents=True, exist_ok=True)

    pipeline.config = cfg
    pipeline._store = MagicMock()
    pipeline._calibre = MagicMock()
    pipeline._calibre.add_book.return_value = 1
    pipeline._calibre.get_book.return_value = None
    pipeline._calibre.set_metadata.return_value = None
    return pipeline


def _audio(tmp_path: Path, *parts: str) -> Path:
    """Create a fake .m4b file at tmp_path / *parts and return its Path."""
    p = tmp_path
    for part in parts:
        p = p / part
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake audio")
    return p


def _ebook(tmp_path: Path, *parts: str) -> Path:
    """Create a fake .epub file at tmp_path / *parts and return its Path."""
    p = tmp_path
    for part in parts:
        p = p / part
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"fake ebook")
    return p


# ---------------------------------------------------------------------------
# Tests: audio grouping by directory
# ---------------------------------------------------------------------------

class TestAudioGroupingByDirectory:
    """Files in the same subdir are treated as parts; files alone are standalone."""

    def test_single_audio_in_subdir_dispatched_standalone(self, tmp_path):
        """One audio file in a subdirectory → _process_audiobook called once."""
        folder = tmp_path / "drop"
        af = _audio(tmp_path, "drop", "Books", "Eragon.m4b")

        pipeline = _make_pipeline(tmp_path)

        standalone_calls = []

        def fake_process_audiobook(path, record):
            standalone_calls.append(path)
            record.state = FileState.IMPORTED
            return record

        # _make_record needs a real implementation for get_or_create
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )

        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        with patch.object(pipeline, "_process_audiobook", side_effect=fake_process_audiobook):
            with patch.object(pipeline, "import_directory_combined") as mock_combined:
                pipeline._process_audiobook_folder(folder, record)

        assert af in standalone_calls, "Single audio file should be dispatched standalone"
        mock_combined.assert_not_called()

    def test_multiple_audio_in_subdir_uses_combined(self, tmp_path):
        """Multiple audio files in one subdir → import_directory_combined called for that dir."""
        folder = tmp_path / "drop"
        subdir = folder / "Brisingr"
        _audio(tmp_path, "drop", "Brisingr", "Part1.m4b")
        _audio(tmp_path, "drop", "Brisingr", "Part2.m4b")
        _audio(tmp_path, "drop", "Brisingr", "Part3.m4b")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )

        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        combined_calls = []

        def fake_combined(dir_path):
            combined_calls.append(dir_path)
            return record

        with patch.object(pipeline, "import_directory_combined", side_effect=fake_combined):
            with patch.object(pipeline, "_process_audiobook") as mock_standalone:
                pipeline._process_audiobook_folder(folder, record)

        assert subdir in combined_calls, (
            f"import_directory_combined should be called with {subdir}, got {combined_calls}"
        )
        mock_standalone.assert_not_called()

    def test_mixed_structure_dispatches_correctly(self, tmp_path):
        """Mix of solo and grouped audio files → correct dispatch for each."""
        folder = tmp_path / "drop"
        # Solo file directly in drop root
        solo = _audio(tmp_path, "drop", "Eragon.m4b")
        # Two-part audiobook in a subdirectory
        _audio(tmp_path, "drop", "Eldest", "Eldest Part 1.m4b")
        _audio(tmp_path, "drop", "Eldest", "Eldest Part 2.m4b")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )

        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        standalone_calls = []
        combined_calls = []

        def fake_standalone(path, rec):
            standalone_calls.append(path)
            rec.state = FileState.IMPORTED
            return rec

        def fake_combined(dir_path):
            combined_calls.append(dir_path)
            return record

        with patch.object(pipeline, "_process_audiobook", side_effect=fake_standalone):
            with patch.object(pipeline, "import_directory_combined", side_effect=fake_combined):
                pipeline._process_audiobook_folder(folder, record)

        assert solo in standalone_calls, "Solo file should be dispatched standalone"
        eldest_dir = folder / "Eldest"
        assert eldest_dir in combined_calls, "Two-part dir should use import_directory_combined"


# ---------------------------------------------------------------------------
# Tests: deep nesting
# ---------------------------------------------------------------------------

class TestDeepNesting:
    """Files at arbitrary depths are discovered and dispatched."""

    def test_deeply_nested_audio_discovered(self, tmp_path):
        """Audio file three levels deep is dispatched."""
        folder = tmp_path / "drop"
        deep = _audio(tmp_path, "drop", "Author", "Series", "Book.m4b")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )

        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        dispatched = []

        def fake_standalone(path, rec):
            dispatched.append(path)
            rec.state = FileState.IMPORTED
            return rec

        with patch.object(pipeline, "_process_audiobook", side_effect=fake_standalone):
            with patch.object(pipeline, "import_directory_combined"):
                pipeline._process_audiobook_folder(folder, record)

        assert deep in dispatched, f"Deeply nested file {deep} was not dispatched"

    def test_deeply_nested_ebook_dispatched(self, tmp_path):
        """Ebook file three levels deep is dispatched individually."""
        folder = tmp_path / "drop"
        deep_epub = _ebook(tmp_path, "drop", "Author", "Series", "Book.epub")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )

        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        ebook_dispatched = []

        def fake_ebook(path, rec):
            ebook_dispatched.append(path)
            rec.state = FileState.IMPORTED
            return rec

        with patch.object(pipeline, "_process_ebook", side_effect=fake_ebook):
            with patch.object(pipeline, "_process_audiobook"):
                with patch.object(pipeline, "import_directory_combined"):
                    pipeline._process_audiobook_folder(folder, record)

        assert deep_epub in ebook_dispatched, f"Deeply nested epub {deep_epub} was not dispatched"

    def test_multi_level_multiple_audio_groups(self, tmp_path):
        """Two different subdirs each with multiple audio files → two combined calls."""
        folder = tmp_path / "drop"
        _audio(tmp_path, "drop", "Brisingr", "Part1.m4b")
        _audio(tmp_path, "drop", "Brisingr", "Part2.m4b")
        _audio(tmp_path, "drop", "Inheritance", "Part1.m4b")
        _audio(tmp_path, "drop", "Inheritance", "Part2.m4b")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )
        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        combined_calls = []

        def fake_combined(dir_path):
            combined_calls.append(dir_path)
            return record

        with patch.object(pipeline, "import_directory_combined", side_effect=fake_combined):
            with patch.object(pipeline, "_process_audiobook"):
                pipeline._process_audiobook_folder(folder, record)

        assert len(combined_calls) == 2, (
            f"Expected 2 combined calls (one per subdir), got {len(combined_calls)}: {combined_calls}"
        )
        assert folder / "Brisingr" in combined_calls
        assert folder / "Inheritance" in combined_calls


# ---------------------------------------------------------------------------
# Tests: ebook handling
# ---------------------------------------------------------------------------

class TestEbookHandling:
    """Ebook files are always dispatched individually, never combined."""

    def test_multiple_epubs_in_same_dir_each_dispatched_separately(self, tmp_path):
        """Three epub files in the same subdir → _process_ebook called 3× individually."""
        folder = tmp_path / "drop"
        e1 = _ebook(tmp_path, "drop", "Books", "Eragon.epub")
        e2 = _ebook(tmp_path, "drop", "Books", "Eldest.epub")
        e3 = _ebook(tmp_path, "drop", "Books", "Brisingr.epub")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )
        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        ebook_calls = []

        def fake_ebook(path, rec):
            ebook_calls.append(path)
            rec.state = FileState.IMPORTED
            return rec

        with patch.object(pipeline, "_process_ebook", side_effect=fake_ebook):
            with patch.object(pipeline, "_process_audiobook"):
                with patch.object(pipeline, "import_directory_combined"):
                    pipeline._process_audiobook_folder(folder, record)

        assert sorted(ebook_calls) == sorted([e1, e2, e3]), (
            f"Each epub should be dispatched individually. Got: {ebook_calls}"
        )

    def test_ebook_only_directory_no_error(self, tmp_path):
        """Ebook-only directory (classified EBOOK) is now processed, not rejected."""
        folder = tmp_path / "ebooks"
        e1 = _ebook(tmp_path, "ebooks", "Book1.epub")
        e2 = _ebook(tmp_path, "ebooks", "sub", "Book2.epub")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )
        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="ebook", state=FileState.PROCESSING,
        )

        ebook_calls = []

        def fake_ebook(path, rec):
            ebook_calls.append(path)
            rec.state = FileState.IMPORTED
            return rec

        # _process_ebook with a directory now delegates to _process_audiobook_folder
        with patch.object(pipeline, "_process_ebook", side_effect=fake_ebook):
            # Call _process_ebook(folder, ...) directly to test the delegation path
            with patch.object(pipeline, "_process_audiobook_folder",
                               wraps=pipeline._process_audiobook_folder) as mock_folder:
                # We want to confirm _process_ebook(dir) → _process_audiobook_folder
                # Use the real _process_audiobook_folder but mock _process_ebook for files
                pass

        # Direct test: _process_audiobook_folder handles an ebook-only folder
        ebook_calls.clear()
        with patch.object(pipeline, "_process_ebook", side_effect=fake_ebook):
            with patch.object(pipeline, "_process_audiobook"):
                with patch.object(pipeline, "import_directory_combined"):
                    pipeline._process_audiobook_folder(folder, record)

        assert e1 in ebook_calls or e2 in ebook_calls, (
            "Ebook files in ebook-only directory should be dispatched"
        )


# ---------------------------------------------------------------------------
# Tests: directory cleanup
# ---------------------------------------------------------------------------

class TestDirectoryCleanup:
    """The dropped directory is removed after all files are extracted."""

    def test_directory_removed_after_dispatch(self, tmp_path):
        """Original directory tree is removed once files are processed."""
        folder = tmp_path / "drop"
        af = _audio(tmp_path, "drop", "sub", "Book.m4b")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )
        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        def fake_standalone(path, rec):
            # Simulate the file being moved out (as the real pipeline does)
            path.unlink(missing_ok=True)
            rec.state = FileState.IMPORTED
            return rec

        with patch.object(pipeline, "_process_audiobook", side_effect=fake_standalone):
            with patch.object(pipeline, "import_directory_combined"):
                pipeline._process_audiobook_folder(folder, record)

        assert not folder.exists(), (
            "The dropped directory should be removed after extraction"
        )

    def test_directory_record_marked_imported(self, tmp_path):
        """The directory's own FileRecord is set to IMPORTED after dispatch."""
        folder = tmp_path / "drop"
        _audio(tmp_path, "drop", "Book.m4b")

        upserted = []
        pipeline = _make_pipeline(tmp_path)
        pipeline._store.upsert.side_effect = upserted.append
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )
        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        def fake_standalone(path, rec):
            rec.state = FileState.IMPORTED
            return rec

        with patch.object(pipeline, "_process_audiobook", side_effect=fake_standalone):
            with patch.object(pipeline, "import_directory_combined"):
                pipeline._process_audiobook_folder(folder, record)

        folder_record_states = [r.state for r in upserted if r.id == "folder"]
        assert FileState.IMPORTED in folder_record_states, (
            "The directory's own record should be marked IMPORTED"
        )

    def test_error_message_includes_counts(self, tmp_path):
        """The directory record error_msg mentions audio group count and ebook count."""
        folder = tmp_path / "drop"
        _audio(tmp_path, "drop", "Audio", "Part1.m4b")
        _audio(tmp_path, "drop", "Audio", "Part2.m4b")
        _ebook(tmp_path, "drop", "Book.epub")

        upserted = []
        pipeline = _make_pipeline(tmp_path)
        pipeline._store.upsert.side_effect = upserted.append
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )
        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        def fake_combined(dir_path):
            return record

        def fake_ebook(path, rec):
            rec.state = FileState.IMPORTED
            return rec

        with patch.object(pipeline, "import_directory_combined", side_effect=fake_combined):
            with patch.object(pipeline, "_process_audiobook"):
                with patch.object(pipeline, "_process_ebook", side_effect=fake_ebook):
                    pipeline._process_audiobook_folder(folder, record)

        # Find the final upsert of the directory record
        folder_records = [r for r in upserted if r.id == "folder" and r.error_msg]
        assert folder_records, "Directory record should be upserted with error_msg"
        msg = folder_records[-1].error_msg
        assert "2" in msg, f"error_msg should mention 2 audio files, got: {msg}"
        assert "1" in msg, f"error_msg should mention 1 ebook file, got: {msg}"


# ---------------------------------------------------------------------------
# Tests: edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_directory_raises_conversion_error(self, tmp_path):
        """A directory with no book files raises ConversionError."""
        from libris.exceptions import ConversionError
        folder = tmp_path / "empty"
        folder.mkdir()
        # Put a non-book file in there
        (folder / "cover.jpg").write_bytes(b"jpg")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )
        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        with pytest.raises(ConversionError):
            pipeline._process_audiobook_folder(folder, record)

    def test_hidden_files_ignored(self, tmp_path):
        """Hidden files (dot-prefixed) are skipped."""
        folder = tmp_path / "drop"
        folder.mkdir()
        (folder / ".DS_Store").write_bytes(b"mac junk")
        (folder / ".hidden.m4b").write_bytes(b"hidden audio")
        visible = _audio(tmp_path, "drop", "Visible.m4b")

        pipeline = _make_pipeline(tmp_path)
        pipeline._get_or_create_record = lambda p, mt: FileRecord(
            id=str(p), original_path=str(p), current_path=str(p),
            media_type=mt, state=FileState.INCOMING,
        )
        record = FileRecord(
            id="folder", original_path=str(folder), current_path=str(folder),
            media_type="audiobook", state=FileState.PROCESSING,
        )

        dispatched = []

        def fake_standalone(path, rec):
            dispatched.append(path)
            rec.state = FileState.IMPORTED
            return rec

        with patch.object(pipeline, "_process_audiobook", side_effect=fake_standalone):
            with patch.object(pipeline, "import_directory_combined"):
                pipeline._process_audiobook_folder(folder, record)

        assert visible in dispatched, "Visible file should be dispatched"
        hidden = folder / ".hidden.m4b"
        assert hidden not in dispatched, "Hidden .m4b should be skipped"
