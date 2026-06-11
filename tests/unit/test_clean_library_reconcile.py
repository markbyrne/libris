"""Tests for clean-library passes 3 & 4 — DB/filesystem reconciliation (Issue #58).

Pass 3 (missing files): Calibre entries whose format files no longer exist on
disk are removed after user confirmation (--yes skips the prompt).

Pass 4 (orphan files): book files found in the library tree that no Calibre
entry points to are moved to review/ with a REVIEW state record so the normal
rematch/accept flow can re-import them.

Both passes run only in calibre.mode: local — docker library paths live
inside the container and cannot be checked from the host.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from libris.cli import main
from libris.state import FileState

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _write_config(tmp_path: Path, *, split: bool = False, mode: str = "local") -> dict:
    """Build a libris dir tree + config; returns paths dict."""
    root = tmp_path / "libris"
    dirs = {name: root / name for name in ("incoming", "staging", "review", "failed")}
    library = tmp_path / "library"        # metadata.db location
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
    return {"config": cfg, "library": library, "book_files": book_files, **dirs}


def _book(book_id: int, title: str, library: Path, *filenames: str) -> dict:
    """A list_books entry whose format_paths sit under *library* (as calibredb reports)."""
    rel_dir = f"Author {book_id}/{title} ({book_id})"
    return {
        "id": book_id,
        "title": title,
        "authors": [f"Author {book_id}"],
        "formats": [Path(f).suffix.lstrip(".").lower() for f in filenames],
        "format_paths": [str(library / rel_dir / f) for f in filenames],
    }


def _seed_file(library_root: Path, book: dict, library: Path, index: int = 0) -> Path:
    """Create the physical file for *book*'s format under *library_root*."""
    rel = Path(book["format_paths"][index]).relative_to(library)
    real = library_root / rel
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"content")
    return real


def _invoke(cfg_path: Path, calibre: MagicMock, store: MagicMock, args: list[str], **kw):
    runner = CliRunner()
    with patch("libris.cli.get_calibre", return_value=calibre), \
         patch("libris.cli._open_store", return_value=store):
        return runner.invoke(main, args, env={"LIBRIS_CONFIG": str(cfg_path)}, **kw)


def _fake_calibre(books: list[dict]) -> MagicMock:
    calibre = MagicMock()
    calibre.list_books.return_value = books
    return calibre


# ---------------------------------------------------------------------------
# Pass 3 — missing files
# ---------------------------------------------------------------------------

class TestMissingFiles:
    def test_missing_entry_removed_with_yes(self, tmp_path):
        tree = _write_config(tmp_path)
        book = _book(7, "Gone Book", tree["library"], "Gone Book.epub")
        calibre = _fake_calibre([book])  # file never created on disk
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert "no files on disk" in result.output
        calibre.remove_book.assert_called_once_with(7)
        store.delete_by_calibre_id.assert_called_once_with(7)

    def test_missing_entry_kept_when_declined(self, tmp_path):
        tree = _write_config(tmp_path)
        book = _book(7, "Gone Book", tree["library"], "Gone Book.epub")
        calibre = _fake_calibre([book])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library"], input="n\n")

        assert result.exit_code == 0, result.output
        assert "Skipped" in result.output
        calibre.remove_book.assert_not_called()
        store.delete_by_calibre_id.assert_not_called()

    def test_dry_run_lists_without_prompt_or_removal(self, tmp_path):
        tree = _write_config(tmp_path)
        book = _book(7, "Gone Book", tree["library"], "Gone Book.epub")
        calibre = _fake_calibre([book])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "Would prompt to remove 1" in result.output
        calibre.remove_book.assert_not_called()
        store.delete_by_calibre_id.assert_not_called()

    def test_entry_with_file_on_disk_untouched(self, tmp_path):
        tree = _write_config(tmp_path)
        book = _book(3, "Live Book", tree["library"], "Live Book.epub")
        _seed_file(tree["book_files"], book, tree["library"])
        calibre = _fake_calibre([book])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert "All Calibre entries have their files on disk" in result.output
        calibre.remove_book.assert_not_called()

    def test_partially_missing_entry_left_with_warning(self, tmp_path):
        """One format gone, one present — entry must survive, with a warning."""
        tree = _write_config(tmp_path)
        book = _book(4, "Half Book", tree["library"], "Half Book.epub", "Half Book.m4b")
        _seed_file(tree["book_files"], book, tree["library"], index=0)  # epub only
        calibre = _fake_calibre([book])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert "missing format" in result.output
        assert "leaving entry" in result.output
        calibre.remove_book.assert_not_called()

    def test_split_mode_file_under_book_root_counts_as_present(self, tmp_path):
        """calibredb reports paths under library_db_path; in split mode the
        file really lives under book_file_path and must count as present."""
        tree = _write_config(tmp_path, split=True)
        book = _book(5, "Split Book", tree["library"], "Split Book.m4b")
        _seed_file(tree["book_files"], book, tree["library"])  # under books/, not library/
        calibre = _fake_calibre([book])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert "All Calibre entries have their files on disk" in result.output
        calibre.remove_book.assert_not_called()

    def test_split_mode_file_stranded_under_library_counts_as_present(self, tmp_path):
        """A crash between add and relocation leaves the file under
        library_db_path — that must not be treated as a missing entry."""
        tree = _write_config(tmp_path, split=True)
        book = _book(6, "Stranded Book", tree["library"], "Stranded Book.epub")
        _seed_file(tree["library"], book, tree["library"])  # under library/, not books/
        calibre = _fake_calibre([book])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        calibre.remove_book.assert_not_called()

    def test_formatless_entry_skipped(self, tmp_path):
        tree = _write_config(tmp_path)
        book = {"id": 9, "title": "Empty", "authors": ["A"], "formats": [], "format_paths": []}
        calibre = _fake_calibre([book])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        calibre.remove_book.assert_not_called()


