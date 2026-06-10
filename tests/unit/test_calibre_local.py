"""Unit tests for LocalCalibre split-path helpers (_relocate_to_book_files, _relocate_cover)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from libris.calibre.local import LocalCalibre
from libris.config import CalibreConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_backend(library: Path, book_files: Path | None = None) -> LocalCalibre:
    """Build a LocalCalibre instance pointing at *library* with optional split path."""
    cfg = CalibreConfig(
        mode="local",
        library_db_path=library,
        book_file_path=book_files,
    )
    return LocalCalibre(cfg)


def _calibredb_formats_response(book_id: int, paths: list[str]) -> str:
    """Build the JSON string that calibredb list --for-machine would return."""
    return json.dumps([{"id": book_id, "formats": paths}])


def _patch_run(stdout: str, returncode: int = 0):
    """Return a mock subprocess.CompletedProcess with the given stdout."""
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = ""
    return mock


# ---------------------------------------------------------------------------
# _relocate_to_book_files — happy path
# ---------------------------------------------------------------------------

class TestRelocateToBookFiles:
    def test_moves_format_file(self, tmp_path):
        """Format file is moved from library to book_files."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        # Seed a realistic calibredb book directory
        book_dir = library / "Andy Weir" / "The Martian (1)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "the-martian.epub"
        epub.write_text("epub content")
        cover = book_dir / "cover.jpg"
        cover.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header
        opf = book_dir / "metadata.opf"
        opf.write_text("<package/>")

        backend = _make_backend(library, book_files)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run(
                _calibredb_formats_response(1, [str(epub)])
            )
            backend._relocate_to_book_files(1)

        dest_dir = book_files / "Andy Weir" / "The Martian (1)"
        assert (dest_dir / "the-martian.epub").exists(), "Format file must be moved"

    def test_moves_cover_jpg(self, tmp_path):
        """cover.jpg is moved together with the format file."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        book_dir = library / "Andy Weir" / "The Martian (1)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "the-martian.epub"
        epub.write_text("epub")
        cover = book_dir / "cover.jpg"
        cover.write_bytes(b"\xff\xd8\xff")

        backend = _make_backend(library, book_files)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run(
                _calibredb_formats_response(1, [str(epub)])
            )
            backend._relocate_to_book_files(1)

        dest_cover = book_files / "Andy Weir" / "The Martian (1)" / "cover.jpg"
        assert dest_cover.exists(), "cover.jpg must be relocated"
        assert dest_cover.read_bytes() == b"\xff\xd8\xff"

    def test_moves_metadata_opf(self, tmp_path):
        """metadata.opf is moved together with the format file."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        book_dir = library / "Andy Weir" / "The Martian (1)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "the-martian.epub"
        epub.write_text("epub")
        opf = book_dir / "metadata.opf"
        opf.write_text("<package/>")

        backend = _make_backend(library, book_files)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run(
                _calibredb_formats_response(1, [str(epub)])
            )
            backend._relocate_to_book_files(1)

        dest_opf = book_files / "Andy Weir" / "The Martian (1)" / "metadata.opf"
        assert dest_opf.exists(), "metadata.opf must be relocated"

    def test_source_directory_removed_after_move(self, tmp_path):
        """The now-empty book directory under library_db_path is removed."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        book_dir = library / "Andy Weir" / "The Martian (1)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "the-martian.epub"
        epub.write_text("epub")

        backend = _make_backend(library, book_files)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run(
                _calibredb_formats_response(1, [str(epub)])
            )
            backend._relocate_to_book_files(1)

        assert not book_dir.exists(), "Empty book dir in library must be removed"

    def test_files_not_present_in_library_after_move(self, tmp_path):
        """Source files are gone after relocation (move, not copy)."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        book_dir = library / "Andy Weir" / "The Martian (1)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "the-martian.epub"
        epub.write_text("epub")
        cover = book_dir / "cover.jpg"
        cover.write_bytes(b"\xff\xd8")

        backend = _make_backend(library, book_files)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run(
                _calibredb_formats_response(1, [str(epub)])
            )
            backend._relocate_to_book_files(1)

        assert not epub.exists(), "Source epub must be gone (moved)"
        assert not cover.exists(), "Source cover.jpg must be gone (moved)"

    def test_no_formats_logs_warning_and_returns(self, tmp_path, caplog):
        """If calibredb returns no formats, the method logs a warning and returns cleanly."""
        import logging
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        backend = _make_backend(library, book_files)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run("[]")
            with caplog.at_level(logging.WARNING, logger="libris.calibre.local"):
                backend._relocate_to_book_files(99)

        assert "relocate_no_formats" in caplog.text

    def test_flat_mode_no_relocation(self, tmp_path):
        """When book_file_path == library_db_path, _relocate_to_book_files is not called."""
        library = tmp_path / "calibre"
        library.mkdir()

        book_dir = library / "Andy Weir" / "The Martian (1)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "the-martian.epub"
        epub.write_text("epub")

        backend = _make_backend(library)  # no book_files → same as library

        # add_book calls _relocate_to_book_files only when _book_files != _library.
        # Confirm _book_files equals _library so no subprocess call for relocation.
        assert backend._book_files == backend._library

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run("Added book ids: 1\n")
            backend.add_book(epub)

        # calibredb was called once (the add), not for relocation
        assert mock_run.call_count == 1


