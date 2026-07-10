"""Unit tests for libris/watcher/fswatch_watcher.py (FswatchWatcher).

30% covered before this file (only _should_process/_wait_stable indirectly
exercised elsewhere). subprocess.Popen is always mocked; _wait_stable and
_wait_stable_dir are driven against real tmp_path files/dirs with a small
interval so the tests stay fast (no real fswatch process is ever spawned).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from libris.config import WatcherConfig
from libris.exceptions import WatcherError
from libris.watcher.fswatch_watcher import (
    FswatchWatcher,
    _wait_stable,
    _wait_stable_dir,
)


def _make_watcher(tmp_path: Path) -> FswatchWatcher:
    cfg = WatcherConfig(incoming_dir=tmp_path / "incoming", poll_interval_seconds=0.5)
    return FswatchWatcher(cfg)


# ---------------------------------------------------------------------------
# _should_process
# ---------------------------------------------------------------------------

class TestShouldProcess:
    def test_missing_path_is_rejected(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        assert watcher._should_process(incoming / "gone.epub") is False

    def test_hidden_file_rejected(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        hidden = incoming / ".hidden.epub"
        hidden.write_text("x")
        assert watcher._should_process(hidden) is False

    def test_direct_child_accepted(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        book = incoming / "book.epub"
        book.write_text("x")
        assert watcher._should_process(book) is True

    def test_nested_file_rejected(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        sub = incoming / "subdir"
        sub.mkdir(parents=True)
        nested = sub / "book.epub"
        nested.write_text("x")
        assert watcher._should_process(nested) is False


# ---------------------------------------------------------------------------
# _start_process
# ---------------------------------------------------------------------------

class TestStartProcess:
    def test_creates_incoming_dir(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        assert not watcher._incoming_dir.exists()
        with patch("libris.watcher.fswatch_watcher.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(stdout=iter([]))
            watcher._start_process()
        assert watcher._incoming_dir.exists()
        watcher.stop()

    def test_missing_fswatch_raises_watcher_error(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        with patch(
            "libris.watcher.fswatch_watcher.subprocess.Popen",
            side_effect=FileNotFoundError("fswatch not found"),
        ):
            with pytest.raises(WatcherError, match="fswatch not found"):
                watcher._start_process()

    def test_command_includes_latency_and_events(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        with patch("libris.watcher.fswatch_watcher.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(stdout=iter([]))
            watcher._start_process()
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "fswatch"
        assert "--recursive" in cmd
        assert "Created" in cmd
        assert "MovedTo" in cmd
        assert "0.5" in cmd
        watcher.stop()


# ---------------------------------------------------------------------------
# _reader
# ---------------------------------------------------------------------------

class TestReader:
    def _fake_proc(self, lines: list[str], returncode: int | None = None) -> MagicMock:
        proc = MagicMock()
        proc.stdout = iter(lines)
        proc.returncode = returncode
        return proc

    def test_valid_file_enqueues_event(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        book = incoming / "book.epub"
        book.write_text("stable content")

        watcher._proc = self._fake_proc([f"{book}\n"], returncode=0)
        watcher._reader()

        event = watcher._queue.get_nowait()
        assert event.path == book
        assert event.event_type == "created"

    def test_directory_event_uses_dir_stabilizer(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        folder = incoming / "audiobook"
        folder.mkdir(parents=True)
        (folder / "part1.m4b").write_bytes(b"x")

        watcher._proc = self._fake_proc([f"{folder}\n"], returncode=0)
        with patch("libris.watcher.fswatch_watcher._wait_stable_dir") as mock_wait_dir, \
             patch("libris.watcher.fswatch_watcher._wait_stable") as mock_wait_file:
            watcher._reader()

        mock_wait_dir.assert_called_once()
        mock_wait_file.assert_not_called()
        event = watcher._queue.get_nowait()
        assert event.path == folder

    def test_missing_path_is_filtered_out(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        gone = incoming / "gone.epub"

        watcher._proc = self._fake_proc([f"{gone}\n"], returncode=0)
        watcher._reader()

        assert watcher._queue.empty()

    def test_nonzero_unexpected_returncode_logs_error(self, tmp_path, caplog):
        import logging

        watcher = _make_watcher(tmp_path)
        watcher._proc = self._fake_proc([], returncode=1)

        with caplog.at_level(logging.ERROR, logger="libris.watcher.fswatch_watcher"):
            watcher._reader()

        assert "process_exited" in caplog.text

    def test_sigterm_returncode_does_not_log_error(self, tmp_path, caplog):
        import logging

        watcher = _make_watcher(tmp_path)
        watcher._proc = self._fake_proc([], returncode=-15)

        with caplog.at_level(logging.ERROR, logger="libris.watcher.fswatch_watcher"):
            watcher._reader()

        assert "process_exited" not in caplog.text


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStop:
    def test_terminates_running_process(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        watcher._proc = mock_proc
        watcher.stop()
        mock_proc.terminate.assert_called_once()
        assert watcher._stop_event.is_set()

    def test_kills_on_terminate_timeout(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="fswatch", timeout=3)
        watcher._proc = mock_proc
        watcher.stop()
        mock_proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# _wait_stable / _wait_stable_dir — real filesystem, tiny interval
# ---------------------------------------------------------------------------

class TestWaitStable:
    def test_returns_immediately_when_file_disappears(self, tmp_path):
        gone = tmp_path / "gone.epub"
        _wait_stable(gone, interval=0.01)  # must not raise / hang

    def test_returns_once_size_is_stable(self, tmp_path):
        f = tmp_path / "book.epub"
        f.write_bytes(b"x" * 10)
        _wait_stable(f, interval=0.01)  # size doesn't change between reads -> returns fast

    def test_logs_warning_on_timeout(self, tmp_path, monkeypatch, caplog):
        import logging

        import libris.watcher.fswatch_watcher as mod

        f = tmp_path / "book.epub"
        f.write_bytes(b"x")

        # Force _STABILIZE_MAX_WAIT down to 0 so the while loop's condition
        # (waited < _STABILIZE_MAX_WAIT) is false on the very first check.
        monkeypatch.setattr(mod, "_STABILIZE_MAX_WAIT", 0.0)

        with caplog.at_level(logging.WARNING, logger="libris.watcher.fswatch_watcher"):
            _wait_stable(f, interval=0.01)

        assert "stabilize_timeout" in caplog.text


class TestWaitStableDir:
    def test_returns_once_size_is_stable(self, tmp_path):
        d = tmp_path / "audiobook"
        d.mkdir()
        (d / "part1.m4b").write_bytes(b"x" * 100)
        _wait_stable_dir(d, interval=0.01)

    def test_returns_immediately_when_dir_disappears(self, tmp_path):
        gone = tmp_path / "gone_dir"
        _wait_stable_dir(gone, interval=0.01)  # rglob on missing dir raises OSError-ish -> caught

    def test_logs_warning_on_timeout(self, tmp_path, monkeypatch, caplog):
        import logging

        import libris.watcher.fswatch_watcher as mod

        d = tmp_path / "audiobook"
        d.mkdir()
        (d / "part1.m4b").write_bytes(b"x")

        monkeypatch.setattr(mod, "_STABILIZE_MAX_WAIT", 0.0)

        with caplog.at_level(logging.WARNING, logger="libris.watcher.fswatch_watcher"):
            _wait_stable_dir(d, interval=0.01)

        assert "dir_stabilize_timeout" in caplog.text
