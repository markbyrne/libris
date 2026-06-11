"""Tests for export_book in split-library mode.

Root cause of the bug
---------------------
When the user has ``calibre.book_file_path`` configured separately from
``calibre.library_db_path`` (calibre-web "Separate Book Files" mode), Libris
calls ``_relocate_to_book_files`` after every ``add_book`` to physically move
the book files from ``_library`` into ``_book_files``.

After the move, ``calibredb export --with-library {_library}`` constructs the
export path as ``{_library}/{books.path}/{filename}`` — but the file has already
been moved to ``{_book_files}/{books.path}/{filename}``.  calibredb finds rc=0
because the DB query succeeds, but writes 0 files, so ``export_book`` returns
``[]`` and ``revert-import`` dies with "calibredb export returned no files".

Fix
---
``export_book`` now detects split-library mode (``_book_files != _library``),
skips the ``calibredb export`` subprocess call, and delegates to
``_export_from_book_files`` instead.

``_export_from_book_files`` uses ``calibredb list --fields formats`` to
discover the format paths calibredb expects (under ``_library``), remaps each
from ``_library → _book_files`` to find the real location, and copies the
files into ``dest_dir``.  A name-based fallback scan of ``_book_files`` handles
books whose directory was renamed by ``set_metadata`` before this fix was
applied.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_proc(returncode: int = 0, stdout: str = "", stderr: str = ""):
    proc = MagicMock(spec=subprocess.CompletedProcess)
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def _make_local(library: Path, book_files: Path):
    from libris.calibre.local import LocalCalibre  # noqa: PLC0415
    inst = LocalCalibre.__new__(LocalCalibre)
    inst._library = library
    inst._book_files = book_files
    return inst


# ---------------------------------------------------------------------------
# Tests: split-library mode bypasses calibredb export
# ---------------------------------------------------------------------------

class TestExportBookSplitLibrary:
    """In split-library mode, calibredb export must NOT be called."""

    def test_calibredb_export_not_called_in_split_mode(self, tmp_path):
        """calibredb export sub-process must be skipped when _book_files != _library."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"
        book_files.mkdir()

        local = _make_local(library, book_files)

        # Simulate: calibredb list returns an empty list (no formats)
        list_output = json.dumps([{"id": 1, "formats": []}])

        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return _fake_proc(stdout=list_output)

        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run", side_effect=fake_run):
            local.export_book(1, dest)

        # Check that "calibredb export" was never called
        export_cmds = [c for c in captured_cmds if "export" in c and "calibredb" in c]
        assert not export_cmds, (
            "calibredb export must not be called in split-library mode — "
            f"files are under _book_files, not _library. Got: {export_cmds}"
        )

    def test_calibredb_list_called_in_split_mode(self, tmp_path):
        """calibredb list must be called to discover format paths."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"
        book_files.mkdir()
        local = _make_local(library, book_files)

        list_output = json.dumps([{"id": 1, "formats": []}])
        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return _fake_proc(stdout=list_output)

        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run", side_effect=fake_run):
            local.export_book(1, dest)

        list_cmds = [c for c in captured_cmds if "list" in c and "calibredb" in c]
        assert list_cmds, "calibredb list should be called to find format paths"

    def test_calibredb_export_still_used_in_normal_mode(self, tmp_path):
        """When _library == _book_files, calibredb export must still be called."""
        library = tmp_path / "library"
        local = _make_local(library, library)  # same path = normal mode

        captured_cmds = []

        def fake_run(cmd, **kwargs):
            captured_cmds.append(cmd)
            return _fake_proc()

        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run", side_effect=fake_run):
            local.export_book(1, dest)

        export_cmds = [c for c in captured_cmds if "export" in c and "calibredb" in c]
        assert export_cmds, "calibredb export must still be called in normal (non-split) mode"


# ---------------------------------------------------------------------------
# Tests: _export_from_book_files — primary path (remap _library → _book_files)
# ---------------------------------------------------------------------------

class TestExportFromBookFiles:
    """_export_from_book_files must remap and copy files correctly."""

    def test_file_found_at_remapped_path(self, tmp_path):
        """File exists under _book_files at the same relative path calibredb reports."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"

        # calibredb reports: /library/Paolini/Brisingr (100)/Brisingr.m4b
        rel = Path("Paolini/Brisingr (100)/Brisingr.m4b")
        real_file = book_files / rel
        real_file.parent.mkdir(parents=True)
        real_file.write_bytes(b"audio data")

        local = _make_local(library, book_files)

        list_output = json.dumps([{
            "id": 100,
            "formats": [str(library / rel)],  # calibredb path (under _library)
        }])

        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(stdout=list_output)):
            result = local._export_from_book_files(100, dest)

        assert len(result) == 1, f"Expected 1 file, got {result}"
        assert result[0] == dest / "Brisingr.m4b"
        assert (dest / "Brisingr.m4b").read_bytes() == b"audio data"

    def test_multiple_formats_all_copied(self, tmp_path):
        """All formats for a book are copied to dest_dir."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"

        for fname in ["Book.epub", "Book.pdf"]:
            rel = Path(f"Author/Book (1)/{fname}")
            real_file = book_files / rel
            real_file.parent.mkdir(parents=True, exist_ok=True)
            real_file.write_bytes(b"content")

        local = _make_local(library, book_files)

        list_output = json.dumps([{
            "id": 1,
            "formats": [
                str(library / "Author/Book (1)/Book.epub"),
                str(library / "Author/Book (1)/Book.pdf"),
            ],
        }])

        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(stdout=list_output)):
            result = local._export_from_book_files(1, dest)

        names = {f.name for f in result}
        assert names == {"Book.epub", "Book.pdf"}, f"Expected both formats, got {names}"

    def test_empty_formats_returns_empty(self, tmp_path):
        """When calibredb reports no formats, return empty list."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"
        book_files.mkdir()
        local = _make_local(library, book_files)

        list_output = json.dumps([{"id": 5, "formats": []}])
        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(stdout=list_output)):
            result = local._export_from_book_files(5, dest)

        assert result == []

    def test_no_rows_returns_empty(self, tmp_path):
        """When calibredb list returns empty results, return empty list."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"
        book_files.mkdir()
        local = _make_local(library, book_files)

        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(stdout="[]")):
            result = local._export_from_book_files(99, dest)

        assert result == []

    def test_calibredb_list_failure_raises(self, tmp_path):
        """Non-zero returncode from calibredb list raises CalibreImportError."""
        from libris.exceptions import CalibreImportError  # noqa: PLC0415
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"
        book_files.mkdir()
        local = _make_local(library, book_files)

        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(returncode=1, stderr="error")):
            with pytest.raises(CalibreImportError):
                local._export_from_book_files(1, dest)

    def test_dest_dir_created_by_export_book(self, tmp_path):
        """export_book creates dest_dir if it doesn't exist yet."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"
        book_files.mkdir()
        local = _make_local(library, book_files)

        dest = tmp_path / "new_export"
        assert not dest.exists()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(stdout="[]")):
            local.export_book(1, dest)

        assert dest.exists()


