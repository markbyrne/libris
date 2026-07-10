"""Coverage-gap tests for libris/calibre/local.py.

Mirrors the mocking style in test_calibre_local.py (patch subprocess.run,
build MagicMock CompletedProcess-alikes, use tmp_path for all filesystem
side effects). Targets the LocalCalibre methods/branches that
test_calibre_local.py, test_author_merge_client.py, test_export_split_library.py
and test_export_single_dir.py do not already exercise: remove_book, search,
add_format, get_formats, convert_ebook, list_books error paths, __init__
validation, _parse_book_id fallback branches, _parse_formats, and a handful
of split-mode edge branches.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from libris.calibre.local import (
    LocalCalibre,
    _metadata_flags,
    _normalise_book_entry,
    _parse_book_id,
    _parse_formats,
)
from libris.config import CalibreConfig
from libris.exceptions import CalibreImportError, ConversionError


def _make_backend(library: Path, book_files: Path | None = None) -> LocalCalibre:
    cfg = CalibreConfig(mode="local", library_db_path=library, book_file_path=book_files)
    return LocalCalibre(cfg)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


# ---------------------------------------------------------------------------
# __init__ validation
# ---------------------------------------------------------------------------

class TestInitValidation:
    def test_missing_library_db_path_raises(self):
        cfg = CalibreConfig(mode="local", library_db_path=None)
        with pytest.raises(ValueError, match="library_db_path"):
            LocalCalibre(cfg)


# ---------------------------------------------------------------------------
# add_book error paths
# ---------------------------------------------------------------------------

class TestAddBookErrors:
    def test_nonzero_rc_raises(self, tmp_path):
        backend = _make_backend(tmp_path)
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            with pytest.raises(CalibreImportError, match="calibredb add failed"):
                backend.add_book(epub)

    def test_were_not_added_stderr_raises(self, tmp_path):
        backend = _make_backend(tmp_path)
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch(
            "subprocess.run",
            return_value=_proc(stdout="", stderr="1 books were not added"),
        ):
            with pytest.raises(CalibreImportError, match="refused to add"):
                backend.add_book(epub)

    def test_no_book_id_in_stdout_raises(self, tmp_path):
        backend = _make_backend(tmp_path)
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch("subprocess.run", return_value=_proc(stdout="nothing useful here")):
            with pytest.raises(CalibreImportError, match="no book ID"):
                backend.add_book(epub)


# ---------------------------------------------------------------------------
# _get_format_paths error paths
# ---------------------------------------------------------------------------

class TestGetFormatPathsErrors:
    def test_nonzero_rc_returns_empty(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            assert backend._get_format_paths(1) == []

    def test_formats_as_bare_string(self, tmp_path):
        """calibredb sometimes reports a single format as a bare string, not a list."""
        backend = _make_backend(tmp_path)
        raw = json.dumps([{"id": 1, "formats": "/lib/A/B (1)/b.epub"}])
        with patch("subprocess.run", return_value=_proc(stdout=raw)):
            paths = backend._get_format_paths(1)
        assert paths == [Path("/lib/A/B (1)/b.epub")]

    def test_json_parse_error_returns_empty(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(stdout="not json")):
            assert backend._get_format_paths(1) == []


class TestDbFormatPathsErrors:
    def test_sqlite_error_returns_empty_dict(self, tmp_path):
        library = tmp_path / "library"
        library.mkdir()
        # Write garbage bytes so sqlite3 raises on read.
        (library / "metadata.db").write_bytes(b"not a real sqlite database")
        backend = _make_backend(library)
        assert backend._db_format_paths() == {}


# ---------------------------------------------------------------------------
# _relocate_to_book_files edge branches
# ---------------------------------------------------------------------------

class TestRelocateToBookFilesEdges:
    def test_format_path_does_not_exist_warns_and_returns(self, tmp_path, caplog):
        import logging

        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()
        backend = _make_backend(library, book_files)

        missing = library / "Author" / "Title (1)" / "gone.epub"
        raw = json.dumps([{"id": 1, "formats": [str(missing)]}])
        with patch("subprocess.run", return_value=_proc(stdout=raw)):
            with caplog.at_level(logging.WARNING, logger="libris.calibre.local"):
                backend._relocate_to_book_files(1)
        assert "relocate_src_dir_missing" in caplog.text

    def test_skips_subdirectories_in_book_dir(self, tmp_path):
        """Only files are moved; subdirectories under the book dir are left alone."""
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        book_dir = library / "Author" / "Title (1)"
        book_dir.mkdir(parents=True)
        epub = book_dir / "book.epub"
        epub.write_text("epub")
        subdir = book_dir / "leftover_subdir"
        subdir.mkdir()
        (subdir / "nested.txt").write_text("nested")

        backend = _make_backend(library, book_files)
        raw = json.dumps([{"id": 1, "formats": [str(epub)]}])
        with patch("subprocess.run", return_value=_proc(stdout=raw)):
            backend._relocate_to_book_files(1)

        assert (book_files / "Author" / "Title (1)" / "book.epub").exists()
        # subdir left behind under library -> rmdir() fails silently (OSError caught)
        assert subdir.exists()
        assert book_dir.exists(), "book dir must remain (non-empty due to leftover subdir)"


class TestRelocateCoverEdges:
    def test_fmt_path_not_under_library_is_skipped(self, tmp_path):
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()
        backend = _make_backend(library, book_files)

        outside = tmp_path / "elsewhere" / "book.epub"
        raw = json.dumps([{"id": 1, "formats": [str(outside)]}])
        with patch("subprocess.run", return_value=_proc(stdout=raw)):
            backend._relocate_cover(1)  # must not raise


# ---------------------------------------------------------------------------
# set_metadata / set_authors book_id guard + split sync
# ---------------------------------------------------------------------------

class TestSetMetadataGuard:
    def test_negative_book_id_skips(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run") as mock_run:
            backend.set_metadata(-1, MagicMock())
            mock_run.assert_not_called()


class TestSetAuthorsSplitSync:
    def test_split_mode_syncs_book_files_dir(self, tmp_path):
        library = tmp_path / "calibre-db"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()

        old_dir = book_files / "Unknown" / "Book (5)"
        old_dir.mkdir(parents=True)
        (old_dir / "book.epub").write_bytes(b"x")

        backend = _make_backend(library, book_files)

        responses = []

        def fake_run(cmd, **kwargs):
            if "set_metadata" in cmd:
                return _proc(stdout="")
            path = (
                str(library / "Unknown" / "Book (5)" / "x.epub")
                if not responses
                else str(library / "Author" / "New Title (5)" / "x.epub")
            )
            responses.append(path)
            return _proc(stdout=json.dumps([{"id": 5, "formats": [path]}]))

        with patch("subprocess.run", side_effect=fake_run):
            ok = backend.set_authors(5, ["Author"])

        assert ok is True
        new_dir = book_files / "Author" / "New Title (5)"
        assert new_dir.exists(), "set_authors must mirror the rename under book_files"
        assert not old_dir.exists()


class TestBookRelDir:
    def test_returns_none_when_no_format_paths(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(stdout="[]")):
            assert backend._book_rel_dir(1) is None

    def test_skips_path_not_under_library(self, tmp_path):
        library = tmp_path / "library"
        library.mkdir()
        backend = _make_backend(library)
        outside = tmp_path / "elsewhere" / "book.epub"
        raw = json.dumps([{"id": 1, "formats": [str(outside)]}])
        with patch("subprocess.run", return_value=_proc(stdout=raw)):
            assert backend._book_rel_dir(1) is None


# ---------------------------------------------------------------------------
# export_book / _export_from_book_files edge branches
# ---------------------------------------------------------------------------

class TestExportFromBookFilesEdges:
    def test_json_decode_error_returns_empty(self, tmp_path):
        library = tmp_path / "library"
        book_files = tmp_path / "books"
        backend = _make_backend(library, book_files)
        dest = tmp_path / "dest"
        dest.mkdir()
        with patch(
            "libris.calibre.local.subprocess.run",
            return_value=_proc(stdout="not json"),
        ):
            assert backend._export_from_book_files(1, dest) == []

    def test_formats_as_bare_string_is_normalised(self, tmp_path):
        library = tmp_path / "library"
        book_files = tmp_path / "books"
        library.mkdir()
        book_files.mkdir()
        backend = _make_backend(library, book_files)
        dest = tmp_path / "dest"
        dest.mkdir()

        rel = "Author/Book (1)/book.epub"
        real = book_files / rel
        real.parent.mkdir(parents=True)
        real.write_bytes(b"data")

        raw = json.dumps({"id": 1, "formats": str(library / rel)})
        # Note: formats is a bare string at the top level of a single dict,
        # matching production's "rows[0].get('formats', ...)" access pattern.
        with patch(
            "libris.calibre.local.subprocess.run",
            return_value=_proc(stdout=json.dumps([json.loads(raw)])),
        ):
            exported = backend._export_from_book_files(1, dest)
        assert len(exported) == 1
        assert exported[0].name == "book.epub"

    def test_nonzero_rc_raises(self, tmp_path):
        library = tmp_path / "library"
        book_files = tmp_path / "books"
        backend = _make_backend(library, book_files)
        dest = tmp_path / "dest"
        dest.mkdir()
        with patch(
            "libris.calibre.local.subprocess.run",
            return_value=_proc(returncode=1, stderr="boom"),
        ):
            with pytest.raises(CalibreImportError, match="calibredb list failed"):
                backend._export_from_book_files(1, dest)


# ---------------------------------------------------------------------------
# remove_book
# ---------------------------------------------------------------------------

class TestRemoveBook:
    def test_success(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc()) as mock_run:
            backend.remove_book(7)
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["calibredb", "remove", "7"]

    def test_failure_raises(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            with pytest.raises(CalibreImportError, match="calibredb remove failed"):
                backend.remove_book(7)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_parses_comma_separated_ids(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(stdout="1,2,3\n")):
            assert backend.search("title:Dune") == [1, 2, 3]

    def test_empty_stdout_returns_empty_list(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(returncode=1, stdout="")):
            assert backend.search("title:Nope") == []

    def test_unparseable_stdout_returns_empty_list(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(stdout="totally, not, ids")):
            assert backend.search("title:Weird") == []


# ---------------------------------------------------------------------------
# add_format
# ---------------------------------------------------------------------------

class TestAddFormat:
    def test_success(self, tmp_path):
        backend = _make_backend(tmp_path)
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch("subprocess.run", return_value=_proc()) as mock_run:
            backend.add_format(3, epub)
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["calibredb", "add_format", "3"]

    def test_failure_raises(self, tmp_path):
        backend = _make_backend(tmp_path)
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            with pytest.raises(CalibreImportError, match="add_format failed"):
                backend.add_format(3, epub)


# ---------------------------------------------------------------------------
# get_formats
# ---------------------------------------------------------------------------

class TestGetFormats:
    def test_extracts_extensions(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(stdout="['/x/book.epub', '/x/book.m4b']")):
            assert backend.get_formats(1) == {"epub", "m4b"}


# ---------------------------------------------------------------------------
# list_books error paths
# ---------------------------------------------------------------------------

class TestListBooksErrors:
    def test_nonzero_rc_returns_empty(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            assert backend.list_books() == []

    def test_json_decode_error_returns_empty(self, tmp_path):
        backend = _make_backend(tmp_path)
        with patch("subprocess.run", return_value=_proc(stdout="not json")):
            assert backend.list_books() == []


# ---------------------------------------------------------------------------
# convert_ebook
# ---------------------------------------------------------------------------

class TestConvertEbook:
    def test_success(self, tmp_path):
        backend = _make_backend(tmp_path)
        src = tmp_path / "book.epub"
        dst = tmp_path / "book.mobi"
        with patch("subprocess.run", return_value=_proc()) as mock_run:
            backend.convert_ebook(src, dst)
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "ebook-convert"

    def test_failure_raises(self, tmp_path):
        backend = _make_backend(tmp_path)
        src = tmp_path / "book.epub"
        dst = tmp_path / "book.mobi"
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            with pytest.raises(ConversionError, match="ebook-convert failed"):
                backend.convert_ebook(src, dst)


# ---------------------------------------------------------------------------
# _metadata_flags — series_index
# ---------------------------------------------------------------------------

class TestMetadataFlagsSeriesIndex:
    def test_series_index_included_when_not_none(self):
        result = MagicMock()
        result.title = "Dune"
        result.best.candidate.authors = ["Frank Herbert"]
        result.publisher = None
        result.description = None
        result.language = None
        result.isbn = None
        result.series = "Dune Chronicles"
        result.series_index = 1.0
        result.year = ""

        flags = [f for pair in _metadata_flags(result) for f in pair]
        assert "series_index:1.0" in flags
        assert "series:Dune Chronicles" in flags


# ---------------------------------------------------------------------------
# _normalise_book_entry — list/str variants
# ---------------------------------------------------------------------------

class TestNormaliseBookEntry:
    def test_authors_as_list(self):
        entry = _normalise_book_entry({"id": 1, "title": "Dune", "authors": ["Frank Herbert"]})
        assert entry["authors"] == ["Frank Herbert"]

    def test_authors_as_ampersand_string(self):
        entry = _normalise_book_entry(
            {"id": 1, "title": "Good Omens", "authors": "Terry Pratchett & Neil Gaiman"}
        )
        assert entry["authors"] == ["Terry Pratchett", "Neil Gaiman"]

    def test_formats_as_bare_string(self):
        entry = _normalise_book_entry({"id": 1, "title": "Dune", "formats": "/x/dune.epub"})
        assert entry["formats"] == ["epub"]
        assert entry["format_paths"] == ["/x/dune.epub"]

    def test_no_formats_key_defaults_empty(self):
        entry = _normalise_book_entry({"id": 1, "title": "Dune"})
        assert entry["formats"] == []
        assert entry["format_paths"] == []


# ---------------------------------------------------------------------------
# _parse_formats — direct
# ---------------------------------------------------------------------------

class TestParseFormats:
    def test_extracts_dot_extensions_case_insensitive(self):
        assert _parse_formats("['/x/Book.EPUB', '/y/book.M4B']") == {"epub", "m4b"}

    def test_no_extensions_returns_empty_set(self):
        assert _parse_formats("no formats here") == set()


# ---------------------------------------------------------------------------
# _parse_book_id — fallback branches
# ---------------------------------------------------------------------------

class TestParseBookIdFallbacks:
    def test_book_id_colon_variant(self):
        assert _parse_book_id("Some noise\nbook id: 42\n") == 42

    def test_standalone_integer_line(self):
        assert _parse_book_id("Importing...\n42\nDone") == 42

    def test_no_id_found_returns_negative_one(self):
        assert _parse_book_id("0.2 seconds\nDeDRM removed") == -1

    def test_canonical_variant_no_space(self):
        assert _parse_book_id("Added book ids:42") == 42
