"""Tests for the directive seam in Pipeline._process_ebook / _resolve_tag_and_import_audio.

Contract: if a directive exists for the file's ORIGINAL incoming basename,
the pipeline builds a pre-resolved MetadataResult from it, marks the
directive consumed, and NEVER calls resolve_metadata. Without a directive,
behaviour is unchanged (resolve_metadata IS called, as before).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _make_config(tmp_path: Path, ebook_format_policy: str = "all") -> Config:
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
            ebook_format_policy=ebook_format_policy,
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


# ---------------------------------------------------------------------------
# Ebook seam
# ---------------------------------------------------------------------------

class TestEbookDirectiveSeam:
    def test_directive_present_skips_resolve_metadata(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.watcher.incoming_dir.mkdir(parents=True, exist_ok=True)

        pipeline = Pipeline(cfg)
        pipeline._calibre = MagicMock()
        pipeline._calibre.search.return_value = []
        pipeline._calibre.add_book.return_value = 42

        book = cfg.watcher.incoming_dir / "dune.epub"
        book.write_bytes(b"fake epub")

        pipeline._store.add_directive(
            "dir1", "dune.epub", _directive_metadata(), source="librarr", confidence=0.95,
        )

        with patch("libris.pipeline.resolve_metadata") as mock_resolve:
            record = pipeline.process_file(book)

        mock_resolve.assert_not_called()
        assert record.matched_title == "Dune"
        assert record.matched_author == "Frank Herbert"
        assert record.state.value == "imported"
        pipeline._calibre.add_book.assert_called_once()

        # Directive marked consumed, but still matchable (crash-safety) —
        # find_directive intentionally ignores consumed_at.
        row = pipeline._store.find_directive("dune.epub")
        assert row is not None
        assert row["consumed_at"] is not None

    def test_no_directive_calls_resolve_metadata_unchanged(self, tmp_path):
        """Regression: without a directive, resolve_metadata IS called (old behaviour)."""
        cfg = _make_config(tmp_path)
        cfg.watcher.incoming_dir.mkdir(parents=True, exist_ok=True)

        pipeline = Pipeline(cfg)
        pipeline._calibre = MagicMock()
        pipeline._calibre.search.return_value = []
        pipeline._calibre.add_book.return_value = 42

        book = cfg.watcher.incoming_dir / "some_random_book.epub"
        book.write_bytes(b"fake epub")

        from libris.metadata.base import MetadataResult, SearchQuery
        fake_result = MetadataResult(
            query=SearchQuery(clean_title="some random book"),
            best=None,
            above_threshold=False,
        )
        with patch("libris.pipeline.resolve_metadata", return_value=fake_result) as mock_resolve:
            record = pipeline.process_file(book)

        mock_resolve.assert_called_once()
        assert record.state.value == "review"

    def test_directive_consumed_second_file_still_matches(self, tmp_path):
        """Behavior change (crash-safety): after a directive is consumed, a
        second identically-named file (e.g. a crash-triggered orphan
        reprocess, or genuinely a re-drop of the same filename) STILL
        matches the consumed directive and skips resolve_metadata.

        Directives are idempotent by filename+metadata, so re-matching a
        consumed directive is safe and is exactly what makes the pipeline
        crash-safe: if the process dies between marking a directive
        consumed and finishing the import, the next startup's
        orphan-reprocess must still find the same directive rather than
        falling back to weak Google/OpenLibrary resolution.
        """
        cfg = _make_config(tmp_path)
        cfg.watcher.incoming_dir.mkdir(parents=True, exist_ok=True)

        pipeline = Pipeline(cfg)
        pipeline._calibre = MagicMock()
        pipeline._calibre.search.return_value = []
        pipeline._calibre.add_book.return_value = 42

        book = cfg.watcher.incoming_dir / "dune.epub"
        book.write_bytes(b"fake epub 1")

        pipeline._store.add_directive(
            "dir1", "dune.epub", _directive_metadata(), source="librarr", confidence=0.95,
        )

        with patch("libris.pipeline.resolve_metadata") as mock_resolve:
            pipeline.process_file(book)
        mock_resolve.assert_not_called()

        # Successful import already deleted the source file. Second file,
        # same basename, different content/mtime → still matches the
        # (consumed) directive, still skips resolve_metadata.
        book2 = cfg.watcher.incoming_dir / "dune.epub"
        book2.write_bytes(b"fake epub 2 - different content and mtime")

        with patch("libris.pipeline.resolve_metadata") as mock_resolve2:
            record2 = pipeline.process_file(book2)
        mock_resolve2.assert_not_called()
        assert record2.matched_title == "Dune"
        assert record2.state.value == "imported"


# ---------------------------------------------------------------------------
# Audiobook seam
# ---------------------------------------------------------------------------

class TestAudiobookDirectiveSeam:
    def test_directive_present_skips_resolve_metadata(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.watcher.incoming_dir.mkdir(parents=True, exist_ok=True)

        pipeline = Pipeline(cfg)
        pipeline._calibre = MagicMock()
        pipeline._calibre.search.return_value = []
        pipeline._calibre.add_book.return_value = 7

        m4b = cfg.watcher.incoming_dir / "dune.m4b"
        m4b.write_bytes(b"fake m4b")

        pipeline._store.add_directive(
            "dir1", "dune.m4b", _directive_metadata(), source="librarr", confidence=0.9,
        )

        with patch("libris.pipeline.resolve_metadata") as mock_resolve, \
             patch("libris.pipeline.audio_tag") as mock_tag:
            record = pipeline.process_file(m4b)

        mock_resolve.assert_not_called()
        mock_tag.embed_metadata.assert_called_once()
        assert record.matched_title == "Dune"
        assert record.state.value == "imported"
        # Directive marked consumed, but still matchable (crash-safety) —
        # find_directive intentionally ignores consumed_at.
        row = pipeline._store.find_directive("dune.m4b")
        assert row is not None
        assert row["consumed_at"] is not None

    def test_no_directive_calls_resolve_metadata_unchanged(self, tmp_path):
        cfg = _make_config(tmp_path)
        cfg.watcher.incoming_dir.mkdir(parents=True, exist_ok=True)

        pipeline = Pipeline(cfg)
        pipeline._calibre = MagicMock()

        m4b = cfg.watcher.incoming_dir / "some_random_audiobook.m4b"
        m4b.write_bytes(b"fake m4b")

        from libris.metadata.base import MetadataResult, SearchQuery
        fake_result = MetadataResult(
            query=SearchQuery(clean_title="some random audiobook"),
            best=None,
            above_threshold=False,
        )
        with patch("libris.pipeline.resolve_metadata", return_value=fake_result) as mock_resolve:
            record = pipeline.process_file(m4b)

        mock_resolve.assert_called_once()
        assert record.state.value == "review"


# ---------------------------------------------------------------------------
# Real logging path — no mocking of `log`.
#
# Regression test for the live crash: log.info("pipeline.directive_match",
# extra={"filename": ...}) raised KeyError("Attempt to overwrite 'filename'
# in LogRecord") because `filename` is a reserved LogRecord attribute name.
# The existing seam tests above never caught this because they run at the
# default logging level (WARNING) with no handler attached to the
# `libris.pipeline` logger, so `Logger.info()` short-circuits on
# `isEnabledFor(INFO)` and never reaches `makeRecord()` — the `extra=` dict
# is never even inspected. caplog.at_level(INFO, "libris.pipeline") forces
# the logger's effective level down to INFO so the real makeRecord() path
# actually runs, the same as it does in production.
# ---------------------------------------------------------------------------

class TestDirectiveMatchLoggingIsReal:
    def test_directive_match_logs_without_crashing(self, tmp_path, caplog):
        """This test must NOT mock `log` in any way — it exists to prove the
        actual logging call in _check_directive is safe. Before the fix,
        this test crashed with KeyError("Attempt to overwrite 'filename' in
        LogRecord") the same way the production daemon did."""
        cfg = _make_config(tmp_path)
        cfg.watcher.incoming_dir.mkdir(parents=True, exist_ok=True)

        pipeline = Pipeline(cfg)
        pipeline._calibre = MagicMock()
        pipeline._calibre.search.return_value = []
        pipeline._calibre.add_book.return_value = 42

        book = cfg.watcher.incoming_dir / "dune.epub"
        book.write_bytes(b"fake epub")

        pipeline._store.add_directive(
            "dir1", "dune.epub", _directive_metadata(), source="librarr", confidence=0.95,
        )

        with caplog.at_level(logging.INFO, logger="libris.pipeline"):
            record = pipeline.process_file(book)

        assert record.state.value == "imported"
        messages = [r.message for r in caplog.records if r.name == "libris.pipeline"]
        assert any("pipeline.directive_match" in m for m in messages)
        # The reserved-key collision would have raised before this point —
        # reaching here at all is the regression check. Also confirm the
        # renamed field made it onto the record without colliding.
        directive_records = [r for r in caplog.records if r.getMessage() == "pipeline.directive_match"]
        assert len(directive_records) == 1
        assert directive_records[0].incoming_filename == "dune.epub"
