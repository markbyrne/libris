"""Tests for list-failed queue refresh after remove/recover actions (Issue #39).

Verifies that after `libris remove` and `libris recover` complete their action,
the updated failed queue is reprinted using the shared _render_failed_list helper.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
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


def _make_failed(record_id: str, path: str, error: str = "something broke") -> FileRecord:
    return FileRecord(
        id=record_id,
        original_path=path,
        current_path=path,
        media_type="ebook",
        state=FileState.FAILED,
        error_msg=error,
    )


def _fake_store_factory(initial_records):
    """Return a factory that creates a MagicMock store whose list_by_state
    reflects `initial_records` until delete() is called, then reflects
    the remaining ones.
    """
    current = list(initial_records)

    store = MagicMock()

    def _list_by_state(state):
        if state == FileState.FAILED:
            return list(current)
        return []

    def _delete(record_id):
        nonlocal current
        current = [r for r in current if r.id != record_id]

    store.list_by_state.side_effect = _list_by_state
    store.delete.side_effect = _delete
    return store


# ---------------------------------------------------------------------------
# Tests: list-failed uses _render_failed_list (backward-compat smoke test)
# ---------------------------------------------------------------------------

class TestListFailed:
    def test_empty_queue(self, libris_tree):
        runner = CliRunner()
        store = MagicMock()
        store.list_by_state.return_value = []
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["list-failed"])
        assert result.exit_code == 0, result.output
        assert "No files in failed state" in result.output

    def test_renders_records(self, libris_tree, tmp_path):
        real_file = tmp_path / "broken.epub"
        real_file.write_text("content")
        record = _make_failed("r1", str(real_file), error="API timed out")

        runner = CliRunner()
        store = MagicMock()
        store.list_by_state.return_value = [record]
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["list-failed"])

        assert result.exit_code == 0, result.output
        assert "1 file(s) in failed state" in result.output
        assert "broken.epub" in result.output
        assert "API timed out" in result.output
        assert "Recover by ID" in result.output

    def test_stale_record_shown_dimmed_with_hint(self, libris_tree):
        """Records whose file is gone are shown with a remove hint."""
        record = _make_failed("r1", "/nonexistent/missing.epub")

        runner = CliRunner()
        store = MagicMock()
        store.list_by_state.return_value = [record]
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["list-failed"])

        assert result.exit_code == 0, result.output
        assert "missing.epub" in result.output
        assert "libris remove --id" in result.output
        assert "record(s) with missing files" in result.output


# ---------------------------------------------------------------------------
# Tests: remove prints updated queue after action
# ---------------------------------------------------------------------------

class TestRemoveRefreshesQueue:
    def test_remove_id_shows_remaining_queue(self, libris_tree, tmp_path):
        """After removing [1], the remaining failed record is reprinted."""
        file_a = tmp_path / "book_a.epub"
        file_b = tmp_path / "book_b.epub"
        file_a.write_text("a")
        file_b.write_text("b")

        rec_a = _make_failed("id_a", str(file_a))
        rec_b = _make_failed("id_b", str(file_b))
        store = _fake_store_factory([rec_a, rec_b])

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["remove", "--id", "1"])

        assert result.exit_code == 0, result.output
        assert "1 file(s) removed" in result.output
        # Remaining queue should be shown
        assert "1 file(s) in failed state" in result.output
        assert "book_b.epub" in result.output

    def test_remove_all_shows_empty_queue(self, libris_tree, tmp_path):
        """After removing all records, the queue shows 'no files in failed state'."""
        file_a = tmp_path / "a.epub"
        file_a.write_text("a")
        rec_a = _make_failed("id_a", str(file_a))
        store = _fake_store_factory([rec_a])

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["remove", "--all"])

        assert result.exit_code == 0, result.output
        assert "1 file(s) removed" in result.output
        assert "No files in failed state" in result.output

    def test_remove_chaff_shows_remaining_non_chaff(self, libris_tree, tmp_path):
        """After removing chaff, normal files still in queue are shown."""
        chaff_file  = tmp_path / "Read Me.epub"
        normal_file = tmp_path / "real-novel.epub"
        chaff_file.write_text("chaff")
        normal_file.write_text("novel")

        chaff_rec  = _make_failed("c1", str(chaff_file))
        normal_rec = _make_failed("n1", str(normal_file))
        store = _fake_store_factory([chaff_rec, normal_rec])

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["remove", "--chaff"])

        assert result.exit_code == 0, result.output
        assert "1 file(s) removed" in result.output
        # remaining queue
        assert "1 file(s) in failed state" in result.output
        assert "real-novel.epub" in result.output


# ---------------------------------------------------------------------------
# Tests: recover --delete prints updated queue after action
# ---------------------------------------------------------------------------

class TestRecoverDeleteRefreshesQueue:
    def test_delete_missing_shows_remaining_queue(self, libris_tree, tmp_path):
        """recover --delete removes missing-file records and shows remaining queue."""
        real_file  = tmp_path / "real.epub"
        real_file.write_text("content")
        live_rec   = _make_failed("live", str(real_file))
        ghost_rec  = _make_failed("ghost", "/gone/missing.epub")

        # upsert marks record as IMPORTED; we reflect that by removing from FAILED list
        current = [live_rec, ghost_rec]
        store = MagicMock()

        def _list_by_state(state):
            if state == FileState.FAILED:
                return [r for r in current if r.state == FileState.FAILED]
            return []

        def _upsert(record):
            for r in current:
                if r.id == record.id:
                    r.state = record.state

        store.list_by_state.side_effect = _list_by_state
        store.upsert.side_effect = _upsert

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["recover", "--delete"])

        assert result.exit_code == 0, result.output
        # remaining queue: live_rec still FAILED
        assert "1 file(s) in failed state" in result.output
        assert "real.epub" in result.output

    def test_delete_all_shows_empty_queue(self, libris_tree, tmp_path):
        """recover --delete --all empties queue and shows 'no files' message."""
        real_file = tmp_path / "stuck.epub"
        real_file.write_text("content")
        rec = _make_failed("r1", str(real_file))

        current = [rec]
        store = MagicMock()

        def _list_by_state(state):
            if state == FileState.FAILED:
                return [r for r in current if r.state == FileState.FAILED]
            return []

        def _upsert(record):
            for r in current:
                if r.id == record.id:
                    r.state = record.state

        store.list_by_state.side_effect = _list_by_state
        store.upsert.side_effect = _upsert

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["recover", "--delete", "--all"])

        assert result.exit_code == 0, result.output
        assert "No files in failed state" in result.output


# ---------------------------------------------------------------------------
# Tests: recover (move to review) prints updated queue
# ---------------------------------------------------------------------------

class TestRecoverMoveRefreshesQueue:
    def test_recover_id_shows_remaining_queue(self, libris_tree, tmp_path):
        """After recovering [1] to review, remaining failed records are shown."""
        file_a = tmp_path / "book_a.epub"
        file_b = tmp_path / "book_b.epub"
        file_a.write_text("a")
        file_b.write_text("b")

        rec_a = _make_failed("id_a", str(file_a))
        rec_b = _make_failed("id_b", str(file_b))

        current = [rec_a, rec_b]
        store = MagicMock()

        def _list_by_state(state):
            if state == FileState.FAILED:
                return [r for r in current if r.state == FileState.FAILED]
            return []

        def _upsert(record):
            for r in current:
                if r.id == record.id:
                    r.state = record.state
                    r.current_path = record.current_path

        store.list_by_state.side_effect = _list_by_state
        store.upsert.side_effect = _upsert

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["recover", "--id", "1"])

        assert result.exit_code == 0, result.output
        assert "Run 'libris list-review'" in result.output
        # remaining queue
        assert "Updated failed queue" in result.output
        assert "1 file(s) in failed state" in result.output
        assert "book_b.epub" in result.output

    def test_recover_all_hides_queue_when_empty(self, libris_tree, tmp_path):
        """After recovering all files, the 'Updated failed queue' section is skipped."""
        file_a = tmp_path / "book_a.epub"
        file_a.write_text("a")
        rec_a = _make_failed("id_a", str(file_a))

        current = [rec_a]
        store = MagicMock()

        def _list_by_state(state):
            if state == FileState.FAILED:
                return [r for r in current if r.state == FileState.FAILED]
            return []

        def _upsert(record):
            for r in current:
                if r.id == record.id:
                    r.state = record.state
                    r.current_path = record.current_path

        store.list_by_state.side_effect = _list_by_state
        store.upsert.side_effect = _upsert

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["recover", "--all"])

        assert result.exit_code == 0, result.output
        assert "Run 'libris list-review'" in result.output
        # No remaining records — the "Updated failed queue:" header should NOT appear
        assert "Updated failed queue" not in result.output
        assert "No files in failed state" not in result.output
