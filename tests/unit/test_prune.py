"""Tests for the `libris prune` CLI command (Issue #43)."""

from __future__ import annotations

import textwrap
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from libris.cli import main
from libris.state import FileRecord, FileState

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def libris_tree(tmp_path):
    """Minimal libris directory tree + config.yaml."""
    root     = tmp_path / "libris"
    incoming = root / "incoming"
    staging  = root / "staging"
    review   = root / "review"
    failed   = root / "failed"
    calibre  = tmp_path / "calibre-db"
    for d in (incoming, staging, review, failed, calibre):
        d.mkdir(parents=True)

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
    env = kwargs.pop("env", {})
    env["LIBRIS_CONFIG"] = str(cfg_path)
    return runner.invoke(main, args, env=env, **kwargs)


def _make_record(record_id: str, path: str, state: FileState) -> FileRecord:
    return FileRecord(
        id=record_id,
        original_path=path,
        current_path=path,
        media_type="ebook",
        state=state,
    )


def _fake_store(failed_records=None, pending_records=None):
    """Return a MagicMock StateStore with controlled list_by_state behaviour."""
    store = MagicMock()
    failed_records  = failed_records  or []
    pending_records = pending_records or []

    def _list_by_state(state):
        if state == FileState.FAILED:
            return failed_records
        if state == FileState.PENDING_PARTS:
            return pending_records
        return []

    store.list_by_state.side_effect = _list_by_state
    return store


# ---------------------------------------------------------------------------
# Tests: no stale records
# ---------------------------------------------------------------------------

class TestPruneNoStaleRecords:
    def test_no_records_at_all(self, libris_tree, tmp_path):
        runner = CliRunner()
        store = _fake_store()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune"])
        assert result.exit_code == 0, result.output
        assert "No stale records found" in result.output
        store.delete.assert_not_called()

    def test_existing_files_not_pruned(self, libris_tree, tmp_path):
        """Records whose files still exist on disk are not pruned."""
        real_file = tmp_path / "real_book.epub"
        real_file.write_text("content")
        record = _make_record("aaa", str(real_file), FileState.FAILED)

        runner = CliRunner()
        store = _fake_store(failed_records=[record])
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune"])
        assert result.exit_code == 0, result.output
        assert "No stale records found" in result.output
        store.delete.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: stale FAILED records
# ---------------------------------------------------------------------------

class TestPruneStaleFailedRecords:
    def test_prune_removes_stale_failed(self, libris_tree, tmp_path):
        """A FAILED record whose file is gone gets deleted from the store."""
        record = _make_record("abc123", "/nonexistent/book.epub", FileState.FAILED)

        runner = CliRunner()
        store = _fake_store(failed_records=[record])
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune"])

        assert result.exit_code == 0, result.output
        assert "1 stale record(s) found" in result.output
        assert "FAILED" in result.output
        assert "book.epub" in result.output
        assert "1 record(s) pruned" in result.output
        store.delete.assert_called_once_with("abc123")

    def test_prune_multiple_stale_failed(self, libris_tree, tmp_path):
        """All stale FAILED records are pruned when multiple exist."""
        records = [
            _make_record("id1", "/gone/book_a.epub", FileState.FAILED),
            _make_record("id2", "/gone/book_b.epub", FileState.FAILED),
        ]
        runner = CliRunner()
        store = _fake_store(failed_records=records)
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune"])

        assert result.exit_code == 0, result.output
        assert "2 stale record(s) found" in result.output
        assert "2 record(s) pruned" in result.output
        assert store.delete.call_count == 2

    def test_only_stale_removed_not_existing(self, libris_tree, tmp_path):
        """Only the missing-file record is pruned; the live record is untouched."""
        real_file = tmp_path / "live.epub"
        real_file.write_text("content")
        live    = _make_record("live_id",  str(real_file),       FileState.FAILED)
        missing = _make_record("gone_id",  "/gone/missing.epub", FileState.FAILED)

        runner = CliRunner()
        store = _fake_store(failed_records=[live, missing])
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune"])

        assert result.exit_code == 0, result.output
        assert "1 stale record(s) found" in result.output
        assert "1 record(s) pruned" in result.output
        store.delete.assert_called_once_with("gone_id")


# ---------------------------------------------------------------------------
# Tests: stale PENDING_PARTS records
# ---------------------------------------------------------------------------

class TestPruneStalePendingRecords:
    def test_prune_removes_stale_pending(self, libris_tree, tmp_path):
        """A PENDING_PARTS record whose file is gone gets deleted from the store."""
        record = _make_record("pend1", "/gone/part01.mp3", FileState.PENDING_PARTS)

        runner = CliRunner()
        store = _fake_store(pending_records=[record])
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune"])

        assert result.exit_code == 0, result.output
        assert "1 stale record(s) found" in result.output
        assert "PENDING" in result.output
        assert "part01.mp3" in result.output
        assert "1 record(s) pruned" in result.output
        store.delete.assert_called_once_with("pend1")

    def test_prune_mixed_failed_and_pending(self, libris_tree, tmp_path):
        """Both FAILED and PENDING_PARTS stale records are pruned in one run."""
        failed_rec  = _make_record("f1", "/gone/failed.epub",  FileState.FAILED)
        pending_rec = _make_record("p1", "/gone/part01.mp3",   FileState.PENDING_PARTS)

        runner = CliRunner()
        store = _fake_store(failed_records=[failed_rec], pending_records=[pending_rec])
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune"])

        assert result.exit_code == 0, result.output
        assert "2 stale record(s) found" in result.output
        assert "FAILED" in result.output
        assert "PENDING" in result.output
        assert "2 record(s) pruned" in result.output
        assert store.delete.call_count == 2


# ---------------------------------------------------------------------------
# Tests: --dry-run flag
# ---------------------------------------------------------------------------

class TestPruneDryRun:
    def test_dry_run_does_not_delete(self, libris_tree, tmp_path):
        """--dry-run reports what would be pruned but calls no deletes."""
        record = _make_record("xyz", "/gone/stale.epub", FileState.FAILED)

        runner = CliRunner()
        store = _fake_store(failed_records=[record])
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "1 stale record(s) found" in result.output
        assert "would be pruned" in result.output
        assert "[dry-run]" in result.output
        store.delete.assert_not_called()

    def test_dry_run_shows_both_states(self, libris_tree, tmp_path):
        """--dry-run lists both FAILED and PENDING records."""
        f_rec = _make_record("f1", "/gone/failed.epub", FileState.FAILED)
        p_rec = _make_record("p1", "/gone/part01.mp3",  FileState.PENDING_PARTS)

        runner = CliRunner()
        store = _fake_store(failed_records=[f_rec], pending_records=[p_rec])
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "2 stale record(s) found" in result.output
        assert "FAILED" in result.output
        assert "PENDING" in result.output
        assert "would be pruned" in result.output
        store.delete.assert_not_called()

    def test_dry_run_no_stale(self, libris_tree, tmp_path):
        """--dry-run with no stale records shows the 'nothing to prune' message."""
        runner = CliRunner()
        store = _fake_store()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["prune", "--dry-run"])

        assert result.exit_code == 0, result.output
        assert "No stale records found" in result.output
        store.delete.assert_not_called()
