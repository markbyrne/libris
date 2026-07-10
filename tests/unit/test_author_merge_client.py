"""Unit tests for the author-merge logic shared by both calibredb backends:

  - libris.calibre.base._replace_author_tokens — the pure token-replace /
    co-author-preserving / de-duplicating function.
  - CalibreBackend.merge_authors — the concrete orchestration method that
    LocalCalibre and DockerCalibre both inherit unmodified.
  - LocalCalibre.set_authors — the calibredb subprocess call itself.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from libris.calibre.base import CalibreBackend, _replace_author_tokens
from libris.calibre.local import LocalCalibre
from libris.config import CalibreConfig

# ---------------------------------------------------------------------------
# _replace_author_tokens — pure logic
# ---------------------------------------------------------------------------

class TestReplaceAuthorTokens:
    def test_exact_spelling_variant_replaced(self):
        result = _replace_author_tokens(["D. J. MacHale"], ["D. J. MacHale"], "D.J. MacHale")
        assert result == ["D.J. MacHale"]

    def test_case_and_space_insensitive_match(self):
        # "D.J. MacHale" (no inner spaces) vs "D. J. MacHale" (spaced) — same
        # person once whitespace is stripped and case-folded.
        result = _replace_author_tokens(["d.j.  machale"], ["D. J. MacHale"], "D.J. MacHale")
        assert result == ["D.J. MacHale"]

    def test_coauthor_preserved(self):
        result = _replace_author_tokens(
            ["A. Author", "D. J. MacHale"], ["D. J. MacHale"], "D.J. MacHale"
        )
        assert result == ["A. Author", "D.J. MacHale"]

    def test_no_match_returns_none(self):
        assert _replace_author_tokens(["Frank Herbert"], ["D. J. MacHale"], "D.J. MacHale") is None

    def test_already_canonical_returns_none_idempotent(self):
        # Book already has to_name and nothing else — no change, even if
        # to_name happens to also appear in from_names (harmless per spec).
        result = _replace_author_tokens(
            ["D.J. MacHale"], ["D. J. MacHale", "D.J. MacHale"], "D.J. MacHale"
        )
        assert result is None

    def test_dedupes_when_both_variants_present(self):
        result = _replace_author_tokens(
            ["D. J. MacHale", "D.J. MacHale"], ["D. J. MacHale"], "D.J. MacHale"
        )
        assert result == ["D.J. MacHale"]

    def test_multiple_from_names(self):
        result = _replace_author_tokens(
            ["D J MacHale"], ["D. J. MacHale", "D.J. MacHale", "DJ MacHale"], "D.J. MacHale"
        )
        assert result == ["D.J. MacHale"]


# ---------------------------------------------------------------------------
# CalibreBackend.merge_authors — orchestration, backend-agnostic
# ---------------------------------------------------------------------------

def _mock_backend(library: list[dict]) -> MagicMock:
    """A MagicMock CalibreBackend whose search()/list_books()/set_authors()
    are backed by *library* (mutated in place), with the real merge_authors
    bound so orchestration itself is under test, not a re-implementation.
    """
    mock = MagicMock()
    mock.list_books.return_value = [dict(b) for b in library]

    def _search(query: str) -> list[int]:
        import re as _re
        m = _re.search(r'authors:"([^"]*)"', query)
        name = m.group(1) if m else ""
        return [b["id"] for b in library if any(name.lower() in a.lower() for a in b["authors"])]

    mock.search.side_effect = _search

    def _set_authors(book_id: int, authors: list[str]) -> bool:
        for b in library:
            if b["id"] == book_id:
                b["authors"] = authors
                mock.list_books.return_value = [dict(x) for x in library]
                return True
        return False

    mock.set_authors.side_effect = _set_authors
    mock.merge_authors = CalibreBackend.merge_authors.__get__(mock, CalibreBackend)
    return mock


class TestMergeAuthorsOrchestration:
    def test_renames_all_matching_books(self):
        library = [
            {"id": 1, "authors": ["D. J. MacHale"]},
            {"id": 2, "authors": ["D.J. MacHale"]},
            {"id": 3, "authors": ["Frank Herbert"]},
        ]
        backend = _mock_backend(library)
        renamed = backend.merge_authors(["D. J. MacHale", "D.J. MacHale"], "D.J. MacHale")
        assert renamed == 1
        assert library[0]["authors"] == ["D.J. MacHale"]
        assert library[2]["authors"] == ["Frank Herbert"]

    def test_empty_to_name_is_noop(self):
        library = [{"id": 1, "authors": ["D. J. MacHale"]}]
        backend = _mock_backend(library)
        assert backend.merge_authors(["D. J. MacHale"], "") == 0
        assert library[0]["authors"] == ["D. J. MacHale"]

    def test_empty_from_names_is_noop(self):
        library = [{"id": 1, "authors": ["D. J. MacHale"]}]
        backend = _mock_backend(library)
        assert backend.merge_authors([], "D.J. MacHale") == 0
        assert library[0]["authors"] == ["D. J. MacHale"]

    def test_set_authors_failure_is_best_effort(self):
        library = [
            {"id": 1, "authors": ["D. J. MacHale"]},
            {"id": 2, "authors": ["D. J. MacHale"]},
        ]
        backend = _mock_backend(library)
        # First call fails, second succeeds — merge_authors must not crash
        # and must still report the one success.
        backend.set_authors.side_effect = [False, True]
        # Re-wire list_books to keep returning the (unmutated-by-failure) library
        backend.list_books.return_value = [dict(b) for b in library]
        renamed = backend.merge_authors(["D. J. MacHale"], "D.J. MacHale")
        assert renamed == 1

    def test_search_exception_does_not_crash(self):
        library = [{"id": 1, "authors": ["D. J. MacHale"]}]
        backend = _mock_backend(library)
        backend.search.side_effect = RuntimeError("calibredb not found")
        assert backend.merge_authors(["D. J. MacHale"], "D.J. MacHale") == 0

    def test_idempotent(self):
        library = [{"id": 1, "authors": ["D. J. MacHale"]}]
        backend = _mock_backend(library)
        first = backend.merge_authors(["D. J. MacHale", "D.J. MacHale"], "D.J. MacHale")
        second = backend.merge_authors(["D. J. MacHale", "D.J. MacHale"], "D.J. MacHale")
        assert first == 1
        assert second == 0


# ---------------------------------------------------------------------------
# LocalCalibre.set_authors — the calibredb subprocess call
# ---------------------------------------------------------------------------

def _make_local(library: Path) -> LocalCalibre:
    cfg = CalibreConfig(mode="local", library_db_path=library)
    return LocalCalibre(cfg)


def _proc(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    mock = MagicMock()
    mock.returncode = returncode
    mock.stdout = stdout
    mock.stderr = stderr
    return mock


class TestLocalSetAuthors:
    def test_invokes_calibredb_set_metadata_with_authors_field(self, tmp_path):
        backend = _make_local(tmp_path)
        with patch("subprocess.run", return_value=_proc()) as mock_run:
            ok = backend.set_authors(42, ["D.J. MacHale"])
        assert ok is True
        cmd = mock_run.call_args[0][0]
        assert cmd[:3] == ["calibredb", "set_metadata", "42"]
        assert "--field" in cmd
        assert "authors:D.J. MacHale" in cmd

    def test_joins_multiple_authors_with_ampersand(self, tmp_path):
        backend = _make_local(tmp_path)
        with patch("subprocess.run", return_value=_proc()) as mock_run:
            backend.set_authors(1, ["A. Author", "D.J. MacHale"])
        cmd = mock_run.call_args[0][0]
        assert "authors:A. Author & D.J. MacHale" in cmd

    def test_failure_returns_false(self, tmp_path):
        backend = _make_local(tmp_path)
        with patch("subprocess.run", return_value=_proc(returncode=1, stderr="boom")):
            assert backend.set_authors(1, ["X"]) is False

    def test_negative_book_id_short_circuits(self, tmp_path):
        backend = _make_local(tmp_path)
        with patch("subprocess.run") as mock_run:
            assert backend.set_authors(-1, ["X"]) is False
            mock_run.assert_not_called()
