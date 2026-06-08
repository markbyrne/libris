"""Tests for migrate-libris and migrate-library CLI commands (Issue #19)."""

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from libris.cli import main


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def libris_tree(tmp_path):
    """Create a minimal libris directory tree + config.yaml and return paths."""
    root     = tmp_path / "libris"
    incoming = root / "incoming"
    staging  = root / "staging"
    review   = root / "review"
    failed   = root / "failed"
    calibre  = tmp_path / "calibre-db"
    for d in (incoming, staging, review, failed, calibre):
        d.mkdir(parents=True)

    # Seed some files
    (incoming / "book.epub").write_text("epub content")
    (review   / "maybe.epub").write_text("review content")
    db_file = root / "libris.db"
    db_file.write_text("fake db")

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
        "config": cfg,
        "root": root,
        "incoming": incoming,
        "staging": staging,
        "review": review,
        "failed": failed,
        "db": db_file,
        "calibre": calibre,
        "tmp": tmp_path,
    }


def _invoke(runner, cfg_path, args, **kwargs):
    """Invoke a libris command with LIBRIS_CONFIG env var set."""
    env = kwargs.pop("env", {})
    env["LIBRIS_CONFIG"] = str(cfg_path)
    return runner.invoke(main, args, env=env, **kwargs)


# ---------------------------------------------------------------------------
# _update_config_paths helper
# ---------------------------------------------------------------------------

class TestUpdateConfigPaths:
    def test_updates_simple_path(self, tmp_path):
        from libris.cli import _update_config_paths
        cfg = tmp_path / "config.yaml"
        cfg.write_text("paths:\n  staging_dir: /old/staging\n  review_dir: /old/review\n")
        _update_config_paths(cfg, {"paths.staging_dir": Path("/new/staging")})
        text = cfg.read_text()
        assert "/new/staging" in text
        assert "/old/staging" not in text
        # review_dir should be untouched
        assert "/old/review" in text

    def test_preserves_inline_comments(self, tmp_path):
        from libris.cli import _update_config_paths
        cfg = tmp_path / "config.yaml"
        cfg.write_text("paths:\n  staging_dir: /old/staging  # temp workspace\n")
        _update_config_paths(cfg, {"paths.staging_dir": Path("/new/staging")})
        text = cfg.read_text()
        assert "# temp workspace" in text
        assert "/new/staging" in text

    def test_updates_multiple_keys(self, tmp_path):
        from libris.cli import _update_config_paths
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "paths:\n"
            "  staging_dir: /old/staging\n"
            "  review_dir: /old/review\n"
            "  failed_dir: /old/failed\n"
        )
        _update_config_paths(cfg, {
            "paths.staging_dir": Path("/new/staging"),
            "paths.failed_dir": Path("/new/failed"),
        })
        text = cfg.read_text()
        assert "/new/staging" in text
        assert "/old/review" in text        # untouched
        assert "/new/failed" in text


# ---------------------------------------------------------------------------
# _update_config_calibre_split helper
# ---------------------------------------------------------------------------

class TestUpdateConfigCalibreSplit:
    def test_renames_library_path_to_library_db_path(self, tmp_path):
        from libris.cli import _update_config_calibre_split
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "calibre:\n"
            "  mode: local\n"
            "  library_path: /calibre\n"
        )
        _update_config_calibre_split(cfg, "/calibre", "/mnt/books")
        text = cfg.read_text()
        assert "library_db_path:" in text
        assert "library_path:" not in text   # renamed
        assert "book_file_path: /mnt/books" in text

    def test_updates_existing_library_db_path(self, tmp_path):
        from libris.cli import _update_config_calibre_split
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "calibre:\n"
            "  mode: local\n"
            "  library_db_path: /calibre\n"
        )
        _update_config_calibre_split(cfg, "/calibre", "/mnt/books")
        text = cfg.read_text()
        assert "library_db_path: /calibre" in text
        assert "book_file_path: /mnt/books" in text

    def test_updates_existing_book_file_path(self, tmp_path):
        from libris.cli import _update_config_calibre_split
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "calibre:\n"
            "  mode: local\n"
            "  library_db_path: /calibre\n"
            "  book_file_path: /old/books\n"
        )
        _update_config_calibre_split(cfg, "/calibre", "/new/books")
        text = cfg.read_text()
        assert "book_file_path: /new/books" in text
        assert "/old/books" not in text


