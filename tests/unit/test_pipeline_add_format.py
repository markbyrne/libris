"""Tests for add_format guard — audio formats must not be passed to calibredb add_format.

Regression for: spurious 'force_import_add_format_failed: e-book file must have an
extension' warning when importing an audiobook (M4B) where a matching ebook (EPUB)
already exists in Calibre.  calibredb add_format is an ebook-only command; audio
formats should create a separate Calibre entry via add_book instead.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from libris.classifier import MediaType
from libris.state import FileRecord, FileState

# ---------------------------------------------------------------------------
# Minimal pipeline factory
# ---------------------------------------------------------------------------

def _make_pipeline(*, calibre_formats: set[str], add_book_id: int = 99):
    """Return a bare-minimum Pipeline instance with a mocked calibre backend.

    calibre_formats: what get_formats() returns for the existing duplicate book.
    add_book_id: the book ID that add_book() returns.
    """
    from libris.pipeline import Pipeline

    mock_calibre = MagicMock()
    mock_calibre.search.return_value = [42]          # one duplicate always found
    mock_calibre.get_formats.return_value = calibre_formats
    mock_calibre.add_book.return_value = add_book_id

    cfg = MagicMock()
    cfg.metadata.mock_mode = False
    cfg.metadata.duplicate_action = "skip"           # no --overwrite
    cfg.output.embed_cover_art = False

    pipeline = Pipeline.__new__(Pipeline)
    pipeline._calibre = mock_calibre
    pipeline.config = cfg
    pipeline._store = MagicMock()
    pipeline._notifier = MagicMock()

    return pipeline, mock_calibre


def _fake_record(path: Path) -> FileRecord:
    return FileRecord(
        id="deadbeef",
        original_path=str(path),
        current_path=str(path),
        media_type="audiobook",
        state=FileState.INCOMING,
    )


# ---------------------------------------------------------------------------
# _handle_duplicate — add_format guard
# ---------------------------------------------------------------------------

class TestHandleDuplicateAudioGuard:
    """_handle_duplicate must not call add_format for audio formats."""

    def test_m4b_incoming_epub_existing_skips_add_format(self, tmp_path):
        """M4B incoming + EPUB in Calibre → add_format never called."""
        pipeline, mock_calibre = _make_pipeline(calibre_formats={"epub"})

        m4b = tmp_path / "project_hail_mary.m4b"
        m4b.write_bytes(b"fake")

        record = _fake_record(m4b)
        result = MagicMock()
        result.title = "Project Hail Mary"
        result.author = "Andy Weir"
        result.cover_path = None

        # Returns None → caller (add_book) creates a separate audiobook entry
        ret = pipeline._handle_duplicate(record, result, m4b)
        assert ret is None, f"expected None but got state={ret.state if ret else None}"
        mock_calibre.add_format.assert_not_called()

    def test_mp3_incoming_epub_existing_skips_add_format(self, tmp_path):
        """Any audio format incoming + EPUB in Calibre → add_format never called."""
        pipeline, mock_calibre = _make_pipeline(calibre_formats={"epub"})

        mp3 = tmp_path / "book.mp3"
        mp3.write_bytes(b"fake")

        record = _fake_record(mp3)
        result = MagicMock()
        result.title = "Some Book"
        result.author = "Some Author"
        result.cover_path = None

        ret = pipeline._handle_duplicate(record, result, mp3)
        assert ret is None, f"expected None but got state={ret.state if ret else None}"
        mock_calibre.add_format.assert_not_called()

    def test_mobi_incoming_epub_existing_calls_add_format(self, tmp_path):
        """MOBI incoming + EPUB existing → add_format IS called (ebook format merge)."""
        pipeline, mock_calibre = _make_pipeline(calibre_formats={"epub"})

        mobi = tmp_path / "book.mobi"
        mobi.write_bytes(b"fake")

        record = _fake_record(mobi)
        result = MagicMock()
        result.title = "Some Book"
        result.author = "Some Author"
        result.cover_path = None

        pipeline._handle_duplicate(record, result, mobi)
        mock_calibre.add_format.assert_called_once()

    def test_m4b_incoming_m4b_existing_skips_add_format_goes_to_dup_action(self, tmp_path):
        """M4B incoming + M4B already in Calibre → not 'different format', dup_action applies."""
        pipeline, mock_calibre = _make_pipeline(calibre_formats={"m4b"})

        m4b = tmp_path / "project_hail_mary.m4b"
        m4b.write_bytes(b"fake")

        record = _fake_record(m4b)
        result = MagicMock()
        result.title = "Project Hail Mary"
        result.author = "Andy Weir"
        result.cover_path = None

        # M4B already in Calibre — same format, duplicate_action="skip" → handled as dup
        ret = pipeline._handle_duplicate(record, result, m4b)
        assert ret is not None, "same-format audio duplicate should be handled (not None)"
        # duplicate_action="skip" maps to IMPORTED with a "Duplicate" error_msg
        assert "Duplicate" in (ret.error_msg or ""), f"unexpected error_msg: {ret.error_msg!r}"
        mock_calibre.add_format.assert_not_called()  # "skip" never calls add_format


# ---------------------------------------------------------------------------
# force_import — add_format guard
# ---------------------------------------------------------------------------

class TestForceImportAudioGuard:
    """force_import must not emit force_import_add_format_failed for audio formats."""

    def _make_force_import_pipeline(self, tmp_path, *, existing_formats: set[str]):
        from libris.pipeline import Pipeline

        mock_calibre = MagicMock()
        mock_calibre.search.return_value = [42]
        mock_calibre.get_formats.return_value = existing_formats
        mock_calibre.add_book.return_value = 77

        cfg = MagicMock()
        cfg.metadata.mock_mode = False
        cfg.metadata.duplicate_action = "skip"
        cfg.output.embed_cover_art = False

        mock_store = MagicMock()
        _fake_record(tmp_path / "placeholder")
        mock_store.get_by_current_path.return_value = None
        mock_store.get_by_original_path.return_value = None

        mock_classifier = MagicMock()
        mock_classifier.classify.return_value = MediaType.AUDIOBOOK

        pipeline = Pipeline.__new__(Pipeline)
        pipeline._calibre = mock_calibre
        pipeline.config = cfg
        pipeline._store = mock_store
        pipeline._classifier = mock_classifier
        pipeline._notifier = MagicMock()

        return pipeline, mock_calibre

    def test_m4b_with_epub_duplicate_no_add_format_called(self, tmp_path):
        """force_import of M4B with existing EPUB duplicate must not call add_format."""
        pipeline, mock_calibre = self._make_force_import_pipeline(
            tmp_path, existing_formats={"epub"}
        )

        m4b = tmp_path / "project_hail_mary.m4b"
        m4b.write_bytes(b"fake m4b")

        result = MagicMock()
        result.title = "Project Hail Mary"
        result.author = "Andy Weir"
        result.confidence = 0.95
        result.year = "2021"
        result.publisher = "Audible"
        result.isbn = None
        result.best = MagicMock()
        result.best.candidate.cover_url = None
        result.best.candidate.authors = ["Andy Weir"]
        result.cover_path = None

        with patch("libris.pipeline.audio_tag"), \
             patch.object(pipeline, "_get_or_create_record", return_value=_fake_record(m4b)), \
             patch.object(pipeline, "_mark_imported", return_value=_fake_record(m4b)):
            pipeline.force_import(m4b, result)

        mock_calibre.add_format.assert_not_called()
        mock_calibre.add_book.assert_called_once()
        assert mock_calibre.add_book.call_args.args[0] == m4b
        assert mock_calibre.add_book.call_args.kwargs == {
            "title": "Project Hail Mary",
            "authors": "Andy Weir",
        }
