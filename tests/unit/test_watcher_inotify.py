"""Unit tests for libris/watcher/inotify_watcher.py (InotifyWatcher).

0% covered before this file. Never spawns a real inotifywait process:
subprocess.Popen is mocked throughout, and _reader()/_should_process()/
_parse_event_type() (the pure/near-pure pieces) are driven directly with
fake stdout iterables and tmp_path directories.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from libris.config import WatcherConfig
from libris.exceptions import WatcherError
from libris.watcher.inotify_watcher import InotifyWatcher, _parse_event_type


def _make_watcher(tmp_path: Path) -> InotifyWatcher:
    cfg = WatcherConfig(incoming_dir=tmp_path / "incoming")
    return InotifyWatcher(cfg)


# ---------------------------------------------------------------------------
# _parse_event_type
# ---------------------------------------------------------------------------

class TestParseEventType:
    def test_moved_to_detected_case_insensitive(self):
        assert _parse_event_type("MOVED_TO") == "moved_to"
        assert _parse_event_type("moved_to") == "moved_to"

    def test_close_write_maps_to_created(self):
        assert _parse_event_type("CLOSE_WRITE,CLOSE") == "created"

    def test_isdir_moved_to_still_moved_to(self):
        assert _parse_event_type("MOVED_TO,ISDIR") == "moved_to"


# ---------------------------------------------------------------------------
# _should_process
# ---------------------------------------------------------------------------

class TestShouldProcess:
    def test_direct_child_processed(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        assert watcher._should_process(incoming / "book.epub") is True

    def test_hidden_file_skipped(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        assert watcher._should_process(incoming / ".hidden.epub") is False

    def test_nested_file_skipped(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        nested = incoming / "subdir" / "book.epub"
        assert watcher._should_process(nested) is False

    def test_direct_child_directory_processed(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        assert watcher._should_process(incoming / "audiobook_folder") is True


# ---------------------------------------------------------------------------
# _start_process
# ---------------------------------------------------------------------------

class TestStartProcess:
    def test_creates_incoming_dir_if_missing(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        assert not watcher._incoming_dir.exists()
        with patch("libris.watcher.inotify_watcher.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(stdout=iter([]))
            watcher._start_process()
        assert watcher._incoming_dir.exists()
        watcher.stop()

    def test_missing_inotifywait_raises_watcher_error(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        with patch(
            "libris.watcher.inotify_watcher.subprocess.Popen",
            side_effect=FileNotFoundError("inotifywait not found"),
        ):
            with pytest.raises(WatcherError, match="inotifywait not found"):
                watcher._start_process()

    def test_command_includes_recursive_and_events(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        with patch("libris.watcher.inotify_watcher.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(stdout=iter([]))
            watcher._start_process()
        cmd = mock_popen.call_args[0][0]
        assert cmd[0] == "inotifywait"
        assert "--recursive" in cmd
        assert "close_write" in cmd
        assert "moved_to" in cmd
        assert str(watcher._incoming_dir) in cmd
        watcher.stop()


# ---------------------------------------------------------------------------
# _reader — drive with a fake Popen whose stdout is a canned line iterator
# ---------------------------------------------------------------------------

class TestReader:
    def _fake_proc(self, lines: list[str], returncode: int | None = None) -> MagicMock:
        proc = MagicMock()
        proc.stdout = iter(lines)
        proc.returncode = returncode
        return proc

    def test_valid_line_enqueues_event(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        line = f"CLOSE_WRITE,CLOSE|{incoming}/book.epub\n"
        watcher._proc = self._fake_proc([line], returncode=0)

        watcher._reader()

        event = watcher._queue.get_nowait()
        assert event.event_type == "created"
        assert event.path == Path(f"{incoming}/book.epub")

    def test_moved_to_line_enqueues_moved_to_event(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        line = f"MOVED_TO|{incoming}/book.epub\n"
        watcher._proc = self._fake_proc([line], returncode=0)

        watcher._reader()

        event = watcher._queue.get_nowait()
        assert event.event_type == "moved_to"

    def test_line_without_pipe_is_skipped(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        watcher._proc = self._fake_proc(["garbage no pipe\n"], returncode=0)

        watcher._reader()

        assert watcher._queue.empty()

    def test_nested_path_filtered_by_should_process(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        line = f"CLOSE_WRITE|{incoming}/subdir/book.epub\n"
        watcher._proc = self._fake_proc([line], returncode=0)

        watcher._reader()

        assert watcher._queue.empty()

    def test_stop_event_set_breaks_loop_early(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()
        watcher._stop_event.set()
        line = f"CLOSE_WRITE|{incoming}/book.epub\n"
        watcher._proc = self._fake_proc([line], returncode=0)

        watcher._reader()

        assert watcher._queue.empty()

    def test_nonzero_unexpected_returncode_logs_error(self, tmp_path, caplog):
        import logging

        watcher = _make_watcher(tmp_path)
        watcher._proc = self._fake_proc([], returncode=1)

        with caplog.at_level(logging.ERROR, logger="libris.watcher.inotify_watcher"):
            watcher._reader()

        assert "process_exited" in caplog.text

    def test_sigterm_returncode_does_not_log_error(self, tmp_path, caplog):
        """-15 (SIGTERM, from our own stop()) is an expected exit, not an error."""
        import logging

        watcher = _make_watcher(tmp_path)
        watcher._proc = self._fake_proc([], returncode=-15)

        with caplog.at_level(logging.ERROR, logger="libris.watcher.inotify_watcher"):
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

        assert watcher._stop_event.is_set()
        mock_proc.terminate.assert_called_once()

    def test_kills_on_terminate_timeout(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        mock_proc = MagicMock()
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="inotifywait", timeout=3)
        watcher._proc = mock_proc

        watcher.stop()

        mock_proc.kill.assert_called_once()

    def test_stop_without_started_process_is_safe(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        watcher.stop()  # _proc is None — must not raise
        assert watcher._stop_event.is_set()


# ---------------------------------------------------------------------------
# events() — end-to-end with a real background thread + queue
# ---------------------------------------------------------------------------

class TestEventsEndToEnd:
    def test_yields_queued_events_then_stops(self, tmp_path):
        watcher = _make_watcher(tmp_path)
        incoming = tmp_path / "incoming"
        incoming.mkdir()

        with patch("libris.watcher.inotify_watcher.subprocess.Popen") as mock_popen:
            # stdout is read line-by-line by _reader in a background thread —
            # give it one valid line, then block (empty iterator ends the for
            # loop immediately after, which is fine for this test).
            mock_popen.return_value = MagicMock(
                stdout=iter([f"CLOSE_WRITE|{incoming}/book.epub\n"]),
                returncode=0,
            )

            gen = watcher.events()
            first = next(gen)
            watcher.stop()

        assert first.path == Path(f"{incoming}/book.epub")
        assert first.event_type == "created"