# ---------------------------------------------------------------------------
# migrate-libris command
# ---------------------------------------------------------------------------

class TestMigrateLibris:
    def test_dry_run_prints_plan_no_changes(self, libris_tree):
        runner = CliRunner()
        to = libris_tree["tmp"] / "new-libris"
        result = _invoke(runner, libris_tree["config"], [
            "migrate-libris", str(to), "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert not to.exists(), "dry-run must not create destination"

    def test_moves_dirs_and_db(self, libris_tree):
        runner = CliRunner()
        to = libris_tree["tmp"] / "new-libris"
        result = _invoke(runner, libris_tree["config"], [
            "migrate-libris", str(to),
        ], input="y\n")
        assert result.exit_code == 0, result.output

        # Files should exist at new location
        assert (to / "incoming" / "book.epub").exists()
        assert (to / "review"   / "maybe.epub").exists()
        assert (to / "libris.db").exists()

        # Old locations should be gone
        assert not libris_tree["incoming"].exists()
        assert not libris_tree["db"].exists()

    def test_config_updated_after_move(self, libris_tree):
        runner = CliRunner()
        to = libris_tree["tmp"] / "new-libris"
        _invoke(runner, libris_tree["config"], [
            "migrate-libris", str(to),
        ], input="y\n")

        text = libris_tree["config"].read_text()
        assert str(to) in text
        # Old paths should no longer appear for migrated dirs
        assert str(libris_tree["incoming"]) not in text

    def test_aborted_by_user_makes_no_changes(self, libris_tree):
        runner = CliRunner()
        to = libris_tree["tmp"] / "new-libris"
        _invoke(runner, libris_tree["config"], [
            "migrate-libris", str(to),
        ], input="n\n")
        assert not to.exists()
        assert libris_tree["incoming"].exists()


# ---------------------------------------------------------------------------
# migrate-library command
# ---------------------------------------------------------------------------

class TestMigrateLibrary:
    def _seed_calibre(self, calibre_path: Path) -> list[Path]:
        """Create a realistic calibre directory tree and return file list."""
        files = []
        for author_title, filename in [
            ("Andy Weir/The Martian (1)", "the-martian.epub"),
            ("Brandon Sanderson/Mistborn (2)", "mistborn.epub"),
        ]:
            d = calibre_path / author_title
            d.mkdir(parents=True)
            f = d / filename
            f.write_text("fake epub")
            files.append(f)
        (calibre_path / "metadata.db").write_text("fake db")
        return files

    def test_books_only_dry_run(self, libris_tree):
        runner = CliRunner()
        calibre = libris_tree["calibre"]
        self._seed_calibre(calibre)
        dest = libris_tree["tmp"] / "ext-drive" / "books"
        result = _invoke(runner, libris_tree["config"], [
            "migrate-library", str(calibre), str(dest), "--books-only", "--dry-run",
        ])
        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert not dest.exists()
        # metadata.db must still be in calibre
        assert (calibre / "metadata.db").exists()

    def test_books_only_moves_files_not_db(self, libris_tree):
        runner = CliRunner()
        calibre = libris_tree["calibre"]
        book_files = self._seed_calibre(calibre)
        dest = libris_tree["tmp"] / "ext-drive" / "books"
        result = _invoke(runner, libris_tree["config"], [
            "migrate-library", str(calibre), str(dest), "--books-only",
        ], input="y\n")
        assert result.exit_code == 0, result.output

        # Book files exist at dest
        for bf in book_files:
            rel = bf.relative_to(calibre)
            assert (dest / rel).exists(), f"Missing at dest: {rel}"

        # metadata.db stays at calibre
        assert (calibre / "metadata.db").exists()
        # Book files gone from calibre
        for bf in book_files:
            assert not bf.exists()

    def test_books_only_updates_config_split_mode(self, libris_tree):
        runner = CliRunner()
        calibre = libris_tree["calibre"]
        self._seed_calibre(calibre)
        dest = libris_tree["tmp"] / "ext-drive" / "books"
        _invoke(runner, libris_tree["config"], [
            "migrate-library", str(calibre), str(dest), "--books-only",
        ], input="y\n")

        text = libris_tree["config"].read_text()
        assert "library_db_path" in text
        assert f"book_file_path: {dest}" in text

    def test_full_move_moves_everything(self, libris_tree):
        runner = CliRunner()
        calibre = libris_tree["calibre"]
        book_files = self._seed_calibre(calibre)
        dest = libris_tree["tmp"] / "new-calibre"
        result = _invoke(runner, libris_tree["config"], [
            "migrate-library", str(calibre), str(dest),
        ], input="y\n")
        assert result.exit_code == 0, result.output

        assert (dest / "metadata.db").exists()
        for bf in book_files:
            rel = bf.relative_to(calibre)
            assert (dest / rel).exists()
        assert not calibre.exists()

    def test_mutual_exclusion_raises(self, libris_tree):
        runner = CliRunner()
        result = _invoke(runner, libris_tree["config"], [
            "migrate-library", "/from", "/to", "--books-only", "--db-only",
        ])
        assert result.exit_code != 0
        assert "--books-only" in result.output or "cannot be used together" in result.output

    def test_books_only_skip_conflicts(self, libris_tree):
        """When dest already has a file, 'skip' leaves dest unchanged, skips that file."""
        runner = CliRunner()
        calibre = libris_tree["calibre"]
        book_files = self._seed_calibre(calibre)
        dest = libris_tree["tmp"] / "ext-drive" / "books"

        # Pre-plant one of the book files at the destination with different content
        pre_existing = dest / book_files[0].relative_to(calibre)
        pre_existing.parent.mkdir(parents=True, exist_ok=True)
        pre_existing.write_text("original content — should survive")

        result = _invoke(runner, libris_tree["config"], [
            "migrate-library", str(calibre), str(dest), "--books-only",
        ], input="skip\ny\n")   # conflict prompt: skip; proceed: yes
        assert result.exit_code == 0, result.output
        assert "skipped" in result.output

        # Destination file must keep its original content
        assert pre_existing.read_text() == "original content — should survive"
        # The conflicting source file must still be at the source (was not moved)
        assert book_files[0].exists()
        # Non-conflicting file must have been moved
        assert (dest / book_files[1].relative_to(calibre)).exists()

    def test_books_only_overwrite_conflicts(self, libris_tree):
        """When dest already has a file, 'overwrite' replaces it with the source."""
        runner = CliRunner()
        calibre = libris_tree["calibre"]
        book_files = self._seed_calibre(calibre)
        dest = libris_tree["tmp"] / "ext-drive" / "books"

        pre_existing = dest / book_files[0].relative_to(calibre)
        pre_existing.parent.mkdir(parents=True, exist_ok=True)
        pre_existing.write_text("old content")

        result = _invoke(runner, libris_tree["config"], [
            "migrate-library", str(calibre), str(dest), "--books-only",
        ], input="overwrite\ny\n")
        assert result.exit_code == 0, result.output

        # Destination file replaced by source
        assert pre_existing.read_text() == "fake epub"

    def test_books_only_abort_on_conflict(self, libris_tree):
        """Choosing 'abort' when conflicts are found leaves both source and dest unchanged."""
        runner = CliRunner()
        calibre = libris_tree["calibre"]
        book_files = self._seed_calibre(calibre)
        dest = libris_tree["tmp"] / "ext-drive" / "books"

        pre_existing = dest / book_files[0].relative_to(calibre)
        pre_existing.parent.mkdir(parents=True, exist_ok=True)
        pre_existing.write_text("original")

        result = _invoke(runner, libris_tree["config"], [
            "migrate-library", str(calibre), str(dest), "--books-only",
        ], input="abort\n")
        assert result.exit_code == 0, result.output
        assert "Aborted" in result.output

        # Source files intact
        for bf in book_files:
            assert bf.exists()
        # Pre-existing dest file unchanged
        assert pre_existing.read_text() == "original"

    def test_full_move_conflict_warns_and_aborts(self, libris_tree):
        """Full move with a conflict at dest warns and aborts when user says no."""
        runner = CliRunner()
        calibre = libris_tree["calibre"]
        book_files = self._seed_calibre(calibre)
        dest = libris_tree["tmp"] / "new-calibre"

        # Pre-plant a conflicting file
        conflict = dest / book_files[0].relative_to(calibre)
        conflict.parent.mkdir(parents=True, exist_ok=True)
        conflict.write_text("keep me")

        result = _invoke(runner, libris_tree["config"], [
            "migrate-library", str(calibre), str(dest), "--books-only",
        ], input="abort\n")
        assert result.exit_code == 0
        assert conflict.read_text() == "keep me"
        assert calibre.exists()  # source not deleted


# ---------------------------------------------------------------------------
# check-config display section (Issues #26, #27)
# ---------------------------------------------------------------------------

class TestCheckConfigDisplay:
    """Tests for the settings display block of check-config."""

    def test_book_files_shown_when_set(self, libris_tree, tmp_path):
        """check-config shows 'Book files:' when book_file_path is configured."""
        books = tmp_path / "ext-books"
        books.mkdir()
        # Patch the config to include book_file_path under the calibre section
        text = libris_tree["config"].read_text()
        text = text.replace(
            f"  library_path: {libris_tree['calibre']}",
            f"  library_db_path: {libris_tree['calibre']}\n  book_file_path: {books}",
        )
        libris_tree["config"].write_text(text)
        runner = CliRunner()
        result = _invoke(runner, libris_tree["config"], ["check-config"])
        assert "Book files:" in result.output
        assert str(books) in result.output

    def test_book_files_not_shown_when_unset(self, libris_tree):
        """check-config does not show 'Book files:' when book_file_path is absent."""
        runner = CliRunner()
        result = _invoke(runner, libris_tree["config"], ["check-config"])
        assert "Book files:" not in result.output

    def test_book_files_warns_when_missing(self, libris_tree):
        """check-config warns when book_file_path is configured but doesn't exist."""
        text = libris_tree["config"].read_text()
        text = text.replace(
            f"  library_path: {libris_tree['calibre']}",
            f"  library_db_path: {libris_tree['calibre']}\n  book_file_path: /nonexistent/books",
        )
        libris_tree["config"].write_text(text)
        runner = CliRunner()
        result = _invoke(runner, libris_tree["config"], ["check-config"])
        assert "Book files:" in result.output
        assert "does not exist" in result.output or "⚠" in result.output

    def test_google_books_enabled_shown(self, libris_tree):
        """check-config shows 'enabled (key configured)' when API key is set; key not printed."""
        text = libris_tree["config"].read_text()
        # Inject google_books_api_key under the metadata section
        text = text.replace(
            "  confidence_threshold: 0.75",
            "  confidence_threshold: 0.75\n  google_books_api_key: secret-api-key-xyz",
        )
        libris_tree["config"].write_text(text)
        runner = CliRunner()
        result = _invoke(runner, libris_tree["config"], ["check-config"])
        assert "Google Books:" in result.output
        assert "enabled (key configured)" in result.output
        assert "secret-api-key-xyz" not in result.output  # key must never be printed

    def test_google_books_disabled_shown(self, libris_tree):
        """check-config shows 'disabled (no key)' when no API key is configured."""
        runner = CliRunner()
        result = _invoke(runner, libris_tree["config"], ["check-config"])
        assert "Google Books:" in result.output
        assert "disabled (no key)" in result.output
