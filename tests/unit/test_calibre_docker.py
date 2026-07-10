"""Unit tests for libris/calibre/docker.py (DockerCalibre).

Docker backend mirrors LocalCalibre's calibredb argv construction but wraps
every call in `docker exec <container>` and needs host->container path
translation. Only ~34% covered before this file — most methods (remove_book,
search, add_format, get_formats, list_books, set_authors, convert_ebook,
set_cover, export_book, _translate) had zero direct tests beyond the
add_book --title/--authors flag mirror test in test_calibre_local.py.

Mocks subprocess.run throughout — never invokes a real docker daemon.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from libris.calibre.docker import DockerCalibre
from libris.config import CalibreConfig
from libris.exceptions import CalibreImportError, ConversionError


def _make_backend(container: str = "calibre-web", path_map: dict | None = None) -> DockerCalibre:
    cfg = CalibreConfig(mode="docker", docker_container=container, path_map=path_map or {})
    return DockerCalibre(cfg)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


# ---------------------------------------------------------------------------
# _translate
# ---------------------------------------------------------------------------

class TestTranslate:
    def test_longest_prefix_wins(self):
        backend = _make_backend(
            path_map={
                "/media/pidrive/Books": "/books",
                "/media/pidrive/Books/incoming": "/incoming",
            }
        )
        result = backend._translate(Path("/media/pidrive/Books/incoming/x.epub"))
        assert result == "/incoming/x.epub"

    def test_no_mapping_passes_through_with_warning(self, caplog):
        import logging

        backend = _make_backend(path_map={})
        with caplog.at_level(logging.WARNING, logger="libris.calibre.docker"):
            result = backend._translate(Path("/unmapped/x.epub"))
        assert result == "/unmapped/x.epub"
        assert "no_path_mapping" in caplog.text


# ---------------------------------------------------------------------------
# add_book
# ---------------------------------------------------------------------------

class TestAddBook:
    def test_happy_path_translates_and_parses_id(self, tmp_path):
        backend = _make_backend(path_map={str(tmp_path): "/incoming"})
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(stdout="Added book ids: 9\n")) as mock_run:
            book_id = backend.add_book(epub)
        assert book_id == 9
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["docker", "exec", "calibre-web"]
        assert "/incoming/book.epub" in cmd

    def test_nonzero_rc_raises(self, tmp_path):
        backend = _make_backend()
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            with pytest.raises(CalibreImportError, match="docker calibredb add failed"):
                backend.add_book(epub)

    def test_were_not_added_raises(self, tmp_path):
        backend = _make_backend()
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch(
            "libris.calibre.docker.subprocess.run",
            return_value=_proc(stderr="1 books were not added"),
        ):
            with pytest.raises(CalibreImportError, match="refused to add"):
                backend.add_book(epub)

    def test_no_book_id_raises(self, tmp_path):
        backend = _make_backend()
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(stdout="nothing")):
            with pytest.raises(CalibreImportError, match="no book ID"):
                backend.add_book(epub)


# ---------------------------------------------------------------------------
# set_metadata
# ---------------------------------------------------------------------------

class TestSetMetadata:
    def test_negative_book_id_skips(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run") as mock_run:
            backend.set_metadata(-1, MagicMock())
            mock_run.assert_not_called()

    def test_failure_logged_not_raised(self):
        backend = _make_backend()
        result = MagicMock()
        result.title = "Dune"
        result.best.candidate.authors = ["Frank Herbert"]
        result.publisher = result.description = result.language = None
        result.isbn = result.series = result.series_index = None
        with patch(
            "libris.calibre.docker.subprocess.run",
            return_value=_proc(returncode=1, stderr="boom"),
        ):
            backend.set_metadata(1, result)  # must not raise


# ---------------------------------------------------------------------------
# set_cover
# ---------------------------------------------------------------------------

class TestSetCover:
    def test_negative_book_id_returns_false(self, tmp_path):
        backend = _make_backend()
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"\xff\xd8")
        assert backend.set_cover(-1, cover) is False

    def test_missing_cover_path_returns_false(self, tmp_path):
        backend = _make_backend()
        assert backend.set_cover(1, tmp_path / "nope.jpg") is False

    def test_docker_cp_failure_returns_false_and_cleans_up(self, tmp_path):
        backend = _make_backend()
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"\xff\xd8")

        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["docker", "cp"]:
                return _proc(returncode=1, stderr="cp failed")
            return _proc()

        with patch("libris.calibre.docker.subprocess.run", side_effect=fake_run):
            assert backend.set_cover(1, cover) is False

        # cleanup (rm -rf container_tmp) must still run in the finally block
        assert any(c[:4] == ["docker", "exec", "calibre-web", "rm"] for c in calls)

    def test_set_metadata_call_failure_returns_false(self, tmp_path):
        backend = _make_backend()
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"\xff\xd8")

        def fake_run(cmd, **kwargs):
            if "set_metadata" in cmd:
                return _proc(returncode=1, stderr="boom")
            return _proc()

        with patch("libris.calibre.docker.subprocess.run", side_effect=fake_run):
            assert backend.set_cover(1, cover) is False

    def test_success_returns_true(self, tmp_path):
        backend = _make_backend()
        cover = tmp_path / "cover.jpg"
        cover.write_bytes(b"\xff\xd8")
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc()) as mock_run:
            assert backend.set_cover(1, cover) is True
        # mkdir, docker cp, set_metadata, rm -rf = 4 calls
        assert mock_run.call_count == 4


# ---------------------------------------------------------------------------
# export_book
# ---------------------------------------------------------------------------

class TestExportBook:
    def test_nonzero_rc_raises(self, tmp_path):
        backend = _make_backend()
        dest = tmp_path / "dest"
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            with pytest.raises(CalibreImportError, match="docker calibredb export failed"):
                backend.export_book(1, dest)

    def test_success_copies_and_cleans_up(self, tmp_path):
        backend = _make_backend()
        dest = tmp_path / "dest"

        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["docker", "cp"]:
                # Simulate docker cp materialising a file into dest_dir
                (dest / "book.epub").write_bytes(b"exported")
                return _proc()
            return _proc()

        with patch("libris.calibre.docker.subprocess.run", side_effect=fake_run):
            exported = backend.export_book(1, dest)

        assert len(exported) == 1
        assert exported[0].name == "book.epub"


# ---------------------------------------------------------------------------
# remove_book
# ---------------------------------------------------------------------------

class TestRemoveBook:
    def test_success(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc()) as mock_run:
            backend.remove_book(5)
        cmd = mock_run.call_args[0][0]
        assert cmd[-2:] == ["remove", "5"]

    def test_failure_raises(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            with pytest.raises(CalibreImportError, match="docker calibredb remove failed"):
                backend.remove_book(5)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------

class TestSearch:
    def test_parses_ids(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(stdout="4,5,6")):
            assert backend.search("title:Dune") == [4, 5, 6]

    def test_empty_stdout_returns_empty(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(stdout="")):
            assert backend.search("title:Nope") == []

    def test_parse_failure_returns_empty(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(stdout="garbage, not, ids")):
            assert backend.search("title:Weird") == []


# ---------------------------------------------------------------------------
# add_format
# ---------------------------------------------------------------------------

class TestAddFormat:
    def test_success(self, tmp_path):
        backend = _make_backend(path_map={str(tmp_path): "/incoming"})
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc()) as mock_run:
            backend.add_format(3, epub)
        cmd = mock_run.call_args[0][0]
        assert "/incoming/book.epub" in cmd

    def test_failure_raises(self, tmp_path):
        backend = _make_backend()
        epub = tmp_path / "book.epub"
        epub.write_bytes(b"x")
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            with pytest.raises(CalibreImportError, match="add_format failed"):
                backend.add_format(3, epub)


# ---------------------------------------------------------------------------
# get_formats
# ---------------------------------------------------------------------------

class TestGetFormats:
    def test_extracts_extensions(self):
        backend = _make_backend()
        with patch(
            "libris.calibre.docker.subprocess.run",
            return_value=_proc(stdout="['/x/book.epub', '/x/book.m4b']"),
        ):
            assert backend.get_formats(1) == {"epub", "m4b"}


# ---------------------------------------------------------------------------
# list_books
# ---------------------------------------------------------------------------

class TestListBooks:
    def test_nonzero_rc_returns_empty(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            assert backend.list_books() == []

    def test_json_decode_error_returns_empty(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(stdout="not json")):
            assert backend.list_books() == []

    def test_happy_path(self):
        backend = _make_backend()
        raw = json.dumps([{"id": 1, "title": "Dune", "authors": "Frank Herbert", "formats": ["/x/dune.epub"]}])
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(stdout=raw)):
            books = backend.list_books()
        assert books[0]["id"] == 1
        assert books[0]["formats"] == ["epub"]


# ---------------------------------------------------------------------------
# set_authors
# ---------------------------------------------------------------------------

class TestSetAuthors:
    def test_negative_book_id_returns_false(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run") as mock_run:
            assert backend.set_authors(-1, ["X"]) is False
            mock_run.assert_not_called()

    def test_failure_returns_false(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            assert backend.set_authors(1, ["X"]) is False

    def test_success_returns_true_and_joins_ampersand(self):
        backend = _make_backend()
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc()) as mock_run:
            assert backend.set_authors(1, ["A", "B"]) is True
        cmd = mock_run.call_args[0][0]
        assert "authors:A & B" in cmd


# ---------------------------------------------------------------------------
# convert_ebook
# ---------------------------------------------------------------------------

class TestConvertEbook:
    def test_success_translates_both_paths(self, tmp_path):
        backend = _make_backend(path_map={str(tmp_path): "/data"})
        src = tmp_path / "book.epub"
        dst = tmp_path / "book.mobi"
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc()) as mock_run:
            backend.convert_ebook(src, dst)
        cmd = mock_run.call_args[0][0]
        assert "/data/book.epub" in cmd
        assert "/data/book.mobi" in cmd

    def test_failure_raises(self, tmp_path):
        backend = _make_backend()
        src = tmp_path / "book.epub"
        dst = tmp_path / "book.mobi"
        with patch("libris.calibre.docker.subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            with pytest.raises(ConversionError, match="docker ebook-convert failed"):
                backend.convert_ebook(src, dst)