# ---------------------------------------------------------------------------
# _relocate_cover — happy path
# ---------------------------------------------------------------------------

class TestRelocateCover:
    def test_moves_cover_from_library_to_book_files(self, tmp_path):
        """cover.jpg in library_db_path is moved to book_file_path."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        book_dir = library / "Brandon Sanderson" / "Mistborn (2)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "mistborn.epub"
        epub.write_text("epub content")
        cover = book_dir / "cover.jpg"
        cover.write_bytes(b"\xff\xd8\xff\xe0")  # JPEG SOI + APP0

        backend = _make_backend(library, book_files)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run(
                _calibredb_formats_response(2, [str(epub)])
            )
            backend._relocate_cover(2)

        dest_cover = book_files / "Brandon Sanderson" / "Mistborn (2)" / "cover.jpg"
        assert dest_cover.exists(), "cover.jpg must exist at book_files destination"
        assert dest_cover.read_bytes() == b"\xff\xd8\xff\xe0"

    def test_cover_removed_from_library_after_move(self, tmp_path):
        """Source cover.jpg is gone after _relocate_cover (move, not copy)."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        book_dir = library / "Brandon Sanderson" / "Mistborn (2)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "mistborn.epub"
        epub.write_text("epub")
        cover = book_dir / "cover.jpg"
        cover.write_bytes(b"\xff\xd8")

        backend = _make_backend(library, book_files)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run(
                _calibredb_formats_response(2, [str(epub)])
            )
            backend._relocate_cover(2)

        assert not cover.exists(), "Source cover.jpg must be removed after move"

    def test_no_cover_no_error(self, tmp_path, caplog):
        """If cover.jpg doesn't exist in library, _relocate_cover logs debug and returns."""
        import logging
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        book_dir = library / "Brandon Sanderson" / "Mistborn (2)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "mistborn.epub"
        epub.write_text("epub")
        # No cover.jpg seeded

        backend = _make_backend(library, book_files)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run(
                _calibredb_formats_response(2, [str(epub)])
            )
            with caplog.at_level(logging.DEBUG, logger="libris.calibre.local"):
                backend._relocate_cover(2)  # should not raise

        assert "cover_relocate_not_found" in caplog.text


# ---------------------------------------------------------------------------
# set_cover integration — _relocate_cover called in split-path mode
# ---------------------------------------------------------------------------