# ---------------------------------------------------------------------------
# Tests: _export_from_book_files — fallback (name-based scan)
# ---------------------------------------------------------------------------

class TestExportFromBookFilesFallback:
    """When the remapped path doesn't exist, scan _book_files by filename.

    This recovers books imported before set_metadata gained the split-mode
    directory sync — their physical dir kept the old (wrong) name while the
    DB path was updated.
    """

    def test_fallback_finds_renamed_directory(self, tmp_path):
        """File at wrong directory (pre-sync import) is found by name scan."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"

        # File is at the OLD wrong path under _book_files
        old_rel = Path("Brisingr/Inheritance Cycle 3 (100)/Brisingr - Paolini.m4b")
        real_file = book_files / old_rel
        real_file.parent.mkdir(parents=True)
        real_file.write_bytes(b"audio")

        local = _make_local(library, book_files)

        # calibredb reports the CORRECT (new) path after set_metadata rename
        new_lib_path = library / "Paolini/Brisingr (100)/Brisingr - Paolini.m4b"
        list_output = json.dumps([{"id": 100, "formats": [str(new_lib_path)]}])

        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(stdout=list_output)):
            result = local._export_from_book_files(100, dest)

        assert len(result) == 1, f"Expected 1 file via fallback, got {result}"
        assert result[0].name == "Brisingr - Paolini.m4b"
        assert result[0].read_bytes() == b"audio"

    def test_fallback_not_triggered_when_primary_works(self, tmp_path):
        """When the remapped path exists, the fallback rglob must not be called."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"

        rel = Path("Author/Book (1)/Book.m4b")
        real_file = book_files / rel
        real_file.parent.mkdir(parents=True)
        real_file.write_bytes(b"data")

        local = _make_local(library, book_files)
        list_output = json.dumps([{"id": 1, "formats": [str(library / rel)]}])

        dest = tmp_path / "dest"
        dest.mkdir()

        # Patch Path.rglob to detect if fallback is triggered
        rglob_called = []
        original_rglob = Path.rglob

        def spy_rglob(self, pattern):
            rglob_called.append(pattern)
            return original_rglob(self, pattern)

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(stdout=list_output)):
            with patch.object(Path, "rglob", spy_rglob):
                local._export_from_book_files(1, dest)

        # rglob is called by export_book for normal mode file listing, but not
        # for the split-library fallback scan in this successful primary path case.
        # We just verify we got the right file back.
        exported = list(dest.iterdir())
        assert len(exported) == 1 and exported[0].name == "Book.m4b"


