"""Tests for issue #49 — calibredb export must include --single-dir flag.

Root cause: export_book in both libris/calibre/local.py and libris/calibre/docker.py
called `calibredb export` without `--single-dir`.  Without that flag, calibredb
creates per-book subdirectories inside dest_dir.  In certain conditions (split-library
mode, path mismatches, calibredb quirks) no files appear directly in dest_dir —
rglob returns empty even though rc=0, causing revert-import to report "no files".

Fix:
- Add `--single-dir` to the calibredb export command in both backends.
- Add `f.is_file()` guard to the rglob filter (avoids returning directories with
  book-like names and is correct defensive practice).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_completed_process(returncode: int = 0, stdout: str = "", stderr: str = ""):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


# ---------------------------------------------------------------------------
# Tests: local backend
# ---------------------------------------------------------------------------

class TestLocalExportSingleDir:
    """libris.calibre.local.LocalCalibre.export_book must pass --single-dir."""

    def _make_local(self, library_path: Path):
        from libris.calibre.local import LocalCalibre  # noqa: PLC0415
        inst = LocalCalibre.__new__(LocalCalibre)
        inst._library = library_path
        inst._book_files = library_path
        return inst

    def test_single_dir_flag_in_command(self, tmp_path):
        """--single-dir appears in the calibredb export command."""
        local = self._make_local(tmp_path / "library")

        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return _fake_completed_process()

        dest = tmp_path / "export"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run", side_effect=fake_run):
            local.export_book(1, dest)

        assert captured_cmds, "subprocess.run was not called"
        export_cmd = captured_cmds[0]
        assert "--single-dir" in export_cmd, (
            f"--single-dir missing from export command: {export_cmd}"
        )

    def test_to_dir_points_to_dest(self, tmp_path):
        """--to-dir is set to dest_dir in the export command."""
        local = self._make_local(tmp_path / "library")
        dest = tmp_path / "export"
        dest.mkdir()
        captured_cmds = []

        with patch("libris.calibre.local.subprocess.run",
                   side_effect=lambda cmd, **kw: captured_cmds.append(cmd) or _fake_completed_process()):
            local.export_book(42, dest)

        cmd = captured_cmds[0]
        assert "--to-dir" in cmd
        idx = cmd.index("--to-dir")
        assert cmd[idx + 1] == str(dest), f"--to-dir value wrong: {cmd[idx+1]}"

    def test_returns_files_from_dest_dir(self, tmp_path):
        """Returns files found in dest_dir after export."""
        local = self._make_local(tmp_path / "library")
        dest = tmp_path / "export"
        dest.mkdir()

        # Simulate calibredb writing a flat file into dest_dir (as --single-dir would do)
        book_file = dest / "Brisingr.m4b"
        book_file.write_bytes(b"fake")

        with patch("libris.calibre.local.subprocess.run", return_value=_fake_completed_process()):
            result = local.export_book(100, dest)

        assert book_file in result, f"Expected {book_file} in result, got {result}"

    def test_directories_not_returned(self, tmp_path):
        """Directories are excluded from the returned list (f.is_file() guard)."""
        local = self._make_local(tmp_path / "library")
        dest = tmp_path / "export"
        dest.mkdir()

        # A subdirectory with a book-like name — should NOT be returned
        subdir = dest / "Brisingr"
        subdir.mkdir()
        # A real file — should be returned
        real_file = dest / "Brisingr.m4b"
        real_file.write_bytes(b"fake")

        with patch("libris.calibre.local.subprocess.run", return_value=_fake_completed_process()):
            result = local.export_book(100, dest)

        assert real_file in result, "Real .m4b file should be in result"
        assert subdir not in result, "Directory should not be in result"

    def test_nonzero_rc_raises(self, tmp_path):
        """A non-zero returncode from calibredb raises CalibreImportError."""
        from libris.exceptions import CalibreImportError
        local = self._make_local(tmp_path / "library")
        dest = tmp_path / "export"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_completed_process(returncode=1, stderr="not found")):
            with pytest.raises(CalibreImportError):
                local.export_book(999, dest)

    def test_dest_dir_created_if_missing(self, tmp_path):
        """export_book creates dest_dir if it doesn't exist yet."""
        local = self._make_local(tmp_path / "library")
        dest = tmp_path / "new_export_dir"
        assert not dest.exists()

        with patch("libris.calibre.local.subprocess.run", return_value=_fake_completed_process()):
            local.export_book(1, dest)

        assert dest.exists(), "dest_dir should have been created"

    def test_book_id_in_command(self, tmp_path):
        """The numeric book ID is passed as a positional argument."""
        local = self._make_local(tmp_path / "library")
        dest = tmp_path / "export"
        dest.mkdir()
        captured_cmds = []

        with patch("libris.calibre.local.subprocess.run",
                   side_effect=lambda cmd, **kw: captured_cmds.append(cmd) or _fake_completed_process()):
            local.export_book(777, dest)

        assert "777" in captured_cmds[0], f"Book ID 777 not found in command: {captured_cmds[0]}"


# ---------------------------------------------------------------------------
# Tests: docker backend
# ---------------------------------------------------------------------------

class TestDockerExportSingleDir:
    """libris.calibre.docker.DockerCalibre.export_book must pass --single-dir."""

    def _make_docker(self):
        from libris.calibre.docker import DockerCalibre  # noqa: PLC0415
        inst = DockerCalibre.__new__(DockerCalibre)
        inst._container = "calibre-web"
        inst._library = Path("/calibre")
        return inst

    def _patch_docker_run(self, side_effects):
        """Return a side_effect function that returns responses in sequence."""
        responses = iter(side_effects)

        def fake_run(cmd, **kwargs):
            return next(responses)

        return fake_run

    def test_single_dir_flag_in_export_command(self, tmp_path):
        """--single-dir appears in the docker exec calibredb export command."""
        docker = self._make_docker()
        dest = tmp_path / "export"
        dest.mkdir()
        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            if "cp" in cmd:
                return _fake_completed_process()
            return _fake_completed_process()

        with patch("libris.calibre.docker.subprocess.run", side_effect=fake_run):
            docker.export_book(1, dest)

        export_cmd = next(
            (cmd for cmd in captured_cmds if "calibredb" in cmd and "export" in cmd),
            None,
        )
        assert export_cmd is not None, "No calibredb export command was found"
        assert "--single-dir" in export_cmd, (
            f"--single-dir missing from docker export command: {export_cmd}"
        )

    def test_docker_nonzero_rc_raises(self, tmp_path):
        """Non-zero calibredb return code inside docker raises CalibreImportError."""
        from libris.exceptions import CalibreImportError
        docker = self._make_docker()
        dest = tmp_path / "export"
        dest.mkdir()

        with patch("libris.calibre.docker.subprocess.run",
                   return_value=_fake_completed_process(returncode=1, stderr="no book")):
            with pytest.raises(CalibreImportError):
                docker.export_book(999, dest)

    def test_directories_not_returned_docker(self, tmp_path):
        """Directories are excluded from result even in docker path (f.is_file() guard)."""
        docker = self._make_docker()
        dest = tmp_path / "export"
        dest.mkdir()

        # Subdir should not be returned; flat file should be
        subdir = dest / "MyBook"
        subdir.mkdir()
        real_file = dest / "MyBook.epub"
        real_file.write_bytes(b"epub content")

        with patch("libris.calibre.docker.subprocess.run", return_value=_fake_completed_process()):
            result = docker.export_book(1, dest)

        assert real_file in result, "Real .epub file should be in result"
        assert subdir not in result, "Directory should not be in result"
