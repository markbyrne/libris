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