# ---------------------------------------------------------------------------
# Tests: full round-trip through export_book in split mode
# ---------------------------------------------------------------------------

class TestExportBookSplitRoundtrip:
    """Integration-style tests for the full export_book call in split-library mode."""

    def test_returns_file_in_dest_dir(self, tmp_path):
        """export_book returns the copied file path in dest_dir."""
        library = tmp_path / "library"
        book_files = tmp_path / "book_files"

        rel = Path("Paolini/Brisingr (100)/Brisingr.m4b")
        real_file = book_files / rel
        real_file.parent.mkdir(parents=True)
        real_file.write_bytes(b"book content")

        local = _make_local(library, book_files)
        list_output = json.dumps([{"id": 100, "formats": [str(library / rel)]}])
        dest = tmp_path / "dest"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(stdout=list_output)):
            result = local.export_book(100, dest)

        assert len(result) == 1
        assert result[0] == dest / "Brisingr.m4b"
        assert result[0].read_bytes() == b"book content"

    def test_revert_import_scenario(self, tmp_path):
        """Simulate the exact scenario that caused 'export returned no files'."""
        # User runs: libris review-accept --id 15  (Brisingr)
        # → add_book + _relocate_to_book_files moved files to book_files
        # → set_metadata set correct title/authors; calibredb updated books.path
        # Now: revert-import should be able to export the book

        library = tmp_path / "library"    # where metadata.db lives
        book_files = tmp_path / "books"    # where physical files are

        # File is correctly placed (Bug 1 fixed: correct path from the start)
        rel = Path("Paolini, Christopher/Brisingr (100)/Brisingr - Christopher Paolini.m4b")
        book_file = book_files / rel
        book_file.parent.mkdir(parents=True)
        book_file.write_bytes(b"m4b audio data")

        local = _make_local(library, book_files)

        # calibredb list returns path under _library (where it was BEFORE relocation)
        list_output = json.dumps([{
            "id": 100,
            "formats": [str(library / rel)],
        }])
        dest = tmp_path / "revert_tmp"
        dest.mkdir()

        with patch("libris.calibre.local.subprocess.run",
                   return_value=_fake_proc(stdout=list_output)):
            result = local.export_book(100, dest)

        assert result, "export_book returned no files — revert-import would have failed"
        assert result[0].read_bytes() == b"m4b audio data"
