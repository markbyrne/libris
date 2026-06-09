"""Tests for the `libris pair-pending` CLI command (Issue #40)."""

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


def _make_pending(record_id: str, path: str, part_num: int = 1,
                  total_parts: int = 1, group_key: str = "group") -> FileRecord:
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
    """Return a MagicMock store with list_pending_groups returning *groups*."""
    store = MagicMock()
    store.list_pending_groups.return_value = groups
    return store


# ---------------------------------------------------------------------------
# Tests: edge cases / validation
# ---------------------------------------------------------------------------

class TestPairPendingEdgeCases:
    def test_no_groups_exits_cleanly(self, libris_tree):
        runner = CliRunner()
        store = _fake_store_with_groups({})
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pair-pending", "--id1", "1", "--id2", "2"])
        assert result.exit_code == 0, result.output
        assert "No pending groups" in result.output
        store.upsert.assert_not_called()

    def test_same_ids_rejected(self, libris_tree, tmp_path):
        runner = CliRunner()
        store = _fake_store_with_groups({})
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pair-pending", "--id1", "1", "--id2", "1"])
        assert result.exit_code != 0

    def test_out_of_range_id1_dies(self, libris_tree, tmp_path):
        f = tmp_path / "p1.m4b"
        f.write_bytes(b"x")
        rec = _make_pending("r1", str(f))
        store = _fake_store_with_groups({"g": [rec]})
        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pair-pending", "--id1", "9", "--id2", "1"])
        assert result.exit_code != 0 or "out of range" in result.output

    def test_missing_flags_errors(self, libris_tree):
        """Both --id1 and --id2 are required."""
        runner = CliRunner()
        store = _fake_store_with_groups({})
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pair-pending", "--id1", "1"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Tests: normal merge operation (no auto-combine trigger)
# ---------------------------------------------------------------------------

class TestPairPendingMerge:
    def test_records_reassigned_to_group1_key(self, libris_tree, tmp_path):
        """After merge, all records have group_key == key1."""
        p1 = tmp_path / "part1.m4b"
        p2 = tmp_path / "part2.m4b"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")

        rec1 = _make_pending("r1", str(p1), part_num=1, group_key="group-alpha")
        rec2 = _make_pending("r2", str(p2), part_num=1, group_key="group-beta")
        store = _fake_store_with_groups({
            "group-alpha": [rec1],
            "group-beta":  [rec2],
        })

        upserted = []
        store.upsert.side_effect = upserted.append

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            # Patch Pipeline to prevent actual combine (missing parts warning path)
            with patch("libris.cli.Pipeline"):
                result = _invoke(runner, libris_tree["config"], ["pair-pending", "--id1", "1", "--id2", "2"])

        assert result.exit_code == 0, result.output
        # Both records should now use group-alpha as their key
        for saved in upserted:
            assert saved.part_group_key == "group-alpha", \
                f"Expected 'group-alpha', got '{saved.part_group_key}'"

    def test_part_nums_resequenced(self, libris_tree, tmp_path):
        """Part numbers are reassigned 1…N across both groups."""
        p1 = tmp_path / "book_a_part1.m4b"
        p2 = tmp_path / "book_b_part1.m4b"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")

        rec1 = _make_pending("r1", str(p1), part_num=1, group_key="group-a")
        rec2 = _make_pending("r2", str(p2), part_num=1, group_key="group-b")
        store = _fake_store_with_groups({
            "group-a": [rec1],
            "group-b": [rec2],
        })

        upserted = []
        store.upsert.side_effect = upserted.append

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            with patch("libris.cli.Pipeline"):
                result = _invoke(runner, libris_tree["config"], ["pair-pending", "--id1", "1", "--id2", "2"])

        assert result.exit_code == 0, result.output
        part_nums = sorted(r.part_num for r in upserted)
        assert part_nums == [1, 2], f"Expected [1, 2], got {part_nums}"

    def test_total_parts_updated(self, libris_tree, tmp_path):
        """total_parts is set to the combined count for all records."""
        p1 = tmp_path / "part1.m4b"
        p2 = tmp_path / "part2.m4b"
        p3 = tmp_path / "part3.m4b"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")
        p3.write_bytes(b"c")

        # group-a has 2 parts, group-b has 1 part → merged total = 3
        rec1 = _make_pending("r1", str(p1), part_num=1, total_parts=2, group_key="group-a")
        rec2 = _make_pending("r2", str(p2), part_num=2, total_parts=2, group_key="group-a")
        rec3 = _make_pending("r3", str(p3), part_num=1, total_parts=1, group_key="group-b")
        store = _fake_store_with_groups({
            "group-a": [rec1, rec2],
            "group-b": [rec3],
        })

        upserted = []
        store.upsert.side_effect = upserted.append

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            with patch("libris.cli.Pipeline"):
                result = _invoke(runner, libris_tree["config"], ["pair-pending", "--id1", "1", "--id2", "2"])

        assert result.exit_code == 0, result.output
        for saved in upserted:
            assert saved.total_parts == 3, f"Expected total=3, got {saved.total_parts}"

    def test_output_shows_group_names(self, libris_tree, tmp_path):
        """The command echoes both group names in the merge message."""
        p1 = tmp_path / "a.m4b"
        p2 = tmp_path / "b.m4b"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")

        rec1 = _make_pending("r1", str(p1), group_key="the-eragon-series")
        rec2 = _make_pending("r2", str(p2), group_key="eragon-audiobook")
        store = _fake_store_with_groups({
            "the-eragon-series": [rec1],
            "eragon-audiobook":  [rec2],
        })

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            with patch("libris.cli.Pipeline"):
                result = _invoke(runner, libris_tree["config"], ["pair-pending", "--id1", "1", "--id2", "2"])

        assert result.exit_code == 0, result.output
        assert "the-eragon-series" in result.output
        assert "eragon-audiobook" in result.output

    def test_missing_parts_shows_warning(self, libris_tree, tmp_path):
        """When files are missing after merge, a 'still missing' warning is shown."""
        present = tmp_path / "present.m4b"
        present.write_bytes(b"a")

        rec1 = _make_pending("r1", str(present),         group_key="group-a")
        rec2 = _make_pending("r2", "/gone/missing.m4b",  group_key="group-b")
        store = _fake_store_with_groups({
            "group-a": [rec1],
            "group-b": [rec2],
        })
        store.upsert.return_value = None

        runner = CliRunner()
        with patch("libris.cli._open_store", return_value=store):
            result = _invoke(runner, libris_tree["config"], ["pair-pending", "--id1", "1", "--id2", "2"])

        assert result.exit_code == 0, result.output
        assert "missing" in result.output.lower() or "combine-parts" in result.output


