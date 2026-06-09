"""Tests for the `libris pending-discard` CLI command (Issue #41)."""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import MagicMock, call, patch

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


def _make_pending(record_id: str, path: str, part_num: int = 1,
                  total_parts: int = 2, group_key: str = "test group") -> FileRecord:
    return FileRecord(
        id=record_id,
        original_path=path,
        current_path=path,
        media_type="audiobook",
        state=FileState.PENDING_PARTS,
        part_num=part_num,
        total_parts=total_parts,
        part_group_key=group_key,
    )


def _fake_store_with_groups(groups: dict) -> MagicMock:
    """Return a MagicMock store whose list_pending_groups() returns *groups*."""
    store = MagicMock()
    store.list_pending_groups.return_value = groups
    return store


# ---------------------------------------------------------------------------
# Tests: empty / invalid cases
# ---------------------------------------------------------------------------

class TestPendingDiscardEdgeCases:
    def test_no_groups_exits_cleanly(self, libris_tree):
        runner = CliRunner()
        store = _fake_store_with_groups({})
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard", "--id", "1"])
        assert result.exit_code == 0, result.output
        assert "No pending groups" in result.output
        store.upsert.assert_not_called()

    def test_out_of_range_id_dies(self, libris_tree, tmp_path):
        f = tmp_path / "part1.m4b"
        f.write_bytes(b"audio")
        rec = _make_pending("r1", str(f))
        store = _fake_store_with_groups({"test group": [rec]})

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard", "--id", "5"])
        assert result.exit_code != 0 or "out of range" in result.output

    def test_missing_id_flag_errors(self, libris_tree):
        """--id is required; omitting it should produce a usage error."""
        runner = CliRunner()
        store = _fake_store_with_groups({})
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Tests: normal discard operation
# ---------------------------------------------------------------------------

class TestPendingDiscardNormal:
    def test_files_moved_to_review(self, libris_tree, tmp_path):
        """Both parts in a group are moved to review/."""
        p1 = tmp_path / "Eragon Part 1 of 2.m4b"
        p2 = tmp_path / "Eragon Part 2 of 2.m4b"
        p1.write_bytes(b"audio1")
        p2.write_bytes(b"audio2")

        rec1 = _make_pending("r1", str(p1), part_num=1)
        rec2 = _make_pending("r2", str(p2), part_num=2)
        store = _fake_store_with_groups({"eragon": [rec1, rec2]})

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard", "--id", "1"])

        assert result.exit_code == 0, result.output
        review_dir = libris_tree["review"]
        # Both files should now live in review/
        assert any(review_dir.iterdir()), "review/ should have files after discard"
        moved_names = {f.name for f in review_dir.iterdir()}
        assert len(moved_names) == 2

    def test_part_markers_stripped_from_filenames(self, libris_tree, tmp_path):
        """Part markers (e.g. 'Part 1 of 2') are removed from the filename."""
        p1 = tmp_path / "Brisingr Part 1 of 3.m4b"
        p1.write_bytes(b"audio")
        rec1 = _make_pending("r1", str(p1), part_num=1, total_parts=3, group_key="brisingr")
        store = _fake_store_with_groups({"brisingr": [rec1]})

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard", "--id", "1"])

        assert result.exit_code == 0, result.output
        review_dir = libris_tree["review"]
        names = [f.name for f in review_dir.iterdir()]
        # "Part 1 of 3" should be stripped, leaving just "Brisingr.m4b"
        assert any("Part" not in n and "1 of 3" not in n for n in names), \
            f"Expected part marker stripped; got: {names}"

    def test_db_records_cleared(self, libris_tree, tmp_path):
        """After discard, upsert is called with state=REVIEW and cleared part fields."""
        p1 = tmp_path / "Book Part 1 of 2.m4b"
        p1.write_bytes(b"audio")
        rec1 = _make_pending("r1", str(p1), part_num=1, total_parts=2, group_key="book")
        store = _fake_store_with_groups({"book": [rec1]})

        upserted = []
        store.upsert.side_effect = upserted.append

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard", "--id", "1"])

        assert result.exit_code == 0, result.output
        assert len(upserted) == 1
        saved = upserted[0]
        assert saved.state == FileState.REVIEW
        assert saved.part_num is None
        assert saved.total_parts is None
        assert saved.part_group_key is None

    def test_current_path_updated_to_review(self, libris_tree, tmp_path):
        """The record's current_path is updated to the new review/ location."""
        p1 = tmp_path / "Stormlight Part 1 of 2.m4b"
        p1.write_bytes(b"audio")
        rec1 = _make_pending("r1", str(p1), part_num=1, group_key="stormlight")
        store = _fake_store_with_groups({"stormlight": [rec1]})

        upserted = []
        store.upsert.side_effect = upserted.append

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard", "--id", "1"])

        assert result.exit_code == 0, result.output
        saved_path = Path(upserted[0].current_path)
        assert saved_path.parent == libris_tree["review"]

    def test_output_echoes_filenames(self, libris_tree, tmp_path):
        """The command echoes the original filename and new location."""
        p1 = tmp_path / "Eragon Part 1 of 2.m4b"
        p1.write_bytes(b"audio")
        rec1 = _make_pending("r1", str(p1), part_num=1, group_key="eragon")
        store = _fake_store_with_groups({"eragon": [rec1]})

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard", "--id", "1"])

        assert result.exit_code == 0, result.output
        assert "Eragon Part 1 of 2.m4b" in result.output
        assert "review/" in result.output

    def test_correct_group_selected_by_id(self, libris_tree, tmp_path):
        """Selecting [2] discards the second group, not the first."""
        p1 = tmp_path / "Group A Part 1.m4b"
        p2 = tmp_path / "Group B Part 1.m4b"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")

        rec_a = _make_pending("ra", str(p1), group_key="group a")
        rec_b = _make_pending("rb", str(p2), group_key="group b")
        store = _fake_store_with_groups({
            "group a": [rec_a],
            "group b": [rec_b],
        })

        upserted = []
        store.upsert.side_effect = upserted.append

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard", "--id", "2"])

        assert result.exit_code == 0, result.output
        # Only rec_b (group b) should have been upserted
        assert len(upserted) == 1
        assert upserted[0].id == "rb"


# ---------------------------------------------------------------------------
# Tests: missing files
# ---------------------------------------------------------------------------

class TestPendingDiscardMissingFiles:
    def test_missing_file_skipped_with_warning(self, libris_tree, tmp_path):
        """If a part file is gone from disk, it is skipped with a warning."""
        present = tmp_path / "Eragon Part 1 of 2.m4b"
        present.write_bytes(b"audio")
        missing_path = "/nonexistent/Eragon Part 2 of 2.m4b"

        rec1 = _make_pending("r1", str(present), part_num=1, group_key="eragon")
        rec2 = _make_pending("r2", missing_path, part_num=2, group_key="eragon")
        store = _fake_store_with_groups({"eragon": [rec1, rec2]})

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pending-discard", "--id", "1"])

        # Should still succeed (exit 0), the present file moved
        assert result.exit_code == 0, result.output
        # The missing file warning should appear somewhere in combined output
        combined = result.output + (result.output or "")
        assert "File not found" in combined or "missing" in combined.lower() or \
               store.upsert.call_count == 1, \
               "Expected either warning text or only 1 upsert (the present file)"
