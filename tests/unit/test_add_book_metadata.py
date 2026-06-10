"""Tests that resolved title/authors reach calibredb add (--title/--authors).

Root cause of the bug
---------------------
``calibredb add`` does NOT read embedded M4B audio tags.  It derives the
book's directory ({author_sort}/{title} ({id})/) by parsing the FILENAME as
"{title} - {author}".  So "Book01-Merchant of Death.m4b" (no " - "
separator) imported to Books/Unknown/Book01-Merchant of Death (102)/, and
"Inheritance Cycle 3 - Brisingr.m4b" imported with author "Brisingr".

In split-library mode the later set_metadata could not repair this: the
physical files had already been relocated to book_file_path, so calibredb's
directory rename under library_db_path was a silent no-op.

Fix
---
Pass the resolved metadata to ``calibredb add`` via --title/--authors
(built by ``_add_book_args``), so the directory is correct at creation and
set_metadata never needs to rename it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from libris.state import FileRecord, FileState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(
    title: str = "Pendragon: The Merchant Of Death",
    authors: list[str] | None = None,
    best: bool = True,
) -> MagicMock:
    result = MagicMock()
    result.title = title
    result.author = ", ".join(authors) if authors else ""
    result.above_threshold = True
    result.confidence = 0.92
    result.cover_path = None
    result.year = None
    result.publisher = None
    result.isbn = None
    if best:
        result.best = MagicMock()
        result.best.candidate.authors = authors or []
        result.best.candidate.cover_url = None
    else:
        result.best = None
    return result


def _make_pipeline(tmp_path: Path):
    from libris.pipeline import Pipeline

    cfg = MagicMock()
    cfg.metadata.confidence_threshold = 0.75
    cfg.output.embed_cover_art = False
    cfg.output.preferred_ebook_format = "epub"
    cfg.output.ebook_format_policy = "all"
    cfg.paths.staging_dir = tmp_path / "staging"
    cfg.paths.review_dir = tmp_path / "review"
    cfg.paths.failed_dir = tmp_path / "failed"
    for d in (cfg.paths.staging_dir, cfg.paths.review_dir, cfg.paths.failed_dir):
        d.mkdir(parents=True, exist_ok=True)
    cfg.calibre.mode = "local"

    pipeline = Pipeline.__new__(Pipeline)
    pipeline.config = cfg
    pipeline._calibre = MagicMock()
    pipeline._calibre.add_book.return_value = 102
    pipeline._store = MagicMock()
    return pipeline


def _make_record(path: str) -> FileRecord:
    return FileRecord(
        id="rec1",
        original_path=path,
        current_path=path,
        media_type="audiobook",
        state=FileState.PROCESSING,
    )


# ---------------------------------------------------------------------------
# _add_book_args unit behaviour
# ---------------------------------------------------------------------------

class TestAddBookArgs:
    def test_title_and_single_author(self):
        from libris.pipeline import _add_book_args

        args = _add_book_args(_make_result(authors=["D.J. MacHale"]))
        assert args == {
            "title": "Pendragon: The Merchant Of Death",
            "authors": "D.J. MacHale",
        }

    def test_multi_author_joined_with_ampersand(self):
        """Never join with ', ' — Calibre parses that as one inverted name."""
        from libris.pipeline import _add_book_args

        args = _add_book_args(
            _make_result(authors=["Terry Pratchett", "Neil Gaiman"])
        )
        assert args["authors"] == "Terry Pratchett & Neil Gaiman"

    def test_no_best_candidate_passes_no_authors(self):
        from libris.pipeline import _add_book_args

        args = _add_book_args(_make_result(authors=None, best=False))
        assert args["authors"] is None
        assert args["title"] == "Pendragon: The Merchant Of Death"

    def test_empty_authors_list_passes_none(self):
        from libris.pipeline import _add_book_args

        args = _add_book_args(_make_result(authors=[]))
        assert args["authors"] is None

    def test_empty_title_passes_none(self):
        from libris.pipeline import _add_book_args

        args = _add_book_args(_make_result(title="", authors=["A"]))
        assert args["title"] is None


# ---------------------------------------------------------------------------
# Pipeline call sites pass the args through
# ---------------------------------------------------------------------------

class TestEbookImportPassesMetadata:
    def test_ebook_add_book_receives_title_and_authors(self, tmp_path):
        """_process_ebook passes resolved title/authors to add_book.

        Ebooks have no pre-add tag-embed step at all, so --title/--authors
        is the ONLY thing standing between a junk filename and a wrong
        directory.
        """
        pipeline = _make_pipeline(tmp_path)
        epub = tmp_path / "merchant_of_death.epub"
        epub.write_bytes(b"epub content")
        record = _make_record(str(epub))

        result = _make_result(authors=["D.J. MacHale"])

        with patch("libris.pipeline.resolve_metadata", return_value=result), \
             patch.object(pipeline, "_handle_duplicate", return_value=None), \
             patch.object(pipeline, "_mark_imported", return_value=record):
            pipeline._process_ebook(epub, record)

        pipeline._calibre.add_book.assert_called_once()
        call = pipeline._calibre.add_book.call_args
        assert call.args[0] == epub
        assert call.kwargs == {
            "title": "Pendragon: The Merchant Of Death",
            "authors": "D.J. MacHale",
        }


class TestAudioImportPassesMetadata:
    def test_audio_add_book_receives_title_and_authors(self, tmp_path):
        pipeline = _make_pipeline(tmp_path)
        m4b = tmp_path / "Book01-Merchant of Death.m4b"
        m4b.write_bytes(b"audio")
        record = _make_record(str(m4b))

        result = _make_result(authors=["D.J. MacHale"])

        with patch("libris.pipeline.resolve_metadata", return_value=result), \
             patch("libris.pipeline.audio_tag.embed_metadata"), \
             patch.object(pipeline, "_handle_duplicate", return_value=None), \
             patch.object(pipeline, "_mark_imported", return_value=record):
            pipeline._resolve_tag_and_import_audio(m4b, record, m4b)

        pipeline._calibre.add_book.assert_called_once()
        call = pipeline._calibre.add_book.call_args
        assert call.args[0] == m4b
        assert call.kwargs == {
            "title": "Pendragon: The Merchant Of Death",
            "authors": "D.J. MacHale",
        }