# ---------------------------------------------------------------------------
# Pass 4 — orphan files
# ---------------------------------------------------------------------------

class TestOrphanFiles:
    def test_orphan_moved_to_review_with_record(self, tmp_path):
        tree = _write_config(tmp_path)
        orphan = tree["book_files"] / "Lost Author/Lost Book (99)/Lost Book.m4b"
        orphan.parent.mkdir(parents=True)
        orphan.write_bytes(b"audio")
        calibre = _fake_calibre([])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert "orphan" in result.output
        dest = tree["review"] / "Lost Book.m4b"
        assert dest.exists(), "orphan must be moved to review/"
        assert not orphan.exists()
        assert not orphan.parent.exists(), "emptied book dir must be cleaned up"

        record = store.upsert.call_args.args[0]
        assert record.state == FileState.REVIEW
        assert record.current_path == str(dest)
        assert record.media_type == "audiobook"

    def test_orphan_ebook_gets_ebook_media_type(self, tmp_path):
        tree = _write_config(tmp_path)
        orphan = tree["book_files"] / "stray.epub"
        orphan.write_bytes(b"epub")
        calibre = _fake_calibre([])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        record = store.upsert.call_args.args[0]
        assert record.media_type == "ebook"

    def test_orphan_dry_run_not_moved(self, tmp_path):
        tree = _write_config(tmp_path)
        orphan = tree["book_files"] / "stray.epub"
        orphan.write_bytes(b"epub")
        calibre = _fake_calibre([])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "Would move 1 orphan file(s)" in result.output
        assert orphan.exists()
        store.upsert.assert_not_called()

    def test_known_file_not_treated_as_orphan(self, tmp_path):
        tree = _write_config(tmp_path)
        book = _book(3, "Live Book", tree["library"], "Live Book.epub")
        real = _seed_file(tree["book_files"], book, tree["library"])
        calibre = _fake_calibre([book])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert "No orphan files found" in result.output
        assert real.exists()

    def test_hidden_dirs_skipped(self, tmp_path):
        """Files under .caltrash (Calibre's trash) must not be rescued."""
        tree = _write_config(tmp_path)
        trashed = tree["book_files"] / ".caltrash/b/1/Old Book.epub"
        trashed.parent.mkdir(parents=True)
        trashed.write_bytes(b"trash")
        calibre = _fake_calibre([])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert "No orphan files found" in result.output
        assert trashed.exists()

    def test_non_book_files_ignored(self, tmp_path):
        """cover.jpg / metadata.opf / metadata.db are library artefacts, not orphans."""
        tree = _write_config(tmp_path)
        for name in ("Author/Book (1)/cover.jpg", "Author/Book (1)/metadata.opf", "metadata.db"):
            f = tree["book_files"] / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"x")
        calibre = _fake_calibre([])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert "No orphan files found" in result.output

    def test_review_name_collision_gets_suffix(self, tmp_path):
        tree = _write_config(tmp_path)
        (tree["review"] / "stray.epub").write_bytes(b"existing")
        orphan = tree["book_files"] / "stray.epub"
        orphan.write_bytes(b"orphan")
        calibre = _fake_calibre([])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert (tree["review"] / "stray_orphan.epub").exists()
        assert (tree["review"] / "stray.epub").read_bytes() == b"existing"


# ---------------------------------------------------------------------------
# Docker mode — passes 3+4 skipped
# ---------------------------------------------------------------------------

class TestDockerModeSkips:
    def test_docker_mode_skips_reconciliation(self, tmp_path):
        tree = _write_config(tmp_path, mode="docker")
        # An orphan that must NOT be touched in docker mode
        orphan = tree["book_files"] / "stray.epub"
        orphan.write_bytes(b"epub")
        calibre = _fake_calibre([])
        store = MagicMock()

        result = _invoke(tree["config"], calibre, store, ["clean-library", "--yes"])

        assert result.exit_code == 0, result.output
        assert "Skipped — requires calibre.mode: local" in result.output
        assert orphan.exists()
        calibre.remove_book.assert_not_called()
        store.upsert.assert_not_called()