class TestSetCoverRelocates:
    def test_set_cover_triggers_relocate_in_split_mode(self, tmp_path):
        """set_cover() moves cover.jpg to book_files when split-path is active."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        # Simulate: after calibredb set_metadata writes the cover, it appears in library
        book_dir = library / "Jim Butcher" / "Storm Front (3)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "storm-front.epub"
        epub.write_text("epub")
        cover_src = book_dir / "cover.jpg"
        cover_in = tmp_path / "cover_input.jpg"
        cover_in.write_bytes(b"\xff\xd8\xff")  # the cover libris downloaded

        backend = _make_backend(library, book_files)

        def fake_run(cmd, **kwargs):
            # First call: calibredb set_metadata (writes cover.jpg to library)
            if "set_metadata" in cmd:
                cover_src.write_bytes(b"\xff\xd8\xff")
                return _patch_run("")
            # Second call: calibredb list --for-machine (for _get_format_paths)
            return _patch_run(_calibredb_formats_response(3, [str(epub)]))

        with patch("subprocess.run", side_effect=fake_run):
            backend.set_cover(3, cover_in)

        dest_cover = book_files / "Jim Butcher" / "Storm Front (3)" / "cover.jpg"
        assert dest_cover.exists(), "set_cover must relocate cover.jpg to book_files"
        assert not cover_src.exists(), "Source cover.jpg must be gone after set_cover"

    def test_set_cover_no_relocate_in_flat_mode(self, tmp_path):
        """set_cover() does NOT call _relocate_cover when library == book_files."""
        library = tmp_path / "calibre"
        library.mkdir()
        cover_in = tmp_path / "cover.jpg"
        cover_in.write_bytes(b"\xff\xd8")

        backend = _make_backend(library)  # flat mode

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run("")
            with patch.object(backend, "_relocate_cover") as mock_reloc:
                backend.set_cover(1, cover_in)
                mock_reloc.assert_not_called()


# ---------------------------------------------------------------------------
# add_book --title/--authors flags
# ---------------------------------------------------------------------------

class TestAddBookMetadataFlags:
    """add_book must pass --title/--authors to calibredb add when provided.

    These flags determine the directory calibredb creates
    ({author_sort}/{title} ({id})/).  Without them calibredb parses the
    FILENAME as "{title} - {author}" — it never reads embedded M4B audio
    tags — so "Book01-Merchant of Death.m4b" landed in
    Books/Unknown/Book01-Merchant of Death (102)/.
    """

    def test_title_and_authors_in_command(self, tmp_path):
        library = tmp_path / "calibre"
        library.mkdir()
        epub = tmp_path / "Book01-Merchant of Death.m4b"
        epub.write_bytes(b"audio")
        backend = _make_backend(library)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run("Added book ids: 102\n")
            backend.add_book(
                epub,
                title="Pendragon: The Merchant Of Death",
                authors="D.J. MacHale",
            )

        cmd = mock_run.call_args_list[0].args[0]
        assert "--title" in cmd
        assert cmd[cmd.index("--title") + 1] == "Pendragon: The Merchant Of Death"
        assert "--authors" in cmd
        assert cmd[cmd.index("--authors") + 1] == "D.J. MacHale"

    def test_flags_omitted_when_none(self, tmp_path):
        """No --title/--authors flags when not provided (back-compat)."""
        library = tmp_path / "calibre"
        library.mkdir()
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"epub")
        backend = _make_backend(library)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run("Added book ids: 1\n")
            backend.add_book(epub)

        cmd = mock_run.call_args_list[0].args[0]
        assert "--title" not in cmd
        assert "--authors" not in cmd

    def test_flags_omitted_when_empty_string(self, tmp_path):
        """Empty strings are treated like None — flags omitted."""
        library = tmp_path / "calibre"
        library.mkdir()
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"epub")
        backend = _make_backend(library)

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run("Added book ids: 1\n")
            backend.add_book(epub, title="", authors="")

        cmd = mock_run.call_args_list[0].args[0]
        assert "--title" not in cmd
        assert "--authors" not in cmd

    def test_docker_backend_mirrors_flags(self, tmp_path):
        """DockerCalibre.add_book passes the same flags inside docker exec."""
        from libris.calibre.docker import DockerCalibre

        backend = DockerCalibre.__new__(DockerCalibre)
        backend._container = "calibre-web"
        backend._path_map = []
        epub = tmp_path / "book.m4b"
        epub.write_bytes(b"audio")

        with patch.object(DockerCalibre, "_translate", return_value="/incoming/book.m4b"):
            with patch("libris.calibre.docker.subprocess.run") as mock_run:
                mock_run.return_value = _patch_run("Added book ids: 7\n")
                backend.add_book(epub, title="Brisingr", authors="Christopher Paolini")

        cmd = mock_run.call_args_list[0].args[0]
        assert "--title" in cmd
        assert cmd[cmd.index("--title") + 1] == "Brisingr"
        assert "--authors" in cmd
        assert cmd[cmd.index("--authors") + 1] == "Christopher Paolini"


# ---------------------------------------------------------------------------
# format_authors helper
# ---------------------------------------------------------------------------

class TestFormatAuthors:
    def test_single_author(self):
        from libris.calibre.base import format_authors
        assert format_authors(["D.J. MacHale"]) == "D.J. MacHale"

    def test_multiple_authors_ampersand_joined(self):
        """Calibre's multi-author separator is ' & ' — never ', ' (parsed as
        a single inverted 'Surname, Given' name)."""
        from libris.calibre.base import format_authors
        assert format_authors(["Terry Pratchett", "Neil Gaiman"]) == (
            "Terry Pratchett & Neil Gaiman"
        )

    def test_metadata_flags_uses_same_join(self):
        """_metadata_flags authors field must match format_authors output."""
        from libris.calibre.local import _metadata_flags

        result = MagicMock()
        result.title = "Good Omens"
        result.best.candidate.authors = ["Terry Pratchett", "Neil Gaiman"]
        result.publisher = None
        result.description = None
        result.language = None
        result.isbn = None
        result.series = None
        result.series_index = None

        flags = [f for pair in _metadata_flags(result) for f in pair]
        assert "authors:Terry Pratchett & Neil Gaiman" in flags
