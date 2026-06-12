"""Unit tests for LocalCalibre split-path helpers (_relocate_to_book_files, _relocate_cover)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# set_metadata split-mode directory sync
# ---------------------------------------------------------------------------

class TestSetMetadataSplitSync:
    """In split-library mode, set_metadata must mirror calibredb's directory
    rename under book_file_path.

    calibredb renames the book dir under library_db_path when title/authors
    change — but the physical files were already relocated to book_file_path,
    so the rename is a silent no-op on them while the DB path still updates.
    Without the sync, the physical dir keeps its wrong name forever (e.g.
    Books/Unknown/Book01-Merchant of Death (102)/) and desyncs from the DB.
    """

    def _make_result(self):
        result = MagicMock()
        result.title = "Pendragon: The Merchant Of Death"
        result.best.candidate.authors = ["D.J. MacHale"]
        result.publisher = None
        result.description = None
        result.language = None
        result.isbn = None
        result.series = None
        result.series_index = None
        return result

    def _run_with_paths(self, backend, old_path: str | None, new_path: str | None):
        """Drive set_metadata with calibredb list returning old_path before the
        set_metadata subprocess call and new_path after it."""
        responses = []

        def fake_run(cmd, **kwargs):
            if "set_metadata" in cmd:
                return _patch_run("")
            # calibredb list — first call returns old, later calls return new
            path = old_path if not responses else new_path
            responses.append(path)
            return _patch_run(
                _calibredb_formats_response(102, [path] if path else [])
            )

        with patch("subprocess.run", side_effect=fake_run):
            backend.set_metadata(102, self._make_result())

    def test_renames_physical_dir_when_db_path_changes(self, tmp_path):
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        old_dir = book_files / "Unknown" / "Book01-Merchant of Death (102)"
        old_dir.mkdir(parents=True)
        m4b = old_dir / "Book01-Merchant of Death - Unknown.m4b"
        m4b.write_bytes(b"audio")

        backend = _make_backend(library, book_files)

        self._run_with_paths(
            backend,
            old_path=str(library / "Unknown" / "Book01-Merchant of Death (102)" / "x.m4b"),
            new_path=str(library / "MacHale, D.J." / "Pendragon_ The Merchant Of Death (102)" / "x.m4b"),
        )

        new_dir = book_files / "MacHale, D.J." / "Pendragon_ The Merchant Of Death (102)"
        assert new_dir.exists(), "Physical dir must be renamed to match the DB path"
        assert (new_dir / "Book01-Merchant of Death - Unknown.m4b").exists()
        assert not old_dir.exists(), "Old dir must be gone"
        assert not (book_files / "Unknown").exists(), "Empty old author dir removed"

    def test_noop_when_rel_dir_unchanged(self, tmp_path):
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        book_dir = book_files / "MacHale, D.J." / "Pendragon (102)"
        book_dir.mkdir(parents=True)
        (book_dir / "book.m4b").write_bytes(b"audio")

        backend = _make_backend(library, book_files)
        same = str(library / "MacHale, D.J." / "Pendragon (102)" / "book.m4b")

        self._run_with_paths(backend, old_path=same, new_path=same)

        assert book_dir.exists(), "Unchanged path must not be touched"

    def test_noop_when_physical_dir_missing(self, tmp_path):
        """If the old dir doesn't exist under book_files, skip gracefully."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        backend = _make_backend(library, book_files)

        self._run_with_paths(
            backend,
            old_path=str(library / "Unknown" / "Gone (102)" / "x.m4b"),
            new_path=str(library / "Author" / "Title (102)" / "x.m4b"),
        )  # must not raise

        assert not (book_files / "Author").exists()

    def test_collision_skips_move_and_warns(self, tmp_path, caplog):
        """If the destination dir already exists, skip (avoid nesting)."""
        import logging

        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        old_dir = book_files / "Unknown" / "Book (102)"
        old_dir.mkdir(parents=True)
        (old_dir / "book.m4b").write_bytes(b"a")
        new_dir = book_files / "Author" / "Title (102)"
        new_dir.mkdir(parents=True)
        (new_dir / "existing.m4b").write_bytes(b"b")

        backend = _make_backend(library, book_files)

        with caplog.at_level(logging.WARNING):
            self._run_with_paths(
                backend,
                old_path=str(library / "Unknown" / "Book (102)" / "x.m4b"),
                new_path=str(library / "Author" / "Title (102)" / "x.m4b"),
            )

        assert old_dir.exists(), "Source must be untouched on collision"
        assert (new_dir / "existing.m4b").exists()
        assert "split_dir_sync_collision" in caplog.text

    def test_flat_mode_makes_no_list_calls(self, tmp_path):
        """When library == book_files, set_metadata runs exactly one subprocess."""
        library = tmp_path / "calibre"
        library.mkdir()
        backend = _make_backend(library)  # flat mode

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = _patch_run("")
            backend.set_metadata(1, self._make_result())

        assert mock_run.call_count == 1, (
            "Flat mode must not pay for the calibredb list round-trips"
        )

    def test_set_metadata_failure_skips_sync(self, tmp_path):
        """rc != 0 from calibredb set_metadata must skip the dir sync."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        old_dir = book_files / "Unknown" / "Book (102)"
        old_dir.mkdir(parents=True)

        backend = _make_backend(library, book_files)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if "set_metadata" in cmd:
                proc = _patch_run("", returncode=1)
                proc.stderr = "boom"
                return proc
            return _patch_run(
                _calibredb_formats_response(
                    102, [str(library / "Unknown" / "Book (102)" / "x.m4b")]
                )
            )

        with patch("subprocess.run", side_effect=fake_run):
            backend.set_metadata(102, self._make_result())

        # one list (before) + one set_metadata; no post-call list
        list_calls = [c for c in calls if "list" in c]
        assert len(list_calls) == 1, "No post-failure list call expected"
        assert old_dir.exists()


# ---------------------------------------------------------------------------
# list_books format_path enrichment from metadata.db (split-mode blindness fix)
# ---------------------------------------------------------------------------

class TestFormatPathEnrichment:
    """calibredb list --fields formats reports a format only when the file
    exists under the LIBRARY root — in split-library mode every relocated
    book comes back with NO formats.  list_books must fall back to reading
    books.path + data from metadata.db so file-locating consumers
    (get-covers, clean-library) see every book.

    Production case (June 2026): 84 of 90 books invisible to get-covers.
    """

    @staticmethod
    def _seed_metadata_db(library: Path, rows: list[tuple[int, str, str, str]]) -> None:
        """rows: (book_id, rel_path, name, FORMAT)."""
        library.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(library / "metadata.db")
        con.execute("CREATE TABLE books (id INTEGER PRIMARY KEY, path TEXT)")
        con.execute(
            "CREATE TABLE data (id INTEGER PRIMARY KEY, book INTEGER, format TEXT, name TEXT)"
        )
        for book_id, rel, name, fmt in rows:
            con.execute("INSERT OR IGNORE INTO books VALUES (?, ?)", (book_id, rel))
            con.execute("INSERT INTO data (book, format, name) VALUES (?, ?, ?)",
                        (book_id, fmt, name))
        con.commit()
        con.close()

    def _list_books(self, backend, calibredb_json: str):
        with patch("libris.calibre.local.subprocess.run",
                   return_value=_patch_run(calibredb_json)):
            return backend.list_books()

    def test_empty_formats_enriched_from_db(self, tmp_path):
        """The exact production failure: relocated book, calibredb says no formats."""
        library = tmp_path / "library"
        self._seed_metadata_db(library, [
            (88, "Christopher Paolini/The Fork, the Witch, and the Worm (88)",
             "The Fork, the Witch, and the Worm - Christopher Paolini", "M4B"),
        ])
        backend = _make_backend(library, tmp_path / "books")

        calibredb_json = json.dumps([{
            "id": 88, "title": "The Fork, the Witch, and the Worm",
            "authors": "Christopher Paolini", "formats": [],
        }])
        books = self._list_books(backend, calibredb_json)

        assert books[0]["format_paths"] == [str(
            library / "Christopher Paolini/The Fork, the Witch, and the Worm (88)"
                    / "The Fork, the Witch, and the Worm - Christopher Paolini.m4b"
        )]
        assert books[0]["formats"] == ["m4b"]

    def test_calibredb_reported_paths_left_untouched(self, tmp_path):
        library = tmp_path / "library"
        self._seed_metadata_db(library, [
            (1, "Author/Book (1)", "WRONG NAME FROM DB", "EPUB"),
        ])
        backend = _make_backend(library)

        reported = str(library / "Author/Book (1)/Book.epub")
        calibredb_json = json.dumps([{
            "id": 1, "title": "Book", "authors": "Author", "formats": [reported],
        }])
        books = self._list_books(backend, calibredb_json)

        assert books[0]["format_paths"] == [reported], \
            "non-empty calibredb paths must win over the DB fallback"

    def test_multiple_formats_enriched(self, tmp_path):
        library = tmp_path / "library"
        self._seed_metadata_db(library, [
            (2, "Author/Book (2)", "Book", "EPUB"),
            (2, "Author/Book (2)", "Book", "PDF"),
        ])
        backend = _make_backend(library, tmp_path / "books")

        calibredb_json = json.dumps([{
            "id": 2, "title": "Book", "authors": "Author", "formats": [],
        }])
        books = self._list_books(backend, calibredb_json)

        assert sorted(books[0]["formats"]) == ["epub", "pdf"]
        assert len(books[0]["format_paths"]) == 2

    def test_missing_metadata_db_degrades_gracefully(self, tmp_path):
        library = tmp_path / "library"
        library.mkdir()
        backend = _make_backend(library)

        calibredb_json = json.dumps([{
            "id": 3, "title": "Book", "authors": "Author", "formats": [],
        }])
        books = self._list_books(backend, calibredb_json)

        assert books[0]["format_paths"] == []  # no crash, just unenriched

    def test_book_absent_from_db_left_empty(self, tmp_path):
        library = tmp_path / "library"
        self._seed_metadata_db(library, [(7, "A/B (7)", "B", "EPUB")])
        backend = _make_backend(library)

        calibredb_json = json.dumps([{
            "id": 99, "title": "Ghost", "authors": "A", "formats": [],
        }])
        books = self._list_books(backend, calibredb_json)

        assert books[0]["format_paths"] == []
