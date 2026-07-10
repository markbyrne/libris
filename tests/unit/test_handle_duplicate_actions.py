"""Tests for Pipeline._handle_duplicate (libris/pipeline.py), covering the
branches test_pipeline_add_format.py's TestHandleDuplicateAudioGuard doesn't:
the mock_mode/no-dup-ids short circuits, get_formats() raising, the
format-merge success/failure paths, and all three duplicate_action values
("import", "skip", "review") on a same-format duplicate.

Follows test_pipeline_add_format.py's pattern of building a bare Pipeline via
Pipeline.__new__ with a MagicMock calibre backend/store/notifier, rather than
going through __init__ or process_file — _handle_duplicate is self-contained
given (record, result, file_path).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from libris.metadata.base import BookCandidate, ScoredCandidate
from libris.state import FileRecord, FileState


def _make_pipeline(tmp_path: Path, *, duplicate_action: str = "review", mock_mode: bool = False):
    from libris.pipeline import Pipeline

    mock_calibre = MagicMock()
    mock_calibre.search.return_value = [42]
    mock_calibre.get_formats.return_value = {"epub"}

    cfg = MagicMock()
    cfg.metadata.mock_mode = mock_mode
    cfg.metadata.duplicate_action = duplicate_action
    cfg.paths.review_dir = tmp_path / "review"

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
        media_type="ebook",
        state=FileState.INCOMING,
    )


def _make_result(cover_path: Path | None = None) -> MagicMock:
    """A MagicMock MetadataResult whose .best is a REAL ScoredCandidate.

    The review-action branch of _handle_duplicate calls
    _serialize_candidate(result.best), which json.dumps()s
    result.best.candidate's fields -- a MagicMock .best would fail there, so
    .best must be a real ScoredCandidate/BookCandidate pair.
    """
    result = MagicMock()
    result.title = "Dune"
    result.author = "Frank Herbert"
    result.confidence = 0.9
    result.year = "1965"
    result.publisher = "Ace"
    result.isbn = None
    result.cover_path = cover_path
    result.best = ScoredCandidate(
        candidate=BookCandidate(title="Dune", authors=["Frank Herbert"], source="mock"),
        confidence=0.9,
        score_breakdown={},
    )
    return result


# ---------------------------------------------------------------------------
# Short-circuits
# ---------------------------------------------------------------------------

class TestShortCircuits:
    def test_mock_mode_returns_none_without_searching(self, tmp_path):
        pipeline, mock_calibre = _make_pipeline(tmp_path, mock_mode=True)
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        record = _fake_record(epub)

        ret = pipeline._handle_duplicate(record, _make_result(), epub)

        assert ret is None
        mock_calibre.search.assert_not_called()

    def test_no_duplicates_found_returns_none(self, tmp_path):
        pipeline, mock_calibre = _make_pipeline(tmp_path)
        mock_calibre.search.return_value = []
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        record = _fake_record(epub)

        assert pipeline._handle_duplicate(record, _make_result(), epub) is None


# ---------------------------------------------------------------------------
# get_formats() raising must not crash the pipeline
# ---------------------------------------------------------------------------

class TestGetFormatsException:
    def test_exception_treated_as_new_format_and_merged(self, tmp_path):
        """get_formats() raising -> existing_formats={} -> incoming epub
        looks like a 'new' format relative to the (unknown) duplicate, so
        the format-merge path runs rather than crashing."""
        pipeline, mock_calibre = _make_pipeline(tmp_path)
        mock_calibre.get_formats.side_effect = RuntimeError("calibredb list boom")
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        record = _fake_record(epub)

        ret = pipeline._handle_duplicate(record, _make_result(), epub)

        assert ret is not None
        assert ret.state == FileState.IMPORTED
        mock_calibre.add_format.assert_called_once_with(42, epub)


# ---------------------------------------------------------------------------
# Format-merge (different format from what's already in Calibre)
# ---------------------------------------------------------------------------

class TestFormatMerge:
    def test_success_merges_and_cleans_up(self, tmp_path):
        pipeline, mock_calibre = _make_pipeline(tmp_path)
        mock_calibre.get_formats.return_value = {"m4b"}  # incoming is epub -> different format
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"\xff\xd8")
        record = _fake_record(epub)

        ret = pipeline._handle_duplicate(record, _make_result(cover_path=cover), epub)

        assert ret is not None
        assert ret.state == FileState.IMPORTED
        assert ret.calibre_book_id == 42
        assert "Added EPUB format" in ret.error_msg
        mock_calibre.add_format.assert_called_once_with(42, epub)
        mock_calibre.set_cover.assert_called_once_with(42, cover)
        mock_calibre.set_metadata.assert_called_once()
        assert not epub.exists(), "source file must be unlinked after add_format merge"
        assert not cover.exists(), "temp cover file must be cleaned up"

    def test_add_format_exception_falls_through_to_duplicate_action(self, tmp_path):
        """add_format() raising inside the merge branch must fall through to
        the normal duplicate_action handling rather than propagating."""
        pipeline, mock_calibre = _make_pipeline(tmp_path, duplicate_action="skip")
        mock_calibre.get_formats.return_value = {"m4b"}
        mock_calibre.add_format.side_effect = RuntimeError("calibredb boom")
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        record = _fake_record(epub)

        ret = pipeline._handle_duplicate(record, _make_result(), epub)

        assert ret is not None
        assert ret.state == FileState.IMPORTED
        assert "Duplicate" in ret.error_msg  # fell through to skip's dup_msg wording

    def test_audio_format_skips_merge_returns_none(self, tmp_path):
        """Audio incoming vs ebook existing: add_format is ebook-only, so the
        caller must fall back to add_book (a separate Calibre entry)."""
        pipeline, mock_calibre = _make_pipeline(tmp_path)
        mock_calibre.get_formats.return_value = {"epub"}
        m4b = tmp_path / "book.m4b"
        m4b.write_bytes(b"x")
        record = _fake_record(m4b)

        ret = pipeline._handle_duplicate(record, _make_result(), m4b)

        assert ret is None
        mock_calibre.add_format.assert_not_called()


# ---------------------------------------------------------------------------
# Same format already in Calibre — duplicate_action dispatch
# ---------------------------------------------------------------------------

class TestDuplicateActionImport:
    def test_success_replaces_format(self, tmp_path):
        pipeline, mock_calibre = _make_pipeline(tmp_path, duplicate_action="import")
        mock_calibre.get_formats.return_value = {"epub"}  # same format as incoming
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        record = _fake_record(epub)

        ret = pipeline._handle_duplicate(record, _make_result(), epub)

        assert ret.state == FileState.IMPORTED
        assert "Replaced EPUB format" in ret.error_msg
        mock_calibre.add_format.assert_called_once_with(42, epub)

    def test_exception_falls_through_to_review_default(self, tmp_path):
        """action=='import' whose add_format raises has no other branch to
        catch it (the trailing code isn't gated on action=='review') — it
        falls through to the same review-move code the default case uses."""
        pipeline, mock_calibre = _make_pipeline(tmp_path, duplicate_action="import")
        mock_calibre.get_formats.return_value = {"epub"}
        mock_calibre.add_format.side_effect = RuntimeError("calibredb boom")
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        record = _fake_record(epub)

        ret = pipeline._handle_duplicate(record, _make_result(), epub)

        assert ret.state == FileState.REVIEW
        dest = cfg_review_dest(pipeline, "book.epub")
        assert dest.exists()
        pipeline._notifier.send_review_alert.assert_called_once()


def cfg_review_dest(pipeline, filename: str) -> Path:
    return pipeline.config.paths.review_dir / filename


class TestDuplicateActionSkip:
    def test_marks_imported_and_deletes_file(self, tmp_path):
        pipeline, mock_calibre = _make_pipeline(tmp_path, duplicate_action="skip")
        mock_calibre.get_formats.return_value = {"epub"}
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"\xff\xd8")
        record = _fake_record(epub)

        ret = pipeline._handle_duplicate(record, _make_result(cover_path=cover), epub)

        assert ret.state == FileState.IMPORTED
        assert "Duplicate" in ret.error_msg
        assert not epub.exists()
        assert not cover.exists()
        mock_calibre.add_format.assert_not_called()


class TestDuplicateActionReview:
    def test_moves_to_review_dir_and_notifies(self, tmp_path):
        pipeline, mock_calibre = _make_pipeline(tmp_path, duplicate_action="review")
        mock_calibre.get_formats.return_value = {"epub"}
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        record = _fake_record(epub)

        ret = pipeline._handle_duplicate(record, _make_result(), epub)

        assert ret.state == FileState.REVIEW
        assert ret.current_path == str(pipeline.config.paths.review_dir / "book.epub")
        assert (pipeline.config.paths.review_dir / "book.epub").exists()
        assert not epub.exists()
        pipeline._notifier.send_review_alert.assert_called_once()