# ---------------------------------------------------------------------------
# Tests: auto-trigger combine when all parts present
# ---------------------------------------------------------------------------

class TestPairPendingAutoTrigger:
    def test_auto_combine_triggered_when_complete(self, libris_tree, tmp_path):
        """When all files are on disk after merge, Pipeline._combine_pending_group is called."""
        p1 = tmp_path / "part1.m4b"
        p2 = tmp_path / "part2.m4b"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")

        rec1 = _make_pending("r1", str(p1), part_num=1, group_key="group-a")
        rec2 = _make_pending("r2", str(p2), part_num=1, group_key="group-b")
        store = _fake_store_with_groups({
            "group-a": [rec1],
            "group-b": [rec2],
        })
        store.upsert.return_value = None

        fake_result = FileRecord(
            id="result",
            original_path="/out/book.m4b",
            current_path="/out/book.m4b",
            media_type="audiobook",
            state=FileState.IMPORTED,
            matched_title="Eragon",
            matched_author="Christopher Paolini",
            confidence=0.92,
        )

        runner = CliRunner()
        mock_pipeline = MagicMock()
        mock_pipeline._combine_pending_group.return_value = fake_result

        with patch("libris.cli._open_store", return_value=store):
            with patch("libris.cli.Pipeline", return_value=mock_pipeline):
                result = _invoke(runner, libris_tree["config"],
                                 ["pair-pending", "--id1", "1", "--id2", "2"])

        assert result.exit_code == 0, result.output
        mock_pipeline._combine_pending_group.assert_called_once()
        assert "Eragon" in result.output
        assert "✅" in result.output or "0.92" in result.output

    def test_auto_combine_review_result(self, libris_tree, tmp_path):
        """When combine lands in review, the output says so."""
        p1 = tmp_path / "part1.m4b"
        p2 = tmp_path / "part2.m4b"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")

        rec1 = _make_pending("r1", str(p1), group_key="group-a")
        rec2 = _make_pending("r2", str(p2), group_key="group-b")
        store = _fake_store_with_groups({
            "group-a": [rec1],
            "group-b": [rec2],
        })
        store.upsert.return_value = None

        review_result = FileRecord(
            id="result",
            original_path="/review/book.m4b",
            current_path="/review/book.m4b",
            media_type="audiobook",
            state=FileState.REVIEW,
            matched_title="Unknown Book",
            confidence=0.45,
        )

        runner = CliRunner()
        mock_pipeline = MagicMock()
        mock_pipeline._combine_pending_group.return_value = review_result

        with patch("libris.cli._open_store", return_value=store):
            with patch("libris.cli.Pipeline", return_value=mock_pipeline):
                result = _invoke(runner, libris_tree["config"],
                                 ["pair-pending", "--id1", "1", "--id2", "2"])

        assert result.exit_code == 0, result.output
        assert "review/" in result.output or "rematch" in result.output

    def test_auto_combine_exception_exits_nonzero(self, libris_tree, tmp_path):
        """If _combine_pending_group raises, the command exits non-zero."""
        p1 = tmp_path / "part1.m4b"
        p2 = tmp_path / "part2.m4b"
        p1.write_bytes(b"a")
        p2.write_bytes(b"b")

        rec1 = _make_pending("r1", str(p1), group_key="group-a")
        rec2 = _make_pending("r2", str(p2), group_key="group-b")
        store = _fake_store_with_groups({
            "group-a": [rec1],
            "group-b": [rec2],
        })
        store.upsert.return_value = None

        runner = CliRunner()
        mock_pipeline = MagicMock()
        mock_pipeline._combine_pending_group.side_effect = RuntimeError("ffmpeg crashed")

        with patch("libris.cli._open_store", return_value=store):
            with patch("libris.cli.Pipeline", return_value=mock_pipeline):
                result = _invoke(runner, libris_tree["config"],
                                 ["pair-pending", "--id1", "1", "--id2", "2"])

        assert result.exit_code != 0
