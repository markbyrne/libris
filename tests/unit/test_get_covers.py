"""Tests for cover.jpg handling: always-saved covers and the get-covers command.

Covers are saved to the book directory (via calibredb set_cover) for every
import with a matched cover — independent of output.embed_cover_art, which
now gates only embedding art inside the audio file itself.

`libris get-covers` backfills cover.jpg for books already in the library:
state-store matched cover URL first, fresh metadata lookup as fallback.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from libris.cli import main

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, *, split: bool = False, mode: str = "local") -> dict:
    root = tmp_path / "libris"
    dirs = {name: root / name for name in ("incoming", "staging", "review", "failed")}
    library = tmp_path / "library"
    book_files = tmp_path / "books" if split else library
    for d in (*dirs.values(), library, book_files):
        d.mkdir(parents=True, exist_ok=True)
    db_file = root / "libris.db"
    db_file.write_text("fake db")
    book_path_line = f"book_file_path: {book_files}" if split else ""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(textwrap.dedent(f"""
        watcher:
          incoming_dir: {dirs['incoming']}
        paths:
          staging_dir: {dirs['staging']}
          review_dir:  {dirs['review']}
          failed_dir:  {dirs['failed']}
          state_db:    {db_file}
        calibre:
          mode: {mode}
          library_path: {library}
          {book_path_line}
        metadata:
          confidence_threshold: 0.75
        ntfy:
          topic: test
    """))
    return {"config": cfg, "library": library, "book_files": book_files}


def _book_on_disk(tree: dict, book_id: int, title: str, *, cover: bool) -> dict:
    """Create a book dir (with file, optionally cover.jpg); return list_books entry."""
    rel = Path(f"Author {book_id}/{title} ({book_id})")
    book_dir = tree["book_files"] / rel
    book_dir.mkdir(parents=True, exist_ok=True)
    (book_dir / f"{title}.epub").write_bytes(b"epub")
    if cover:
        (book_dir / "cover.jpg").write_bytes(b"jpeg")
    return {
        "id": book_id,
        "title": title,
        "authors": [f"Author {book_id}"],
        "formats": ["epub"],
        "format_paths": [str(tree["library"] / rel / f"{title}.epub")],
    }


def _invoke(tree: dict, calibre: MagicMock, store: MagicMock, args: list[str], **kw):
    runner = CliRunner()
    with patch("libris.cli.get_calibre", return_value=calibre), \
         patch("libris.cli._open_store", return_value=store):
        return runner.invoke(main, args, env={"LIBRIS_CONFIG": str(tree["config"])}, **kw)


def _store_with_record(cover_url: str | None):
    store = MagicMock()
    if cover_url is None:
        store.get_by_calibre_id.return_value = None
    else:
        record = MagicMock()
        record.matched_cover_url = cover_url
        store.get_by_calibre_id.return_value = record
    return store


# ---------------------------------------------------------------------------
# get-covers command
# ---------------------------------------------------------------------------

class TestGetCovers:
    def test_missing_cover_fetched_from_recorded_url(self, tmp_path):
        tree = _write_config(tmp_path)
        book = _book_on_disk(tree, 5, "No Cover Book", cover=False)
        calibre = MagicMock()
        calibre.list_books.return_value = [book]
        store = _store_with_record("http://covers/5.jpg")

        downloaded = tmp_path / "tmp_cover.jpg"
        downloaded.write_bytes(b"img")

        with patch("libris.cli._download_cover", return_value=downloaded) as dl:
            result = _invoke(tree, calibre, store, ["get-covers"])

        assert result.exit_code == 0, result.output
        dl.assert_called_once()
        assert dl.call_args.args[0] == "http://covers/5.jpg"
        calibre.set_cover.assert_called_once_with(5, downloaded)
        assert "1 cover(s) fetched" in result.output
        assert not downloaded.exists(), "temp cover must be cleaned up"

    def test_existing_cover_skipped(self, tmp_path):
        tree = _write_config(tmp_path)
        book = _book_on_disk(tree, 3, "Covered Book", cover=True)
        calibre = MagicMock()
        calibre.list_books.return_value = [book]
        store = _store_with_record(None)

        result = _invoke(tree, calibre, store, ["get-covers"])

        assert result.exit_code == 0, result.output
        assert "All 1 book(s) have a cover.jpg" in result.output
        calibre.set_cover.assert_not_called()

    def test_dry_run_lists_without_fetching(self, tmp_path):
        tree = _write_config(tmp_path)
        book = _book_on_disk(tree, 7, "Dry Book", cover=False)
        calibre = MagicMock()
        calibre.list_books.return_value = [book]
        store = _store_with_record("http://covers/7.jpg")

        with patch("libris.cli._download_cover") as dl:
            result = _invoke(tree, calibre, store, ["get-covers", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "Would fetch 1 cover(s)" in result.output
        dl.assert_not_called()
        calibre.set_cover.assert_not_called()

    def test_fallback_to_metadata_lookup(self, tmp_path):
        """No state record → resolve_metadata provides the cover."""
        tree = _write_config(tmp_path)
        book = _book_on_disk(tree, 9, "Lookup Book", cover=False)
        calibre = MagicMock()
        calibre.list_books.return_value = [book]
        store = _store_with_record(None)

        resolved_cover = tmp_path / "resolved_cover.jpg"
        resolved_cover.write_bytes(b"img")
        fake_result = MagicMock()
        fake_result.cover_path = resolved_cover

        with patch("libris.cli.resolve_metadata", return_value=fake_result) as rm:
            result = _invoke(tree, calibre, store, ["get-covers"])

        assert result.exit_code == 0, result.output
        assert rm.call_args.args[0] == "Lookup Book - Author 9"
        calibre.set_cover.assert_called_once_with(9, resolved_cover)

    def test_no_cover_found_reported(self, tmp_path):
        tree = _write_config(tmp_path)
        book = _book_on_disk(tree, 11, "Obscure Book", cover=False)
        calibre = MagicMock()
        calibre.list_books.return_value = [book]
        store = _store_with_record(None)

        fake_result = MagicMock()
        fake_result.cover_path = None

        with patch("libris.cli.resolve_metadata", return_value=fake_result):
            result = _invoke(tree, calibre, store, ["get-covers"])

        assert result.exit_code == 0, result.output
        assert "no cover found" in result.output
        calibre.set_cover.assert_not_called()

    def test_split_mode_checks_book_root(self, tmp_path):
        """Cover existence must be checked where files really live."""
        tree = _write_config(tmp_path, split=True)
        book = _book_on_disk(tree, 4, "Split Book", cover=True)  # under books/
        calibre = MagicMock()
        calibre.list_books.return_value = [book]
        store = _store_with_record(None)

        result = _invoke(tree, calibre, store, ["get-covers"])

        assert result.exit_code == 0, result.output
        assert "All 1 book(s) have a cover.jpg" in result.output

    def test_docker_mode_refused(self, tmp_path):
        tree = _write_config(tmp_path, mode="docker")
        result = _invoke(tree, MagicMock(), MagicMock(), ["get-covers"])
        assert result.exit_code != 0
        assert "requires calibre.mode: local" in result.output


# ---------------------------------------------------------------------------
# Import path: cover always saved, embed flag only gates audio-file art
# ---------------------------------------------------------------------------

class TestCoverAlwaysSaved:
    def _make_pipeline(self, tmp_path, embed_cover_art: bool):
        from libris.pipeline import Pipeline

        cfg = MagicMock()
        cfg.metadata.confidence_threshold = 0.75
        cfg.output.embed_cover_art = embed_cover_art
        cfg.output.preferred_ebook_format = "epub"
        cfg.output.ebook_format_policy = "all"
        cfg.paths.staging_dir = tmp_path / "staging"
        cfg.paths.review_dir = tmp_path / "review"
        cfg.paths.failed_dir = tmp_path / "failed"
        for d in (cfg.paths.staging_dir, cfg.paths.review_dir, cfg.paths.failed_dir):
            d.mkdir(parents=True, exist_ok=True)
        cfg.calibre.reconnect_url = None

        pipeline = Pipeline.__new__(Pipeline)
        pipeline.config = cfg
        pipeline._calibre = MagicMock()
        pipeline._calibre.add_book.return_value = 1
        pipeline._store = MagicMock()
        return pipeline

    def _make_result(self, cover_path):
        result = MagicMock()
        result.title = "Book"
        result.author = "Author"
        result.above_threshold = True
        result.confidence = 0.9
        result.cover_path = cover_path
        result.best.candidate.authors = ["Author"]
        return result

    def _run_audio_import(self, pipeline, tmp_path, result):
        from libris.state import FileRecord, FileState

        m4b = tmp_path / "book.m4b"
        m4b.write_bytes(b"audio")
        record = FileRecord(
            id="r1", original_path=str(m4b), current_path=str(m4b),
            media_type="audiobook", state=FileState.PROCESSING,
        )
        with patch("libris.pipeline.resolve_metadata", return_value=result), \
             patch("libris.pipeline.audio_tag.embed_metadata") as embed, \
             patch.object(pipeline, "_handle_duplicate", return_value=None):
            pipeline._resolve_tag_and_import_audio(m4b, record, m4b)
        return embed

    def test_cover_saved_even_when_embed_disabled(self, tmp_path):
        pipeline = self._make_pipeline(tmp_path, embed_cover_art=False)
        cover = tmp_path / "cover_tmp.jpg"
        cover.write_bytes(b"img")
        result = self._make_result(cover)

        embed = self._run_audio_import(pipeline, tmp_path, result)

        pipeline._calibre.set_cover.assert_called_once_with(1, cover)
        # flag off → no art inside the audio file
        assert embed.call_args.kwargs.get("cover_path") is None

    def test_embed_flag_passes_cover_into_audio_file(self, tmp_path):
        pipeline = self._make_pipeline(tmp_path, embed_cover_art=True)
        cover = tmp_path / "cover_tmp.jpg"
        cover.write_bytes(b"img")
        result = self._make_result(cover)

        embed = self._run_audio_import(pipeline, tmp_path, result)

        pipeline._calibre.set_cover.assert_called_once_with(1, cover)
        assert embed.call_args.kwargs.get("cover_path") == cover
