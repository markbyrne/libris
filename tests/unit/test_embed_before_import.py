"""Tests for issue #50 — embed_metadata always uses overwrite=True before add_book.

Root cause: _resolve_tag_and_import_audio was passing
overwrite=self.config.metadata.overwrite_existing to embed_metadata.  When the
M4B already had embedded tags (e.g. preserved via ffmpeg stream-copy during
combine_parts) and overwrite_existing=False, embed_metadata would skip the tag
write.  Calibre then read the stale tags when building its directory structure,
creating the wrong author/title path (e.g. "Books/Brisingr/Inheritance Cycle 3 (100)/").

Fix: always pass overwrite=True for the pre-import embed call.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from libris.state import FileRecord, FileState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(overwrite_existing: bool = False, tmp_path: Path = None):
    """Return a minimal mock config with the given overwrite_existing flag."""
    cfg = MagicMock()
    cfg.metadata.overwrite_existing = overwrite_existing
    cfg.metadata.confidence_threshold = 0.75
    cfg.metadata.google_books_api_key = None
    cfg.output.embed_cover_art = False
    if tmp_path:
        cfg.paths.staging_dir = tmp_path / "staging"
        cfg.paths.review_dir = tmp_path / "review"
        cfg.paths.failed_dir = tmp_path / "failed"
        for d in (cfg.paths.staging_dir, cfg.paths.review_dir, cfg.paths.failed_dir):
            d.mkdir(parents=True, exist_ok=True)
    else:
        cfg.paths.staging_dir = Path("/staging")
        cfg.paths.review_dir = Path("/review")
        cfg.paths.failed_dir = Path("/failed")
    cfg.calibre.mode = "local"
    return cfg


def _make_mock_result(title: str = "Brisingr", author: str = "Christopher Paolini",
                      above_threshold: bool = True) -> MagicMock:
    """Return a mock MetadataResult with key properties set."""
    result = MagicMock()
    result.title = title
    result.author = author
    result.above_threshold = above_threshold
    result.confidence = 0.92
    result.cover_path = None
    result.best = MagicMock()
    result.best.score = 0.92
    result.best.candidate.authors = [author]
    return result


def _make_record(path: str = "/staging/book.m4b") -> FileRecord:
    return FileRecord(
        id="rec1",
        original_path=path,
        current_path=path,
        media_type="audiobook",
        state=FileState.PROCESSING,
    )


def _make_pipeline(cfg, tmp_path=None):
    """Construct a bare Pipeline instance (no __init__) with mocked calibre + store."""
    from libris.pipeline import Pipeline

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = cfg

    mock_cal = MagicMock()
    mock_cal.add_book.return_value = 42
    mock_cal.get_book.return_value = None
    mock_cal.set_metadata.return_value = None
    mock_cal.set_cover.return_value = None
    pipeline._calibre = mock_cal
    pipeline._store = MagicMock()
    return pipeline


# ---------------------------------------------------------------------------
# Tests: overwrite=True is always passed to embed_metadata before add_book
# ---------------------------------------------------------------------------

class TestEmbedAlwaysOverwritesBeforeImport:
    """_resolve_tag_and_import_audio must pass overwrite=True to embed_metadata
    regardless of the overwrite_existing config setting."""

    def _run_and_capture_embed_calls(self, overwrite_existing: bool, tmp_path: Path):
        """Run _resolve_tag_and_import_audio and return recorded embed_metadata calls."""
        m4b = tmp_path / "book.m4b"
        m4b.write_bytes(b"fake audio")

        cfg = _make_config(overwrite_existing=overwrite_existing, tmp_path=tmp_path)
        pipeline = _make_pipeline(cfg, tmp_path)
        record = _make_record(str(m4b))

        mock_result = _make_mock_result()
        embed_calls = []

        def fake_embed(path, res, overwrite=True, **kwargs):
            embed_calls.append({"path": path, "overwrite": overwrite})

        with patch("libris.pipeline.resolve_metadata", return_value=mock_result):
            with patch("libris.pipeline.audio_tag.embed_metadata", side_effect=fake_embed):
                with patch.object(pipeline, "_handle_duplicate", return_value=None):
                    pipeline._resolve_tag_and_import_audio(m4b, record, m4b)

        return embed_calls

    def test_overwrite_true_when_config_false(self, tmp_path):
        """When overwrite_existing=False in config, embed_metadata still receives overwrite=True."""
        calls = self._run_and_capture_embed_calls(overwrite_existing=False, tmp_path=tmp_path)
        assert calls, "embed_metadata was never called"
        assert calls[0]["overwrite"] is True, (
            f"Expected overwrite=True (config=False), got overwrite={calls[0]['overwrite']}. "
            "This would cause Calibre to create wrong directory structure from stale tags."
        )

    def test_overwrite_true_when_config_true(self, tmp_path):
        """When overwrite_existing=True in config, embed_metadata also receives overwrite=True."""
        calls = self._run_and_capture_embed_calls(overwrite_existing=True, tmp_path=tmp_path)
        assert calls, "embed_metadata was never called"
        assert calls[0]["overwrite"] is True

    def test_embed_called_before_add_book(self, tmp_path):
        """embed_metadata must be invoked before add_book (tag-then-import order)."""
        m4b = tmp_path / "book.m4b"
        m4b.write_bytes(b"fake audio")

        cfg = _make_config(overwrite_existing=False, tmp_path=tmp_path)
        pipeline = _make_pipeline(cfg, tmp_path)
        record = _make_record(str(m4b))
        mock_result = _make_mock_result()
        call_order = []

        def fake_embed(path, res, overwrite=True, **kwargs):
            call_order.append("embed")

        pipeline._calibre.add_book.side_effect = (
            lambda p, **kw: (call_order.append("add_book"), 42)[1]
        )

        with patch("libris.pipeline.resolve_metadata", return_value=mock_result):
            with patch("libris.pipeline.audio_tag.embed_metadata", side_effect=fake_embed):
                with patch.object(pipeline, "_handle_duplicate", return_value=None):
                    pipeline._resolve_tag_and_import_audio(m4b, record, m4b)

        assert "embed" in call_order, "embed_metadata was not called"
        assert "add_book" in call_order, "add_book was not called"
        embed_idx = call_order.index("embed")
        add_book_idx = call_order.index("add_book")
        assert embed_idx < add_book_idx, (
            f"embed_metadata ({embed_idx}) must come before add_book ({add_book_idx})"
        )

    def test_embed_not_called_when_below_threshold(self, tmp_path):
        """When confidence < threshold (above_threshold=False), embed is skipped entirely
        and the file goes to review — embed_metadata should not be called."""
        m4b = tmp_path / "book.m4b"
        m4b.write_bytes(b"fake audio")

        cfg = _make_config(overwrite_existing=False, tmp_path=tmp_path)
        pipeline = _make_pipeline(cfg, tmp_path)
        record = _make_record(str(m4b))
        mock_result = _make_mock_result(above_threshold=False)  # low confidence
        embed_calls = []

        review_record = _make_record(str(m4b))

        with patch("libris.pipeline.resolve_metadata", return_value=mock_result):
            with patch("libris.pipeline.audio_tag.embed_metadata",
                       side_effect=lambda p, r, **kw: embed_calls.append(kw.get("overwrite"))):
                with patch.object(pipeline, "_mark_review", return_value=review_record):
                    pipeline._resolve_tag_and_import_audio(m4b, record, m4b)

        assert embed_calls == [], (
            "embed_metadata should not be called for below-threshold results"
        )


# ---------------------------------------------------------------------------
# Tests: regression — directory structure follows resolved metadata
# ---------------------------------------------------------------------------

class TestDirectoryStructureFromMetadata:
    """Confirm the Calibre add_book call receives a properly tagged file."""

    def test_stale_tags_replaced_before_add_book(self, tmp_path):
        """Even with overwrite_existing=False, the pre-import embed uses overwrite=True
        so Calibre reads the resolved metadata (not stale source tags) when creating
        the book directory structure."""
        m4b = tmp_path / "Inheritance Cycle 3 - Brisingr.m4b"
        m4b.write_bytes(b"fake audio with stale tags")

        # User has overwrite_existing=False — but bug fix means we override this
        cfg = _make_config(overwrite_existing=False, tmp_path=tmp_path)
        pipeline = _make_pipeline(cfg, tmp_path)
        record = _make_record(str(m4b))

        # Resolved metadata: correct title/author
        mock_result = _make_mock_result(title="Brisingr", author="Christopher Paolini")
        embed_overwrite_values = []

        def capture_embed(path, res, overwrite=True, **kwargs):
            embed_overwrite_values.append(overwrite)

        with patch("libris.pipeline.resolve_metadata", return_value=mock_result):
            with patch("libris.pipeline.audio_tag.embed_metadata", side_effect=capture_embed):
                with patch.object(pipeline, "_handle_duplicate", return_value=None):
                    pipeline._resolve_tag_and_import_audio(m4b, record, m4b)

        assert embed_overwrite_values, "embed_metadata was not called at all"
        assert embed_overwrite_values[0] is True, (
            f"Pre-import embed used overwrite={embed_overwrite_values[0]}, expected True. "
            "Stale tags would reach Calibre and create wrong directory structure."
        )

    def test_add_book_called_with_m4b_path(self, tmp_path):
        """add_book receives the m4b_path — not some other path."""
        m4b = tmp_path / "book.m4b"
        m4b.write_bytes(b"fake")

        cfg = _make_config(overwrite_existing=False, tmp_path=tmp_path)
        pipeline = _make_pipeline(cfg, tmp_path)
        record = _make_record(str(m4b))
        mock_result = _make_mock_result()

        with patch("libris.pipeline.resolve_metadata", return_value=mock_result):
            with patch("libris.pipeline.audio_tag.embed_metadata"):
                with patch.object(pipeline, "_handle_duplicate", return_value=None):
                    pipeline._resolve_tag_and_import_audio(m4b, record, m4b)

        pipeline._calibre.add_book.assert_called_once()
        assert pipeline._calibre.add_book.call_args.args[0] == m4b
        # The resolved title/authors must ride along — they determine the
        # directory structure calibredb creates.
        assert pipeline._calibre.add_book.call_args.kwargs == {
            "title": "Brisingr",
            "authors": "Christopher Paolini",
        }

    def test_set_metadata_called_with_book_id_from_add_book(self, tmp_path):
        """set_metadata is invoked with the book_id returned by add_book."""
        m4b = tmp_path / "book.m4b"
        m4b.write_bytes(b"fake")

        cfg = _make_config(overwrite_existing=False, tmp_path=tmp_path)
        pipeline = _make_pipeline(cfg, tmp_path)
        pipeline._calibre.add_book.return_value = 55  # specific book_id
        record = _make_record(str(m4b))
        mock_result = _make_mock_result()

        with patch("libris.pipeline.resolve_metadata", return_value=mock_result):
            with patch("libris.pipeline.audio_tag.embed_metadata"):
                with patch.object(pipeline, "_handle_duplicate", return_value=None):
                    pipeline._resolve_tag_and_import_audio(m4b, record, m4b)

        pipeline._calibre.set_metadata.assert_called_once()
        call_args = pipeline._calibre.set_metadata.call_args
        assert call_args[0][0] == 55, (
            f"set_metadata called with book_id={call_args[0][0]}, expected 55 "
            "(the id returned by add_book)"
        )

    def test_record_calibre_book_id_set(self, tmp_path):
        """After import, record.calibre_book_id is set to the Calibre book id."""
        m4b = tmp_path / "book.m4b"
        m4b.write_bytes(b"fake")

        cfg = _make_config(overwrite_existing=False, tmp_path=tmp_path)
        pipeline = _make_pipeline(cfg, tmp_path)
        pipeline._calibre.add_book.return_value = 77
        record = _make_record(str(m4b))
        mock_result = _make_mock_result()

        with patch("libris.pipeline.resolve_metadata", return_value=mock_result):
            with patch("libris.pipeline.audio_tag.embed_metadata"):
                with patch.object(pipeline, "_handle_duplicate", return_value=None):
                    returned = pipeline._resolve_tag_and_import_audio(m4b, record, m4b)

        assert record.calibre_book_id == 77 or (
            returned is not None and returned.calibre_book_id == 77
        ), "calibre_book_id was not set to 77 after import"
